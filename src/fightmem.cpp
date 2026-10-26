// SPDX-License-Identifier: GPL-3.0-or-later
#include "fightmem.h"

#include "ai.h"
#include "guards.h"
#include "log.h"
#include "net.h"
#include "npcs.h"
#include "remote.h"

#include <Game.h>
#include <Script.h>
#include <Windows.h>
#include <cstdio>
#include <cstring>
#include <initializer_list>
#include <string>
#include <unordered_map>
#include <vector>

namespace g3t::fightmem {
namespace {
constexpr uint8_t Kind = 18;  // net::WorldState kind (17: the epilogue, ending.cpp): guard = the player it is about, npc = LastFightAgainstPlayer
bool on = true;               // QA negative control: fightmemoff
bool apart = true;            // QA negative control (grudgeoff): the NPC keeps what it remembers of another player, no grudge
bool installed = false;
DWORD lastTry = 0;
unsigned sounds = 0, defeats = 0, watched = 0, ends = 0, stateRuns = 0, sent = 0, applied = 0;
unsigned defeatsSeen = 0, defeatsYes = 0;  // QA: every AssessDefeat here and those that reacted (helped, attacked, fled)
unsigned upsetAsked = 0, upsets = 0;

using Script = GEInt(GE_STDCALL*)(gCScriptProcessingUnit*, GELPVoid, GELPVoid, GEInt);
using Callback = GEBool(GE_STDCALL*)(gCScriptProcessingUnit*);
Script originalSound = nullptr, originalDefeated = nullptr, originalDefeat = nullptr, originalCancel = nullptr;
Callback originalWatch = nullptr;

eCEntity* Of(gCScriptProcessingUnit* spu, GELPVoid entity, bool self)
{
    if (entity) return static_cast<Entity*>(entity)->GetInstance();
    return spu ? (self ? spu->GetSelfEntity() : spu->GetOtherEntity()) : nullptr;
}

bool IsNpc(eCEntity* entity) { return entity && entity->GetPropertySet(eEPropertySetType_NPC); }
eCEntity* TargetOf(eCEntity* npc) { return IsNpc(npc) ? Entity(npc).NPC.GetCurrentTarget().GetInstance() : nullptr; }
eCEntity* AlternativeOf(eCEntity* npc) { return IsNpc(npc) ? Entity(npc).NPC.GetAlternativeTarget().GetInstance() : nullptr; }
eCEntity* AttackerOf(eCEntity* npc) { return IsNpc(npc) ? Entity(npc).NPC.GetCurrentAttacker().GetInstance() : nullptr; }

eCEntity* PuppetAmong(std::initializer_list<eCEntity*> fighters)
{
    if (!on) return nullptr;
    for (eCEntity* e : fighters)
        if (remote::IsPuppet(e)) return e;
    return nullptr;
}

struct Memory {
    eCEntity* npc = nullptr;
    int value = 0;
    float at = 0.0f;
    int reason = 0;  // LastPlayerAR
};

Memory Read(eCEntity* npc)
{
    if (!IsNpc(npc) || remote::IsPuppet(npc)) return {};
    Entity e(npc);
    return {npc, static_cast<int>(e.NPC.LastFightAgainstPlayer), static_cast<float>(e.NPC.LastFightTimestamp),
            static_cast<int>(e.NPC.LastPlayerAR)};
}

void Write(eCEntity* npc, Memory const& m)
{
    Entity e(npc);
    e.NPC.LastFightAgainstPlayer = static_cast<gELastFightAgainstPlayer>(m.value);
    e.NPC.LastFightTimestamp = m.at;
    e.NPC.LastPlayerAR = static_cast<gEAttackReason>(m.reason);
}

int ReasonOf(eCEntity* npc) { return IsNpc(npc) ? static_cast<int>(Entity(npc).NPC.AttackReason) : 0; }

// The game has one memory of "the player" per NPC. On the machine that simulates the NPC, what it remembers of a fight
// with another player is kept apart (a fight given up against the puppet, CancelAttack with the puppet as the player,
// stayed in the host's NPC, where IsUpset would hold it against the host's hero); the NPC keeps this machine's player's.
std::unordered_map<std::string, Memory> grudges;  // key: the NPC's GUID and the player id

std::string Key(eCEntity* npc, int player)
{
    const eCEntityProxy proxy(npc);
    std::string key(reinterpret_cast<char const*>(&proxy.GetEntityID().GetCore()), 16);
    key += static_cast<char>(player);
    return key;
}

// After a script ran with `puppet` as the game's player: what the NPC now remembers is about the puppet's player. It goes
// to that player's machine (its copy of the NPC remembers the same); here the NPC gets its own player's memory back, and
// this machine keeps the other player's for the grudge (Upset). `reason`: the NPC's attack reason before the script; the
// game sets LastPlayerAR for its own player only, so the reason it fought the puppet for stands in.
void Pass(Memory const& before, eCEntity* puppet, char const* where, int reason = 0)
{
    if (!before.npc || !puppet) return;
    Memory now = Read(before.npc);
    if (now.value == before.value && now.at == before.at && now.reason == before.reason) return;
    if (apart) Write(before.npc, before);
    if (npcs::IsMirrored(before.npc)) return;  // its owner keeps it
    // Only what the script changed is about the other player (an onlooker's earlier memory is this machine's player's).
    const int player = remote::PuppetOwner(puppet);
    Memory& kept = grudges[Key(before.npc, player)];
    const Memory was = kept.npc ? kept : Memory{before.npc, 0, 0.0f, 0};
    kept = was;
    kept.npc = before.npc;
    if (now.value != before.value) kept.value = now.value;
    if (now.at != before.at) kept.at = now.at;
    if (now.reason != before.reason) kept.reason = now.reason;
    else if (reason && now.value != before.value) kept.reason = reason;
    now = kept;
    if ((now.value == was.value && now.at == was.at) || !net::Connected()) return;
    net::WorldState s{};
    s.kind = Kind;
    const eCEntityProxy proxy(before.npc);
    std::memcpy(s.guid, &proxy.GetEntityID().GetCore(), 16);
    s.guard = static_cast<uint8_t>(remote::PuppetOwner(puppet));
    s.npc = static_cast<uint8_t>(now.value);
    net::SendWorldState(s, 0);
    ++sent;
    Logf("fightmem: %s remembers a fight with player %u: %d (%s, reason %d)", Entity(before.npc).GetName().GetText(), s.guard, now.value,
         where, now.reason);
}

// The scripts as the game calls them (self, other); the fight they are about is other's (AssessFightSound, AssessDefeat:
// the one hit or knocked out, and its attacker) or self's and other's (Defeat, CancelAttack).
GEInt GE_STDCALL Sound(gCScriptProcessingUnit* spu, GELPVoid self, GELPVoid other, GEInt args)
{
    eCEntity* victim = Of(spu, other, false);
    eCEntity* puppet = PuppetAmong({victim, AttackerOf(victim)});
    const Memory before = Read(Of(spu, self, true));
    guards::Acting acting(puppet);
    const GEInt result = originalSound(spu, self, other, args);
    if (puppet) {
        ++sounds;
        Pass(before, puppet, "fight sound");
    }
    return result;
}

GEInt GE_STDCALL Defeated(gCScriptProcessingUnit* spu, GELPVoid self, GELPVoid other, GEInt args)
{
    eCEntity* seer = Of(spu, self, true);
    eCEntity* down = Of(spu, other, false);
    eCEntity* attacker = AttackerOf(down);
    eCEntity* puppet = PuppetAmong({down, attacker});
    const Memory before = Read(seer);
    guards::Acting acting(puppet);
    const GEInt result = originalDefeated(spu, self, other, args);
    ++defeatsSeen;
    if (result && ++defeatsYes <= 40 && seer)
        Logf("fightmem: %s saw %s knocked out by %s%s: task %s, target %s", Entity(seer).GetName().GetText(),
             down ? Entity(down).GetName().GetText() : "-", attacker ? Entity(attacker).GetName().GetText() : "-",
             puppet ? " (a puppet)" : "", Entity(seer).Routine.GetCurrentTask().GetText(),
             TargetOf(seer) ? Entity(TargetOf(seer)).GetName().GetText() : "-");
    if (puppet) {
        ++defeats;
        Pass(before, puppet, "knock-out seen");
    }
    return result;
}

GEInt GE_STDCALL Defeat(gCScriptProcessingUnit* spu, GELPVoid self, GELPVoid other, GEInt args)
{
    eCEntity* a = Of(spu, self, true);
    eCEntity* b = Of(spu, other, false);
    eCEntity* puppet = PuppetAmong({a, b});
    const Memory first = Read(a), second = Read(b);
    guards::Acting acting(puppet);
    const GEInt result = originalDefeat(spu, self, other, args);
    if (puppet) {
        ++ends;
        Pass(first, puppet, "defeat");
        Pass(second, puppet, "defeat");
    }
    return result;
}

GEInt GE_STDCALL Cancel(gCScriptProcessingUnit* spu, GELPVoid self, GELPVoid other, GEInt args)
{
    eCEntity* a = Of(spu, self, true);
    eCEntity* puppet = PuppetAmong({Of(spu, other, false), TargetOf(a)});
    const Memory before = Read(a);
    const int reason = ReasonOf(a);
    guards::Acting acting(puppet);
    const GEInt result = originalCancel(spu, self, other, args);
    if (puppet) {
        ++ends;
        Pass(before, puppet, "fight called off", reason);
    }
    return result;
}

GEBool GE_STDCALL Watch(gCScriptProcessingUnit* spu)
{
    eCEntity* self = spu ? spu->GetSelfEntity() : nullptr;
    eCEntity* puppet = PuppetAmong({TargetOf(self), AlternativeOf(self)});
    const Memory before = Read(self);
    guards::Acting acting(puppet);
    const GEBool result = originalWatch(spu);
    if (puppet) {
        ++watched;
        Pass(before, puppet, "watched");
    }
    return result;
}

// ai.cpp, the attack states that run with a puppet as the player: each one's memory and attack reason before. A stack:
// a state runs others inside (an inner one cleared the single record of 26.10., and the running fight stayed unpassed).
struct Frame {
    Memory before;
    int reason;
    eCEntity* puppet;
};
std::vector<Frame> frames;

template <typename T>
T Hook(HMODULE game, uintptr_t rva, void* detour)
{
    static const unsigned char prologue[] = {0x55, 0x8b, 0xec, 0x83, 0xe4, 0xf8};  // this build
    auto* code = reinterpret_cast<unsigned char*>(game) + rva;
    if (std::memcmp(code, prologue, sizeof(prologue)) != 0) return nullptr;
    return reinterpret_cast<T>(ai::JumpTo(code, detour));
}
}

void Tick()
{
    if (installed || !gCSession::GetInstance().IsGameRunning()) return;
    const DWORD now = GetTickCount();
    if (now - lastTry < 1000) return;
    lastTry = now;
    installed = true;
    HMODULE game = GetModuleHandleA("Script_Game.dll");
    if (!game) return;
    originalSound = Hook<Script>(game, 0x2a510, reinterpret_cast<void*>(&Sound));       // AssessFightSound
    originalDefeated = Hook<Script>(game, 0x29fb0, reinterpret_cast<void*>(&Defeated));  // AssessDefeat
    originalDefeat = Hook<Script>(game, 0x27510, reinterpret_cast<void*>(&Defeat));      // Defeat
    originalCancel = Hook<Script>(game, 0x1a4d0, reinterpret_cast<void*>(&Cancel));      // CancelAttack
    originalWatch = Hook<Callback>(game, 0x27e40, reinterpret_cast<void*>(&Watch));      // OnWatchFight
    Logf("fightmem: AssessFightSound %d, AssessDefeat %d, Defeat %d, CancelAttack %d, OnWatchFight %d", originalSound != nullptr,
         originalDefeated != nullptr, originalDefeat != nullptr, originalCancel != nullptr, originalWatch != nullptr);
}

eCEntity* StateActor(eCEntity* self, char const* state)
{
    if (!on || !state || std::strncmp(state, "ZS_Attack", 9) != 0) return nullptr;
    eCEntity* puppet = PuppetAmong({TargetOf(self)});
    if (!puppet) return nullptr;
    ++stateRuns;
    frames.push_back({Read(self), ReasonOf(self), puppet});
    return puppet;
}

void AfterState(eCEntity* self)
{
    if (frames.empty()) return;
    const Frame frame = frames.back();
    frames.pop_back();
    if (frame.before.npc == self) Pass(frame.before, frame.puppet, "attack", frame.reason);
}

void OnState(uint8_t from, net::WorldState const& s)
{
    if (s.kind != Kind || s.guard != net::LocalId()) return;
    eCEntity* npc = npcs::NpcByGuid(s.guid);
    if (!IsNpc(npc)) return;
    Entity e(npc);
    const int before = static_cast<int>(e.NPC.LastFightAgainstPlayer);
    e.NPC.LastFightAgainstPlayer = static_cast<gELastFightAgainstPlayer>(s.npc);
    e.NPC.LastFightTimestamp = Entity::GetPlayingTime();
    ++applied;
    Logf("fightmem: %s remembers our fight (from player %u): %d -> %d", e.GetName().GetText(), from, before, static_cast<int>(s.npc));
}

// The game's grudge on sight (AssessActivePerception, its player's slot): an NPC that gave up a fight with its player
// (CancelAttack: Cancel and the time) takes it up again when it sees that player within a while, by the reason it fought
// for (IsUpset 0x115d0), with AssessTarget(self, player, LastPlayerAR). Here for another player whose fight this machine
// keeps: its memory in the NPC meanwhile, the puppet as the player. Only once the NPC sees it, as the game puts its player
// in that slot only when perceived (Script_Game 0x8bd0: in view with a free line of sight; upsetdiag 26.10.: the host's
// hero standing beside the NPC, out of view, was left alone).
int Upset(eCEntity* npc, eCEntity* puppet)
{
    if (!on || !apart || !IsNpc(npc) || !remote::IsPuppet(puppet)) return 0;
    auto it = grudges.find(Key(npc, remote::PuppetOwner(puppet)));
    if (it == grudges.end() || it->second.value != gELastFightAgainstPlayer_Cancel) return 0;
    const Entity self(npc), other(puppet);
    if (!self.IsInFOV(other) || !self.IsFreeLOS(other, GEFalse)) return 0;
    ++upsetAsked;
    const Memory local = Read(npc);
    const Memory kept = it->second;
    Write(npc, kept);
    GEInt upset = 0;
    {
        guards::Acting acting(puppet);
        upset = GetScriptAdminExt().CallScript(bCString("IsUpset"), npc, puppet, 0);
    }
    Write(npc, local);
    if (!upset) return 0;
    ++upsets;
    GetScriptAdminExt().CallScript(bCString("AssessTarget"), npc, puppet, kept.reason);
    Logf("fightmem: %s still holds the fight against player %d (reason %d, %.0f game minutes ago): task %s", Entity(npc).GetName().GetText(),
         remote::PuppetOwner(puppet), kept.reason, (Entity::GetPlayingTime() - kept.at) * 60.0f, Entity(npc).Routine.GetCurrentTask().GetText());
    return 1;
}

std::string Kept(eCEntity* npc)
{
    std::string out;
    if (!IsNpc(npc)) return "-";
    for (int id = 1; id <= net::MaxPlayers; ++id) {
        auto it = grudges.find(Key(npc, id));
        if (it == grudges.end()) continue;
        char part[64];
        sprintf_s(part, "%s%d:%d:%.1f:%d", out.empty() ? "" : ",", id, it->second.value, it->second.at, it->second.reason);
        out += part;
    }
    return out.empty() ? "-" : out;
}

void Report(LogFn log)
{
    char line[360];
    sprintf_s(line, "FIGHTMEM on=%d installed=%d sound=%d defeated=%d defeat=%d cancel=%d watch=%d sounds=%u defeats=%u watched=%u ends=%u state_runs=%u sent=%u applied=%u defeats_seen=%u defeats_yes=%u",
              on ? 1 : 0, installed ? 1 : 0, originalSound != nullptr, originalDefeated != nullptr, originalDefeat != nullptr,
              originalCancel != nullptr, originalWatch != nullptr, sounds, defeats, watched, ends, stateRuns, sent, applied, defeatsSeen,
              defeatsYes);
    char more[80];
    sprintf_s(more, " apart=%d kept=%u upset_asked=%u upsets=%u", apart ? 1 : 0, static_cast<unsigned>(grudges.size()), upsetAsked, upsets);
    strcat_s(line, more);
    log(line);
}

void Off(LogFn log)
{
    on = false;
    log("FIGHTMEMOFF ok=1");
}

void ApartOff(LogFn log)
{
    apart = false;
    log("GRUDGEOFF ok=1");
}
}
