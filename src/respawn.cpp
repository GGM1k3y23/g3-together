// SPDX-License-Identifier: GPL-3.0-or-later
#include "respawn.h"

#include "ai.h"
#include "body.h"
#include "journal.h"
#include "log.h"
#include "net.h"
#include "npcs.h"

#include <Game.h>
#include <Script.h>
#include <Windows.h>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <iterator>
#include <map>
#include <string>

namespace g3t::respawn {
namespace {
// Respawn (Script_Game.dll 0xac3c0, self = the dead NPC; re/findings/hero-death.md 4, re/out/hero1-scriptgame.c): a success
// sets AIMode 0, refills it (RefreshHitPoints, RefreshWeaponry), moves it to its spawn point and returns 1; a failed roll
// sets species 40 and moves it out of the world (MoveTo -220000,100000,100000); too early or the player within 30 m: nothing.
constexpr uintptr_t RespawnRva = 0xac3c0;
constexpr uint8_t Kind = 14;  // net::WorldState kind: guard 1 = stood up again, 2 = out of the world; position and rotation
constexpr uint8_t StoodUp = 1, Gone = 2;
constexpr int GoneSpecies = 40;  // gESpecies the game gives an NPC it takes out of the world (hero-death.md 4)
gFScript original = nullptr;
bool installed = false, native = false;
DWORD lastTry = 0;
bool enabled = true;  // QA negative control: respawnoff
unsigned rolls = 0, stood = 0, gone = 0, kept = 0, mirrorsSkipped = 0, applied = 0;
// Results for NPCs not in this part of the world when they came: applied once this machine has them (as the journal does).
struct Pending
{
    uint8_t from;
    net::WorldState state;
};
std::map<std::string, Pending> pending;
DWORD lastPending = 0;

unsigned char* NativeRespawn()
{
    static const unsigned char expected[] = {0x55, 0x8b, 0xec, 0x83, 0xe4, 0xf8, 0x81, 0xec, 0xbc, 0x03, 0x00, 0x00};
    HMODULE module = GetModuleHandleA("Script_Game.dll");
    if (!module) return nullptr;
    auto* code = reinterpret_cast<unsigned char*>(module) + RespawnRva;
    return std::memcmp(code, expected, sizeof(expected)) == 0 ? code : nullptr;
}

bool Dead(Entity& npc) { return npc.Routine.AIMode == gEAIMode_Dead; }

// The routine's task stayed the death task it ended with (ZS_RagDollDead): the next lethal hit set that task again, which
// does not run it again, so the animal stood at 0 hit points and never died a second time (stageRespawn at Kent, 14:08).
void EndDeathTask(Entity& npc)
{
    const bCString task = npc.Routine.GetCurrentTask();
    if (!std::strstr(task.GetText(), "Dead")) return;
    npc.Routine.FullStop();
    npc.Routine.SetTask(bCString("ZS_Ambient"));
    npc.Routine.ContinueRoutine();
    Logf("respawn: %s still had its death task %s; now %s", npc.GetName().GetText(), task.GetText(),
         npc.Routine.GetCurrentTask().GetText());
}

void Send(eCEntity* instance, uint8_t outcome)
{
    net::WorldState s{};
    s.kind = Kind;
    const eCEntityProxy proxy(instance);
    std::memcpy(s.guid, &proxy.GetEntityID().GetCore(), 16);
    s.guard = outcome;
    const bCVector& p = instance->GetWorldPosition();
    s.position[0] = p.GetX(); s.position[1] = p.GetY(); s.position[2] = p.GetZ();
    const bCQuaternion q = instance->GetWorldRotation();
    s.rotation[0] = q.GetX(); s.rotation[1] = q.GetY(); s.rotation[2] = q.GetZ(); s.rotation[3] = q.GetW();
    net::SendWorldState(s, 0);
}

GEInt GE_STDCALL Detour(gCScriptProcessingUnit* spu, GELPVoid self, GELPVoid other, GEInt args)
{
    eCEntity* instance = self ? static_cast<Entity*>(self)->GetInstance() : (spu ? spu->GetSelfEntity() : nullptr);
    if (!enabled || !instance || !net::Connected()) return original(spu, self, other, args);
    // A mirror is simulated on the host: the host rolls (or has rolled) for it, its result comes as kind 14.
    if (npcs::IsMirrored(instance)) { ++mirrorsSkipped; return 0; }
    Entity npc(instance);
    const bool deadBefore = Dead(npc);
    const bCVector before = instance->GetWorldPosition();
    const GEInt result = original(spu, self, other, args);
    ++rolls;
    if (!deadBefore) return result;
    if (!Dead(npc)) {
        ++stood;
        EndDeathTask(npc);
        journal::NewLife(instance);
        Send(instance, StoodUp);
        Logf("respawn: %s stands up again here; everyone is told", npc.GetName().GetText());
    } else if ((instance->GetWorldPosition() - before).GetMagnitude() > 10000.0f) {
        ++gone;
        Send(instance, Gone);
        Logf("respawn: %s does not come back (taken out of the world); everyone is told", npc.GetName().GetText());
    } else {
        ++kept;
    }
    return result;
}

// Another machine rolled for this NPC: its copy here does the same, as the game's own Respawn would have done it.
bool Apply(uint8_t from, net::WorldState const& s)
{
    eCEntity* instance = npcs::NpcByGuid(s.guid);
    if (!instance) return false;
    // Its death has not reached this copy yet (the host mirrored the client's animal and applies the death only once it
    // processes it; run 135700: the result came first and found it alive): the death first, then the result.
    if (journal::Pending(instance)) return false;
    Entity npc(instance);
    const bCMatrix pose = body::Pose(*instance, s.position, s.rotation);
    if (s.guard == StoodUp && !Dead(npc)) {  // never dead here (or the snapshots brought it): at its spawn point, refilled
        npc.MoveTo(pose);
        GetScriptAdminExt().CallScript(bCString("RefreshHitPoints"), instance, nullptr, 0);
        Logf("respawn: player %u's roll stood %s up again; alive here, moved to its place", from, npc.GetName().GetText());
    } else if (s.guard == StoodUp) {
        journal::NewLife(instance);
        npc.Routine.FullStop();
        npc.NPC.DefeatedByPlayer = GEFalse;
        npc.Routine.AIMode = gEAIMode_Routine;
        npc.NPC.ForceNextPose(gEPose_P0);
        npc.MoveTo(pose);
        GetScriptAdminExt().CallScript(bCString("RefreshHitPoints"), instance, nullptr, 0);
        GetScriptAdminExt().CallScript(bCString("RefreshWeaponry"), instance, nullptr, 0);
        npc.Routine.ContinueRoutine();
        EndDeathTask(npc);
        Logf("respawn: player %u's roll stood %s up again; here too (hp %d)", from, npc.GetName().GetText(),
             static_cast<int>(npc.DamageReceiver.HitPoints));
    } else {
        npc.NPC.Species = static_cast<gESpecies>(GoneSpecies);
        npc.MoveTo(pose);
        Logf("respawn: player %u's roll took %s out of the world; here too", from, npc.GetName().GetText());
    }
    ++applied;
    return true;
}
}

void Tick()
{
    const DWORD now = GetTickCount();
    if (!pending.empty() && now - lastPending >= 250 && gCSession::GetInstance().IsGameRunning()) {
        lastPending = now;
        for (auto it = pending.begin(); it != pending.end();)
            it = Apply(it->second.from, it->second.state) ? pending.erase(it) : std::next(it);
    }
    if (installed || !gCSession::GetInstance().IsGameRunning()) return;
    if (now - lastTry < 1000) return;
    lastTry = now;
    gSScript* script = nullptr;
    if (!GetScriptAdminExt().GetScripts().GetAt(bCString("Respawn"), script) || !script || !script->m_funcScript) return;
    if (unsigned char* code = NativeRespawn(); code && reinterpret_cast<void*>(script->m_funcScript) == code) {
        original = reinterpret_cast<gFScript>(ai::JumpTo(code, reinterpret_cast<void*>(&Detour)));
        native = original != nullptr;
    }
    installed = true;
    Logf("respawn: Respawn %s", native ? "wrapped (native jump)" : "not wrapped (another build): each machine rolls for itself");
}

void OnState(uint8_t from, net::WorldState const& s)
{
    if (s.guard != StoodUp && s.guard != Gone) return;
    if (s.guard == StoodUp) journal::NewLife(s.guid);  // a new death of it is reported again, here or wherever it dies
    if (!Apply(from, s)) pending[std::string(reinterpret_cast<char const*>(s.guid), 16)] = Pending{from, s};
}

void Report(LogFn log)
{
    char line[220];
    Format(line, "RESPAWNSYNC enabled=%d installed=%d native=%d rolls=%u stood=%u gone=%u kept=%u mirrors_skipped=%u applied=%u",
              enabled ? 1 : 0, installed ? 1 : 0, native ? 1 : 0, rolls, stood, gone, kept, mirrorsSkipped, applied);
    log(line);
}

void Off(LogFn log)
{
    enabled = false;
    log("RESPAWNOFF ok=1");
}
}
