// SPDX-License-Identifier: GPL-3.0-or-later
#include "talk.h"

#include "ai.h"
#include "body.h"
#include "hud.h"
#include "i18n.h"
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

namespace g3t::talk {
namespace {
// Client: the mirrored NPC our hero is talking to.
eCEntity* talkingWith = nullptr;
uint8_t talkingGuid[16] = {};
unsigned begins = 0, ends = 0;

// Host: NPCs held for a remote player's dialog.
struct Hold {
    Entity entity;
    body::State state;
    uint8_t player;
};
std::unordered_map<std::string, Hold> holds;
bool holdDisabled = false;
unsigned holdsStarted = 0, holdsEnded = 0, repacified = 0;
DWORD lastHostTick = 0;
// Phase 5 (25.10., dialogviewdiag): while a client talked to the host's NPC, the host could start a dialog of its own with it,
// two dialogs with one NPC at once. As in single player an NPC in a dialog is busy: the jump into the game's AssessTalk
// (Script_Game 0x30d50, self = the NPC, other = the one who addresses it) turns the host's hero away from a held NPC.
constexpr uintptr_t AssessTalkRva = 0x30d50;
gFScript originalAssessTalk = nullptr;
bool assessInstalled = false, assessNative = false;
DWORD lastAssessTry = 0, lastBusyMessage = 0;
unsigned busyRefused = 0;
// Gestures (owner 25.10.): the client's copy of its dialog partner runs the talk states (gestures, listening, facing the
// hero); the host's NPC stood still, held. The client sends each new motion of the partner with its facing (world state
// kind 15, the motion as the hash of its name, the name once through net::SendMotionName); the host plays it on the held NPC.
constexpr uint8_t GestureKind = 15;
bool gestures = true;  // QA negative control: gesturesoff
uint32_t lastGesture = 0;
DWORD lastGestureLook = 0;
std::unordered_map<uint32_t, bool> gestureNamesSent;
unsigned gesturesSent = 0, gesturesPlayed = 0, gesturesUnknown = 0;

std::string Key(uint8_t const guid[16]) { return std::string(reinterpret_cast<char const*>(guid), 16); }

void Guid(eCEntity* entity, uint8_t out[16])
{
    const eCEntityProxy proxy(entity);
    std::memcpy(out, &proxy.GetEntityID().GetCore(), 16);
}

gCInfoManager_PS* Infos() { return gCSession::GetInstance().IsGameRunning() ? gCInfoManager_PS::GetInstance() : nullptr; }

void Release(Hold& hold)
{
    body::Release(hold.entity, hold.state);
    ++holdsEnded;
}

unsigned char* NativeAssessTalk()
{
    static const unsigned char expected[] = {0x55, 0x8b, 0xec, 0x83, 0xe4, 0xf8, 0x81, 0xec, 0x5c, 0x05, 0x00, 0x00};
    HMODULE module = GetModuleHandleA("Script_Game.dll");
    if (!module) return nullptr;
    auto* code = reinterpret_cast<unsigned char*>(module) + AssessTalkRva;
    return std::memcmp(code, expected, sizeof(expected)) == 0 ? code : nullptr;
}

Hold const* HoldOf(eCEntity const* entity)
{
    for (auto const& entry : holds)
        if (entity && entry.second.entity.GetInstance() == entity) return &entry.second;
    return nullptr;
}

GEInt GE_STDCALL AssessTalkDetour(gCScriptProcessingUnit* spu, GELPVoid self, GELPVoid other, GEInt args)
{
    eCEntity* npc = self ? static_cast<Entity*>(self)->GetInstance() : (spu ? spu->GetSelfEntity() : nullptr);
    eCEntity* talker = other ? static_cast<Entity*>(other)->GetInstance() : (spu ? spu->GetOtherEntity() : nullptr);
    Hold const* hold = net::IsHost() && !holdDisabled ? HoldOf(npc) : nullptr;
    if (!hold || !talker || talker != gCSession::GetInstance().GetHero()) return originalAssessTalk(spu, self, other, args);
    ++busyRefused;
    const DWORD now = GetTickCount();
    if (now - lastBusyMessage > 3000) {
        lastBusyMessage = now;
        wchar_t who[64] = {}, player[64] = {}, text[200];
        MultiByteToWideChar(CP_UTF8, 0, Entity(npc).GetName().GetText(), -1, who, 64);
        MultiByteToWideChar(CP_UTF8, 0, net::PeerName(hold->player), -1, player, 64);
        swprintf_s(text, i18n::Text(i18n::NpcTalking), who, player[0] ? player : L"?");
        hud::Message(hud::Info, text);
        Logf("talk: %s talks to player %u; this player's address refused", Entity(npc).GetName().GetText(), hold->player);
    }
    return 0;
}

// Client: the partner's motion now, when it changed (looked at every 100 ms).
void SendGesture(eCEntity* partner)
{
    const DWORD now = GetTickCount();
    if (!gestures || now - lastGestureLook < 100) return;
    lastGestureLook = now;
    char motion[96];
    if (!body::CurrentMotion(partner, motion, sizeof(motion)) || !*motion) return;
    const uint32_t hash = net::HashName(motion);
    if (hash == lastGesture) return;
    lastGesture = hash;
    if (!gestureNamesSent[hash]) {
        net::SendMotionName(hash, motion);
        gestureNamesSent[hash] = true;
    }
    net::WorldState s{};
    s.kind = GestureKind;
    Guid(partner, s.guid);
    sprintf_s(s.routine, "%08x", hash);
    const bCQuaternion q = partner->GetWorldRotation();
    s.rotation[0] = q.GetX(); s.rotation[1] = q.GetY(); s.rotation[2] = q.GetZ(); s.rotation[3] = q.GetW();
    net::SendWorldState(s, 0);
    ++gesturesSent;
}


void OnTalk(uint8_t from, bool begin, uint8_t const guid[16])
{
    if (!net::IsHost() || holdDisabled) return;
    const std::string key = Key(guid);
    auto it = holds.find(key);
    if (begin) {
        if (it != holds.end()) { it->second.player = from; return; }
        Entity npc(npcs::NpcByGuid(guid));  // an NPC only, not the host's hero (Fable 1.7)
        if (!npc.GetInstance() || npc.IsDead()) return;
        // One dialog per player: a new hold ends that player's earlier one (one message per GUID held a whole town).
        for (auto other = holds.begin(); other != holds.end(); ++other)
            if (other->second.player == from) {
                Logf("talk: releasing %s (player %u talks to someone else)", other->second.entity.GetName().GetText(), from);
                Release(other->second);
                holds.erase(other);
                break;
            }
        Hold& hold = holds.emplace(key, Hold{npc, {}, from}).first->second;
        body::Pacify(hold.entity, hold.state);
        ++holdsStarted;
        Logf("talk: holding %s for player %u", npc.GetName().GetText(), from);
    } else if (it != holds.end()) {
        Logf("talk: releasing %s", it->second.entity.GetName().GetText());
        Release(it->second);
        holds.erase(it);
    }
}
}

void Start() { net::SetTalkHandler(&OnTalk); }

void OnGesture(uint8_t from, uint8_t const guid[16], uint32_t motion, float const rotation[4])
{
    if (!net::IsHost()) return;
    auto it = holds.find(Key(guid));
    if (it == holds.end() || it->second.player != from || !it->second.entity.GetInstance()) return;
    char const* name = npcs::MotionNameOf(motion);
    if (!name) { ++gesturesUnknown; return; }
    eCEntity* npc = it->second.entity.GetInstance();
    const bCVector& p = npc->GetWorldPosition();
    const float position[3] = {p.GetX(), p.GetY(), p.GetZ()};
    // As a mirror is driven (body::Drive): facing the client's hero, the motion replayed, and an action (a gesture) kept
    // for a while, so that the held body's idle task does not put its ambient loop back at once (InGesture, ai.cpp).
    const body::Target target{position, rotation, 0, name, 0.0f, -1};
    body::Drive(it->second.entity, it->second.state, target, net::Now());
    ++gesturesPlayed;
}

void Tick()
{
    if (!assessInstalled && gCSession::GetInstance().IsGameRunning() && GetTickCount() - lastAssessTry >= 1000) {
        lastAssessTry = GetTickCount();
        gSScript* script = nullptr;
        if (GetScriptAdminExt().GetScripts().GetAt(bCString("AssessTalk"), script) && script && script->m_funcScript) {
            if (unsigned char* code = NativeAssessTalk(); code && reinterpret_cast<void*>(script->m_funcScript) == code) {
                originalAssessTalk = reinterpret_cast<gFScript>(ai::JumpTo(code, reinterpret_cast<void*>(&AssessTalkDetour)));
                assessNative = originalAssessTalk != nullptr;
            }
            assessInstalled = true;
            Logf("talk: AssessTalk %s", assessNative ? "wrapped (an NPC in another player's dialog is busy)" : "not wrapped (another build)");
        }
    }
    if (net::IsHost()) {
        const DWORD now = GetTickCount();
        if (now - lastHostTick < 500) return;
        lastHostTick = now;
        net::Remote const* remotes = net::Remotes();
        for (auto it = holds.begin(); it != holds.end();) {
            Hold& hold = it->second;
            if (!hold.entity.GetInstance() || !remotes[hold.player].connected) {
                // The player left mid-dialog: the NPC goes back to its routine.
                Release(hold);
                it = holds.erase(it);
                continue;
            }
            if (std::strcmp(hold.entity.Routine.GetCurrentTask().GetText(), body::IdleTask()) != 0) {
                body::Pacify(hold.entity, hold.state);
                ++repacified;
            }
            ++it;
        }
        return;
    }
    gCInfoManager_PS* manager = Infos();
    eCEntity* partner = nullptr;
    if (manager && manager->AccessIsRunning()) {
        gCEntity* npc = manager->GetDialogPartnerNPC();
        if (npc && npcs::IsMirrored(npc)) partner = npc;
    }
    if (partner && partner == talkingWith) {
        SendGesture(partner);
        return;
    }
    if (partner == talkingWith) return;
    lastGesture = 0;
    if (talkingWith) {
        net::SendTalk(false, talkingGuid);
        ++ends;
    }
    talkingWith = partner;
    if (partner) {
        Guid(partner, talkingGuid);
        net::SendTalk(true, talkingGuid);
        ++begins;
        // The copy turns to our hero and takes the talk state, as AssessTalk does with an NPC of this machine: a mirror's
        // AI mode is "abandoned" (driven), so the dialog left it in its idle task, looking away, without its talk and
        // listen motions (stageNpcGestures 25.10.); ai.cpp runs ZS_Talk for our dialog partner.
        if (gestures) {
            Entity npc(partner);
            npc.NPC.SetCurrentTarget(Entity(gCSession::GetInstance().GetHero()));
            npc.Routine.AIMode = gEAIMode_Talk;
            npc.Routine.SetTask(bCString("ZS_Talk"));
            Logf("talk: %s takes the talk state here (task %s)", npc.GetName().GetText(), npc.Routine.GetCurrentTask().GetText());
        }
    }
}

bool TalkingWithHero(eCEntity const* entity) { return entity && entity == talkingWith; }

bool InGesture(eCEntity const* entity)
{
    for (auto const& entry : holds)
        if (entity && entry.second.entity.GetInstance() == entity)
            return static_cast<int32_t>(entry.second.state.actionUntil - net::Now()) > 0;
    return false;
}

void OnWorldChange()
{
    holds.clear();
    talkingWith = nullptr;
}

bool IsHeld(eCEntity const* entity)
{
    for (auto const& entry : holds)
        if (entity && entry.second.entity.GetInstance() == entity) return true;
    return false;
}

void Choose(int index, LogFn log)
{
    char line[200];
    gCInfoManager_PS* manager = Infos();
    gCInfo_PS* info = manager && manager->AccessIsRunning() ? manager->GetAvailableInfo(index) : nullptr;
    if (!info) { log("REFUSED talkchoose: no dialog or no such option"); return; }
    const GEBool ok = manager->ExecuteDialogInfo(info);
    sprintf_s(line, "TALKCHOOSE index=%d name=%s ok=%d", index, info->AccessName().GetText(), ok ? 1 : 0);
    log(line);
}

// QA, stage H3: run a named dialog option in the running dialog (its conditions not checked), as if chosen.
void ChooseInfo(char const* name, LogFn log)
{
    char line[200];
    gCInfoManager_PS* manager = Infos();
    gCInfo_PS* info = manager && manager->AccessIsRunning() ? manager->GetInfo(bCString(name)) : nullptr;
    if (!info) { sprintf_s(line, "REFUSED talkinfo %s: no dialog or no such info", name); log(line); return; }
    const GEBool ok = manager->ExecuteDialogInfo(info);
    sprintf_s(line, "TALKINFO name=%s ok=%d", name, ok ? 1 : 0);
    log(line);
}

void Report(LogFn log)
{
    char line[260];
    std::string held;
    for (auto const& entry : holds) held += std::string(held.empty() ? "" : ",") + entry.second.entity.GetName().GetText();
    sprintf_s(line, "TALKSYNC talking=%d partner=%s begins=%u ends=%u held=%zu held_names=%s holds_started=%u holds_ended=%u repacified=%u hold_disabled=%d busy_refused=%u assess_wrapped=%d gestures_sent=%u gestures_played=%u gestures_unknown=%u",
              talkingWith != nullptr, talkingWith ? Entity(talkingWith).GetName().GetText() : "-", begins, ends, holds.size(),
              held.empty() ? "-" : held.c_str(), holdsStarted, holdsEnded, repacified, holdDisabled, busyRefused, assessNative ? 1 : 0, gesturesSent,
              gesturesPlayed, gesturesUnknown);
    log(line);
}

void DisableHold(LogFn log)
{
    holdDisabled = true;
    log("TALKHOLDOFF ok=1");
}

void GesturesOff(LogFn log)
{
    gestures = false;
    log("GESTURESOFF ok=1");
}
}
