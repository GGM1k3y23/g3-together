// SPDX-License-Identifier: GPL-3.0-or-later
#include "body.h"

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
// Owner of motion slot 0 after a replayed action. Setting 5 (what the game's combat moves set) kept attacks from being
// replaced by the stance's idle loop, but froze bodies in their bind pose once the motion ended (owner's look after V2:
// ~80 % of the client's mirrors had nothing running). 0 = leave as StartPlayAni set it.
int motionOwner = 0;
bool combatReplay = true;  // QA comparison: combatreplayoff
unsigned kindStarted[3] = {}, kindPlayed[3] = {}, kindShown[3] = {};
unsigned scriptAnis = 0, scriptAniFaults = 0, loopRestarts = 0;
bool scriptAniReplay = true;  // QA comparison: scriptanioff
bool loopRestart = true;      // QA comparison: looprestartoff
Glide glide[2];
unsigned stanceStops = 0;   // replayed stance motions ended because the body set off
unsigned stopOwners[8] = {};  // QA: slot 0's owner when that happened
std::map<std::string, unsigned> glideMotions[2];  // what slot 0 showed while a body glided, ms by motion name

void GlideReport(void (*log)(char const*))
{
    char line[200];
    for (int kind = 0; kind < 2; ++kind) {
        Glide const& g = glide[kind];
        sprintf_s(line, "GLIDE kind=%s moving_ms=%u glide_ms=%u steer_ms=%u place_ms=%u snaps=%u stance_stops=%u owners=%u/%u/%u/%u/%u/%u/%u",
                  kind ? "puppet" : "mirror", g.movingMs, g.glideMs, g.steerMs, g.placeMs, g.snaps, stanceStops, stopOwners[0], stopOwners[1],
                  stopOwners[2], stopOwners[3], stopOwners[4], stopOwners[5], stopOwners[6]);
        log(line);
        // The five motions shown longest while gliding (a name without _Move_ is not always a still body: starts, stops).
        std::vector<std::pair<unsigned, std::string>> top;
        for (auto const& [name, ms] : glideMotions[kind]) top.emplace_back(ms, name);
        std::sort(top.rbegin(), top.rend());
        for (size_t i = 0; i < top.size() && i < 5; ++i) {
            sprintf_s(line, "GLIDEMOTION kind=%s ms=%u motion=%.120s", kind ? "puppet" : "mirror", top[i].first, top[i].second.c_str());
            log(line);
        }
    }
}

bool arrivalSpeed = false;  // QA negative control: glideold
bool settleStance = true;   // QA negative control: glideold
bool stopMotion = true;     // QA comparison: glidestopoff (only StopPlayAni on set-off)

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
    glideMotions[0].clear();
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

void Drive(Entity& entity, State& state, Target const& target, uint32_t now)
{
    eCEntity* instance = entity.GetInstance();
    if (!instance) return;
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
                auto& seen = glideMotions[state.puppet ? 1 : 0];
                if (auto it = seen.find(shows); it != seen.end()) it->second += dt;
                else if (seen.size() < 200) seen[shows] = dt;
            }
        }
    }
    std::memcpy(state.lastPos, current, sizeof(current));
    state.lastAt = now;
    const bCMatrix pose = Pose(*instance, target.position, target.rotation);
    // Movement motions (Move_*) belong to the engine's movement system and only play while a body really
    // travels. A moving body is therefore steered through its movement controller with the sender's
    // movement mode; MoveTo only snaps large errors (teleport, lag) and the final stance.
    const gECharMovementMode mode = static_cast<gECharMovementMode>(target.movementMode);
    // An attack, parade or hit moves the body a little (lunges, steps back); steering it as walking showed a walk
    // instead of the attack. Action motions play in place, the pose follows.
    const bool action = target.motion && *target.motion && !strstr(target.motion, "_Move_") && !strstr(target.motion, "_Ambient_");
    const bool moving = !action && mode >= gECharMovementMode_Sneak && mode <= gECharMovementMode_Sprint && (target.speed > 40.0f || error > 60.0f);
    if (error > 300.0f) {
        entity.CharacterMovement.StopMovement();
        entity.MoveTo(pose);
        state.walking = false;
        state.steered = false;
    } else if (moving || (!action && state.walking && error > 60.0f)) {
        // Up from a knock-down and off at once: a body left in its sitting state cannot walk and froze on its get-up
        // motion (19.10.: most of the raid's frozen mirrors). Take the stance here too, as below when standing.
        if (static_cast<int32_t>(state.actionUntil - now) <= 0 && target.aniState >= 0 &&
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
        entity.CharacterMovement.SetMovementMode(moving ? mode : gECharMovementMode_Walk);
        entity.CharacterMovement.SetMovementGoal(bCVector(target.position[0], target.position[1], target.position[2]));
        state.walking = true;
        state.steered = true;
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
        if (!acting || error > 100.0f) entity.MoveTo(pose);
        // A motion of another stance (lying knocked out: AniState 16) did not play while the body stayed in its
        // standing state (stage A: the client fell, its puppet on the host kept standing); take the stance first.
        // Not during an action: an attack changes the body's stance by itself, and setting it back started that
        // stance's standing loop, which cut the attack within 150 ms (stage V2: 44 of 919 attacks showed).
        if (!acting && target.aniState >= 0 && static_cast<int>(entity.Routine.AniState) != target.aniState) {
            entity.Routine.AniState = static_cast<gEAniState>(target.aniState);
            *state.playing = '\0';
        }
        // A loop (frying, sweeping, thinking) keeps its name on the owner for as long as it runs there, but a replay
        // plays it once: the mirror froze after the first round (19.10.: Gorn at the fire, 19 of 80 looks with
        // nothing running). Start it again once it no longer runs. A stance idle (_Ambient_) only when nothing runs:
        // standing, the body plays its own idle variants, but sitting or lying it froze as well (Gorn with fruit).
        // An animal shown for a transformed player plays its idles through its own AI, which a driven body does not run
        // (transformdiag: a wolf puppet stood frozen in 5 of 12 looks); its idles (_Ambient_Ambient) restart as well.
        if (loopRestart && target.motion && (strstr(target.motion, "_Loop_") || strstr(target.motion, "_Ambient_")) &&
            !strstr(target.motion, "_Move_") &&
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
        // Stance and action motions exist in the game; StartPlayAni takes the name without extension.
        if (target.motion && *target.motion && !strstr(target.motion, "_Move_") && strcmp(target.motion, state.playing) != 0) {
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
    }
}
}
