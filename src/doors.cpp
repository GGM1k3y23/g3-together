// SPDX-License-Identifier: GPL-3.0-or-later
#include "doors.h"

#include "log.h"
#include "world.h"

#include <Engine.h>
#include <Game.h>
#include <Script.h>
#include <Windows.h>
#include <cstdio>
#include <cstring>
#include <string>
#include <unordered_map>

namespace g3t::doors {
namespace {
constexpr uint8_t Kind = 10;  // world state kind: guard = open, alignment = locked, npcClass bits 1 door, 2 lock
constexpr uint32_t ScanEvery = 500;  // ms

struct Seen {
    uint8_t open = 0, locked = 0, parts = 0;
};
std::unordered_map<std::string, Seen> seen;    // what this machine last saw (or was told) per entity
std::unordered_map<std::string, Seen> wanted;  // states from elsewhere, applied once the entity is processed here
// Host: every door or lock that changed this session (here or elsewhere). A player who joins later loads the host's
// last save, which may be older; it asks once (npcClass 0x80) and the host sends these again.
std::unordered_map<std::string, Seen> changed;
constexpr uint8_t Request = 0x80;
bool wasConnected = false;
uint32_t lastScan = 0;
bool sync = true;  // QA negative control
unsigned sent = 0, received = 0, applied = 0, watched = 0;

std::string Key(uint8_t const guid[16]) { return std::string(reinterpret_cast<char const*>(guid), 16); }

void GuidOf(eCEntity* entity, uint8_t out[16])
{
    const eCEntityProxy proxy(entity);
    std::memcpy(out, &proxy.GetEntityID().GetCore(), 16);
}

Seen Read(eCEntity* entity)
{
    Entity e(entity);
    Seen s;
    if (entity->GetPropertySet(eEPropertySetType_Door)) s.parts |= 1, s.open = e.Door.IsOpen() ? 1 : 0;
    if (entity->GetPropertySet(eEPropertySetType_Lock)) s.parts |= 2, s.locked = e.Lock.IsLocked() ? 1 : 0;
    return s;
}

bool Same(Seen const& a, Seen const& b) { return a.open == b.open && a.locked == b.locked; }

// Bring the entity to the wanted state the way the game's own code changes it (PSLock status, PSDoor::Open/Close).
void Apply(eCEntity* entity, Seen const& want)
{
    Entity e(entity);
    if ((want.parts & 2) && entity->GetPropertySet(eEPropertySetType_Lock))
        e.Lock.Status = want.locked ? gELockStatus_Locked : gELockStatus_Unlocked;
    if ((want.parts & 1) && entity->GetPropertySet(eEPropertySetType_Door)) {
        const bool open = e.Door.IsOpen() != GEFalse;
        if (want.open && !open) e.Door.Open(GEFalse);
        else if (!want.open && open) e.Door.Close(GEFalse);
    }
    ++applied;
}

void Send(uint8_t const guid[16], Seen const& s)
{
    if (net::IsHost()) changed[Key(guid)] = s;
    net::WorldState w{};
    w.kind = Kind;
    std::memcpy(w.guid, guid, 16);
    w.guard = s.open;
    w.alignment = s.locked;
    w.npcClass = s.parts;
    net::SendWorldState(w, 0);
    ++sent;
}
}

void Tick()
{
    if (!sync || !net::Connected() || !gCSession::GetInstance().IsGameRunning()) { wasConnected = false; return; }
    if (!wasConnected) {
        wasConnected = true;
        if (!net::IsHost()) {  // joined (or joined again): ask the host for this session's changes
            net::WorldState w{};
            w.kind = Kind;
            w.npcClass = Request;
            net::SendWorldState(w, 0);
        }
    }
    const uint32_t now = GetTickCount();
    if (now - lastScan < ScanEvery) return;
    lastScan = now;
    bTSmallArray<eCEntity*> const* every = world::ProcessedEntities();
    unsigned count = 0;
    for (GEU16 i = 0; every && i < every->GetCount(); ++i) {
        eCEntity* entity = every->GetAt(i);
        if (!entity || (!entity->GetPropertySet(eEPropertySetType_Door) && !entity->GetPropertySet(eEPropertySetType_Lock))) continue;
        if (entity->GetPropertySet(eEPropertySetType_NPC) || entity->GetPropertySet(eEPropertySetType_Item)) continue;
        ++count;
        uint8_t guid[16];
        GuidOf(entity, guid);
        const std::string key = Key(guid);
        Seen now_ = Read(entity);
        if (auto want = wanted.find(key); want != wanted.end()) {
            if (!Same(now_, want->second)) {
                Apply(entity, want->second);
                now_ = Read(entity);
                Logf("doors: %s set to open=%u locked=%u (now open=%u locked=%u)", entity->GetName().GetText(), want->second.open,
                     want->second.locked, now_.open, now_.locked);
            }
            if (Same(now_, want->second)) wanted.erase(want);
            seen[key] = now_;
            continue;
        }
        auto last = seen.find(key);
        if (last == seen.end()) {
            seen[key] = now_;  // first sight: the state from the save, the same everywhere
            continue;
        }
        if (!Same(now_, last->second)) {
            last->second = now_;
            Send(guid, now_);
            Logf("doors: %s changed here: open=%u locked=%u", entity->GetName().GetText(), now_.open, now_.locked);
        }
    }
    watched = count;
}

void OnState(uint8_t /*from*/, net::WorldState const& s)
{
    if (!sync) return;
    if (s.npcClass & Request) {  // host: a player joined; everyone gets this session's changes again (unchanged ones are ignored)
        if (net::IsHost())
            for (auto const& entry : changed) {
                uint8_t guid[16];
                std::memcpy(guid, entry.first.data(), 16);
                Send(guid, entry.second);
            }
        return;
    }
    ++received;
    Seen want;
    want.open = s.guard ? 1 : 0;
    want.locked = s.alignment ? 1 : 0;
    want.parts = s.npcClass;
    const std::string key = Key(s.guid);
    wanted[key] = want;
    seen[key] = want;  // so that applying it is not taken for a change made here
    if (net::IsHost()) changed[key] = want;
}

void OnWorldChange()
{
    seen.clear();
    wanted.clear();
    changed.clear();  // the host loaded a save: the save is the state now
    wasConnected = false;  // a client that just loaded the host's save asks again
}

void Report(LogFn log)
{
    char line[200];
    sprintf_s(line, "DOORS sync=%d watched=%u sent=%u received=%u applied=%u pending=%zu changed=%zu", sync ? 1 : 0, watched, sent, received,
              applied, wanted.size(), changed.size());
    log(line);
}

void SyncOff(LogFn log)
{
    sync = false;
    log("DOORSYNCOFF ok=1");
}
}
