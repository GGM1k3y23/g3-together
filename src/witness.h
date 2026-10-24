// SPDX-License-Identifier: GPL-3.0-or-later
// Witnesses (owner 24.10.): an NPC that sees a player's crime reacts as in single-player, also when the other machine runs
// it. The game judges a crime against its local player only (AssessTakeItem, MemorizePlayerCrime ask Entity::GetPlayer), so
// it is judged where that player is: there the NPC is a mirror whose AI does not run. Its judgement (AssessTarget with a
// crime's attack reason) goes to the machine that runs it, which lets the real NPC react to that player's puppet: it goes
// after the thief. reputation = together: what such an NPC now holds against one player (LastPlayerCrime: it refuses to
// talk), its copies on the other machines hold against their players too.
#pragma once

namespace g3t::witness {
void Start();
void Tick();  // installs the jump into the game's AssessTarget once its scripts are registered
using LogFn = void (*)(char const*);
void Report(LogFn log);                         // QA: WITNESS line
void Off(LogFn log);                            // QA negative control (witnessoff): nothing is passed on
void Saw(char const* argument, LogFn log);      // QA (witness): "<NPC>_<reason>": that NPC judges this machine's hero (AssessTarget)
}
