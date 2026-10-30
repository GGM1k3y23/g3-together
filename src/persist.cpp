// SPDX-License-Identifier: GPL-3.0-or-later
#include "persist.h"

#include "config.h"
#include "containers.h"
#include "dialogaudio.h"
#include "hero.h"
#include "hud.h"
#include "i18n.h"
#include "journal.h"
#include "net.h"
#include "npcs.h"
#include "qa.h"
#include "log.h"
#include "remote.h"
#include "explore.h"
#include "steam.h"

#include <Game.h>
#include <Windows.h>
#include <cstdio>
#include <cstdlib>
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
struct Client { uint32_t told = 0; std::string toldCampaign; uint32_t ready = 0; uint32_t sent = 0; unsigned resends = 0; uint32_t hinted = 0; bool wants = false;
                uint32_t generation = 0;     // net::PeerGeneration of the connection this is about
                uint32_t waitHinted = 0;     // the epoch the host was last shown that this player waits for its hero
                uint32_t sentAt = 0;         // GetTickCount() of the last send of the campaign to this player
                bool arrivedSinceSend = false; };  // the player came into the host's world since that send
uint32_t savedAt = 0;            // GetTickCount() of the host's last save: the game writes the files after SaveGame returns
// A send still on its way is not repeated for a player who is only in its menu or starting a game (29.10. under G3_NETSIM 800:
// menu then new game sent the 2-MB save twice more; they arrived 1.5 and 3 min later, and each loaded the client's world again,
// the second in the middle of play). QA resendguardoff: sent again, as before.
constexpr uint32_t ResendGuard = 180000;  // ms
bool resendGuard = true;
unsigned resendsSkipped = 0;
Client clients[net::MaxPlayers];
bool byGeneration = true;        // host: false after the QA negative control `generationoff` (a slot is reset only once empty)
uint32_t pausedUntil = 0;        // host, QA (stage W1): HostTick waits until then (`persistpause <ms>`), as if leave and join fell in one frame
bool earlyGate = true;           // client: false after the QA negative control `worldgatelate` (the gate as before 23.10.)
// Client: the player's own hero record for the loaded save was refused (damaged, R1). The player waits outside the host's
// world until it loads (owner 23.10., C's recommendation); no copy of the host's hero any more. Tried again whenever the
// file changes; removed, the rule of a first join holds (a copy of the host's hero, NoCampaignHero).
std::string refusedRecord;
FILETIME refusedWritten{};
uint32_t refusedHinted = 0;
bool recordWait = true;          // client: false after the QA negative control `recordwaitoff` (a refused record opened the gate)
// The game's own load runs (LoadDetour). It is reentrant: its Session::Start runs twelve frames of its own, in which
// persist::Tick and the network handlers run again before it returns (C 22.10., re/findings/load-worldactivate-hero-r6-
// 20261022-codex.md). Settling, the hero restore, an autoload and opening the gate wait until it returned.
unsigned nativeLoadDepth = 0;
// Nothing of the mod runs in those frames either (BeginFrame): remote, npcs and the rest handled the network and the
// half-made world there. A client's autoload of the host's campaign starts from a window timer, outside the module's
// Process (C 23.10., re/findings/p3-load-autoload-ransack-contract-c-20261023.md): started from persist::Tick, the
// subsystems after it ran on in the same frame in the new world.
unsigned processDepth = 0;       // inside the module's Process (BeginFrame .. EndFrame)
unsigned loadFrames = 0, loadFramesRan = 0;  // frames the game ran inside its loads; the mod's ticks ran in these
bool loadTicks = false;          // QA negative control `loadticksoff`: the ticks run in them, the autoload from Tick (as before 23.10.)
bool autoloadHold = false;       // QA stage W6 only: keep a valid timer order armed until `loadticksoff release`
constexpr UINT_PTR AutoloadTimer = 0x47335436;  // 'G3T6'
// Client: one order, made when the campaign arrived (OnFileDone) and never bound again: the save, the host world and the
// number of loads and welcomes at that moment, and a serial of its own. A load, another host world, a rejoin or leaving
// the host voids it.
struct Autoload {
    std::string slot;
    uint32_t epoch = 0, welcome = 0;
    unsigned loads = 0, serial = 0;
    HWND window = nullptr;
    bool valid = false, armed = false;
};
Autoload autoload;
unsigned autoloadSerial = 0;
unsigned autoloadPosted = 0, autoloadStale = 0;
bool autoloadWarned = false;
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
    if (to < net::MaxPlayers) {
        clients[to].sentAt = GetTickCount() ? GetTickCount() : 1;
        clients[to].arrivedSinceSend = false;
    }
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

