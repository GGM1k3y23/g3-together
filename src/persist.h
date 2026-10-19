// SPDX-License-Identifier: GPL-3.0-or-later
// The game's own save and load (gCHUDFileManager::SaveGame / LoadGame in Game.dll), hooked:
// - before a save, puppets are removed (a save holding a puppet, which is a hero entity, aborts on load with
//   "current world has more than one hero!"; true for saves from the game's menu as well);
// - next to every save the mod writes a sidecar file <name>.g3t with its own campaign state (the world journal),
//   and reads it back when that save is loaded.
#pragma once

namespace g3t::persist {
void Install();          // once the game DLLs are loaded; refuses (and logs) on any other build
void* SaveTarget();      // the game's SaveGame entry (checked before hooking), nullptr if not hooked
void* LoadTarget();
// Once per frame: after a client loaded the host's save, its own hero record (written when the host saved)
// is applied to the hero in that world.
void Tick();
using LogFn = void (*)(char const*);
void Off(LogFn log);     // QA negative control: write no sidecar
void CampaignOff(LogFn log);  // QA negative control (host): joining players get no campaign
void Report(LogFn log);  // QA: PERSIST line
}
