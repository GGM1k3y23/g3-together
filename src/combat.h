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
void HitPuppet(char const* id, LogFn log);  // QA: hit player <id>'s puppet the same way (its stumble, stumblediag)
void HitNpc(char const* guid, LogFn log);  // QA: hit the NPC (GUID as the npcs report prints it) the way gCEntity::DoDamage does
void HitNpcBurst(char const* argument, LogFn log);  // QA, client (stage W5): "<guid>_<count>" lethal hit claims in one frame
void KillOnceOff(LogFn log);   // QA negative control (stage W5): kills and rewards not counted per victim (as before 23.10.)
void KillConfirmOff(LogFn log);  // QA negative control (stageLiberate): a kill here that did not take keeps its victim from hits
void KillReport(LogFn log);    // QA: KILLONCE line
void OnWorldChange();          // a load or another world: the dead of the last one are forgotten
void GameString(char const* rva, LogFn log);  // QA: text of a bCString global in Game.dll
void HitPuppet(int playerId, LogFn log);  // QA: host hits a player's puppet the same way
void GiveXp(char const* guid, LogFn log);   // QA: GiveXP(hero, NPC, -1) and the result
void HeroState(LogFn log);                 // QA stage A: HERO line (hp, dead/down, task, AI mode, pause, GUI page)
void HurtHero(char const* guid, LogFn log); // QA stage A: an NPC hits the hero through the game's hit judgement
void KillHero(char const* guid, LogFn log); // QA stage A: OnCheatKill on the hero, an NPC as killer
void HitPuppetBy(char const* guid, LogFn log);  // QA stage A: an NPC hits player 2's puppet on the host
void FleeFrom(char const* guid, LogFn log);     // QA stage V5: an NPC flees from player 2's puppet; FLEE line with its attitude
void Report(LogFn log);                    // QA: HP of the local hero and counters
// The game's IsInParadeMode (SG+d870: AniState parade and the parade held), for the parades sent (npcs.cpp, Stufe 3.3).
bool InParadeMode(eCEntity* entity);
// QA (Stufe 4.1): foreignjudgeskipon / foreignjudgeskipoff; true if it was one of them.
bool Switch(char const* action, LogFn log);
void Uninstall(LogFn log);                  // QA negative control: let every hit through
void PuppetParadeOff(LogFn log);  // QA negative control: a parrying puppet blocks on the judging machine, as before
void ParadeReport(LogFn log);
void NpcBlockOff(LogFn log);      // QA negative control: a client's blow on a parrying NPC is judged on the mirror, as before     // QA: PARADE line (judgement wrappers, blows passed on)
void NpcHitsOff(LogFn log);  // QA negative control: our NPCs' hits on others' NPCs are dropped
void RemoteKnockOutsOff(LogFn log);  // QA negative control (P7): a client's lethal claim kills, never knocks out (as before 24.10.)
void ShapeHitsOff(LogFn log);  // QA negative control (P6): an animal form's hits go out as one of our NPCs' (as before 24.10.)
void ReactHitsOff(LogFn log);  // QA negative control: a client's hit does not make the host's NPC judge its attacker
void AgressorOff(LogFn log);   // QA negative control: the struck NPC judges its attacker with AssessTarget (a friend fights at once)
void ArenaRuleOff(LogFn log);  // QA negative control: hits on an NPC in a duel or arena bout count from anyone (as before)
void ArenaReport(char const* name, LogFn log);  // QA: ARENA line (attack reason, target, refused hits, knock-outs)
void NpcHp(char const* argument, LogFn log);  // QA: "Name_HP" sets an NPC's hit points
void PartyXpOff(LogFn log);  // QA negative control: kills by our companions/summons pay no one
void Arm(char const* item, LogFn log);  // QA: give the hero this item template and hold it in the right hand
bool LethalHit(eCEntity* victim);  // QA: kill through the game's own hit judgement
void Guard(char const* shield, LogFn log);
void NpcGuard(char const* argument, LogFn log);  // QA P4: "<guid>_<hero|puppetN>": the NPC parries that one for 15 s
void NpcParade(char const* guid, LogFn log);       // QA P4: NPCPARADE line (that NPC's parade, stamina, hit points)
void PuppetState(LogFn log);                      // QA P1: PUPPETSTATE line (player 2's puppet here)
void PuppetParade(char const* guid, LogFn log);   // QA P4: PUPPETPARADE line (player 2's puppet against that NPC)
void NpcStill(char const* guid, LogFn log);       // QA P4: the NPC stops (its fight too) and stands idle
void HitProbe(char const* guid, LogFn log);  // QA P4: parade, strength and protection as the game judges them here  // QA P4: shield in the left hand, parrying, full stamina
void Swing(char const* state, LogFn log);  // QA: the local hero swings as a key press would
}
