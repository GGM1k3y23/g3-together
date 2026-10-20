// SPDX-License-Identifier: GPL-3.0-or-later
// Driving a game body from network state: used for remote players' puppets and for NPCs mirrored
// from the host. The body's own AI is switched off; movement goes through its movement controller so
// the engine plays its own walk/run motions; stance and action motions are replayed by name.
#pragma once

#include <Game.h>
#include <Script.h>
#include <cstdint>

namespace g3t::body {
float Distance(float const a[3], float const b[3]);
// Motion slot `type` of a body; slot 0 is what it visibly plays right now. Guarded against bad slots.
bool MotionName(eCVisualAnimation_PS* animation, int type, char* out, size_t size);
bool CurrentMotion(eCEntity* entity, char* out, size_t size);
bool MotionRunning(eCEntity* entity);  // slot 0 plays a motion (it is not stopped at its end)
float BoundaryHeight(eCEntity* entity); // highest bone of the posed body above its position in cm, < 0 unknown
bCMatrix Pose(eCEntity const& base, float const position[3], float const rotation[4]);

struct State
{
    bool walking = false;    // the movement controller is driving the body
    bool pacified = false;   // AI switched off by us
    unsigned repacified = 0; // how often a reaction slipped in and had to be undone
    uint32_t lastPacify = 0;
    char playing[96] = {};   // stance/action motion last started
    char routine[64] = {};   // the body's own daily routine, restored on Release
    char lastForeign[48] = {}; // the last task the game's AI picked before we undid it
    int aiMode = 0;
    uint32_t actionUntil = 0; // an action motion (attack, parade, hit) plays until then; the idle task waits
    uint32_t checkAt = 0;     // QA: when to look what the body shows after starting an action
    char started[96] = {};
    bool startedByMove = false; // QA: started as a combat move (its motion appears a frame later)
    bool scriptAniFaulted = false; // a play-ani instruction faulted on this body: StartPlayAni from then on
    uint32_t loopCheckAt = 0;      // when to look again whether a replayed loop still runs
    bool idleAsScriptAni = false;  // the next replay is a frozen idle: start it as a play-ani instruction
    bool combatOwned = false; // slot 0 carries the combat owner we set; released once the action stopped running
    bool combatMoves = false; // replay attacks as the game's combat moves (mirrored NPCs; a player's hero fights without its SPU)
    bool combatFaulted = false; // a combat move faulted on this body: motion names only from then on
    uint32_t combatMoveAt = 0;  // when the last combat move started (its own recover follows the hit by itself)
};

struct Target
{
    float const* position;
    float const* rotation;
    uint8_t movementMode;  // gECharMovementMode
    char const* motion;    // motion file name, may be empty
    float speed;           // cm/s estimated from the last two states
    int aniState = -1;     // gEAniState to take on first (motions of another stance do not play otherwise), -1 = keep
};

// QA: action motions started on driven bodies / on slot 0 right after the start / still showing 150 ms later
extern unsigned actionsStarted, actionsShown, actionsPlayed, pacifies, ownerReleases;
extern int motionOwner;  // QA: owner set on slot 0 after a replayed action (0 = leave)
extern unsigned combatMoves, combatMoveFaults;  // attacks/parades/staggers replayed as the game's combat move
extern bool combatReplay;                       // QA: off = replay those by motion name too
// QA: StartPlayAni replays by kind (0 attack/parade/stagger by name, 1 knock-down/get-up, 2 gestures and the rest):
// started, on slot 0 right after the start, still on slot 0 150 ms later
extern unsigned kindStarted[3], kindPlayed[3], kindShown[3];
extern unsigned scriptAnis, scriptAniFaults, loopRestarts;  // gestures/knock-downs as play-ani instructions; loops restarted
extern bool scriptAniReplay, loopRestart;                    // QA comparisons: scriptanioff, looprestartoff
char const* IdleTask();
void Pacify(Entity& entity, State& state);
void Release(Entity& entity, State& state);
void Drive(Entity& entity, State& state, Target const& target, uint32_t now);
// A motion as a play-ani instruction on the body's script processing unit, the way the game's scripts play them.
bool PlayAniInstruction(eCEntity* instance, State& state, char const* name);
int AIModeOf(eCEntity* entity);  // gEAIMode, -1 without a script routine
}
