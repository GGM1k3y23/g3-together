// SPDX-License-Identifier: GPL-3.0-or-later
// Bows and spells (owner 19.10.: test them, the meteor spell in particular): QA commands that make the local player
// shoot or cast the way its key presses do (the player tasks PS_Ranged and PS_Magic), and counters of what flies.
#pragma once

namespace g3t::ranged {
using LogFn = void (*)(char const*);
extern bool homing;                           // QA: shootat aims the arrow at its target (off: the camera's aim)
void Inventory(LogFn log);                    // QA: INV lines (name, amount, use type, category, in hand)
void Ready(char const* item, LogFn log);      // QA: hold a bow (with arrows) or a spell, creating it if missing
void Target(char const* guid, LogFn log);     // QA: face an NPC and put it into the player's focus
void Act(char const* taskAndState, LogFn log);
void FinishAt(char const* guid, LogFn log);  // QA (P7): the player's finishing blow on a downed NPC (PS_Melee_FinishingAttack)
void ShootAt(char const* argument, LogFn log);     // QA: shoot at an NPC with the game's script Shoot, aimed as an NPC's arrow
void CastAt(char const* guid, LogFn log);       // QA: cast the spell in hand at an NPC the way _AI_StartCastPhase does// QA: "PS_Ranged-PS_Ranged_Aim": the player task and state a key sets
void Tick();                                   // once per frame: aims the arrow of a pending shootat
void LastArrow(LogFn log);                    // QA: LASTARROW line: where the latest shootat arrow is
void Summons(LogFn log);                      // QA: SUMMON lines: summoned entities (Smn_*), owner, property sets
void Mana(LogFn log);                         // QA: fill the hero's mana
void Flying(LogFn log);                       // QA: FLYING line: projectiles and meteors in the world right now
}
