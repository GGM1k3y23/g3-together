// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once

#include <cstdarg>
#include <cstddef>

namespace g3t {
// Opens <game dir>\<name> next to the game's scripts folder; lines are appended with a timestamp.
void OpenLog(const char* name);
void Log(const char* text);
void Logf(const char* format, ...);

// Formats into a fixed buffer and cuts off what does not fit. sprintf_s ends the game instead (0xC0000409, no log line:
// the trader line of 22.10., QA findnpc 25./26.10.; review 26.10.: TALKSYNC, HITNPC and others close to their size).
// Returns the characters written, so `n += Format(line + n, sizeof(line) - n, ...)` stays inside the buffer.
int FormatV(char* out, size_t size, const char* format, va_list args);
int Format(char* out, size_t size, const char* format, ...);
template <size_t N>
int Format(char (&out)[N], const char* format, ...)
{
    va_list args;
    va_start(args, format);
    const int length = FormatV(out, N, format, args);
    va_end(args);
    return length;
}
using FormatLogFn = void (*)(char const*);
void FormatProbe(char const* argument, FormatLogFn log);  // QA: one line of <n> characters into a 64-byte buffer
void FormatStrict(FormatLogFn log);                       // QA negative control (formatstrict): sprintf_s as before 26.10.
}
