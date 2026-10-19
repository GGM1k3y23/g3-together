// SPDX-License-Identifier: GPL-3.0-or-later
#include "talk.h"

#include "body.h"
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

void OnTalk(uint8_t from, bool begin, uint8_t const guid[16])
{
    if (!net::IsHost() || holdDisabled) return;
    const std::string key = Key(guid);
    auto it = holds.find(key);
    if (begin) {
        if (it != holds.end()) { it->second.player = from; return; }
        _GUID raw;
        std::memcpy(&raw, guid, 16);
        Entity npc = Entity(bCPropertyID(bCGuid(raw)));
        if (!npc.GetInstance() || npc.IsDead()) return;
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

void Tick()
{
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
    if (partner == talkingWith) return;
    if (talkingWith) {
        net::SendTalk(false, talkingGuid);
        ++ends;
    }
    talkingWith = partner;
    if (partner) {
        Guid(partner, talkingGuid);
        net::SendTalk(true, talkingGuid);
        ++begins;
    }
}

bool TalkingWithHero(eCEntity const* entity) { return entity && entity == talkingWith; }

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
    sprintf_s(line, "TALKSYNC talking=%d partner=%s begins=%u ends=%u held=%zu held_names=%s holds_started=%u holds_ended=%u repacified=%u hold_disabled=%d",
              talkingWith != nullptr, talkingWith ? Entity(talkingWith).GetName().GetText() : "-", begins, ends, holds.size(),
              held.empty() ? "-" : held.c_str(), holdsStarted, holdsEnded, repacified, holdDisabled);
    log(line);
}

void DisableHold(LogFn log)
{
    holdDisabled = true;
    log("TALKHOLDOFF ok=1");
}
}
