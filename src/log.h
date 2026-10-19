// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once

namespace g3t {
// Opens <game dir>\<name> next to the game's scripts folder; lines are appended with a timestamp.
void OpenLog(const char* name);
void Log(const char* text);
void Logf(const char* format, ...);
}
