// SPDX-License-Identifier: GPL-3.0-or-later
#include "guide.h"

#include "infocmd.h"
#include "log.h"
#include "net.h"
#include "npcs.h"

#include <Game.h>
#include <Script.h>
#include <cstdio>
#include <cstring>
#include <string>
#include <unordered_map>
#include <vector>

namespace g3t::guide {
namespace {
// Static analysis (re/out/p8-check-20261024-c.c, p8-decompiled.c): StartGuidePlayer (Script_Game 0xa9110) sets
// GetPlayer().Party.Leader = the guide; SelectRoutineTask (0xa9440) chooses ZS_GuidePlayer only when the local hero's
// leader is the NPC, ZS_FollowPlayer only when the NPC's leader is the local hero; ZS_GuidePlayer_Wait measures the
// distance to GetPlayer. A quest the client takes sets these here, on a mirror, while the host simulates the NPC: Mitch
// never guided, the escaped slave never followed (followdiag G3_VIA=dialog, 25.10.). The same for the host with an NPC
// the client simulates (its region). Taken over, the player's own game guides and follows its hero; it waits only for the
// player who took the quest, as in the single-player game (owner 25.10.). A client takes the NPC like a recruited
// companion (claim), the host takes it back from the client (npcs::HostTake).
// A guide is read from the hero's leader every half second (the quest's start reaches StartGuidePlayer by a path not
// traced). Followers only from a join in a dialog here (CmdPartyJoin, CmdMobJoin, CmdSlaveJoin: info conditions 14, 28,
// 29, none a JoinPlayer line): a companion handed back by JoinPlayer false still has the hero as leader on the mirror
// for a moment and was taken again (stageE3, 25.10.).
enum Kind : uint8_t { Guide, Follower };
struct Held { Kind kind; std::string name; };
std::unordered_map<std::string, Held> held;  // GUID -> what this machine took over here
std::unordered_map<std::string, uint32_t> joined;  // GUID -> until when a slave join here is watched
constexpr uint32_t JoinWatch = 5000;  // ms
bool on = true;  // QA negative control: guidesyncoff
uint32_t nextCheck = 0;
constexpr uint32_t CheckEvery = 500;  // ms
unsigned guidesTaken = 0, followersTaken = 0, given = 0, readopted = 0, joinsStarted = 0;

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

eCEntity* LeaderOf(eCEntity* entity)
{
    if (!entity || !entity->GetPropertySet(eEPropertySetType_Party)) return nullptr;
    return Entity(entity).Party.GetPartyLeader().GetInstance();
}

// A guide taken over: its own routine again, chosen as at a dialog's end; with the local hero's leader on it,
// SelectRoutineTask makes it ZS_GuidePlayer.
void StartGuiding(Entity& npc)
{
    npc.Routine.FullStop();
    npc.Routine.ContinueRoutine();
}

void Take(eCEntity* npc, Kind kind)
{
    const std::string key = Key(npc);
    if (held.count(key) || npcs::Claimed(npc) || npcs::HostTaken(npc)) return;  // ours already (a recruited companion too)
    Entity e(npc);
    if (net::IsHost()) {
        if (!npcs::HostTake(npc)) return;
        if (kind == Follower) {
            e.Routine.FullStop();
            e.Routine.SetTask(bCString("ZS_FollowPlayer"));
        }
    } else if (!infocmd::TakeOver(npc, true, kind == Follower)) {
        return;
    }
    held[key] = {kind, e.GetName().GetText()};
    if (kind == Guide) {
        StartGuiding(e);
        ++guidesTaken;
    } else {
        ++followersTaken;
    }
    Logf("guide: took over %s (it %s this player), task %s", e.GetName().GetText(), kind == Guide ? "guides" : "follows",
         e.Routine.GetCurrentTask().GetText());
}

// Whether a running follow quest (type FollowNpc) is delivered by this NPC: the quest it guides in.
bool GuidesInQuest(eCEntity* npc)
{
    gCQuestManager_PS* manager = gCQuestManager_PS::GetInstance();
    if (!manager || !npc) return false;
    const bCString name = Entity(npc).GetName();
    auto const& quests = manager->GetQuests();
    for (GEInt qi = 0; qi < quests.GetCount(); ++qi) {
        gCQuest_PS const* quest = quests.GetAt(qi).operator->();
        if (!quest || quest->GetType().GetValue() != gEQuestType_FollowNpc || quest->GetStatus().GetValue() != gEQuestStatus_Running) continue;
        auto const& who = quest->GetDeliveryEntities();
        for (GEInt i = 0; i < who.GetCount(); ++i)
            if (who.GetAt(i) == name) return true;
    }
    return false;
}
}

void Joined(eCEntity* npc)
{
    if (on && npc) joined[Key(npc)] = net::Now() + JoinWatch;
}

void Tick()
{
    if (!on || !gCSession::GetInstance().IsGameRunning()) return;
    const uint32_t now = net::Now();
    if (static_cast<int32_t>(now - nextCheck) < 0) return;
    nextCheck = now + CheckEvery;
    eCEntity* hero = gCSession::GetInstance().GetHero();
    if (!hero) return;
    for (auto it = joined.begin(); it != joined.end();) {
        Entity e = Resolve(it->first);
        eCEntity* npc = e.GetInstance();
        if (npc && LeaderOf(npc) == hero) {
            if (npcs::IsMirrored(npc)) {
                if (net::Connected()) Take(npc, Follower);  // follows through the companion path
            } else {
                // Simulated here: what JoinPlayer true does after JoinPlayerSlavery (re/findings/party-members.md).
                e.Routine.FullStop();
                e.Routine.SetTask(bCString("ZS_FollowPlayer"));
                ++joinsStarted;
                Logf("guide: %s joined this player and follows", e.GetName().GetText());
            }
            it = joined.erase(it);
        } else if (static_cast<int32_t>(now - it->second) > 0) {
            it = joined.erase(it);
        } else {
            ++it;
        }
    }
    if (!net::Connected()) return;
    if (eCEntity* leader = LeaderOf(hero); leader && leader != hero && npcs::IsMirrored(leader)) Take(leader, Guide);
    // Given back once the relation ends: StopGuidePlayer at the destination, the slave's delivery, a dismissal.
    for (auto it = held.begin(); it != held.end();) {
        Entity e = Resolve(it->first);
        eCEntity* npc = e.GetInstance();
        if (!npc) { ++it; continue; }  // not loaded here now; kept until it is
        const bool ours = net::IsHost() ? npcs::HostTaken(npc) : npcs::Claimed(npc);
        const bool holds = it->second.kind == Guide ? LeaderOf(hero) == npc : LeaderOf(npc) == hero;
        if (holds && ours) { ++it; continue; }
        if (ours) {
            if (net::IsHost()) npcs::HostRelease(npc);
            else infocmd::TakeOver(npc, false);
            ++given;
            Logf("guide: %s no longer %s this player; %s", it->second.name.c_str(), it->second.kind == Guide ? "guides" : "follows",
                 net::IsHost() ? "the usual simulation rules again" : "given back to the host");
        }
        it = held.erase(it);  // or the host took it back (a revoked claim)
    }
}

bool Readopt(eCEntity* npc)
{
    if (!on || !GuidesInQuest(npc)) return false;
    // StartGuidePlayer as the quest's start runs it: the local hero's leader becomes the guide.
    GetScriptAdminExt().CallScript(bCString("StartGuidePlayer"), npc, nullptr, 0);
    Entity e(npc);
    held[Key(npc)] = {Guide, e.GetName().GetText()};
    StartGuiding(e);
    ++readopted;
    Logf("guide: %s guides this player again (from the host's save), task %s", e.GetName().GetText(), e.Routine.GetCurrentTask().GetText());
    return true;
}

void OnWorldChange()
{
    held.clear();
    joined.clear();
}

void Report(char const* name, LogFn log)
{
    char line[400];
    eCEntity* hero = gCSession::GetInstance().GetHero();
    eCEntity* leader = LeaderOf(hero);
    std::string about;
    if (name && *name) {
        Entity npc = Entity::GetEntity(bCString(name));
        eCEntity* instance = npc.GetInstance();
        eCEntity* its = LeaderOf(instance);
        char part[200];
        sprintf_s(part, " npc=%s found=%d leader=%s task=%s mirrored=%d claimed=%d", name, instance ? 1 : 0,
                  its ? Entity(its).GetName().GetText() : "-", instance ? npc.Routine.GetCurrentTask().GetText() : "-",
                  npcs::IsMirrored(instance) ? 1 : 0, npcs::Claimed(instance) ? 1 : 0);
        about = part;
    }
    sprintf_s(line, "GUIDE on=%d hero_leader=%s held=%zu guides_taken=%u followers_taken=%u given=%u readopted=%u joins_started=%u%s",
              on ? 1 : 0, leader ? Entity(leader).GetName().GetText() : "-", held.size(), guidesTaken, followersTaken, given, readopted,
              joinsStarted, about.c_str());
    log(line);
}

void Off(LogFn log)
{
    on = false;
    log("GUIDESYNCOFF ok=1");
}
}
