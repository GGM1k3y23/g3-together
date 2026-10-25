// SPDX-License-Identifier: GPL-3.0-or-later
// Animals coming back (P3, plan to 90 %): the game's Respawn (Script_Game, called from OnEnterProcessingRange for a dead
// NPC that enters this machine's processing range) rolls once 120 game hours have passed since its last fight. A success
// stands it up again at its spawn point, a failure takes it out of the world. Each machine rolled for itself, so one player
// saw the animal again and the other its corpse, and the world journal never reported its second death. Now the roll of
// the machine that simulates the NPC counts: its result goes to everyone (world state kind 14), a mirror never rolls.
#pragma once

#include <cstdint>

namespace g3t::net { struct WorldState; }

namespace g3t::respawn {
void Tick();  // installs the jump into the game's Respawn once its scripts are registered
void OnState(uint8_t from, net::WorldState const& s);  // kind 14: guard 1 stood up again, 2 out of the world
using LogFn = void (*)(char const*);
void Report(LogFn log);  // QA: RESPAWNSYNC line
void Off(LogFn log);     // QA negative control (respawnoff): every machine rolls for itself, as before
}
