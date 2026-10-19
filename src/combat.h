// SPDX-License-Identifier: GPL-3.0-or-later
// Damage with host authority, enforced where every hit in the game ends: the AddHitPoints script.
// - a client hitting an NPC mirrored from the host: the client does not apply it, but sends it to the host,
//   which applies it (and kills at zero); the result comes back through NPC mirroring;
// - something on the host hitting a remote player's puppet: not applied to the puppet, but sent to that
//   player, whose real hero takes it.
#pragma once

class eCEntity;

namespace g3t::combat {
void Start();
void Tick();  // installs the filter once the game's scripts are registered
using LogFn = void (*)(char const*);
void HitNpc(char const* guid, LogFn log);  // QA: hit the NPC (GUID as the npcs report prints it) the way gCEntity::DoDamage does
void GameString(char const* rva, LogFn log);  // QA: text of a bCString global in Game.dll
void HitPuppet(int playerId, LogFn log);  // QA: host hits a player's puppet the same way
void GiveXp(char const* guid, LogFn log);   // QA: GiveXP(hero, NPC, -1) and the result
void HeroState(LogFn log);                 // QA stage A: HERO line (hp, dead/down, task, AI mode, pause, GUI page)
void HurtHero(char const* guid, LogFn log); // QA stage A: an NPC hits the hero through the game's hit judgement
void KillHero(char const* guid, LogFn log); // QA stage A: OnCheatKill on the hero, an NPC as killer
void HitPuppetBy(char const* guid, LogFn log);  // QA stage A: an NPC hits player 2's puppet on the host
void FleeFrom(char const* guid, LogFn log);     // QA stage V5: an NPC flees from player 2's puppet; FLEE line with its attitude
void Report(LogFn log);                    // QA: HP of the local hero and counters
void Uninstall(LogFn log);                  // QA negative control: let every hit through
void NpcHitsOff(LogFn log);
void PartyXpOff(LogFn log);  // QA negative control: kills by our companions/summons pay no one                 // QA negative control: our NPCs' hits on others' NPCs are dropped
void Arm(char const* item, LogFn log);
bool LethalHit(eCEntity* victim);  // QA: kill through the game's own hit judgement
      // QA: give the hero this item template and hold it in the right hand
void Swing(char const* state, LogFn log);  // QA: the local hero swings as a key press would
}