// Client: the autoload order is void (a load began, another host world, left the host, a newer campaign); logged once.
// Only that order's save is taken back from pendingLoad, so nothing newer is lost.
void DropAutoload(char const* why)
{
    if (!autoload.valid) return;
    if (autoload.armed && autoload.window) KillTimer(autoload.window, AutoloadTimer);
    if (pendingLoad == autoload.slot) pendingLoad.clear();
    ++autoloadStale;
    Logf("persist: autoload of %s is stale (%s), order %u; not loaded", autoload.slot.c_str(), why, autoload.serial);
    autoload = Autoload{};
}

// Client, from the window's timer (the game's message loop, between two of its frames): the order runs if it still holds.
// Nothing else here; the next Process settles the world and restores the hero, as after any load.
void CALLBACK AutoloadNow(HWND window, UINT, UINT_PTR id, DWORD)
{
    if (!autoload.valid || !autoload.armed) { KillTimer(window, id); return; }
    if (processDepth || nativeLoadDepth) return;  // a frame or a load runs (a message loop of its own): the timer comes again
    char const* why = net::IsHost() ? "this machine hosts" : !net::Joined() ? "not joined" : autoload.welcome != net::WelcomeSerial() ? "another welcome"
                    : autoload.epoch != hostWorld.epoch ? "another host world" : autoload.loads != loads ? "a load ran since"
                    : pendingLoad != autoload.slot ? "another campaign" : nullptr;
    if (why) { DropAutoload(why); return; }
    if (autoloadHold) return;  // QA timing only; validity above and Tick invalidation still apply while held
    if (!KillTimer(window, id)) Logf("persist: the autoload timer could not be stopped (%lu)", GetLastError());
    autoload.armed = false;
    const std::string name = autoload.slot;
    const uint32_t welcome = autoload.welcome;
    const unsigned serial = autoload.serial;
    autoload = Autoload{};  // used up before the load, so its LoadDetour finds no order to void
    pendingLoad.clear();
    ++autoLoads;
    Logf("persist: autoload of %s runs outside the module's frame (process depth %u, welcome %u), order %u", name.c_str(), processDepth,
         welcome, serial);
    Logf("persist: loading the host's campaign %s", name.c_str());
    explore::NativeLoad(name.c_str(), &Log);
}

// Client: the order's timer; false (logged once) if there is no window or no timer yet, and Tick tries the same order again.
bool ArmAutoload()
{
    HWND window = eCApplication::GetInstance().GetHandle();
    if (!window || !SetTimer(window, AutoloadTimer, USER_TIMER_MINIMUM, &AutoloadNow)) {
        if (!autoloadWarned) Logf("persist: autoload of %s cannot be posted yet (window %p, error %lu), order %u", autoload.slot.c_str(),
                                  static_cast<void*>(window), GetLastError(), autoload.serial);
        autoloadWarned = true;
        return false;
    }
    autoloadWarned = false;
    autoload.window = window;
    autoload.armed = true;
    ++autoloadPosted;
    Logf("persist: autoload of %s posted (host world %08x, welcome %u, loads %u), order %u", autoload.slot.c_str(), autoload.epoch,
         autoload.welcome, autoload.loads, autoload.serial);
    return true;
}

