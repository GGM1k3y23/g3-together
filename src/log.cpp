// SPDX-License-Identifier: GPL-3.0-or-later
#include "log.h"

#include <Windows.h>
#include <cstdarg>
#include <cstdio>
#include <cstring>

namespace g3t {
namespace {
char path[MAX_PATH] = {};
}

void OpenLog(const char* name)
{
    HMODULE self = nullptr;
    if (!GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                            reinterpret_cast<LPCSTR>(&OpenLog), &self) ||
        !GetModuleFileNameA(self, path, MAX_PATH)) {
        *path = '\0';
        return;
    }
    // <game dir>\scripts\Script_G3Together.dll -> <game dir>\<name>
    for (int part = 0; part != 2; ++part) {
        char* separator = std::strrchr(path, '\\');
        if (!separator) { *path = '\0'; return; }
        *separator = '\0';
    }
    if (strcat_s(path, "\\") || strcat_s(path, name)) *path = '\0';
}

void Log(const char* text)
{
    if (!*path) return;
    HANDLE file = CreateFileA(path, FILE_APPEND_DATA, FILE_SHARE_READ, nullptr, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (file == INVALID_HANDLE_VALUE) return;
    SYSTEMTIME now;
    GetLocalTime(&now);
    char line[1024];
    const int length = sprintf_s(line, "%02u:%02u:%02u.%03u %s\r\n", now.wHour, now.wMinute, now.wSecond,
                                 now.wMilliseconds, text);
    DWORD written = 0;
    if (length > 0) WriteFile(file, line, static_cast<DWORD>(length), &written, nullptr);
    CloseHandle(file);
}

void Logf(const char* format, ...)
{
    char text[900];
    va_list args;
    va_start(args, format);
    vsprintf_s(text, format, args);
    va_end(args);
    Log(text);
}
}
