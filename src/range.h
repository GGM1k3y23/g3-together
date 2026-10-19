// SPDX-License-Identifier: GPL-3.0-or-later
// Distance warning: the host's world is only kept in step within the engine's processing range around the
// host (Entity.ROI, 40 m). A client that walks further away plays there with its own, unsynchronised NPCs;
// it is told so, and the host is told who is far away.
#pragma once

namespace g3t::range {
void Tick();             // once per frame
using LogFn = void (*)(char const*);
void Off(LogFn log);     // QA negative control: no warnings
void Report(LogFn log);  // QA: RANGE line
}
