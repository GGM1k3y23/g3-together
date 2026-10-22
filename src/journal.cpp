// SPDX-License-Identifier: GPL-3.0-or-later
#include "journal.h"

#include "log.h"
#include "net.h"
#include "npcs.h"
#include "remote.h"

#include <Game.h>
#include <Script.h>
#include <cstdio>
#include <cstring>
#include <string>
#include <unordered_map>

namespace g3t::journal {
namespace {
struct Death
{
    uint8_t by = 0;          // player in whose region it died
    bool applied = false;    // dead here too
    bool questPending = false;  // host: count it for the quests once it is dead here as well
    uint32_t seenSince = 0;  // when this machine started processing it (0: not processed)
    std::string name;
};

std::unordered_map<std::string, Death> deaths;  // by GUID (16 raw bytes)
std::unordered_map<std::string, bool> alive;    // NPCs this machine simulates: alive when last seen
bool reporting = true;
unsigned reported = 0, received = 0, appliedCount = 0;
bool questCounting = true;  // QA negative control: QuestCountOff
unsigned questKills = 0;
uint32_t lastScan = 0;
constexpr uint32_t ScanInterval = 250;
// An NPC entering the processing range is reset by the game (OnEnterProcessingRange: hit points to maximum,
// routine); apply only after that has run.
constexpr uint32_t SettleTime = 700;

std::string Key(eCEntity const* entity)
{
    const eCEntityProxy proxy(const_cast<eCEntity*>(entity));
    return std::string(reinterpret_cast<char const*>(&proxy.GetEntityID().GetCore()), 16);
}

bool Processed(eCEntity const* entity)
{
    const bTObjArray<Entity> all = Entity::GetNPCs();
    for (int i = 0; i < all.GetCount(); ++i)
        if (all.GetAt(i).GetInstance() == entity) return true;
    return false;
}

void CountQuest(Entity& npc)
{
    if (gCQuestManager_PS* quests = gCQuestManager_PS::GetInstance()) {
        quests->OnNPCKilled(*npc.GetInstance());
        ++questKills;
        Logf("journal: counted the kill of %s for the quests", npc.GetName().GetText());
    }
}

void OnDeath(uint8_t by, uint8_t const guid[16])
{
    ++received;
    if (net::IsHost()) net::SendDeath(guid, by, by);  // pass it on to everyone else
    const std::string key(reinterpret_cast<char const*>(guid), 16);
    Entity npc(npcs::NpcByGuid(guid));  // an NPC only (Fable 1.7: quest counters ran on any GUID)
    if (!npc.GetInstance()) return;
    const bool first = deaths.find(key) == deaths.end();
    Death& d = deaths[key];
    // Kill quests count by the victim's name in gCQuest_PS::OnNPCKilled, called from the death script Kill, which
    // ran only where the NPC was simulated: on that client, whose quest counters the host never sees. The host
    // keeps and shares the quests, so it counts the kill here, once (OnNPCKilled has no guard against a second
    // call for the same NPC; static analysis of Game.dll 0x15ED00 and 0x159110).
    // Not while it still stands here: counting the last raider of the opening raid while the host's copy was alive
    // completed the quest with that NPC alive, and the host crashed in Script.dll two seconds later (stage E5, once).
    // An NPC the host does not process is counted at once (it is dead the moment the host gets there).
    if (net::IsHost() && first && by >= 2 && questCounting) {
        if (npc.IsDead() || !Processed(npc.GetInstance())) CountQuest(npc);
        else d.questPending = true;
    }
    d.by = by;
    d.name = npc.GetName().GetText();
    d.applied = npc.IsDead();
    d.seenSince = 0;
    Logf("journal: %s died in player %u's region; %s here", d.name.c_str(), by, d.applied ? "already dead" : "pending");
}

// The death as a state, not as an event: the machine in whose region it happened already ran the game's death
// script Kill there (quest OnNPCKilled, experience, plunder) and shared its results (story sync, containers).
// Here the NPC only has to be dead: hit points 0 and AIMode 9 first, then ZS_Dead, which with AIMode already 9
// only lays the body down (static analysis) and does not run Kill. With the kill cheat instead, Kill ran here
// too: the client's quest manager completed the raid quest again and paid its reward (stage 3c regression).
void Apply(Entity& npc, Death& d)
{
    npc.NPC.DefeatedByPlayer = GETrue;
    npc.Routine.FullStop();
    npc.DamageReceiver.HitPoints = 0;
    npc.Routine.AIMode = gEAIMode_Dead;
    npc.Routine.SetTask(bCString("ZS_Dead"));
    npcs::LayDownCorpse(npc.GetInstance());  // ZS_Dead with AIMode 9 lays nothing down (PLAN-2026-10-22 1.5)
    d.applied = true;
    ++appliedCount;
    Logf("journal: applied the death of %s (player %u's region): dead=%d", d.name.c_str(), d.by, npc.IsDead() ? 1 : 0);
}
}

void Start() { net::SetDeathHandler(&OnDeath); }

unsigned Write(FILE* file)
{
    unsigned n = 0;
    for (auto const& [key, d] : deaths) {
        fprintf(file, "death ");
        for (unsigned char c : key) fprintf(file, "%02x", c);
        fprintf(file, " %u %d %s\n", d.by, d.applied ? 1 : 0, d.name.c_str());
        ++n;
    }
    return n;
}

unsigned Read(FILE* file)
{
    deaths.clear();
    char line[256];
    unsigned n = 0;
    while (fgets(line, sizeof(line), file)) {
        char hex[33] = {}, name[128] = {};
        unsigned by = 0;
        int applied = 0;
        if (sscanf_s(line, "death %32s %u %d %127s", hex, 33u, &by, &applied, name, 128u) < 3) continue;
        std::string key(16, '\0');
        for (int i = 0; i < 16; ++i) {
            unsigned v = 0;
            sscanf_s(hex + 2 * i, "%2x", &v);
            key[i] = static_cast<char>(v);
        }
        Death& d = deaths[key];
        d.by = static_cast<uint8_t>(by);
        d.applied = applied != 0;
        d.name = name;
        ++n;
    }
    return n;
}

void Clear() { deaths.clear(); }

void OnWorldChange() { alive.clear(); }

bool Pending(eCEntity const* entity)
{
    if (deaths.empty() || !entity) return false;
    auto it = deaths.find(Key(entity));
    return it != deaths.end() && !it->second.applied;
}

void Tick()
{
    gCSession& session = gCSession::GetInstance();
    eCDynamicEntity* hero = session.GetHero();
    if (!hero || !session.IsGameRunning()) return;
    const uint32_t now = net::Now();
    if (now - lastScan < ScanInterval) return;
    lastScan = now;
    const bTObjArray<Entity> all = Entity::GetNPCs();
    for (int i = 0; i < all.GetCount(); ++i) {
        Entity npc = all.GetAt(i);
        eCEntity* instance = npc.GetInstance();
        if (!instance || instance == hero || remote::IsPuppet(instance)) continue;
        const std::string key = Key(instance);
        const bool dead = npc.IsDead();
        auto entry = deaths.find(key);
        if (npcs::IsMirrored(instance) || npcs::DiedOnHost(instance)) {
            // The host's snapshots decide here; a recorded death it reports dead counts as applied.
            if (entry != deaths.end() && dead) {
                entry->second.applied = true;
                if (entry->second.questPending) { entry->second.questPending = false; CountQuest(npc); }
            }
            continue;
        }
        // 1. Deaths of NPCs this machine simulates go to the host (and on from there).
        auto seen = alive.find(key);
        const bool fromJournal = entry != deaths.end();
        if (seen != alive.end() && seen->second && dead && !fromJournal && reporting && net::Connected()) {
            uint8_t guid[16];
            std::memcpy(guid, key.data(), 16);
            net::SendDeath(guid, net::LocalId(), 0);
            ++reported;
            Death& d = deaths[key];  // recorded here as well, already applied
            d.by = net::LocalId();
            d.applied = true;
            d.name = npc.GetName().GetText();
            Logf("journal: %s died here; reported", d.name.c_str());
        }
        alive[key] = !dead;
        // 2. A recorded death of an NPC this machine now processes.
        if (entry != deaths.end() && dead && entry->second.questPending) { entry->second.questPending = false; CountQuest(npc); }
        if (entry == deaths.end() || entry->second.applied) continue;
        if (dead) { entry->second.applied = true; continue; }
        if (!entry->second.seenSince) { entry->second.seenSince = now; continue; }
        if (now - entry->second.seenSince >= SettleTime) Apply(npc, entry->second);
    }
}

void Off(LogFn log)
{
    reporting = false;
    log("JOURNALOFF ok=1");
}

void QuestCountOff(LogFn log)
{
    questCounting = false;
    log("QUESTCOUNTOFF ok=1");
}

void Report(LogFn log)
{
    unsigned pending = 0;
    for (auto const& [key, d] : deaths) pending += d.applied ? 0 : 1;
    char line[200];
    sprintf_s(line, "JOURNAL entries=%zu pending=%u reported=%u received=%u applied=%u reporting=%d quest_kills=%u", deaths.size(), pending,
              reported, received, appliedCount, reporting ? 1 : 0, questKills);
    log(line);
}

void Entry(char const* name, LogFn log)
{
    char line[200];
    for (auto const& [key, d] : deaths)
        if (d.name == name) {
            sprintf_s(line, "JENTRY name=%s found=1 by=%u applied=%d", name, d.by, d.applied ? 1 : 0);
            log(line);
            return;
        }
    sprintf_s(line, "JENTRY name=%s found=0", name);
    log(line);
}
}
