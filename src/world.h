// SPDX-License-Identifier: GPL-3.0-or-later
// QA for the world interactions of the inventory (INVENTAR-SKRIPTE.md, agent A, 20.10.): doors, ore, campfire, anvil,
// shrine, beds, water barrels, boulders. The hero uses an interact object the way the game's OnPlayerAction does: the
// object as its target and focus, then the player state PS_Interact, which picks PS_Interact_<kind> by the object.
#pragma once

#include <Engine.h>

namespace g3t::world {
using LogFn = void (*)(char const*);
// Every entity the engine processes here, static ones included (doors, ore veins); null before the scene exists.
bTSmallArray<eCEntity*> const* ProcessedEntities();
void Usables(char const* radius, LogFn log);  // USABLE lines within radius cm (default 3000) of the hero, then USABLE end
void Face(char const* name, LogFn log);       // FACE line: stand in front of the nearest entity of that name (or GUID)
void Use(char const* name, LogFn log);        // USE line: the hero uses the nearest entity of that name
void UseInfo(char const* name, LogFn log);    // USEINFO line: door, lock, user of the nearest entity of that name
void Unlock(char const* name, LogFn log);  // QA stand-in for the lock-picking minigame or a key: UNLOCK line
void FindEntities(char const* prefix, LogFn log);  // QA: FINDENT lines (name, position) of processed entities whose name starts so
}
