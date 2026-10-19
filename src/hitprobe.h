// SPDX-License-Identifier: GPL-3.0-or-later
// Diagnostic: which game code changes hit points? Wraps the AddHitPoints script and records, per call, the
// return addresses found on the stack inside the game's modules (module+RVA), grouped by call path.
#pragma once

namespace g3t::hitprobe {
void Tick();  // installs the wrapper once the game's scripts are registered
using LogFn = void (*)(char const*);
void Report(LogFn log);  // QA: one HITSTACK line per distinct call path, then "HITSTACKS end"
}
