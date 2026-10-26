// SPDX-License-Identifier: GPL-3.0-or-later
// Bodies we drive (remote players' puppets, NPCs mirrored from the host) must not perceive anything and
// must not run the game's AI states (scene scripts kept handing them combat states, which crashed once we
// had touched their targets). Both are wrapped through the script admin's own tables.
#pragma once

namespace g3t::ai {
void Tick();             // installs the filter once the game's scripts have registered it
bool PerceptionFiltered();
unsigned BlockedCount(); // perceptions refused for driven bodies so far
unsigned SkippedCount(); // AI states/functions not run for driven bodies so far
unsigned WrappedCount(); // AI states/functions wrapped
unsigned UnknownCount(); // wrapper calls that named no wrapped state (must stay 0)
// QA: which script admin table holds `name` (script, AI callback, AI state, AI function, AI routine).
void Describe(char const* name, void (*log)(char const*));
void ListNames(char const* text, void (*log)(char const*));  // QA: registered names containing text
void DumpNames(char const* path, void (*log)(char const*));  // QA: every registered name, its module+RVA and source file, as TSV
void Count(char const* names, void (*log)(char const*));     // QA aicount <prefix>: calls of AI states/functions by name
void FindByRva(char const* rvaHex, void (*log)(char const*)); // QA: registered names whose function is Script_Game.dll+rva
void ContinueRoutineReport(void (*log)(char const*));  // QA: CONTINUEROUTINE line (puppets kept out of their routine)
void ContinueRoutineOn(void (*log)(char const*));      // QA negative control: ContinueRoutine runs for puppets again
void MoveLengthReport(void (*log)(char const*));       // QA: MOVELENGTH line (GetCombatMoveLength without the fight target)
void MoveLengthOff(void (*log)(char const*));          // QA negative control: the original binds the target again
// Jump from a native function whose first 6 bytes are complete, position-independent instructions (push ebp; mov ebp,esp;
// and esp,-8) to `detour`; returns the trampoline that runs the original, or null.
void* JumpTo(unsigned char* target, void* detour);
// Points one import of `module` (by DLL and function name) at `replacement`; returns what it pointed at.
void* PatchImport(void* module, char const* dll, char const* function, void* replacement);
// GetAttitude wrapper: an NPC judging a puppet judges the local hero instead.
bool AttitudeInstalled();
bool AttitudeNative();          // jump in the native function (all callers), not only the script table
unsigned AttitudeAsked();       // GetAttitude calls with a puppet as the judged entity
unsigned AttitudeRedirected();  // of these, answered as for the local hero
unsigned AttitudeFleeKept();    // of these, left alone: the judge flees from that puppet (Panic)
void AttitudeOff(void (*log)(char const*));  // QA negative control
void FleeKeepOff(void (*log)(char const*));  // QA negative control of stage V5: no Panic kept for fleeing NPCs
}
