// SPDX-License-Identifier: GPL-3.0-or-later
// Who uses an object (P1, plan to 90 %): a stool, bench, campfire, anvil ... has one user (Interaction.User), set by the
// game on the machine where its user acts. The other machine never knew: two heroes sat on one stool, and at a campfire
// the second player stayed in PS_Interact (A, usediag 20.10.). Each machine reports when its hero becomes the user of an
// object and when it lets go (world state kind 13); the others set that player's puppet as the user meanwhile, as an
// NPC sitting there would be, so their NPCs do not take it. The player states do not ask for a user (they make the hero
// the user; stageSharedUse 24.10.): a hero that starts to use a place another player uses is stopped before it gets there.
#pragma once

#include <cstdint>

class eCEntity;
namespace g3t::net { struct WorldState; }

namespace g3t::occupancy {
void Tick();  // every frame; looks at the local hero every 250 ms
void OnState(uint8_t from, net::WorldState const& s);  // kind 13: player `guard` uses (npc 1) or let go of (npc 0) object guid
void OnWorldChange();
void Forget(int player, eCEntity* puppet);  // that player's puppet goes: the objects it used here are free again
using LogFn = void (*)(char const*);
void Report(LogFn log);  // QA: OCCUPANCY line
void Off(LogFn log);     // QA negative control (usesyncoff): nothing is reported
void StandUp(LogFn log); // QA (unuse): the hero ends its interaction the game's way (PS_Interact_End)
}
