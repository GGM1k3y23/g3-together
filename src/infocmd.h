// SPDX-License-Identifier: GPL-3.0-or-later
// Dialog commands that change the world. A dialog's script lines (gCInfoScriptLine_PS: command, two entities,
// two ids) run only on the machine that holds the dialog (IS_Running_ProcessInfo in Script_Game.dll); marking
// the info as told elsewhere replays none of them (static analysis). Commands that change the world are sent to
// the host and from there to everyone, and replayed on each machine.
#pragma once

struct gCScriptProcessingUnit;
class eCEntity;

namespace g3t::infocmd {
void Start();
// Called by the AI wrapper before IS_Running_ProcessInfo runs: notes the current command.
void Observe(gCScriptProcessingUnit* spu);
void Tick();                     // once per frame: sends the states read after commands
void OnWorldChange();            // a world (or save) was left: pending fights and states are dropped
void Changed(eCEntity* entity);  // QA: as if a dialog command had changed this entity
using LogFn = void (*)(char const*);
void Report(LogFn log);   // QA: INFOCMD line
void Off(LogFn log);      // QA negative control: pass nothing on
void JoinQa(char const* argument, LogFn log);  // QA: Name_1 / Name_0, like JoinPlayer true/false on a client
}
