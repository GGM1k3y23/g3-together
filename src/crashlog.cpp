// SPDX-License-Identifier: GPL-3.0-or-later
#include "crashlog.h"

#include "log.h"
#include "qa.h"

#include <Windows.h>
#include <cstdio>
#include <cstring>

namespace g3t::crashlog {
namespace {
bool installed = false;
LONG logged = 0;

struct Module {
    char const* name;
    uintptr_t base = 0, size = 0;
};
Module modules[] = {{"Gothic3.exe"}, {"Engine.dll"}, {"Game.dll"}, {"Script.dll"}, {"Script_Game.dll"}, {"SharedBase.dll"},
                    {"Script_G3Together.dll"}, {"Script_paru.dll"}};

void FindModules()
{
    for (Module& m : modules) {
        HMODULE h = GetModuleHandleA(m.name);
        if (!h) continue;
        auto* dos = reinterpret_cast<IMAGE_DOS_HEADER const*>(h);
        auto* nt = reinterpret_cast<IMAGE_NT_HEADERS const*>(reinterpret_cast<unsigned char const*>(h) + dos->e_lfanew);
        m.base = reinterpret_cast<uintptr_t>(h);
        m.size = nt->OptionalHeader.SizeOfImage;
    }
}

// "Module+rva" for an address in one of the modules, else nothing.
bool Where(uintptr_t address, char* out, size_t size)
{
    for (Module const& m : modules)
        if (m.base && address >= m.base && address < m.base + m.size) {
            Format(out, size, "%s+%#x", m.name, static_cast<unsigned>(address - m.base));
            return true;
        }
    return false;
}

LONG CALLBACK OnException(EXCEPTION_POINTERS* info)
{
    if (!info || info->ExceptionRecord->ExceptionCode != EXCEPTION_ACCESS_VIOLATION) return EXCEPTION_CONTINUE_SEARCH;
    if (InterlockedIncrement(&logged) > 12) return EXCEPTION_CONTINUE_SEARCH;
    CONTEXT const& c = *info->ContextRecord;
    char eip[64] = "?", line[1000];
    Where(c.Eip, eip, sizeof(eip));
    Format(line, "CRASH access violation at %s (%08lX) %s %08lX eax=%08lX ebx=%08lX ecx=%08lX edx=%08lX esi=%08lX edi=%08lX esp=%08lX ebp=%08lX thread=%lu",
              eip, c.Eip, info->ExceptionRecord->ExceptionInformation[0] ? "writing" : "reading",
              static_cast<unsigned long>(info->ExceptionRecord->ExceptionInformation[1]), c.Eax, c.Ebx, c.Ecx, c.Edx, c.Esi, c.Edi, c.Esp,
              c.Ebp, GetCurrentThreadId());
    Log(line);
    // Return addresses (and other code pointers) on the stack, nearest first.
    char stack[900] = "CRASH stack";
    int found = 0;
    auto const* top = reinterpret_cast<uintptr_t const*>(c.Esp);
    MEMORY_BASIC_INFORMATION mbi{};
    if (VirtualQuery(top, &mbi, sizeof(mbi))) {
        const uintptr_t end = reinterpret_cast<uintptr_t>(mbi.BaseAddress) + mbi.RegionSize;
        for (int i = 0; i < 4096 && found < 32 && reinterpret_cast<uintptr_t>(top + i) + sizeof(uintptr_t) <= end; ++i) {
            char where[64];
            if (!Where(top[i], where, sizeof(where))) continue;
            if (std::strlen(stack) + std::strlen(where) + 2 >= sizeof(stack)) break;
            strcat_s(stack, " ");
            strcat_s(stack, where);
            ++found;
        }
    }
    Log(stack);
    return EXCEPTION_CONTINUE_SEARCH;
}
}

void Tick()
{
    if (installed || !qa::Enabled()) return;
    installed = true;
    FindModules();
    AddVectoredExceptionHandler(1, &OnException);
    Log("crashlog: access violations are logged (QA)");
}
}
