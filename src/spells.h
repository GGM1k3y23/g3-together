// SPDX-License-Identifier: GPL-3.0-or-later
// Spells a player casts on another player. On the caster's machine the target is that player's puppet, so the spell
// changes only the puppet (MagicHealOther sets the target's hit points to their maximum and lifts poison and the like:
// re/findings/spells-b.md). Here such a cast on a puppet is sent to the puppet's player, whose machine replays the same
// script on its own hero. Only scripts from a fixed list are replayed.
#pragma once

namespace g3t::spells {
void Start();
void Tick();  // installs the wrappers once the game runs
using LogFn = void (*)(char const*);
void Report(LogFn log);    // QA: SPELLSTATE line (counters)
void MirrorOff(LogFn log); // QA negative control: casts on puppets are not sent
void SetHp(char const* value, LogFn log);  // QA: set the local hero's hit points (to have something to heal)
void PuppetGuid(char const* player, LogFn log);  // QA: PUPPET line with the GUID of that player's puppet here
}
