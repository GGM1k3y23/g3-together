// SPDX-License-Identifier: GPL-3.0-or-later
// NPCs that walk up to a player to talk (P7 rest, 25.10.). From its look around (AssessActivePerception, Script_Game
// 0x295f0, re/out/p7-activeperc-b.c) an NPC within 20 m that has an important info for Entity::GetPlayer starts
// AssessTalk: a quest giver who calls the hero over, a guard with news. It asks for its own player only, and on a client the
// host's NPCs are mirrors whose look around does not run (importanttalkdiag 25.10.: Hamlar walked up to the host and
// talked; the client, for whom its game had an important info with him as well, he never addressed).
// Now the client looks: a mirrored NPC within 20 m with an important info for this hero is asked for (world state kind
// 16); the host sends its NPC to that player's puppet; once it stands by, the client addresses it the way the game's talk
// action does, and the dialog runs as any other with the host's NPC (talk.cpp holds it on the host).
#pragma once

#include <cstdint>

namespace g3t::net { struct WorldState; }

namespace g3t::address {
void Tick();  // client: looks every 500 ms; host: walks the NPCs asked for
void OnState(uint8_t from, net::WorldState const& s);  // host, kind 16: player `guard` asks for NPC guid
void OnWorldChange();
using LogFn = void (*)(char const*);
void Report(LogFn log);  // QA: ADDRESS line
void Off(LogFn log);     // QA negative control (addressoff, client): nothing is asked for
void On(LogFn log);      // QA (addresson): back on; the harness turns it off for stages that do not test it (25.10.)
}