// Client, when a campaign arrived: the order is bound now, to this save, this host world and this many loads. A newer one
// replaces it, also for the same save.
void OrderAutoload(std::string const& slot)
{
    DropAutoload("a newer campaign arrived");
    autoload = Autoload{slot, hostWorld.epoch, net::WelcomeSerial(), loads, ++autoloadSerial, nullptr, true, false};
    pendingLoad = slot;
    ArmAutoload();
}

// Client: the newest record of this player's own hero for any of the host's saves (g3t-hero-G3T_host_*.txt, not the
// quick-slot files), written within the last day; empty if there is none.
bool lastOwnHero = true;  // false: a save the host made without this player gives a copy of the host's hero (as before 27.10.)
std::string LatestOwnRecord()
{
    WIN32_FIND_DATAA found;
    const std::string folder = Folder();
    HANDLE find = FindFirstFileA((folder + "g3t-hero-G3T_host_*.txt").c_str(), &found);
    if (find == INVALID_HANDLE_VALUE) return {};
    std::string best;
    FILETIME bestTime{};
    do {
        const size_t n = std::strlen(found.cFileName);
        if (n > 10 && !_stricmp(found.cFileName + n - 10, ".quick.txt")) continue;
        if (CompareFileTime(&found.ftLastWriteTime, &bestTime) > 0) { bestTime = found.ftLastWriteTime; best = folder + found.cFileName; }
    } while (FindNextFileA(find, &found));
    FindClose(find);
    if (best.empty()) return {};
    FILETIME nowTime;
    GetSystemTimeAsFileTime(&nowTime);
    const auto ticks = [](FILETIME const& t) { return (static_cast<uint64_t>(t.dwHighDateTime) << 32) | t.dwLowDateTime; };
    if (ticks(nowTime) - ticks(bestTime) > 24ull * 3600 * 10000000) return {};  // older than a day: another campaign
    return best;
}

