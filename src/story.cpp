// SPDX-License-Identifier: GPL-3.0-or-later
#include "story.h"

#include "body.h"
#include "combat.h"
#include "log.h"
#include "net.h"
#include "npcs.h"
#include "remote.h"

#include <Game.h>
#include <Script.h>
#include <Windows.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace g3t::story {
namespace {
// QA: an NPC by name, or "puppet<id>" for that player's puppet (its template name, PC_Hero, finds the hero itself).
Entity Named(char const* name)
{
    return !std::strncmp(name, "puppet", 6) ? Entity(remote::PuppetOf(std::atoi(name + 6))) : Entity::GetEntity(bCString(name));
}
}

namespace {
constexpr DWORD PollInterval = 500;
using States = std::unordered_map<std::string, uint8_t>;

struct Domain {
    uint8_t kind;
    char const* label;
    bool (*ready)();
    void (*snapshot)(States&);
    bool (*apply)(char const* name, uint8_t value);  // true if the game's own function did it, false if set directly
    States known;  // host: values last sent; client: the host's values as far as this client knows them
    // Host: names that went back to 0 while the game ran here. The full state sends their zeros: a client that loads the
    // host's save got them as set, since the save is older (Codex review 22.10. R8). All zeros would be thousands of infos.
    std::unordered_set<std::string> zeroed;
    bool primed = false;
    bool wasConnected = false;
    unsigned sent = 0, received = 0, applied = 0, forced = 0, claims = 0, requests = 0, skipped = 0;
};

bool disabled = false;
bool fullZeros = true;  // QA negative control (host, stage W4): storyzerosoff (the full state without zeros, as before 23.10.)
DWORD lastPoll = 0;

void Track(Domain& d, std::string const& name, uint8_t value)
{
    if (!net::IsHost()) return;
    auto it = d.known.find(name);
    if (value) d.zeroed.erase(name);
    else if (it != d.known.end() && it->second) d.zeroed.insert(name);
}
bool Running() { return gCSession::GetInstance().IsGameRunning(); }

// --- quests ---
gCQuestManager_PS* Quests() { return Running() ? gCQuestManager_PS::GetInstance() : nullptr; }
uint8_t StatusOf(gCQuest_PS* quest) { return static_cast<uint8_t>(static_cast<gEQuestStatus const&>(quest->AccessStatus())); }
bool QuestsReady() { return Quests() != nullptr; }

void QuestSnapshot(States& states)
{
    auto& all = Quests()->AccessQuests();
    for (int i = 0; i < all.GetCount(); ++i)
        if (gCQuest_PS* quest = static_cast<gCQuest_PS*>(all.AccessAt(i))) states[quest->AccessName().GetText()] = StatusOf(quest);
}

// Through the game's own functions where one exists (journal entries, experience), else set directly.
bool QuestApply(char const* name, uint8_t status)
{
    gCQuestManager_PS* manager = Quests();
    const bCString id(name);
    gCQuest_PS* quest = manager->GetQuest(id);
    if (!quest || StatusOf(quest) == status) return true;
    const gEQuestStatus target = static_cast<gEQuestStatus>(status);
    if ((target == gEQuestStatus_Success || target == gEQuestStatus_Failed) && StatusOf(quest) == gEQuestStatus_Open)
        manager->RunQuest(id);
    if (target == gEQuestStatus_Running) manager->RunQuest(id);
    else if (target == gEQuestStatus_Success) manager->SucceedQuest(id);
    else if (target == gEQuestStatus_Failed) manager->FailQuest(id);
    if (StatusOf(quest) == status) return true;
    quest->SetStatus(target);
    return false;
}

// --- dialog options ---
gCInfoManager_PS* Infos() { return Running() ? gCInfoManager_PS::GetInstance() : nullptr; }
bool InfosReady() { return Infos() != nullptr; }

void InfoSnapshot(States& states)
{
    auto& all = Infos()->AccessInfos();
    for (int i = 0; i < all.GetCount(); ++i)
        if (gCInfo_PS* info = all.AccessAt(i)) states[info->AccessName().GetText()] = info->AccessInfoGiven() ? 1 : 0;
}

bool InfoApply(char const* name, uint8_t given)
{
    if (gCInfo_PS* info = Infos()->GetInfo(bCString(name))) info->AccessInfoGiven() = given != 0;
    return false;  // no game function marks a dialog as told without running it
}

// --- story events the hero knows ---
// Dialogs set them on the hero who holds the dialog (SetGameEvent / ClearGameEvent, kept in the hero's
// PlayerMemory.PlayerKnows); other dialogs check them. Shared like quests, so that every hero knows what one
// learned (the client's Diego dialog set Diego_WarIsLost on the client's hero only; stage H exploration).
std::unordered_map<std::string, uint8_t> everSeen;  // events that were set at some point: 0 once cleared
// The chapter lives beside them in the hero's PlayerMemory (SetChapter, info command 53: 2 once a god is chosen) and decides
// attitudes through Entity::GetPlayer (re/out/notify-enclave-predicate-20261024-c.c): shared as one more entry of this kind.
// When the client chose Innos, the host's hero stayed in chapter 1 (storyinnosdiag, 25.10.).
constexpr char ChapterEntry[] = "G3T_Chapter";
bool chapterSharing = true;  // QA negative control: chaptersyncoff
bool EventsReady() { return Running() && gCSession::GetInstance().GetHero(); }

void EventSnapshot(States& states)
{
    for (auto& entry : everSeen) entry.second = 0;
    eCEntity* hero = gCSession::GetInstance().GetHero();
    auto* memory = static_cast<gCPlayerMemory_PS*>(hero->GetPropertySet(eEPropertySetType_PlayerMemory));
    if (memory) {
        auto& knows = memory->AccessPlayerKnows();
        for (int i = 0; i < knows.GetCount(); ++i) everSeen[knows.AccessAt(i).GetText()] = 1;
    }
    for (auto const& entry : everSeen) states[entry.first] = entry.second;
    if (chapterSharing) states[ChapterEntry] = static_cast<uint8_t>(Entity(hero).PlayerMemory.Chapter);
}

bool EventApply(char const* name, uint8_t known)
{
    Entity hero(gCSession::GetInstance().GetHero());
    if (!std::strcmp(name, ChapterEntry)) {
        if (!chapterSharing) return true;
        if (known && static_cast<int>(hero.PlayerMemory.Chapter) != known) Logf("story: chapter %d -> %d", static_cast<int>(hero.PlayerMemory.Chapter), known);
        if (known) hero.PlayerMemory.Chapter = known;
        return true;
    }
    const bCString event(name);
    if (known) hero.PlayerMemory.SetGameEvent(event);
    else hero.PlayerMemory.ClearGameEvent(event);
    everSeen[name] = known;
    return true;
}

Domain domains[] = {
    {net::StateQuests, "quests", &QuestsReady, &QuestSnapshot, &QuestApply},
    {net::StateInfos, "infos", &InfosReady, &InfoSnapshot, &InfoApply},
    {net::StateEvents, "events", &EventsReady, &EventSnapshot, &EventApply},
};

Domain* Find(uint8_t kind)
{
    for (Domain& d : domains)
        if (d.kind == kind) return &d;
    return nullptr;
}

void Send(Domain& d, std::vector<net::StateEntry> const& entries)
{
    for (size_t at = 0; at < entries.size(); at += net::MaxStatesPerMessage) {
        const size_t left = entries.size() - at;
        const int count = static_cast<int>(left < net::MaxStatesPerMessage ? left : net::MaxStatesPerMessage);
        net::SendStates(d.kind, entries.data() + at, count);
        d.sent += count;
    }
}

bool Entry(Domain& d, std::string const& name, uint8_t value, std::vector<net::StateEntry>& out)
{
    net::StateEntry entry{value, {}};
    if (name.size() >= sizeof(entry.name)) { ++d.skipped; return false; }  // would arrive as another name
    strcpy_s(entry.name, name.c_str());
    out.push_back(entry);
    return true;
}

void Apply(Domain& d, char const* name, uint8_t value)
{
    if (!d.apply(name, value)) ++d.forced;
    ++d.applied;
}

void OnStates(uint8_t from, uint8_t kind, net::StateEntry const* entries, int count)
{
    Domain* d = Find(kind);
    if (disabled || !d || !d->ready()) return;
    d->received += count;
    if (net::IsHost() && count == 0) {
        // A client entered the game (or reconnected): everything off its default, so it continues the
        // host's story. Sent to everyone; applying a value twice changes nothing.
        States current;
        d->snapshot(current);
        std::vector<net::StateEntry> all;
        size_t zeros = 0;
        for (auto const& [name, value] : current) {
            if (value != 0) Entry(*d, name, value, all);
            else if (fullZeros && d->zeroed.count(name) && Entry(*d, name, 0, all)) ++zeros;
        }
        Send(*d, all);
        Logf("story: full %s state (%zu entries, %zu of them cleared here) for player %u", d->label, all.size(), zeros, from);
        return;
    }
    if (net::IsHost()) {
        // A client changed these: apply, then tell everyone the host's resulting values.
        for (int i = 0; i < count; ++i) Apply(*d, entries[i].name, entries[i].value);
        States current;
        d->snapshot(current);
        std::vector<net::StateEntry> result;
        for (int i = 0; i < count; ++i) {
            auto it = current.find(entries[i].name);
            if (it == current.end()) continue;
            Track(*d, it->first, it->second);
            d->known[it->first] = it->second;
            Entry(*d, it->first, it->second, result);
        }
        Send(*d, result);
        Logf("story: applied %d %s change(s) from player %u", count, d->label, from);
    } else {
        for (int i = 0; i < count; ++i) {
            d->known[entries[i].name] = entries[i].value;
            Apply(*d, entries[i].name, entries[i].value);
        }
    }
}

void Poll(Domain& d)
{
    if (!net::IsHost() && !net::Connected()) d.wasConnected = false;
    if (!d.ready() || (!net::IsHost() && !net::Connected())) return;
    States current;
    d.snapshot(current);
    if (!d.primed) {
        // Values before this point are the new game's own defaults, identical on every machine.
        for (auto const& [name, value] : current)
            if (!d.known.count(name)) d.known[name] = value;
        d.primed = true;
    }
    if (!net::IsHost() && !d.wasConnected) {
        net::SendStates(d.kind, nullptr, 0);
        ++d.requests;
    }
    d.wasConnected = net::IsHost() || net::Connected();
    std::vector<net::StateEntry> changes;
    for (auto const& [name, value] : current) {
        auto it = d.known.find(name);
        if (it != d.known.end() && it->second == value) continue;
        Logf("story: local %s %s=%d", d.label, name.c_str(), static_cast<int>(value));
        Track(d, name, value);
        d.known[name] = value;
        Entry(d, name, value, changes);
    }
    if (changes.empty()) return;
    if (!net::IsHost()) d.claims += static_cast<unsigned>(changes.size());
    Send(d, changes);
}
}

