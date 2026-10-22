// SPDX-License-Identifier: GPL-3.0-or-later
#include "persist.h"

#include "hero.h"
#include "hud.h"
#include "i18n.h"
#include "journal.h"
#include "net.h"
#include "npcs.h"
#include "log.h"
#include "remote.h"
#include "explore.h"

#include <Game.h>
#include <Windows.h>
#include <cstdio>
#include <cstring>
#include <algorithm>
#include <string>
#include <vector>

namespace g3t::persist {
namespace {
// Game.dll of this build (Steam 1037023, CP 1.75.14); see explore.cpp. Both functions are __stdcall(this, name)
// and start with 9 bytes of complete, position-independent instructions, which the trampoline runs before
// jumping back:
//   SaveGame: 55 / 8b ec / 83 e4 f8 / 83 ec 1c        push ebp; mov ebp,esp; and esp,-8; sub esp,1c
//   LoadGame: 55 / 8b ec / 83 e4 f8 / 8b 45 08        push ebp; mov ebp,esp; and esp,-8; mov eax,[ebp+8]
struct Internal { uintptr_t rva; char const* bytes; };
const Internal SaveGameFn{0xa9e90, "558bec83e4f883ec1c535657"};
const Internal LoadGameFn{0xa9a10, "558bec83e4f88b450883ec5c83780400"};
constexpr size_t Stolen = 9;

using FileFn = bool(__stdcall*)(void* self, bCString const* name);
FileFn originalSave = nullptr;
FileFn originalLoad = nullptr;
unsigned char* saveTarget = nullptr;
unsigned char* loadTarget = nullptr;
bool installed = false;
bool writing = true;
unsigned saves = 0, loads = 0, sidecarsWritten = 0, sidecarsRead = 0;
std::string lastSave, lastLoad;
unsigned recordsWritten = 0, recordsApplied = 0;
std::string pendingRecord;   // client: hero record to apply once the loaded world runs
uint32_t pendingSince = 0;

unsigned char* Resolve(Internal const& fn)
{
    HMODULE game = GetModuleHandleA("Game.dll");
    if (!game) return nullptr;
    auto* code = reinterpret_cast<unsigned char*>(reinterpret_cast<uintptr_t>(game) + fn.rva);
    for (size_t i = 0; fn.bytes[2 * i]; ++i) {
        unsigned value = 0;
        sscanf_s(fn.bytes + 2 * i, "%2x", &value);
        if (code[i] != value) return nullptr;
    }
    return code;
}

// Jump from `target` to `detour`; the returned trampoline runs the stolen bytes and continues in the original.
void* Hook(unsigned char* target, void* detour)
{
    auto* trampoline = static_cast<unsigned char*>(VirtualAlloc(nullptr, 32, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE));
    if (!trampoline) return nullptr;
    std::memcpy(trampoline, target, Stolen);
    trampoline[Stolen] = 0xE9;
    const int32_t back = static_cast<int32_t>(reinterpret_cast<intptr_t>(target + Stolen) - reinterpret_cast<intptr_t>(trampoline + Stolen + 5));
    std::memcpy(trampoline + Stolen + 1, &back, 4);
    DWORD old = 0;
    if (!VirtualProtect(target, Stolen, PAGE_EXECUTE_READWRITE, &old)) return nullptr;
    target[0] = 0xE9;
    const int32_t to = static_cast<int32_t>(reinterpret_cast<intptr_t>(detour) - reinterpret_cast<intptr_t>(target + 5));
    std::memcpy(target + 1, &to, 4);
    for (size_t i = 5; i < Stolen; ++i) target[i] = 0x90;
    VirtualProtect(target, Stolen, old, &old);
    FlushInstructionCache(GetCurrentProcess(), target, Stolen);
    return trampoline;
}

std::string SidecarPath(bCString const* name)
{
    std::string folder = gCWorld::GetSaveGameFolder().GetText();
    if (!folder.empty() && folder.back() != '\\' && folder.back() != '/') folder += '\\';
    return folder + name->GetText() + ".g3t";
}

// --- The campaign travels to players in another world ------------------------------------------------------
// The host's current campaign is the save it last loaded or saved. A client that does not play the host's world
// gets its three files (.g3savcpx, .g3savcpxdat, .g3t), stores them under G3T_host_<name> (never over one of its
// own saves) and loads that save; its own hero record for that name, if any, then goes on top (D3a).
char const* const Extensions[net::FileCount] = {".g3savcpx", ".g3savcpxdat", ".g3t"};
std::string campaign;                    // host: current campaign save name
bool transferring = true;                // host: QA negative control `campaignoff`
bool autoLoad = true;
std::vector<uint8_t> incoming[net::FileCount];
std::string pendingLoad;                 // client: received campaign to load
unsigned filesSent = 0, filesReceived = 0, autoLoads = 0;
std::string lastReceived = "-";

// --- Which world each machine plays; the world gate (PLAN-2026-10-22 0.1/0.2) --------------------------------
// A machine is in the startup menu, in a new game of its own or in a loaded save. The host numbers its worlds: a new
// epoch when a load begins, when it leaves a world and when a new game runs (the high 16 bits are random per run, so a
// restarted host does not repeat one), and tells every client. A client that does not play that world answers
// WorldOther and gets the host's save, as at a join, and loads it. Only a client that plays the host's world (it
// loaded the host's current campaign, or both play a new game the host has not saved) passes the gate
// (net::SetInWorld). Before (spieltest 21.10.), the save went out once, at the join, and only if the host had one: a
// load later, or a client in a new game joining a host still in the menu, left two worlds.
uint8_t localKind = net::KindMenu;
std::string localSave;           // KindSaved: the save loaded here
std::string loading;             // a load began and its world does not run yet: the save's name
bool newWorld = false;           // a world came up without a load: a new game, or the startup menu
uint32_t settledAt = 0;          // GetTickCount() when the last load's world ran
bool syncing = true;             // host: false after the QA negative control `worldsyncoff`
unsigned worldChanges = 0;
// Host
uint32_t epoch = 0, epochs = 0;
struct Client { uint32_t told = 0; std::string toldCampaign; uint32_t ready = 0; uint32_t sent = 0; unsigned resends = 0; uint32_t hinted = 0; bool wants = false; };
uint32_t savedAt = 0;            // GetTickCount() of the host's last save: the game writes the files after SaveGame returns
Client clients[net::MaxPlayers];
// Client
net::WorldNote hostWorld;        // the host's last WorldHost
uint32_t synced = 0;             // the host epoch this machine plays, 0: none
uint32_t hinted = 0;             // the host epoch the last hint on the screen was about
bool everIn = false;             // this machine was in a host world before
unsigned readies = 0, others = 0;

uint32_t Crc32(uint8_t const* data, size_t size)
{
    static uint32_t table[256];
    static bool ready = false;
    if (!ready) {
        for (uint32_t i = 0; i < 256; ++i) {
            uint32_t c = i;
            for (int k = 0; k < 8; ++k) c = (c & 1) ? 0xEDB88320u ^ (c >> 1) : c >> 1;
            table[i] = c;
        }
        ready = true;
    }
    uint32_t crc = 0xFFFFFFFFu;
    for (size_t i = 0; i < size; ++i) crc = table[(crc ^ data[i]) & 0xFF] ^ (crc >> 8);
    return crc ^ 0xFFFFFFFFu;
}

std::string Folder()
{
    std::string folder = gCWorld::GetSaveGameFolder().GetText();
    if (!folder.empty() && folder.back() != '\\' && folder.back() != '/') folder += '\\';
    return folder;
}

bool ReadAll(std::string const& path, std::vector<uint8_t>& out)
{
    out.clear();
    FILE* file = nullptr;
    if (fopen_s(&file, path.c_str(), "rb") || !file) return false;
    uint8_t buffer[65536];
    size_t n;
    while ((n = fread(buffer, 1, sizeof(buffer), file)) > 0) out.insert(out.end(), buffer, buffer + n);
    fclose(file);
    return true;
}

// Host: false (nothing sent) if the save is not complete on disk yet. A save sent 50 ms after SaveGame returned had an
// empty .g3savcpxdat (stageS3, 22.10.), and the client that loaded it hung.
// Both main files exist, are not empty and kept their sizes for a second (the game may still be writing).
bool CampaignComplete()
{
    static std::string name;
    static uint64_t seen[2] = {};
    static uint32_t since = 0;
    uint64_t now[2] = {};
    for (int f = 0; f < 2; ++f) {
        WIN32_FILE_ATTRIBUTE_DATA info{};
        if (GetFileAttributesExA((Folder() + campaign + Extensions[f]).c_str(), GetFileExInfoStandard, &info))
            now[f] = (static_cast<uint64_t>(info.nFileSizeHigh) << 32) | info.nFileSizeLow;
    }
    if (name != campaign || now[0] != seen[0] || now[1] != seen[1]) {
        name = campaign;
        seen[0] = now[0], seen[1] = now[1];
        since = GetTickCount();
        return false;
    }
    if (!now[0] || !now[1]) {
        Logf("persist: campaign %s is not complete on disk (%s); not sent yet", campaign.c_str(), now[0] ? Extensions[1] : Extensions[0]);
        return false;
    }
    return GetTickCount() - since >= 1000;
}

bool SendCampaign(uint8_t to)
{
    uint32_t sizes[net::FileCount] = {}, crcs[net::FileCount] = {};
    if (!CampaignComplete()) return false;
    for (int f = 0; f < net::FileCount; ++f) {
        std::vector<uint8_t> data;
        if (!ReadAll(Folder() + campaign + Extensions[f], data)) continue;
        sizes[f] = static_cast<uint32_t>(data.size());
        crcs[f] = Crc32(data.data(), data.size());
        for (size_t at = 0; at < data.size(); at += net::MaxChunk) {
            const uint16_t length = static_cast<uint16_t>(std::min<size_t>(net::MaxChunk, data.size() - at));
            net::SendFileChunk(to, static_cast<uint8_t>(f), sizes[f], static_cast<uint32_t>(at), data.data() + at, length);
        }
    }
    net::SendFileDone(to, campaign.c_str(), sizes, crcs);
    ++filesSent;
    Logf("persist: campaign %s sent to player %u (%u + %u + %u bytes)", campaign.c_str(), to, sizes[0], sizes[1], sizes[2]);
    return true;
}

// A save name from the host: letters, digits, underscore, minus and space only (Fable's review 22.10. 1.4: "..\\" left the save
// folder, and a ":" made an NTFS stream, which creates its file, e.g. qa-enable.txt).
bool SafeName(char const* name)
{
    const size_t n = std::strlen(name);
    if (!n || n > 48) return false;
    for (size_t i = 0; i < n; ++i) {
        const char c = name[i];
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '_' || c == '-' || c == ' ')) return false;
    }
    return true;
}

