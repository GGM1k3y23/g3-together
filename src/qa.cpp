// SPDX-License-Identifier: GPL-3.0-or-later
// Test bot for disposable game copies (ported from the Myrtana evaluation helper).
// Menus are driven through the game's GUI API plus Enter posted to the game's own window, so the
// game handles each key in its normal message pump. No OS input, no address patterns.
#include "qa.h"

#include "ai.h"
#include "body.h"
#include "combat.h"
#include "hitprobe.h"
#include "story.h"
#include "partner.h"
#include "talk.h"
#include "items.h"
#include "containers.h"
#include "explore.h"
#include "npcs.h"
#include "downed.h"
#include "transform.h"
#include "ranged.h"
#include "summon.h"
#include "hud.h"
#include "range.h"
#include "journal.h"
#include "persist.h"
#include "hero.h"
#include "clock.h"
#include "infocmd.h"
#include "remote.h"

#include <Game.h>
#include <Windows.h>
#include <winternl.h>
#include <cctype>
#include <cstdlib>
#include <cstdio>
#include <cstring>
#include <cwchar>
#include <vector>

namespace g3t::qa {
namespace {
bool enabled = false;
bool detached = false;
char root[MAX_PATH] = {};
DWORD lastPoll = 0;
unsigned lastSequence = 0;
WNDPROC originalWindowProc = nullptr;
HWND gameWindow = nullptr;
constexpr UINT commandMessage = WM_APP + 0x36A;
char pendingAction[32] = {};
char pendingArg[64] = {};
char argument[64] = {};
unsigned commandDepth = 0;
bool newGamePending = false;

bool Path(char (&path)[MAX_PATH], const char* file)
{
    return *root && sprintf_s(path, "%s\\%s", root, file) >= 0;
}

void Log(const char* text)
{
    char path[MAX_PATH] = {};
    if (!Path(path, "qa-engine.log")) return;
    HANDLE file = CreateFileA(path, FILE_APPEND_DATA, FILE_SHARE_READ, nullptr, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (file == INVALID_HANDLE_VALUE) return;
    DWORD written = 0;
    WriteFile(file, text, static_cast<DWORD>(std::strlen(text)), &written, nullptr);
    WriteFile(file, "\r\n", 2, &written, nullptr);
    CloseHandle(file);
}

void Detach()
{
    detached = true;
    if (gameWindow && originalWindowProc)
        SetWindowLongPtrA(gameWindow, GWLP_WNDPROC, reinterpret_cast<LONG_PTR>(originalWindowProc));
}

// Gothic3.exe exits with code 0 while the named mutex GenomeMutex exists, so a second test copy can
// only start after this process closes its own handle to it.
using NtQueryObjectFn = LONG(NTAPI*)(HANDLE, int, PVOID, ULONG, PULONG);

bool MutexName(HANDLE handle, wchar_t (&name)[260])
{
    static const auto query = reinterpret_cast<NtQueryObjectFn>(GetProcAddress(GetModuleHandleA("ntdll.dll"), "NtQueryObject"));
    alignas(8) unsigned char buffer[1024] = {};
    ULONG size = 0;
    if (!query || query(handle, 2, buffer, sizeof(buffer), &size) < 0) return false;  // ObjectTypeInformation
    const auto* type = reinterpret_cast<const UNICODE_STRING*>(buffer);
    if (type->Length != 12 || std::wcsncmp(type->Buffer, L"Mutant", 6)) return false;
    if (query(handle, 1, buffer, sizeof(buffer), &size) < 0) return false;  // ObjectNameInformation
    const auto* text = reinterpret_cast<const UNICODE_STRING*>(buffer);
    if (!text->Length) return false;
    swprintf_s(name, L"%.*s", static_cast<int>(text->Length / 2), text->Buffer);
    return true;
}

void InstanceLocks(const wchar_t* release)
{
    char line[600] = {};
    for (ULONG_PTR value = 4; value < 0x10000; value += 4) {
        HANDLE handle = reinterpret_cast<HANDLE>(value);
        wchar_t name[260] = {};
        if (!MutexName(handle, name)) continue;
        const bool close = release && std::wcsstr(name, release);
        sprintf_s(line, "LOCK mutex handle=%#Ix name=%ls%s", value, name, close ? " -> released" : "");
        Log(line);
        if (close) CloseHandle(handle);
    }
    Log("LOCK scan complete");
}

void State(gCSession& session)
{
    char line[1024] = {};
    gCWorld* world = gCWorld::GetCurrentWorld();
    eCDynamicEntity* hero = session.GetHero();
    const bCString name = world ? world->GetFileBaseName() : bCString("none");
    const bCString folder = gCWorld::GetSaveGameFolder();
    sprintf_s(line, "STATE running=%d paused=%d save_allowed=%d world=%s hero=%s save_folder=%s", session.IsGameRunning(),
              session.IsPaused(), session.IsSaveAllowed(), name.GetText(), hero ? hero->GetName().GetText() : "none",
              folder.GetText());
    Log(line);
    if (hero) {
        const bCVector& p = hero->GetWorldPosition();
        sprintf_s(line, "POS x=%.1f y=%.1f z=%.1f", p.GetX(), p.GetY(), p.GetZ());
        Log(line);
    }
    CFFGFCWnd* focus = CFFGFCWnd::FromHandle(CFFGUIModule::GetInstance().GetFocusWnd());
    if (focus) {
        sprintf_s(line, "FOCUS id=%u text=%.512s", focus->GetDlgCtrlID(), focus->GetWindowTextA().GetAnsiText().GetText());
        Log(line);
    }
}

void InspectMenu()
{
    CFFGUIModule& gui = CFFGUIModule::GetInstance();
    std::vector<IFFGUIWindow*> seen;
    char info[768] = {};
    for (int y = 8; y < 768; y += 16) {
        for (int x = 8; x < 1024; x += 16) {
            CFFGFCWnd* window = CFFGFCWnd::FromHandle(gui.WindowFromPoint(bCPoint(x, y), 0));
            for (int depth = 0; window && depth < 8; ++depth, window = window->GetParentWindow()) {
                IFFGUIWindow* handle = window->GetHandle();
                bool known = false;
                for (auto* previous : seen) if (previous == handle) { known = true; break; }
                if (known) break;
                seen.push_back(handle);
                char text[512] = {};
                const bCUnicodeString caption = window->GetWindowTextA();
                WideCharToMultiByte(CP_UTF8, 0, caption.GetText(), -1, text, sizeof(text), nullptr, nullptr);
                sprintf_s(info, "MENU id=%u visible=%d enabled=%d sample=%d,%d depth=%d text=%s", window->GetDlgCtrlID(),
                          window->IsWindowVisible(), window->IsWindowEnabled(), x, y, depth, text);
                Log(info);
                for (unsigned id = 1; id < 22000; ++id) {
                    if (id == 256) id = 20000;
                    CFFGFCWnd* child = window->GetDlgItem(id);
                    if (!child || !child->IsWindowVisible()) continue;
                    sprintf_s(info, "MENU child id=%u enabled=%d text=%.512s", id, child->IsWindowEnabled(),
                              child->GetWindowTextA().GetAnsiText().GetText());
                    Log(info);
                }
            }
        }
    }
    Log("MENU scan complete (game GUI API; no OS input)");
}

// Focus a visible, enabled control with this id and caption. For the shared Yes button (20711) the
// dialog question (20709) must contain prompt.
bool Focus(unsigned id, const char* caption, const char* prompt = nullptr)
{
    CFFGUIModule& gui = CFFGUIModule::GetInstance();
    char line[512] = {};
    for (int y = 8; y < 768; y += 16) {
        for (int x = 8; x < 1024; x += 16) {
            CFFGFCWnd* button = CFFGFCWnd::FromHandle(gui.WindowFromPoint(bCPoint(x, y), 0));
            if (!button) continue;
            if (button->GetDlgCtrlID() != id) button = button->GetDlgItem(id);
            if (!button || !button->IsWindowVisible() || !button->IsWindowEnabled()) continue;
            if (std::strcmp(button->GetWindowTextA().GetAnsiText().GetText(), caption)) continue;
            CFFGFCWnd* parent = button->GetParentWindow();
            if (!parent) return false;
            if (prompt) {
                CFFGFCWnd* question = parent->GetDlgItem(20709);
                if (!question || !std::strstr(question->GetWindowTextA().GetAnsiText().GetText(), prompt)) continue;
            }
            button->SetFocus();
            sprintf_s(line, "MENU_FOCUS id=%u focused=%d text=%s", id, button->HasFocus(), caption);
            Log(line);
            return button->HasFocus();
        }
    }
    Log("REFUSED menu command: exact visible/enabled control not found");
    return false;
}

void PostKey(UINT key, UINT scan)
{
    PostMessageA(gameWindow, WM_KEYDOWN, key, 1 | scan << 16);
    PostMessageA(gameWindow, WM_KEYUP, key, 1 | scan << 16 | 0xC0000000);
}

void Quest(gCSession& session, const char* action)
{
    char line[256] = {};
    gCQuestManager_PS* quests = gCQuestManager_PS::GetInstance();
    if (!quests || !*argument || !session.IsGameRunning()) { Log("REFUSED quest: no running game or quest id"); return; }
    const bCString id(argument);
    gCQuest_PS* quest = quests->GetQuest(id);
    if (!quest) { sprintf_s(line, "QUEST id=%s missing=1", argument); Log(line); return; }
    int changed = -1;
    if (!std::strcmp(action, "runquest")) changed = quests->RunQuest(id);
    else if (!std::strcmp(action, "succeedquest")) changed = quests->SucceedQuest(id);
    const gEQuestStatus status = static_cast<gEQuestStatus const&>(quest->AccessStatus());
    sprintf_s(line, "QUEST id=%s status=%d changed=%d", argument, static_cast<int>(status), changed);
    Log(line);
}

// Commands added after the chain below reached MSVC's limit of nested blocks (C1061).
bool RunLaterAction(const char* action)
{
    if (!std::strcmp(action, "partnerjump")) {
        g3t::partner::JumpQa(&Log);
    } else if (!std::strcmp(action, "partyxpoff")) {
        g3t::combat::PartyXpOff(&Log);
    } else if (!std::strcmp(action, "npcrecvoff")) {
        g3t::npcs::RecvOff(&Log);
    } else if (!std::strcmp(action, "fleekeepoff")) {
        g3t::ai::FleeKeepOff(&Log);
    } else if (!std::strcmp(action, "flee")) {
        g3t::combat::FleeFrom(argument, &Log);
    } else if (!std::strcmp(action, "scriptanioff")) {
        g3t::body::scriptAniReplay = false;
        Log("SCRIPTANIOFF gestures and knock-downs are replayed with StartPlayAni");
    } else if (!std::strcmp(action, "looprestartoff")) {
        g3t::body::loopRestart = false;
        Log("LOOPRESTARTOFF replayed loops play once");
    } else if (!std::strcmp(action, "potionoff")) {
        g3t::downed::PotionOff(&Log);
    } else if (!std::strcmp(action, "givepotion")) {
        g3t::downed::GivePotion(argument, &Log);
    } else if (!std::strcmp(action, "potions")) {
        g3t::downed::Potions(&Log);
    } else if (!std::strcmp(action, "transform")) {
        g3t::transform::Start(argument, &Log);
    } else if (!std::strcmp(action, "transformback")) {
        g3t::transform::Back(&Log);
    } else if (!std::strcmp(action, "transformstate")) {
        g3t::transform::Report(&Log);
    } else if (!std::strcmp(action, "inventory")) {
        g3t::ranged::Inventory(&Log);
    } else if (!std::strcmp(action, "ready")) {
        g3t::ranged::Ready(argument, &Log);
    } else if (!std::strcmp(action, "aimat")) {
        g3t::ranged::Target(argument, &Log);
    } else if (!std::strcmp(action, "act")) {
        g3t::ranged::Act(argument, &Log);
    } else if (!std::strcmp(action, "shootat")) {
        g3t::ranged::ShootAt(argument, &Log);
    } else if (!std::strcmp(action, "summonstate")) {
        g3t::summon::Report(&Log);
    } else if (!std::strcmp(action, "summoncopyhero")) {
        g3t::summon::CopyHeroOff(&Log);
    } else if (!std::strcmp(action, "summonmirroroff")) {
        g3t::summon::MirrorOff(&Log);
    } else if (!std::strcmp(action, "summons")) {
        g3t::ranged::Summons(&Log);
    } else if (!std::strcmp(action, "lastarrow")) {
        g3t::ranged::LastArrow(&Log);
    } else if (!std::strcmp(action, "homingoff")) {
        g3t::ranged::homing = false;
        Log("HOMINGOFF shootat keeps the camera's aim");
    } else if (!std::strcmp(action, "castat")) {
        g3t::ranged::CastAt(argument, &Log);
    } else if (!std::strcmp(action, "mana")) {
        g3t::ranged::Mana(&Log);
    } else if (!std::strcmp(action, "flying")) {
        g3t::ranged::Flying(&Log);
    } else if (!std::strcmp(action, "shapeoff")) {
        g3t::remote::ShapeOff(&Log);
    } else if (!std::strcmp(action, "walkby")) {
        g3t::transform::Walk(std::atoi(argument), &Log);
    } else {
        return false;
    }
    return true;
}

void RunAction(const char* action)
{
    gCSession& session = gCSession::GetInstance();
    char line[256] = {};
    if (RunLaterAction(action)) return;
    if (!std::strcmp(action, "state")) {
        State(session);
    } else if (!std::strcmp(action, "menu")) {
        InspectMenu();
    } else if (!std::strcmp(action, "focusnew")) {
        if (session.IsGameRunning()) { Log("REFUSED focusnew: already running"); return; }
        newGamePending = Focus(20571, "Neues Spiel");
    } else if (!std::strcmp(action, "focusyes")) {
        if (!newGamePending || session.IsGameRunning()) { Log("REFUSED focusyes: no pending new game"); return; }
        Focus(20711, "Ja", "ein neues Spiel");
    } else if (!std::strcmp(action, "focussave")) {
        if (!session.IsGameRunning() || !session.IsSaveAllowed()) { Log("REFUSED focussave: game not save-ready"); return; }
        Focus(20574, "Spiel speichern");
    } else if (!std::strcmp(action, "focusload")) {
        Focus(20573, "Spiel laden");
    } else if (!std::strcmp(action, "focusquit")) {
        Focus(20720, "Spiel beenden");
    } else if (!std::strcmp(action, "focusquityes")) {
        Focus(20711, "Ja", "beenden");
    } else if (!std::strcmp(action, "pressenter")) {
        // Only for known menu buttons that a previous focus command selected.
        CFFGFCWnd* focus = CFFGFCWnd::FromHandle(CFFGUIModule::GetInstance().GetFocusWnd());
        const unsigned id = focus ? focus->GetDlgCtrlID() : 0;
        if (id != 20571 && id != 20711 && id != 20573 && id != 20574 && id != 20720) {
            sprintf_s(line, "REFUSED pressenter: focus id=%u is not an allowed menu button", id); Log(line); return;
        }
        PostKey(VK_RETURN, 0x1C);
        sprintf_s(line, "PRESSENTER focus id=%u queued=1 (game window message queue, no OS input)", id);
        Log(line);
    } else if (!std::strcmp(action, "openmenu")) {
        if (!session.IsGameRunning()) { Log("REFUSED openmenu: no game running"); return; }
        session.OpenMenu();
        Log("OPENMENU returned");
    } else if (!std::strcmp(action, "closewindow")) {
        // Same path as the title-bar close button; detach on WM_DESTROY.
        Log("CLOSEWINDOW queue SC_CLOSE; detach on WM_DESTROY");
        PostMessageA(gameWindow, WM_SYSCOMMAND, SC_CLOSE, 0);
    } else if (!std::strcmp(action, "listlocks")) {
        InstanceLocks(nullptr);
    } else if (!std::strcmp(action, "releaselock")) {
        InstanceLocks(L"\\BaseNamedObjects\\GenomeMutex");
    } else if (!std::strcmp(action, "remotes")) {
        g3t::remote::Report(&Log);
        Log("REMOTES end");
    } else if (!std::strcmp(action, "hitnpc")) {
        g3t::combat::HitNpc(argument, &Log);
    } else if (!std::strcmp(action, "gamestr")) {
        g3t::combat::GameString(argument, &Log);
    } else if (!std::strcmp(action, "hitpuppet")) {
        g3t::combat::HitPuppet(std::atoi(argument), &Log);
    } else if (!std::strcmp(action, "arm")) {
        g3t::combat::Arm(argument, &Log);
    } else if (!std::strcmp(action, "combatoff")) {
        g3t::combat::Uninstall(&Log);
    } else if (!std::strcmp(action, "story")) {
        g3t::story::Report(&Log);
    } else if (!std::strcmp(action, "storyoff")) {
        g3t::story::Disable(&Log);
    } else if (!std::strcmp(action, "info") || !std::strcmp(action, "giveinfo")) {
        g3t::story::Info(argument, !std::strcmp(action, "giveinfo"), &Log);
    } else if (!std::strcmp(action, "items")) {
        g3t::items::List(&Log);
    } else if (!std::strcmp(action, "itemtake")) {
        g3t::items::Take(argument, &Log);
    } else if (!std::strcmp(action, "itemdecay")) {
        g3t::items::Decay(argument, &Log);
    } else if (!std::strcmp(action, "itempick")) {
        g3t::items::Pick(argument, &Log);
    } else if (!std::strcmp(action, "npcsummary")) {
        g3t::explore::Summary(&Log);
    } else if (!std::strcmp(action, "teleport")) {
        g3t::explore::Teleport(argument, &Log);
    } else if (!std::strcmp(action, "nativesave")) {
        g3t::explore::NativeSave(argument, &Log);
    } else if (!std::strcmp(action, "nativeload")) {
        g3t::explore::NativeLoad(argument, &Log);
    } else if (!std::strcmp(action, "page")) {
        g3t::explore::Page(argument, &Log);
    } else if (!std::strcmp(action, "teleportto")) {
        g3t::explore::TeleportTo(argument, &Log);
    } else if (!std::strcmp(action, "findnpc")) {
        g3t::explore::Find(argument, &Log);
    } else if (!std::strcmp(action, "farkill")) {
        g3t::explore::FarKill(argument, &Log);
    } else if (!std::strcmp(action, "farmove")) {
        g3t::explore::FarMove(argument, &Log);
    } else if (!std::strcmp(action, "farnpcs")) {
        g3t::explore::FarNpcs(&Log);
    } else if (!std::strcmp(action, "near")) {
        g3t::explore::Near(argument, &Log);
    } else if (!std::strcmp(action, "contents")) {
        g3t::containers::Contents(argument, &Log);
    } else if (!std::strcmp(action, "lootopen")) {
        g3t::containers::Open(argument, &Log);
    } else if (!std::strcmp(action, "loottake")) {
        g3t::containers::Take(argument, &Log);
    } else if (!std::strcmp(action, "tradeopen")) {
        g3t::containers::TradeOpen(argument, &Log);
    } else if (!std::strcmp(action, "tradebuy")) {
        g3t::containers::TradeBuy(argument, &Log);
    } else if (!std::strcmp(action, "stock")) {
        g3t::containers::Stock(argument, &Log);
    } else if (!std::strcmp(action, "goods")) {
        g3t::containers::Goods(argument, &Log);
    } else if (!std::strcmp(action, "lootsilent")) {
        g3t::containers::Silent(argument, &Log);
    } else if (!std::strcmp(action, "containersync")) {
        g3t::containers::Report(&Log);
    } else if (!std::strcmp(action, "containersyncoff")) {
        g3t::containers::Disable(&Log);
    } else if (!std::strcmp(action, "containers")) {
        g3t::items::Containers(&Log);
    } else if (!std::strcmp(action, "invcount")) {
        g3t::items::Count(argument, &Log);
    } else if (!std::strcmp(action, "downedoff")) {
        g3t::downed::Off(&Log);
    } else if (!std::strcmp(action, "herodump")) {
        g3t::hero::Dump(&Log);
    } else if (!std::strcmp(action, "herosave")) {
        g3t::hero::SaveCommand(argument, &Log);
    } else if (!std::strcmp(action, "heroload")) {
        g3t::hero::LoadCommand(argument, &Log);
    } else if (!std::strcmp(action, "herorestoreoff")) {
        g3t::hero::RestoreOff(&Log);
    } else if (!std::strcmp(action, "persist")) {
        g3t::persist::Report(&Log);
    } else if (!std::strcmp(action, "event") || !std::strcmp(action, "setevent") || !std::strcmp(action, "clearevent")) {
        g3t::story::Event(action, argument, &Log);
    } else if (!std::strcmp(action, "infodesc")) {
        g3t::explore::InfoDescribe(argument, &Log);
    } else if (!std::strcmp(action, "companionsoff")) {
        g3t::npcs::CompanionsOff(&Log);
    } else if (!std::strcmp(action, "questcountoff")) {
        g3t::journal::QuestCountOff(&Log);
    } else if (!std::strcmp(action, "questcounters")) {
        g3t::explore::QuestCounters(argument, &Log);
    } else if (!std::strcmp(action, "styleoff")) {
        g3t::npcs::StyleOff(&Log);
    } else if (!std::strcmp(action, "puppetholdoff")) {
        g3t::remote::PuppetHoldOff(&Log);
    } else if (!std::strcmp(action, "puppetcombatoff")) {
        g3t::remote::PuppetCombatOff(&Log);
    } else if (!std::strcmp(action, "swing")) {
        g3t::combat::Swing(argument, &Log);
    } else if (!std::strcmp(action, "puppetpartyoff")) {
        g3t::remote::PuppetPartyOff(&Log);
    } else if (!std::strcmp(action, "attitudeoff")) {
        g3t::ai::AttitudeOff(&Log);
    } else if (!std::strcmp(action, "combatreplayoff")) {
        g3t::body::combatReplay = false;
        Log("COMBATREPLAYOFF attacks are replayed by motion name");
    } else if (!std::strcmp(action, "motionowner")) {
        g3t::body::motionOwner = std::atoi(argument);
        char text[64];
        sprintf_s(text, "MOTIONOWNER %d", g3t::body::motionOwner);
        Log(text);
    } else if (!std::strcmp(action, "nearnpcs")) {
        g3t::explore::NearNpcs(&Log);
    } else if (!std::strcmp(action, "npcfight")) {
        g3t::explore::NpcFight(argument, &Log);
    } else if (!std::strcmp(action, "npchitsoff")) {
        g3t::combat::NpcHitsOff(&Log);
    } else if (!std::strcmp(action, "aggro")) {
        g3t::explore::Aggro(argument, &Log);
    } else if (!std::strcmp(action, "talkinfo")) {
        g3t::talk::ChooseInfo(argument, &Log);
    } else if (!std::strcmp(action, "infoscan")) {
        g3t::explore::InfoScan(argument, &Log);
    } else if (!std::strcmp(action, "join")) {
        g3t::infocmd::JoinQa(argument, &Log);
    } else if (!std::strcmp(action, "infocmdoff")) {
        g3t::infocmd::Off(&Log);
    } else if (!std::strcmp(action, "npcstate") || !std::strcmp(action, "simdialog")) {
        g3t::explore::NpcState(argument, !std::strcmp(action, "simdialog"), &Log);
    } else if (!std::strcmp(action, "infocmd")) {
        g3t::infocmd::Report(&Log);
    } else if (!std::strcmp(action, "clock")) {
        g3t::clock::Report(&Log);
    } else if (!std::strcmp(action, "clockset")) {
        g3t::clock::Set(argument, &Log);
    } else if (!std::strcmp(action, "clockoff")) {
        g3t::clock::Off(&Log);
    } else if (!std::strcmp(action, "campaignoff")) {
        g3t::persist::CampaignOff(&Log);
    } else if (!std::strcmp(action, "persistoff")) {
        g3t::persist::Off(&Log);
    } else if (!std::strcmp(action, "journal")) {
        g3t::journal::Report(&Log);
    } else if (!std::strcmp(action, "journaloff")) {
        g3t::journal::Off(&Log);
    } else if (!std::strcmp(action, "jentry")) {
        g3t::journal::Entry(argument, &Log);
    } else if (!std::strcmp(action, "range")) {
        g3t::range::Report(&Log);
    } else if (!std::strcmp(action, "rangeoff")) {
        g3t::range::Off(&Log);
    } else if (!std::strcmp(action, "holdoff")) {
        g3t::downed::HoldOff(&Log);
    } else if (!std::strcmp(action, "hud")) {
        g3t::hud::Report(&Log);
    } else if (!std::strcmp(action, "downed")) {
        g3t::downed::Report(&Log);
    } else if (!std::strcmp(action, "hitpuppetby")) {
        g3t::combat::HitPuppetBy(argument, &Log);
    } else if (!std::strcmp(action, "killhero")) {
        g3t::combat::KillHero(argument, &Log);
    } else if (!std::strcmp(action, "herostate")) {
        g3t::combat::HeroState(&Log);
    } else if (!std::strcmp(action, "hurthero")) {
        g3t::combat::HurtHero(argument, &Log);
    } else if (!std::strcmp(action, "givexp")) {
        g3t::combat::GiveXp(argument, &Log);
    } else if (!std::strcmp(action, "ownoff")) {
        g3t::npcs::OwnOff(&Log);
    } else if (!std::strcmp(action, "xpguardoff")) {
        g3t::npcs::XpGuardOff(&Log);
    } else if (!std::strcmp(action, "itemsyncoff")) {
        g3t::items::Disable(&Log);
    } else if (!std::strcmp(action, "itemtook")) {
        g3t::items::TookReport(&Log);
    } else if (!std::strcmp(action, "iteminfo")) {
        g3t::items::Describe(argument, &Log);
    } else if (!std::strcmp(action, "talkchoose")) {
        g3t::talk::Choose(std::atoi(argument), &Log);
    } else if (!std::strcmp(action, "talksync")) {
        g3t::talk::Report(&Log);
    } else if (!std::strcmp(action, "talkholdoff")) {
        g3t::talk::DisableHold(&Log);
    } else if (!std::strcmp(action, "assesstalk")) {
        g3t::story::AssessTalk(argument, &Log);
    } else if (!std::strcmp(action, "approach")) {
        g3t::story::Approach(argument, &Log);
    } else if (!std::strcmp(action, "killraiders")) {
        g3t::story::KillRaiders(&Log);
    } else if (!std::strcmp(action, "talk")) {
        g3t::story::Talk(argument, &Log);
    } else if (!std::strcmp(action, "talkstate")) {
        g3t::story::TalkState(&Log);
    } else if (!std::strcmp(action, "endtalk")) {
        g3t::story::EndTalk(&Log);
    } else if (!std::strcmp(action, "infolist")) {
        g3t::story::InfoCandidates(&Log);
    } else if (!std::strcmp(action, "hitstacks")) {
        g3t::hitprobe::Report(&Log);
    } else if (!std::strcmp(action, "hp")) {
        g3t::combat::Report(&Log);
    } else if (!std::strcmp(action, "names")) {
        g3t::ai::ListNames(argument, &Log);
    } else if (!std::strcmp(action, "scriptinfo")) {
        g3t::ai::Describe(argument, &Log);
    } else if (!std::strcmp(action, "npcs")) {
        g3t::npcs::Report(&Log);
        Log("NPCS end");
    } else if (!std::strcmp(action, "walkto")) {
        g3t::remote::WalkTo(std::atoi(argument), &Log);
    } else if (!std::strcmp(action, "step")) {
        g3t::remote::Step(std::atoi(argument), &Log);
    } else if (!std::strcmp(action, "quest") || !std::strcmp(action, "runquest") || !std::strcmp(action, "succeedquest")) {
        Quest(session, action);
    } else {
        Log("REFUSED unknown command");
    }
}

LRESULT CALLBACK WindowProc(HWND window, UINT message, WPARAM wparam, LPARAM lparam)
{
    if (message == WM_DESTROY) {
        Log("DESTROY window close accepted; detach QA callbacks");
        const WNDPROC next = originalWindowProc;
        Detach();
        return CallWindowProcA(next, window, message, wparam, lparam);
    }
    if (message == commandMessage) {
        char action[32] = {};
        strcpy_s(action, pendingAction);
        strcpy_s(argument, pendingArg);
        *pendingAction = '\0';
        *pendingArg = '\0';
        ++commandDepth;
        __try { RunAction(action); }
        __except (EXCEPTION_EXECUTE_HANDLER) {
            detached = true;
            Log("FAIL native QA command exception; commands disabled");
        }
        --commandDepth;
        return 0;
    }
    return CallWindowProcA(originalWindowProc, window, message, wparam, lparam);
}
}

bool Enabled() { return enabled && !detached; }

void Init()
{
    HMODULE self = nullptr;
    if (!GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                            reinterpret_cast<LPCSTR>(&Log), &self))
        return;
    const DWORD length = GetModuleFileNameA(self, root, MAX_PATH);
    if (!length || length >= MAX_PATH) { *root = '\0'; return; }
    for (int part = 0; part != 2; ++part) {
        char* separator = std::strrchr(root, '\\');
        if (!separator) { *root = '\0'; return; }
        *separator = '\0';
    }
    char flag[MAX_PATH] = {};
    enabled = Path(flag, "qa-enable.txt") && GetFileAttributesA(flag) != INVALID_FILE_ATTRIBUTES;
    if (enabled) Log("INIT G3 Together QA bot: opt-in disposable-install test");
}

void Tick()
{
    if (!Enabled()) return;
    g3t::ranged::Tick();
    const DWORD now = GetTickCount();
    if (now - lastPoll < 250 || commandDepth || *pendingAction) return;
    lastPoll = now;
    if (!originalWindowProc) {
        gameWindow = eCApplication::GetInstance().GetHandle();
        if (!gameWindow) return;
        originalWindowProc = reinterpret_cast<WNDPROC>(SetWindowLongPtrA(gameWindow, GWLP_WNDPROC, reinterpret_cast<LONG_PTR>(&WindowProc)));
        if (!originalWindowProc) { detached = true; Log("FAIL cannot register deferred command dispatcher"); return; }
        Log("READY deferred engine commands outside the active simulation frame");
    }
    FILE* file = nullptr;
    char path[MAX_PATH] = {};
    if (!Path(path, "qa-command.txt") || fopen_s(&file, path, "rb") || !file) return;
    char text[128] = {};
    const bool got = fgets(text, sizeof(text), file) != nullptr;
    fclose(file);
    if (!got) return;
    unsigned sequence = 0;
    char action[32] = {};
    char arg[64] = {};
    const int fields = sscanf_s(text, "%u %31s %63s", &sequence, action, static_cast<unsigned>(sizeof(action)), arg,
                                static_cast<unsigned>(sizeof(arg)));
    if (fields < 2 || sequence <= lastSequence) return;
    lastSequence = sequence;
    for (const char* c = arg; *c; ++c) {
        if (!(std::isalnum(static_cast<unsigned char>(*c)) || *c == '_' || *c == '-')) { Log("REFUSED argument: only [A-Za-z0-9_-]"); return; }
    }
    char line[160] = {};
    sprintf_s(line, "COMMAND sequence=%u action=%s%s%s thread=%lu", sequence, action, *arg ? " " : "", arg, GetCurrentThreadId());
    Log(line);
    strcpy_s(pendingAction, action);
    strcpy_s(pendingArg, arg);
    if (!PostMessageA(gameWindow, commandMessage, 0, 0)) { detached = true; Log("FAIL cannot queue deferred command"); }
}
}
