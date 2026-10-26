// SPDX-License-Identifier: GPL-3.0-or-later
// P4, the story's end: the epilogue is a chain of infos of the hero himself (owner PC_Hero; the first asks for the events
// Epilog_FirstInfo and the chosen god's Epilog_<God>, the last ends with EndGame) and played only on the machine whose hero
// started it. Owner 25.10.: both players see the end. That machine tells everyone (world state kind 17); every other machine
// starts the same info for its own hero, so the pictures, subtitles and the game's end play there too.
#pragma once

#include "net.h"

namespace g3t::ending {
void Tick();  // tell the others when this hero's epilogue starts; start one another player started once this hero is free
void OnState(uint8_t from, net::WorldState const& s);  // kind 17: another player's epilogue started (its first info's name)
void OnWorldChange();
using LogFn = void (*)(char const*);
void Report(LogFn log);  // QA: ENDING line
void Off(LogFn log);     // QA negative control: endingsyncoff, the epilogue plays only where it started
}