void OnFileChunk(uint8_t file, uint32_t total, uint32_t offset, uint8_t const* data, uint16_t length)
{
    // offset + length could wrap past 2^32 and pass a plain sum check (Fable's review 22.10. 1.1: a heap write).
    if (file >= net::FileCount || total > 64u * 1024 * 1024 || offset > total || length > total - offset) return;
    if (incoming[file].size() != total) incoming[file].assign(total, 0);
    std::memcpy(incoming[file].data() + offset, data, length);
}

void OnFileDone(char const* name, uint32_t const size[net::FileCount], uint32_t const crc[net::FileCount])
{
    if (!SafeName(name)) {
        Logf("persist: a campaign name the host sent is not a plain save name; not stored");
        for (auto& v : incoming) v.clear();
        return;
    }
    const std::string local = std::string("G3T_host_") + name;
    if (!size[0] || !size[1]) {  // a save without its world data must never be loaded (the game hangs)
        Logf("persist: campaign %s arrived without %s; not loaded", name, size[0] ? Extensions[1] : Extensions[0]);
        for (auto& v : incoming) v.clear();
        return;
    }
    for (int f = 0; f < net::FileCount; ++f) {
        if (!size[f]) continue;
        if (incoming[f].size() != size[f] || Crc32(incoming[f].data(), incoming[f].size()) != crc[f]) {
            Logf("persist: campaign %s arrived damaged (file %d); not loaded", name, f);
            for (auto& v : incoming) v.clear();
            return;
        }
    }
    for (int f = 0; f < net::FileCount; ++f) {
        if (!size[f]) continue;
        FILE* out = nullptr;
        const std::string path = Folder() + local + Extensions[f];
        if (fopen_s(&out, path.c_str(), "wb") || !out) { Logf("persist: could not write %s", path.c_str()); return; }
        fwrite(incoming[f].data(), 1, incoming[f].size(), out);
        fclose(out);
        incoming[f].clear();
    }
    ++filesReceived;
    lastReceived = local;
    Logf("persist: campaign %s received as %s (host world %08x)", name, local.c_str(), hostWorld.epoch);
    // First time in this campaign: no record of this player's hero for it yet. If a hero is in play (a new game
    // started before joining), that hero is kept; from the main menu there is none, and the hero in the host's
    // save (a copy of the host's) stays.
    const std::string record = hero::RecordPath(local.c_str());
    if (GetFileAttributesA(record.c_str()) == INVALID_FILE_ATTRIBUTES) {
        gCSession& session = gCSession::GetInstance();
        if (session.GetHero() && session.IsGameRunning() && hero::SaveRecord(record.c_str())) {
            Logf("persist: first time in %s; the current hero is kept as this player's hero", local.c_str());
        } else {
            hud::Message(hud::Warning, i18n::Text(i18n::NoCampaignHero));
            Logf("persist: first time in %s and no hero in play; the host's hero is copied", local.c_str());
        }
    }
    if (autoLoad) pendingLoad = local;
}

