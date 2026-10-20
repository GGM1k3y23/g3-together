// SPDX-License-Identifier: GPL-3.0-or-later
// QA diagnostics for the exploration block: which NPCs and entities a machine has, where, and whether they move.
#pragma once

namespace g3t::explore {
using LogFn = void (*)(char const*);
void Summary(LogFn log);                    // NPCSUMMARY: all NPCs this machine knows, by distance from the hero
void Teleport(char const* xyz, LogFn log);  // "X_Y_Z" in cm: move the hero there
void Attitude(char const* name, LogFn log); // QA: ATTITUDE line, the game's GetAttitude of an NPC to the hero and to puppet 2
void HeroPos(LogFn log);                   // QA: HEROPOS line, where the controlled body is now
void LookAt(char const* name, LogFn log);   // put the hero 4 m from a named entity, facing it (camera looks at it)
void Near(char const* xyz, LogFn log);      // NEAR lines: NPCs within 40 m of "X_Y_Z", then "NEAR end"
void FarNpcs(LogFn log);                    // FARNPC lines: NPCs anywhere, found via dialog owners
void Find(char const* name, LogFn log);     // FIND line: one named entity wherever it is
void FarKill(char const* name, LogFn log);    // kill a named NPC wherever it is (hit points 0, OnCheatKill)
void FarMove(char const* argument, LogFn log);  // "X_Y_Z_<name>": move a named entity wherever it is
void TeleportTo(char const* name, LogFn log);  // the game's own teleport to a named entity
void Page(char const* what, LogFn log);     // inventory|menu|close|state: GUI pages that may pause the game
void NpcState(char const* name, bool change, LogFn log);  // QA: NPCSTATE line; change = flip trade and guard as a dialog would
void InfoScan(char const* command, LogFn log);  // QA: dialogs whose script uses `command`, nearest owner first
void QuestCounters(char const* name, LogFn log);  // QA: kill/defeat quests with target counters
void NearNpcs(LogFn log);  // QA: NPCs near the hero with target and visible motion
void NpcFight(char const* argument, LogFn log);  // QA: "Attacker_Target": an NPC attacks another NPC
void Aggro(char const* argument, LogFn log);  // QA: "Name_Reason": an NPC attacks player 2's puppet (AssessTarget)
void InfoDescribe(char const* name, LogFn log);  // QA: a dialog option's conditions and script lines
void NativeSave(char const* name, LogFn log);  // the game's own save (gCHUDFileManager::SaveGame)
void NativeLoad(char const* name, LogFn log);  // the game's own load (gCHUDFileManager::LoadGame)
}