void OnFileDone(char const* name, uint32_t const size[net::FileCount], uint32_t const crc[net::FileCount])
{
    if (!SafeName(name)) {
        Logf("persist: a campaign name the host sent is not a plain save name; not stored");
        hud::Message(hud::Warning, i18n::Text(i18n::SaveDamaged));
        for (auto& v : incoming) v.clear();
        return;
    }
    const std::string local = std::string("G3T_host_") + name;
    if (!size[0] || !size[1]) {  // a save without its world data must never be loaded (the game hangs)
        Logf("persist: campaign %s arrived without %s; not loaded", name, size[0] ? Extensions[1] : Extensions[0]);
        hud::Message(hud::Warning, i18n::Text(i18n::SaveDamaged));
        for (auto& v : incoming) v.clear();
        return;
    }
    for (int f = 0; f < net::FileCount; ++f) {
        if (!size[f]) continue;
        if (incoming[f].size() != size[f] || Crc32(incoming[f].data(), incoming[f].size()) != crc[f]) {
            Logf("persist: campaign %s arrived damaged (file %d); not loaded", name, f);
            hud::Message(hud::Warning, i18n::Text(i18n::SaveDamaged));
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
        // A save the host made while this player was not there (refused, or not joined yet) has no record of this
        // player either: its newest own record goes on instead of a copy of the host's hero (the client as a copy of the host's hero after
        // the host saved alone, playtest 27.10.).
        const std::string latest = lastOwnHero ? LatestOwnRecord() : std::string();
        if (session.GetHero() && session.IsGameRunning() && hero::SaveRecord(record.c_str())) {
            Logf("persist: first time in %s; the current hero is kept as this player's hero", local.c_str());
        } else if (!latest.empty() && CopyFileA(latest.c_str(), record.c_str(), FALSE)) {
            std::string quickFrom = latest, quickTo = record;
            quickFrom.resize(quickFrom.size() - 4);
            quickTo.resize(quickTo.size() - 4);
            CopyFileA((quickFrom + ".quick.txt").c_str(), (quickTo + ".quick.txt").c_str(), FALSE);
            Logf("persist: no hero of this player in %s; its newest own hero (%s) goes on", local.c_str(), latest.c_str());
        } else {
            hud::Message(hud::Warning, i18n::Text(i18n::NoCampaignHero));
            Logf("persist: first time in %s and no hero in play; the host's hero is copied", local.c_str());
        }
    }
    if (autoLoad && loadTicks) pendingLoad = local;  // QA negative control: Tick loads it, as before
    else if (autoLoad) OrderAutoload(local);
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
    // The player's own hero comes back first (Codex review 22.10. R6): opened before the record was applied, a reward in
    // those ~3 s was overwritten, and a host save then kept the host's hero as this player's record.
    if (earlyGate && (!pendingRecord.empty() || nativeLoadDepth)) return;
    if (!refusedRecord.empty()) {  // waits until its own hero loads (owner 23.10.); the host learns why
        SendAnswer(net::WorldWaiting);
        return;
    }
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
    refusedRecord.clear();  // a load or another world checks its own record again
    // Closed at once (Codex review 22.10. R6): during a load Evaluate returns before it closes the gate, so it stayed open
    // through the world change, and the first frame of the new world handled the host's messages before persist::Tick.
    if (earlyGate) net::SetInWorld(net::LocalId(), false);
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
        if (note.epoch != hostWorld.epoch) DropAutoload("another host world");
        hostWorld = note;
        Evaluate();
        return;
    }
    if (from < 2 || from >= net::MaxPlayers || note.epoch != epoch) return;  // an answer about an older world
    Client& c = clients[from];
    if (note.op == net::WorldReady) {
        c.ready = epoch;
        if (nativeLoadDepth) {  // the host's own load still runs (its frames handle the network): HostTick opens afterwards
            Logf("persist: player %u is ready for world %08x; the gate opens once this load returned", from, epoch);
            return;
        }
        net::SetInWorld(from, true);
        c.arrivedSinceSend = true;
        Logf("persist: player %u (%s) is in the host's world %08x", from, net::PeerName(from), epoch);
    } else if (note.op == net::WorldOther) {
        c.ready = 0;
        net::SetInWorld(from, false);
        Logf("persist: player %u (%s) plays another world (%s %s)", from, net::PeerName(from), KindName(note.kind), note.campaign);
        const bool onItsWay = resendGuard && !GetConfig().qaResendGuardOff && c.sentAt && !c.arrivedSinceSend && GetTickCount() - c.sentAt < ResendGuard &&
                              c.sent == epoch && (note.kind == net::KindMenu || note.kind == net::KindNew);
        if (onItsWay) {
            ++resendsSkipped;
            Logf("persist: the save sent to player %u %u s ago is still on its way; not sent again", from, (GetTickCount() - c.sentAt) / 1000);
        } else if (!campaign.empty() && transferring) {
            if (c.sent != epoch || c.resends < 2) {  // a client that loaded something else gets the save again, twice at most
                if (c.sent == epoch) ++c.resends;
                c.sent = epoch;
                c.wants = true;  // sent from HostTick once the save is complete on disk
            }
        } else if (localKind == net::KindNew && note.kind != net::KindMenu && c.hinted != epoch) {  // not one about to start a game
            c.hinted = epoch;
            hud::Message(hud::Warning, i18n::Text(i18n::PartnerOtherWorldSave));
        }
    } else if (note.op == net::WorldWaiting) {  // in the host's save, but its own hero record was refused (owner 23.10.)
        c.ready = 0;
        net::SetInWorld(from, false);
        Logf("persist: player %u (%s) waits outside the host's world: its own hero record was refused", from, net::PeerName(from));
        if (c.waitHinted != epoch) {
            c.waitHinted = epoch;
            wchar_t name[32] = {}, text[400];
            MultiByteToWideChar(CP_UTF8, 0, net::PeerName(from), -1, name, 31);
            _snwprintf_s(text, _TRUNCATE, i18n::Text(i18n::PartnerWaitsForHero), name);
            hud::Message(hud::Warning, text);
        }
    }
}