void Start() { net::SetStateHandler(&OnStates); }

void Tick()
{
    if (disabled) return;
    const DWORD now = GetTickCount();
    if (now - lastPoll < PollInterval) return;
    lastPoll = now;
    for (Domain& d : domains) Poll(d);
}

// QA: the local hero's knowledge of a story event; set or clear it as a dialog would.
void Event(char const* action, char const* name, LogFn log)
{
    char line[200];
    if (!EventsReady()) { log("REFUSED event: no hero"); return; }
    Entity hero(gCSession::GetInstance().GetHero());
    const bCString event(name);
    if (!std::strcmp(action, "setevent")) hero.PlayerMemory.SetGameEvent(event);
    else if (!std::strcmp(action, "clearevent")) hero.PlayerMemory.ClearGameEvent(event);
    sprintf_s(line, "EVENT name=%s known=%d", name, hero.PlayerMemory.IsSetGameEvent(event) ? 1 : 0);
    log(line);
}

void Report(LogFn log)
{
    char line[260];
    for (Domain const& d : domains) {
        size_t longest = 0;
        for (auto const& entry : d.known) longest = entry.first.size() > longest ? entry.first.size() : longest;
        sprintf_s(line, "STORY kind=%s longest=%zu disabled=%d primed=%d known=%zu sent=%u received=%u applied=%u forced=%u claims=%u requests=%u skipped=%u zeroed=%zu",
                  d.label, longest, disabled, d.primed, d.known.size(), d.sent, d.received, d.applied, d.forced, d.claims, d.requests, d.skipped,
                  d.zeroed.size());
        log(line);
    }
    log("STORY end");
}