bool __stdcall SaveDetour(void* self, bCString const* name)
{
    remote::DespawnAll("save");  // they come back with the next network update
    const bool ok = originalSave(self, name);
    ++saves;
    savedAt = GetTickCount();
    lastSave = name ? name->GetText() : "";
    if (ok && name && writing) {
        const std::string path = SidecarPath(name);
        FILE* file = nullptr;
        if (!fopen_s(&file, path.c_str(), "w") && file) {
            fprintf(file, "G3T 1\n");
            const unsigned entries = journal::Write(file);
            if (net::IsHost()) npcs::WriteCompanions(file);
            fclose(file);
            ++sidecarsWritten;
            Logf("persist: saved %s with %u journal entries", lastSave.c_str(), entries);
            if (net::IsHost()) net::SendCampaign(1, lastSave.c_str());  // every client keeps its own hero for it
            campaign = lastSave;
        } else {
            Logf("persist: could not write the sidecar of %s", lastSave.c_str());
        }
    }
    return ok;
}

char const* KindName(uint8_t kind) { return kind == net::KindNew ? "new-game" : kind == net::KindSaved ? "save" : "menu"; }

void SendAnswer(uint8_t op)
{
    net::WorldNote note;
    note.op = op;
    note.kind = localKind;
    note.epoch = hostWorld.epoch;
    strncpy_s(note.campaign, localSave.c_str(), _TRUNCATE);
    net::SendWorld(note, 1);
    ++(op == net::WorldReady ? readies : others);
}

