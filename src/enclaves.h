// SPDX-License-Identifier: GPL-3.0-or-later
// Town reputation (A1, owner 20.10.: three models, INI `reputation`). The game keeps one set of counters per enclave
// (CrimeCount per crime, SuspectComment, KnownPlayerCrime, PlayerFame) and counts only the local player's deeds
// (AddEnclaveTheftCount, NotifyEnclave), so each machine had its own: a theft by the client never reached the host
// (theftdiag 20.10.). shared/together: the host's counters are the campaign's; a client sends what its own player's
// deeds added, the host adds it and sends its counters to everyone. A theft counted for a take the host then gave to
// someone else (the game counts before _AI_TakeItem) is taken back. personal: every machine keeps its own counters.
#pragma once

#include <cstdint>

class eCEntity;

namespace g3t::enclaves {
using LogFn = void (*)(char const*);
void Start();
void Tick();           // every frame; scans the enclaves every 500 ms
void OnWorldChange();  // a loaded save: forget what was seen, ask the host again
void Report(LogFn log);   // QA: ENCLAVESYNC line
void SyncOff(LogFn log);  // QA negative control: no reputation sync on this machine
void KeepLocalOff(LogFn log);                      // QA negative control: host counters overwrite unreported deeds
void PauseScan(char const* ms, LogFn log);         // QA: no scan for ms milliseconds
void AddCrime(char const* argument, LogFn log);    // QA: "<enclave>-<crime>-<n>", a deed counted here
void SetKnown(char const* argument, LogFn log);  // QA crimeknown <enclave>-<crime>: what the town knows of the player
void SetSuspect(char const* argument, LogFn log);  // QA crimesuspect <enclave>-<crime>-<n>: deeds of that kind commented on
bool Together();  // reputation = together (the host's mode): what a witness holds against one player, it holds against all
// P7 (plan to 90 %; C re/findings/notify-enclave-murder-followup-20261024-c.md): the game counts a murder in NotifyEnclave
// (mode 0, from the victim's Kill) only when the killer is this machine's hero or one of its party. A lethal claim of
// another machine's player (combat.cpp) kills with OnCheatKill, whose Kill names no player: no town counted it
// (murderdiag 24.10.: the host's hero +1 at Ishtar, the client's kill +0). NoteRemoteKill marks such a victim before its
// death; the wrapped NotifyEnclave then counts that murder once, under the game's own conditions (town, not Flee, Faring's
// three orcs, GetPoliticalAttitude(victim, town) == 1, the killer not Montera_Orc_02..05), before the game's own steps.
// shared/together: into this machine's counters (the sync carries it); personal: to the killer's machine (EnclaveCredit).
void NoteRemoteKill(uint8_t const victim[16], uint8_t player, eCEntity* actor);
void RemoteMurdersOff(LogFn log);            // QA negative control: such murders count nowhere (as before 24.10.)
void TownStatusOff(LogFn log);               // QA negative control (stageLiberate): a town's status stays where it turned
void PoliticalOff(LogFn log);                // QA negative control (stageBossNews): a freed town's political crime stays with its machine's hero
void CreditHold(char const* ms, LogFn log);  // QA (credithold <ms>): personal credits count the killer as out of the world that long
void CreditQueueOff(LogFn log);              // QA negative control: a credit for a killer out of the world is lost (as before 24.10.)
// QA murdercand "<enclave>": its members with the game's own murder conditions. QA cheatkill "<NPC>": this machine's hero
// kills it with OnCheatKill (which names no killer: it counted nothing, murderdiag 24.10.).
void MurderCandidates(char const* enclave, LogFn log);
void CheatKill(char const* name, LogFn log);
// QA (stageLiberate): "<enclave>": this machine's hero kills every living member farther than 40 m (as farkill), so that
// the ones near it are left for the other player; a town flees once fewer than a fifth of its defenders live.
void TownFarKill(char const* enclave, LogFn log);
bool Switch(char const* action, LogFn log);  // QA negative controls of the play-evening fixes (27.10.): true if it was one
}
