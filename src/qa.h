// SPDX-License-Identifier: GPL-3.0-or-later
// Test bot for disposable game copies. Active only if <game dir>\qa-enable.txt exists.
// Reads numbered commands from qa-command.txt, writes results to qa-engine.log.
#pragma once

namespace g3t::qa {
void Init();   // from ScriptInit
void Tick();   // once per frame on the engine thread
bool Enabled();
}
