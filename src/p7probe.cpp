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
enum Probe { WatchFight, Warn, SendWarn, ToKill, UseObject, SendUse, HasNews, Agressor,
             // 26.10. (P7 attitudes, pair 1): the parts of GetAttitude and the theft and zone rules that read the game's
             // player; called directly by the game, so counted from the function itself. Logged only for another player.
             ZoneAtt, Political, Species, Bothered, MemorizeEnclave, EnterZone, TakeItem, PlayerZone, Count };
struct Site {
    char const* name; uintptr_t rva; bool quiet; void* original;
    unsigned calls, puppet, positive;
    unsigned asPuppet, asPuppetPositive, logged, loggedHero;  // asPuppet: run for a puppet (as the player or GetAttitude's)
    unsigned puppetResults, heroResults;  // bit n: result n seen (n < 16), for a puppet / for the local hero
};
Site sites[Count] = {{"OnWatchFight", 0x27e40}, {"AssessWarn", 0x318c0}, {"SendPerceptionWarn", 0x32bd0},
                     {"HasAttackReasonToKill", 0x103b0}, {"AssessUseInteractObject", 0x316f0}, {"SendPerceptionUseInteractObject", 0x32a90},
                     {"HasGuardNews", 0xbcf00}, {"AssessAgressor", 0x24490},
                     {"GetZoneAttitudeToPlayer", 0xf410, true}, {"GetPoliticalAttitude", 0xf620, true}, {"GetSpeciesAttitude", 0xfbc0, true},
                     {"IsBotheredByPlayerTheftOf", 0x30840, true}, {"MemorizeEnclaveCrime", 0x60670, true},
                     {"AssessPlayerEnterZone", 0x30000, true}, {"AssessTakeItem", 0x30620, true}, {"AssessPlayerZone", 0x2fca0, true}};
bool installed = false;
constexpr unsigned Logged = 60;  // lines per probe
constexpr unsigned LoggedHero = 15;  // quiet probes: lines for the local hero

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
    eCEntity* actor = guards::CurrentActor();
    eCEntity* answeredFor = ai::RedirectedFor();
    const bool forPuppet = remote::IsPuppet(actor) || answeredFor || remote::IsPuppet(other);
    const unsigned bit = result >= 0 && result < 16 ? 1u << result : 0x8000u;
    if (forPuppet) {
        ++s.asPuppet;
        if (result) ++s.asPuppetPositive;
        s.puppetResults |= bit;
    } else {
        s.heroResults |= bit;
    }
    if (s.quiet) {
        // Logged for a puppet only where it stands for the game's player or is asked about itself: the GetAttitude redirect
        // asks the parts thousands of times (counted, not logged).
        const bool acting = remote::IsPuppet(actor) || remote::IsPuppet(other);
        if (forPuppet && !acting) return;
        unsigned& n = forPuppet ? s.logged : s.loggedHero;
        if (++n > (forPuppet ? Logged : LoggedHero)) return;
    } else if (s.calls > Logged) {
        return;
    }
    Logf("p7probe: %s self=%s other=%s target=%s player=%s mirrored=%d result=%d for=%s", s.name, NameOf(self), NameOf(other), NameOf(target),
         actor ? NameOf(actor) : "hero", self && npcs::IsMirrored(self) ? 1 : 0, static_cast<int>(result), answeredFor ? "puppet" : "-");
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

template <int P>
void* DetourOf(Probe p)
{
    if constexpr (P < Count) {
        if (p == P) return P == WatchFight ? reinterpret_cast<void*>(&WrappedWatch) : reinterpret_cast<void*>(&Wrapped<static_cast<Probe>(P)>);
        return DetourOf<P + 1>(p);
    } else {
        return nullptr;
    }
}

void* Detour(Probe p) { return DetourOf<0>(p); }
}

void On(LogFn log)
{
    char line[200];
    HMODULE game = GetModuleHandleA("Script_Game.dll");
    static const unsigned char prologue[] = {0x55, 0x8b, 0xec, 0x83, 0xe4, 0xf8};  // this build, all six
    int hooked = 0;
    // Again: the counts start over (one run measures several scenes).
    for (Site& s : sites) {
        s.calls = s.puppet = s.positive = s.asPuppet = s.asPuppetPositive = s.logged = s.loggedHero = 0;
        s.puppetResults = s.heroResults = 0;
        hooked += s.original != nullptr;
    }
    for (int p = 0; game && !installed && p < Count; ++p) {
        auto* code = reinterpret_cast<unsigned char*>(game) + sites[p].rva;
        if (std::memcmp(code, prologue, sizeof(prologue)) != 0) continue;
        sites[p].original = ai::JumpTo(code, Detour(static_cast<Probe>(p)));
        hooked += sites[p].original != nullptr;
    }
    installed = installed || hooked > 0;
    Format(line, "P7PROBEON hooked=%d installed=%d", hooked, installed ? 1 : 0);
    log(line);
}

void Report(LogFn log)
{
    char line[400];
    auto results = [](unsigned bits, char* out, size_t size) {
        size_t used = 0;
        out[0] = '-';
        out[1] = 0;
        for (int n = 0; n < 16; ++n)
            if (bits & (1u << n)) used += Format(out + used, size - used, used ? ",%d" : "%d", n);
    };
    for (Site const& s : sites) {
        char forPuppet[64], forHero[64];
        results(s.puppetResults, forPuppet, sizeof(forPuppet));
        results(s.heroResults, forHero, sizeof(forHero));
        Format(line, "P7PROBE name=%s calls=%u puppet=%u positive=%u hooked=%d as_puppet=%u as_puppet_positive=%u puppet_results=%s hero_results=%s",
                  s.name, s.calls, s.puppet, s.positive, s.original ? 1 : 0, s.asPuppet, s.asPuppetPositive, forPuppet, forHero);
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
    Format(line, "FIGHTMEMORY name=%s last=%d at=%.1f now=%.1f task=%s ar=%d target=%s kept=%s", name,
              static_cast<int>(npc.NPC.LastFightAgainstPlayer), static_cast<float>(npc.NPC.LastFightTimestamp), Entity::GetPlayingTime(),
              npc.Routine.GetCurrentTask().GetText(), static_cast<int>(npc.NPC.LastPlayerAR),
              npc.NPC.GetCurrentTarget().GetInstance() ? npc.NPC.GetCurrentTarget().GetName().GetText() : "-",
              fightmem::Kept(npc.GetInstance()).c_str());
    log(line);
}
}