void ChapterOff(LogFn log)
{
    chapterSharing = false;
    log("CHAPTERSYNCOFF ok=1");
}

void Disable(LogFn log)
{
    disabled = true;
    log("STORYOFF ok=1");
}

void ZerosOff(LogFn log)
{
    fullZeros = false;
    log("STORYZEROSOFF ok=1 (the full state leaves out what was cleared)");
}

void Info(char const* name, bool give, LogFn log)
{
    char line[260];
    gCInfoManager_PS* manager = Infos();
    gCInfo_PS* info = manager ? manager->GetInfo(bCString(name)) : nullptr;
    if (!info) { sprintf_s(line, "INFO name=%s missing=1", name); log(line); return; }
    if (give) info->AccessInfoGiven() = true;
    sprintf_s(line, "INFO name=%s given=%d permanent=%d owner=%s", name, info->AccessInfoGiven() ? 1 : 0,
              info->AccessPermanent() ? 1 : 0, info->AccessOwner().GetText());
    log(line);
}

void InfoCandidates(LogFn log)
{
    char line[260];
    gCInfoManager_PS* manager = Infos();
    int found = 0;
    if (manager) {
        auto& all = manager->AccessInfos();
        for (int i = 0; i < all.GetCount() && found < 20; ++i) {
            gCInfo_PS* info = all.AccessAt(i);
            if (!info || info->AccessInfoGiven() || info->AccessPermanent() || info->AccessOwner().IsEmpty()) continue;
            char const* name = info->AccessName().GetText();
            // Names the QA command file accepts: [A-Za-z0-9_-], and short enough for one message entry.
            bool plain = std::strlen(name) < sizeof(net::StateEntry::name);
            for (char const* c = name; *c && plain; ++c) plain = isalnum(static_cast<unsigned char>(*c)) || *c == '_' || *c == '-';
            if (!plain) continue;
            sprintf_s(line, "INFOS name=%s owner=%s", name, info->AccessOwner().GetText());
            log(line);
            ++found;
        }
        sprintf_s(line, "INFOS total=%d", all.GetCount());
        log(line);
    }
    log("INFOS end");
}
}

