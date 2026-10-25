// SPDX-License-Identifier: GPL-3.0-or-later
// Pickpocketing (P1, plan to 90 %): the game decides it on the thief's machine (the dialog command PickPocket, Script_Game
// PickPocket): on success it creates the loot from the NPC's treasure sets for the thief, counts a theft for the NPC's town
// and marks the NPC (Dialog.PickedPocket), so that nobody picks its pocket again. The mark stayed on the thief's copy: the
// real NPC on the other machine could be picked once more, by either player. A success is passed on (world state kind 12),
// every machine marks its copy. The theft count goes through enclaves.cpp, a failure's reaction through witness.cpp.
#pragma once

#include <cstdint>

namespace g3t::net { struct WorldState; }

namespace g3t::pickpocket {
void Tick();  // installs the jump into the game's PickPocket once its scripts are registered
void OnState(uint8_t from, net::WorldState const& s);  // kind 12: that NPC's pocket was picked by player `guard`
using LogFn = void (*)(char const*);
void Report(LogFn log);                          // QA: PICKSYNC line
void Off(LogFn log);                             // QA negative control (pickpocketoff): a success is not passed on
void Candidates(LogFn log);                      // QA (pickcands): NPCs within 30 m with a dialog, their level and mark
void Info(char const* guid, LogFn log);          // QA (pickinfo <guid>): that NPC's mark
void Pick(char const* guid, LogFn log);          // QA (pickpocket <guid>): this machine's hero picks its pocket the game's way
void ThiefPrep(LogFn log);
void LearnPerk(char const* name, LogFn log);  // QA (learnperk <Perk_...>): this machine's hero learns that skill  // QA (thiefprep): the hero learns Perk_PickPocket_3 and has theft 150 (PickPocket then always succeeds)
}
