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
#include "trade.h"
#include "mapmarks.h"
#include "ransack.h"
#include "witness.h"
#include "pickpocket.h"
#include "occupancy.h"
#include "dialogaudio.h"
#include "explore.h"
#include "npcs.h"
#include "offer.h"
#include "downed.h"
#include "transform.h"
#include "ranged.h"
#include "shot.h"
#include "spells.h"
#include "summon.h"
#include "hud.h"
#include "range.h"
#include "journal.h"
#include "persist.h"
#include "hero.h"
#include "clock.h"
#include "infocmd.h"
#include "remote.h"
#include "trace.h"
#include "world.h"
#include "social.h"
#include "enclaves.h"
#include "doors.h"
#include "net.h"

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

// QA (stageArtifactFound): the hero receives an item as a pickup or a dialog's Give does it: into the inventory, then the
// quest manager's OnReceiveItem (CreateItems alone fulfilled no quest that wants the hero to hold the item, 24.10.).
void ReceiveItem()
{
    char line[200];
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    gCQuestManager_PS* quests = gCQuestManager_PS::GetInstance();
    const Template item(argument);
    if (!hero || !quests || !item.IsValid()) { Log("REFUSED receiveitem: no hero, no quest manager or no such item"); return; }
    const GEInt stack = Entity(hero).Inventory.CreateItems(item, 0, 1);
    bCPropertyID const& id = item;
    const GEBool told = quests->OnReceiveItem(nullptr, hero, id, 1);
    sprintf_s(line, "RECEIVEITEM item=%s stack=%d told=%d", argument, stack, told ? 1 : 0);
    Log(line);
}

// Codex review 22.10. (R1, R8, R9): a third chain, the other two are at MSVC's limit of nested blocks (C1061).
bool RunReviewAction(const char* action)
{
    if (!std::strcmp(action, "herocut")) {
        g3t::hero::CutCommand(argument, &Log);
    } else if (!std::strcmp(action, "recordcheckoff")) {
        g3t::hero::CheckOff(&Log);
    } else if (!std::strcmp(action, "recordwritefault")) {
        g3t::hero::WriteFault(&Log);
    } else if (!std::strcmp(action, "storyzerosoff")) {
        g3t::story::ZerosOff(&Log);
    } else if (!std::strcmp(action, "hitnpcburst")) {
        g3t::combat::HitNpcBurst(argument, &Log);
    } else if (!std::strcmp(action, "killonceoff")) {
        g3t::combat::KillOnceOff(&Log);
    } else if (!std::strcmp(action, "killconfirmoff")) {
        g3t::combat::KillConfirmOff(&Log);
    } else if (!std::strcmp(action, "killreport")) {
        g3t::combat::KillReport(&Log);
    } else if (!std::strcmp(action, "recordwaitoff")) {
        g3t::persist::RecordWaitOff(&Log);
    } else if (!std::strcmp(action, "recordwait")) {
        g3t::persist::Waiting(&Log);
    } else if (!std::strcmp(action, "helloversion")) {  // stage W7: the next hello claims this protocol version
        g3t::net::ClaimVersionForTest(static_cast<uint16_t>(std::atoi(argument)));
        Log("HELLOVERSION ok=1");
    } else if (!std::strcmp(action, "mirrorretryoff")) {
        g3t::npcs::MirrorRetryOff(&Log);
    } else if (!std::strcmp(action, "mirrorstats")) {
        g3t::npcs::MirrorStats(&Log);
    } else if (!std::strcmp(action, "summonlatecopy")) {
        g3t::summon::LateCopyCommand(argument, &Log);
    } else if (!std::strcmp(action, "skillreplaceoff")) {
        g3t::hero::SkillReplaceOff(&Log);
    } else if (!std::strcmp(action, "hasskill")) {
        g3t::hero::HasSkill(argument, &Log);
    } else if (!std::strcmp(action, "skilldiag")) {
        g3t::hero::SkillDiag(&Log);
    } else if (!std::strcmp(action, "skilltry")) {
        g3t::hero::SkillTry(&Log);
    } else if (!std::strcmp(action, "offersend")) {  // stage W10: player trade offers (offer.cpp)
        g3t::offer::SendCommand(argument, &Log);
    } else if (!std::strcmp(action, "offeraccept")) {
        g3t::offer::Decide(true, &Log);
    } else if (!std::strcmp(action, "offerreject")) {
        g3t::offer::Decide(false, &Log);
    } else if (!std::strcmp(action, "offerstate")) {
        g3t::offer::Report(&Log);
    } else if (!std::strcmp(action, "offerpartial")) {
        g3t::offer::PartialOn(&Log);
    } else if (!std::strcmp(action, "heroadd")) {
        g3t::offer::AddCommand(argument, &Log);
    } else if (!std::strcmp(action, "herocount")) {
        g3t::offer::CountCommand(argument, &Log);
    } else if (!std::strcmp(action, "keycheck")) {
        g3t::offer::KeyCheck(&Log);
    } else if (!std::strcmp(action, "menutext")) {
        g3t::hud::Probe(argument, &Log);
    } else if (!std::strcmp(action, "menuline")) {
        g3t::hud::MenuLineReport(&Log);
    } else if (!std::strcmp(action, "stacks")) {
        g3t::containers::Stacks(argument, &Log);
    } else if (!std::strcmp(action, "holditem")) {
        g3t::containers::HoldItem(argument, &Log);
    } else {
        return false;
    }
    return true;
}