namespace g3t::story {
// QA diagnostics: hold a dialog the way talking to an NPC does (info manager between hero and NPC).
void Talk(char const* npcName, LogFn log)
{
    gCInfoManager_PS* manager = Infos();
    eCEntity* hero = gCSession::GetInstance().GetHero();
    Entity npc = Named(npcName);
    if (!manager || !hero || !npc.GetInstance()) { log("REFUSED talk: no game, hero or NPC"); return; }
    const GEBool started = manager->StartInfoManager(*hero, *npc.GetInstance());
    // The options on offer; ":xp" marks one whose script gives experience (GiveXP), for stage Q2.
    std::string offered;
    for (GEInt i = 0; i < manager->GetAvailableInfoCount(); ++i) {
        gCInfo_PS* info = manager->GetAvailableInfo(i);
        if (!info) continue;
        bool xp = false;
        gCInfoScript_PS* script = info->AccessInfoScript().GetObject();
        for (GEInt c = 0; script && c < script->GetCommandCount(); ++c)
            if (gCInfoScriptLine_PS* command = script->GetCommand(c)) xp = xp || command->AccessCommand() == bCString("GiveXP");
        offered += std::string(offered.empty() ? "" : ",") + info->AccessName().GetText() + (xp ? ":xp" : "");
    }
    char line[900];
    sprintf_s(line, "TALK npc=%s started=%d running=%d available=%d npc_task=%s infos=%s", npcName, started ? 1 : 0,
              manager->AccessIsRunning() ? 1 : 0, manager->GetAvailableInfoCount(), npc.Routine.GetCurrentTask().GetText(),
              offered.empty() ? "-" : offered.c_str());
    log(line);
}

void TalkState(LogFn log)
{
    char line[260];
    gCInfoManager_PS* manager = Infos();
    if (!manager) { log("REFUSED talkstate: no game"); return; }
    gCInfo_PS* current = manager->GetCurrentInfo();
    gCEntity* partner = manager->GetDialogPartnerNPC();
    sprintf_s(line, "TALKSTATE running=%d available=%d current=%s partner=%s waiting=%d", manager->AccessIsRunning() ? 1 : 0,
              manager->GetAvailableInfoCount(), current ? current->AccessName().GetText() : "-",
              partner ? partner->GetName().GetText() : "-", manager->AccessWaitForScript() ? 1 : 0);
    log(line);
}

void EndTalk(LogFn log)
{
    gCInfoManager_PS* manager = Infos();
    if (!manager) { log("REFUSED endtalk: no game"); return; }
    manager->EndInfoManager(manager->GetCurrentInfo());
    TalkState(log);
}
}

