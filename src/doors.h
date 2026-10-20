// SPDX-License-Identifier: GPL-3.0-or-later
// Doors and locks (INVENTAR-SKRIPTE.md, doordiag 20.10.): a door one player opened stayed shut and locked for the
// other, who could not follow. Every machine watches the doors and locks it processes; a change seen here (a key,
// the lock-picking game, a spell, a script, the door opened or shut) goes to the others through the host as world
// state kind 10, and a state from elsewhere is applied as soon as the entity is processed here.
#pragma once

#include "net.h"

#include <cstdint>

namespace g3t::doors {
using LogFn = void (*)(char const*);
void Tick();                                           // once per frame; scans every 500 ms
void OnState(uint8_t from, net::WorldState const& s);  // kind 10 from the network
void OnWorldChange();                                  // a save was loaded: forget what was seen
void Report(LogFn log);                                // QA: DOORS line
void SyncOff(LogFn log);                               // QA negative control: nothing sent or applied
}