// Commands added after the chain below reached MSVC's limit of nested blocks (C1061).
// Phase 1b/2 (B, 23.10.): glide, what puppets wear and look like, talking and trading with them. Apart from the long chain
// in RunAction, which reached the compiler's nesting limit.
bool RunPlayerAction(const char* action)
{
    if (!std::strcmp(action, "puppettradeon")) {
        g3t::remote::PuppetTradeOn(argument, &Log);
    } else if (!std::strcmp(action, "puppetinfo")) {
        g3t::remote::PuppetInfo(argument, &Log);
    } else if (!std::strcmp(action, "lookoff")) {
        g3t::remote::LookOff(&Log);
    } else if (!std::strcmp(action, "wear")) {
        g3t::remote::WearHero(argument, &Log);
    } else if (!std::strcmp(action, "wornscan")) {
        g3t::remote::WornScan(&Log);
    } else if (!std::strcmp(action, "wearoff")) {
        g3t::remote::WearOff(&Log);
    } else if (!std::strcmp(action, "glidestopoff")) {
        g3t::body::GlideStopOff(&Log);
    } else if (!std::strcmp(action, "glideold")) {
        g3t::body::GlideOld(&Log);
    } else if (!std::strcmp(action, "glidereset")) {
        g3t::body::GlideReset(&Log);
    } else if (!std::strcmp(action, "glide")) {
        g3t::body::GlideReport(&Log);
    } else if (!std::strcmp(action, "tradesync")) {
        g3t::trade::Report(&Log);
    } else if (!std::strcmp(action, "autohelpon")) {
        g3t::downed::AutoHelpOn(&Log);
    } else if (!std::strcmp(action, "helpoptionoff")) {
        g3t::downed::HelpOptionOff(&Log);
    } else if (!std::strcmp(action, "tradeclose")) {
        g3t::trade::CloseCommand(&Log);
    } else if (!std::strcmp(action, "tradeholdoff")) {
        g3t::trade::HoldOff(&Log);
    } else if (!std::strcmp(action, "murdercand")) {
        g3t::enclaves::MurderCandidates(argument, &Log);
    } else if (!std::strcmp(action, "creditqueueoff")) {
        g3t::enclaves::CreditQueueOff(&Log);
    } else if (!std::strcmp(action, "credithold")) {
        g3t::enclaves::CreditHold(argument, &Log);
    } else if (!std::strcmp(action, "townstatusoff")) {
        g3t::enclaves::TownStatusOff(&Log);
    } else if (!std::strcmp(action, "townfarkill")) {
        g3t::enclaves::TownFarKill(argument, &Log);
    } else if (!std::strcmp(action, "cheatkill")) {
        g3t::enclaves::CheatKill(argument, &Log);
    } else if (!std::strcmp(action, "occupancy")) {
        g3t::occupancy::Report(&Log);
    } else if (!std::strcmp(action, "usesyncoff")) {
        g3t::occupancy::Off(&Log);
    } else if (!std::strcmp(action, "unuse")) {
        g3t::occupancy::StandUp(&Log);
    } else if (!std::strcmp(action, "npcroutine")) {
        g3t::explore::NpcRoutine(argument, &Log);
    } else if (!std::strcmp(action, "findents")) {
        g3t::world::FindEntities(argument, &Log);
    } else if (!std::strcmp(action, "weatherfollowoff")) {
        g3t::summon::WeatherFollowOff(&Log);
    } else if (!std::strcmp(action, "weather")) {
        g3t::summon::WeatherReport(&Log);
    } else if (!std::strcmp(action, "receiveitem")) {
        ReceiveItem();
    } else if (!std::strcmp(action, "pickcands")) {
        g3t::pickpocket::Candidates(&Log);
    } else if (!std::strcmp(action, "pickinfo")) {
        g3t::pickpocket::Info(argument, &Log);
    } else if (!std::strcmp(action, "pickpocket")) {
        g3t::pickpocket::Pick(argument, &Log);
    } else if (!std::strcmp(action, "thiefprep")) {
        g3t::pickpocket::ThiefPrep(&Log);
    } else if (!std::strcmp(action, "pickpocketoff")) {
        g3t::pickpocket::Off(&Log);
    } else if (!std::strcmp(action, "picksync")) {
        g3t::pickpocket::Report(&Log);
    } else if (!std::strcmp(action, "remotemurdersoff")) {
        g3t::enclaves::RemoteMurdersOff(&Log);
    } else if (!std::strcmp(action, "shapehitsoff")) {
        g3t::combat::ShapeHitsOff(&Log);
    } else if (!std::strcmp(action, "remotekooff")) {
        g3t::combat::RemoteKnockOutsOff(&Log);
    } else if (!std::strcmp(action, "questdump")) {
        char path[MAX_PATH] = {};
        if (Path(path, "quests.tsv")) g3t::explore::QuestDump(path, &Log);
    } else if (!std::strcmp(action, "ransack")) {
        g3t::ransack::Report(&Log);
    } else if (!std::strcmp(action, "ransackoff")) {
        g3t::ransack::Off(&Log);
    } else if (!std::strcmp(action, "npcgold")) {
        g3t::ransack::NpcGold(argument, &Log);
    } else if (!std::strcmp(action, "subtitlesoff")) {
        g3t::dialogaudio::SubtitlesOff(&Log);
    } else if (!std::strcmp(action, "witness")) {
        g3t::witness::Saw(argument, &Log);
    } else if (!std::strcmp(action, "witnesses")) {
        g3t::witness::Report(&Log);
    } else if (!std::strcmp(action, "witnessoff")) {
        g3t::witness::Off(&Log);
    } else if (!std::strcmp(action, "assesshero")) {
        g3t::explore::AssessHero(argument, &Log);
    } else if (!std::strcmp(action, "mapmarks")) {
        g3t::mapmarks::Report(&Log);
    } else if (!std::strcmp(action, "mapmarksoff")) {
        g3t::mapmarks::Off(&Log);
    } else if (!std::strcmp(action, "mapopen")) {
        g3t::mapmarks::OpenMap(&Log);
    } else if (!std::strcmp(action, "mapclose")) {
        g3t::mapmarks::CloseMap(&Log);
    } else if (!std::strcmp(action, "tradequalityoff")) {
        g3t::trade::QualityOff(&Log);
    } else if (!std::strcmp(action, "givequality")) {
        g3t::trade::GiveQuality(argument, &Log);
    } else if (!std::strcmp(action, "qualitycount")) {
        g3t::trade::QualityCount(argument, &Log);
    } else if (!std::strcmp(action, "tradesell")) {
        g3t::containers::TradeSell(argument, &Log);
    } else if (!std::strcmp(action, "tradeprice")) {
        g3t::trade::Price(argument, &Log);
    } else if (!std::strcmp(action, "tradepricesoff")) {
        g3t::trade::PricesOff(&Log);
    } else if (!std::strcmp(action, "tradefilloff")) {
        g3t::trade::FillOff(&Log);
    } else if (!std::strcmp(action, "playergoods")) {
        g3t::trade::GoodsReport(argument, &Log);
    } else {
        return false;
    }
    return true;
}

