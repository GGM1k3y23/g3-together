// SPDX-License-Identifier: GPL-3.0-or-later
// Town reputation (A1, owner 20.10.: three models, INI `reputation`). The game keeps one set of counters per enclave
// (CrimeCount per crime, SuspectComment, KnownPlayerCrime, PlayerFame) and counts only the local player's deeds
// (AddEnclaveTheftCount, NotifyEnclave), so each machine had its own: a theft by the client never reached the host
// (theftdiag 20.10.). shared/together: the host's counters are the campaign's; a client sends what its own player's
// deeds added, the host adds it and sends its counters to everyone. A theft counted for a take the host then gave to
// someone else (the game counts before _AI_TakeItem) is taken back. personal: every machine keeps its own counters.
#pragma once

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
bool Together();  // reputation = together (the host's mode): what a witness holds against one player, it holds against all
}
