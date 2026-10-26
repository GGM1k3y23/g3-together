// SPDX-License-Identifier: GPL-3.0-or-later
// NPCs remember fights with the other player as with their own (P7, the fight memory package, 26.10.). The game decides
// for its own player only: onlookers come to watch a fight of the player (AssessFightSound), react to a knock-out
// (AssessDefeat) and remember a fight they watched (OnWatchFight: LastFightAgainstPlayer Friendly); the opponent remembers
// the fight running and its end (ZS_Attack, ZS_Attack_End, Defeat, CancelAttack: Running, Won, Lost, Cancel), all compared
// with Entity::GetPlayer (re/out/fightmem-b.c). With the other player's puppet in the fight nobody came and nobody
// remembered it (fightmemorydiag 25.10.: Joey's duel, single-player Copper watched and kept Friendly, Joey kept Lost; with
// the client neither). Here the puppet in the fight is the game's player meanwhile, and what the NPC then remembers about
// that fight goes to the puppet's player (world state kind 18): its game's copy of the NPC remembers the same.
#pragma once

#include <cstdint>
#include <string>

class eCEntity;
namespace g3t::net { struct WorldState; }

namespace g3t::fightmem {
void Tick();  // installs the hooks once the game's scripts are registered
eCEntity* StateActor(eCEntity* self, char const* state);  // ai.cpp: the puppet an attack state of `self` fights, else none
void AfterState(eCEntity* self);                         // ai.cpp, after that state ran: memory changes go out
void OnState(uint8_t from, net::WorldState const& s);     // kind 18: an NPC's memory of a fight with this player
void OnWorldChange();  // a load or a new world: the grudges kept for other players belong to the world before
int Upset(eCEntity* npc, eCEntity* puppet);  // guards.cpp: the NPC takes up a fight it gave up against that puppet's player
std::string Kept(eCEntity* npc);             // QA: "<player>:<memory>:<time>:<reason>" kept here for other players, or -
using LogFn = void (*)(char const*);
void Report(LogFn log);  // QA: FIGHTMEM line
void Off(LogFn log);     // QA negative control (fightmemoff): as before 26.10.
void ApartOff(LogFn log);  // QA negative control (grudgeoff): the NPC keeps another player's fight as its own, no grudge
void KeepOff(LogFn log);   // QA negative control (grudgeloadoff): the grudges outlive a load, as before 26.10.
}