namespace g3t::story {
// QA: address an NPC the way the player's talk action does (the AssessTalk script, NPC first).
void AssessTalk(char const* npcName, LogFn log)
{
    char line[260];
    eCEntity* hero = gCSession::GetInstance().GetHero();
    Entity npc = Named(npcName);
    if (!hero || !npc.GetInstance()) { log("REFUSED assesstalk: no hero or NPC"); return; }
    const GEInt result = GetScriptAdminExt().CallScript(bCString("AssessTalk"), npc.GetInstance(), hero, 0);
    sprintf_s(line, "ASSESSTALK npc=%s result=%d npc_task=%s npc_aimode=%d hero_task=%s has_info=%d frozen=%d npc_talking=%d npc_talked_to=%d",
              npcName, result, npc.Routine.GetCurrentTask().GetText(), body::AIModeOf(npc.GetInstance()),
              Entity(hero).Routine.GetCurrentTask().GetText(), npc.HasInfo(Entity(hero)) ? 1 : 0, npc.NPC.IsFrozen() ? 1 : 0,
              npc.Dialog.IsTalking() ? 1 : 0, npc.Dialog.IsBeingTalkedTo() ? 1 : 0);
    log(line);
}

// QA (host): end the opening raid so that the people of Ardea calm down and can talk.
void KillRaiders(LogFn log)
{
    char line[120];
    eCEntity* hero = gCSession::GetInstance().GetHero();
    int killed = 0;
    const bTObjArray<Entity> all = Entity::GetNPCs();
    for (int i = 0; i < all.GetCount() && hero; ++i) {
        Entity npc = all.GetAt(i);
        if (!npc.GetInstance() || npc.IsDead() || std::strncmp(npc.GetName().GetText(), "Orc_", 4) != 0) continue;
        const bCVector& a = npc.GetInstance()->GetWorldPosition();
        const bCVector& b = hero->GetWorldPosition();
        const float at[3] = {a.GetX(), a.GetY(), a.GetZ()}, from[3] = {b.GetX(), b.GetY(), b.GetZ()};
        if (body::Distance(at, from) > 30000.0f) continue;
        // Not Entity::Kill (removes the body while others still target it: crash) and not a death task set
        // from outside (crashed in Script.dll mid-fight): a lethal hit, judged by the game.
        if (combat::LethalHit(npc.GetInstance())) ++killed;
    }
    sprintf_s(line, "KILLRAIDERS killed=%d", killed);
    log(line);
}
}

namespace g3t::story {
// QA: put the hero 1.5 m from an NPC, on the side he came from, as a player walks up before talking.
void Approach(char const* npcName, LogFn log)
{
    char line[200];
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    Entity npc = Named(npcName);
    if (!hero || !npc.GetInstance()) { log("REFUSED approach: no hero or NPC"); return; }
    const bCVector& n = npc.GetInstance()->GetWorldPosition();
    const bCVector& h = hero->GetWorldPosition();
    const float target[3] = {n.GetX(), n.GetY(), n.GetZ()}, from[3] = {h.GetX(), h.GetY(), h.GetZ()};
    const float before = body::Distance(target, from);
    const float dx = before > 1.0f ? (from[0] - target[0]) / before : 1.0f, dz = before > 1.0f ? (from[2] - target[2]) / before : 0.0f;
    const float position[3] = {target[0] + 150.0f * dx, target[1], target[2] + 150.0f * dz};
    const bCQuaternion q = hero->GetWorldRotation();
    const float rotation[4] = {q.GetX(), q.GetY(), q.GetZ(), q.GetW()};
    Entity(hero).MoveTo(body::Pose(*hero, position, rotation));
    const bCVector& p = hero->GetWorldPosition();
    const float now[3] = {p.GetX(), p.GetY(), p.GetZ()};
    sprintf_s(line, "APPROACH npc=%s before_cm=%.0f after_cm=%.0f", npcName, before, body::Distance(target, now));
    log(line);
}
}

namespace g3t::story {
// QA: NPCs within `radius` cm (default 2000, the distance from which AssessActivePerception lets an NPC with an important
// info walk up to the player and talk) that have an important info for this machine's hero, as the game asks it.
void Important(char const* radius, LogFn log)
{
    char line[300];
    eCEntity* hero = gCSession::GetInstance().GetHero();
    if (!hero) { log("REFUSED important: no hero"); return; }
    const float within = radius && *radius ? static_cast<float>(std::atof(radius)) : 2000.0f;
    const bCVector& h = hero->GetWorldPosition();
    const float from[3] = {h.GetX(), h.GetY(), h.GetZ()};
    const bTObjArray<Entity> list = Entity::GetNPCs();
    for (int i = 0; i < list.GetCount(); ++i) {
        Entity npc = list.GetAt(i);
        if (!npc.GetInstance() || npc.IsDead() || remote::IsPuppet(npc.GetInstance())) continue;
        const bCVector& n = npc.GetInstance()->GetWorldPosition();
        const float at[3] = {n.GetX(), n.GetY(), n.GetZ()};
        const float distance = body::Distance(at, from);
        if (distance > within || !npc.HasImportantInfo(Entity(hero))) continue;
        sprintf_s(line, "IMPORTANT npc=%s distance=%.0f mirrored=%d task=%s", npc.GetName().GetText(), distance,
                  npcs::IsMirrored(npc.GetInstance()) ? 1 : 0, npc.Routine.GetCurrentTask().GetText());
        log(line);
    }
    log("IMPORTANT end");
}
}
