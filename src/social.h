// SPDX-License-Identifier: GPL-3.0-or-later
// QA for theft, witnesses and the enclave's memory of crimes (A1, re/findings/social-crime-identity-c.md). Taking an
// item counts a theft at the enclave (AddEnclaveTheftCount, local player only), and the NPCs that see it judge the thief
// (SendPerceptionTakeItem -> AssessTakeItem -> AssessTarget with reason Theft). Read only: these commands report the
// fields the game writes, they change nothing.
#pragma once

namespace g3t::social {
using LogFn = void (*)(char const*);
// OWNED lines: world items within radius cm (default 3000) of the hero that lie in an indoor owner zone, with the zone's
// owner and security level (what AddEnclaveTheftCount and IsBotheredByPlayerTheftOf ask), then OWNED end.
void Owned(char const* radius, LogFn log);
void Bothered(char const* radius, LogFn log);  // QA bothered: BOTHERED lines, owned items and the NPCs that mind their theft
void OwnedObjects(char const* radius, LogFn log);  // QA ownedobj: OWNEDOBJ lines, interact objects in an indoor owner zone
// CRIME line: what the named NPC remembers (LastPlayerCrime, AttackReason, LastPlayerAR, target, task) and its enclave's
// counters (CrimeCount, SuspectComment per crime 0..4, KnownPlayerCrime, PlayerFame).
void Crime(char const* name, LogFn log);
// ENCLAVE lines: every enclave of the world with its member count, a member to go to, status and counters; ENCLAVES end.
void Enclaves(LogFn log);
void Revolution(char const* town, LogFn log);  // QA: the town rises (as SetRevolutionEnabled <town> true)
void StartRevolution(char const* town, LogFn log);  // QA: the uprising starts (as StartRevolution: raid, status Attack)
// QA: the hero puts its weapons away (NPCs react to a drawn weapon before they would notice a theft): UNARM line.
void Unarm(LogFn log);
}
