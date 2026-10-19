// SPDX-License-Identifier: GPL-3.0-or-later
// NPCs with host authority: the host sends every NPC near any player ten times a second; clients switch
// the same NPC's AI off (found by its GUID) and drive it from those snapshots, including death.
#pragma once

#include <cstdint>
#include <cstdio>

class eCEntity;

namespace g3t::npcs {
void Start();
void Tick();
void OnWorldDeactivate();
bool IsMirrored(eCEntity const* entity);  // a client NPC currently driven from host snapshots
bool InAction(eCEntity const* entity);   // a mirror plays an action motion (its idle task must not replace it)
bool DiedOnHost(eCEntity const* entity);  // a mirrored NPC the host reported dead
using LogFn = void (*)(char const*);
// Companions: a client takes over an NPC it recruited (Claim, then sends it like its own region); the host keeps
// mirroring it until it is given back (Unclaim) or that player leaves.
void Claim(eCEntity* entity);
void Unclaim(eCEntity* entity);
bool OnClaim(uint8_t const guid[16], uint8_t player, bool take);  // host; false: refused (someone else has it)
void Revoked(uint8_t const guid[16]);  // client: the host refused or ended our claim
void Adopt(uint8_t const guid[16]);    // client: a companion from the host's save is ours again
// Host: companions by player name, kept in the save's sidecar (lines "companion <guid hex> <player name>").
unsigned WriteCompanions(FILE* file);
unsigned ReadCompanions(FILE* file);
void ClearCompanions();
void CompanionsOff(LogFn log);
void StyleOff(LogFn log);
void RecvOff(LogFn log);  // QA: ignore every NPC snapshot from now on (invariant self-test)
unsigned HoldChanges();  // QA: weapons put into mirrors' hands so far  // QA negative control: mirrors hold no weapons and keep their stance  // QA negative control: saved companions are not handed over
bool Claimed(eCEntity const* entity);  // client: taken over by this player
void OwnOff(LogFn log);      // QA negative control (client): send none of its own NPCs
uint8_t OwnerOf(eCEntity const* entity);  // player simulating this mirrored NPC, 0 if simulated here
void XpGuardOff(LogFn log);  // QA negative control: mirrors die through the game's death script again (and pay)
void Report(LogFn log);  // QA: one NPC line per NPC within 40 m of the local hero
}
