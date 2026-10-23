// SPDX-License-Identifier: GPL-3.0-or-later
// Shared story state with host authority, one mechanism for several kinds of named state:
// - quests: each quest's status;
// - infos: each dialog option's InfoGiven flag ("already told"), which other dialogs' conditions read.
// The host polls every entry and sends changes to all clients; a client asks for the host's full state once
// it is in the game (it connects from the main menu) and after every reconnect. A client applies the host's
// values (quests through the game's own quest functions: journal, experience) and sends its own player's
// changes to the host, which applies and redistributes them. Effects of a dialog (items, teaching) stay with
// the player who held it; the dialog then counts as held for everyone.
#pragma once

namespace g3t::story {
void Start();
void Tick();
using LogFn = void (*)(char const*);
void Event(char const* action, char const* name, LogFn log);  // QA: event | setevent | clearevent <name>
void Report(LogFn log);   // QA: one STORY line per kind, then "STORY end"
void Disable(LogFn log);  // QA negative control: stop sending and applying story state
void ZerosOff(LogFn log); // QA negative control (host, stage W4): the full state without the values cleared since
void Info(char const* name, bool give, LogFn log);  // QA: show (or mark as told) one dialog option
void InfoCandidates(LogFn log);                      // QA: dialog options not told yet, then "INFOS end"
void Talk(char const* npcName, LogFn log);  // QA diagnostic: start a dialog between the hero and this NPC
void TalkState(LogFn log);
void EndTalk(LogFn log);
void AssessTalk(char const* npcName, LogFn log);  // QA: address an NPC like the player's talk action
void KillRaiders(LogFn log);
void Approach(char const* npcName, LogFn log);  // QA: stand 1.5 m from an NPC                      // QA (host): end the opening raid
}
