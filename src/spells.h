// SPDX-License-Identifier: GPL-3.0-or-later
// Spells that act on something another machine owns.
// - On another player: on the caster's machine the target is that player's puppet, so the spell changes only the puppet
//   (MagicHealOther sets the target's hit points to their maximum and lifts poison and the like: re/findings/spells-b.md).
//   The cast is sent to the puppet's player, whose machine replays the same script on its own hero.
// - On NPCs another machine simulates (sleep, fear, stun, hypnosis, poison, disease: re/findings/npc-spells-c.md): on a mirror the spell
//   would only change the mirror, which the owner's snapshots then override. The cast is sent to the NPC's owner, which
//   runs the game's script there, cast by the caster's puppet.
// Only scripts from a fixed list are run for others.
#pragma once

namespace g3t::spells {
void Start();
void Tick();  // installs the wrappers once the game runs
using LogFn = void (*)(char const*);
void Report(LogFn log);    // QA: SPELLSTATE line (counters)
void MirrorOff(LogFn log); // QA negative control: casts on puppets are not sent
void SetHp(char const* value, LogFn log);  // QA: set the local hero's hit points (to have something to heal)
void PuppetGuid(char const* player, LogFn log);  // QA: PUPPET line with the GUID of that player's puppet here
void NpcMemory(char const* guid, LogFn log);  // QA: NPCMEMORY line, what the NPC remembers of the player
void NpcGrudge(char const* guid, LogFn log);  // QA: the NPC is angry at the player (to have something to charm away)
void Party(LogFn log);  // QA: MEMBER lines, the local hero's party with each member's type
void SpawnNpc(char const* name, LogFn log);
void StatusOf(char const* guid, LogFn log);  // QA: STATUSOF line, an NPC's status effects and hit points
void SpellFunc(char const* names, LogFn log);  // QA: SPELLFUNC lines, the scripts each spell item runs
void Locks(LogFn log);  // QA: LOCK lines, every lock within 30 m (status, difficulty, key) and LOCKS end
void TickGuardOff(LogFn log);  // QA negative control: mirrors and puppets run the game's OnTick (poison ticks) again  // QA: SPAWNNPC line, an NPC from a template 4 m in front of the local hero
}
