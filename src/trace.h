// SPDX-License-Identifier: GPL-3.0-or-later
// Runtime trace (owner 2026-10-19: "who stands the dead orc up?"): instead of guessing from the decompiled code, log at
// run time which code starts an animation or sets a task on a body the mod drives (puppets, mirrors), with the caller
// as module+RVA. Off by default; QA "trace on|all|off" or INI "trace = on|all". Output: <game dir>\g3trace.log.
#pragma once

namespace g3t::trace {
using LogFn = void (*)(char const*);
void Configure(char const* mode);       // "on" (driven bodies), "all" (every entity), "off"
void Command(char const* mode, LogFn log); // QA: TRACE line; empty mode: status only
}
