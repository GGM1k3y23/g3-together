// SPDX-License-Identifier: GPL-3.0-or-later
#include "hud.h"

#include "log.h"

#include <Script.h>
#include <Windows.h>
#include <cstdio>
#include <string>

namespace g3t::hud {
namespace {
unsigned count = 0;
std::string last[4];  // newest first, as UTF-8 for the log
}

void Message(Kind kind, wchar_t const* text)
{
    gui2.PrintGameMessage(bCUnicodeString(text), static_cast<gEGameMessageType>(kind));
    char utf8[300] = {};
    WideCharToMultiByte(CP_UTF8, 0, text, -1, utf8, sizeof(utf8) - 1, nullptr, nullptr);
    for (int i = 3; i > 0; --i) last[i] = last[i - 1];
    last[0] = utf8;
    ++count;
    Logf("hud: [%d] %s", static_cast<int>(kind), utf8);
}

void Report(LogFn log)
{
    char line[400];
    sprintf_s(line, "HUD count=%u last=%s", count, last[0].c_str());
    log(line);
}
}
