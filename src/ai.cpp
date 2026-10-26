// SPDX-License-Identifier: GPL-3.0-or-later
#include "ai.h"

#include "log.h"
#include "npcs.h"
#include "remote.h"
#include "talk.h"
#include "items.h"
#include "downed.h"
#include "guards.h"
#include "fightmem.h"
#include "infocmd.h"
#include "ransack.h"

#include <Game.h>
#include <Script.h>
#include <Windows.h>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <unordered_map>

namespace g3t::ai {
namespace {
char const* const Name = "CanReceivePerceptionFrom";
bool installed = false;
bool searched = false;
DWORD lastTry = 0;
unsigned blocked = 0;
gFScript originalScript = nullptr;
gFScriptAICallback originalCallback = nullptr;
// The game's script ContinueRoutine (Script_Game.dll 0x100a5ea0) ends a reaction (stumble, slide) and hands the body
// to SelectRoutineTask (0x100a9440): for a puppet that set ZS_HangAround every frame for ~3 s, which our driver undid
// every frame, and the puppet turned on the spot (stage2 red 2 of 20; trace with call chains and scriptfind, 20.10.).
// A player's puppet has no routine: its life is the player's.
gFScript originalContinue = nullptr;
bool continueBlock = true;  // QA negative control: ContinueRoutineOn
unsigned continueBlocked = 0, continueRun = 0;
// The script GetCombatMoveLength (Script_Game.dll 0xaa270), called by name from sAICombatMoveStart with other unset,
// binds the SPU's other (the fight's saved target) into a wrapper before answering from self's species and the action
// alone. A stale target crashed AttachTo there (Script.dll 0x13ea3, ~1 s after an orc died in the raid; 20.10.
// 22:56, re/findings/attachto-guard-c.md). Hand it an empty wrapper so it never touches that target.
gFScript originalMoveLength = nullptr;
bool moveLengthSearched = false;
bool moveLengthGuard = true;  // QA negative control: movelengthoff
unsigned moveLengthCalls = 0, moveLengthSanitized = 0, moveLengthUnknown = 0;
bool statesWrapped = false;
unsigned skipped = 0;
// QA aicount: how often AI states or functions whose name starts with the prefix ran, by the local hero and by anyone else.
std::string countPrefix;
std::unordered_map<std::string, std::pair<unsigned, unsigned>> counted;
unsigned unknown = 0;  // calls whose stack top named no wrapped state: must stay 0
unsigned wrapped = 0;
std::unordered_map<std::string, gFScriptAIState> originalStates;
std::unordered_map<std::string, gFScriptAIFunction> originalFunctions;
char const* const IdleTaskName = "ZS_HangAround_DoNothing";

// The SDK's gCScriptAdminExt::GetScript* call bTValMap::GetAt(key), which dereferences null for a missing
// name (it crashed the first lookup of an unregistered name). Look up safely instead.
template <typename T>
T* Find(bTValMap<bCString, T*>& map, char const* name)
{
    T* found = nullptr;
    return map.GetAt(bCString(name), found) ? found : nullptr;
}

bool Driven(eCEntity* entity)
{
    return entity && (remote::IsPuppet(entity) || npcs::IsMirrored(entity) || talk::IsHeld(entity));
}

eCEntity* Self(gCScriptProcessingUnit* spu, GELPVoid self)
{
    if (self) return static_cast<Entity*>(self)->GetInstance();
    return spu ? spu->GetSelfEntity() : nullptr;
}

GEInt GE_STDCALL FilterScript(gCScriptProcessingUnit* spu, GELPVoid self, GELPVoid other, GEInt args)
{
    if (Driven(Self(spu, self))) { ++blocked; return 0; }
    return originalScript(spu, self, other, args);
}

// One wrapper for every AI state and AI function: the entry on top of the run-time stack names the one
// being run. Driven bodies run nothing except the idle task; everyone else runs the game's original.
template <typename Fn>
GEBool Run(std::unordered_map<std::string, Fn> const& originals, bTObjStack<gScriptRunTimeSingleState>& stack,
           gCScriptProcessingUnit* spu)
{
    char const* name = stack.GetCount() ? stack.Peek().m_strScriptName.GetText() : "";
    eCEntity* self = spu ? spu->GetSelfEntity() : nullptr;
    // A mirrored NPC runs its death state only once the host reported it dead (npcs.cpp sets it then);
    // the client's own hit judgement must not decide that.
    const bool dying = (strncmp(name, "ZS_Dead", 7) == 0 || strncmp(name, "ZS_RagDollDead", 14) == 0) && npcs::DiedOnHost(self);
    // A mirrored NPC in a dialog with this client's hero runs its talk states (facing, gestures).
    const bool talking = (strcmp(name, "ZS_Talk") == 0 || strcmp(name, "ZS_Talk_Loop") == 0) && talk::TalkingWithHero(self);
    // The idle task of a mirror plays its standing motions every frame and replaced every attack the owner sent
    // (first human test: the client saw 11 attack motions where the host played 147); it waits while one plays.
    const bool acting = strcmp(name, IdleTaskName) == 0 && (npcs::InAction(self) || talk::InGesture(self));
    // Tried on Fable's advice (2026-10-18), no effect on attack motions: returning true here (E3) and running no idle
    // task at all for mirrors (E2). The stance's ambient loop is re-selected outside the AI states.
    if ((Driven(self) && strcmp(name, IdleTaskName) != 0 && !dying && !talking) || acting) {
        ++skipped;
        return GEFalse;  // nothing ran; our driver puts the idle task back
    }
    auto it = originals.find(name);
    if (it == originals.end()) { ++unknown; return GEFalse; }
    if (!countPrefix.empty() && !std::strncmp(name, countPrefix.c_str(), countPrefix.size())) {
        auto& c = counted[name];
        ++(self && self == gCSession::GetInstance().GetHero() ? c.first : c.second);
    }
    if (downed::RedirectDeath(self, name)) return GEFalse;  // the local hero is knocked out instead of dying
    if (strcmp(name, "IS_Running_ProcessInfo") == 0) infocmd::Observe(spu);  // a dialog script line runs
    if (strcmp(name, "ZS_RansackBody") == 0) ransack::OnRobbing(spu);         // one of our NPCs robs a body (a puppet?)
    if (self && self == gCSession::GetInstance().GetHero() && strcmp(name, "_AI_TakeItem") == 0) {
        // The world item this hero takes vanishes into a stack (its GUID is gone afterwards): note it first.
        items::BeforeTake(spu);
        const GEBool result = it->second(stack, spu);
        items::AfterTake(spu);
        return result;
    }
    // A guard's reaction to another player follows that player; an attack on a puppet is a fight with its player (fightmem.cpp).
    eCEntity* actor = guards::StateActor(self, name);
    eCEntity* fighter = actor ? nullptr : fightmem::StateActor(self, name);
    guards::Acting player(actor ? actor : fighter);
    const GEBool result = it->second(stack, spu);
    if (fighter) fightmem::AfterState(self);
    return result;
}

GEBool GE_STDCALL FilterState(bTObjStack<gScriptRunTimeSingleState>& stack, gCScriptProcessingUnit* spu)
{
    return Run(originalStates, stack, spu);
}

GEBool GE_STDCALL FilterFunction(bTObjStack<gScriptRunTimeSingleState>& stack, gCScriptProcessingUnit* spu)
{
    return Run(originalFunctions, stack, spu);
}

void WrapStates(gCScriptAdminExt& admin)
{
    // Walk the admin's own name -> entry maps (its per-DLL arrays held a null entry in this build and
    // crashed the first attempt); swap each entry's function pointer for the filter.
    GE_MAP_FOR_EACH (name, state, admin.GetScriptAIStates())
    {
        if (!state || state->m_funcScriptAIState == &FilterState || !state->m_funcScriptAIState) continue;
        originalStates[name.GetText()] = state->m_funcScriptAIState;
        state->m_funcScriptAIState = &FilterState;
        ++wrapped;
    }
    GE_MAP_FOR_EACH (name, function, admin.GetScriptAIFunctions())
    {
        if (!function || function->m_funcScriptAIFunction == &FilterFunction || !function->m_funcScriptAIFunction) continue;
        originalFunctions[name.GetText()] = function->m_funcScriptAIFunction;
        function->m_funcScriptAIFunction = &FilterFunction;
        ++wrapped;
    }
    statesWrapped = true;
    Logf("ai: %u AI states and functions wrapped for driven bodies", wrapped);
}

// NPCs choose targets through the script GetAttitude, called by name from the perception scanner and the combat
// retarget (Script_Game.dll 0x92F0, 0xA070; re/findings/target-selection.md). The player is special only by identity
// (Entity::GetPlayer), so a puppet was an ordinary human NPC to them: attitude 0, never a target (first human test).
// Here a puppet counts as the local hero: whoever is hostile to the hero is hostile to the puppet.
gFScript originalAttitude = nullptr;
bool attitudeInstalled = false;
bool attitudeOn = true;  // QA negative control: attitudeoff
bool fleeKeep = true;    // QA negative control: fleekeepoff
unsigned attitudeAsked = 0, attitudeRedirected = 0, attitudeFleeKept = 0;

bool attitudeNative = false;

// Script_Game.dll of this build: GetAttitude at RVA 0xE3B0 starts with push ebp; mov ebp,esp; and esp,-8 (6 bytes
// of complete, position-independent instructions), then sub esp,0x354.
unsigned char* NativeAttitude()
{
    static const unsigned char expected[] = {0x55, 0x8b, 0xec, 0x83, 0xe4, 0xf8, 0x81, 0xec, 0x54, 0x03, 0x00, 0x00};
    HMODULE module = GetModuleHandleA("Script_Game.dll");
    if (!module) return nullptr;
    auto* code = reinterpret_cast<unsigned char*>(module) + 0xE3B0;
    return std::memcmp(code, expected, sizeof(expected)) == 0 ? code : nullptr;
}

}

// Points one import of `module` (by DLL and function name) at `replacement`; returns what it pointed at.
void* PatchImport(void* module, char const* dll, char const* function, void* replacement)
{
    auto* base = static_cast<unsigned char*>(module);
    auto* dos = reinterpret_cast<IMAGE_DOS_HEADER*>(base);
    auto* nt = reinterpret_cast<IMAGE_NT_HEADERS*>(base + dos->e_lfanew);
    const IMAGE_DATA_DIRECTORY& dir = nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    for (auto* imp = reinterpret_cast<IMAGE_IMPORT_DESCRIPTOR*>(base + dir.VirtualAddress); imp->Name; ++imp) {
        if (_stricmp(reinterpret_cast<char const*>(base + imp->Name), dll)) continue;
        auto* names = reinterpret_cast<IMAGE_THUNK_DATA*>(base + (imp->OriginalFirstThunk ? imp->OriginalFirstThunk : imp->FirstThunk));
        auto* slots = reinterpret_cast<IMAGE_THUNK_DATA*>(base + imp->FirstThunk);
        for (; names->u1.AddressOfData; ++names, ++slots) {
            if (IMAGE_SNAP_BY_ORDINAL(names->u1.Ordinal)) continue;
            auto* byName = reinterpret_cast<IMAGE_IMPORT_BY_NAME*>(base + names->u1.AddressOfData);
            if (std::strcmp(reinterpret_cast<char const*>(byName->Name), function)) continue;
            DWORD old = 0;
            if (!VirtualProtect(&slots->u1.Function, sizeof(slots->u1.Function), PAGE_READWRITE, &old)) return nullptr;
            void* previous = reinterpret_cast<void*>(slots->u1.Function);
            slots->u1.Function = reinterpret_cast<DWORD>(replacement);
            VirtualProtect(&slots->u1.Function, sizeof(slots->u1.Function), old, &old);
            return previous;
        }
    }
    return nullptr;
}

// Jump from `target` to `detour`; the returned trampoline runs the 6 stolen bytes and continues in the original.
void* JumpTo(unsigned char* target, void* detour)
{
    constexpr size_t Stolen = 6;
    auto* trampoline = static_cast<unsigned char*>(VirtualAlloc(nullptr, 32, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE));
    if (!trampoline) return nullptr;
    std::memcpy(trampoline, target, Stolen);
    trampoline[Stolen] = 0xE9;
    const int32_t back = static_cast<int32_t>(reinterpret_cast<intptr_t>(target + Stolen) - reinterpret_cast<intptr_t>(trampoline + Stolen + 5));
    std::memcpy(trampoline + Stolen + 1, &back, 4);
    DWORD old = 0;
    if (!VirtualProtect(target, Stolen, PAGE_EXECUTE_READWRITE, &old)) return nullptr;
    target[0] = 0xE9;
    const int32_t to = static_cast<int32_t>(reinterpret_cast<intptr_t>(detour) - reinterpret_cast<intptr_t>(target + 5));
    std::memcpy(target + 1, &to, 4);
    target[5] = 0x90;
    VirtualProtect(target, Stolen, old, &old);
    FlushInstructionCache(GetCurrentProcess(), target, Stolen);
    return trampoline;
}

namespace {

GEInt GE_STDCALL FilterAttitude(gCScriptProcessingUnit* spu, GELPVoid self, GELPVoid other, GEInt args)
{
    eCEntity* target = other ? static_cast<Entity*>(other)->GetInstance() : (spu ? spu->GetOtherEntity() : nullptr);
    if (target && remote::IsPuppet(target)) {
        ++attitudeAsked;
        eCEntity* judge = Self(spu, self);
        remote::NoteContact(judge, target);
        eCEntity* hero = gCSession::GetInstance().GetHero();
        if (attitudeOn && hero && judge && judge != hero && !Driven(judge)) {
            // GetAttitude answers Panic when self flees and its current target is other. With the hero in place of
            // the puppet that check fails, and an NPC fleeing from the puppet turned on it (re/findings/attitude-callers.md).
            Entity npc(judge);
            if (fleeKeep && judge->GetPropertySet(eEPropertySetType_NPC) && npc.Routine.AIMode == gEAIMode_Flee &&
                npc.NPC.GetCurrentTarget().GetInstance() == target) {
                ++attitudeFleeKept;
                return originalAttitude(spu, self, other, args);
            }
            ++attitudeRedirected;
            Entity asHero(hero);
            return originalAttitude(spu, self, &asHero, args);
        }
    }
    return originalAttitude(spu, self, other, args);
}

GEInt GE_STDCALL FilterContinue(gCScriptProcessingUnit* spu, GELPVoid self, GELPVoid other, GEInt args)
{
    if (continueBlock && remote::IsPuppet(Self(spu, self))) { ++continueBlocked; return 0; }
    ++continueRun;
    return originalContinue(spu, self, other, args);
}

GEInt GE_STDCALL SafeMoveLength(gCScriptProcessingUnit* spu, GELPVoid self, GELPVoid other, GEInt args)
{
    ++moveLengthCalls;
    // Other feeds no branch of the original; a valid empty wrapper takes its copy branch instead of GetOtherEntity.
    Entity noOther;
    if (moveLengthGuard) ++moveLengthSanitized;
    const GEInt length = originalMoveLength(spu, self, moveLengthGuard ? &noOther : other, args);
    if (length < 0) ++moveLengthUnknown;  // the original answers -1 for actions it has no length for
    return length;
}

GEBool GE_STDCALL FilterCallback(gCScriptProcessingUnit* spu)
{
    if (Driven(spu ? spu->GetSelfEntity() : nullptr)) { ++blocked; return GEFalse; }
    return originalCallback(spu);
}
}

bool PerceptionFiltered() { return installed; }
bool AttitudeInstalled() { return attitudeInstalled; }
bool AttitudeNative() { return attitudeNative; }
unsigned AttitudeAsked() { return attitudeAsked; }
unsigned AttitudeRedirected() { return attitudeRedirected; }
unsigned AttitudeFleeKept() { return attitudeFleeKept; }
void AttitudeOff(void (*log)(char const*))
{
    attitudeOn = false;
    log("ATTITUDEOFF puppets are judged as ordinary NPCs again");
}
void FleeKeepOff(void (*log)(char const*))
{
    fleeKeep = false;
    log("FLEEKEEPOFF an NPC fleeing from a puppet judges the hero instead");
}
unsigned BlockedCount() { return blocked; }
unsigned SkippedCount() { return skipped; }
unsigned WrappedCount() { return wrapped; }
unsigned UnknownCount() { return unknown; }
void ContinueRoutineReport(void (*log)(char const*))
{
    char line[160];
    sprintf_s(line, "CONTINUEROUTINE wrapped=%d block=%d blocked=%u run=%u", originalContinue != nullptr, continueBlock ? 1 : 0,
              continueBlocked, continueRun);
    log(line);
}
void ContinueRoutineOn(void (*log)(char const*))
{
    continueBlock = false;
    log("CONTINUEROUTINEON ok=1");
}
void MoveLengthReport(void (*log)(char const*))
{
    char line[160];
    sprintf_s(line, "MOVELENGTH wrapped=%d guard=%d calls=%u sanitized=%u unknown=%u", originalMoveLength != nullptr,
              moveLengthGuard ? 1 : 0, moveLengthCalls, moveLengthSanitized, moveLengthUnknown);
    log(line);
}
void MoveLengthOff(void (*log)(char const*))
{
    moveLengthGuard = false;
    log("MOVELENGTHOFF ok=1");
}

void Count(char const* names, void (*log)(char const*))
{
    // A name prefix: count those from now on (and start again); empty: show the counts.
    if (names && *names) {
        counted.clear();
        countPrefix = names;
    }
    char line[200];
    for (auto const& entry : counted) {
        sprintf_s(line, "AICOUNT name=%s hero=%u others=%u", entry.first.c_str(), entry.second.first, entry.second.second);
        log(line);
    }
    log("AICOUNT end");
}

void FindByRva(char const* rvaHex, void (*log)(char const*))
{
    gCScriptAdminExt& admin = GetScriptAdminExt();
    HMODULE game = GetModuleHandleA("Script_Game.dll");
    const uintptr_t want = reinterpret_cast<uintptr_t>(game) + std::strtoul(rvaHex, nullptr, 16);
    char line[200];
    unsigned found = 0;
    auto check = [&](char const* table, char const* name, void const* fn) {
        if (reinterpret_cast<uintptr_t>(fn) != want) return;
        sprintf_s(line, "SCRIPTFIND rva=%s table=%s name=%s", rvaHex, table, name);
        log(line);
        ++found;
    };
    GE_MAP_FOR_EACH (name, script, admin.GetScripts())
        if (script) check("script", name.GetText(), reinterpret_cast<void const*>(script->m_funcScript));
    GE_MAP_FOR_EACH (name, callback, admin.GetScriptAICallbacks())
        if (callback) check("callback", name.GetText(), reinterpret_cast<void const*>(callback->m_funcScriptAICallback));
    GE_MAP_FOR_EACH (name, routine, admin.GetScriptAIRoutines())
        if (routine) check("routine", name.GetText(), reinterpret_cast<void const*>(routine->m_funcScriptAIRoutine));
    for (auto const& entry : originalStates) check("state", entry.first.c_str(), reinterpret_cast<void const*>(entry.second));
    for (auto const& entry : originalFunctions) check("function", entry.first.c_str(), reinterpret_cast<void const*>(entry.second));
    sprintf_s(line, "SCRIPTFIND end rva=%s found=%u", rvaHex, found);
    log(line);
}

void Describe(char const* name, void (*log)(char const*))
{
    gCScriptAdminExt& admin = GetScriptAdminExt();
    char line[200];
    sprintf_s(line, "SCRIPTINFO name=%s script=%d callback=%d state=%d function=%d routine=%d", name,
              Find(admin.GetScripts(), name) != nullptr, Find(admin.GetScriptAICallbacks(), name) != nullptr,
              Find(admin.GetScriptAIStates(), name) != nullptr, Find(admin.GetScriptAIFunctions(), name) != nullptr,
              Find(admin.GetScriptAIRoutines(), name) != nullptr);
    log(line);
    // AI states and functions: the original entry (ours is wrapped around it).
    void* target = nullptr;
    if (auto it = originalStates.find(name); it != originalStates.end()) target = reinterpret_cast<void*>(it->second);
    else if (auto it2 = originalFunctions.find(name); it2 != originalFunctions.end()) target = reinterpret_cast<void*>(it2->second);
    if (target) {
        HMODULE owner = nullptr;
        GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                           static_cast<LPCSTR>(target), &owner);
        char file[MAX_PATH] = {};
        if (owner) GetModuleFileNameA(owner, file, MAX_PATH);
        char const* base = strrchr(file, '\\');
        sprintf_s(line, "SCRIPTINFO address name=%s module=%s rva=%#lx", name, base ? base + 1 : file,
                  static_cast<unsigned long>(reinterpret_cast<uintptr_t>(target) - reinterpret_cast<uintptr_t>(owner)));
        log(line);
    }
    if (gSScript* script = Find(admin.GetScripts(), name)) {
        HMODULE owner = nullptr;
        GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                           reinterpret_cast<LPCSTR>(script->m_funcScript), &owner);
        char file[MAX_PATH] = {};
        if (owner) GetModuleFileNameA(owner, file, MAX_PATH);
        char const* base = strrchr(file, '\\');
        sprintf_s(line, "SCRIPTINFO address name=%s module=%s rva=%#lx", name, base ? base + 1 : file,
                  static_cast<unsigned long>(reinterpret_cast<uintptr_t>(script->m_funcScript) - reinterpret_cast<uintptr_t>(owner)));
        log(line);
    }
}