void HostTick()
{
    if (!epoch) NewEpoch();
    if (static_cast<int32_t>(pausedUntil - GetTickCount()) > 0) return;
    for (uint8_t i = 2; i < net::MaxPlayers; ++i) {
        Client& c = clients[i];
        if (!net::PeerJoined(i)) { c = Client{}; continue; }
        // A new connection in this slot, also one that came within the frame the last one left in (R7): told and sent
        // were about the old one, and the new player waited for a world it was never told.
        if (byGeneration && c.generation != net::PeerGeneration(i)) {
            if (c.generation) Logf("persist: player %u is a new connection; its state starts over", i);
            c = Client{};
            c.generation = net::PeerGeneration(i);
        }
        if (c.told != epoch || c.toldCampaign != campaign) Tell(i);
        if (syncing && c.ready == epoch && !net::InWorld(i)) {  // its WorldReady came during the host's own load
            net::SetInWorld(i, true);
            Logf("persist: player %u (%s) is in the host's world %08x", i, net::PeerName(i), epoch);
        }
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
    DropAutoload("a load began");  // before the game gets it: no order of the world before runs after this load (C 23.10.)
    pendingRecord.clear();  // a new load: the restore order of the one before is void (C 22.10.)
    pendingSince = 0;
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
    dialogaudio::OnWorldChange();  // nothing of the old world plays on into the load (B, with R6); idempotent
    containers::OnWorldChange();   // and no container state of it is compared with the reloaded containers (R5)
    const unsigned framesBefore = loadFrames, ranBefore = loadFramesRan;
    ++nativeLoadDepth;
    const bool ok = originalLoad(self, name);
    --nativeLoadDepth;
    Logf("persist: the game's load of %s returned (%d) after %u frames of its own, mod ticks in %u", lastLoad.c_str(), ok ? 1 : 0,
         loadFrames - framesBefore, loadFramesRan - ranBefore);
    if (!ok) {
        Logf("persist: loading %s failed", lastLoad.c_str());
        loading.clear();
        pendingRecord.clear();  // it belonged to that save; kept, it held the gate shut (R6)
        newWorld = true;  // if a world runs after all, it counts as one of this machine's own
    }
    return ok;
}

FILETIME WriteTime(std::string const& path)
{
    WIN32_FILE_ATTRIBUTE_DATA data{};
    if (!GetFileAttributesExA(path.c_str(), GetFileExInfoStandard, &data)) return FILETIME{};
    return data.ftLastWriteTime;
}

// Client, while it waits for its own hero (Tick): the reason on the screen every 30 s; the record is tried again once its
// file changed (repaired); removed, the player goes on with a copy of the host's hero, as at a first join.
void WaitForRecord()
{
    gCSession& session = gCSession::GetInstance();
    if (!session.GetHero() || !session.IsGameRunning()) return;
    if (GetFileAttributesA(refusedRecord.c_str()) == INVALID_FILE_ATTRIBUTES) {
        Logf("persist: own hero record %s was removed; the player goes on with a copy of the host's hero", refusedRecord.c_str());
        hud::Message(hud::Warning, i18n::Text(i18n::NoCampaignHero));
        refusedRecord.clear();
        Evaluate();
        return;
    }
    const FILETIME written = WriteTime(refusedRecord);
    if (CompareFileTime(&written, &refusedWritten) != 0) {
        refusedWritten = written;
        if (hero::LoadRecord(refusedRecord.c_str())) {
            ++recordsApplied;
            Logf("persist: own hero record %s applied after it changed: 1", refusedRecord.c_str());
            hud::Message(hud::Good, i18n::Text(i18n::RecordBack));
            refusedRecord.clear();
            Evaluate();
            return;
        }
        Logf("persist: own hero record %s changed and is still refused (%s)", refusedRecord.c_str(), hero::LastRefusal() ? hero::LastRefusal() : "?");
    }
    const uint32_t now = GetTickCount();
    if (refusedHinted && now - refusedHinted < 30000) return;
    refusedHinted = now ? now : 1;
    wchar_t name[80] = {}, text[400];
    const size_t cut = refusedRecord.find_last_of("\\/");
    MultiByteToWideChar(CP_UTF8, 0, refusedRecord.c_str() + (cut == std::string::npos ? 0 : cut + 1), -1, name, 79);
    _snwprintf_s(text, _TRUNCATE, i18n::Text(i18n::RecordRefusedWaiting), name);
    hud::Message(hud::Bad, text);
}

// Client: the connection on the screen (owner 23.10., point 10): lost and back again, and why the host refused this machine.
// A player whose game never reached the host saw nothing at all (owner 24.10., BERICHT-2026-10-24 point 6), also where it
// waits for the host's campaign: the main menu, where the game's messages do not show (menutextdiag). There the menu's top
// line says so (hud::MenuLine), in the game a message every 30 s; a refusal stays in that line too.
void StatusHints()
{
    static bool wasJoined = false, everJoined = false, unreachableHinted = false, menuSet = false;
    static std::string shown;
    static std::wstring refusal;
    static uint32_t menuAt = 0, hintedAt = 0;
    const bool joined = net::Joined();
    if (wasJoined && !joined) hud::Message(hud::Warning, i18n::Text(i18n::ConnectionLost));
    else if (!wasJoined && joined && everJoined) hud::Message(hud::Good, i18n::Text(i18n::Reconnected));
    everJoined = everJoined || joined;
    wasJoined = joined;
    char const* reason = net::RefusalReason();
    if (*reason && shown != reason) {
        shown = reason;
        static char const install[] = "install differs from the host: ";
        wchar_t text[400], wide[200] = {};
        unsigned mine = 0, host = 0;
        if (sscanf_s(reason, "different G3 Together version (protocol %u, host %u)", &mine, &host) == 2) {
            _snwprintf_s(text, _TRUNCATE, i18n::Text(i18n::RefusedVersion), mine, host);
        } else {
            char const* list = std::strstr(reason, install);
            MultiByteToWideChar(CP_UTF8, 0, list ? list + sizeof(install) - 1 : reason, -1, wide, 199);
            _snwprintf_s(text, _TRUNCATE, i18n::Text(list ? i18n::RefusedInstall : i18n::RefusedOther), wide);
        }
        hud::Message(hud::Bad, text);
        refusal = text;
    }
    const uint32_t now = GetTickCount();
    if (now - menuAt < 1000) return;  // the menu line is looked up by its position: once a second is enough
    menuAt = now;
    const bool running = gCSession::GetInstance().IsGameRunning();
    const uint32_t trying = net::ConnectingFor();
    Config const& config = GetConfig();
    const bool hintsOff = qa::Enabled() && !_stricmp(config.qaHostHint, "off");
    const bool refusalOff = hintsOff || (qa::Enabled() && !_stricmp(config.qaHostHint, "norefusal"));
    // transport = steam without Steam (not running, no steam_api.dll): said once in the game; the log has the details.
    static bool steamToldOff = false;
    if (config.steam && !steam::Ready() && *steam::Error() && running && !steamToldOff) {
        steamToldOff = true;
        wchar_t text[400], reason[200] = {};
        MultiByteToWideChar(CP_UTF8, 0, steam::Error(), -1, reason, 199);
        _snwprintf_s(text, _TRUNCATE, i18n::Text(config.host ? i18n::SteamUnavailableHost : i18n::SteamUnavailableClient), reason);
        hud::Message(hud::Warning, text);
    }
    if (!hintsOff && trying >= 15000 && refusal.empty()) {
        wchar_t text[400], address[80] = {};
        if (steam::Ready()) {  // over Steam there is no address to check: say whom it looks for or waits on
            MultiByteToWideChar(CP_UTF8, 0, steam::HostName(), -1, address, 79);
            if (*address) _snwprintf_s(text, _TRUNCATE, i18n::Text(i18n::SteamConnecting), address, trying / 1000);
            else _snwprintf_s(text, _TRUNCATE, i18n::Text(i18n::SteamSearching), trying / 1000);
        } else {
            MultiByteToWideChar(CP_UTF8, 0, config.address, -1, address, 79);
            _snwprintf_s(text, _TRUNCATE, i18n::Text(i18n::HostUnreachable), address, config.port, trying / 1000, config.port);
        }
        if (!unreachableHinted && steam::Ready())
            Logf("net: no host over Steam for %u s (%s); still trying (said in the menu and the game)", trying / 1000,
                 *steam::HostName() ? steam::HostName() : "no friend hosting");
        else if (!unreachableHinted) Logf("net: host %s:%u not reachable for %u s; still trying (said in the menu and the game)", config.address, config.port, trying / 1000);
        unreachableHinted = true;
        if (!running && hud::MenuLine(text)) menuSet = true;
        if (running && (!hintedAt || now - hintedAt >= 30000)) {
            hintedAt = now ? now : 1;
            hud::Message(hud::Warning, text);
        }
        return;
    }
    if (unreachableHinted && !trying) {
        unreachableHinted = false;
        Log("net: host reached after the not-reachable hint");
        if (running) hud::Message(hud::Good, i18n::Text(i18n::HostReached));
    }
    if (!refusal.empty() && !running && !refusalOff) {
        if (hud::MenuLine(refusal.c_str())) menuSet = true;
    } else if (menuSet && !running && hud::MenuLine(nullptr)) {
        menuSet = false;
    }
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

bool BeginFrame()
{
    if (nativeLoadDepth) {
        ++loadFrames;
        if (!loadTicks) return false;
        ++loadFramesRan;
    }
    ++processDepth;
    return true;
}

void EndFrame()
{
    if (processDepth) --processDepth;
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
    // Inside the game's load (one of its own frames): a hero may run already, but the load has not returned. Settling
    // here opened the gate and began the restore within it; an autoload here would start a load inside the load.
    if (earlyGate && nativeLoadDepth) return;
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
    } else if (autoload.valid) {  // the order of OnFileDone, never bound again here
        if (!net::Joined()) DropAutoload("left the host");
        else if (autoload.welcome != net::WelcomeSerial()) DropAutoload("another welcome");
        else if (!autoload.armed) ArmAutoload();
    } else if (!pendingLoad.empty() && loadTicks) {  // QA negative control: within this frame, as before
        const std::string name = pendingLoad;
        pendingLoad.clear();
        ++autoLoads;
        Logf("persist: loading the host's campaign %s", name.c_str());
        explore::NativeLoad(name.c_str(), &Log);
    }
    if (!net::IsHost()) {
        StatusHints();
        if (!refusedRecord.empty()) WaitForRecord();
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
    if (!ok && recordWait) {
        refusedRecord = pendingRecord;
        refusedWritten = WriteTime(refusedRecord);
        refusedHinted = 0;
        Logf("persist: own hero record %s refused (%s): the player waits outside the host's world until it loads", refusedRecord.c_str(),
             hero::LastRefusal() ? hero::LastRefusal() : "?");
    }
    pendingRecord.clear();
    Evaluate();  // the gate opens now that the hero is this player's own (R6)
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
    Format(line, "BADTRANSFER sent=%u", sent);
    log(line);
}

void WorldSyncOff(LogFn log)
{
    syncing = false;
    log(net::IsHost() ? "WORLDSYNCOFF ok=1" : "WORLDSYNCOFF ok=0 (host only)");
}

void GenerationOff(LogFn log)
{
    byGeneration = false;
    log(net::IsHost() ? "GENERATIONOFF ok=1" : "GENERATIONOFF ok=0 (host only)");
}

void Pause(char const* argument, LogFn log)
{
    const int ms = std::atoi(argument);
    if (!net::IsHost() || ms <= 0 || ms > 30000) { log("REFUSED persistpause: host only, 1 to 30000 ms"); return; }
    pausedUntil = GetTickCount() + static_cast<uint32_t>(ms);
    char line[80];
    Format(line, "PERSISTPAUSE ok=1 ms=%d", ms);
    log(line);
}

void WorldGateLate(LogFn log)
{
    earlyGate = false;
    log(net::IsHost() ? "WORLDGATELATE ok=0 (client only)" : "WORLDGATELATE ok=1");
}

void RecordWaitOff(LogFn log)
{
    recordWait = false;
    log(net::IsHost() ? "RECORDWAITOFF ok=0 (client only)" : "RECORDWAITOFF ok=1 (a refused hero record opens the gate, as before 23.10.)");
}

void Waiting(LogFn log)
{
    char line[400];
    Format(line, "RECORDWAIT waiting=%d record=%.300s", refusedRecord.empty() ? 0 : 1, refusedRecord.empty() ? "-" : refusedRecord.c_str());
    log(line);
}

void LoadTicksOff(char const* argument, LogFn log)
{
    if (argument && !std::strcmp(argument, "hold")) {
        autoloadHold = true;
        log("LOADTICKSOFF hold=1");
        return;
    }
    if (argument && !std::strcmp(argument, "release")) {
        autoloadHold = false;
        log("LOADTICKSOFF hold=0");
        return;
    }
    if (argument && !std::strcmp(argument, "welcome")) {
        const uint32_t before = net::WelcomeSerial();
        net::AdvanceWelcomeSerialForTest();
        char line[120];
        Format(line, "LOADTICKSOFF welcome=%u->%u", before, net::WelcomeSerial());
        log(line);
        return;
    }
    loadTicks = true;
    log("LOADTICKSOFF ok=1");
}

void WorldReport(LogFn log)
{
    char line[700];
    unsigned in = 0, out = 0;
    net::GateCounts(in, out);
    std::string players;
    for (uint8_t i = 2; i < net::MaxPlayers; ++i)
        if (net::PeerJoined(i)) players += std::to_string(i) + (net::InWorld(i) ? ":in," : ":out,");
    Format(line, "WORLD role=%s kind=%s save=%s loading=%s new_pending=%d changes=%u epoch=%08x syncing=%d host_epoch=%08x host_kind=%s host_campaign=%s synced=%08x in_world=%d readies=%u others=%u gated_in=%u gated_out=%u players=%s",
              net::IsHost() ? "host" : "client", KindName(localKind), localSave.empty() ? "-" : localSave.c_str(), loading.empty() ? "-" : loading.c_str(),
              newWorld ? 1 : 0, worldChanges, epoch, syncing ? 1 : 0, hostWorld.epoch, KindName(hostWorld.kind), hostWorld.campaign[0] ? hostWorld.campaign : "-",
              synced, net::InWorld(net::LocalId()) ? 1 : 0, readies, others, in, out, players.empty() ? "-" : players.c_str());
    log(line);
}

void Report(LogFn log)
{
    char line[800];
    Format(line, "PERSIST hooked=%d saves=%u loads=%u written=%u read=%u last_save=%s last_load=%s writing=%d records_written=%u records_applied=%u campaign=%s sent=%u received=%u auto_loads=%u last_received=%s load_frames=%u load_frames_ran=%u autoload_posted=%u autoload_stale=%u load_ticks=%d",
              originalSave && originalLoad ? 1 : 0, saves, loads, sidecarsWritten, sidecarsRead, lastSave.c_str(), lastLoad.c_str(),
              writing ? 1 : 0, recordsWritten, recordsApplied, campaign.empty() ? "-" : campaign.c_str(), filesSent, filesReceived,
              autoLoads, lastReceived.c_str(), loadFrames, loadFramesRan, autoloadPosted, autoloadStale, loadTicks ? 1 : 0);
    log(line);
}
// QA negative controls of the play-evening fixes (27.10.): each switches its fix off (on: clientfameon).
bool Switch(char const* action, LogFn log)
{
    if (!std::strcmp(action, "lastownherooff")) { lastOwnHero = false; log("SWITCH lastownherooff ok=1"); return true; }
    if (!std::strcmp(action, "resendguardoff")) { resendGuard = false; log("SWITCH resendguardoff ok=1"); return true; }
    return false;
}
}