// Client, once per host world: why the game waits, on the screen.
void Hint()
{
    if (hinted == hostWorld.epoch) return;
    hinted = hostWorld.epoch;
    wchar_t const* text = i18n::Text(hostWorld.kind == net::KindMenu ? i18n::HostInMenu
                                     : hostWorld.kind == net::KindNew ? i18n::HostNewGame
                                     : everIn ? i18n::HostLoadedSave : i18n::LoadingHostSave);
    hud::Message(hud::Warning, text);
}

// Client: does this machine play the host's world? Answers the host either way.
void Evaluate()
{
    if (!net::Joined() || !hostWorld.epoch) return;
    if (hostWorld.flags & net::WorldSyncOff) {  // QA: as before, every client counts as in the host's world
        synced = hostWorld.epoch;
        net::SetInWorld(net::LocalId(), true);
        return;
    }
    if (!loading.empty() || newWorld) return;  // this machine's world is not up yet
    if (synced == hostWorld.epoch || (hostWorld.kind == net::KindNew && localKind == net::KindNew)) {
        synced = hostWorld.epoch;
        everIn = true;
        net::SetInWorld(net::LocalId(), true);
        SendAnswer(net::WorldReady);
        Logf("persist: in the host's world %08x (%s %s)", synced, KindName(localKind), localSave.c_str());
        return;
    }
    net::SetInWorld(net::LocalId(), false);
    SendAnswer(net::WorldOther);
    Logf("persist: not in the host's world %08x (host %s %s, here %s %s)", hostWorld.epoch, KindName(hostWorld.kind), hostWorld.campaign,
         KindName(localKind), localSave.c_str());
    Hint();
}

