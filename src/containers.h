// SPDX-License-Identifier: GPL-3.0-or-later
// Containers with host authority: chests, corpses and the trader a client deals with. Their contents differ
// between machines (plunder is rolled per machine when an NPC dies), so the host's contents count:
// - the host sends the contents of containers near any player whenever they change; a client overwrites
//   its copy with them (equipped stacks of a living trader are left alone);
// - a client that opened a container (the game's PlayerOpenLoot / PlayerOpenTrade) sends what its player
//   changed there as one transaction; the host applies all of it or none, and on a refusal the client undoes
//   the change to its hero's inventory.
#pragma once

namespace g3t::containers {
void Start();
void Tick();
using LogFn = void (*)(char const*);
void Contents(char const* guid, LogFn log);  // QA: CONTENTS line for a container
void Open(char const* guid, LogFn log);      // QA: open a container the player's way (PlayerOpenLoot)
void Take(char const* guid, LogFn log);      // QA: move the first stack of a container to the hero
void Report(LogFn log);                      // QA: CONTAINERSYNC counters
void Disable(LogFn log);                     // QA negative control
void Silent(char const* guid, LogFn log);    // QA (host): drop a stack without telling the clients
void TradeOpen(char const* guid, LogFn log);  // QA: open trade with an NPC (PlayerOpenTrade)
void TradeBuy(char const* argument, LogFn log);  // QA: buy one piece ("GUID" or "GUID_Template") and pay
void Goods(char const* guid, LogFn log);      // QA: GOODS line for any inventory
void Stock(char const* argument, LogFn log);  // QA (host): add one item to an NPC ("GUID_Template")
}
