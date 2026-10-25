// SPDX-License-Identifier: GPL-3.0-or-later
// Witnesses (owner 24.10.): an NPC that sees a player's crime reacts as in single-player, also when the other machine runs
// it. The game judges a crime against its local player only (AssessTakeItem, MemorizePlayerCrime ask Entity::GetPlayer), so
// it is judged where that player is: there the NPC is a mirror whose AI does not run. Its judgement (AssessTarget with a
// crime's attack reason) goes to the machine that runs it, which lets the real NPC react to that player's puppet: it goes
// after the thief. reputation = together: what such an NPC now holds against one player (LastPlayerCrime: it refuses to
// talk), its copies on the other machines hold against their players too.
#pragma once

class eCEntity;

namespace g3t::witness {
void Start();
void Tick();  // installs the jump into the game's AssessTarget once its scripts are registered
using LogFn = void (*)(char const*);
void Report(LogFn log);                         // QA: WITNESS line
void Off(LogFn log);                            // QA negative control (witnessoff): nothing is passed on
void Saw(char const* argument, LogFn log);      // QA (witness): "<NPC>_<reason>": that NPC judges this machine's hero (AssessTarget)

// P7, witnesses of what another machine's player or NPC did to an NPC of this machine (combat.cpp applies those hits
// with AddHitPoints and OnCheatKill, which tell nobody around): the perceptions the game's own hit path sends
// (DoLogicalDamage by its hit judgement, OnAI_FinishingAttack for the finishing blow), with that player's puppet, or our
// copy of that NPC (a companion), as the attacker.
void HitSeen(eCEntity* victim, eCEntity* attacker);       // fight noise: onlookers watch, guards step in
void KnockOutSeen(eCEntity* victim, eCEntity* attacker);  // a defeat
void MurderSeen(eCEntity* victim, eCEntity* attacker);    // before the kill: witnesses flee or go after the murderer
void SeenOff(LogFn log);                                // QA negative control (murderseenoff): none of the three is sent
}
