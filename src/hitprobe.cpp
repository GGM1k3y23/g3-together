// SPDX-License-Identifier: GPL-3.0-or-later
#include "hitprobe.h"

#include "log.h"

#include <Game.h>
#include <Script.h>
#include <Windows.h>
#include <cstdio>
#include <cstring>
#include <string>

namespace g3t::hitprobe {
namespace {
bool installed = false;
DWORD lastTry = 0;
gFScript original = nullptr;
unsigned calls = 0;

struct Module { char const* name; uintptr_t base = 0, codeStart = 0, codeEnd = 0; };
Module modules[] = {{"Game.dll"}, {"Script_Game.dll"}, {"Engine.dll"}, {"Script.dll"}, {"Gothic3.exe"}};

struct Path {
    std::string frames, victim, other;
    int delta = 0;
    unsigned count = 0;
};
constexpr int MaxPaths = 24;
Path paths[MaxPaths];
int pathCount = 0;

void FindCode(Module& m)
{
    HMODULE handle = GetModuleHandleA(m.name);
    if (!handle) return;
    m.base = reinterpret_cast<uintptr_t>(handle);
    auto const* dos = reinterpret_cast<IMAGE_DOS_HEADER const*>(handle);
    auto const* nt = reinterpret_cast<IMAGE_NT_HEADERS const*>(m.base + dos->e_lfanew);
    IMAGE_SECTION_HEADER const* section = IMAGE_FIRST_SECTION(nt);
    for (unsigned i = 0; i < nt->FileHeader.NumberOfSections; ++i, ++section) {
        if (!(section->Characteristics & IMAGE_SCN_MEM_EXECUTE)) continue;
        m.codeStart = m.base + section->VirtualAddress;
        m.codeEnd = m.codeStart + section->Misc.VirtualSize;
        return;
    }
}

// A return address points just past a call: E8 rel32 (5 bytes) or FF /2 (2, 3 or 6 bytes).
bool AfterCall(uintptr_t address)
{
    auto const* p = reinterpret_cast<unsigned char const*>(address);
    if (p[-5] == 0xE8) return true;
    for (int length : {2, 3, 6, 7})
        if (p[-length] == 0xFF && ((p[-length + 1] >> 3) & 7) == 2) return true;
    return false;
}

bool SafeAfterCall(uintptr_t address)
{
    __try {
        return AfterCall(address);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return false;
    }
}

// Frame pointers are often omitted in the game's modules, so walk the raw stack instead: every value that
// points just past a call instruction inside a known module's code is taken as a return address.
std::string StackPath()
{
    std::string out;
    int found = 0;
    volatile uintptr_t marker = 0;
    auto const* stack = reinterpret_cast<uintptr_t const*>(const_cast<uintptr_t*>(&marker));
    NT_TIB const* tib = reinterpret_cast<NT_TIB const*>(NtCurrentTeb());
    auto const* top = static_cast<uintptr_t const*>(tib->StackBase);
    for (int i = 0; i < 1024 && stack + i < top && found < 10; ++i) {
        const uintptr_t value = stack[i];
        for (Module const& m : modules) {
            if (!m.codeStart || value <= m.codeStart + 7 || value >= m.codeEnd) continue;
            if (!SafeAfterCall(value)) continue;
            char frame[48];
            sprintf_s(frame, "%s%s+%#lx", found ? " < " : "", m.name, static_cast<unsigned long>(value - m.base));
            out += frame;
            ++found;
        }
    }
    return out;
}

std::string NameOf(Entity const& entity) { return entity.GetInstance() ? entity.GetName().GetText() : "-"; }

GEInt GE_STDCALL Filter(gCScriptProcessingUnit* spu, GELPVoid self, GELPVoid other, GEInt args)
{
    ++calls;
    const std::string frames = StackPath();
    Path* path = nullptr;
    for (int i = 0; i < pathCount && !path; ++i)
        if (paths[i].frames == frames) path = &paths[i];
    if (!path && pathCount < MaxPaths) {
        path = &paths[pathCount++];
        path->frames = frames;
        Entity victim = self ? *static_cast<Entity*>(self) : Entity(spu ? spu->GetSelfEntity() : nullptr);
        Entity source = other ? *static_cast<Entity*>(other) : Entity(spu ? spu->GetOtherEntity() : nullptr);
        path->victim = NameOf(victim);
        path->other = NameOf(source);
        path->delta = args;
    }
    if (path) ++path->count;
    return original(spu, self, other, args);
}
}

void Tick()
{
    if (installed || !gCSession::GetInstance().IsGameRunning()) return;
    const DWORD now = GetTickCount();
    if (now - lastTry < 1000) return;
    lastTry = now;
    gSScript* found = nullptr;
    if (!GetScriptAdminExt().GetScripts().GetAt(bCString("AddHitPoints"), found) || !found) return;
    for (Module& m : modules) FindCode(m);
    original = found->m_funcScript;
    found->m_funcScript = &Filter;
    installed = true;
    Log("hitprobe: AddHitPoints wrapper installed");
}

void Report(LogFn log)
{
    char line[900];
    sprintf_s(line, "HITSTACKS installed=%d calls=%u paths=%d", installed, calls, pathCount);
    log(line);
    for (int i = 0; i < pathCount; ++i) {
        Path const& p = paths[i];
        sprintf_s(line, "HITSTACK count=%u victim=%s other=%s delta=%d frames=%s", p.count, p.victim.c_str(), p.other.c_str(),
                  p.delta, p.frames.empty() ? "-" : p.frames.c_str());
        log(line);
    }
    log("HITSTACKS end");
}
}
