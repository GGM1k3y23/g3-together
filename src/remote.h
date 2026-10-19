// SPDX-License-Identifier: GPL-3.0-or-later
// Other players: send the local hero's state, show every remote player in the same world as a puppet.
#pragma once

class eCEntity;

namespace g3t::remote {
void Start();             // after LoadConfig
void Tick();              // once per frame on the engine thread
void OnWorldDeactivate(); // puppets belong to the old world; forget them
unsigned CurrentWorld();  // hash of the active world's name, 0 if none
bool IsPuppet(eCEntity const* entity);
// The body the local player controls: its hero, or the creature it is turned into (a transformation spell or potion
// makes Entity::GetPlayer() the creature while gCSession::GetHero() stays the hidden human body).
eCEntity* ControlledBody();
int PuppetOwner(eCEntity const* entity);  // player id whose puppet this is, 0 if none
eCEntity* PuppetOf(int playerId);
void DespawnAll(char const* reason);  // before a native save: puppets are hero entities (two heroes = fatal on load)
bool TraceEnabled();
using LogFn = void (*)(char const*);
void Report(LogFn log);   // QA: one REMOTE line per known player
void PuppetPartyOff(LogFn log);
void PuppetHoldOff(LogFn log);
void PuppetCombatOff(LogFn log);
void ShapeOff(LogFn log);  // QA negative control of stage T  // QA negative control: puppets replay swings by motion name  // QA negative control: puppets hold no weapons  // QA negative control: puppets do not join the local hero's party
void Step(int index, LogFn log);  // QA: move the local hero to point `index` of a 3 m circle
void WalkTo(int index, LogFn log); // QA: let the local hero run (game AI goto) to point `index` of a 15 m circle
}
