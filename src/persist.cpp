// SPDX-License-Identifier: GPL-3.0-or-later
#include "persist.h"

#include "hero.h"
#include "hud.h"
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

// --- The campaign travels to joining players ---------------------------------------------------------------
// The host's current campaign is the save it last loaded or saved. A client that joins gets its three files
// (.g3savcpx, .g3savcpxdat, .g3t), stores them under G3T_host_<name> (never over one of its own saves) and
// loads that save; its own hero record for that name, if any, then goes on top (D3a).
char const* const Extensions[net::FileCount] = {".g3savcpx", ".g3savcpxdat", ".g3t"};
std::string campaign;                    // host: current campaign save name
bool sentTo[net::MaxPlayers] = {};       // host: campaign sent to this player since it connected
bool transferring = true;                // host: QA negative control `campaignoff`
bool autoLoad = true;
std::vector<uint8_t> incoming[net::FileCount];
std::string pendingLoad;                 // client: received campaign to load
unsigned filesSent = 0, filesReceived = 0, autoLoads = 0;
std::string lastReceived = "-";

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

void SendCampaign(uint8_t to)
{
    uint32_t sizes[net::FileCount] = {}, crcs[net::FileCount] = {};
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
}

void OnFileChunk(uint8_t file, uint32_t total, uint32_t offset, uint8_t const* data, uint16_t length)
{
    if (file >= net::FileCount || offset + length > total || total > 64u * 1024 * 1024) return;
    if (incoming[file].size() != total) incoming[file].assign(total, 0);
    std::memcpy(incoming[file].data() + offset, data, length);
}

void OnFileDone(char const* name, uint32_t const size[net::FileCount], uint32_t const crc[net::FileCount])
{
    const std::string local = std::string("G3T_host_") + name;
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
    Logf("persist: campaign %s received as %s", name, local.c_str());
    // First time in this campaign: no record of this player's hero for it yet. If a hero is in play (a new game
    // started before joining), that hero is kept; from the main menu there is none, and the hero in the host's
    // save (a copy of the host's) stays.
    const std::string record = hero::RecordPath(local.c_str());
    if (GetFileAttributesA(record.c_str()) == INVALID_FILE_ATTRIBUTES) {
        gCSession& session = gCSession::GetInstance();
        if (session.GetHero() && session.IsGameRunning() && hero::SaveRecord(record.c_str())) {
            Logf("persist: first time in %s; the current hero is kept as this player's hero", local.c_str());
        } else {
            hud::Message(hud::Warning, L"Kein eigener Held f\u00fcr diese Kampagne: du spielst mit einer Kopie des Host-Helden.");
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

bool __stdcall LoadDetour(void* self, bCString const* name)
{
    ++loads;
    lastLoad = name ? name->GetText() : "";
    if (net::IsHost()) campaign = lastLoad;
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
    return originalLoad(self, name);
}

// Client: the host saved the campaign; this player's own hero is kept for that save.
void OnCampaign(uint8_t kind, char const* name)
{
    if (kind != 1) return;
    const std::string record = hero::RecordPath((std::string("G3T_host_") + name).c_str());
    const bool ok = hero::SaveRecord(record.c_str());
    recordsWritten += ok ? 1 : 0;
    Logf("persist: the host saved %s; own hero record %s", name, ok ? "written" : "NOT written");
}
}

void Tick()
{
    if (net::IsHost()) {
        net::Remote const* remotes = net::Remotes();
        // Only when a player joins: a campaign saved later must not pull an already connected player into a load.
        for (int i = 2; i < net::MaxPlayers; ++i) {
            if (!remotes[i].connected) { sentTo[i] = false; continue; }
            if (sentTo[i]) continue;
            sentTo[i] = true;
            if (!campaign.empty() && transferring) SendCampaign(static_cast<uint8_t>(i));
        }
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
