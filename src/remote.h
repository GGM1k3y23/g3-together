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
// An NPC dealt with a puppet (judged it, was hit by it, hit it, attacked it): it lets go of the puppet when that goes.
void NoteContact(eCEntity* npc, eCEntity* puppet);
bool TraceEnabled();
using LogFn = void (*)(char const*);
void Report(LogFn log);   // QA: one REMOTE line per known player
void PuppetPartyOff(LogFn log);  // QA negative control: puppets do not join the local hero's party
void ForgetNpcListOn(LogFn log);  // QA negative control: forget puppets by walking Entity::GetNPCs (before 22.10.)
void ContactsOff(LogFn log);  // QA negative control: no NPC is noted as a puppet's contact
void BadStates(char const* argument, LogFn log);  // QA (stage X1): "on" sends NaN positions and unterminated strings, "off" stops
void PuppetHoldOff(LogFn log);  // QA negative control: puppets hold no weapons
void PuppetCombatOff(LogFn log);  // QA negative control: puppets replay swings by motion name
void ShapeOff(LogFn log);  // QA negative control of stage T
void Step(int index, LogFn log);  // QA: move the local hero to point `index` of a 3 m circle
void WalkTo(int index, LogFn log); // QA: let the local hero run (game AI goto) to point `index` of a 15 m circle
void WalkRel(char const* argument, LogFn log);  // QA: run to anchor + "dx_dz" cm, or "stop"
void WearHero(char const* argument, LogFn log);  // QA: the local hero wears an armor/helmet by name, -armor/-helmet takes it off
void WornScan(LogFn log);  // QA: armor and helmet of NPCs within 50 m
void WearOff(LogFn log);   // QA negative control: puppets wear nothing of their player
void LookOff(LogFn log);   // QA negative control: every player shown as puppet_template, its own look ignored
void PuppetTradeOn(char const* argument, LogFn log);  // QA (phase 2): trading switched on for player <id>'s puppet
void PuppetInfo(char const* argument, LogFn log);  // QA (phase 2): "<id>_<Info>": own name and a copied dialog option for a puppet
bool Switch(char const* action, LogFn log);  // QA negative controls of the play-evening fixes (27.10.): true if it was one
}
