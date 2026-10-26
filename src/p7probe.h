// SPDX-License-Identifier: GPL-3.0-or-later
// QA measurement for the open P7 points: onlooker memory, warnings, intent to kill, use of other people's things (see the .cpp).
#pragma once

namespace g3t::p7probe {
using LogFn = void (*)(char const*);
void On(LogFn log);                          // QA p7probeon: hook and count from now on
void Report(LogFn log);                      // QA p7probe: P7PROBE lines
void FightMemory(char const* name, LogFn log);  // QA fightmemory <name>: FIGHTMEMORY line
}
