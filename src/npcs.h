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
// The NPC with this GUID from the network: null unless it is an NPC (NPC, script routine and damage receiver property sets)
// and neither the local hero nor a puppet (Fable's review 22.10. 1.6, 1.7: handlers ran NPC wrappers on any entity).
eCEntity* NpcByGuid(uint8_t const guid[16]);
bool InAction(eCEntity const* entity);   // a mirror plays an action motion (its idle task must not replace it)
bool DiedOnHost(eCEntity const* entity);  // a mirrored NPC the host reported dead
using LogFn = void (*)(char const*);
void BadRecord(LogFn log);  // QA, client (stage X1): an NPC record with the hero's GUID and a NaN position
// Companions: a client takes over an NPC it recruited (Claim, then sends it like its own region); the host keeps
// mirroring it until it is given back (Unclaim) or that player leaves.
void Claim(eCEntity* entity);
void Unclaim(eCEntity* entity);
bool OnClaim(uint8_t const guid[16], uint8_t player, bool take);  // host; false: refused (someone else has it)
void Revoked(uint8_t const guid[16]);  // client: the host refused or ended our claim
void Rebind(uint8_t const guid[16]);   // look this mirror's GUID up again (its copy was just given that GUID: summon.cpp)
void Foreign(uint8_t const guid[16]);  // an NPC here that another machine simulates: never send it as ours (a copy)
void Adopt(uint8_t const guid[16]);    // client: a companion from the host's save is ours again
// Host: companions by player name, kept in the save's sidecar (lines "companion <guid hex> <player name>").
unsigned WriteCompanions(FILE* file);
unsigned ReadCompanions(FILE* file);
void ClearCompanions();
void CompanionsOff(LogFn log);  // QA negative control: saved companions are not handed over
void StyleOff(LogFn log);  // QA negative control: mirrors hold no weapons and keep their stance
void MirrorRetryOff(LogFn log);  // QA negative control (stage W8): a mirror missing at its first lookup stays missing (as before 23.10.)
void MirrorStats(LogFn log);     // QA (stage W8): MIRRORSTATS line (mirrors, missing ones, found later, expired)
void RecvOn(LogFn log);   // QA: take NPC snapshots again (stageM1: the host's win over this machine's own simulation)
void RecvOff(LogFn log);  // QA: ignore every NPC snapshot from now on (invariant self-test)
void DeadPoseOff(LogFn log);     // QA negative control: dead mirrors are not laid down
void DeadPoseReport(LogFn log);  // QA: DEADPOSE line
void LayDownCorpse(eCEntity* entity);  // a death applied here outside mirroring (journal.cpp): lay the body down like a dead mirror
unsigned HoldChanges();  // QA: weapons put into mirrors' hands so far
bool Claimed(eCEntity const* entity);  // client: taken over by this player
void SendInfo(char const* name, LogFn log);  // QA: NPCSEND line, why this machine last did not send the NPC (name or GUID), or sent
void JoinGraceOff(LogFn log);  // QA negative control (host): take a joining client's snapshots of NPCs near the host
void ContestOff(LogFn log);    // QA negative control (host): take a client's NPC that this machine still processes just outside its range
void ClaimRecord(char const* guid, LogFn log);  // QA (client): one snapshot of that host NPC as this machine's, at its hero
void EdgeReport(LogFn log);    // QA (host): EDGENPC lines, the NPCs processed here 40-60 m from the nearest player
void HostWinsOff(LogFn log);  // QA negative control (client): keep simulating an NPC itself although the host sends it
void OwnOff(LogFn log);      // QA negative control (client): send none of its own NPCs
uint8_t OwnerOf(eCEntity const* entity);  // player simulating this mirrored NPC, 0 if simulated here
void XpGuardOff(LogFn log);  // QA negative control: mirrors die through the game's death script again (and pay)
void Report(LogFn log);  // QA: one NPC line per NPC within 40 m of the local hero
}
