// SPDX-License-Identifier: GPL-3.0-or-later
// A player's own hero as a record: level, experience, learn points, attributes (base values and maxima),
// learned skills and spells and the inventory with quality, equipment and quick slots. When a player loads the host's save, the world
// is the host's and so is the hero in it; the record gives the player its own hero back.
#pragma once

#include <string>

namespace g3t::hero {
using LogFn = void (*)(char const*);
bool SaveRecord(char const* path);      // the local hero into a text file
bool LoadRecord(char const* path);      // the local hero from a text file
char const* LastRefusal();              // why the last LoadRecord refused its record (damaged, unreadable), nullptr if it did not
void Dump(LogFn log);                   // QA: HERODUMP line, a canonical summary of the local hero
void SaveCommand(char const* name, LogFn log);  // QA: <save folder>/g3t-hero-<name>.txt
void LoadCommand(char const* name, LogFn log);
void RestoreOff(LogFn log);             // QA negative control: LoadRecord changes nothing
void CutCommand(char const* argument, LogFn log);  // QA (stage W2): "<name>" or "<name>_header" cuts that record as a broken write
void QuickRestoreOff(LogFn log);        // QA negative control (stageQuickLoad): a record leaves the quick bar empty (as before 27.10.)
void QuickAssign(char const* argument, LogFn log);  // QA (stageQuickLoad): "<item>-<slot>", QUICKASSIGN line
void QuickDiag(LogFn log);              // QA diagnostic: QUICKDIAG line, the raw quick slot of every stack
void SpellRestoreOff(LogFn log);        // QA negative control (stageSpellLoad): a record leaves the spells unlearned (as before 27.10.)
void SpellKnow(char const* argument, LogFn log);  // QA (stageSpellLoad): "<item>" or "<item>-learn", SPELLKNOW line
void CheckOff(LogFn log);               // QA negative control (stage W2): records are applied unchecked
void WriteFault(LogFn log);             // QA (stage W2): the next record write breaks off before it replaces the record
void SkillReplaceOff(LogFn log);        // QA negative control (stage W9): a record's skills are only taught on top (as before 23.10.)
void SkillDiag(LogFn log);              // QA diagnostic (stage W9): SKILLDIAG lines, what a skill stack of the hero is
void SkillTry(LogFn log);               // QA diagnostic (stage W9): SKILLTRY line, which way puts a removed skill back
void HasSkill(char const* id, LogFn log);  // QA (stage W9): HEROSKILL line, whether the hero has learned that skill
std::string RecordPath(char const* name);  // <save folder>/g3t-hero-<name>.txt
}