// Host: tell client `to` the host's world.
void Tell(uint8_t to)
{
    Client& c = clients[to];
    net::WorldNote note;
    note.op = net::WorldHost;
    note.kind = localKind == net::KindMenu ? net::KindMenu : campaign.empty() ? net::KindNew : net::KindSaved;
    note.flags = syncing ? 0 : net::WorldSyncOff;
    note.epoch = epoch;
    strncpy_s(note.campaign, campaign.c_str(), _TRUNCATE);
    net::SendWorld(note, to);
    c.told = epoch;
    c.toldCampaign = campaign;
}

// Host: a new world here (a load begins, the host left its world, or a new game runs). Clients wait at the gate until
// they answer that they play it.
void NewEpoch()
{
    if (!syncing && epoch) return;  // QA: one world for the whole run, as before
    static const uint32_t nonce = ((GetTickCount() * 2654435761u) ^ (GetCurrentProcessId() << 16)) & 0xFFFF0000u;
    epoch = nonce | (++epochs & 0xFFFF);
    for (uint8_t i = 2; i < net::MaxPlayers; ++i) {
        clients[i].resends = 0;
        if (clients[i].ready != epoch) net::SetInWorld(i, false);
    }
    Logf("persist: host world %08x: %s %s", epoch, KindName(localKind), campaign.c_str());
}

// This machine's world begins to change (a load begins; it left a world).
void Changing(uint8_t kind)
{
    localKind = kind;
    localSave.clear();
    ++worldChanges;
    if (net::IsHost()) {
        if (kind == net::KindMenu) campaign.clear();
        NewEpoch();
        return;
    }
    synced = 0;
    Evaluate();
}

// The new world runs (Tick).
void Settled(uint8_t kind, std::string const& save)
{
    localKind = kind;
    localSave = save;
    settledAt = GetTickCount();
    Logf("persist: world here: %s %s", KindName(kind), save.c_str());
    if (net::IsHost()) {
        if (kind == net::KindNew) { campaign.clear(); NewEpoch(); }
        return;
    }
    // Loading the host's current campaign puts this machine in the host's world, as at a join.
    const bool host = kind == net::KindSaved && hostWorld.kind == net::KindSaved && save == std::string("G3T_host_") + hostWorld.campaign;
    synced = host ? hostWorld.epoch : 0;
    Evaluate();
}

void OnWorld(uint8_t from, net::WorldNote const& note)
{
    if (!net::IsHost()) {
        if (note.op != net::WorldHost) return;
        if (note.epoch != hostWorld.epoch && !pendingLoad.empty()) {
            Logf("persist: %s is from an older host world; not loaded", pendingLoad.c_str());
            pendingLoad.clear();
        }
        hostWorld = note;
        Evaluate();
        return;
    }
    if (from < 2 || from >= net::MaxPlayers || note.epoch != epoch) return;  // an answer about an older world
    Client& c = clients[from];
    if (note.op == net::WorldReady) {
        c.ready = epoch;
        net::SetInWorld(from, true);
        Logf("persist: player %u (%s) is in the host's world %08x", from, net::PeerName(from), epoch);
    } else if (note.op == net::WorldOther) {
        c.ready = 0;
        net::SetInWorld(from, false);
        Logf("persist: player %u (%s) plays another world (%s %s)", from, net::PeerName(from), KindName(note.kind), note.campaign);
        if (!campaign.empty() && transferring) {
            if (c.sent != epoch || c.resends < 2) {  // a client that loaded something else gets the save again, twice at most
                if (c.sent == epoch) ++c.resends;
                c.sent = epoch;
                c.wants = true;  // sent from HostTick once the save is complete on disk
            }
        } else if (localKind == net::KindNew && note.kind != net::KindMenu && c.hinted != epoch) {  // not one about to start a game
            c.hinted = epoch;
            hud::Message(hud::Warning, i18n::Text(i18n::PartnerOtherWorldSave));
        }
    }
}

void HostTick()
{
    if (!epoch) NewEpoch();
    for (uint8_t i = 2; i < net::MaxPlayers; ++i) {
        Client& c = clients[i];
        if (!net::PeerJoined(i)) { c = Client{}; continue; }
        if (c.told != epoch || c.toldCampaign != campaign) Tell(i);
        static uint32_t lastTry = 0;
        if (c.wants && !campaign.empty() && GetTickCount() - savedAt >= 2000 && GetTickCount() - lastTry >= 1000) {
            lastTry = GetTickCount();
            if (c.ready == epoch || SendCampaign(i)) c.wants = false;
        }
        if (syncing) continue;
        // QA negative control, the old rule: in the host's world from the join on; the save once, at the join.
        net::SetInWorld(i, true);
        if (!c.sent) {
            c.sent = epoch;
            c.wants = !campaign.empty() && transferring;
        }
    }
}

