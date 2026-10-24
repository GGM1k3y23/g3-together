// SPDX-License-Identifier: GPL-3.0-or-later
// Short on-screen messages through the game's own message list (GUI2::PrintGameMessage: centred, fades out
// after about 6 s, colour by type). Every message is also logged, so that bots can check what a player saw.
#pragma once

namespace g3t::hud {
enum Kind { Info = 0, Good = 8, Warning = 12, Bad = 13 };  // gEGameMessageType: gold, green, orange, red
void Message(Kind kind, wchar_t const* text);
using LogFn = void (*)(char const*);
void Report(LogFn log);  // QA: HUD count and the last messages
// The main menu's top line (the Community Patch's version line) with a second line of the mod's under it, for a player
// waiting there (the game's messages do not show in the main menu); nullptr puts it back. False: no such line here.
bool MenuLine(wchar_t const* text);
void MenuLineReport(LogFn log);          // QA: MENULINE, the line's text now (a line break as \n)
void Probe(char const* way, LogFn log);  // QA: MENUTEXT, a test text by way of hud, gamemsg or subtitle
}
