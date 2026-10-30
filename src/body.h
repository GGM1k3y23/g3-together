// SPDX-License-Identifier: GPL-3.0-or-later
// Driving a game body from network state: used for remote players' puppets and for NPCs mirrored
// from the host. The body's own AI is switched off; movement goes through its movement controller so
// the engine plays its own walk/run motions; stance and action motions are replayed by name.
#pragma once

#include <Game.h>
#include <Script.h>
#include <cstdint>
#include <vector>

namespace g3t::body {
float Distance(float const a[3], float const b[3]);
// Motion slot `type` of a body; slot 0 is what it visibly plays right now. Guarded against bad slots.
bool MotionName(eCVisualAnimation_PS* animation, int type, char* out, size_t size);
bool CurrentMotion(eCEntity* entity, char* out, size_t size);
bool MotionRunning(eCEntity* entity);  // slot 0 plays a motion (it is not stopped at its end)
float BoundaryHeight(eCEntity* entity); // highest bone of the posed body above its position in cm, < 0 unknown
bCMatrix Pose(eCEntity const& base, float const position[3], float const rotation[4]);

// A display buffer of a body's last snapshots (Stufe 1, owner 27.10.): the body is driven toward where the sender was
// Delay ms ago, interpolated between two snapshots by the sender's send times, and at most MaxAhead ms beyond the newest
// when snapshots are late. Before, the goal jumped to each new snapshot (every 50 to 100 ms, uneven over Hamachi).
struct Track
{
    struct Snap { uint32_t sent = 0; float position[3] = {}; float rotation[4] = {}; };
    Snap snaps[8];
    int count = 0;       // valid snapshots, oldest first
    int32_t offset = 0;  // arrival minus send time, the lowest seen (latency plus the clocks' difference)
    bool haveOffset = false;
    // Stufe 3.1: offset as the lowest of this and the last 2-s window (a slow path no longer pulls it up 1 ms a snapshot)
    int32_t windowMin = 0, lastWindowMin = 0;
    uint32_t windowAt = 0;
    uint8_t lastCase = 0;  // QA: how the last Sample answered (0 not sampled, 1 between, 2 ahead, 3 held, 4 older than all)
};
constexpr int32_t BufferDelay = 100;  // ms behind the sender's newest possible snapshot (the display buffer)
void Feed(Track& track, float const position[3], float const rotation[4], uint32_t sent, uint32_t arrived);
// Where to drive the body now: the sender's state at (its newest possible send time + shift ms), between two snapshots or on
// along the newest's velocity (at most MaxAhead beyond it). shift < 0: a display buffer behind the sender; shift > 0: a
// prediction ahead of the newest snapshot (puppet lead, Stufe 3.2). False when there is nothing yet.
bool Sample(Track& track, uint32_t now, float position[3], float rotation[4], int32_t shift = -BufferDelay, bool counted = true);
// Stufe 3.1 (stageGlide 28.10.: a steered body stood still in 20 % of its moving frames, with the display buffer or without):
// its movement controller reached the newest snapshot before the next came and stopped. With goalAhead it walks to where the
// sender will be GoalAhead ms later along its last velocity; placing and correcting keep the shown position.
// QA goalaheadon / goalaheadoff.
constexpr int32_t GoalAhead = 150;
extern bool goalAhead;
// The controller's goal for a body fed this track: ahead (goalAhead), else `fallback` (may be null). `ahead` holds the point.
float const* SteerGoal(Track& track, uint32_t now, float ahead[3], float const* fallback, bool force = false);
// Stufe 3 again (28.10.): a mirror's controller reached the newest snapshot and stood (still 2-31 % of its moving frames,
// stageGlide); goalAhead for everything was not better. With mirrorLead a mirror steers GoalAhead ahead only while its owner
// moves it steadily (Steadiness 1: no stop, start, braking or turn in its last snapshots). QA mirrorleadon (off by default).
extern bool mirrorLead;
extern bool interpolate;   // QA negative control: interpoff (the newest snapshot, as before 27.10.)
extern bool smoothWindow;  // QA smoothwindowoff: the smoothness counts gated by the newest two states (as before 28.10.)
extern bool offsetWindow;  // QA negative control: offsetwindowoff (offset drifting up 1 ms a snapshot, as before 28.10.)
extern unsigned sampledBetween, sampledAhead, sampledHeld, sampledOldest;

struct State
{
    bool walking = false;    // the movement controller is driving the body
    bool pacified = false;   // AI switched off by us
    unsigned repacified = 0; // how often a reaction slipped in and had to be undone
    uint32_t lastPacify = 0;
    char playing[96] = {};   // stance/action motion last started
    int actionSeq = -1;      // the sender's action number last seen (protocol 58)
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
    uint32_t localHitAt = 0;    // our own blow showed on it at once (npcs::LocalHit): its stagger's effect played here already
    uint32_t staggerEffectAt = 0;
    uint32_t combatMoveAt = 0;  // when the last combat move started (its own recover follows the hit by itself)
    bool puppet = false;        // QA (glide): a remote player's puppet, else a mirrored NPC
    float lastPos[3] = {};      // QA (glide): where the body stood at the last Drive, and when
    uint32_t lastAt = 0;
    bool steered = false;       // QA (glide): the last Drive steered the body through its movement controller
    uint32_t settleAt = 0;      // while steered: when to look again for a stance motion left on slot 0
    Track track;                // its snapshots for the display buffer
    uint8_t lastBranch = 0;     // QA: how the last Drive moved it (Glide::stillBy)
    uint8_t lastCase = 0;       // QA: how its last sample answered (Track::lastCase)
    float shownPos[16][3] = {}; // QA: where it stood at its last Drives, and when (display error)
    uint32_t steadySince = 0;   // Stufe 3.2b: since when its sender moves steadily (0: not now)
    uint32_t unsteadyAt = 0;    // when it last stopped, started, braked or turned
    uint32_t shownAt[16] = {};
    int shownNext = 0;
    uint32_t seekAt = 0;        // motion_seek: not again before then
};
// QA (Stufe 3.2): the sender stood at `truth` at this machine's time `at`; how far was the body from it then (Glide::
// displayErrors)? Nothing when no Drive was near that time.
void NoteTruth(State& state, float const truth[3], uint32_t at);

// QA (glide, owner's look 21.10.: characters glide instead of walking): time driven bodies travelled faster than
// 1 m/s, the part of it slot 0 showed no movement motion (gliding), the part steered vs. placed with MoveTo, and jumps.
struct Glide
{
    unsigned movingMs = 0, glideMs = 0, steerMs = 0, placeMs = 0, snaps = 0;
    // Stufe 3.2b, why it glided: placed by MoveTo the frame before (push), steered within OvershootFor after its sender
    // stopped, started, braked or turned (overshoot), or else (other)
    unsigned glidePushMs = 0, glideOvershootMs = 0, glideOtherMs = 0;
    // the puppet lead's steadiness factor, summed over the frames it was asked for
    float leadFactorSum = 0.0f;
    unsigned leadFactorFrames = 0;
    // Stufe 1, smoothness (S3): driven frames while the sender moved, frames that moved the body more than 30 cm, the largest
    // such step (cm), frames the body did not move although the sender did (stutter), and the body's distance to the newest
    // snapshot in those frames (lag behind the sender, cm, summed for the mean).
    unsigned frames = 0, jumps = 0, stillFrames = 0;
    float maxStep = 0.0f, lagSum = 0.0f;
    // Stufe 3.1, why still: by how the frame before drove the body (0 steered, 1 snapped, 2 placed, 3 held in an action) and
    // by how its sample answered (Track::lastCase)
    unsigned stillBy[4] = {}, stillCase[5] = {};
    // Stufe 3.2: how far the body stood from where its sender really was (cm), one sample per snapshot while it moved
    std::vector<float> displayErrors;
    // Display review 28.10.: hard corrections, the body set to the target by MoveTo (over 3 m; in an action over 1 m, swimming
    // over 40 cm), each restarting its motion or cutting an action
    unsigned hardSnaps = 0, actionSnaps = 0;
};
extern Glide glide[2];  // 0: mirrored NPCs, 1: puppets
void GlideReport(void (*log)(char const*));
// Speed in cm/s between two states, from the sender's clock: over Hamachi two states often arrived in one frame, their
// arrival times gave 0 and the body was placed instead of walking (owner's look 21.10.: characters glide). QA negative
// control `glideold`: from the arrival times, and a replayed stance motion is left running when the body sets off, as
// before 23.10.
float Speed(float const from[3], float const to[3], uint32_t sentFrom, uint32_t sentTo, uint32_t arrivedFrom, uint32_t arrivedTo);
void GlideOld(void (*log)(char const*));
void GlideStopOff(void (*log)(char const*));  // QA comparison: on set-off only StopPlayAni, the slot's motion runs on
void GlideReset(void (*log)(char const*));

struct Target
{
    float const* position;
    float const* rotation;
    uint8_t movementMode;  // gECharMovementMode
    char const* motion;    // motion file name, may be empty
    float speed;           // cm/s estimated from the last two states
    int aniState = -1;     // gEAniState to take on first (motions of another stance do not play otherwise), -1 = keep
    // Where the movement controller walks to (the newest snapshot); null: position. With the display buffer the position
    // lags 100 ms, and a controller walking to it reached it and stopped between snapshots (stageGlide 28.10.: 27 % of a
    // mirror's moving frames stood still, 2 % without the buffer); placing and correcting use the buffered position.
    float const* goal = nullptr;
    // Stufe 3.2b (a puppet with its lead): a deviation under DeadZone is not corrected, a larger one up to 3 m is walked off
    // through the movement controller instead of pushing the body (MoveTo) while its sender stands.
    bool smooth = false;
    // Over this error (cm) the body is set to the target at once (MoveTo); below it is walked off. 3 m as before; the walk line
    // (remote.cpp, walklineon) 6 m, a teleport goes over that.
    float snapAt = 300.0f;
    int actionSeq = -1;  // the sender's action number (protocol 58), -1 none
    int motionFrac = -1;      // protocol 59: how far the sender's slot 0 had played (0..65535 of its length), -1 none
    uint32_t motionAgeMs = 0; // how old that is now: since the state arrived plus half the round trip
};
// Display step 3 (review 28.10.): numbers each start of an action motion (not _Move_, not _Ambient_) on slot 0, a restart of the
// same motion too (its play time went back); the receiver then replays an action of the same name again (actionSeq, QA
// actionseqon, off by default). Without it, a second attack of the same name right after the first was not shown.
struct ActionClock
{
    char name[96] = {};
    double time = 0.0;
    uint8_t seq = 0;
};
uint8_t ActionSeq(eCEntity* entity, ActionClock& clock, char const* shown, uint16_t* elapsedMs);
extern bool actionSeq;
// Display step 4 (29.10., owner: "Animation ab Zeitpunkt"): a mirrored blow started at 0 on the receiver, a frame or more after
// the state arrived, so it showed late by the latency and the start. The sender adds how far slot 0 has played
// (MotionFraction, protocol 59); while the receiver's slot 0 runs the same combat motion (attack, parade, stagger; not
// movement, idles, death or knock-outs) its lag behind the expected point is measured (MOTIONLAG), and with motion_seek it
// jumps there (SetPlayTime) once it is more than SeekOver ms off. INI motion_seek = on | off (on since stageSeek 29.10.);
// QA seekon / seekoff (-1: the INI).
uint16_t MotionFraction(eCEntity* entity);  // 65535: nothing runs
extern int motionSeek;
constexpr float SeekOver = 60.0f;      // ms
constexpr uint32_t SeekEvery = 200;    // ms, at most one seek per body in that time
extern unsigned seeks[2];              // 0 mirrors, 1 puppets
void MotionLagReport(void (*log)(char const*));  // MOTIONLAG per kind; reset with glidereset
void MotionTime(eCEntity* entity, void (*log)(char const*));  // QA motiontime: MOTIONTIME name, play and max time of slot 0
extern bool fallMove;  // a sender's short fall drives the body as travel (QA fallmoveon; no gain, off)
extern unsigned sameNameStarts, sameNameReplayed;  // QA: a new number with the same name (lost without actionSeq) / replayed
// Stufe 3.2b: how steadily the sender moves by its last three snapshots, 0..1. Stopping, starting, braking or speeding up by
// a third, or turning more than SteadyTurn degrees: 0 at once (and unsteadyAt); steady again: up to 1 over SteadyRise ms.
float Steadiness(Track const& track, State& state, uint32_t now);
extern bool leadOld;  // QA comparison leadoldon: the lead as before (always whole, no dead zone, pushed back)

// QA: action motions started on driven bodies / on slot 0 right after the start / still showing 150 ms later
extern unsigned actionsStarted, actionsShown, actionsPlayed, pacifies, ownerReleases;
extern int motionOwner;  // QA: owner set on slot 0 after a replayed action (0 = leave)
extern unsigned combatMoves, combatMoveFaults;  // attacks/parades/staggers replayed as the game's combat move
// Stufe 4.4 without a protocol (28.10.): a driven body (mirror, puppet) that staggers from a blow shows the game's hit effect here
// (blood, or the clang of a parried blow); nothing on this machine judged the blow, so nothing started it. QA staggereffectsoff.
extern bool staggerEffects;
extern unsigned staggerEffectsPlayed, staggerEffectsSkipped;
extern bool stanceSync;                         // QA: off = a body keeps its own AniState
extern bool combatReplay;                       // QA: off = replay those by motion name too
// QA: StartPlayAni replays by kind (0 attack/parade/stagger by name, 1 knock-down/get-up, 2 gestures and the rest):
// started, on slot 0 right after the start, still on slot 0 150 ms later
extern unsigned kindStarted[3], kindPlayed[3], kindShown[3];
extern unsigned scriptAnis, scriptAniFaults, loopRestarts;  // gestures/knock-downs as play-ani instructions; loops restarted
extern bool scriptAniReplay, loopRestart;                    // QA comparisons: scriptanioff, looprestartoff
extern bool swimStrokes;                                     // QA negative control: swimstrokeoff
extern unsigned swimStrokesPlayed;                           // swim strokes started on puppets
char const* IdleTask();
void Pacify(Entity& entity, State& state);
void Release(Entity& entity, State& state);
void Drive(Entity& entity, State& state, Target const& target, uint32_t now);
// A motion as a play-ani instruction on the body's script processing unit, the way the game's scripts play them.
bool PlayAniInstruction(eCEntity* instance, State& state, char const* name);
int AIModeOf(eCEntity* entity);  // gEAIMode, -1 without a script routine
}