bool __stdcall LoadDetour(void* self, bCString const* name)
{
    ++loads;
    lastLoad = name ? name->GetText() : "";
    if (net::IsHost()) campaign = lastLoad;
    loading = lastLoad;  // world activations from here until it runs belong to this load
    Changing(net::KindSaved);
    // The loaded campaign replaces whatever the journal held since: read the save's own state, or none.
    unsigned entries = 0;
    bool found = false;
    if (name) {
        FILE* file = nullptr;
        if (!fopen_s(&file, SidecarPath(name).c_str(), "r") && file) {
            char header[16] = {};
            if (fgets(header, sizeof(header), file) && !std::strncmp(header, "G3T 1", 5)) {
                entries = journal::Read(file);
                if (net::IsHost()) { fseek(file, 0, SEEK_SET); npcs::ReadCompanions(file); }
                found = true;
                ++sidecarsRead;
            }
            fclose(file);
        }
    }
    if (!found) { journal::Clear(); if (net::IsHost()) npcs::ClearCompanions(); }
    if (!net::IsHost() && name) {
        // The world in this save is the host's, and so is the hero in it; this player's own record goes on top.
        const std::string record = hero::RecordPath(name->GetText());
        if (GetFileAttributesA(record.c_str()) != INVALID_FILE_ATTRIBUTES) { pendingRecord = record; pendingSince = 0; }
    }
    Logf("persist: loading %s, %s (%u journal entries)", lastLoad.c_str(), found ? "sidecar read" : "no sidecar", entries);
    const bool ok = originalLoad(self, name);
    if (!ok) {
        Logf("persist: loading %s failed", lastLoad.c_str());
        loading.clear();
        newWorld = true;  // if a world runs after all, it counts as one of this machine's own
    }
    return ok;
}

// Client: the host saved the campaign; this player's own hero is kept for that save.
void OnCampaign(uint8_t kind, char const* name)
{
    if (kind != 1 || !SafeName(name)) return;
    const std::string record = hero::RecordPath((std::string("G3T_host_") + name).c_str());
    const bool ok = hero::SaveRecord(record.c_str());
    recordsWritten += ok ? 1 : 0;
    Logf("persist: the host saved %s; own hero record %s", name, ok ? "written" : "NOT written");
}
}

void OnWorldActivate()
{
    if (!loading.empty()) return;  // part of a load
    if (localKind == net::KindSaved && GetTickCount() - settledAt < 5000) {
        Log("persist: world activated right after a load ran; counted as part of the load");
        return;
    }
    newWorld = true;               // settles in Tick once a hero runs in it (a new game); the startup menu never does
    if (localKind != net::KindMenu) Changing(net::KindMenu);
}

void OnWorldDeactivate()
{
    if (!loading.empty()) return;
    Changing(net::KindMenu);
}

void Tick()
{
    gCSession& game = gCSession::GetInstance();
    if (game.GetHero() && game.IsGameRunning()) {
        if (!loading.empty()) {
            const std::string name = loading;
            loading.clear();
            newWorld = false;
            Settled(net::KindSaved, name);
        } else if (newWorld) {
            newWorld = false;
            Settled(net::KindNew, "");
        }
    }
    if (net::IsHost()) {
        HostTick();
    } else if (!pendingLoad.empty()) {
        const std::string name = pendingLoad;
        pendingLoad.clear();
        ++autoLoads;
        Logf("persist: loading the host's campaign %s", name.c_str());
        explore::NativeLoad(name.c_str(), &Log);
    }
    if (pendingRecord.empty()) return;
    gCSession& session = gCSession::GetInstance();
    if (!session.GetHero() || !session.IsGameRunning()) { pendingSince = 0; return; }
    const uint32_t now = GetTickCount();
    if (!pendingSince) { pendingSince = now; return; }
    if (now - pendingSince < 3000) return;  // the loaded world settles first
    const bool ok = hero::LoadRecord(pendingRecord.c_str());
    recordsApplied += ok ? 1 : 0;
    Logf("persist: own hero record %s applied: %d", pendingRecord.c_str(), ok ? 1 : 0);
    pendingRecord.clear();
}

