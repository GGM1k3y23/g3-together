// SPDX-License-Identifier: GPL-3.0-or-later
// Items lying in the world, owned by the host. When a hero takes one (the game's _AI_TakeItem; the item's
// GUID vanishes into a stack), a client claims it at the host; the first claim wins: the host removes it from
// its world and from everyone else's, and a later claimer gives back what it took. The host's own takes are
// announced the same way. Notes name what arrived in the inventory (a gold stack arrives as coins).
#pragma once

class gCScriptProcessingUnit;

namespace g3t::items {
void Start();
void NoteOwnDrop(char const* name);  // client: the weapon this hero held when it was knocked out (downed.cpp)
using LogFn = void (*)(char const*);
void List(LogFn log);                      // QA: world items within 30 m of the hero, then "ITEMS end"
void Take(char const* guid, LogFn log);    // QA: put a world item into the hero's inventory (as _AI_TakeItem does)
void Decay(char const* guid, LogFn log);   // QA: remove a world item with Entity::Decay
void Describe(char const* guid, LogFn log);  // QA: what the GUID resolves to now
void BeforeTake(gCScriptProcessingUnit* spu);  // around the hero's _AI_TakeItem (ai.cpp)
void AfterTake(gCScriptProcessingUnit* spu);
void Pick(char const* guid, LogFn log);  // QA: take an item the player's way (target + PS_Normal_Take)
void TookReport(LogFn log);
void Count(char const* templateName, LogFn log);  // QA: how many of these the hero carries
void Disable(LogFn log);  // QA negative control: no item sync
unsigned Returned();  // client: how many takes the host gave to someone else (enclaves.cpp takes the theft back)
unsigned TakesBegun();  // this hero's _AI_TakeItem calls on a world item
unsigned TakesDone();   // ... after which the item was in the hero's inventory
void Containers(LogFn log);  // QA: chests and corpses near the hero with their contents
bool Switch(char const* action, LogFn log);  // QA negative controls of the play-evening fixes (27.10.): true if it was one
}
