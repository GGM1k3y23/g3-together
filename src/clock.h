// SPDX-License-Identifier: GPL-3.0-or-later
// One time of day for everyone: the host's game clock (the World_MCP entity's clock property set) goes to the
// clients every 5 s; a client more than two game minutes off is set to it. Routines depend on the hour, so
// players far apart would otherwise see NPCs at different places, and day and night at different times.
#pragma once

#include <cstdint>

namespace g3t::clock {
void Start();
void Tick();             // once per frame
using LogFn = void (*)(char const*);
void Report(LogFn log);  // QA: CLOCK line
void Set(char const* hhmm, LogFn log);  // QA: set the local clock to "HH_MM"
void Off(LogFn log);     // QA negative control (client): ignore the host's clock
}
