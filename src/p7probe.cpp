// SPDX-License-Identifier: GPL-3.0-or-later
// QA measurement for the open P7 points (owner 25.10. evening): who runs the game's onlooker memory (OnWatchFight),
// warnings (SendPerceptionWarn -> AssessWarn), the intent to kill (HasAttackReasonToKill) and the use of other people's
// things (SendPerceptionUseInteractObject -> AssessUseInteractObject), for which player, and with what result. Off until
// QA p7probeon; then each call is counted and the first ones logged with self, other, the player the game sees
// (Entity::GetPlayer, as guards.cpp may redirect it) and the result. Changes nothing the game does.
#include "p7probe.h"

#include "ai.h"
#include "fightmem.h"
#include "guards.h"
#include "log.h"
#include "npcs.h"
#include "remote.h"

#include <Game.h>
#include <Script.h>
#include <Windows.h>
#include <cstdio>
#include <cstring>

namespace g3t::p7probe {
namespace {
enum Probe { WatchFight, Warn, SendWarn, ToKill, UseObject, SendUse, HasNews, Agressor, Count };
struct Site { char const* name; uintptr_t rva; void* original; unsigned calls, puppet, positive; };
Site sites[Count] = {{"OnWatchFight", 0x27e40}, {"AssessWarn", 0x318c0}, {"SendPerceptionWarn", 0x32bd0},
                     {"HasAttackReasonToKill", 0x103b0}, {"AssessUseInteractObject", 0x316f0}, {"SendPerceptionUseInteractObject", 0x32a90},
                     {"HasGuardNews", 0xbcf00}, {"AssessAgressor", 0x24490}};
bool installed = false;
constexpr unsigned Logged = 60;  // lines per probe

using Script = GEInt(GE_STDCALL*)(gCScriptProcessingUnit*, GELPVoid, GELPVoid, GEInt);
using Callback = GEBool(GE_STDCALL*)(gCScriptProcessingUnit*);

char const* NameOf(eCEntity* entity)
{
    if (!entity) return "-";
    if (remote::IsPuppet(entity)) return "puppet";
    if (entity == gCSession::GetInstance().GetHero()) return "hero";
    return entity->GetName().GetText();
}

eCEntity* Of(gCScriptProcessingUnit* spu, GELPVoid entity, bool self)
{
    if (entity) return static_cast<Entity*>(entity)->GetInstance();
    return spu ? (self ? spu->GetSelfEntity() : spu->GetOtherEntity()) : nullptr;
}

void Note(Probe p, eCEntity* self, eCEntity* other, eCEntity* target, GEInt result)
{
    Site& s = sites[p];
    ++s.calls;
    if (remote::IsPuppet(other) || remote::IsPuppet(target)) ++s.puppet;
    if (result) ++s.positive;
    if (s.calls > Logged) return;
    eCEntity* actor = guards::CurrentActor();
    Logf("p7probe: %s self=%s other=%s target=%s player=%s mirrored=%d result=%d", s.name, NameOf(self), NameOf(other), NameOf(target),
         actor ? NameOf(actor) : "hero", self && npcs::IsMirrored(self) ? 1 : 0, static_cast<int>(result));
}

eCEntity* TargetOf(eCEntity* self)
{
    return self && self->GetPropertySet(eEPropertySetType_NPC) ? Entity(self).NPC.GetCurrentTarget().GetInstance() : nullptr;
}

template <Probe P>
GEInt GE_STDCALL Wrapped(gCScriptProcessingUnit* spu, GELPVoid self, GELPVoid other, GEInt args)
{
    eCEntity* s = Of(spu, self, true);
    eCEntity* o = Of(spu, other, false);
    const GEInt result = reinterpret_cast<Script>(sites[P].original)(spu, self, other, args);
    Note(P, s, o, TargetOf(s), result);
    return result;
}

GEBool GE_STDCALL WrappedWatch(gCScriptProcessingUnit* spu)
{
    eCEntity* self = spu ? spu->GetSelfEntity() : nullptr;
    eCEntity* target = spu ? spu->GetTargetEntity() : nullptr;
    const GEBool result = reinterpret_cast<Callback>(sites[WatchFight].original)(spu);
    Note(WatchFight, self, target, TargetOf(self), result);
    return result;
}

void* Detour(Probe p)
{
    switch (p) {
    case WatchFight: return reinterpret_cast<void*>(&WrappedWatch);
    case Warn: return reinterpret_cast<void*>(&Wrapped<Warn>);
    case SendWarn: return reinterpret_cast<void*>(&Wrapped<SendWarn>);
    case ToKill: return reinterpret_cast<void*>(&Wrapped<ToKill>);
    case UseObject: return reinterpret_cast<void*>(&Wrapped<UseObject>);
    case SendUse: return reinterpret_cast<void*>(&Wrapped<SendUse>);
    case HasNews: return reinterpret_cast<void*>(&Wrapped<HasNews>);
    case Agressor: return reinterpret_cast<void*>(&Wrapped<Agressor>);
    default: return nullptr;
    }
}
}

void On(LogFn log)
{
    char line[200];
    HMODULE game = GetModuleHandleA("Script_Game.dll");
    static const unsigned char prologue[] = {0x55, 0x8b, 0xec, 0x83, 0xe4, 0xf8};  // this build, all six
    int hooked = 0;
    for (int p = 0; game && !installed && p < Count; ++p) {
        auto* code = reinterpret_cast<unsigned char*>(game) + sites[p].rva;
        if (std::memcmp(code, prologue, sizeof(prologue)) != 0) continue;
        sites[p].original = ai::JumpTo(code, Detour(static_cast<Probe>(p)));
        hooked += sites[p].original != nullptr;
    }
    installed = installed || hooked > 0;
    sprintf_s(line, "P7PROBEON hooked=%d installed=%d", hooked, installed ? 1 : 0);
    log(line);
}

void Report(LogFn log)
{
    char line[200];
    for (Site const& s : sites) {
        sprintf_s(line, "P7PROBE name=%s calls=%u puppet=%u positive=%u", s.name, s.calls, s.puppet, s.positive);
        log(line);
    }
    log("P7PROBE end");
}

// QA: an NPC's memory of the last fight it saw the player in (OnWatchFight sets Friendly, 5) and when.
void FightMemory(char const* name, LogFn log)
{
    char line[300];
    Entity npc = Entity::GetEntity(bCString(name));
    if (!npc.GetInstance() || !npc.GetInstance()->GetPropertySet(eEPropertySetType_NPC)) { log("REFUSED fightmemory: no such NPC"); return; }
    sprintf_s(line, "FIGHTMEMORY name=%s last=%d at=%.1f now=%.1f task=%s ar=%d target=%s kept=%s", name,
              static_cast<int>(npc.NPC.LastFightAgainstPlayer), static_cast<float>(npc.NPC.LastFightTimestamp), Entity::GetPlayingTime(),
              npc.Routine.GetCurrentTask().GetText(), static_cast<int>(npc.NPC.LastPlayerAR),
              npc.NPC.GetCurrentTarget().GetInstance() ? npc.NPC.GetCurrentTarget().GetName().GetText() : "-",
              fightmem::Kept(npc.GetInstance()).c_str());
    log(line);
}
}