bool RunLaterAction(const char* action)
{
    if (!std::strcmp(action, "world")) {
        g3t::persist::WorldReport(&Log);
    } else if (!std::strcmp(action, "worldsyncoff")) {
        g3t::persist::WorldSyncOff(&Log);
    } else if (!std::strcmp(action, "reconnect")) {
        g3t::net::Reconnect(&Log);
    } else if (!std::strcmp(action, "generationoff")) {
        g3t::persist::GenerationOff(&Log);
    } else if (!std::strcmp(action, "persistpause")) {
        g3t::persist::Pause(argument, &Log);
    } else if (!std::strcmp(action, "worldgatelate")) {
        g3t::persist::WorldGateLate(&Log);
    } else if (!std::strcmp(action, "loadticksoff")) {
        g3t::persist::LoadTicksOff(argument, &Log);
    } else if (!std::strcmp(action, "dialogxpoff")) {
        g3t::infocmd::DialogXpOff(&Log);
    } else if (!std::strcmp(action, "duelhero")) {
        g3t::explore::DuelHero(argument, &Log);
    } else if (!std::strcmp(action, "lethalonlyoff")) {
        g3t::downed::LethalOnlyOff(&Log);
    } else if (!std::strcmp(action, "arenaruleoff")) {
        g3t::combat::ArenaRuleOff(&Log);
    } else if (!std::strcmp(action, "arena")) {
        g3t::combat::ArenaReport(argument, &Log);
    } else if (!std::strcmp(action, "npchp")) {
        g3t::combat::NpcHp(argument, &Log);
    } else if (!std::strcmp(action, "xpinfos")) {
        g3t::explore::XpInfos(&Log);
    } else if (!std::strcmp(action, "forgetnpclist")) {
        g3t::remote::ForgetNpcListOn(&Log);
    } else if (!std::strcmp(action, "contactsoff")) {
        g3t::remote::ContactsOff(&Log);
    } else if (!std::strcmp(action, "badtransfer")) {
        g3t::persist::BadTransfer(&Log);
    } else if (!std::strcmp(action, "badrecord")) {
        g3t::npcs::BadRecord(&Log);
    } else if (!std::strcmp(action, "badstates")) {
        g3t::remote::BadStates(argument, &Log);
    } else if (!std::strcmp(action, "reacthitsoff")) {
        g3t::combat::ReactHitsOff(&Log);
    } else if (!std::strcmp(action, "keeplocaloff")) {
        g3t::enclaves::KeepLocalOff(&Log);
    } else if (!std::strcmp(action, "enclavescanpause")) {
        g3t::enclaves::PauseScan(argument, &Log);
    } else if (!std::strcmp(action, "crimeadd")) {
        g3t::enclaves::AddCrime(argument, &Log);
    } else if (!std::strcmp(action, "npcsend")) {
        g3t::npcs::SendInfo(argument, &Log);
    } else if (!std::strcmp(action, "joingraceoff")) {
        g3t::npcs::JoinGraceOff(&Log);
    } else if (!std::strcmp(action, "contestoff")) {
        g3t::npcs::ContestOff(&Log);
    } else if (!std::strcmp(action, "claimrecord")) {
        g3t::npcs::ClaimRecord(argument, &Log);
    } else if (!std::strcmp(action, "edgenpcs")) {
        g3t::npcs::EdgeReport(&Log);
    } else if (!std::strcmp(action, "hostwinsoff")) {
        g3t::npcs::HostWinsOff(&Log);
    } else if (!std::strcmp(action, "doors")) {
        g3t::doors::Report(&Log);
    } else if (!std::strcmp(action, "doorsyncoff")) {
        g3t::doors::SyncOff(&Log);
    } else if (!std::strcmp(action, "owned")) {
        g3t::social::Owned(argument, &Log);
    } else if (!std::strcmp(action, "enclavesync")) {
        g3t::enclaves::Report(&Log);
    } else if (!std::strcmp(action, "enclavesyncoff")) {
        g3t::enclaves::SyncOff(&Log);
    } else if (!std::strcmp(action, "unarm")) {
        g3t::social::Unarm(&Log);
    } else if (!std::strcmp(action, "enclaves")) {
        g3t::social::Enclaves(&Log);
    } else if (!std::strcmp(action, "crime")) {
        g3t::social::Crime(argument, &Log);
    } else if (!std::strcmp(action, "usables")) {
        g3t::world::Usables(argument, &Log);
    } else if (!std::strcmp(action, "face")) {
        g3t::world::Face(argument, &Log);
    } else if (!std::strcmp(action, "use")) {
        g3t::world::Use(argument, &Log);
    } else if (!std::strcmp(action, "unlock")) {
        g3t::world::Unlock(argument, &Log);
    } else if (!std::strcmp(action, "useinfo")) {
        g3t::world::UseInfo(argument, &Log);
    } else if (!std::strcmp(action, "attitude")) {
        g3t::explore::Attitude(argument, &Log);
    } else if (!std::strcmp(action, "continueroutine")) {
        g3t::ai::ContinueRoutineReport(&Log);
    } else if (!std::strcmp(action, "continueroutineon")) {
        g3t::ai::ContinueRoutineOn(&Log);
    } else if (!std::strcmp(action, "movelength")) {
        g3t::ai::MoveLengthReport(&Log);
    } else if (!std::strcmp(action, "movelengthoff")) {
        g3t::ai::MoveLengthOff(&Log);
    } else if (!std::strcmp(action, "scriptfind")) {
        g3t::ai::FindByRva(argument, &Log);
    } else if (!std::strcmp(action, "heropos")) {
        g3t::explore::HeroPos(&Log);
    } else if (!std::strcmp(action, "hitpuppet")) {
        g3t::combat::HitPuppet(argument, &Log);
    } else if (!std::strcmp(action, "lookat")) {
        g3t::explore::LookAt(argument, &Log);
    } else if (!std::strcmp(action, "deadposeoff")) {
        g3t::npcs::DeadPoseOff(&Log);
    } else if (!std::strcmp(action, "deadpose")) {
        g3t::npcs::DeadPoseReport(&Log);
    } else if (!std::strcmp(action, "trace")) {
        g3t::trace::Command(argument, &Log);
    } else if (!std::strcmp(action, "partnerjump")) {
        g3t::partner::JumpQa(&Log);
    } else if (!std::strcmp(action, "partyxpoff")) {
        g3t::combat::PartyXpOff(&Log);
    } else if (!std::strcmp(action, "npcrecvon")) {
        g3t::npcs::RecvOn(&Log);
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
    } else if (!std::strcmp(action, "creatures")) {
        g3t::summon::Creatures(&Log);
    } else if (!std::strcmp(action, "areatickon")) {
        g3t::summon::AreaTickOn(&Log);
    } else if (!std::strcmp(action, "summonmirroroff")) {
        g3t::summon::MirrorOff(&Log);
    } else if (!std::strcmp(action, "shotstate")) {
        g3t::shot::Report(&Log);
    } else if (!std::strcmp(action, "shotcopyhero")) {
        g3t::shot::CopyHeroOff(&Log);
    } else if (!std::strcmp(action, "spellstate")) {
        g3t::spells::Report(&Log);
    } else if (!std::strcmp(action, "spellmirroroff")) {
        g3t::spells::MirrorOff(&Log);
    } else if (!std::strcmp(action, "statusof")) {
        g3t::spells::StatusOf(argument, &Log);
    } else if (!std::strcmp(action, "tickguardoff")) {
        g3t::spells::TickGuardOff(&Log);
    } else if (!std::strcmp(action, "spawnnpc")) {
        g3t::spells::SpawnNpc(argument, &Log);
    } else if (!std::strcmp(action, "party")) {
        g3t::spells::Party(&Log);
    } else if (!std::strcmp(action, "npcmemory")) {
        g3t::spells::NpcMemory(argument, &Log);
    } else if (!std::strcmp(action, "npcgrudge")) {
        g3t::spells::NpcGrudge(argument, &Log);
    } else if (!std::strcmp(action, "puppetguid")) {
        g3t::spells::PuppetGuid(argument, &Log);
    } else if (!std::strcmp(action, "locks")) {
        g3t::spells::Locks(&Log);
    } else if (!std::strcmp(action, "sethp")) {
        g3t::spells::SetHp(argument, &Log);
    } else if (!std::strcmp(action, "touchlog")) {
        g3t::shot::TouchLog(&Log);
    } else if (!std::strcmp(action, "shotmirroroff")) {
        g3t::shot::MirrorOff(&Log);
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
    } else if (!std::strcmp(action, "dialogaudio")) {
        g3t::dialogaudio::Report(&Log);
    } else if (!std::strcmp(action, "mutegame")) {
        g3t::dialogaudio::MuteGame(argument, &Log);
    } else if (!std::strcmp(action, "dialogaudiooff")) {
        g3t::dialogaudio::Disable(&Log);
    } else if (!std::strcmp(action, "spellfunc")) {
        g3t::spells::SpellFunc(argument, &Log);
    } else if (!std::strcmp(action, "quickloot")) {  // "quickloot" queues the action, "quickloot count" only counts
        if (!std::strcmp(argument, "count")) g3t::containers::QuickLootActions(&Log);
        else g3t::containers::QuickLoot(&Log);
    } else {
        return false;
    }
    return true;
}

void RunAction(const char* action)
{
    gCSession& session = gCSession::GetInstance();
    char line[256] = {};
    if (RunLaterAction(action) || RunReviewAction(action) || RunPlayerAction(action)) return;
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
    } else if (!std::strcmp(action, "invstate")) {
        g3t::containers::InvState(argument, &Log);
    } else if (!std::strcmp(action, "chests")) {
        g3t::containers::Chests(&Log);
    } else if (!std::strcmp(action, "lootsilent")) {
        g3t::containers::Silent(argument, &Log);
    } else if (!std::strcmp(action, "containersync")) {
        g3t::containers::Report(&Log);
    } else if (!std::strcmp(action, "containersyncoff")) {
        g3t::containers::Disable(argument, &Log);
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
    } else if (!std::strcmp(action, "scriptdump")) {
        char path[MAX_PATH] = {};
        if (Path(path, "scripts.tsv")) g3t::ai::DumpNames(path, &Log);
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
