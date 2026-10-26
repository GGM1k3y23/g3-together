// SPDX-License-Identifier: GPL-3.0-or-later
// QA diagnostics for the exploration block: which NPCs and entities a machine has, where, and whether they move.
#pragma once

namespace g3t::explore {
using LogFn = void (*)(char const*);
void Summary(LogFn log);                    // NPCSUMMARY: all NPCs this machine knows, by distance from the hero
void Teleport(char const* xyz, LogFn log);  // "X_Y_Z" in cm: move the hero there
void Attitude(char const* name, LogFn log); // QA: ATTITUDE line, the game's GetAttitude of an NPC to the hero and to puppet 2
void HeroPos(LogFn log);                   // QA: HEROPOS line, where the controlled body is now
void RunTo(char const* argument, LogFn log);  // QA: the hero runs to X_Y_Z (AI goto)
void SetAttribute(char const* argument, LogFn log);  // QA: TAG_value, a base attribute of the hero
void LookAt(char const* name, LogFn log);   // put the hero 4 m from a named entity, facing it (camera looks at it)
void Near(char const* xyz, LogFn log);      // NEAR lines: NPCs within 40 m of "X_Y_Z", then "NEAR end"
void FarNpcs(LogFn log);                    // FARNPC lines: NPCs anywhere, found via dialog owners
void Find(char const* name, LogFn log);     // FIND line: one named entity wherever it is
void FarKill(char const* name, LogFn log);
void FarHeal(char const* name, LogFn log);  // QA (stageEscort): that NPC's hit points back
void Chapter(LogFn log);                    // QA (main story): CHAPTER line, the local hero's chapter (PlayerMemory)
void Sector(char const* name, LogFn log);   // QA (main story): SECTOR line, whether that world sector is on
void QuickUse(char const* item, LogFn log); // QA (P2): QUICKUSE line, that item on quick slot 1 used as its key does (OnPlayerUseQuickSlot)
void ScriptWatch(char const* name, LogFn log);  // QA: count calls of a script (name) or report them all (empty)
void NativeWatch(char const* name, LogFn log);  // QA: count every call of a script at its code (name), "reset", or report (empty)
void Sleepers(char const* argument, LogFn log);  // QA: SLEEPER lines, animals with a sleeping point ("all": every animal)
void WaitFree(char const* argument, LogFn log);  // QA: "NAME": that NPC waits for its destination (ZS_WaitForFreeDestination)
void AnimalSleep(char const* argument, LogFn log);  // QA: "GUID": that animal sleeps as ZS_AnimalSleep ends (OnAnimalSleep)
void ZoneAttitude(char const* name, LogFn log);  // QA (P7): ZONEATT line, zone attitude and attitude to the player for hero and puppet 2
void SetChapter(char const* value, LogFn log);  // QA setchapter <n>: SETCHAPTER line
void AttitudeChain(char const* name, LogFn log);  // QA attchain <npc>: ATTCHAIN line, the parts of GetAttitude for hero and puppet 2
void MoveMode(LogFn log);                   // QA (P2, PS_Swim): MOVEMODE line, the movement mode of the hero and of the puppets
void SwimGoal(char const* argument, LogFn log);  // QA (P2, PS_Swim): the hero swims toward DX_DZ for MS ms by its movement goal
void SwimTick();                                 // QA: every frame, holds that goal
void NpcRoutine(char const* name, LogFn log);  // QA (followdiag): FullStop and ContinueRoutine, as at a dialog's end    // kill a named NPC wherever it is (hit points 0, OnCheatKill)
void FarMove(char const* argument, LogFn log);  // "X_Y_Z_<name>": move a named entity wherever it is
void TeleportTo(char const* name, LogFn log);  // the game's own teleport to a named entity
void Page(char const* what, LogFn log);     // inventory|menu|close|state: GUI pages that may pause the game
void NpcState(char const* name, bool change, LogFn log);  // QA: NPCSTATE line; change = flip trade and guard as a dialog would
void InfoScan(char const* command, LogFn log);  // QA: dialogs whose script uses `command`, nearest owner first
void QuestCounters(char const* name, LogFn log);  // QA: kill/defeat quests with target counters
void NearNpcs(LogFn log);  // QA: NPCs near the hero with target and visible motion
void NpcFight(char const* argument, LogFn log);  // QA: "Attacker_Target": an NPC attacks another NPC
void CancelAttack(char const* name, LogFn log);  // QA: that NPC gives up its fight (the game's CancelAttack against its target)
void DuelHero(char const* name, LogFn log);  // QA: that NPC attacks the local hero for a duel (a knock-out, not a death)
void Aggro(char const* argument, LogFn log);  // QA: "Name_Reason": an NPC attacks player 2's puppet (AssessTarget)
void AssessHero(char const* argument, LogFn log);  // QA: "Name_Reason": an NPC attacks the local hero (AssessTarget)
void QuestDump(char const* path, LogFn log);  // QA (questdump): every quest of the game as TSV (plan P0: chapter checklist)
void InfoDescribe(char const* name, LogFn log);  // QA: a dialog option's conditions and script lines
void XpInfos(LogFn log);  // QA: XPINFO lines, dialog options that give experience under simple conditions
void NativeSave(char const* name, LogFn log);  // the game's own save (gCHUDFileManager::SaveGame)
void NativeLoad(char const* name, LogFn log);  // the game's own load (gCHUDFileManager::LoadGame)
void PolMem(char const* argument, LogFn log);  // QA polmem [pal_crime_suspect_fame]: POLMEM, the hero's political memory
void EnclaveFame(char const* argument, LogFn log);  // QA enclavefame Name[-fame]: ENCLAVEFAME, read/set an enclave's PlayerFame
void EnclaveStatus(char const* argument, LogFn log);  // QA enclavestatus Name-status: the game's ChangeEnclaveStatus here
}