void Install()
{
    if (installed) return;
    net::SetCampaignHandler(&OnCampaign);
    net::SetFileHandlers(&OnFileChunk, &OnFileDone);
    net::SetWorldHandler(&OnWorld);
    unsigned char* save = Resolve(SaveGameFn);
    unsigned char* load = Resolve(LoadGameFn);
    if (!save || !load) { Log("persist: Game.dll does not match this build; saves are not hooked"); installed = true; return; }
    originalSave = reinterpret_cast<FileFn>(Hook(save, reinterpret_cast<void*>(&SaveDetour)));
    originalLoad = reinterpret_cast<FileFn>(Hook(load, reinterpret_cast<void*>(&LoadDetour)));
    if (originalSave) saveTarget = save;
    if (originalLoad) loadTarget = load;
    installed = true;
    Logf("persist: save and load hooked (%d, %d)", originalSave != nullptr, originalLoad != nullptr);
}

void* SaveTarget() { return saveTarget; }
void* LoadTarget() { return loadTarget; }

void Off(LogFn log)
{
    writing = false;
    log("PERSISTOFF ok=1");
}

void CampaignOff(LogFn log)
{
    transferring = false;
    log("CAMPAIGNOFF ok=1");
}

// QA, host (stage X1): a file chunk whose offset + length wraps past 2^32, and a save name that leaves the save folder.
void BadTransfer(LogFn log)
{
    if (!net::IsHost()) { log("REFUSED badtransfer: host only"); return; }
    uint8_t junk[32] = {};
    const uint32_t sizes[net::FileCount] = {100, 100, 0}, crcs[net::FileCount] = {};
    unsigned sent = 0;
    for (uint8_t i = 2; i < net::MaxPlayers; ++i) {
        if (!net::PeerJoined(i)) continue;
        net::SendFileChunk(i, 0, 100, 0xFFFFFFF0u, junk, sizeof(junk));
        net::SendFileDone(i, "..\\..\\G3T_evil", sizes, crcs);
        ++sent;
    }
    char line[100];
    sprintf_s(line, "BADTRANSFER sent=%u", sent);
    log(line);
}

void WorldSyncOff(LogFn log)
{
    syncing = false;
    log(net::IsHost() ? "WORLDSYNCOFF ok=1" : "WORLDSYNCOFF ok=0 (host only)");
}

void WorldReport(LogFn log)
{
    char line[700];
    unsigned in = 0, out = 0;
    net::GateCounts(in, out);
    std::string players;
    for (uint8_t i = 2; i < net::MaxPlayers; ++i)
        if (net::PeerJoined(i)) players += std::to_string(i) + (net::InWorld(i) ? ":in," : ":out,");
    sprintf_s(line, "WORLD role=%s kind=%s save=%s loading=%s new_pending=%d changes=%u epoch=%08x syncing=%d host_epoch=%08x host_kind=%s host_campaign=%s synced=%08x in_world=%d readies=%u others=%u gated_in=%u gated_out=%u players=%s",
              net::IsHost() ? "host" : "client", KindName(localKind), localSave.empty() ? "-" : localSave.c_str(), loading.empty() ? "-" : loading.c_str(),
              newWorld ? 1 : 0, worldChanges, epoch, syncing ? 1 : 0, hostWorld.epoch, KindName(hostWorld.kind), hostWorld.campaign[0] ? hostWorld.campaign : "-",
              synced, net::InWorld(net::LocalId()) ? 1 : 0, readies, others, in, out, players.empty() ? "-" : players.c_str());
    log(line);
}

void Report(LogFn log)
{
    char line[600];
    sprintf_s(line, "PERSIST hooked=%d saves=%u loads=%u written=%u read=%u last_save=%s last_load=%s writing=%d records_written=%u records_applied=%u campaign=%s sent=%u received=%u auto_loads=%u last_received=%s",
              originalSave && originalLoad ? 1 : 0, saves, loads, sidecarsWritten, sidecarsRead, lastSave.c_str(), lastLoad.c_str(),
              writing ? 1 : 0, recordsWritten, recordsApplied, campaign.empty() ? "-" : campaign.c_str(), filesSent, filesReceived,
              autoLoads, lastReceived.c_str());
    log(line);
}
}
