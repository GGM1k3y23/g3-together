// SPDX-License-Identifier: GPL-3.0-or-later
// A player's own hero as a record: level, experience, learn points, attributes (base values and maxima),
// learned skills and the inventory with quality and equipment. When a player loads the host's save, the world
// is the host's and so is the hero in it; the record gives the player its own hero back.
#pragma once

#include <string>

namespace g3t::hero {
using LogFn = void (*)(char const*);
bool SaveRecord(char const* path);      // the local hero into a text file
bool LoadRecord(char const* path);      // the local hero from a text file
void Dump(LogFn log);                   // QA: HERODUMP line, a canonical summary of the local hero
void SaveCommand(char const* name, LogFn log);  // QA: <save folder>/g3t-hero-<name>.txt
void LoadCommand(char const* name, LogFn log);
void RestoreOff(LogFn log);             // QA negative control: LoadRecord changes nothing
void CutCommand(char const* argument, LogFn log);  // QA (stage W2): "<name>" or "<name>_header" cuts that record as a broken write
void CheckOff(LogFn log);               // QA negative control (stage W2): records are applied unchecked
void WriteFault(LogFn log);             // QA (stage W2): the next record write breaks off before it replaces the record
std::string RecordPath(char const* name);  // <save folder>/g3t-hero-<name>.txt
}
