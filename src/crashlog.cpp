// SPDX-License-Identifier: GPL-3.0-or-later
#include "crashlog.h"

#include "log.h"
#include "qa.h"

#include <Game.h>
#include <Windows.h>
#include <DbgHelp.h>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <iterator>

namespace g3t::crashlog {
namespace {
bool installed = false, dumpInstalled = false;
volatile bool probing = false;
LONG logged = 0;

// Minidumps (28.10.: a client ended with 0xC0000005 without any CRASH line, game crash log or Windows dump). Written by a
// thread of their own, started at install time, so that a crash with a broken stack still gets one: the crashing thread
// hands over its exception and waits. File: <game dir>\g3together-crash-<hhmmss>-<n>.dmp, next to g3together.log; the QA
// runner moves it into the run folder. For every player on the last chance (an exception nobody handled); with QA also on
// the first chance of an access violation (at most MaxFirstChanceDumps: the game catches some itself).
using DumpFn = BOOL(WINAPI*)(HANDLE, DWORD, HANDLE, MINIDUMP_TYPE, PMINIDUMP_EXCEPTION_INFORMATION, PMINIDUMP_USER_STREAM_INFORMATION,
                             PMINIDUMP_CALLBACK_INFORMATION);
DumpFn writeDump = nullptr;
HANDLE dumpRequest = nullptr, dumpDone = nullptr;
EXCEPTION_POINTERS* dumpInfo = nullptr;
DWORD dumpThreadId = 0;
char dumpDir[MAX_PATH] = {};
LONG dumps = 0;
constexpr LONG MaxFirstChanceDumps = 2;
LPTOP_LEVEL_EXCEPTION_FILTER previousFilter = nullptr;

DWORD WINAPI DumpThread(LPVOID)
{
    for (;;) {
        if (WaitForSingleObject(dumpRequest, INFINITE) != WAIT_OBJECT_0) return 0;
        SYSTEMTIME now;
        GetLocalTime(&now);
        char path[MAX_PATH + 64];
        Format(path, "%sg3together-crash-%02u%02u%02u-%ld.dmp", dumpDir, now.wHour, now.wMinute, now.wSecond, dumps);
        HANDLE file = CreateFileA(path, GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (file != INVALID_HANDLE_VALUE) {
            MINIDUMP_EXCEPTION_INFORMATION exception{dumpThreadId, dumpInfo, FALSE};
            const auto type = static_cast<MINIDUMP_TYPE>(MiniDumpWithIndirectlyReferencedMemory | MiniDumpWithDataSegs | MiniDumpWithThreadInfo);
            writeDump(GetCurrentProcess(), GetCurrentProcessId(), file, type, dumpInfo ? &exception : nullptr, nullptr, nullptr);
            CloseHandle(file);
        }
        SetEvent(dumpDone);
    }
}

void Dump(EXCEPTION_POINTERS* info)
{
    if (!writeDump || !dumpRequest) return;
    dumpInfo = info;
    dumpThreadId = GetCurrentThreadId();
    SetEvent(dumpRequest);
    WaitForSingleObject(dumpDone, 30000);
}

LONG WINAPI LastChance(EXCEPTION_POINTERS* info)
{
    if (InterlockedIncrement(&dumps) <= 4) Dump(info);
    return previousFilter ? previousFilter(info) : EXCEPTION_CONTINUE_SEARCH;
}

void InstallDumps()
{
    if (dumpInstalled) return;
    dumpInstalled = true;
    HMODULE self = nullptr;
    if (!GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                            reinterpret_cast<LPCSTR>(&InstallDumps), &self) || !GetModuleFileNameA(self, dumpDir, MAX_PATH))
        return;
    for (int part = 0; part != 2; ++part) {  // <game dir>\scripts\Script_G3Together.dll -> <game dir>\  (as log.cpp)
        char* separator = std::strrchr(dumpDir, '\\');
        if (!separator) { *dumpDir = '\0'; return; }
        *separator = '\0';
    }
    strcat_s(dumpDir, "\\");
    HMODULE dbghelp = LoadLibraryA("dbghelp.dll");
    writeDump = dbghelp ? reinterpret_cast<DumpFn>(GetProcAddress(dbghelp, "MiniDumpWriteDump")) : nullptr;
    dumpRequest = CreateEventA(nullptr, FALSE, FALSE, nullptr);
    dumpDone = CreateEventA(nullptr, FALSE, FALSE, nullptr);
    HANDLE thread = writeDump && dumpRequest && dumpDone ? CreateThread(nullptr, 256 * 1024, &DumpThread, nullptr, 0, nullptr) : nullptr;
    if (!thread) { writeDump = nullptr; Log("crashlog: no minidumps (dbghelp or thread missing)"); return; }
    CloseHandle(thread);
    previousFilter = SetUnhandledExceptionFilter(&LastChance);
    Logf("crashlog: minidumps on a crash go to %sg3together-crash-*.dmp", dumpDir);
}

struct Module {
    char const* name;
    uintptr_t base = 0, size = 0;
    bool game = true;  // crash lines name its functions
};
Module modules[] = {{"Gothic3.exe"}, {"Engine.dll"}, {"Game.dll"}, {"Script.dll"}, {"Script_Game.dll"}, {"SharedBase.dll"},
                    {"Script_G3Together.dll", 0, 0, false}, {"Script_paru.dll", 0, 0, false}};

// Function names in crash lines (29.10.): the nearest export at or below the address, for Script_Game.dll also the scripts
// the game registered (most of them are not exported). Mangled names are undecorated by dbghelp, loaded at install time.
bool names = true;  // QA crashnamesoff: module+RVA only
unsigned named = 0;
using UndecorateFn = DWORD(WINAPI*)(PCSTR, PSTR, DWORD, DWORD);
UndecorateFn undecorate = nullptr;
constexpr DWORD UndnameNameOnly = 0x1000;  // UNDNAME_NAME_ONLY

struct Script {
    uintptr_t at;
    char name[48];
};
Script scripts[4096];
volatile LONG scriptCount = 0;
bool scriptsTaken = false;

IMAGE_NT_HEADERS const* Headers(Module const& m)
{
    auto* dos = reinterpret_cast<IMAGE_DOS_HEADER const*>(m.base);
    return reinterpret_cast<IMAGE_NT_HEADERS const*>(m.base + dos->e_lfanew);
}

bool Executable(Module const& m, uintptr_t rva)
{
    IMAGE_NT_HEADERS const* nt = Headers(m);
    IMAGE_SECTION_HEADER const* section = IMAGE_FIRST_SECTION(nt);
    for (WORD i = 0; i != nt->FileHeader.NumberOfSections; ++i, ++section) {
        const uintptr_t size = section->Misc.VirtualSize ? section->Misc.VirtualSize : section->SizeOfRawData;
        if (rva >= section->VirtualAddress && rva < section->VirtualAddress + size)
            return (section->Characteristics & IMAGE_SCN_MEM_EXECUTE) != 0;
    }
    return false;
}

// The exported function that starts nearest at or below rva (forwarders and data skipped, a jmp thunk followed).
char const* NearestExport(Module const& m, uintptr_t rva, uintptr_t* start)
{
    auto const* base = reinterpret_cast<unsigned char const*>(m.base);
    IMAGE_DATA_DIRECTORY const& dir = Headers(m)->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_EXPORT];
    if (!dir.VirtualAddress || !dir.Size) return nullptr;
    auto const* exports = reinterpret_cast<IMAGE_EXPORT_DIRECTORY const*>(base + dir.VirtualAddress);
    auto const* functions = reinterpret_cast<DWORD const*>(base + exports->AddressOfFunctions);
    auto const* nameRvas = reinterpret_cast<DWORD const*>(base + exports->AddressOfNames);
    auto const* ordinals = reinterpret_cast<WORD const*>(base + exports->AddressOfNameOrdinals);
    char const* best = nullptr;
    for (DWORD i = 0; i != exports->NumberOfNames; ++i) {
        if (ordinals[i] >= exports->NumberOfFunctions) continue;
        uintptr_t at = functions[ordinals[i]];
        if (at >= dir.VirtualAddress && at < dir.VirtualAddress + dir.Size) continue;  // forwarder
        if (!Executable(m, at)) continue;
        if (base[at] == 0xE9) {  // incremental-link thunk: jmp rel32
            int32_t relative;
            std::memcpy(&relative, base + at + 1, sizeof(relative));
            at += 5 + relative;
            if (at >= m.size || !Executable(m, at)) continue;
        }
        if (at <= rva && (!best || at > *start)) {
            best = reinterpret_cast<char const*>(base + nameRvas[i]);
            *start = at;
        }
    }
    return best;
}

char const* NearestScript(Module const& m, uintptr_t address, uintptr_t* start)
{
    char const* best = nullptr;
    const LONG count = scriptCount;
    for (LONG i = 0; i != count; ++i) {
        const uintptr_t at = scripts[i].at;
        if (at < m.base || at > address || (best && at - m.base <= *start)) continue;
        best = scripts[i].name;
        *start = at - m.base;
    }
    return best;
}

// Once a game runs, all scripts are registered: their entry points with their names, for crash lines.
void TakeScripts()
{
    if (scriptsTaken || !gCSession::GetInstance().IsGameRunning()) return;
    scriptsTaken = true;
    gCScriptAdminExt& admin = GetScriptAdminExt();
    LONG count = 0;
    auto take = [&](bCString const& name, void const* fn) {
        if (!fn || count == static_cast<LONG>(std::size(scripts))) return;
        scripts[count].at = reinterpret_cast<uintptr_t>(fn);
        strncpy_s(scripts[count].name, name.GetText(), _TRUNCATE);
        ++count;
    };
    GE_MAP_FOR_EACH (name, script, admin.GetScripts())
        if (script) take(name, reinterpret_cast<void const*>(script->m_funcScript));
    GE_MAP_FOR_EACH (name, state, admin.GetScriptAIStates())
        if (state) take(name, reinterpret_cast<void const*>(state->m_funcScriptAIState));
    GE_MAP_FOR_EACH (name, function, admin.GetScriptAIFunctions())
        if (function) take(name, reinterpret_cast<void const*>(function->m_funcScriptAIFunction));
    GE_MAP_FOR_EACH (name, callback, admin.GetScriptAICallbacks())
        if (callback) take(name, reinterpret_cast<void const*>(callback->m_funcScriptAICallback));
    InterlockedExchange(&scriptCount, count);
    Logf("crashlog: %d script names taken for crash lines", static_cast<int>(count));
}

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

// "Module+rva" for an address in one of the modules, else nothing; for the game's modules with " (Function+offset)".
bool Where(uintptr_t address, char* out, size_t size)
{
    for (Module const& m : modules)
        if (m.base && address >= m.base && address < m.base + m.size) {
            const uintptr_t rva = address - m.base;
            int length = Format(out, size, "%s+%#x", m.name, static_cast<unsigned>(rva));
            if (!names || !m.game) return true;
            uintptr_t start = 0, scriptStart = 0;
            char const* name = NearestExport(m, rva, &start);
            if (char const* script = NearestScript(m, address, &scriptStart); script && (!name || scriptStart > start)) {
                name = script;
                start = scriptStart;
            }
            if (!name) return true;
            char plain[160];
            if (*name == '?' && undecorate && undecorate(name, plain, sizeof(plain), UndnameNameOnly)) name = plain;
            Format(out + length, size - length, " (%s+%#x)", name, static_cast<unsigned>(rva - start));
            ++named;
            return true;
        }
    return false;
}

LONG CALLBACK OnException(EXCEPTION_POINTERS* info)
{
    if (!info || info->ExceptionRecord->ExceptionCode != EXCEPTION_ACCESS_VIOLATION) return EXCEPTION_CONTINUE_SEARCH;
    if (!probing && GetCurrentThreadId() != dumpThreadId && InterlockedIncrement(&dumps) <= MaxFirstChanceDumps) Dump(info);  // before anything
    // that could fault again
    if (InterlockedIncrement(&logged) > 12) return EXCEPTION_CONTINUE_SEARCH;
    CONTEXT const& c = *info->ContextRecord;
    char eip[240] = "?", line[1200];
    Where(c.Eip, eip, sizeof(eip));
    Format(line, "CRASH access violation at %s (%08lX) %s %08lX eax=%08lX ebx=%08lX ecx=%08lX edx=%08lX esi=%08lX edi=%08lX esp=%08lX ebp=%08lX thread=%lu",
              eip, c.Eip, info->ExceptionRecord->ExceptionInformation[0] ? "writing" : "reading",
              static_cast<unsigned long>(info->ExceptionRecord->ExceptionInformation[1]), c.Eax, c.Ebx, c.Ecx, c.Edx, c.Esi, c.Edi, c.Esp,
              c.Ebp, GetCurrentThreadId());
    Log(line);
    // Return addresses (and other code pointers) on the stack, nearest first. Log() keeps about 1000 characters of a line,
    // so a long stack goes on "CRASH stack+" lines.
    char stack[3000] = "CRASH stack";
    size_t lineStart = 0;
    int found = 0;
    auto const* top = reinterpret_cast<uintptr_t const*>(c.Esp);
    MEMORY_BASIC_INFORMATION mbi{};
    if (VirtualQuery(top, &mbi, sizeof(mbi))) {
        const uintptr_t end = reinterpret_cast<uintptr_t>(mbi.BaseAddress) + mbi.RegionSize;
        for (int i = 0; i < 4096 && found < 32 && reinterpret_cast<uintptr_t>(top + i) + sizeof(uintptr_t) <= end; ++i) {
            char where[240];
            if (!Where(top[i], where, sizeof(where))) continue;
            const size_t need = std::strlen(where) + 2;
            size_t length = std::strlen(stack + lineStart);
            if (length + need >= 1000) {  // this line is full: log it, the next one starts behind it
                if (lineStart + length + 1 + 16 + need >= sizeof(stack)) break;
                Log(stack + lineStart);
                lineStart += length + 1;
                strcpy_s(stack + lineStart, sizeof(stack) - lineStart, "CRASH stack+");
                length = std::strlen(stack + lineStart);
            }
            if (lineStart + length + need >= sizeof(stack)) break;
            strcat_s(stack + lineStart, sizeof(stack) - lineStart, " ");
            strcat_s(stack + lineStart, sizeof(stack) - lineStart, where);
            ++found;
        }
    }
    Log(stack + lineStart);
    return EXCEPTION_CONTINUE_SEARCH;
}

// QA crashprobe: an access violation inside Engine.dll that is caught here (the vectored handler logs it first).
__declspec(noinline) void TouchBadEntity()
{
    bCVector position;
    reinterpret_cast<eCEntity const*>(0x10)->GetWorldPosition(position);
}

int ProbeFault()
{
    __try {
        TouchBadEntity();
        return 0;
    } __except (GetExceptionCode() == EXCEPTION_ACCESS_VIOLATION ? EXCEPTION_EXECUTE_HANDLER : EXCEPTION_CONTINUE_SEARCH) {
        return 1;
    }
}
}

void Tick()
{
    InstallDumps();  // every player: last-chance minidumps
    if (installed) { TakeScripts(); return; }
    if (!qa::Enabled()) return;
    installed = true;
    FindModules();
    HMODULE dbghelp = LoadLibraryA("dbghelp.dll");
    undecorate = dbghelp ? reinterpret_cast<UndecorateFn>(GetProcAddress(dbghelp, "UnDecorateSymbolName")) : nullptr;
    AddVectoredExceptionHandler(1, &OnException);
    Log("crashlog: access violations are logged (QA)");
}

void NamesOff(LogFn log)
{
    names = false;
    log("CRASHNAMESOFF ok=1 (crash lines show module+RVA only)");
}

void Probe(LogFn log)
{
    const unsigned before = named;
    probing = true;  // no minidump for it
    const int fault = ProbeFault();
    probing = false;
    char line[120];
    Format(line, "CRASHPROBE fault=%d named=%u scripts=%d names=%d", fault, named - before, static_cast<int>(scriptCount),
           names ? 1 : 0);
    log(line);
}
}