void Tick()
{
    if (!attitudeInstalled && gCSession::GetInstance().IsGameRunning()) {
        if (auto* script = Find(GetScriptAdminExt().GetScripts(), "GetAttitude")) {
            // The registered function is the native one 29 functions call directly (OnAttack, AssessTarget, damage
            // rules, ...); swapping the table entry only reaches calls by name. Jump from the function itself.
            if (unsigned char* native = NativeAttitude(); native && reinterpret_cast<void*>(script->m_funcScript) == native) {
                originalAttitude = reinterpret_cast<gFScript>(JumpTo(native, reinterpret_cast<void*>(&FilterAttitude)));
                attitudeNative = originalAttitude != nullptr;
            }
            if (!attitudeNative) {
                originalAttitude = script->m_funcScript;
                script->m_funcScript = &FilterAttitude;
            }
            attitudeInstalled = true;
            Logf("ai: GetAttitude wrapped (%s; puppets count as the local hero)", attitudeNative ? "native jump" : "script table only");
        }
    }
    if ((installed || searched) && statesWrapped && originalContinue && moveLengthSearched) return;
    const DWORD now = GetTickCount();
    if (now - lastTry < 1000) return;
    lastTry = now;
    gCScriptAdminExt& admin = GetScriptAdminExt();
    // The game's scripts are registered once the game runs; wrap them then (our DLL loads first).
    if (!statesWrapped && gCSession::GetInstance().IsGameRunning()) WrapStates(admin);
    if (!originalContinue && gCSession::GetInstance().IsGameRunning()) {
        if (auto* script = Find(admin.GetScripts(), "ContinueRoutine"); script && script->m_funcScript && script->m_funcScript != &FilterContinue) {
            originalContinue = script->m_funcScript;
            script->m_funcScript = &FilterContinue;
            Log("ai: ContinueRoutine wrapped (puppets keep no routine)");
        }
    }
    if (!moveLengthSearched && gCSession::GetInstance().IsGameRunning()) {
        moveLengthSearched = true;
        auto* script = Find(admin.GetScripts(), "GetCombatMoveLength");
        if (script && script->m_funcScript && script->m_funcScript != &SafeMoveLength) {
            originalMoveLength = script->m_funcScript;
            script->m_funcScript = &SafeMoveLength;
        }
        Log(originalMoveLength ? "ai: GetCombatMoveLength wrapped (fight target not bound)" : "ai: GetCombatMoveLength NOT found, not wrapped");
    }
    if (installed || searched) return;
    if (auto* script = Find(admin.GetScripts(), Name)) {
        originalScript = script->m_funcScript;
        script->m_funcScript = &FilterScript;
        installed = true;
        Log("ai: perception filter installed (script)");
    } else if (auto* callback = Find(admin.GetScriptAICallbacks(), Name)) {
        originalCallback = callback->m_funcScriptAICallback;
        callback->m_funcScriptAICallback = &FilterCallback;
        installed = true;
        Log("ai: perception filter installed (AI callback)");
    } else if (gCSession::GetInstance().IsGameRunning()) {
        // The game is running and the function is in neither table: stop looking, say so once.
        searched = true;
        Logf("ai: %s not found as script or AI callback; perception filter NOT installed", Name);
    }
}
}

