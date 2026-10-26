// SPDX-License-Identifier: GPL-3.0-or-later
#include "hud.h"

#include "log.h"

#include <Game.h>
#include <Script.h>
#include <Windows.h>
#include <cstdio>
#include <cstring>
#include <string>

namespace g3t::i18n {
char const* GameTextLanguage() { return eCLocAdmin::GetInstance().GetCurrentLanguage().GetText(); }
}

namespace g3t::hud {
namespace {
unsigned count = 0;
std::string last[4];  // newest first, as UTF-8 for the log
}

void Message(Kind kind, wchar_t const* text)
{
    gui2.PrintGameMessage(bCUnicodeString(text), static_cast<gEGameMessageType>(kind));
    char utf8[700] = {};  // the longest German message with a file name in it is ~260 bytes
    WideCharToMultiByte(CP_UTF8, 0, text, -1, utf8, sizeof(utf8) - 1, nullptr, nullptr);
    for (int i = 3; i > 0; --i) last[i] = last[i - 1];
    last[0] = utf8;
    ++count;
    Logf("hud: [%d] %s", static_cast<int>(kind), utf8);
}

void Report(LogFn log)
{
    char line[400];
    Format(line, "HUD count=%u last=%s", count, last[0].c_str());
    log(line);
}

namespace {
// A main menu element by its id (QA's menu scan: the Community Patch's version line is 20812, top centre).
CFFGFCWnd* MenuElement(GEU32 id)
{
    CFFGUIModule& module = CFFGUIModule::GetInstance();
    for (int y = 8; y < 768; y += 64)
        for (int x = 8; x < 1024; x += 64)
            for (CFFGFCWnd* window = CFFGFCWnd::FromHandle(module.WindowFromPoint(bCPoint(x, y), 0)); window; window = window->GetParentWindow()) {
                if (window->GetDlgCtrlID() == id) return window;
                if (CFFGFCWnd* child = window->GetDlgItem(id)) return child;
            }
    return nullptr;
}

constexpr GEU32 VersionLine = 20812;
std::wstring lineText;   // the version line as the game made it
bCRect lineRect;
bool lineSaved = false, lineChanged = false;
}

// In the main menu the game's messages do not show (menutextdiag 24.10.: GUI2, the GUI manager's message and its subtitle
// line all stayed invisible), but its top line is an element: the Community Patch's version line. A second line under it,
// across the whole width, says what a player waiting there must know; the version stays as it is.
bool MenuLine(wchar_t const* text)
{
    if (!text && !lineChanged) return true;
    CFFGFCWnd* line = MenuElement(VersionLine);
    if (!line) return false;
    if (!lineSaved) {
        lineText = line->GetWindowTextA().GetText();
        line->GetWindowRect(lineRect);
        lineSaved = true;
    }
    if (!text) {
        line->SetWindowTextA(bCUnicodeString(lineText.c_str()));
        line->MoveWindow(lineRect);
        lineChanged = false;
        return true;
    }
    const std::wstring shown = lineText + L"\n" + text;
    if (line->GetWindowTextA().GetText() != shown) line->SetWindowTextA(bCUnicodeString(shown.c_str()));
    // Room for the version and two lines of text: a refusal that lists differing files, or German, runs longer.
    if (!lineChanged) line->MoveWindow(bCRect(0, lineRect.GetTop(), 1024, lineRect.GetTop() + 3 * lineRect.GetHeight() + 4));
    lineChanged = true;
    return true;
}

void MenuLineReport(LogFn log)
{
    CFFGFCWnd* line = MenuElement(VersionLine);
    char utf8[600] = {};
    if (line) WideCharToMultiByte(CP_UTF8, 0, line->GetWindowTextA().GetText(), -1, utf8, sizeof(utf8) - 1, nullptr, nullptr);
    std::string shown;  // a line break as \n: the version line holds a | itself
    for (char const* c = utf8; *c; ++c) shown += *c == '\n' ? std::string("\\n") : std::string(1, *c);
    std::string text = "MENULINE found=" + std::string(line ? "1" : "0") + " changed=" + (lineChanged ? "1" : "0") + " text=" + shown;
    log(text.c_str());
}

// QA (menutextdiag 24.10.): a test text through one of the game's ways to show one ("hud": GUI2 as Message does,
// "gamemsg": the GUI manager's PrintGameMessage, "subtitle": its subtitle line); can a player read it in the main menu?
void Probe(char const* way, LogFn log)
{
    gCGUIManager* manager = gCSession::GetInstance().GetGUIManager();
    char line[160];
    if (!std::strcmp(way, "hud")) {
        Message(Info, L"G3 Together test: HUD message (GUI2)");
    } else if (!std::strcmp(way, "gamemsg") && manager) {
        manager->PrintGameMessage(bCUnicodeString(L"G3 Together test: PrintGameMessage (GUI manager)"), static_cast<gEGameMessageType>(Warning));
    } else if (!std::strcmp(way, "subtitle") && manager) {
        manager->ShowSubtitle(bCUnicodeString(L"G3 Together test: subtitle line (GUI manager)"));
    } else {
        Format(line, "REFUSED menutext %s: hud, gamemsg or subtitle (gui=%d)", way, manager ? 1 : 0);
        log(line);
        return;
    }
    Format(line, "MENUTEXT way=%s gui=%d running=%d", way, manager ? 1 : 0, gCSession::GetInstance().IsGameRunning() ? 1 : 0);
    log(line);
}
}
