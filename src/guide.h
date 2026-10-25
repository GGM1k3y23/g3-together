// SPDX-License-Identifier: GPL-3.0-or-later
// P9, guides and escorted NPCs (57 follow and 20 escort quests): the game guides and follows only the local hero
// (Entity::GetPlayer). The player who takes such a quest takes the NPC over from the other machine while it guides or
// follows that player.
#pragma once

class eCEntity;

namespace g3t::guide {
void Tick();  // take over the hero's guide and the NPCs that joined the hero; give them back when that ends
// Client: the host hands back an NPC from its save (npcs::Adopt). True when it guides in a running follow quest: it
// guides this player again (StartGuidePlayer) instead of following like a companion.
bool Readopt(eCEntity* npc);
// A join in a dialog here (the player's word CmdPartyJoin, CmdMobJoin or CmdSlaveJoin): once the game has made the hero
// the NPC's leader, it follows at once, as a dialog's JoinPlayer makes a companion (the slave started only ~50 s later on
// the host, never on a client, 25.10.).
void Joined(eCEntity* npc);
void OnWorldChange();  // the world (or save) was left: nothing is held any more
using LogFn = void (*)(char const*);
void Report(char const* name, LogFn log);  // QA: GUIDE line (the hero's leader; with a name, that NPC's leader and task)
void Off(LogFn log);                       // QA negative control: guidesyncoff, nothing is taken over
}