namespace g3t::ai {
// QA diagnostic: every registered script, AI callback, state, function and routine whose name contains text.
void ListNames(char const* text, void (*log)(char const*))
{
    gCScriptAdminExt& admin = GetScriptAdminExt();
    char line[200];
    auto scan = [&](char const* kind, auto& map) {
        GE_MAP_FOR_EACH (name, entry, map)
        {
            if (!entry || !strstr(name.GetText(), text)) continue;
            sprintf_s(line, "NAME kind=%s name=%s", kind, name.GetText());
            log(line);
        }
    };
    scan("script", admin.GetScripts());
    scan("callback", admin.GetScriptAICallbacks());
    scan("state", admin.GetScriptAIStates());
    scan("function", admin.GetScriptAIFunctions());
    scan("routine", admin.GetScriptAIRoutines());
    log("NAMES end");
}

// QA, for the inventory of what the game can do: every registered script, AI callback, state, function and routine
// with the module and RVA of its function and the source file the game registered it from (m_strSource).
void DumpNames(char const* path, void (*log)(char const*))
{
    FILE* file = nullptr;
    if (fopen_s(&file, path, "wb") || !file) { log("SCRIPTDUMP refused cannot write the file"); return; }
    std::fputs("kind\tname\tmodule\trva\tsource\r\n", file);
    gCScriptAdminExt& admin = GetScriptAdminExt();
    int count = 0;
    auto dump = [&](char const* kind, auto& map, auto function) {
        GE_MAP_FOR_EACH (name, entry, map)
        {
            if (!entry) continue;
            void const* code = reinterpret_cast<void const*>(entry->*function);
            HMODULE module = nullptr;
            char moduleName[MAX_PATH] = "-";
            GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                               static_cast<LPCSTR>(code), &module);
            if (module) {
                GetModuleFileNameA(module, moduleName, sizeof(moduleName));
                if (char const* slash = std::strrchr(moduleName, '\\')) std::memmove(moduleName, slash + 1, std::strlen(slash));
            }
            const uintptr_t rva = module ? reinterpret_cast<uintptr_t>(code) - reinterpret_cast<uintptr_t>(module) : 0;
            std::fprintf(file, "%s\t%s\t%s\t%#x\t%s\r\n", kind, name.GetText(), moduleName, static_cast<unsigned>(rva),
                         entry->m_strSource.GetText());
            ++count;
        }
    };
    dump("script", admin.GetScripts(), &gSScript::m_funcScript);
    dump("callback", admin.GetScriptAICallbacks(), &gSScriptAICallback::m_funcScriptAICallback);
    dump("state", admin.GetScriptAIStates(), &gSScriptAIState::m_funcScriptAIState);
    dump("function", admin.GetScriptAIFunctions(), &gSScriptAIFunction::m_funcScriptAIFunction);
    dump("routine", admin.GetScriptAIRoutines(), &gSScriptAIRoutine::m_funcScriptAIRoutine);
    std::fclose(file);
    char line[120];
    sprintf_s(line, "SCRIPTDUMP count=%d", count);
    log(line);
}
}
