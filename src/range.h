// SPDX-License-Identifier: GPL-3.0-or-later
// Other-world notice: a partner who is in another world than this player is told to host and client alike (each
// player's surroundings run on that player's PC since stage E, so distance alone needs no notice).
#pragma once

namespace g3t::range {
void Tick();             // once per frame
using LogFn = void (*)(char const*);
void Off(LogFn log);     // QA negative control: no warnings
void Report(LogFn log);  // QA: RANGE line
}
