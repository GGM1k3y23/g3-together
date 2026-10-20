// SPDX-License-Identifier: GPL-3.0-or-later
#include "trace.h"

#include "log.h"
#include "npcs.h"
#include "remote.h"

#include <Engine.h>
#include <Game.h>
#include <Script.h>
#include <Windows.h>
#include <intrin.h>
#include <cstdio>
#include <share.h>
#include <cstring>

namespace g3t::trace {
namespace {
enum Mode { Off, Driven, All };
Mode mode = Off;
bool installed = false;
FILE* out = nullptr;
unsigned lines = 0;
constexpr unsigned MaxLines = 50000;  // one session; the file is rewritten at every start

// Prologues of this build (position-independent, complete instructions; checked before patching):
// Engine.dll eCVisualAnimation_PS::PlayMotion RVA 0x30860: push ebp; mov ebp,esp; and esp,-8 (6 bytes)
// Game.dll gCScriptRoutine_PS::SetCurrentTask RVA 0x66100: push esi; mov esi,ecx; mov eax,[esi]; mov edx,[eax+58] (8)
// Game.dll gCScriptRoutine_PS::AISetTask RVA 0x164350: mov eax,[esp+4]; mov eax,[eax] (6)
struct Site { char const* module; uint32_t rva; unsigned char expected[8]; size_t stolen; };
const Site PlayMotionSite{"Engine.dll", 0x30860, {0x55, 0x8b, 0xec, 0x83, 0xe4, 0xf8}, 6};
const Site SetCurrentTaskSite{"Game.dll", 0x66100, {0x56, 0x8b, 0xf1, 0x8b, 0x06, 0x8b, 0x50, 0x58}, 8};
const Site AISetTaskSite{"Game.dll", 0x164350, {0x8b, 0x44, 0x24, 0x04, 0x8b, 0x00}, 6};

unsigned char* Locate(Site const& site)
{
    HMODULE module = GetModuleHandleA(site.module);
    if (!module) return nullptr;
    auto* code = reinterpret_cast<unsigned char*>(module) + site.rva;
    return std::memcmp(code, site.expected, site.stolen) == 0 ? code : nullptr;
}

void* JumpTo(unsigned char* target, size_t stolen, void* detour)
{
    auto* trampoline = static_cast<unsigned char*>(VirtualAlloc(nullptr, 32, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE));
    if (!trampoline) return nullptr;
    std::memcpy(trampoline, target, stolen);
    trampoline[stolen] = 0xE9;
    const int32_t back = static_cast<int32_t>(reinterpret_cast<intptr_t>(target + stolen) - reinterpret_cast<intptr_t>(trampoline + stolen + 5));
    std::memcpy(trampoline + stolen + 1, &back, 4);
    DWORD old = 0;
    if (!VirtualProtect(target, stolen, PAGE_EXECUTE_READWRITE, &old)) return nullptr;
    target[0] = 0xE9;
    const int32_t to = static_cast<int32_t>(reinterpret_cast<intptr_t>(detour) - reinterpret_cast<intptr_t>(target + 5));
    std::memcpy(target + 1, &to, 4);
    for (size_t i = 5; i < stolen; ++i) target[i] = 0x90;
    VirtualProtect(target, stolen, old, &old);
    FlushInstructionCache(GetCurrentProcess(), target, stolen);
    return trampoline;
}

bool Wanted(eCEntity* entity)
{
    if (mode == Off || !entity || !out || lines >= MaxLines) return false;
    return mode == All || remote::IsPuppet(entity) || npcs::IsMirrored(entity);
}

// "Game.dll+0x16abb0" for a code address.
void Caller(void* address, char* text, size_t size)
{
    HMODULE module = nullptr;
    char name[MAX_PATH] = "?";
    if (GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                           static_cast<LPCSTR>(address), &module) && module) {
        GetModuleFileNameA(module, name, MAX_PATH);
        char const* base = std::strrchr(name, '\\');
        sprintf_s(text, size, "%s+0x%x", base ? base + 1 : name,
                  static_cast<unsigned>(reinterpret_cast<uintptr_t>(address) - reinterpret_cast<uintptr_t>(module)));
    } else {
        sprintf_s(text, size, "%p", address);
    }
}

void Write(char const* kind, eCEntity* entity, char const* what, void* caller)
{
    char from[MAX_PATH + 16];
    Caller(caller, from, sizeof(from));
    Entity e(entity);
    fprintf(out, "%10lu %s %s%s %s by %s\n", GetTickCount(), kind, e.GetName().GetText(),
            remote::IsPuppet(entity) ? "(puppet)" : npcs::IsMirrored(entity) ? "(mirror)" : "", what, from);
    if (++lines % 50 == 0) fflush(out);
}

using MotionDesc = eCWrapper_emfx2Motion::eSMotionDesc;
using PlayMotionFn = void(__fastcall*)(eCVisualAnimation_PS*, void*, int, MotionDesc*);
PlayMotionFn playMotion = nullptr;
// The play options (eCWrapper_emfx2Motion::eSMotionDesc) are opaque in the SDK; the motion itself is the one set in
// the slot of this type, read after the call.
char const* MotionName(eCVisualAnimation_PS* self, int type)
{
    __try {
        return self->GetMotionDesc(static_cast<eCWrapper_emfx2Actor::eEMotionType>(type)).GetMotionFilename().GetText();
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return "?";
    }
}
void __fastcall PlayMotionDetour(eCVisualAnimation_PS* self, void* edx, int type, MotionDesc* desc)
{
    void* caller = _ReturnAddress();
    playMotion(self, edx, type, desc);
    eCEntity* entity = self ? self->GetEntity() : nullptr;
    if (!Wanted(entity)) return;
    char what[200];
    sprintf_s(what, "type=%d motion=%s", type, MotionName(self, type));
    Write("anim", entity, what, caller);
}

using SetTaskFn = void(__fastcall*)(gCScriptRoutine_PS*, void*, bCString const&);
SetTaskFn setCurrentTask = nullptr, aiSetTask = nullptr;
void TaskLine(char const* kind, gCScriptRoutine_PS* self, bCString const& task, void* caller)
{
    eCEntity* entity = self ? self->GetEntity() : nullptr;
    if (!Wanted(entity)) return;
    char what[160];
    sprintf_s(what, "task=%s", task.GetText());
    Write(kind, entity, what, caller);
}
void __fastcall SetCurrentTaskDetour(gCScriptRoutine_PS* self, void* edx, bCString const& task)
{
    TaskLine("task", self, task, _ReturnAddress());
    setCurrentTask(self, edx, task);
}
void __fastcall AISetTaskDetour(gCScriptRoutine_PS* self, void* edx, bCString const& task)
{
    TaskLine("aitask", self, task, _ReturnAddress());
    aiSetTask(self, edx, task);
}

void Install()
{
    if (installed) return;
    installed = true;
    if (unsigned char* code = Locate(PlayMotionSite)) playMotion = reinterpret_cast<PlayMotionFn>(JumpTo(code, PlayMotionSite.stolen, reinterpret_cast<void*>(&PlayMotionDetour)));
    if (unsigned char* code = Locate(SetCurrentTaskSite)) setCurrentTask = reinterpret_cast<SetTaskFn>(JumpTo(code, SetCurrentTaskSite.stolen, reinterpret_cast<void*>(&SetCurrentTaskDetour)));
    if (unsigned char* code = Locate(AISetTaskSite)) aiSetTask = reinterpret_cast<SetTaskFn>(JumpTo(code, AISetTaskSite.stolen, reinterpret_cast<void*>(&AISetTaskDetour)));
    Logf("trace: hooks play_motion=%d set_current_task=%d ai_set_task=%d", playMotion != nullptr, setCurrentTask != nullptr, aiSetTask != nullptr);
}
}

void Configure(char const* text)
{
    const Mode wanted = !_stricmp(text, "all") ? All : (!_stricmp(text, "on") || !_stricmp(text, "driven")) ? Driven : Off;
    if (wanted != Off) {
        if (!out) {
            char path[MAX_PATH];
            GetModuleFileNameA(nullptr, path, MAX_PATH);
            if (char* slash = std::strrchr(path, '\\')) strcpy_s(slash + 1, MAX_PATH - (slash + 1 - path), "g3trace.log");
            out = _fsopen(path, "w", _SH_DENYNO);  // readable while the game runs (logs, QA)
            lines = 0;
        }
        Install();
    }
    mode = wanted;
    if (out) fflush(out);
}

void Command(char const* text, LogFn log)
{
    if (text && *text) Configure(text);  // no argument: status only
    char line[160];
    sprintf_s(line, "TRACE mode=%s lines=%u hooks=%d%d%d", mode == All ? "all" : mode == Driven ? "driven" : "off", lines,
              playMotion != nullptr, setCurrentTask != nullptr, aiSetTask != nullptr);
    log(line);
}
}
