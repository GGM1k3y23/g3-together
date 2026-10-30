// SPDX-License-Identifier: GPL-3.0-or-later
#include "body.h"

#include "config.h"
#include "log.h"

#include <Windows.h>
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <map>
#include <string>
#include <utility>
#include <vector>

namespace g3t::body {
unsigned actionsStarted = 0, actionsShown = 0, actionsPlayed = 0, pacifies = 0, ownerReleases = 0;
unsigned combatMoves = 0, combatMoveFaults = 0;
bool staggerEffects = true;
unsigned staggerEffectsPlayed = 0, staggerEffectsSkipped = 0;
// Owner of motion slot 0 after a replayed action. Setting 5 (what the game's combat moves set) kept attacks from being
// replaced by the stance's idle loop, but froze bodies in their bind pose once the motion ended (owner's look after V2:
// ~80 % of the client's mirrors had nothing running). 0 = leave as StartPlayAni set it.
int motionOwner = 0;
bool combatReplay = true;  // QA comparison: combatreplayoff
bool stanceSync = true;    // QA negative control: stanceoff (a body keeps its own AniState)
unsigned kindStarted[3] = {}, kindPlayed[3] = {}, kindShown[3] = {};
unsigned scriptAnis = 0, scriptAniFaults = 0, loopRestarts = 0;
bool scriptAniReplay = true;  // QA comparison: scriptanioff
bool loopRestart = true;      // QA comparison: looprestartoff
bool swimStrokes = true;      // QA negative control: swimstrokeoff (a swimming puppet only glides, as before 26.10.)
unsigned swimStrokesPlayed = 0;
Glide glide[2];
unsigned stanceStops = 0;   // replayed stance motions ended because the body set off
unsigned stopOwners[8] = {};  // QA: slot 0's owner when that happened
std::map<std::string, unsigned> glideMotions[2];  // what slot 0 showed while a body glided, ms by motion name
std::map<std::string, unsigned> stillMotions[2];  // QA (28.10.): the sender's motion in the still frames, frames by name

void GlideReport(void (*log)(char const*))
{
    char line[320];
    for (int kind = 0; kind < 2; ++kind) {
        Glide const& g = glide[kind];
        Format(line, "GLIDE kind=%s moving_ms=%u glide_ms=%u steer_ms=%u place_ms=%u snaps=%u stance_stops=%u owners=%u/%u/%u/%u/%u/%u/%u",
                  kind ? "puppet" : "mirror", g.movingMs, g.glideMs, g.steerMs, g.placeMs, g.snaps, stanceStops, stopOwners[0], stopOwners[1],
                  stopOwners[2], stopOwners[3], stopOwners[4], stopOwners[5], stopOwners[6]);
        log(line);
        Format(line, "SMOOTH kind=%s frames=%u jumps30=%u max_step_cm=%.0f still=%u lag_cm=%.0f interp=%d still_by=%u/%u/%u/%u "
                     "still_case=%u/%u/%u/%u/%u offset_window=%d goal_ahead=%d glide_by=%u/%u/%u lead_factor=%.2f lead_old=%d",
                  kind ? "puppet" : "mirror", g.frames, g.jumps, g.maxStep, g.stillFrames, g.frames ? g.lagSum / g.frames : 0.0f,
                  interpolate ? 1 : 0, g.stillBy[0], g.stillBy[1], g.stillBy[2], g.stillBy[3], g.stillCase[0], g.stillCase[1],
                  g.stillCase[2], g.stillCase[3], g.stillCase[4], offsetWindow ? 1 : 0, goalAhead ? 1 : 0, g.glidePushMs,
                  g.glideOvershootMs, g.glideOtherMs, g.leadFactorFrames ? g.leadFactorSum / g.leadFactorFrames : 0.0f, leadOld ? 1 : 0);
        log(line);
        std::vector<float> errors = g.displayErrors;
        std::sort(errors.begin(), errors.end());
        Format(line, "SNAPS kind=%s hard=%u action=%u same_name=%u replayed=%u action_seq=%d", kind ? "puppet" : "mirror", g.hardSnaps,
                  g.actionSnaps, sameNameStarts, sameNameReplayed, actionSeq ? 1 : 0);
        log(line);
        Format(line, "LEAD kind=%s samples=%zu err_median_cm=%.0f err_p95_cm=%.0f sampled=%u/%u/%u/%u", kind ? "puppet" : "mirror",
                  errors.size(), errors.empty() ? 0.0f : errors[errors.size() / 2],
                  errors.empty() ? 0.0f : errors[std::min(errors.size() - 1, errors.size() * 95 / 100)], sampledBetween, sampledAhead,
                  sampledHeld, sampledOldest);
        log(line);
        // The five motions shown longest while gliding (a name without _Move_ is not always a still body: starts, stops).
        std::vector<std::pair<unsigned, std::string>> top;
        for (auto const& [name, ms] : glideMotions[kind]) top.emplace_back(ms, name);
        std::sort(top.rbegin(), top.rend());
        for (size_t i = 0; i < top.size() && i < 5; ++i) {
            Format(line, "GLIDEMOTION kind=%s ms=%u motion=%.120s", kind ? "puppet" : "mirror", top[i].first, top[i].second.c_str());
            log(line);
        }
        std::vector<std::pair<unsigned, std::string>> still;
        for (auto const& [name, frames] : stillMotions[kind]) still.emplace_back(frames, name);
        std::sort(still.rbegin(), still.rend());
        for (size_t i = 0; i < still.size() && i < 5; ++i) {
            Format(line, "STILLMOTION kind=%s frames=%u motion=%.120s", kind ? "puppet" : "mirror", still[i].first, still[i].second.c_str());
            log(line);
        }
    }
}

bool arrivalSpeed = false;  // QA negative control: glideold
bool settleStance = true;   // QA negative control: glideold
bool stopMotion = true;     // QA comparison: glidestopoff (only StopPlayAni on set-off)

// Off by default (28.10.): under G3_NETSIM 80,40,3 mirrors stood still in some 25 % of their moving frames with the
// buffer (2 % without), with the controller's goal on the newest snapshot as well; the puppet's jumps were not fewer in a
// second run. QA interpon switches it on for the measurement.
bool interpolate = false;
bool smoothWindow = true;
bool fallMove = false;  // off: no gain (stageWalk 28.10.: fall still frames 37/19/0 with it, 10/5 without)
bool actionSeq = true;  // on by default (stageActionSeq 28.10.: without it 68-83 starts of the same name lost, with it all replayed; stageV2 3/3 green)
unsigned sameNameStarts = 0, sameNameReplayed = 0;

uint8_t ActionSeq(eCEntity* entity, ActionClock& clock, char const* shown, uint16_t* elapsedMs)
{
    if (elapsedMs) *elapsedMs = 0;
    const bool action = shown && *shown && !strstr(shown, "_Move_") && !strstr(shown, "_Ambient_");
    if (!action) {
        *clock.name = '\0';
        return clock.seq;
    }
    auto* animation = entity ? static_cast<eCVisualAnimation_PS*>(entity->GetPropertySet(eEPropertySetType_Animation)) : nullptr;
    eCWrapper_emfx2Actor* actor = animation ? animation->GetActor() : nullptr;
    const double time = actor ? actor->GetPlayTime(static_cast<eCWrapper_emfx2Actor::eEMotionType>(0)) : 0.0;
    if (strcmp(shown, clock.name) != 0 || time + 0.05 < clock.time) {
        ++clock.seq;
        strncpy_s(clock.name, shown, _TRUNCATE);
    }
    clock.time = time;
    if (elapsedMs) *elapsedMs = static_cast<uint16_t>(std::min(time * 1000.0, 65535.0));
    return clock.seq;
}

int motionSeek = -1;
unsigned seeks[2] = {};
std::vector<float> motionLags[2];  // ms the receiver's combat motion was behind where the sender's was, per Drive (+ behind)
bool lagMsUnit = false;            // the last length looked like milliseconds (see MotionUnitMs)
// QA (stageSeek 29.10.: no mirror sample at all): Drives with a combat motion as target, and why FollowSender measured none:
// no fraction, not a combat motion, nothing running here, another motion here, over at the sender; and measured
unsigned combatTargets[2] = {}, followSkips[2][6] = {}, otherNamesLogged[2] = {};

// Play and max time of slot 0 come in one unit, not yet seen in a test (QA motiontime). The fraction travels without one; for
// the age of a state (ms) it is needed: a combat motion lasts about 0.3 to 4 s, so a length over 30 is taken as milliseconds.
double MotionUnitMs(double maxTime)
{
    lagMsUnit = maxTime > 30.0;
    return lagMsUnit ? 1.0 : 1000.0;
}

uint16_t MotionFraction(eCEntity* entity)
{
    auto* animation = entity ? static_cast<eCVisualAnimation_PS*>(entity->GetPropertySet(eEPropertySetType_Animation)) : nullptr;
    eCWrapper_emfx2Actor* actor = animation ? animation->GetActor() : nullptr;
    const auto slot0 = static_cast<eCWrapper_emfx2Actor::eEMotionType>(0);
    if (!actor || !actor->IsMotionRunning(slot0)) return 65535;
    const double max = actor->GetMaxTime(slot0);
    if (!(max > 0.0)) return 65535;
    const double part = actor->GetPlayTime(slot0) / max;
    return static_cast<uint16_t>(std::clamp(part, 0.0, 1.0) * 65535.0 + 0.5);
}

void MotionTime(eCEntity* entity, void (*log)(char const*))
{
    char line[260], name[128] = "-";
    auto* animation = entity ? static_cast<eCVisualAnimation_PS*>(entity->GetPropertySet(eEPropertySetType_Animation)) : nullptr;
    eCWrapper_emfx2Actor* actor = animation ? animation->GetActor() : nullptr;
    if (!actor) { log("REFUSED motiontime: no body or actor"); return; }
    const auto slot0 = static_cast<eCWrapper_emfx2Actor::eEMotionType>(0);
    MotionName(animation, 0, name, sizeof(name));
    Format(line, "MOTIONTIME name=%.120s running=%d play=%.4f max=%.4f frac=%u", *name ? name : "-", actor->IsMotionRunning(slot0) ? 1 : 0,
           actor->GetPlayTime(slot0), actor->GetMaxTime(slot0), MotionFraction(entity));
    log(line);
}

void MotionLagReport(void (*log)(char const*))
{
    char line[220];
    const bool on = motionSeek < 0 ? GetConfig().motionSeek : motionSeek > 0;
    for (int kind = 0; kind < 2; ++kind) {
        std::vector<float> behind = motionLags[kind], off;
        for (float lag : behind) off.push_back(std::fabs(lag));
        std::sort(behind.begin(), behind.end());
        std::sort(off.begin(), off.end());
        auto at = [](std::vector<float> const& v, size_t percent) { return v.empty() ? 0.0f : v[std::min(v.size() - 1, v.size() * percent / 100)]; };
        unsigned const* why = followSkips[kind];
        Format(line, "MOTIONLAG kind=%s n=%zu p50=%.0fms p95=%.0fms behind_p50=%.0fms seeks=%u seek=%d unit=%s combat_targets=%u "
                     "skipped=%u/%u/%u/%u/%u measured=%u", kind ? "puppet" : "mirror", off.size(), at(off, 50), at(off, 95), at(behind, 50),
               seeks[kind], on ? 1 : 0, lagMsUnit ? "ms" : "s", combatTargets[kind], why[0], why[1], why[2], why[3], why[4], why[5]);
        log(line);
    }
}

// QA: the sender's travel over its states of the last 200 ms or more (cm/s, by its own clock).
float TravelSpeed(Track const& track)
{
    if (track.count < 2) return 0.0f;
    Track::Snap const& newest = track.snaps[track.count - 1];
    for (int i = track.count - 2; i >= 0; --i) {
        const uint32_t span = newest.sent - track.snaps[i].sent;
        if (span >= 200) return Distance(newest.position, track.snaps[i].position) * 1000.0f / span;
    }
    return 0.0f;
}
bool offsetWindow = true;
unsigned sampledBetween = 0, sampledAhead = 0, sampledHeld = 0, sampledOldest = 0;
constexpr int32_t MaxAhead = 250;  // ms beyond the newest snapshot at most
constexpr uint32_t OffsetWindow = 2000;  // ms

void Feed(Track& track, float const position[3], float const rotation[4], uint32_t sent, uint32_t arrived)
{
    if (track.count && static_cast<int32_t>(sent - track.snaps[track.count - 1].sent) <= 0) return;  // known, or out of order
    const int32_t offset = static_cast<int32_t>(arrived - sent);
    if (!track.haveOffset) {
        track.offset = track.windowMin = track.lastWindowMin = offset;
        track.windowAt = arrived;
        track.haveOffset = true;
    } else if (offsetWindow) {
        // The lowest arrival minus send time of this and the last window: follows a clock drift or a slower path within 4 s at
        // most, and a single slow snapshot does not move it.
        if (arrived - track.windowAt >= OffsetWindow) {
            track.lastWindowMin = track.windowMin;
            track.windowMin = offset;
            track.windowAt = arrived;
        } else if (offset < track.windowMin) {
            track.windowMin = offset;
        }
        track.offset = std::min(track.windowMin, track.lastWindowMin);
    } else if (offset < track.offset) {
        track.offset = offset;
    } else {
        ++track.offset;  // a slower path than the fastest one seen: follow it slowly (1 ms a snapshot)
    }
    if (track.count == 8) {
        for (int i = 1; i < 8; ++i) track.snaps[i - 1] = track.snaps[i];
        --track.count;
    }
    Track::Snap& s = track.snaps[track.count++];
    s.sent = sent;
    std::memcpy(s.position, position, sizeof(s.position));
    std::memcpy(s.rotation, rotation, sizeof(s.rotation));
}

bool goalAhead = false;
bool leadOld = false;
constexpr float SteadyTurn = 30.0f;     // degrees (45 and 0.5-2x speed, stageGlide 28.10.: not steadier, a puppet stood 18 %)
constexpr uint32_t SteadyRise = 200;    // ms
constexpr uint32_t OvershootFor = 500;  // ms after an unsteady moment, for the glide causes
constexpr float DeadZone = 50.0f;       // cm

float Steadiness(Track const& track, State& state, uint32_t now)
{
    bool steady = track.count >= 3;
    if (steady) {
        Track::Snap const& a = track.snaps[track.count - 3];
        Track::Snap const& b = track.snaps[track.count - 2];
        Track::Snap const& c = track.snaps[track.count - 1];
        const int32_t t0 = static_cast<int32_t>(b.sent - a.sent), t1 = static_cast<int32_t>(c.sent - b.sent);
        float v0[3], v1[3], s0 = 0.0f, s1 = 0.0f, dot = 0.0f;
        for (int k = 0; k < 3; ++k) {
            v0[k] = t0 > 0 ? (b.position[k] - a.position[k]) * 1000.0f / t0 : 0.0f;
            v1[k] = t1 > 0 ? (c.position[k] - b.position[k]) * 1000.0f / t1 : 0.0f;
            s0 += v0[k] * v0[k], s1 += v1[k] * v1[k], dot += v0[k] * v1[k];
        }
        s0 = std::sqrt(s0), s1 = std::sqrt(s1);
        steady = t0 > 0 && t1 > 0 && t0 < 500 && t1 < 500 && s0 > 100.0f && s1 > 100.0f && s1 > s0 * 0.67f && s1 < s0 * 1.5f &&
                 dot / (s0 * s1) > std::cos(SteadyTurn * 3.14159265f / 180.0f);
    }
    if (!steady) {
        state.steadySince = 0;
        state.unsteadyAt = now;
        return 0.0f;
    }
    if (!state.steadySince) state.steadySince = now ? now : 1;
    const float rise = static_cast<float>(now - state.steadySince) / SteadyRise;
    return rise < 1.0f ? rise : 1.0f;
}


bool mirrorLead = false;

float const* SteerGoal(Track& track, uint32_t now, float ahead[3], float const* fallback, bool force)
{
    if (!goalAhead && !force) return fallback;
    float turn[4];
    const uint8_t lastCase = track.lastCase;  // the shown sample's, for the diagnostics
    const bool got = Sample(track, now, ahead, turn, GoalAhead, false);
    track.lastCase = lastCase;
    return got ? ahead : fallback;
}

bool Sample(Track& track, uint32_t now, float position[3], float rotation[4], int32_t shift, bool counted)
{
    if (counted) track.lastCase = 0;
    if (!track.count) return false;
    const uint32_t render = now - static_cast<uint32_t>(track.offset) + static_cast<uint32_t>(shift);  // in the sender's clock
    Track::Snap const& newest = track.snaps[track.count - 1];
    if (static_cast<int32_t>(render - newest.sent) >= 0) {
        // Later than the newest: on along its last velocity, at most MaxAhead, then held.
        int32_t ahead = static_cast<int32_t>(render - newest.sent);
        if (!counted) ahead = std::min(ahead, MaxAhead);
        else if (ahead > MaxAhead) ahead = MaxAhead, ++sampledHeld, track.lastCase = 3;
        else ++sampledAhead, track.lastCase = 2;
        std::memcpy(position, newest.position, sizeof(newest.position));
        std::memcpy(rotation, newest.rotation, sizeof(newest.rotation));
        if (track.count >= 2) {
            Track::Snap const& before = track.snaps[track.count - 2];
            const int32_t span = static_cast<int32_t>(newest.sent - before.sent);
            if (span > 0 && span < 1000)
                for (int k = 0; k < 3; ++k) position[k] += (newest.position[k] - before.position[k]) * ahead / span;
        }
        return true;
    }
    for (int i = track.count - 1; i > 0; --i) {
        Track::Snap const& a = track.snaps[i - 1];
        Track::Snap const& b = track.snaps[i];
        if (static_cast<int32_t>(render - a.sent) < 0) continue;
        const int32_t span = static_cast<int32_t>(b.sent - a.sent);
        const float t = span > 0 ? static_cast<float>(static_cast<int32_t>(render - a.sent)) / span : 1.0f;
        for (int k = 0; k < 3; ++k) position[k] = a.position[k] + (b.position[k] - a.position[k]) * t;
        // Rotation: the nearer of the two quaternions' sides, blended and normalised.
        float dot = 0.0f;
        for (int k = 0; k < 4; ++k) dot += a.rotation[k] * b.rotation[k];
        const float sign = dot < 0.0f ? -1.0f : 1.0f;
        float length = 0.0f;
        for (int k = 0; k < 4; ++k) rotation[k] = a.rotation[k] * (1.0f - t) + sign * b.rotation[k] * t, length += rotation[k] * rotation[k];
        length = std::sqrt(length);
        if (length > 0.0f) for (int k = 0; k < 4; ++k) rotation[k] /= length;
        if (counted) ++sampledBetween, track.lastCase = 1;
        return true;
    }
    if (counted) ++sampledOldest, track.lastCase = 4;
    std::memcpy(position, track.snaps[0].position, sizeof(track.snaps[0].position));  // older than all: the oldest
    std::memcpy(rotation, track.snaps[0].rotation, sizeof(track.snaps[0].rotation));
    return true;
}

void NoteTruth(State& state, float const truth[3], uint32_t at)
{
    int best = -1;
    int32_t bestGap = 60;  // ms: a Drive at most this far from the time asked for
    for (int i = 0; i < 16; ++i) {
        if (!state.shownAt[i]) continue;
        const int32_t gap = std::abs(static_cast<int32_t>(state.shownAt[i] - at));
        if (gap < bestGap) bestGap = gap, best = i;
    }
    if (best < 0) return;
    auto& errors = glide[state.puppet ? 1 : 0].displayErrors;
    if (errors.size() < 20000) errors.push_back(Distance(state.shownPos[best], truth));
}

float Speed(float const from[3], float const to[3], uint32_t sentFrom, uint32_t sentTo, uint32_t arrivedFrom, uint32_t arrivedTo)
{
    const uint32_t interval = arrivalSpeed ? arrivedTo - arrivedFrom : sentTo - sentFrom;
    return interval ? Distance(from, to) * 1000.0f / interval : 0.0f;
}

void GlideStopOff(void (*log)(char const*))
{
    stopMotion = false;
    log("GLIDESTOPOFF ok");
}

void GlideOld(void (*log)(char const*))
{
    arrivalSpeed = true;
    settleStance = false;
    log("GLIDEOLD on");
}

void GlideReset(void (*log)(char const*))
{
    glide[0] = glide[1] = Glide{};
    motionLags[0].clear();
    motionLags[1].clear();
    seeks[0] = seeks[1] = 0;
    for (int kind = 0; kind < 2; ++kind) {
        combatTargets[kind] = otherNamesLogged[kind] = 0;
        for (unsigned& n : followSkips[kind]) n = 0;
    }
    glideMotions[0].clear();
    stillMotions[0].clear();
    stillMotions[1].clear();
    glideMotions[1].clear();
    log("GLIDERESET ok");
}

int ReplayKind(char const* name)
{
    if (strstr(name, "KnockDown") || strstr(name, "GetUp")) return 1;
    if (strstr(name, "Attack") || strstr(name, "Parade") || strstr(name, "Stumble")) return 0;
    return 2;
}
namespace {
// A driven body must not think for itself: no daily routine, AI mode "Abandoned" (perception reactions
// such as watching a fight kept overriding plain tasks), and an idle task.
char const* const Idle = "ZS_HangAround_DoNothing";

gCScriptRoutine_PS* RoutineOf(eCEntity* entity)
{
    return entity ? static_cast<gCScriptRoutine_PS*>(entity->GetPropertySet(eEPropertySetType_ScriptRoutine)) : nullptr;
}

// gEAction values of the combat moves whose motion names travel (GameEnum.h); deaths and knock-outs keep StartPlayAni.
struct ActionName { char const* name; int value; };
const ActionName CombatActions[] = {
    // Stufe 4.3: JumpBack (the evasive jump of fighters and animals, _AI_JumpBack SG 0x46560) and ExitParade were replayed
    // by name; they are combat moves too.
    {"JumpBack", 51}, {"ExitParade", 19},
    {"Attack", 1}, {"PowerAttack", 2}, {"QuickAttack", 3}, {"QuickAttackR", 4}, {"QuickAttackL", 5}, {"SimpleWhirl", 6},
    {"SprintAttack", 9}, {"WhirlAttack", 10}, {"PierceAttack", 11}, {"JumpAttack", 12}, {"RamAttack", 13},
    {"HackAttack", 14}, {"FinishingAttack", 15}, {"Parade", 16}, {"ParadeR", 17}, {"ParadeL", 18},
    {"QuickParadeStumble", 20}, {"ParadeStumble", 21}, {"ParadeStumbleR", 22}, {"ParadeStumbleL", 23},
    {"HeavyParadeStumble", 24}, {"QuickStumble", 25}, {"Stumble", 26}, {"StumbleR", 27}, {"StumbleL", 28},
    {"GetUpAttack", 30}, {"GetUpParade", 31}, {"PierceStumble", 34}, {"AbortAttack", 39}};

// "Orc_Stand_None_2H_P0_PowerAttack_Hit_N_Fwd_00_%_00_P1_140_R": the sixth part is the action, the seventh the phase.
bool ParseCombatMove(char const* name, int& action, char* phase, size_t size)
{
    char const* part = name;
    for (int i = 0; i < 5 && part; ++i) {
        part = std::strchr(part, '_');
        if (part) ++part;
    }
    if (!part) return false;
    char const* end = std::strchr(part, '_');
    if (!end) return false;
    const size_t length = static_cast<size_t>(end - part);
    action = 0;
    for (auto const& entry : CombatActions)
        if (std::strlen(entry.name) == length && !std::strncmp(entry.name, part, length)) action = entry.value;
    if (!action) return false;
    char const* phaseStart = end + 1;
    char const* phaseEnd = std::strchr(phaseStart, '_');
    if (!phaseEnd || static_cast<size_t>(phaseEnd - phaseStart) >= size) return false;
    std::memcpy(phase, phaseStart, phaseEnd - phaseStart);
    phase[phaseEnd - phaseStart] = '\0';
    return true;
}

GEBool CallCombatMove(gCScriptProcessingUnit::sAICombatMoveInstr_Args* args, gCScriptProcessingUnit* spu, bool* fault)
{
    __try {
        return gCScriptProcessingUnit::sAICombatMoveInstr(args, spu, GEFalse);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        *fault = true;
        return GETrue;
    }
}

GEBool CallPlayAni(gCScriptProcessingUnit::sAIPlayAniInstr_Args* args, gCScriptProcessingUnit* spu, bool* fault)
{
    __try {
        return gCScriptProcessingUnit::sAIPlayAniInstr(args, spu, GEFalse);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        *fault = true;
        return GETrue;
    }
}

// Replay a gesture, a knock-down or anything else that is no combat move the way the game's scripts play them (e.g.
// _AI_TransferItem in ZS_Cook): as a play-ani instruction on the body's script processing unit. Started with
// Entity::StartPlayAni the body's stance replaced them within 150 ms (19.10.: 4 of 91 gestures and 4 of 77 knock-downs
// still showing).
bool PlayScriptAni(eCEntity* instance, State& state, char const* name)
{
    if (!scriptAniReplay || state.scriptAniFaulted) return false;
    gCScriptRoutine_PS* routine = RoutineOf(instance);
    if (!routine) return false;
    auto* args = GE_NEW(gCScriptProcessingUnit::sAIPlayAniInstr_Args)(instance, nullptr, bCString(name), 1.0f);
    bool fault = false;
    CallPlayAni(args, &routine->GetSPU(), &fault);
    if (fault) {
        ++scriptAniFaults;
        state.scriptAniFaulted = true;
        Logf("body: play-ani %s faulted; this body replays by StartPlayAni from now on", name);
        return false;
    }
    ++scriptAnis;
    return true;
}

// Replay an attack, parade or stagger the way the game's own fight AI plays it: as a combat move on the body's
// script processing unit, which keeps the motion (owner 5) and drives it to its end, then hands slot 0 back. No
// target: nobody is hit (and a mirror's hits are dropped anyway, combat.cpp rule 1).
bool PlayCombatMove(eCEntity* instance, State& state, char const* name, uint32_t now)
{
    int action = 0;
    char phase[24];
    if (!combatReplay || !state.combatMoves || state.combatFaulted || !ParseCombatMove(name, action, phase, sizeof(phase))) return false;
    // The game's combat move goes on into its recover after the hit by itself (sAICombatMoveStartRecover); starting a
    // recover as a move of its own faulted now and then. Leave it to the running move.
    if (!std::strcmp(phase, "Recover")) return static_cast<int32_t>(now - state.combatMoveAt) < 1500;
    gCScriptRoutine_PS* routine = RoutineOf(instance);
    if (!routine) return false;
    auto* args = GE_NEW(gCScriptProcessingUnit::sAICombatMoveInstr_Args)(instance, nullptr, static_cast<gEAction>(action),
                                                                          bCString(phase), 1.0f);
    bool fault = false;
    CallCombatMove(args, &routine->GetSPU(), &fault);
    if (fault) {
        ++combatMoveFaults;
        state.combatFaulted = true;  // never again on this body
        Logf("body: combat move %s faulted; this body replays by motion name from now on", name);
        return false;
    }
    ++combatMoves;
    // A stagger from a blow (Stumble*, QuickStumble, PierceStumble: blood; ParadeStumble*: the clang of a parried blow), at its
    // hit phase: the game's hit effect, as DoLogicalDamage starts it where the blow is judged (SG 0xb46d0). Not when our own
    // blow showed on it at once (the game here started its effect with our hit), and once per stagger.
    // Mirrors only: a puppet is struck on this machine, where the blow was judged and the game started its effect.
    if (staggerEffects && !state.puppet && !std::strcmp(phase, "Hit") && ((action >= 20 && action <= 28) || action == 34)) {
        if ((state.localHitAt && now - state.localHitAt < 1500) || (state.staggerEffectAt && now - state.staggerEffectAt < 400)) {
            ++staggerEffectsSkipped;
        } else {
            state.staggerEffectAt = now ? now : 1;
            const bCString effect(action <= 24 ? "eff_col_wh_01_me_me" : "eff_col_wh_02_me_fl");
            if (EffectSystem::ExistsEffect(effect)) {
                EffectSystem::StartEffect(effect, Entity(instance));
                ++staggerEffectsPlayed;
            }
        }
    }
    state.combatMoveAt = now;
    return true;
}
}

char const* IdleTask() { return Idle; }

float Distance(float const a[3], float const b[3])
{
    const float dx = a[0] - b[0], dy = a[1] - b[1], dz = a[2] - b[2];
    return std::sqrt(dx * dx + dy * dy + dz * dz);
}

bool MotionName(eCVisualAnimation_PS* animation, int type, char* out, size_t size)
{
    __try {
        bCString const& file = animation->GetMotionDesc(static_cast<eCWrapper_emfx2Actor::eEMotionType>(type)).GetMotionFilename();
        strncpy_s(out, size, file.GetText(), _TRUNCATE);
        return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        strncpy_s(out, size, "<fault>", _TRUNCATE);
        return false;
    }
}

bool CurrentMotion(eCEntity* entity, char* out, size_t size)
{
    *out = '\0';
    eCVisualAnimation_PS* animation =
        entity ? static_cast<eCVisualAnimation_PS*>(entity->GetPropertySet(eEPropertySetType_Animation)) : nullptr;
    return animation && MotionName(animation, 0, out, size);
}

bool MotionRunning(eCEntity* entity)
{
    auto* animation = entity ? static_cast<eCVisualAnimation_PS*>(entity->GetPropertySet(eEPropertySetType_Animation)) : nullptr;
    eCWrapper_emfx2Actor* actor = animation ? animation->GetActor() : nullptr;
    return actor && actor->IsMotionRunning(static_cast<eCWrapper_emfx2Actor::eEMotionType>(0));
}

float BoneTop(eCVisualAnimation_PS* animation, bCVector const& base, int* bones)
{
    float top = -1e9f;
    *bones = 0;
    __try {
        bCMatrix m;
        for (int i = 0; i < 128 && animation->GetBoneMatrix(i, m); ++i) {
            bCVector const& at = m.GetTranslation();
            const float dx = at.GetX() - base.GetX(), dz = at.GetZ() - base.GetZ(), dy = at.GetY() - base.GetY();
            if (dx * dx + dz * dz > 400.0f * 400.0f || dy < -200.0f || dy > 400.0f) continue;  // not part of this body's skeleton
            if (dy > top) top = dy;
            ++*bones;
        }
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return -2.0f;
    }
    return *bones ? top : -1.0f;
}

// Height of the posed body: its highest bone above its position (a lying body is flat). The bounds of the actor do not
// follow the pose (deaddiag: about 195 cm for standing and lying orcs alike).
float BoundaryHeight(eCEntity* entity)
{
    auto* animation = entity ? static_cast<eCVisualAnimation_PS*>(entity->GetPropertySet(eEPropertySetType_Animation)) : nullptr;
    if (!animation) return -1.0f;
    int bones = 0;
    return BoneTop(animation, entity->GetWorldMatrix().GetTranslation(), &bones);
}

bCMatrix Pose(eCEntity const& base, float const position[3], float const rotation[4])
{
    bCMatrix m = base.GetWorldMatrix();
    bCQuaternion q;
    q.AccessX() = rotation[0];
    q.AccessY() = rotation[1];
    q.AccessZ() = rotation[2];
    q.AccessW() = rotation[3];
    m.ModifyRotation(q);
    m.AccessTranslation() = bCVector(position[0], position[1], position[2]);
    return m;
}

bool PlayAniInstruction(eCEntity* instance, State& state, char const* name)
{
    return PlayScriptAni(instance, state, name);
}

int AIModeOf(eCEntity* entity)
{
    gCScriptRoutine_PS* routine = RoutineOf(entity);
    return routine ? static_cast<int>(static_cast<gEAIMode&>(routine->AccessAIMode())) : -1;
}

void Pacify(Entity& entity, State& state)
{
    // Never touch combat targets here: clearing them while a scene script ran a combat state crashed the
    // game (gCScriptProcessingUnit::sAICombatMoveInstr). AI states of driven bodies are skipped in ai.cpp.
    entity.Routine.FullStop();
    if (gCScriptRoutine_PS* routine = RoutineOf(entity.GetInstance())) {
        bCString& name = static_cast<bCString&>(routine->AccessRoutine());
        gEAIMode& mode = static_cast<gEAIMode&>(routine->AccessAIMode());
        if (!state.pacified) {
            strncpy_s(state.routine, name.GetText(), _TRUNCATE);
            state.aiMode = static_cast<int>(mode);
        }
        name = bCString("");
        mode = gEAIMode_Abandoned;
    }
    entity.Routine.SetTask(bCString(Idle));
    state.pacified = true;
}

void Release(Entity& entity, State& state)
{
    if (!state.pacified || !entity.GetInstance()) { state = State{}; return; }
    entity.CharacterMovement.StopMovement();
    if (gCScriptRoutine_PS* routine = RoutineOf(entity.GetInstance())) {
        static_cast<bCString&>(routine->AccessRoutine()) = bCString(state.routine);
        static_cast<gEAIMode&>(routine->AccessAIMode()) = static_cast<gEAIMode>(state.aiMode);
    }
    entity.Routine.FullStop();
    entity.Routine.ContinueRoutine();
    state = State{};
}

bool SlotRunning(eCEntity* instance)
{
    auto* animation = static_cast<eCVisualAnimation_PS*>(instance->GetPropertySet(eEPropertySetType_Animation));
    eCWrapper_emfx2Actor* actor = animation ? animation->GetActor() : nullptr;
    return actor && actor->IsMotionRunning(static_cast<eCWrapper_emfx2Actor::eEMotionType>(0));
}

// The combat owner keeps the stance logic from replacing a replayed attack, but it also keeps it from taking over
// when the attack has ended: with nothing running on slot 0 the body froze in its bind pose (owner's look after V2:
// ~80 % of the client's mirrors stood still). Hand slot 0 back (owner 2, as the game does before it starts one).
void ReleaseFinished(eCEntity* instance, State& state)
{
    if (!state.combatOwned) return;
    auto* animation = static_cast<eCVisualAnimation_PS*>(instance->GetPropertySet(eEPropertySetType_Animation));
    eCWrapper_emfx2Actor* actor = animation ? animation->GetActor() : nullptr;
    const auto slot0 = static_cast<eCWrapper_emfx2Actor::eEMotionType>(0);
    if (actor && actor->IsMotionRunning(slot0)) return;
    if (animation) animation->SetMotionOwner(slot0, static_cast<eCVisualAnimation_PS::eSMotionDesc::eEMotionOwner>(2));
    state.combatOwned = false;
    state.actionUntil = 0;   // the action is over: pose and stance may be corrected again
    *state.playing = '\0';   // the same motion may be started again
    ++ownerReleases;
}

// Display step 4: a combat motion running here as at the sender is measured against where the sender's is now and, with
// motion_seek, moved there. A combat move starts its motion a frame after it was asked for, so the start is caught in the first
// frame slot 0 shows it.
void FollowSender(eCEntity* instance, State& state, Target const& target, uint32_t now)
{
    const int kind = state.puppet ? 1 : 0;
    if (!target.motion || !*target.motion || strstr(target.motion, "_Move_") || strstr(target.motion, "_Ambient_") ||
        ReplayKind(target.motion) != 0) { ++followSkips[kind][1]; return; }
    if (target.motionFrac < 0 || target.motionFrac >= 65535) { ++followSkips[kind][0]; return; }
    auto* animation = static_cast<eCVisualAnimation_PS*>(instance->GetPropertySet(eEPropertySetType_Animation));
    eCWrapper_emfx2Actor* actor = animation ? animation->GetActor() : nullptr;
    const auto slot0 = static_cast<eCWrapper_emfx2Actor::eEMotionType>(0);
    if (!actor || !actor->IsMotionRunning(slot0)) { ++followSkips[kind][2]; return; }
    char shows[128];
    if (!MotionName(animation, 0, shows, sizeof(shows)) || strcmp(shows, target.motion) != 0) {
        ++followSkips[kind][3];
        if (otherNamesLogged[kind] < 8 && ++otherNamesLogged[kind])
            Logf("body: seek %s: the sender plays %.100s, here %.100s", kind ? "puppet" : "mirror", target.motion, shows);
        return;
    }
    const double max = actor->GetMaxTime(slot0);
    if (!(max > 0.0)) { ++followSkips[kind][2]; return; }
    const double unitMs = MotionUnitMs(max);
    const double expected = target.motionFrac / 65535.0 * max + target.motionAgeMs / unitMs;
    if (expected >= max) { ++followSkips[kind][4]; return; }  // over at the sender by now (or started again: its new number restarts it here)
    ++followSkips[kind][5];
    float lag = static_cast<float>((expected - actor->GetPlayTime(slot0)) * unitMs);
    const bool on = motionSeek < 0 ? GetConfig().motionSeek : motionSeek > 0;
    if (on && std::fabs(lag) > SeekOver && static_cast<int32_t>(now - state.seekAt) >= 0) {
        actor->SetPlayTime(slot0, static_cast<float>(expected));
        state.seekAt = now + SeekEvery;
        ++seeks[kind];
        lag = static_cast<float>((expected - actor->GetPlayTime(slot0)) * unitMs);  // what this frame shows: did it take?
    }
    std::vector<float>& lags = motionLags[kind];
    if (lags.size() < 50000) lags.push_back(lag);
}

void Drive(Entity& entity, State& state, Target const& target, uint32_t now)
{
    eCEntity* instance = entity.GetInstance();
    if (!instance) return;
    if (target.motion && *target.motion && !strstr(target.motion, "_Move_") && ReplayKind(target.motion) == 0) ++combatTargets[state.puppet ? 1 : 0];
    ReleaseFinished(instance, state);
    const bCString task = entity.Routine.GetCurrentTask();
    // Undo any task the AI picked in the same frame: a delay let combat AI start attacks.
    if (!state.pacified || (task.GetLength() && strcmp(task.GetText(), Idle) != 0)) {
        if (state.pacified) {
            ++state.repacified;
            strncpy_s(state.lastForeign, task.GetText(), _TRUNCATE);
        }
        Pacify(entity, state);
        ++pacifies;
        state.lastPacify = now;
        *state.playing = '\0';
    }
    if (state.checkAt && static_cast<int32_t>(now - state.checkAt) >= 0) {
        char shows[128];
        CurrentMotion(instance, shows, sizeof(shows));
        // QA: did the action really show (stage V2)?
        const bool shown = !strncmp(shows, state.started, strlen(state.started));
        if (shown) ++actionsShown, kindShown[ReplayKind(state.started)] += !state.startedByMove;
        else if (actionsStarted - actionsShown < 40)
            Logf("body: started %s, 150 ms later shows %s", state.started, shows);
        state.checkAt = 0;
    }
    const bCVector& at = instance->GetWorldPosition();
    const float current[3] = {at.GetX(), at.GetY(), at.GetZ()};
    const float error = Distance(current, target.position);
    // QA (glide): how the body travelled since the last Drive. Faster than 15 m/s is a jump (snap), not travel.
    const uint32_t dt = now - state.lastAt;
    if (state.lastAt && dt > 0 && dt < 500) {
        const float moved = Distance(current, state.lastPos), speed = moved * 1000.0f / dt;
        Glide& g = glide[state.puppet ? 1 : 0];
        if (moved > 150.0f && speed > 1500.0f) ++g.snaps;
        else if (speed > 100.0f) {
            g.movingMs += dt;
            (state.steered ? g.steerMs : g.placeMs) += dt;
            char shows[128];
            CurrentMotion(instance, shows, sizeof(shows));
            if (!strstr(shows, "_Move_")) {
                g.glideMs += dt;
                if (!state.steered) g.glidePushMs += dt;
                else if (state.unsteadyAt && now - state.unsteadyAt < OvershootFor) g.glideOvershootMs += dt;
                else g.glideOtherMs += dt;
                auto& seen = glideMotions[state.puppet ? 1 : 0];
                if (auto it = seen.find(shows); it != seen.end()) it->second += dt;
                else if (seen.size() < 200) seen[shows] = dt;
            }
        }
    }
    // Smoothness (S3): per driven frame while the sender moves. Since 28.10. by its travel over at least 200 ms of its states
    // (smoothWindow): a sender stuck at an obstacle or restarting its walk jitters a few cm between close states, which
    // counted as moving while its body rightly stood (stageWalk: 36-99 % "still" in the turn back). QA smoothwindowoff.
    const bool senderMoves = smoothWindow ? TravelSpeed(state.track) > 100.0f : target.speed > 100.0f;
    if (state.lastAt && dt > 0 && dt < 500 && senderMoves) {
        Glide& g = glide[state.puppet ? 1 : 0];
        const float step = Distance(current, state.lastPos);
        ++g.frames;
        if (step > 30.0f) ++g.jumps;
        if (step > g.maxStep) g.maxStep = step;
        if (step < 0.5f) {
            ++g.stillFrames;
            if (stillMotions[state.puppet ? 1 : 0].size() < 200 || stillMotions[state.puppet ? 1 : 0].count(target.motion ? target.motion : "-"))
                ++stillMotions[state.puppet ? 1 : 0][target.motion && *target.motion ? target.motion : "-"];
            ++g.stillBy[state.lastBranch & 3];
            ++g.stillCase[state.lastCase < 5 ? state.lastCase : 0];
        }
        if (state.track.count) g.lagSum += Distance(current, state.track.snaps[state.track.count - 1].position);
    }
    std::memcpy(state.lastPos, current, sizeof(current));
    state.lastAt = now;
    std::memcpy(state.shownPos[state.shownNext], current, sizeof(current));
    state.shownAt[state.shownNext] = now;
    state.shownNext = (state.shownNext + 1) % 16;
    state.lastCase = state.track.lastCase;
    state.track.lastCase = 0;
    const bCMatrix pose = Pose(*instance, target.position, target.rotation);
    // Movement motions (Move_*) belong to the engine's movement system and only play while a body really
    // travels. A moving body is therefore steered through its movement controller with the sender's
    // movement mode; MoveTo only snaps large errors (teleport, lag) and the final stance.
    const gECharMovementMode mode = static_cast<gECharMovementMode>(target.movementMode);
    // An attack, parade or hit moves the body a little (lunges, steps back); steering it as walking showed a walk
    // instead of the attack. Action motions play in place, the pose follows.
    // A short fall (Fall_Loop, a step down while running) is travel, not an action (STILLMOTION 28.10.: the puppet stood while
    // its sender fell on, 2-30 still frames a stageWalk run). Tried 28.10. (QA fallmoveon): not fewer, off by default.
    const bool falling = fallMove && target.motion && strstr(target.motion, "_Fall_");
    const bool action = target.motion && *target.motion && !strstr(target.motion, "_Move_") && !strstr(target.motion, "_Ambient_") && !falling;
    const bool moving = !action && ((mode >= gECharMovementMode_Sneak && mode <= gECharMovementMode_Sprint) || falling) &&
                        (target.speed > 40.0f || error > 60.0f);
    // Stufe 3.2b: a puppet with its lead that ran past where its player stopped walks back instead of being pushed there.
    const bool actingNow = static_cast<int32_t>(state.actionUntil - now) > 0;
    const bool catchUp = target.smooth && !action && !actingNow && error >= DeadZone && error <= target.snapAt;
    // Swimming is no mode the movement controller takes for a driven body (steered with mode 7 it stayed in mode 1, 26.10.):
    // the body is placed and showed the swim idle while its player made strokes (stageSwim). The player's stroke
    // (Hero_Swim_..._Move_*) plays here like a loop instead, and the pose follows closely.
    const bool swimStroke = swimStrokes && mode == gECharMovementMode_Swim && target.motion && strstr(target.motion, "_Swim_") &&
                            strstr(target.motion, "_Move_");
    if (error > target.snapAt) {
        ++glide[state.puppet ? 1 : 0].hardSnaps;
        entity.CharacterMovement.StopMovement();
        entity.MoveTo(pose);
        state.walking = false;
        state.steered = false;
        state.lastBranch = 1;
    } else if (moving || catchUp || (!action && state.walking && error > 60.0f && !(target.smooth && error < DeadZone))) {
        // Up from a knock-down and off at once: a body left in its sitting state cannot walk and froze on its get-up
        // motion (19.10.: most of the raid's frozen mirrors). Take the stance here too, as below when standing.
        if (stanceSync && static_cast<int32_t>(state.actionUntil - now) <= 0 && target.aniState >= 0 &&
            static_cast<int>(entity.Routine.AniState) != target.aniState)
            entity.Routine.AniState = static_cast<gEAniState>(target.aniState);
        // A stance motion replayed while the player stood (an idle loop, a gesture: StartPlayAni or a play-ani instruction)
        // kept slot 0 when it walked off: the body travelled but showed it standing (stageGlide 23.10.: 13-16 % of the
        // travel under G3_NETSIM, nearly all of it steered). End it as the body sets off, and again while it travels.
        if (settleStance && (!state.walking || static_cast<int32_t>(now - state.settleAt) >= 0)) {
            state.settleAt = now + 250;
            char shows[128];
            CurrentMotion(instance, shows, sizeof(shows));
            if (*shows && !strstr(shows, "_Move_")) {
                // Stopping the replay alone left a looping idle on slot 0 now and then (host trace 23.10. 18:11: 1.4 s steered in
                // Ambient_Loop); stop the motion on the slot too, blended, so movement takes it next frame. (Handing the slot to
                // owner 2 instead made it worse: 85-98 % glided, run 20261023-182342.)
                entity.StopPlayAni();
                if (gCScriptRoutine_PS* routine = RoutineOf(instance)) routine->AIStopPlayAnimation();  // only a running play-ani
                if (auto* animation = static_cast<eCVisualAnimation_PS*>(instance->GetPropertySet(eEPropertySetType_Animation))) {
                    const auto slot0 = static_cast<eCWrapper_emfx2Actor::eEMotionType>(0);
                    ++stopOwners[static_cast<unsigned>(animation->GetMotionOwner(slot0)) & 7];
                    if (stopMotion) animation->StopMotion(slot0, 0.2f);
                }
                ++stanceStops;
            }
        }
        entity.CharacterMovement.SetMovementMode(moving ? (falling ? gECharMovementMode_Run : mode) : gECharMovementMode_Walk);
        float const* goal = target.goal ? target.goal : target.position;
        entity.CharacterMovement.SetMovementGoal(bCVector(goal[0], goal[1], goal[2]));
        state.walking = true;
        state.steered = true;
        state.lastBranch = 0;
        *state.playing = '\0';
    } else {
        state.steered = false;
        if (state.walking) {
            entity.CharacterMovement.StopMovement();
            state.walking = false;
        }
        // Setting the pose every frame restarted the body's motion and cut attacks short; while an action plays the
        // pose is only corrected when it drifted.
        const bool acting = static_cast<int32_t>(state.actionUntil - now) > 0;
        if (target.smooth && !acting && error < DeadZone) {
            // Dead zone: turned as its player, but not pushed the last few centimetres (a push shows as gliding)
            const bCVector& here = instance->GetWorldPosition();
            const float stay[3] = {here.GetX(), here.GetY(), here.GetZ()};
            entity.MoveTo(Pose(*instance, stay, target.rotation));
            state.lastBranch = 3;
        } else if (!acting || error > (swimStroke ? 40.0f : 100.0f)) {
            if (acting) ++glide[state.puppet ? 1 : 0].actionSnaps;
            entity.MoveTo(pose), state.lastBranch = 2;
        } else {
            state.lastBranch = 3;
        }
        // A motion of another stance (lying knocked out: AniState 16) did not play while the body stayed in its
        // standing state (stage A: the client fell, its puppet on the host kept standing); take the stance first.
        // Not during an action: an attack changes the body's stance by itself, and setting it back started that
        // stance's standing loop, which cut the attack within 150 ms (stage V2: 44 of 919 attacks showed).
        if (stanceSync && !acting && target.aniState >= 0 && static_cast<int>(entity.Routine.AniState) != target.aniState) {
            entity.Routine.AniState = static_cast<gEAniState>(target.aniState);
            *state.playing = '\0';
        }
        // A loop (frying, sweeping, thinking) keeps its name on the owner for as long as it runs there, but a replay
        // plays it once: the mirror froze after the first round (19.10.: Gorn at the fire, 19 of 80 looks with
        // nothing running). Start it again once it no longer runs. A stance idle (_Ambient_) only when nothing runs:
        // standing, the body plays its own idle variants, but sitting or lying it froze as well (Gorn with fruit).
        // An animal shown for a transformed player plays its idles through its own AI, which a driven body does not run
        // (transformdiag: a wolf puppet stood frozen in 5 of 12 looks); its idles (_Ambient_Ambient) restart as well.
        if (loopRestart && target.motion && (strstr(target.motion, "_Loop_") || strstr(target.motion, "_Ambient_") || swimStroke) &&
            (!strstr(target.motion, "_Move_") || swimStroke) &&
            !strcmp(target.motion, state.playing) && static_cast<int32_t>(now - state.loopCheckAt) >= 0) {
            const bool idle = strstr(target.motion, "_Ambient_") != nullptr;
            state.loopCheckAt = now + (idle ? 1000 : 300);
            if (!idle) state.actionUntil = now + 1200;  // still in the loop: the pose every frame would cut it (see below)
            char shows[128];
            CurrentMotion(instance, shows, sizeof(shows));
            if (!SlotRunning(instance) || (!idle && strcmp(shows, target.motion) != 0)) {
                *state.playing = '\0';
                // An idle restarted with StartPlayAni did not run either (holding a bowl, sitting with meat: 778 restarts,
                // still frozen), and restarting it every 300 ms kept a standing mirror turning on the spot.
                state.idleAsScriptAni = idle;
                ++loopRestarts;
            }
        }
        // Protocol 58: a new action number with the same motion name is a new start of it (a second attack alike), shown only
        // with actionSeq (counted either way).
        if (target.actionSeq >= 0) {
            if (state.actionSeq >= 0 && target.actionSeq != state.actionSeq && target.motion && *target.motion && !strcmp(target.motion, state.playing)) {
                ++sameNameStarts;
                if (actionSeq) {
                    *state.playing = '\0';
                    ++sameNameReplayed;
                }
            }
            state.actionSeq = target.actionSeq;
        }
        // Stance and action motions exist in the game; StartPlayAni takes the name without extension.
        if (target.motion && *target.motion && (!strstr(target.motion, "_Move_") || swimStroke) && strcmp(target.motion, state.playing) != 0) {
            if (swimStroke) ++swimStrokesPlayed;
            char name[96];
            strncpy_s(name, target.motion, _TRUNCATE);
            if (char* extension = strstr(name, ".xmot")) *extension = '\0';
            const bool combatMove = PlayCombatMove(instance, state, name, now);
            const bool scriptAni = !combatMove && (!strstr(name, "_Ambient_") || state.idleAsScriptAni) && PlayScriptAni(instance, state, name);
            state.idleAsScriptAni = false;
            if (!combatMove && !scriptAni) entity.StartPlayAni(bCString(name), 0, GEFalse, 1.0f, GEFalse);
            if (!strstr(name, "_Ambient_")) {
                // Did StartPlayAni play it at all? It returns silently when the motion file is not found. (A combat
                // move starts its motion on the body's next frame, so it never shows here.)
                char now0[128];
                CurrentMotion(instance, now0, sizeof(now0));
                const int kind = ReplayKind(name);
                if (!combatMove) ++kindStarted[kind];
                if (!strncmp(now0, name, strlen(name))) ++actionsPlayed, kindPlayed[kind] += !combatMove;
                else if (actionsStarted - actionsPlayed < 20) Logf("body: StartPlayAni %s did not play, slot 0 shows %s", name, now0);
                if (motionOwner > 0) {
                    if (auto* animation = static_cast<eCVisualAnimation_PS*>(instance->GetPropertySet(eEPropertySetType_Animation)))
                        animation->SetMotionOwner(static_cast<eCWrapper_emfx2Actor::eEMotionType>(0),
                                                  static_cast<eCVisualAnimation_PS::eSMotionDesc::eEMotionOwner>(motionOwner));
                    state.combatOwned = true;
                }
                state.actionUntil = now + 1200;
                ++actionsStarted;
                state.checkAt = now + 150;
                strncpy_s(state.started, name, _TRUNCATE);
                state.startedByMove = combatMove;
            }
            strncpy_s(state.playing, target.motion, _TRUNCATE);
        }
        FollowSender(instance, state, target, now);
    }
}
}
