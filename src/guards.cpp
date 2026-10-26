// SPDX-License-Identifier: GPL-3.0-or-later
#include "guards.h"

#include "ai.h"
#include "fightmem.h"
#include "log.h"
#include "net.h"
#include "npcs.h"
#include "remote.h"

#include <Game.h>
#include <Script.h>
#include <Windows.h>
#include <cstdint>
#include <cstdio>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <new>
#include <unordered_map>

namespace g3t::guards {
namespace {
// AssessActivePerception (Script_Game.dll 0x295f0): an NPC's look around (re/out/p7-activeperc-b.c). It returns 0 when
// it found nothing to react to; for the player it perceived it calls AssessPlayerZone (0x2fca0) and AssessPlayerWeapon
// (0x2f780) directly, each giving up for anyone but the player.
constexpr uintptr_t PerceiveRva = 0x295f0;
gFScript originalPerceive = nullptr;
using GetPlayerFn = Entity*(GE_STDCALL*)(Entity* result);
using IsPlayerFn = GEBool(__fastcall*)(Entity const* self, void* edx);  // thiscall, called through fastcall
GetPlayerFn originalGetPlayer = nullptr;
IsPlayerFn originalIsPlayer = nullptr;
bool installed = false, enabled = true;  // enabled: QA negative control trespassoff
// Sneaking is not checked against puppets: in single-player nobody near Gelford (Reddock) noticed the sneaking hero, while
// the puppet check had the rebels there notice the sneaking client at once (stageSneak 25.10., cause not found; giving
// GetAttitude the hero as the player too changed nothing). QA sneakcheckon: checked again (negative control).
bool sneakCheck = false;
// Entering a zone is not sent for puppets: the game never sends SendPerceptionEnterZone for its own player (no caller,
// only its registration; stageEnterHome G3_WHO=host 26.10.: 0 calls, re/findings/p7-attitudes-20261026-b.md), so the
// owner of a home never came after the player in single-player but did after the puppet (owner 26.10.: take it out).
// QA enterzoneon: sent again (negative control).
bool enterZone = false;
DWORD lastTry = 0;
eCEntity* actor = nullptr;  // the puppet that stands for the game's player now
int depth = 0;
unsigned checks = 0, reactions = 0, stateRuns = 0;
std::unordered_map<eCEntity*, DWORD> lastCheck;  // per NPC: its look around runs every frame in some states
// Entering a place: the game tells the NPCs when its player walks into a zone (SendPerceptionEnterZone, 0x33170, called
// by name; AssessPlayerEnterZone 0x30000 sends the owner of a home there after the player, ZS_GotoPlayerZone;
// re/out/p7-enterzone-b.c). It has no caller in the game, though, so this is sent for puppets only with QA enterzoneon.
eCEntity* zoneOf[net::MaxPlayers + 1] = {};
DWORD lastZoneLook = 0;
unsigned zonesEntered = 0;

// Script_Game.dll's import of Entity::GetPlayer (static, __stdcall, the result returned through a hidden pointer).
Entity* GE_STDCALL ActingGetPlayer(Entity* result)
{
    if (!actor) return originalGetPlayer(result);
    return new (result) Entity(actor);
}

GEBool __fastcall ActingIsPlayer(Entity const* self, void* edx)
{
    if (!actor) return originalIsPlayer(self, edx);
    return self && self->GetInstance() == actor;
}

// This build: push ebp; mov ebp,esp; and esp,-8; sub esp,0x6a4.
unsigned char* NativePerceive()
{
    static const unsigned char expected[] = {0x55, 0x8b, 0xec, 0x83, 0xe4, 0xf8, 0x81, 0xec, 0xa4, 0x06, 0x00, 0x00};
    HMODULE module = GetModuleHandleA("Script_Game.dll");
    if (!module) return nullptr;
    auto* code = reinterpret_cast<unsigned char*>(module) + PerceiveRva;
    return std::memcmp(code, expected, sizeof(expected)) == 0 ? code : nullptr;
}

float Distance(eCEntity* a, eCEntity* b)
{
    const bCVector& p = a->GetWorldPosition();
    const bCVector& q = b->GetWorldPosition();
    const float dx = p.GetX() - q.GetX(), dy = p.GetY() - q.GetY(), dz = p.GetZ() - q.GetZ();
    return std::sqrt(dx * dx + dy * dy + dz * dz);
}

// The two rules the game checks for its own player, now for each other player near an NPC that saw nothing else.
GEInt GE_STDCALL Perceive(gCScriptProcessingUnit* spu, GELPVoid self, GELPVoid other, GEInt args)
{
    const GEInt result = originalPerceive(spu, self, other, args);
    if (result || !enabled || depth || !net::Connected()) return result;
    eCEntity* npc = self ? static_cast<Entity*>(self)->GetInstance() : (spu ? spu->GetSelfEntity() : nullptr);
    if (!npc || !npc->GetPropertySet(eEPropertySetType_NPC) || remote::IsPuppet(npc) || npcs::IsMirrored(npc)) return result;
    Entity guard(npc);
    if (guard.IsDead() || guard.IsDown()) return result;
    // At most twice a second per NPC, not for 8 s after it reacted (an observing NPC let go of the puppet within a second
    // and started over: sneakhomediag 25.10.), and the rules not while it already reacts to a player (the reaction states
    // look around too: that restarted them every frame).
    const DWORD now = GetTickCount();
    if (lastCheck.size() > 4096) lastCheck.clear();
    DWORD& last = lastCheck[npc];
    if (static_cast<int32_t>(last - now) > 0 || now - last < 500) return result;
    last = now;
    // A fight it gave up against another player first, as the game asks IsUpset of its own player before the rules
    // (fightmem.cpp); not while it fights.
    if (static_cast<gEAIMode>(guard.Routine.AIMode) != gEAIMode_Combat) {
        for (int id = 1; id <= net::MaxPlayers; ++id) {
            eCEntity* puppet = remote::PuppetOf(id);
            if (!puppet || Distance(npc, puppet) > 3000.0f || !fightmem::Upset(npc, puppet)) continue;
            ++reactions;
            remote::NoteContact(npc, puppet);
            return 1;
        }
    }
    if (remote::IsPuppet(guard.NPC.GetCurrentTarget().GetInstance())) return result;
    for (int id = 1; id <= net::MaxPlayers; ++id) {
        eCEntity* puppet = remote::PuppetOf(id);
        if (!puppet || Distance(npc, puppet) > 3000.0f) continue;  // the game's own checks decide nearer ones
        ++checks;
        ++depth;
        GEInt reacted = 0;
        char const* rule = nullptr;
        {
            Acting acting(puppet);
            // Sneaking (if checked): the puppet carries its player's AniState (body.cpp), which the check reads.
            for (char const* name : {"AssessPlayerZone", "AssessPlayerWeapon", "AssessPlayerSneaking"}) {
                if (!sneakCheck && !std::strcmp(name, "AssessPlayerSneaking")) continue;
                reacted = GetScriptAdminExt().CallScript(bCString(name), npc, puppet, 0);
                if (reacted) { rule = name + 12; break; }
            }
        }
        --depth;
        if (reacted) {
            ++reactions;
            last = now + 8000;
            remote::NoteContact(npc, puppet);
            Logf("guards: %s reacts to player %d (%s; task %s, target %s, ani state %d)", guard.GetName().GetText(), id, rule,
                 guard.Routine.GetCurrentTask().GetText(),
                 guard.NPC.GetCurrentTarget().GetInstance() ? guard.NPC.GetCurrentTarget().GetName().GetText() : "-",
                 static_cast<int>(Entity(puppet).Routine.AniState));
            return reacted;
        }
    }
    return result;
}

// The intent to kill (HasAttackReasonToKill, 0x103b0) holds for gate guards and intruders only against Entity::GetPlayer
// in its zone (re/out/p7open-b.c): Muntasir's guards never meant to kill the intruding client (intruderkodiag 25.10.: 0 for
// the client's puppet, 1 for the host's hero; after a knock-out they let it be, the host they fought on). Asked about a
// puppet, the game's player is that puppet meanwhile.
gFScript originalKill = nullptr;
bool fightsOn = true;  // QA negative control: fightsoff
unsigned killAsked = 0, killYes = 0;

// A puppet's zone as the game's checks read it: Entity::GetZone answers an NPC's navigation's current zone (Script.dll
// 0x19410, re/out/zone-b.c), which the game's own movement keeps up. A puppet is placed, not walked: it had none at times,
// and asked then, the kill intent said no (stageIntruderFight 25.10.: every "no" with the puppet in no zone, every "yes" in
// one). Before such a question and every half second, a puppet without a zone gets the zone at its position
// (gCNavigationAdmin::GetZone). QA zonefixoff: not.
bool zoneFix = true;
unsigned zonesSet = 0;

void KeepZone(eCEntity* puppet)
{
    static gCNavigationAdmin* admin = nullptr;
    if (!zoneFix || !puppet) return;
    if (!admin) admin = static_cast<gCNavigationAdmin*>(eCModuleAdmin::GetInstance().FindModule(bCString("gCNavigationAdmin")));
    auto* navigation = static_cast<gCNavigation_PS*>(puppet->GetPropertySet(eEPropertySetType_Navigation));
    // Only where it has none: the zone at the position can be the town around a guarded place (Nav_Zone with security 0
    // where the game's own tracking had the guarded one, stageIntruderFight 26.10.).
    if (!admin || !navigation || navigation->GetCurrentZoneEntityProxy().GetEntity()) return;
    eCEntityProxy here;
    GEBool path = GEFalse;
    if (admin->GetZone(*puppet, here, path, GEFalse) != bEResult_Ok || !here.GetEntity()) return;
    navigation->AccessCurrentZoneEntityProxy() = here;
    ++zonesSet;
}

GEInt GE_STDCALL KillIntent(gCScriptProcessingUnit* spu, GELPVoid self, GELPVoid other, GEInt args)
{
    eCEntity* asked = other ? static_cast<Entity*>(other)->GetInstance() : (spu ? spu->GetOtherEntity() : nullptr);
    eCEntity* puppet = fightsOn && enabled && remote::IsPuppet(asked) ? asked : nullptr;
    KeepZone(puppet);
    Acting acting(puppet);
    const GEInt result = originalKill(spu, self, other, args);
    if (puppet) {
        ++killAsked;
        if (result) ++killYes;
        eCEntity* judge = self ? static_cast<Entity*>(self)->GetInstance() : (spu ? spu->GetSelfEntity() : nullptr);
        if (judge && judge->GetPropertySet(eEPropertySetType_NPC) && killAsked <= 40) {
            Entity npc(judge);
            Entity zone = Entity(puppet).GetZone();
            Logf("guards: %s means to kill a puppet: %d (attack reason %d, its target %s, puppet in %s, security %d)", npc.GetName().GetText(),
                 static_cast<int>(result), static_cast<int>(npc.NPC.AttackReason),
                 remote::IsPuppet(npc.NPC.GetCurrentTarget().GetInstance()) ? "the puppet" : "another",
                 zone.GetInstance() ? zone.GetName().GetText() : "-", zone.GetInstance() ? static_cast<int>(zone.GetAIZoneSecurityLevel()) : -1);
        }
    }
    return result;
}

// The AI callbacks of those states (by name from the state's callback, so the table entry is enough): OnReactToZone
// asks GetPlayer whether the intruder is still in the zone, OnReactToWeapon whether the weapon is still out.
gFScriptAICallback originalOnZone = nullptr, originalOnWeapon = nullptr;

eCEntity* TargetPuppet(gCScriptProcessingUnit* spu)
{
    eCEntity* self = spu ? spu->GetSelfEntity() : nullptr;
    if (!enabled || !self || !self->GetPropertySet(eEPropertySetType_NPC)) return nullptr;
    eCEntity* target = Entity(self).NPC.GetCurrentTarget().GetInstance();
    return remote::IsPuppet(target) ? target : nullptr;
}

GEBool GE_STDCALL OnZone(gCScriptProcessingUnit* spu)
{
    Acting acting(TargetPuppet(spu));
    return originalOnZone(spu);
}

GEBool GE_STDCALL OnWeapon(gCScriptProcessingUnit* spu)
{
    Acting acting(TargetPuppet(spu));
    return originalOnWeapon(spu);
}

// A sleeping animal (ZS_AnimalSleep, AI mode 7) checks in its task callback OnAnimalSleep (Script_Game.dll 0x89f20,
// re/out/animalsleep-20261026-b.c) whether Entity::GetPlayer comes within 10 m (5 m sneaking; never with Perk_Sneak_2 when
// sneaking or aiming), and wakes and attacks it unless friendly or neutral. Only this machine's hero woke it; while it still
// sleeps after that, each puppet within 11 m is the game's player for the same check. QA animalwakeoff: not.
gFScriptAICallback originalOnSleep = nullptr;
bool animalWake = true;
unsigned sleepChecks = 0, sleepWakes = 0;

GEBool GE_STDCALL OnAnimalSleep(gCScriptProcessingUnit* spu)
{
    eCEntity* animal = spu ? spu->GetSelfEntity() : nullptr;
    const GEBool result = originalOnSleep(spu);
    if (!animalWake || depth || !animal || !net::Connected() || !animal->GetPropertySet(eEPropertySetType_NPC) ||
        remote::IsPuppet(animal) || npcs::IsMirrored(animal))
        return result;
    Entity self(animal);
    const bCString asleep = self.Routine.GetCurrentState();
    if (!std::strstr(asleep.GetText(), "Sleep")) return result;  // the game's own player woke it
    for (int id = 1; id <= net::MaxPlayers; ++id) {
        eCEntity* puppet = remote::PuppetOf(id);
        if (!puppet || Distance(animal, puppet) > 1100.0f) continue;
        ++sleepChecks;
        ++depth;
        GEBool again;
        {
            Acting acting(puppet);
            again = originalOnSleep(spu);
        }
        --depth;
        if (std::strcmp(self.Routine.GetCurrentState().GetText(), asleep.GetText())) {
            ++sleepWakes;
            remote::NoteContact(animal, puppet);
            Logf("guards: %s wakes up for player %d (%.0f m; task %s, target %s)", self.GetName().GetText(), id, Distance(animal, puppet) / 100.0f,
                 self.Routine.GetCurrentTask().GetText(),
                 self.NPC.GetCurrentTarget().GetInstance() ? self.NPC.GetCurrentTarget().GetName().GetText() : "-");
            return again;
        }
    }
    return result;
}

// An NPC whose way ended at a taken destination waits (EndGoto, goto status 6: ZS_WaitForFreeDestination) and its loop
// (0x42db0) goes on with its routine once Entity::GetPlayer is more than 1.5 m from that destination: it waited only for
// this machine's hero. While another player stands nearer the destination than the hero, that puppet is the game's
// player for the loop. QA freedestoff: not.
bool freeDest = true;
unsigned waitActs = 0;

eCEntity* WaitActor(eCEntity* self)
{
    if (!freeDest || !net::Connected() || remote::IsPuppet(self) || npcs::IsMirrored(self)) return nullptr;
    Entity dest = Entity(self).Navigation.GetCurrentDestinationPoint();
    eCEntity* point = dest.GetInstance();
    eCEntity* hero = gCSession::GetInstance().GetHero();
    if (!point || !hero) return nullptr;
    float best = Distance(hero, point);
    eCEntity* nearest = nullptr;
    for (int id = 1; id <= net::MaxPlayers; ++id) {
        eCEntity* puppet = remote::PuppetOf(id);
        if (!puppet) continue;
        const float d = Distance(puppet, point);
        if (d < best) { best = d; nearest = puppet; }
    }
    if (nearest) ++waitActs;
    return nearest;
}

// The reaction states that ask Entity::GetPlayer for the one they react to (re/out/callees-states-d1.tsv), and those
// that lead into them.
bool FollowsPlayer(char const* state)
{
    static char const* const names[] = {"ZS_ReactToZone", "ZS_GotoPlayerZone", "ZS_ObserveZonePlayer", "ZS_ObserveSuspect",
                                        "ZS_ObserveWeapon", "ZS_ReactToWeapon", "ZS_Threaten", "ZS_ReactToSneak"};
    for (char const* name : names)
        if (!std::strncmp(state, name, std::strlen(name))) return true;
    return false;
}
}

// The scripts read the story chapter of Entity::GetPlayer (GetPoliticalAttitude: in chapter 2 the Hashishin and the orcs are
// enemies; re/out/p7-half-20261026-b.c 0xf620). A puppet is made from the hero's template with chapter 0 (attchain 26.10.: the
// host's hero 1, the puppet 0), so while it stands for the player it carries the chapter, which story.cpp shares among the
// heroes: the local hero's. QA chaptercopyoff: not.
bool chapterCopy = true;
unsigned chaptersSet = 0;

static void KeepChapter(eCEntity* puppet)
{
    eCEntity* hero = gCSession::GetInstance().GetHero();
    if (!chapterCopy || !hero || !puppet->GetPropertySet(eEPropertySetType_PlayerMemory) || !hero->GetPropertySet(eEPropertySetType_PlayerMemory)) return;
    Entity body(puppet), self(hero);
    const GEI32 chapter = self.PlayerMemory.Chapter;
    if (static_cast<GEI32>(body.PlayerMemory.Chapter) == chapter) return;
    body.PlayerMemory.Chapter = chapter;
    ++chaptersSet;
}

Acting::Acting(eCEntity* puppet) : before(actor)
{
    if (puppet) {
        KeepChapter(puppet);
        actor = puppet;
    }
}

Acting::~Acting() { actor = before; }

eCEntity* CurrentActor() { return actor; }

eCEntity* StateActor(eCEntity* self, char const* state)
{
    if (!installed || !self || !state || !self->GetPropertySet(eEPropertySetType_NPC)) return nullptr;
    if (!std::strcmp(state, "ZS_WaitForFreeDestination_Loop")) return WaitActor(self);  // its own switch: freedestoff
    if (!enabled || !FollowsPlayer(state)) return nullptr;
    eCEntity* target = Entity(self).NPC.GetCurrentTarget().GetInstance();
    if (!target || !remote::IsPuppet(target)) return nullptr;
    ++stateRuns;
    return target;
}

static void WatchZones()
{
    const DWORD now = GetTickCount();
    if (!enabled || !originalPerceive || now - lastZoneLook < 500 || !net::Connected()) return;
    lastZoneLook = now;
    for (int id = 1; id <= net::MaxPlayers; ++id) {
        eCEntity* puppet = remote::PuppetOf(id);
        KeepZone(puppet);
        eCEntity* zone = puppet ? Entity(puppet).GetZone().GetInstance() : nullptr;
        if (zone == zoneOf[id]) continue;
        zoneOf[id] = zone;
        if (!zone || !enterZone) continue;
        ++zonesEntered;
        ++depth;
        {
            Acting acting(puppet);
            GetScriptAdminExt().CallScript(bCString("SendPerceptionEnterZone"), puppet, zone, 0);
        }
        --depth;
        Logf("guards: player %d entered %s", id, Entity(zone).GetName().GetText());
    }
}

void Tick()
{
    if (installed) {
        if (gCSession::GetInstance().IsGameRunning()) WatchZones();
        return;
    }
    if (!gCSession::GetInstance().IsGameRunning()) return;
    const DWORD now = GetTickCount();
    if (now - lastTry < 1000) return;
    lastTry = now;
    HMODULE game = GetModuleHandleA("Script_Game.dll");
    unsigned char* code = NativePerceive();
    if (!game || !code) {
        installed = true;
        Logf("guards: not installed (other build)");
        return;
    }
    originalGetPlayer = reinterpret_cast<GetPlayerFn>(ai::PatchImport(game, "Script.dll", "?GetPlayer@Entity@@SG?AV1@XZ", reinterpret_cast<void*>(&ActingGetPlayer)));
    originalIsPlayer = reinterpret_cast<IsPlayerFn>(ai::PatchImport(game, "Script.dll", "?IsPlayer@Entity@@QBE_NXZ", reinterpret_cast<void*>(&ActingIsPlayer)));
    if (originalGetPlayer && originalIsPlayer) {
        originalPerceive = reinterpret_cast<gFScript>(ai::JumpTo(code, reinterpret_cast<void*>(&Perceive)));
        // This build: the same prologue as AssessActivePerception's first six bytes.
        static const unsigned char prologue[] = {0x55, 0x8b, 0xec, 0x83, 0xe4, 0xf8};
        auto* kill = reinterpret_cast<unsigned char*>(game) + 0x103b0;
        if (!std::memcmp(kill, prologue, sizeof(prologue))) originalKill = reinterpret_cast<gFScript>(ai::JumpTo(kill, reinterpret_cast<void*>(&KillIntent)));
        GE_MAP_FOR_EACH (name, callback, GetScriptAdminExt().GetScriptAICallbacks())
        {
            if (!callback || !callback->m_funcScriptAICallback) continue;
            if (!std::strcmp(name.GetText(), "OnReactToZone") && callback->m_funcScriptAICallback != &OnZone) {
                originalOnZone = callback->m_funcScriptAICallback;
                callback->m_funcScriptAICallback = &OnZone;
            } else if (!std::strcmp(name.GetText(), "OnReactToWeapon") && callback->m_funcScriptAICallback != &OnWeapon) {
                originalOnWeapon = callback->m_funcScriptAICallback;
                callback->m_funcScriptAICallback = &OnWeapon;
            } else if (!std::strcmp(name.GetText(), "OnAnimalSleep") && callback->m_funcScriptAICallback != &OnAnimalSleep) {
                originalOnSleep = callback->m_funcScriptAICallback;
                callback->m_funcScriptAICallback = &OnAnimalSleep;
            }
        }
    }
    installed = true;
    Logf("guards: GetPlayer %s, IsPlayer %s, AssessActivePerception %s, OnReactToZone %s, OnReactToWeapon %s, "
         "HasAttackReasonToKill %s, OnAnimalSleep %s",
         originalGetPlayer ? "redirectable" : "not found", originalIsPlayer ? "redirectable" : "not found",
         originalPerceive ? "wrapped" : "not wrapped", originalOnZone ? "wrapped" : "not found", originalOnWeapon ? "wrapped" : "not found",
         originalKill ? "wrapped" : "not wrapped", originalOnSleep ? "wrapped" : "not found");
}

void Report(LogFn log)
{
    char line[480];
    Format(line, "GUARDS installed=%d enabled=%d perceive=%d checks=%u reactions=%u state_runs=%u zones_entered=%u fights=%d kill_asked=%u kill_yes=%u zones_set=%u"
                    " chapter_copy=%d chapters_set=%u animal_wake=%d sleep_wrapped=%d sleep_checks=%u sleep_wakes=%u free_dest=%d wait_acts=%u",
              installed ? 1 : 0, enabled ? 1 : 0, originalPerceive ? 1 : 0, checks, reactions, stateRuns, zonesEntered, fightsOn ? 1 : 0,
              killAsked, killYes, zonesSet, chapterCopy ? 1 : 0, chaptersSet, animalWake ? 1 : 0, originalOnSleep ? 1 : 0, sleepChecks,
              sleepWakes, freeDest ? 1 : 0, waitActs);
    log(line);
}

void AnimalWakeOff(LogFn log)
{
    animalWake = false;
    log("ANIMALWAKEOFF ok=1");
}

void FreeDestOff(LogFn log)
{
    freeDest = false;
    log("FREEDESTOFF ok=1");
}

void Off(LogFn log)
{
    enabled = false;
    log("TRESPASSOFF ok=1");
}

void FightsOff(LogFn log)
{
    fightsOn = false;
    log("FIGHTSOFF ok=1");
}

void ChapterCopyOff(LogFn log)
{
    chapterCopy = false;
    log("CHAPTERCOPYOFF ok=1");
}

void ZoneFixOff(LogFn log)
{
    zoneFix = false;
    log("ZONEFIXOFF ok=1");
}

void SneakCheckOn(LogFn log)
{
    sneakCheck = true;
    log("SNEAKCHECKON ok=1");
}

void EnterZoneOn(LogFn log)
{
    enterZone = true;
    log("ENTERZONEON ok=1");
}

// NPCs within `radius` cm of the local hero whose home (the zone of their sleeping point) they are not in now: where a
// test can walk into someone's home while the owner is away (AssessPlayerEnterZone). "<radius>_all": also those at
// home, with the home zone's position (where a scan looks for houses, homescandiag).
void Homes(char const* argument, LogFn log)
{
    char line[480];
    eCEntity* hero = gCSession::GetInstance().GetHero();
    if (!hero) { log("REFUSED homes: no hero"); return; }
    const float within = argument && *argument ? static_cast<float>(std::atof(argument)) : 4000.0f;
    const bool all = argument && std::strstr(argument, "all");
    const bTObjArray<Entity> list = Entity::GetNPCs();
    for (int i = 0; i < list.GetCount(); ++i) {
        Entity npc = list.GetAt(i);
        if (!npc.GetInstance() || npc.IsDead() || remote::IsPuppet(npc.GetInstance()) || Distance(hero, npc.GetInstance()) > within) continue;
        Entity bed = npc.Navigation.GetSleepingPoint();
        if (!bed.GetInstance()) continue;
        Entity home = bed.GetZone(), now = npc.GetZone();
        if (!home.GetInstance() || (!all && home.GetInstance() == now.GetInstance())) continue;
        const bCVector& at = bed.GetInstance()->GetWorldPosition();
        const bCVector& zone = home.GetInstance()->GetWorldPosition();
        // 26.10. (pair 1): AssessPlayerEnterZone sends the owner only where the home zone's attitude to the player is not
        // friendly (GetZoneAttitudeToPlayer with the zone as self: its AI owner, security level and the player's fame).
        Entity owner = home.GetAIZoneOwner();
        const GEInt zatt = GetScriptAdminExt().CallScript(bCString("GetZoneAttitudeToPlayer"), home.GetInstance(), nullptr, 0);
        Format(line, "HOME npc=%s home=%s now=%s bed=%.0f,%.0f,%.0f zone_at=%.0f,%.0f,%.0f distance=%.0f mirrored=%d at_home=%d security=%d"
                        " zone_owner=%s zatt=%d",
                  npc.GetName().GetText(), home.GetName().GetText(), now.GetInstance() ? now.GetName().GetText() : "-", at.GetX(), at.GetY(),
                  at.GetZ(), zone.GetX(), zone.GetY(), zone.GetZ(), Distance(hero, npc.GetInstance()), npcs::IsMirrored(npc.GetInstance()) ? 1 : 0,
                  home.GetInstance() == now.GetInstance() ? 1 : 0, static_cast<int>(home.GetAIZoneSecurityLevel()),
                  owner.GetInstance() ? owner.GetName().GetText() : "-", static_cast<int>(zatt));
        log(line);
    }
    log("HOMES end");
}

void Sneak(char const* on, LogFn log)
{
    char line[160];
    eCEntity* hero = gCSession::GetInstance().GetHero();
    if (!hero) { log("REFUSED sneak: no hero"); return; }
    Entity self(hero);
    const bool sneak = on && *on == '1';
    self.SetMovementMode(sneak ? gECharMovementMode_Sneak : gECharMovementMode_Run);
    // As the sneak key does (OnPlayerSneak, Script_Game 0x63a00, re/out/rest-sneakkey-b.c): it keeps whether the key is held
    // in a flag the player's states read; without it the hero stood again at once (stageSneak 25.10.: AniState 2 again).
    if (HMODULE game = GetModuleHandleA("Script_Game.dll")) *(reinterpret_cast<volatile uint8_t*>(game) + 0x118b42) = sneak ? 1 : 0;
    if (!std::strcmp(self.Routine.GetCurrentTask().GetText(), "PS_Normal")) self.Routine.SetState(bCString(sneak ? "PS_Normal_Sneak" : "PS_Normal_Loop"));
    self.Routine.AniState = sneak ? gEAniState_Sneak : gEAniState_Stand;
    Format(line, "SNEAK on=%d ani_state=%d task=%s state=%s", sneak ? 1 : 0, static_cast<int>(self.Routine.AniState),
              self.Routine.GetCurrentTask().GetText(), self.Routine.GetCurrentState().GetText());
    log(line);
}
}
