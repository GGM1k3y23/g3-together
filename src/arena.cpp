// SPDX-License-Identifier: GPL-3.0-or-later
#include "arena.h"

#include "ai.h"
#include "infocmd.h"
#include "log.h"
#include "net.h"
#include "npcs.h"

#include <Game.h>
#include <Script.h>
#include <Windows.h>
#include <cstdio>
#include <cstring>
#include <string>
#include <unordered_map>

namespace g3t::arena {
namespace {
// The Activator info of an arena or sparring quest ends with SendToArena(opponent, arena) for each of its delivery NPCs
// (Game gCInfo_PS::OnEndInfo, quest types 5 and 12, re/out/research-game.c). SendToArena (Script_Game 0xa7f80,
// re/out/arena-start-20261025-b.c) stops the opponent and continues its routine; SelectRoutineTask then picks ZS_Arena, which
// walks into the arena and attacks with reason Arena once Entity::GetPlayer stands in it (OnArena 0x11e60). A client's
// dialog did this to a mirror while the host simulated the opponent: Copper never went into the arena (arenastartdiag,
// 25.10.); the host's own dialog worked as in the single-player game. Taken over before the original runs, the opponent
// goes into the arena on the machine of the player who took the fight and fights only that player, as in the single-player
// game (owner 22.10.: only the two fighters count). A client takes it like a recruited companion (claim, no following), the
// host takes it back from the client (npcs::HostTake). The arena's status turns Running only when the quest does
// (gCQuest_PS::SetStatus, re/out/xp-world-native-c.c) and SendToArena set the arena as the opponent's destination point only
// for that call (re/out/arena-start2-20261025-b.c); Copper sat down again (runs 17:17, 17:21, a second ContinueRoutine
// alone did not help), so SendToArena runs once more when its quest runs here and its dialog is over.
constexpr uintptr_t SendToArenaRva = 0xa7f80;
gFScript original = nullptr;
bool installed = false, native = false;
DWORD lastTry = 0;
bool on = true;  // QA negative control: arenasyncoff
struct Held { std::string name; std::string arena; bool started = false; unsigned tries = 0; };
constexpr unsigned MaxTries = 20;  // half seconds
std::unordered_map<std::string, Held> held;  // GUID -> opponent taken over here
uint32_t nextCheck = 0;
constexpr uint32_t CheckEvery = 500;  // ms
unsigned sent = 0, taken = 0, started = 0, given = 0, readopted = 0;

unsigned char* NativeSendToArena()
{
    static const unsigned char expected[] = {0x55, 0x8b, 0xec, 0x83, 0xe4, 0xf8, 0x81, 0xec, 0x54, 0x01, 0x00, 0x00};
    HMODULE module = GetModuleHandleA("Script_Game.dll");
    if (!module) return nullptr;
    auto* code = reinterpret_cast<unsigned char*>(module) + SendToArenaRva;
    return std::memcmp(code, expected, sizeof(expected)) == 0 ? code : nullptr;
}

std::string Key(eCEntity* entity)
{
    const eCEntityProxy proxy(entity);
    return std::string(reinterpret_cast<char const*>(&proxy.GetEntityID().GetCore()), 16);
}

Entity Resolve(std::string const& key)
{
    _GUID raw;
    std::memcpy(&raw, key.data(), 16);
    return Entity(bCPropertyID(bCGuid(raw)));
}

// Whether a running arena or sparring quest names this NPC as its opponent.
bool Fights(eCEntity* npc)
{
    gCQuestManager_PS* manager = gCQuestManager_PS::GetInstance();
    if (!manager || !npc) return false;
    const bCString name = Entity(npc).GetName();
    auto const& quests = manager->GetQuests();
    for (GEInt qi = 0; qi < quests.GetCount(); ++qi) {
        gCQuest_PS const* quest = quests.GetAt(qi).operator->();
        if (!quest || quest->GetStatus().GetValue() != gEQuestStatus_Running) continue;
        const gEQuestType type = quest->GetType().GetValue();
        if (type != gEQuestType_Arena && type != gEQuestType_Sparring) continue;
        auto const& who = quest->GetDeliveryEntities();
        for (GEInt i = 0; i < who.GetCount(); ++i)
            if (who.GetAt(i) == name) return true;
    }
    return false;
}

// Still in its fight, beaten or beating: the arena tasks, the attack, a knock-out, the winner's search of the loser.
bool Busy(Entity& npc)
{
    char const* task = npc.Routine.GetCurrentTask().GetText();
    for (char const* part : {"ZS_Arena", "ZS_Attack", "ZS_Unconscious", "ZS_LieKnockDown", "ZS_PiercedKO", "ZS_RansackBody", "ZS_QuickStumble"})
        if (std::strstr(task, part)) return true;
    return false;
}

void Take(eCEntity* npc, eCEntity* arena)
{
    const std::string key = Key(npc);
    if (held.count(key) || !npcs::IsMirrored(npc) || npcs::Claimed(npc) || npcs::HostTaken(npc)) return;  // simulated here already
    if (net::IsHost() ? !npcs::HostTake(npc) : !infocmd::TakeOver(npc, true, false)) return;
    Entity e(npc);
    held[key] = {e.GetName().GetText(), arena ? Entity(arena).GetName().GetText() : ""};
    ++taken;
    Logf("arena: took over %s for this player's fight", e.GetName().GetText());
}

GEInt GE_STDCALL Detour(gCScriptProcessingUnit* spu, GELPVoid self, GELPVoid other, GEInt args)
{
    eCEntity* npc = self ? static_cast<Entity*>(self)->GetInstance() : (spu ? spu->GetSelfEntity() : nullptr);
    eCEntity* arena = other ? static_cast<Entity*>(other)->GetInstance() : (spu ? spu->GetOtherEntity() : nullptr);
    ++sent;
    if (on && npc && net::Connected()) Take(npc, arena);  // before the original: it stops the opponent and continues its routine here
    return original(spu, self, other, args);
}
}

void Tick()
{
    if (!gCSession::GetInstance().IsGameRunning()) return;
    if (!installed) {
        const DWORD tick = GetTickCount();
        if (tick - lastTry < 1000) return;
        lastTry = tick;
        gSScript* script = nullptr;
        if (!GetScriptAdminExt().GetScripts().GetAt(bCString("SendToArena"), script) || !script || !script->m_funcScript) return;
        if (unsigned char* code = NativeSendToArena(); code && reinterpret_cast<void*>(script->m_funcScript) == code) {
            original = reinterpret_cast<gFScript>(ai::JumpTo(code, reinterpret_cast<void*>(&Detour)));
            native = original != nullptr;
        }
        installed = true;
        Logf("arena: SendToArena %s", native ? "wrapped (native jump)" : "not wrapped (another build): a fight starts only where its opponent is simulated");
        return;
    }
    const uint32_t now = net::Now();
    if (held.empty() || static_cast<int32_t>(now - nextCheck) < 0) return;
    nextCheck = now + CheckEvery;
    // Given back once the fight is over: the quest won, lost or ended, and the opponent back on its feet.
    for (auto it = held.begin(); it != held.end();) {
        Entity e = Resolve(it->first);
        eCEntity* npc = e.GetInstance();
        if (!npc) { ++it; continue; }  // not loaded here now; kept until it is
        const bool ours = net::IsHost() ? npcs::HostTaken(npc) : npcs::Claimed(npc);
        Held& h = it->second;
        if (ours && !h.started && h.tries < MaxTries && Fights(npc) && !std::strstr(e.Routine.GetCurrentTask().GetText(), "ZS_Talk")) {
            Entity arena = Entity::GetEntity(bCString(h.arena.c_str()));
            if (!std::strstr(e.Routine.GetCurrentTask().GetText(), "ZS_Arena") && arena.GetInstance()) {
                if (!h.tries)
                    Logf("arena: %s in %s, arena %s status %d; sent there again", h.name.c_str(), e.Routine.GetCurrentTask().GetText(),
                         h.arena.c_str(), arena.GetInstance()->GetPropertySet(eEPropertySetType_Arena) ? static_cast<int>(arena.Arena.Status) : -1);
                GetScriptAdminExt().CallScript(bCString("SendToArena"), npc, arena.GetInstance(), 0);
                ++h.tries;
            }
            if (std::strstr(e.Routine.GetCurrentTask().GetText(), "ZS_Arena")) {
                h.started = true;
                ++started;
                Logf("arena: %s goes into the arena for this player (task %s)", h.name.c_str(), e.Routine.GetCurrentTask().GetText());
            }
        }
        if (ours && (Fights(npc) || Busy(e))) { ++it; continue; }
        if (ours) {
            if (net::IsHost()) npcs::HostRelease(npc);
            else infocmd::TakeOver(npc, false);
            ++given;
            Logf("arena: %s's fight is over (task %s); %s", it->second.name.c_str(), e.Routine.GetCurrentTask().GetText(),
                 net::IsHost() ? "the usual simulation rules again" : "given back to the host");
        }
        it = held.erase(it);  // or the host took it back (a revoked claim)
    }
}

bool Readopt(eCEntity* npc)
{
    if (!Fights(npc)) return false;
    infocmd::TakeOver(npc, false);
    ++readopted;
    Logf("arena: %s from the host's save is an arena opponent, not a companion; given back to the host", Entity(npc).GetName().GetText());
    return true;
}

void OnWorldChange() { held.clear(); }

void Report(LogFn log)
{
    char line[200];
    Format(line, "ARENASYNC on=%d installed=%d native=%d sent=%u taken=%u started=%u held=%zu given=%u readopted=%u", on ? 1 : 0,
              installed ? 1 : 0, native ? 1 : 0, sent, taken, started, held.size(), given, readopted);
    log(line);
}

void Off(LogFn log)
{
    on = false;
    log("ARENASYNCOFF ok=1");
}
}
