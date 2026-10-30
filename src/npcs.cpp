// SPDX-License-Identifier: GPL-3.0-or-later
#include "npcs.h"

#include "body.h"
#include "combat.h"
#include "guide.h"
#include "arena.h"
#include "log.h"
#include "net.h"
#include "remote.h"
#include "journal.h"
#include "talk.h"
#include "world.h"

#include <Game.h>
#include <Script.h>
#include <Windows.h>
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace g3t::npcs {
namespace {
constexpr float InterestRadius = 4000.0f;  // cm around any player
constexpr uint32_t SendInterval = 50;      // ms (Stufe 1, 27.10.: 20 Hz; 100 before)
// At 20 Hz only what changed since its last snapshot goes out every time; an unchanged NPC still every 100 ms, so the
// bandwidth stays near the old one while moving NPCs show twice as often.
constexpr uint32_t FullEvery = 100;        // ms
bool fastNpcs = true;  // QA negative control: fastnpcsoff (every NPC every 100 ms, as before)
struct LastSent { net::NpcRecord record; uint32_t at = 0; };
std::unordered_map<std::string, LastSent> lastSent;
unsigned npcsUnchangedSkipped = 0;
bool Changed(net::NpcRecord const& a, net::NpcRecord const& b)
{
    float d = 0.0f;
    for (int k = 0; k < 3; ++k) d += (a.position[k] - b.position[k]) * (a.position[k] - b.position[k]);
    float q = 0.0f;
    for (int k = 0; k < 4; ++k) q += std::fabs(a.rotation[k] - b.rotation[k]);
    return d > 4.0f || q > 0.002f || a.motion != b.motion || a.flags != b.flags || a.hitPoints != b.hitPoints ||
           a.aniState != b.aniState || a.movementMode != b.movementMode || a.heldRight != b.heldRight || a.heldLeft != b.heldLeft;
}
constexpr uint32_t StaleAfter = 3000;      // ms without a snapshot: give the NPC its own AI back

// Owned NPCs (the host's processing area; a client's own region): sent to the others
uint32_t lastSend = 0;
uint32_t knownJoins = 0;
bool wasConnected = false;
std::unordered_set<uint32_t> namesSent;
std::unordered_set<std::string> owned;  // GUIDs this machine sent in its last update
bool owning = true;                     // QA negative control (client): send nothing of its own
unsigned claimsRefused = 0;             // host: a client's snapshots of NPCs the host simulates itself
// Dead stays dead (playtest 27.10.: wolves the host killed lived on at the client, which then simulated them and they
// attacked the host, unseen there). Client: what the host reported dead is held dead here and never sent as its own.
// Host: a client's snapshots of an NPC dead here are refused (combat.cpp drops its hits).
std::unordered_set<std::string> deadOnHost;
std::unordered_map<std::string, body::ActionClock> actionClocks;  // protocol 58: this machine's NPCs' action numbers
bool deadStays = true;  // false: as before 27.10.
bool newLife = true;     // QA negative control deadnewlifeoff: a respawned NPC stays held dead (code review P2, as before)
unsigned deadRefused = 0, deadKept = 0;
unsigned hostWins = 0;                  // client: NPCs it simulated itself until the host's snapshots came (now mirrors)
bool hostWinsRule = true;               // QA negative control: HostWinsOff
constexpr uint32_t JoinGrace = 10000;   // ms after a client joined in which the host keeps the NPCs near its hero
uint32_t joinedAt[net::MaxPlayers] = {};  // host: when each client was first seen connected (net::Now), 0 = not now
unsigned joinRefused = 0;               // host: a joining client's snapshots of NPCs near the host, refused
bool joinGraceRule = true;              // QA negative control: JoinGraceOff
// The edge of the host's range (stageM2 24.10.): an NPC the host's game still processes just outside the host's 40 m, which
// the client's own copy puts just inside, went to the client (Orc_GameStartRaider_Scout_07: 47 m here, 37.7 m there), so two
// machines simulated it, and from the client it fought the host and killed Slave_03 next to it. Whoever simulates an NPC
// first keeps it: such a contested NPC stays the host's while it is within KeepRadius of a player here; the host refuses the
// client's snapshots of it and sends its own, and the client's copy becomes a mirror again. NPCs the host does not process
// (a client's region far from the host, stage E) are not touched.
constexpr float KeepRadius = InterestRadius + 2000.0f;  // cm
std::unordered_set<std::string> contested;  // host: NPCs a client sent that this machine processes in the band
unsigned contestedRefused = 0;
bool contestRule = true;                    // QA negative control: ContestOff
float lastPlayers[net::MaxPlayers][3] = {};  // host: the players of the last SendOwned
int lastPlayerCount = 0;
// Companions: a client that recruits one of the host's NPCs takes it over for good (it follows that player, and
// the game's follow state follows the local hero, GetPlayer()); the host then only mirrors it.
std::unordered_map<std::string, uint8_t> claimedBy;  // host: NPC -> the client that took it over
std::unordered_set<std::string> myClaims;            // client: NPCs this player took over
std::unordered_map<std::string, uint32_t> claimSeen; // host: last sign of life of a claim (claim or snapshot)
std::unordered_map<std::string, uint32_t> givenBack; // client: NPCs handed back, not sent until the host's arrive
std::unordered_set<std::string> hostTaken;           // host: clients' NPCs that guide or follow the host's hero (P9)
// host_sim = elastic (Stufe 2): a client that jumped (partner key) simulates the NPCs around it before the host's sphere
// follows, and whoever simulates an NPC first keeps it, so they stayed the client's while it stood still (stageRoiHold 28.10.:
// 67 % the host's after a jump, 100 % after walking). Inside the host's sphere for ReclaimAfter, near a player, not fighting
// (no attack, parade, stumble or knock-down in its last ReclaimCalm, not lying) and nobody's companion, the host takes it back: own AI here, and the client's copy
// becomes a mirror when the host's snapshots arrive (hostWinsRule). While it stays inside, the client's snapshots of it are
// refused; after it left the sphere (or the sphere is off) it is not taken back for ReclaimPause, so nothing goes to and fro.
constexpr uint32_t ReclaimAfter = 2000;   // ms inside the sphere
constexpr uint32_t ReclaimPause = 15000;  // ms after it left the sphere
constexpr float ReclaimInside = 1000.0f;  // cm inside the sphere's edge
constexpr uint32_t ReclaimCalm = 5000;    // ms without a combat motion
std::unordered_map<std::string, uint32_t> inSphereSince;  // host: clients' NPCs inside the sphere, since
std::unordered_set<std::string> reclaimed;                // host: taken back; the client's snapshots refused
std::unordered_map<std::string, uint32_t> reclaimPause;   // host: left the sphere; not taken back before then
unsigned reclaims = 0, reclaimRefused = 0;
bool reclaimRule = true;  // QA negative control: reclaimoff
// Stufe 3.3: whether each NPC this machine simulates parried in the states it sent, by send time, so that a player's blow is
// judged against the parade the player saw on its mirror (half a round trip and a snapshot old), not against the parade now.
// The game's IsInParadeMode, not AniState alone: an NPC keeps AniState parade for a while after it stopped parrying (Hamlar,
// stageLagBlock 28.10.: AniState 4 with parade_mode=0), and every blow counted as one on a parrying NPC.
struct SentStates
{
    uint32_t at[32] = {};
    bool parrying[32] = {};
    int next = 0;
};
std::unordered_map<std::string, SentStates> sentStates;
constexpr uint32_t SentStatesFor = 1500;  // ms
// On by default since 29.10. (stageLagBlock under G3_NETSIM 800: without it every blow on an NPC parrying when the claim came,
// though the client saw him not parrying, was blocked; with it they land as seen, 4 of 4 runs). QA lagblockon / lagblockoff.
bool lagBlocks = true;
unsigned swingSamples = 0, swingArmed = 0;  // Stufe 4.1 (SwingSamples)
unsigned fightSent = 0, fightSeenAny = 0, fightSeenUs = 0, fightSeenNear = 0;  // Stufe 4.2 (FightStats)
constexpr uint32_t RevokeAfter = 15000;   // ms: a claim whose owner sends nothing goes back to the host
constexpr uint32_t GiveBackFor = 5000;    // ms: a handed-back NPC is not sent while the host takes it over
unsigned claimsDenied = 0, claimsRevoked = 0;
// Host: whose companion each NPC is, by player name. Unlike claimedBy it outlives a disconnect and goes into the
// save's sidecar: the recruiting (JoinPlayerSlavery: leader, type Slave, not waiting) ran only on that player's
// machine, so in the host's save the NPC is nobody's companion.
std::unordered_map<std::string, std::string> companions;
std::unordered_map<std::string, uint32_t> offered;  // host: when a saved companion was last handed to its player
constexpr uint32_t OfferEvery = 20000;  // ms
bool offering = true;  // QA negative control: CompanionsOff
bool companionNear = true;  // false: a saved companion is handed over wherever it is (as before 27.10.)
unsigned companionsFar = 0;
unsigned companionsOffered = 0, companionsAdopted = 0;

// Client side
struct Mirror
{
    Entity entity;
    bool resolved = false;
    bool missing = false;  // GUID not found in this copy
    uint32_t resolvedAt = 0;  // net::Now() of the last lookup, 0: never looked up
    uint32_t firstSeen = 0;   // net::Now() of its first snapshot
    bool killed = false;
    uint8_t owner = 1;             // player whose machine simulates it
    bool guarded = false;          // DefeatedByPlayer forced on while mirrored
    bool defeatedBefore = false;   // its own value, restored on release
    body::State body;
    net::NpcRecord previous;
    net::NpcRecord latest;
    uint32_t previousTicks = 0;
    uint32_t latestTicks = 0;
    uint32_t previousSent = 0, latestSent = 0;  // the sender's clock (body::Speed)
    uint32_t heldRight = 0, heldLeft = 0;  // what this mirror holds now (hashes)
    uint32_t holdChecked = 0;
    char skeleton[24] = {};        // prefix of its motion names (Orc, Hero, ...), for the lying-dead motions
    bool laid = false;             // lying-dead start motion played
    uint32_t lieCheckAt = 0;
    unsigned lieTries = 0;
    uint8_t lieNoted = 0;          // log lines written about lying down (PLAN-2026-10-22 1.5): 1 no skeleton, 2 lies, 4 gave up
    uint32_t killedAt = 0;         // net::Now() when it died here (mirrored death or journal), for the late check
    bool lateNoted = false;        // "corpse X after 45 s: lies / STANDS" written
    bool wasMirror = false;        // a dead mirror whose owner stopped sending it, laid down here further
};
unsigned holdChanges = 0;
bool style = true;  // QA negative control: StyleOff (no weapons in hand, no stance on mirrors)
constexpr uint32_t RetryLookup = 1000;  // ms between lookups of a mirror whose NPC is not here (yet)
bool retryMissing = true;  // QA negative control: mirrorretryoff (a missing mirror stays missing and stays, as before 23.10.)
unsigned mirrorsFoundLater = 0, mirrorsExpired = 0, mirrorLookups = 0;
std::unordered_map<std::string, Mirror> mirrors;
// Deaths applied here as a state, outside mirroring (journal.cpp: an NPC that died in another player's region, applied
// once this machine processes it). They got AIMode 9 and ZS_Dead, which lays nothing down: the body froze in its last
// pose (deaddiag 19.10., the same as dead mirrors before LayDown; Brenton stood on the host on 21.10.). Laid down like
// dead mirrors. Looked up by GUID every time, never kept as a raw pointer (save-forget-npclist-c.md).
std::unordered_map<std::string, Mirror> corpses;
std::unordered_map<uint32_t, std::string> motionNames;

std::string Key(uint8_t const guid[16]) { return std::string(reinterpret_cast<char const*>(guid), 16); }

void Guid(eCEntity* entity, uint8_t out[16])
{
    const eCEntityProxy proxy(entity);
    std::memcpy(out, &proxy.GetEntityID().GetCore(), 16);
}

void FormatGuid(uint8_t const guid[16], char* out, size_t size)
{
    bCPropertyID::bSCore core;
    std::memcpy(&core, guid, 16);
    Format(out, size, "%08X-%04X-%04X-%02X%02X-%02X%02X%02X%02X%02X%02X", core.Data1, core.Data2, core.Data3, core.Data4[0],
              core.Data4[1], core.Data4[2], core.Data4[3], core.Data4[4], core.Data4[5], core.Data4[6], core.Data4[7]);
}

// Which item an NPC holds in a hand, as the hash of the item's name (the same template name on every machine).
uint32_t HeldHash(Entity& npc, gESlot slot)
{
    const Entity held = npc.Inventory.GetItemFromSlot(slot);
    return held.GetInstance() ? net::HashName(held.GetName().GetText()) : 0;
}

// The stack of the mirror's own inventory with that item, or -1.
GEInt StackOf(Entity& npc, uint32_t hash)
{
    if (!hash) return -1;
    const GEInt count = npc.Inventory.GetStackCount();
    for (GEInt i = 0; i < count; ++i)
        if (Entity item = npc.Inventory.GetTemplateItem(i); item.GetInstance() && net::HashName(item.GetName().GetText()) == hash)
            return i;
    return -1;
}

void EnsureItem(Entity& npc, uint32_t hash)
{
    if (!hash || StackOf(npc, hash) >= 0) return;
    auto name = motionNames.find(hash);  // the name table carries item names too
    if (name == motionNames.end()) return;
    npc.Inventory.CreateItems(Template(name->second.c_str()), 0, 1);
}

Entity Resolve(uint8_t const guid[16])
{
    _GUID raw;
    std::memcpy(&raw, guid, 16);
    return Entity(bCPropertyID(bCGuid(raw)));
}

bool IsNpc(eCEntity* instance)
{
    return instance && instance->GetPropertySet(eEPropertySetType_NPC) && instance->GetPropertySet(eEPropertySetType_ScriptRoutine) &&
           instance->GetPropertySet(eEPropertySetType_DamageReceiver) && instance != gCSession::GetInstance().GetHero() &&
           !remote::IsPuppet(instance);
}

// QA (NPCSEND): why SendOwned last left an NPC out, or "sent", and when (net::Now()).
struct SendNote { char const* reason = "never"; uint32_t at = 0; };
std::unordered_map<std::string, SendNote> sendNotes;
// Fewer changes of hands around the edge (owner 27.10.: NPCs changed hands three times as often when the players stood
// some 40 m apart, every change can jerk). What this machine sends it keeps up to KeepRadius from the nearest player as
// long as it processes it, not just to InterestRadius; for MinHold after it began, and while it fights, up to HoldRadius.
// Taking one over still needs InterestRadius. With host_roi the host processes far enough for this (world.cpp).
struct Held { uint32_t since = 0, last = 0; };
std::unordered_map<std::string, Held> sending;  // since when and until when this machine sent each NPC
bool hysteresis = true;  // false: sent only within InterestRadius (as before 27.10.)
constexpr uint32_t MinHold = 10000;  // ms
unsigned heldBand = 0;

// Host: a client's NPC becomes a mirror here (stageM2 24.10.: Orc_GameStartRaider_Scout_07 ran on the client 12 s after a
// join, 1 run in 3): where it is here and there, and why this machine last left it out of its own snapshots.
void NoteTakeover(std::string const& key, net::NpcRecord const& record, uint8_t owner)
{
    static unsigned noted = 0;
    if (noted >= 60) return;
    ++noted;
    Entity npc = Resolve(reinterpret_cast<uint8_t const*>(key.data()));
    eCEntity* body = remote::ControlledBody();
    if (!npc.GetInstance() || !body) return;
    const bCVector& h = body->GetWorldPosition();
    const bCVector& p = npc.GetInstance()->GetWorldPosition();
    const float hero[3] = {h.GetX(), h.GetY(), h.GetZ()}, here[3] = {p.GetX(), p.GetY(), p.GetZ()};
    const auto sent = sendNotes.find(key);
    Logf("npcs: host mirrors %s of player %u: here %.1f m from the host's hero, there %.1f m; last left out here: %s, %d ms ago",
         npc.GetName().GetText(), owner, body::Distance(here, hero) / 100.0f, body::Distance(record.position, hero) / 100.0f,
         sent == sendNotes.end() ? "never seen" : sent->second.reason, sent == sendNotes.end() ? -1 : static_cast<int>(net::Now() - sent->second.at));
}

// Host: is this client's NPC one this machine processes in the band (left out of its last snapshots for range only, within
// KeepRadius of a player)? Then it stays here (see KeepRadius).
bool Contested(std::string const& key, uint8_t owner)
{
    const auto sent = sendNotes.find(key);
    if (sent == sendNotes.end() || std::strcmp(sent->second.reason, "range") || static_cast<int32_t>(net::Now() - sent->second.at) > 1000) return false;
    Entity npc = Resolve(reinterpret_cast<uint8_t const*>(key.data()));
    if (!npc.GetInstance() || npc.IsDead() || !lastPlayerCount) return false;
    const bCVector& p = npc.GetInstance()->GetWorldPosition();
    const float here[3] = {p.GetX(), p.GetY(), p.GetZ()};
    float nearest = 1e30f;
    for (int k = 0; k < lastPlayerCount; ++k) nearest = std::min(nearest, body::Distance(here, lastPlayers[k]));
    if (nearest > KeepRadius) return false;
    if (contested.insert(key).second && contested.size() < 200)
        Logf("npcs: %s of player %u refused: here %.1f m from the nearest player, in this machine's band; sent from here now",
             npc.GetName().GetText(), owner, nearest / 100.0f);
    return true;
}

bool receiving = true;  // QA (invariant self-test): RecvOff drops all NPC snapshots, so both machines simulate

void OnNpcs(net::NpcRecord const* records, int count, uint32_t now, uint32_t sent, uint8_t owner)
{
    if (!receiving) return;
    for (int i = 0; i < count; ++i) {
        const std::string key = Key(records[i].guid);
        // Whoever simulated an NPC first keeps it. Both starting at once: the host keeps it, and the client
        // turns its copy into a mirror as soon as the host's snapshots arrive.
        if (!net::IsHost() && myClaims.count(key)) continue;  // ours now; the host's last snapshots are late
        if (net::IsHost() && hostTaken.count(key)) { ++claimsRefused; continue; }  // it guides or follows the host's hero (P9)
        if (net::IsHost() && reclaimed.count(key)) { ++reclaimRefused; continue; }  // taken back into the elastic sphere
        if (deadStays && net::IsHost() && owner > 1 && !(records[i].flags & 1)) {
            Entity here = Resolve(records[i].guid);
            if (here.GetInstance() && here.IsDead()) { ++deadRefused; continue; }
        }
        if (owned.count(key) && !claimedBy.count(key)) {
            // Host: it keeps what it simulates. Client: the host's snapshots win over its own simulation (a mirror gone
            // stale for 3 s gets its own AI back and is then sent from here; refusing the host's snapshots left both
            // simulating the NPC for good: Slave_04 following the host in stageControl, Lester in stageP3a, 20.10.).
            if (net::IsHost() || owner != 1 || !hostWinsRule) { ++claimsRefused; continue; }
            owned.erase(key);
            ++hostWins;
        }
        const auto claim = claimedBy.find(key);
        if (claim != claimedBy.end() && claim->second != owner) { ++claimsRefused; continue; }  // someone else's companion
        if (net::IsHost() && owner > 1 && owner < net::MaxPlayers && claim == claimedBy.end() && joinGraceRule) {
            // A client that just joined sends what it processes before the host's first snapshots reach it; the host then
            // took over as mirrors the NPCs around its own hero (37 of 40 runs, 20.10.: raiders fought by the host ran on
            // the client). Near the host's hero, the host keeps them for a while after a join; the client turns its copies
            // into mirrors when the host's snapshots arrive.
            if (!joinedAt[owner]) joinedAt[owner] = now ? now : 1;
            if (static_cast<int32_t>(now - joinedAt[owner]) < static_cast<int32_t>(JoinGrace) && !mirrors.count(key)) {
                eCEntity* body = remote::ControlledBody();
                const bCVector& h = body ? body->GetWorldPosition() : bCVector();
                const float heroAt[3] = {h.GetX(), h.GetY(), h.GetZ()};
                if (body && body::Distance(records[i].position, heroAt) <= InterestRadius) { ++joinRefused; continue; }
            }
        }
        if (claim != claimedBy.end()) claimSeen[key] = now;
        givenBack.erase(key);  // the host sends it again: mirror it
        if (net::IsHost() && owner > 1 && claim == claimedBy.end() && !mirrors.count(key)) {
            if (contestRule && Contested(key, owner)) { ++contestedRefused; continue; }
            NoteTakeover(key, records[i], owner);
        }
        Mirror& m = mirrors[key];
        if (!m.firstSeen) m.firstSeen = now ? now : 1;
        m.owner = owner;
        m.previous = m.latestTicks ? m.latest : records[i];
        m.previousTicks = m.latestTicks ? m.latestTicks : now;
        m.previousSent = m.latestTicks ? m.latestSent : sent;
        m.latest = records[i];
        m.latestTicks = now;
        m.latestSent = sent;
    }
}

// Dead as a state, the way dead mirrors die here (MirrorTick): no death script, so no experience and no crime here.
void KeepDead(Entity& npc)
{
    if (npc.IsDead()) return;
    npc.NPC.DefeatedByPlayer = GETrue;  // the game's scripts reward nothing for it
    npc.Routine.FullStop();
    npc.DamageReceiver.HitPoints = 0;
    npc.Routine.AIMode = gEAIMode_Dead;
    npc.Routine.SetTask(bCString("ZS_Dead"));
    if (++deadKept < 20) Logf("npcs: %s is dead on the host; dead here again", npc.GetName().GetText());
}

void OnMotionName(uint32_t hash, char const* name)
{
    if (motionNames.size() < 50000 || motionNames.count(hash)) motionNames[hash] = name;  // the game has some 20000 motions
}

// Stufe 4.2: NpcRecord.flags bits 2-5 for an NPC in combat mode: whom it fights, as a player id (our hero, a puppet, or a party
// member of either: the game's fight music counts the player's party).
uint8_t PlayerOf(eCEntity* entity)
{
    if (!entity) return 0;
    eCEntity* hero = gCSession::GetInstance().GetHero();
    if (entity == hero || entity == remote::ControlledBody()) return net::LocalId();
    if (const int owner = remote::PuppetOwner(entity)) return static_cast<uint8_t>(owner);
    return 0;
}

uint8_t FightFlags(Entity& npc)
{
    if (npc.Routine.AIMode != gEAIMode_Combat) return 0;
    ++fightSent;
    eCEntity* target = npc.NPC.GetCurrentTarget().GetInstance();
    uint8_t player = PlayerOf(target);
    if (!player && target && target->GetPropertySet(eEPropertySetType_Party))
        player = PlayerOf(Entity(target).Party.GetPartyLeader().GetInstance());
    return static_cast<uint8_t>(4 | ((player & 7) << 3));
}

// Every NPC this machine processes and nobody else simulates, near a player: one snapshot each.
void SendOwned(eCEntity* hero, float const players[][3], int playerCount)
{
    owned.clear();
    lastPlayerCount = playerCount;
    for (int k = 0; k < playerCount; ++k) std::memcpy(lastPlayers[k], players[k], sizeof(float) * 3);
    eCEntity* controlled = remote::ControlledBody();
    const bTObjArray<Entity> all = Entity::GetNPCs();
    net::NpcRecord batch[net::MaxNpcsPerMessage];
    int n = 0;
    for (int i = 0; i < all.GetCount(); ++i) {
        Entity const& npc = all.GetAt(i);
        eCEntity* instance = npc.GetInstance();
        if (!instance || instance == hero || remote::IsPuppet(instance)) continue;
        if (instance == controlled) continue;  // a transformed player's creature travels as its player state
        uint8_t guid[16];
        Guid(instance, guid);
        SendNote& note = sendNotes[Key(guid)];
        note.at = net::Now();
        if (journal::Pending(instance)) { note.reason = "pending"; continue; }  // a recorded death not applied yet: never show it alive
        if (mirrors.count(Key(guid))) { note.reason = "mirror"; continue; }      // simulated elsewhere: we show theirs
        if (deadStays && !net::IsHost() && deadOnHost.count(Key(guid))) {
            Entity corpse(instance);
            KeepDead(corpse);
            note.reason = "dead on host";
            continue;
        }
        if (claimedBy.count(Key(guid))) { note.reason = "claimed"; continue; }
        const auto back = givenBack.find(Key(guid));
        if (back != givenBack.end() && static_cast<int32_t>(net::Now() - back->second) < 0) { note.reason = "givenback"; continue; }  // handed back
        const bCVector& p = instance->GetWorldPosition();
        const float at[3] = {p.GetX(), p.GetY(), p.GetZ()};
        float nearest = 1e30f;
        for (int k = 0; k < playerCount; ++k) nearest = std::min(nearest, body::Distance(at, players[k]));
        bool inRange = nearest <= InterestRadius;
        // This player's companion, as long as this machine processes it: dropped at the radius, the host ended the claim
        // for want of snapshots and handed it over again (Gorn, playtest 27.10.).
        if (!inRange && companionNear && !net::IsHost() && myClaims.count(Key(guid)) && nearest <= KeepRadius) inRange = true;
        if (!inRange) {
            const auto held = contested.find(Key(guid));
            if (held != contested.end() && nearest <= KeepRadius) inRange = true;  // contested: kept here in the band
            else if (held != contested.end()) contested.erase(held);
        }
        if (!inRange && hysteresis) {
            const auto held = sending.find(Key(guid));
            const uint32_t t = net::Now();
            if (held != sending.end() && static_cast<int32_t>(t - held->second.last) <= 1500) {  // ours until now
                const bool young = static_cast<int32_t>(t - held->second.since) < static_cast<int32_t>(MinHold);
                const bool fighting = Entity(instance).Routine.AIMode == gEAIMode_Combat;
                if (nearest <= KeepRadius || ((young || fighting) && nearest <= KeepRadius + 2000.0f)) { inRange = true; ++heldBand; }
            }
        }
        if (!inRange) { note.reason = "range"; continue; }
        note.reason = "sent";
        {
            Held& held = sending[Key(guid)];
            const uint32_t t = net::Now();
            if (!held.last || static_cast<int32_t>(t - held.last) > 1500) held.since = t;
            held.last = t;
        }
        net::NpcRecord& r = batch[n];
        r = net::NpcRecord{};
        std::memcpy(r.guid, guid, 16);
        owned.insert(Key(guid));
        std::memcpy(r.position, at, sizeof(at));
        const bCQuaternion q = instance->GetWorldRotation();
        r.rotation[0] = q.GetX(); r.rotation[1] = q.GetY(); r.rotation[2] = q.GetZ(); r.rotation[3] = q.GetW();
        Entity self(instance);
        r.movementMode = static_cast<uint8_t>(self.CharacterMovement.GetMovementMode());
        r.flags = static_cast<uint8_t>((self.IsDead() ? 1 : 0) | (self.IsDown() ? 2 : 0) | FightFlags(self));
        r.hitPoints = self.DamageReceiver.HitPoints;
        r.aniState = static_cast<uint8_t>(static_cast<int>(self.Routine.AniState));
        {
            SentStates& states = sentStates[Key(guid)];
            states.at[states.next] = net::Now();  // the snapshot's send time (NpcsHeader.sent) within a millisecond
            states.parrying[states.next] = r.aniState == gEAniState_Parade && combat::InParadeMode(instance);
            states.next = (states.next + 1) % 32;
        }
        r.heldRight = HeldHash(self, gESlot_RightHand);
        r.heldLeft = HeldHash(self, gESlot_LeftHand);
        for (gESlot slot : {gESlot_RightHand, gESlot_LeftHand}) {
            const Entity held = self.Inventory.GetItemFromSlot(slot);
            if (!held.GetInstance()) continue;
            const uint32_t hash = net::HashName(held.GetName().GetText());
            if (namesSent.insert(hash).second) net::SendMotionName(hash, held.GetName().GetText());
        }
        char motion[96];
        if (body::CurrentMotion(instance, motion, sizeof(motion)) && *motion) {
            r.motion = net::HashName(motion);
            if (namesSent.insert(r.motion).second) net::SendMotionName(r.motion, motion);
        } else {
            *motion = '\0';
        }
        r.actionSeq = body::ActionSeq(instance, actionClocks[Key(guid)], motion, nullptr);  // protocol 58
        r.motionFrac = body::MotionFraction(instance);  // protocol 59 (not in Changed: a running motion alone sends nothing new)
        {
            LastSent& last = lastSent[Key(guid)];
            const uint32_t t = net::Now();
            if (fastNpcs && last.at && static_cast<int32_t>(t - last.at) < static_cast<int32_t>(FullEvery) && !Changed(r, last.record)) {
                ++npcsUnchangedSkipped;
                continue;  // still ours (owned): only not sent again this time
            }
            last.record = r;
            last.at = t;
        }
        if (++n == net::NpcsPerSend) { net::SendNpcs(batch, n); n = 0; }
    }
    if (n) net::SendNpcs(batch, n);
}

// Host, host_sim = elastic: takes back the clients' NPCs inside its sphere (see ReclaimAfter).
void Reclaim(float const players[][3], int playerCount)
{
    const bool on = reclaimRule && world::Extended();
    const uint32_t now = net::Now();
    const auto inside = [&](float const at[3]) { return on && world::HostProcesses(at, ReclaimInside); };
    for (auto it = reclaimed.begin(); it != reclaimed.end();) {
        Entity npc = Resolve(reinterpret_cast<uint8_t const*>(it->data()));
        bool keep = false;
        if (npc.GetInstance() && !npc.IsDead()) {
            const bCVector& p = npc.GetInstance()->GetWorldPosition();
            const float at[3] = {p.GetX(), p.GetY(), p.GetZ()};
            keep = inside(at);
        }
        if (keep) { ++it; continue; }
        reclaimPause[*it] = now + ReclaimPause;
        it = reclaimed.erase(it);
    }
    if (!on) { inSphereSince.clear(); return; }
    std::unordered_map<std::string, uint32_t> stillInside;
    std::vector<std::string> take;
    for (auto& entry : mirrors) {
        Mirror const& m = entry.second;
        if (m.owner <= 1 || m.killed || !m.entity.GetInstance() || (m.latest.flags & 3)) continue;
        // Not in a fight: a slave holds a tool in its hand all day (stageRoiHold 28.10.: Slave_06 stayed the client's), so the
        // motions tell, not the hands.
        if (m.latest.aniState == gEAniState_Parade || m.latest.aniState >= gEAniState_SitKnockDown) continue;
        if (m.body.combatMoveAt && now - m.body.combatMoveAt < ReclaimCalm) continue;
        if (const auto name = motionNames.find(m.latest.motion); name != motionNames.end()) {
            char const* motion = name->second.c_str();
            if (std::strstr(motion, "Attack") || std::strstr(motion, "Parade") || std::strstr(motion, "Stumble") || std::strstr(motion, "KnockDown") ||
                std::strstr(motion, "GetUp"))
                continue;
        }
        if (claimedBy.count(entry.first) || hostTaken.count(entry.first)) continue;
        if (const auto pause = reclaimPause.find(entry.first); pause != reclaimPause.end()) {
            if (static_cast<int32_t>(now - pause->second) < 0) continue;
            reclaimPause.erase(pause);
        }
        float nearest = 1e30f;
        for (int k = 0; k < playerCount; ++k) nearest = std::min(nearest, body::Distance(m.latest.position, players[k]));
        if (nearest > InterestRadius || !inside(m.latest.position)) continue;
        const auto since = inSphereSince.find(entry.first);
        const uint32_t from = since != inSphereSince.end() ? since->second : now;
        stillInside[entry.first] = from;
        if (now - from >= ReclaimAfter) take.push_back(entry.first);
    }
    inSphereSince.swap(stillInside);
    for (auto const& key : take) {
        auto it = mirrors.find(key);
        Mirror& m = it->second;
        const unsigned owner = m.owner;
        body::Release(m.entity, m.body);  // its own AI again, here
        if (m.guarded) m.entity.NPC.DefeatedByPlayer = m.defeatedBefore;
        if (++reclaims <= 50) Logf("npcs: took back %s from player %u (inside the host's range)", m.entity.GetName().GetText(), owner);
        mirrors.erase(it);
        reclaimed.insert(key);
        inSphereSince.erase(key);
    }
}

void HostTick()
{
    const uint32_t now = net::Now();
    if (now - lastSend < (fastNpcs ? SendInterval : FullEvery)) return;
    lastSend = now;
    if (net::JoinCount() != knownJoins) {  // a new client needs every motion name again
        knownJoins = net::JoinCount();
        namesSent.clear();
    }
    // Players in this world: the host's hero and every remote player here.
    float players[net::MaxPlayers][3];
    int playerCount = 0;
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    if (hero) {
        const bCVector& p = remote::ControlledBody()->GetWorldPosition();  // a transformed host: where its creature is
        players[playerCount][0] = p.GetX(); players[playerCount][1] = p.GetY(); players[playerCount][2] = p.GetZ();
        ++playerCount;
    }
    net::Remote const* remotes = net::Remotes();
    bool anyClient = false;
    for (int id = 2; id < net::MaxPlayers; ++id) {
        if (!remotes[id].connected) { joinedAt[id] = 0; continue; }
        anyClient = true;
        if (!(remotes[id].latest.flags & 1) || remotes[id].latest.world != remote::CurrentWorld()) continue;
        std::memcpy(players[playerCount++], remotes[id].latest.position, sizeof(float) * 3);
    }
    if (!anyClient || !playerCount) return;
    Reclaim(players, playerCount);
    SendOwned(hero, players, playerCount);
}

// A client simulates what it processes and the host does not send (its own surroundings far from the host);
// it sends those NPCs to the host, which shows them and passes them on.
void OwnTick()
{
    const bool connected = net::Connected();
    if (connected && !wasConnected) namesSent.clear();  // a (new) host needs every motion name
    if (!connected && wasConnected) {
        // The host ends our claims when we drop; holding on to them would ignore its snapshots after a rejoin.
        myClaims.clear();
        givenBack.clear();
    }
    wasConnected = connected;
    if (!connected || !owning) { owned.clear(); return; }
    const uint32_t now = net::Now();
    if (now - lastSend < (fastNpcs ? SendInterval : FullEvery)) return;
    lastSend = now;
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    if (!hero) return;
    const bCVector& p = remote::ControlledBody()->GetWorldPosition();  // a transformed player: where its creature is
    const float players[1][3] = {{p.GetX(), p.GetY(), p.GetZ()}};
    SendOwned(hero, players, 1);
}

// Host: a mirror of a companion goes back to the host's own simulation.
void DropMirror(std::string const& key)
{
    auto it = mirrors.find(key);
    if (it == mirrors.end()) return;
    if (!it->second.killed && it->second.entity.GetInstance()) {
        body::Release(it->second.entity, it->second.body);
        if (it->second.guarded) it->second.entity.NPC.DefeatedByPlayer = it->second.defeatedBefore;
    }
    mirrors.erase(it);
}

void SendRevoke(std::string const& key, uint8_t player)
{
    net::WorldState s{};
    s.kind = 6;  // to `player` (guard): this NPC is not yours (any more)
    std::memcpy(s.guid, key.data(), 16);
    s.guard = player;
    net::SendWorldState(s, 0);
}

// Host: claims whose player left, or that stay silent (the player no longer processes the NPC, or left before its
// first snapshot), go back to the host; a silent but connected player is told so.
void OfferCompanions(uint32_t now)
{
    if (!offering) return;
    for (auto const& [key, name] : companions) {
        if (claimedBy.count(key)) continue;
        auto last = offered.find(key);
        if (last != offered.end() && static_cast<int32_t>(now - last->second) < static_cast<int32_t>(OfferEvery)) continue;
        for (uint8_t id = 2; id < net::MaxPlayers; ++id) {
            net::Remote const& r = net::Remotes()[id];
            if (!r.connected || !(r.latest.flags & 1) || r.latest.world != remote::CurrentWorld() || name != r.latest.name) continue;
            Entity npc = Resolve(reinterpret_cast<uint8_t const*>(key.data()));
            if (!npc.GetInstance()) break;
            // Only near its player: one far away is not processed there and never sent, the claim ran out for want of
            // snapshots and it was handed over again every 20 s (Gorn some 320 times, playtest 27.10.). The host keeps it
            // until its player comes close.
            if (companionNear) {
                const bCVector& p = npc.GetInstance()->GetWorldPosition();
                const float at[3] = {p.GetX(), p.GetY(), p.GetZ()};
                if (body::Distance(at, r.latest.position) > InterestRadius) { ++companionsFar; break; }
            }
            net::WorldState s{};
            s.kind = 7;  // to player `guard`: this NPC is your companion (from the save)
            std::memcpy(s.guid, key.data(), 16);
            s.guard = id;
            net::SendWorldState(s, 0);
            claimedBy[key] = id;
            claimSeen[key] = now;
            DropMirror(key);
            offered[key] = now;
            ++companionsOffered;
            Logf("npcs: %s is %s's companion (saved); handed over", Resolve(reinterpret_cast<uint8_t const*>(key.data())).GetName().GetText(), name.c_str());
            break;
        }
    }
}

void ClaimTick(uint32_t now)
{
    OfferCompanions(now);
    for (auto it = claimedBy.begin(); it != claimedBy.end();) {
        const bool gone = !net::Remotes()[it->second].connected;
        const auto seen = claimSeen.find(it->first);
        const bool silent = seen == claimSeen.end() || static_cast<int32_t>(now - seen->second) > static_cast<int32_t>(RevokeAfter);
        if (!gone && !silent) { ++it; continue; }
        if (!gone) SendRevoke(it->first, it->second);
        ++claimsRevoked;
        Logf("npcs: claim of player %u on %s ended (%s)", it->second,
             Resolve(reinterpret_cast<uint8_t const*>(it->first.data())).GetInstance() ? Resolve(reinterpret_cast<uint8_t const*>(it->first.data())).GetName().GetText() : "?",
             gone ? "player left" : "no snapshots");
        DropMirror(it->first);
        claimSeen.erase(it->first);
        it = claimedBy.erase(it);
    }
}

bool xpGuard = true;  // QA negative control: XpGuardOff
bool farXpGuard = true;  // QA negative control: FarXpOff
unsigned farXpRefused = 0;
bool deadPose = true;  // QA negative control: DeadPoseOff
unsigned corpsesLaid = 0, corpseLoops = 0, slotsStopped = 0;

// The motion-name prefix of a body's skeleton ("Orc" of "Orc_Stand_None_2H_..."), from what it shows or last showed
// on the owner's machine.
void LearnSkeleton(Mirror& m, char const* motion)
{
    if (*m.skeleton || !motion) return;
    const size_t n = std::strcspn(motion, "_");
    if (n && n < sizeof(m.skeleton) && motion[n] == '_') std::memcpy(m.skeleton, motion, n), m.skeleton[n] = '\0';
}

// A dead mirror lies down the way the game lays its dead down (deaddiag 19.10., agent B's finding "dead orcs stand on
// the client"): on the client ZS_Dead, with AIMode 9 set first so that it skips the death script, played no motion at
// all and the body froze in its last pose, mostly standing; the owner's ZS_Dead plays <skeleton>_..._LieDead_Begin and
// then LieDead_Ambient_Loop every ~400 ms, and a ragdoll death falls by physics there. Here: the start motion once,
// then the loop whenever slot 0 shows something else.
// Lying as the QA's deadpose measures it: a lying motion runs on slot 0, nothing on the locomotion slots 1-3.
bool Lies(eCEntity* entity, char* shows, size_t size, char* slots, size_t slotsSize)
{
    body::CurrentMotion(entity, shows, size);
    bool still = true;
    *slots = '\0';
    if (auto* animation = static_cast<eCVisualAnimation_PS*>(entity->GetPropertySet(eEPropertySetType_Animation)))
        for (int slot = 1; slot <= 3; ++slot) {
            char name[128] = {};
            body::MotionName(animation, slot, name, sizeof(name));
            const bool runs = animation->GetActor() && animation->GetActor()->IsMotionRunning(static_cast<eCWrapper_emfx2Actor::eEMotionType>(slot));
            still = still && !runs;
            Format(slots + strlen(slots), slotsSize - strlen(slots), "%s%d:%s:%s", slot > 1 ? "|" : "", slot, runs ? "run" : "off", *name ? name : "-");
        }
    return std::strstr(shows, "_Lie") && body::MotionRunning(entity) && still;  // LieDead, or a knock-out's LieKnockDown
}

// Once per corpse, 45 s after its death here: does it lie? A play evening's log counts standing corpses from these lines
// (owner, 22.10.: far more stood than the two examples).
void LateCheck(Mirror& m, uint32_t now, char const* kind)
{
    if (m.lateNoted || !m.killedAt || static_cast<int32_t>(now - m.killedAt) < 45000) return;
    m.lateNoted = true;
    char shows[128], slots[400];
    const bool lies = Lies(m.entity.GetInstance(), shows, sizeof(shows), slots, sizeof(slots));
    Logf("npcs: corpse %s (%s) after 45 s: %s (shows %s, slots %s)", m.entity.GetName().GetText(), kind, lies ? "lies" : "STANDS", shows, slots);
}

void LayDown(Mirror& m, uint32_t now)
{
    if (!deadPose || static_cast<int32_t>(now - m.lieCheckAt) < 0) return;
    if (m.lieTries >= 40) {
        if (!(m.lieNoted & 4)) {
            m.lieNoted |= 4;
            char shows[128];
            body::CurrentMotion(m.entity.GetInstance(), shows, sizeof(shows));
            Logf("npcs: corpse %s: the lying motion did not run after 40 tries; gave up (shows %s)", m.entity.GetName().GetText(), shows);
        }
        return;
    }
    m.lieCheckAt = now + 400;
    char shows[128];
    body::CurrentMotion(m.entity.GetInstance(), shows, sizeof(shows));
    LearnSkeleton(m, shows);
    if (!*m.skeleton) {
        auto name = motionNames.find(m.latest.motion);
        if (name != motionNames.end()) LearnSkeleton(m, name->second.c_str());
    }
    if (!*m.skeleton) {
        if (!(m.lieNoted & 1)) {
            m.lieNoted |= 1;
            Logf("npcs: corpse %s: no skeleton known from its motions (shows %s); it cannot be laid down yet", m.entity.GetName().GetText(), shows);
        }
        return;
    }
    // Nothing of its life may go on underneath, at every check: the movement controller (a mirror walking when it died)
    // and the locomotion slots 1-3 kept playing standing and walking motions, and 3 of 12 corpses stood with the lying
    // loop running on slot 0 (deaddiag 19.10.); stopped once, 1 of 12 still stood (it had been in its fighting stance).
    m.entity.CharacterMovement.StopMovement();
    if (auto* animation = static_cast<eCVisualAnimation_PS*>(m.entity.GetInstance()->GetPropertySet(eEPropertySetType_Animation)))
        for (int slot = 1; slot <= 3; ++slot)
            if (animation->GetActor() && animation->GetActor()->IsMotionRunning(static_cast<eCWrapper_emfx2Actor::eEMotionType>(slot)))
                animation->StopMotion(static_cast<eCWrapper_emfx2Actor::eEMotionType>(slot), 0.2f), ++slotsStopped;
    // Lying already: the start motion or the loop still plays (once a motion ends the body falls back to its bind pose).
    if (std::strstr(shows, "LieDead") && body::MotionRunning(m.entity.GetInstance())) {
        m.lieTries = 0;  // the cap counts only starts in a row that did not run
        if (!(m.lieNoted & 2)) {
            m.lieNoted |= 2;
            Logf("npcs: corpse %s lies (%s)", m.entity.GetName().GetText(), shows);
        }
        return;
    }
    char motion[128];
    if (!m.laid) {
        Format(motion, "%s_Stand_None_None_P0_LieDead_Begin_N_Back_00_%%_00_P0_200", m.skeleton);
        m.laid = true;
        m.lieCheckAt = now + 2000;  // the start motion takes ~2 s (owner's trace)
        ++corpsesLaid;
    } else {
        // The loop belongs to the lying-dead stance: it plays only once the body has that stance (body.cpp: motions of
        // another stance do not play), and like the game's own ZS_Dead_Loop as a play-ani instruction (deaddiag: started
        // with StartPlayAni it showed on slot 0 but did not run, and 2 of 4 corpses stood).
        Format(motion, "%s_LieDead_None_None_P0_Ambient_Loop_N_Back_00_%%_00_P0_0", m.skeleton);
        m.entity.Routine.AniState = gEAniState_LieDead;
        ++corpseLoops;
        ++m.lieTries;
        if (!body::PlayAniInstruction(m.entity.GetInstance(), m.body, motion)) m.entity.StartPlayAni(bCString(motion), 0, GEFalse, 1.0f, GEFalse);
        return;
    }
    ++m.lieTries;
    m.entity.StartPlayAni(bCString(motion), 0, GEFalse, 1.0f, GEFalse);
}

void MirrorTick()
{
    static char const* const DeathTask = "ZS_Dead";
    const uint32_t now = net::Now();
    // The host also mirrors NPCs a client simulates; it drives only those it processes itself (the others are
    // frozen here anyway, and get their state when the host arrives).
    if (net::IsHost()) ClaimTick(now);
    std::unordered_set<eCEntity*> processed;
    if (net::IsHost()) {
        const bTObjArray<Entity> all = Entity::GetNPCs();
        for (int i = 0; i < all.GetCount(); ++i) processed.insert(all.GetAt(i).GetInstance());
    }
    for (auto it = mirrors.begin(); it != mirrors.end();) {
        Mirror& m = it->second;
        if (!m.resolved) {
            const bool again = m.resolvedAt != 0;
            m.entity = Resolve(m.latest.guid);
            m.resolved = true;
            m.resolvedAt = now ? now : 1;
            m.missing = !IsNpc(m.entity.GetInstance());  // the host's hero, a chest or a puppet is no mirror (Fable 1.6)
            if (again && !m.missing) {
                ++mirrorsFoundLater;
                Logf("npcs: mirror %s found here, %u ms after its first snapshot", m.entity.GetName().GetText(), now - m.firstSeen);
            }
            // The game's death and knock-out scripts reward the player (GiveXP) for any NPC not yet marked as
            // defeated by the player, and then mark it. For mirrors that rewarded this client for every NPC the
            // host or anyone else defeated (1250 XP in stage 3c without a single own hit; 500 when marked only
            // once the host reported it down or dead, since hit points arrive first). The host decides who
            // earned it (net::SendKilled), so a mirror is marked for as long as it is mirrored.
            if (!m.missing && xpGuard) {
                m.defeatedBefore = m.entity.NPC.DefeatedByPlayer;
                m.entity.NPC.DefeatedByPlayer = GETrue;
                m.guarded = true;
            }
        }
        // Not here (yet), or gone: looked up again every second while its owner sends it, and dropped once stale (Codex
        // review 22.10.: an NPC missing at its first lookup stayed missing, and its entry stayed for good; only summons were
        // bound again, through Rebind).
        if (m.missing || !m.entity.GetInstance()) {
            if (retryMissing && static_cast<int32_t>(now - m.latestTicks) > static_cast<int32_t>(StaleAfter)) {
                ++mirrorsExpired;
                it = mirrors.erase(it);
                continue;
            }
            if (retryMissing && static_cast<int32_t>(now - m.resolvedAt) >= static_cast<int32_t>(RetryLookup)) {
                m.resolved = false;
                ++mirrorLookups;
            }
            ++it;
            continue;
        }
        if (net::IsHost() && !processed.count(m.entity.GetInstance()) &&
            static_cast<int32_t>(now - m.latestTicks) <= static_cast<int32_t>(StaleAfter)) { ++it; continue; }
        const auto claim = claimedBy.find(it->first);
        if (claim != claimedBy.end() && !net::Remotes()[claim->second].connected) claimedBy.erase(claim);  // its player left
        else if (claim != claimedBy.end()) { if (static_cast<int32_t>(now - m.latestTicks) > static_cast<int32_t>(StaleAfter)) { ++it; continue; } }
        if (static_cast<int32_t>(now - m.latestTicks) > static_cast<int32_t>(StaleAfter)) {
            if (!m.killed) {
                body::Release(m.entity, m.body);  // out of the host's interest area: own AI again
                if (m.guarded) m.entity.NPC.DefeatedByPlayer = m.defeatedBefore;
            } else if (!m.lateNoted) {
                // Dead, and its owner stopped sending it before it lay (or before its late check): lying down goes on here,
                // looked up by GUID (B's stageK1 run 20261022-153310-p2: Slave_03 stood in a get-up motion, and no line at all
                // said it lay or gave up).
                char shows[128], slots[400];
                const bool lies = Lies(m.entity.GetInstance(), shows, sizeof(shows), slots, sizeof(slots));
                Logf("npcs: corpse %s: its owner stopped sending it (%s); laid down here further", m.entity.GetName().GetText(),
                     lies ? "lies" : "not lying yet");
                Mirror& c = corpses[it->first];
                c = m;
                c.wasMirror = true;
            }
            it = mirrors.erase(it);
            continue;
        }
        if (m.latest.flags & 1) {  // dead on the host
            // Die like the game's scripts let NPCs die. Entity::Kill removed the body from the NPC list (and
            // from view), so a client never showed a corpse.
            const bool first = !m.killed;
            if (!net::IsHost()) deadOnHost.insert(it->first);
            m.killed = true;  // before SetTask: the AI filter lets the death state run only for killed mirrors
            if (first) m.killedAt = now ? now : 1;
            if (first)
                Logf("npcs: mirror %s dead on the host; here dead=%d defeated_by_player=%d task=%s", m.entity.GetName().GetText(),
                     m.entity.IsDead() ? 1 : 0, m.entity.NPC.DefeatedByPlayer ? 1 : 0, m.entity.Routine.GetCurrentTask().GetText());
            if (first && !m.entity.IsDead() && !xpGuard) {
                // QA negative control: the old way, the death task with the game's death script Kill here too.
                m.entity.Routine.FullStop();
                m.entity.Routine.SetTask(bCString("ZS_RagDollDead"));
            } else if (first && !m.entity.IsDead()) {
                // Dead as a state, not as an event: the host ran the death script Kill (quest OnNPCKilled,
                // experience, plunder) and shares its results. With AIMode 9 set first, ZS_Dead only lays the
                // body down and skips Kill (static analysis); ZS_RagDollDead ran Kill here as well, and the
                // client's quest manager completed the raid quest on its own (stage 3c regression).
                m.entity.Routine.FullStop();
                m.entity.DamageReceiver.HitPoints = 0;
                m.entity.Routine.AIMode = gEAIMode_Dead;
                m.entity.Routine.SetTask(bCString(DeathTask));
            }
            if (xpGuard) LayDown(m, now);
            LateCheck(m, now, "mirror");
            ++it;
            continue;
        }
        // Dead here (a death from the journal, applied while the host had not processed it yet) but alive in
        // the host's snapshot: wait for the host's own state instead of driving a corpse.
        if (m.entity.IsDead()) { ++it; continue; }
        if (static_cast<int32_t>(m.entity.DamageReceiver.HitPoints) != m.latest.hitPoints)
            m.entity.DamageReceiver.HitPoints = m.latest.hitPoints;
        if (talk::TalkingWithHero(m.entity.GetInstance())) { ++it; continue; }  // stands with our hero; the host holds it
        // Weapons in hand and the fighting stance: without them a mirror walks unarmed and its attack motions
        // never show (first human test: the client saw no fight at all; 0 of 170 attack motions matched).
        // What the mirror really holds, not what it was last given: stopping its AI (Pacify: FullStop) puts weapons
        // away again. Checked at most every 250 ms.
        if (style && static_cast<int32_t>(now - m.holdChecked) >= 250 &&
            (m.latest.heldRight != HeldHash(m.entity, gESlot_RightHand) || m.latest.heldLeft != HeldHash(m.entity, gESlot_LeftHand))) {
            m.holdChecked = now;
            // A mirror never ran its own AI, which fills an NPC's inventory with its equipment (its inventory was empty;
            // AssureAll froze the client): create the held item itself, by the name the owner sent once.
            EnsureItem(m.entity, m.latest.heldRight);
            EnsureItem(m.entity, m.latest.heldLeft);
            const GEInt right = StackOf(m.entity, m.latest.heldRight), left = StackOf(m.entity, m.latest.heldLeft);
            const GEBool held = m.entity.Inventory.HoldStacks(left, right);
            ++holdChanges;
            if (holdChanges < 20)
                Logf("npcs: %s holds stacks %d/%d (wanted %08x/%08x) ok=%d now=%s", m.entity.GetName().GetText(), left, right, m.latest.heldLeft,
                     m.latest.heldRight, held ? 1 : 0, m.entity.Inventory.GetItemFromSlot(gESlot_RightHand).GetInstance() ? "right" : "-");
            m.heldRight = m.latest.heldRight;
            m.heldLeft = m.latest.heldLeft;
        }
        const float speed = body::Speed(m.previous.position, m.latest.position, m.previousSent, m.latestSent, m.previousTicks, m.latestTicks);
        auto name = motionNames.find(m.latest.motion);
        if (name != motionNames.end()) LearnSkeleton(m, name->second.c_str());
        const uint32_t newestBefore = m.body.track.count ? m.body.track.snaps[m.body.track.count - 1].sent : 0;
        body::Feed(m.body.track, m.latest.position, m.latest.rotation, m.latestSent, m.latestTicks);
        // QA (Stufe 3.2): how far the mirror stood from where its owner had the NPC (half the round trip before it arrived)
        if (m.body.track.count && m.body.track.snaps[m.body.track.count - 1].sent != newestBefore && speed > 100.0f)
            body::NoteTruth(m.body, m.latest.position, m.latestTicks - net::RoundTrip(m.owner) / 2);
        float shownAt[3], shownTurn[4];
        const bool buffered = body::interpolate && body::Sample(m.body.track, now, shownAt, shownTurn);
        const float steady = body::Steadiness(m.body.track, m.body, now);  // also the glide causes (overshoot after a stop or turn)
        float ahead[3];
        body::Target target{buffered ? shownAt : m.latest.position, buffered ? shownTurn : m.latest.rotation, m.latest.movementMode,
                                  name != motionNames.end() ? name->second.c_str() : nullptr, speed,
                                  style && m.latest.aniState ? static_cast<int>(m.latest.aniState) : -1,
                                  body::SteerGoal(m.body.track, now, ahead, buffered ? m.latest.position : nullptr,
                                                  body::mirrorLead && steady >= 1.0f)};
        target.actionSeq = m.latest.actionSeq;  // protocol 58
        target.motionFrac = m.latest.motionFrac;  // protocol 59: where the owner's motion was, and how long ago
        target.motionAgeMs = now - m.latestTicks + net::RoundTrip(m.owner) / 2;
        m.body.combatMoves = true;  // attacks replay as the game's combat moves (body.cpp)
        body::Drive(m.entity, m.body, target, now);
        if (m.body.combatMoveAt && now - m.body.combatMoveAt < 1000) {
            ++swingSamples;
            Entity weapon = m.entity.Inventory.GetItemFromSlot(gESlot_RightHand);
            if (weapon.GetInstance() && weapon.GetInstance()->GetPropertySet(eEPropertySetType_Damage) &&
                static_cast<gEDamageType>(weapon.Damage.DamageType) != gEDamageType_None)
                ++swingArmed;
        }
        ++it;
    }
}
}

bool MirrorFightsHero()
{
    eCEntity* body = remote::ControlledBody();
    if (!body || !net::LocalId()) return false;
    const bCVector& p = body->GetWorldPosition();
    const float at[3] = {p.GetX(), p.GetY(), p.GetZ()};
    bool any = false, us = false;
    for (auto const& entry : mirrors) {
        Mirror const& m = entry.second;
        if (m.killed || !(m.latest.flags & 4) || (m.latest.flags & 1)) continue;
        any = true;
        if (((m.latest.flags >> 3) & 7) != net::LocalId()) continue;
        us = true;
        if (body::Distance(m.latest.position, at) <= 3000.0f) {  // HandleMusicSituation's 30 m (SG 0xb4070)
            ++fightSeenAny, ++fightSeenUs, ++fightSeenNear;
            return true;
        }
    }
    fightSeenAny += any;
    fightSeenUs += us;
    return false;
}

void FightStats(unsigned& sent, unsigned& seenAny, unsigned& seenUs, unsigned& seenNear)
{
    sent = fightSent, seenAny = fightSeenAny, seenUs = fightSeenUs, seenNear = fightSeenNear;
}

void SwingSamples(unsigned& armed, unsigned& samples)
{
    armed = swingArmed;
    samples = swingSamples;
}

uint32_t SeenAt(eCEntity const* entity)
{
    if (!entity) return 0;
    for (auto const& entry : mirrors)
        if (entry.second.entity.GetInstance() == entity && entry.second.latestSent)
            // With the display buffer the mirror shows the owner's state some BufferDelay older than the newest snapshot.
            return entry.second.latestSent - (body::interpolate ? static_cast<uint32_t>(body::BufferDelay) : 0u);
    return 0;
}

bool ParryingAt(eCEntity const* entity, uint32_t at, bool& parrying)
{
    if (!lagBlocks || !entity || !at) return false;
    uint8_t guid[16];
    Guid(const_cast<eCEntity*>(entity), guid);
    const auto it = sentStates.find(Key(guid));
    if (it == sentStates.end()) return false;
    // The newest state sent at or before `at` (25 ms leeway for the clock reading), not older than SentStatesFor.
    int best = -1;
    for (int i = 0; i < 32; ++i) {
        const uint32_t t = it->second.at[i];
        if (!t || static_cast<int32_t>(t - at) > 25 || static_cast<int32_t>(at - t) > static_cast<int32_t>(SentStatesFor)) continue;
        if (best < 0 || static_cast<int32_t>(t - it->second.at[best]) > 0) best = i;
    }
    if (best < 0) return false;
    parrying = it->second.parrying[best];
    return true;
}

void Start() { net::SetNpcHandlers(&OnNpcs, &OnMotionName); }

bool InAction(eCEntity const* entity)
{
    if (!entity) return false;
    const uint32_t now = net::Now();
    for (auto const& entry : mirrors)
        if (entry.second.entity.GetInstance() == entity) return static_cast<int32_t>(entry.second.body.actionUntil - now) > 0;
    return false;
}

bool DiedOnHost(eCEntity const* entity)
{
    if (!entity) return false;
    for (auto const& entry : mirrors)
        if (entry.second.killed && entry.second.entity.GetInstance() == entity) return true;
    return false;
}

// QA, client (stage X1): one NPC record with this machine's hero GUID (the same entity as the host's hero in a shared
// world) and a NaN position, sent as if it were an NPC of this client's region.
void BadRecord(LogFn log)
{
    eCEntity* hero = gCSession::GetInstance().GetHero();
    if (!hero || net::IsHost()) { log("REFUSED badrecord: client with a hero only"); return; }
    net::NpcRecord record{};
    Guid(hero, record.guid);
    record.position[0] = std::nanf("");
    record.position[1] = record.position[2] = 0.0f;
    record.rotation[3] = 1.0f;
    record.hitPoints = 1;
    net::SendNpcs(&record, 1);
    log("BADRECORD sent=1");
}

// QA (client, stage EdgeClaim): one snapshot of the host NPC with this GUID as if this machine simulated it, where this
// player's hero stands: the state of stageM2, where the client's own copy of a raid scout was near the players while the
// host's was just outside its range.
void ClaimRecord(char const* text, LogFn log)
{
    eCEntity* hero = gCSession::GetInstance().GetHero();
    unsigned d1, d2, d3, b[8];
    if (!hero || net::IsHost() || !text ||
        sscanf_s(text, "%8X-%4X-%4X-%2X%2X-%2X%2X%2X%2X%2X%2X", &d1, &d2, &d3, &b[0], &b[1], &b[2], &b[3], &b[4], &b[5], &b[6], &b[7]) != 11) {
        log("REFUSED claimrecord: client with a hero, <GUID>");
        return;
    }
    _GUID raw;
    raw.Data1 = d1;
    raw.Data2 = static_cast<unsigned short>(d2);
    raw.Data3 = static_cast<unsigned short>(d3);
    for (int i = 0; i < 8; ++i) raw.Data4[i] = static_cast<unsigned char>(b[i]);
    net::NpcRecord record{};
    std::memcpy(record.guid, &raw, 16);
    const bCVector& p = hero->GetWorldPosition();
    record.position[0] = p.GetX() + 200.0f;
    record.position[1] = p.GetY();
    record.position[2] = p.GetZ();
    record.rotation[3] = 1.0f;
    Entity copy = Resolve(record.guid);
    record.hitPoints = copy.GetInstance() ? copy.DamageReceiver.HitPoints : 100;
    net::SendNpcs(&record, 1);
    char line[120];
    Format(line, "CLAIMRECORD sent=1 known_here=%d", copy.GetInstance() ? 1 : 0);
    log(line);
}

char const* MotionNameOf(uint32_t hash)
{
    auto name = motionNames.find(hash);
    return name == motionNames.end() ? nullptr : name->second.c_str();
}

eCEntity* NpcByGuid(uint8_t const guid[16])
{
    eCEntity* instance = Resolve(guid).GetInstance();
    return IsNpc(instance) ? instance : nullptr;
}

// Our hero's blow on a mirror shows at once (Stufe 1, owner 27.10.): its owner judges it a network round later, and until
// then the NPC stood unmoved. The mirror plays a short stumble here, only the look: no hit points, no AI. Its motion is
// one the owner already sent for this skeleton (a stumble's hit motion, of the same weapon stance if one is known), so no
// name is guessed; with none known yet nothing plays. The owner's own snapshot takes over after LocalHitFor: a parade or
// a miss there shows as such then.
bool localHits = true;  // QA negative control: localhitsoff
bool fallbackStumbles = true;  // a first blow on a skeleton the owner sent no stumble of yet
constexpr uint32_t LocalHitFor = 350;  // ms
unsigned localHitsShown = 0, localHitsUnknown = 0;
std::unordered_map<std::string, std::string> stumbleFor;  // "<skeleton>_<stance>" -> a stumble motion the owner sent

std::string StumbleMotion(char const* skeleton, char const* stance)
{
    const std::string key = std::string(skeleton) + "_" + stance;
    if (auto it = stumbleFor.find(key); it != stumbleFor.end()) return it->second;
    std::string any, same;
    const std::string prefix = std::string(skeleton) + "_Stand_";
    for (auto const& [hash, name] : motionNames) {
        if (name.compare(0, prefix.size(), prefix) || name.find("Stumble") == std::string::npos || name.find("_Hit_") == std::string::npos) continue;
        if (name.find("Parade") != std::string::npos) continue;
        if (any.empty()) any = name;
        if (*stance && name.find(std::string("_") + stance + "_") != std::string::npos) { same = name; break; }
    }
    std::string chosen = same.empty() ? any : same;
    // None sent yet (a first blow): the stumbles seen in the play evening's logs, 27.10. (Hero without a weapon
    // StumbleR ..._P0_90, with one QuickStumble ..._P0_90; a waran StumbleR ..._P0_200, used for every other skeleton).
    if (chosen.empty() && fallbackStumbles) {
        if (!std::strcmp(skeleton, "Hero") && (!*stance || !std::strcmp(stance, "None")))
            chosen = "Hero_Stand_None_None_P0_StumbleR_Hit_N_Back_00_%_00_P0_90";
        else if (!std::strcmp(skeleton, "Hero"))
            chosen = std::string("Hero_Stand_None_") + stance + "_P0_QuickStumble_Hit_N_Fwd_00_%_00_P0_90";
        else
            chosen = std::string(skeleton) + "_Stand_None_Fist_P0_StumbleR_Hit_N_Back_00_%_00_P0_200";
        return chosen;  // not kept: a name the owner sends later replaces it
    }
    if (const size_t dot = chosen.find(".xmot"); dot != std::string::npos) chosen.resize(dot);
    if (!chosen.empty()) stumbleFor[key] = chosen;
    return chosen;
}

void LocalHit(eCEntity* entity)
{
    if (!localHits || !entity) return;
    for (auto& [key, m] : mirrors) {
        if (m.entity.GetInstance() != entity || m.killed || !*m.skeleton) continue;
        // The weapon stance from what it shows now (<Skeleton>_<State>_<Sub>_<Weapon>_...), e.g. "1H" of Orc_Stand_None_1H_...
        char shows[128];
        body::CurrentMotion(entity, shows, sizeof(shows));
        char stance[16] = "";
        int field = 0;
        for (char const* c = shows; *c && field < 4; ++c) {
            if (*c == '_') { ++field; continue; }
            if (field == 3) { const size_t n = std::strlen(stance); if (n + 1 < sizeof(stance)) stance[n] = *c, stance[n + 1] = '\0'; }
        }
        const std::string motion = StumbleMotion(m.skeleton, stance);
        if (motion.empty()) { ++localHitsUnknown; return; }
        m.entity.StartPlayAni(bCString(motion.c_str()), 0, GEFalse, 1.0f, GEFalse);
        m.body.localHitAt = net::Now() ? net::Now() : 1;  // its stagger's effect came with our hit (body::staggerEffects)
        m.body.actionUntil = net::Now() + LocalHitFor;  // Drive leaves the pose alone meanwhile, then the owner's state again
        if (++localHitsShown < 10) Logf("npcs: our blow on %s shows here at once (%s)", m.entity.GetName().GetText(), motion.c_str());
        return;
    }
}

bool IsMirrored(eCEntity const* entity)
{
    if (!entity) return false;
    for (auto const& entry : mirrors)
        if (entry.second.body.pacified && entry.second.entity.GetInstance() == entity) return true;
    return false;
}

void OnWorldDeactivate()
{
    mirrors.clear();
    corpses.clear();
    namesSent.clear();
    // A loaded save or another world starts without companions taken over (nothing of them is saved yet).
    claimedBy.clear();
    claimSeen.clear();
    myClaims.clear();
    givenBack.clear();
    contested.clear();
    hostTaken.clear();
    reclaimed.clear();
    reclaimPause.clear();
    sentStates.clear();
    inSphereSince.clear();
    deadOnHost.clear();
    lastSent.clear();
    sending.clear();
}

void CorpseTick(uint32_t now)
{
    for (auto it = corpses.begin(); it != corpses.end();) {
        Mirror& c = it->second;
        c.entity = Resolve(c.latest.guid);
        if (!c.entity.GetInstance()) { ++it; continue; }  // not in this part of the world now; again when it is
        if (deadStays && !net::IsHost() && deadOnHost.count(it->first) && !mirrors.count(it->first)) KeepDead(c.entity);
        if (!c.entity.IsDead() || mirrors.count(it->first)) { it = corpses.erase(it); continue; }  // alive again, or mirrored now
        LayDown(c, now);
        LateCheck(c, now, c.wasMirror ? "mirror" : "journal");
        ++it;
    }
}

void Tick()
{
    gCSession& session = gCSession::GetInstance();
    if (!session.IsGameRunning() || !session.GetHero()) return;
    // Diagnostic: every change of the hero's experience, to find who rewards whom (stage 3c).
    static int lastXp = -1;
    Entity hero(session.GetHero());
    const int xp = static_cast<int>(hero.PlayerMemory.XP);
    if (lastXp >= 0 && xp != lastXp) Logf("xp: %+d total=%d", xp - lastXp, xp);
    lastXp = xp;
    if (net::IsHost()) HostTick();
    else OwnTick();
    MirrorTick();
    CorpseTick(net::Now());
}

void LayDownCorpse(eCEntity* entity)
{
    if (!entity) return;
    uint8_t guid[16];
    Guid(entity, guid);
    const std::string key = Key(guid);
    if (corpses.count(key)) return;
    Mirror& c = corpses[key];
    std::memcpy(c.latest.guid, guid, 16);
    c.killed = true;
    c.killedAt = net::Now() ? net::Now() : 1;
}

void Claim(eCEntity* entity)
{
    if (!entity) return;
    uint8_t guid[16];
    Guid(entity, guid);
    const std::string key = Key(guid);
    myClaims.insert(key);
    // Ours again right after the host took it back (kind 7 follows the revoke within milliseconds): the hand-back pause
    // from Unclaim would keep it from being sent, and the host ends the claim again for want of snapshots (run 223228).
    givenBack.erase(key);
    auto it = mirrors.find(key);
    if (it != mirrors.end()) {
        if (!it->second.killed) body::Release(it->second.entity, it->second.body);  // its own AI again, here
        if (it->second.guarded) it->second.entity.NPC.DefeatedByPlayer = it->second.defeatedBefore;
        mirrors.erase(it);
    }
    Logf("npcs: took over %s (companion)", Entity(entity).GetName().GetText());
}

void Unclaim(eCEntity* entity)
{
    if (!entity) return;
    uint8_t guid[16];
    Guid(entity, guid);
    myClaims.erase(Key(guid));
    givenBack[Key(guid)] = net::Now() + GiveBackFor;  // stop sending it; the host's snapshots make it a mirror again
}

void Foreign(uint8_t const guid[16])
{
    // As for a companion handed back: not sent until the snapshots of the machine that simulates it arrive.
    givenBack[Key(guid)] = net::Now() + GiveBackFor;
}

void Rebind(uint8_t const guid[16])
{
    const auto it = mirrors.find(Key(guid));
    if (it == mirrors.end()) return;
    it->second.resolved = false;  // the next tick resolves it again
    it->second.missing = false;
}

void Revoked(uint8_t const guid[16])
{
    Entity e = Resolve(guid);
    Logf("npcs: the host took back %s", e.GetInstance() ? e.GetName().GetText() : "?");
    if (e.GetInstance()) Unclaim(e.GetInstance());
    else myClaims.erase(Key(guid));
}

bool OnClaim(uint8_t const guid[16], uint8_t player, bool take)
{
    const std::string key = Key(guid);
    const auto current = claimedBy.find(key);
    if (take && current != claimedBy.end() && current->second != player && net::Remotes()[current->second].connected) {
        // Two players recruited it at once: the first keeps it, the second mirrors it again.
        ++claimsDenied;
        SendRevoke(key, player);
        Logf("npcs: player %u cannot take over %s (player %u has it)", player,
             Resolve(guid).GetInstance() ? Resolve(guid).GetName().GetText() : "?", current->second);
        return false;
    }
    if (!take && (current == claimedBy.end() || current->second != player)) return false;  // not its companion
    if (take) {
        // No placeholder mirror: one without a snapshot drove the NPC to the origin (0,0,0), out of processing.
        // The player's first snapshot creates the mirror; until then the host just stops sending it.
        claimedBy[key] = player;
        claimSeen[key] = net::Now();
        companions[key] = net::Remotes()[player].latest.name;
        DropMirror(key);  // an older mirror from another owner
    } else {
        claimedBy.erase(key);
        companions.erase(key);
        claimSeen.erase(key);
        DropMirror(key);  // the host simulates and sends it again; the player stops sending it
    }
    Logf("npcs: player %u %s %s", player, take ? "took over" : "gave back", Resolve(guid).GetInstance() ? Resolve(guid).GetName().GetText() : "?");
    return true;
}

void PlaceGivenBack(uint8_t const guid[16], float const position[3], float const rotation[4])
{
    // While the player had it far from here, nothing moved this copy: the escaped slave stood 290 m from Freman on the host
    // after the client had brought him there, and both simulated their own copy (escortfardiag, 25.10.).
    Entity e = Resolve(guid);
    eCEntity* instance = e.GetInstance();
    if (!IsNpc(instance) || e.IsDead()) return;
    const bCVector& p = instance->GetWorldPosition();
    const float here[3] = {p.GetX(), p.GetY(), p.GetZ()};
    const float off = body::Distance(here, position);
    if (off < 1000.0f) return;
    e.MoveTo(body::Pose(*instance, position, rotation));
    Logf("npcs: %s given back %.0f m from its copy here; moved to where the player left it", e.GetName().GetText(), off / 100.0f);
}

void StoryMoved(uint8_t const guid[16], float const position[3], float const rotation[4])
{
    // OnEnclaveSectorRefresh on the host moved this NPC far (infocmd.cpp, kind 1 with moved 2). A mirror here was put back
    // to the host's last snapshot by its body driver (stageSectorRefresh 26.10.: Diego stayed at Ardea on the client, 1751 m
    // from the host's in Braga). The mirror ends; the copy goes where the host's stands, until the host sends it again.
    DropMirror(Key(guid));
    Entity e = Resolve(guid);
    eCEntity* instance = e.GetInstance();
    if (!IsNpc(instance) || e.IsDead()) return;
    e.MoveTo(body::Pose(*instance, position, rotation));
    Logf("npcs: the story moved %s on the host; moved here too", e.GetName().GetText());
}

bool companionWins = true;  // false: the host takes a client's companion for a guide too (as before 27.10.)

bool HostTake(eCEntity* entity)
{
    if (!entity || !net::IsHost()) return false;
    uint8_t guid[16];
    Guid(entity, guid);
    const std::string key = Key(guid);
    // A client's companion stays with its player: taken back here for a guide, its player's snapshots were refused, the claim
    // ran out and the host handed it over again every 15 s (Gorn guiding the host while the client's companion, playtest 27.10.).
    if (companionWins)
        if (const auto c = claimedBy.find(key); c != claimedBy.end() && net::Remotes()[c->second].connected) return false;
    auto it = mirrors.find(key);
    if (it == mirrors.end()) return false;
    const unsigned owner = it->second.owner;
    hostTaken.insert(key);
    if (!it->second.killed) body::Release(it->second.entity, it->second.body);  // its own AI again, here
    if (it->second.guarded) it->second.entity.NPC.DefeatedByPlayer = it->second.defeatedBefore;
    mirrors.erase(it);
    Logf("npcs: took %s from player %u (it guides or follows the host)", Entity(entity).GetName().GetText(), owner);
    return true;
}

void HostRelease(eCEntity* entity)
{
    if (!entity) return;
    uint8_t guid[16];
    Guid(entity, guid);
    hostTaken.erase(Key(guid));
}

bool HostTaken(eCEntity const* entity)
{
    if (!entity) return false;
    uint8_t guid[16];
    Guid(const_cast<eCEntity*>(entity), guid);
    return hostTaken.count(Key(guid)) != 0;
}

void Mirrored(std::vector<eCEntity*>& out)
{
    for (auto& [key, m] : mirrors)
        if (m.resolved && !m.missing && !m.killed && m.entity.GetInstance()) out.push_back(m.entity.GetInstance());
}

bool DeadStays() { return deadStays; }

uint8_t CompanionOwner(eCEntity* entity)
{
    if (!entity || claimedBy.empty()) return 0;
    uint8_t guid[16];
    Guid(entity, guid);
    const auto it = claimedBy.find(Key(guid));
    return it != claimedBy.end() ? it->second : 0;
}

void NewLife(uint8_t const guid[16])
{
    if (newLife && deadOnHost.erase(Key(guid))) Logf("npcs: %s no longer held dead here (it stood up again)", Resolve(guid).GetName().GetText());
}

bool Claimed(eCEntity const* entity)
{
    if (!entity) return false;
    uint8_t guid[16];
    Guid(const_cast<eCEntity*>(entity), guid);
    return myClaims.count(Key(guid)) != 0;
}

void OwnOff(LogFn log)
{
    owning = false;
    log("OWNOFF ok=1");
}

uint8_t ClaimedBy(eCEntity const* entity)
{
    if (!entity || !net::IsHost()) return 0;
    uint8_t guid[16];
    Guid(const_cast<eCEntity*>(entity), guid);
    const auto it = claimedBy.find(Key(guid));
    return it == claimedBy.end() ? 0 : it->second;
}

uint8_t OwnerOf(eCEntity const* entity)
{
    if (!entity) return 0;
    for (auto const& entry : mirrors)
        if (entry.second.entity.GetInstance() == entity) return entry.second.owner;
    return 0;
}

bool PaysDeathWithoutKiller(eCEntity const* victim)
{
    if (net::IsHost() || !farXpGuard || !victim || IsMirrored(victim) || Claimed(victim)) return true;
    uint8_t guid[16];
    Guid(const_cast<eCEntity*>(victim), guid);
    if (owned.count(Key(guid))) return true;
    ++farXpRefused;
    return false;
}

void FarXpOff(LogFn log)
{
    farXpGuard = false;
    log("FARXPOFF ok=1");
}

void XpGuardOff(LogFn log)
{
    xpGuard = false;
    log("XPGUARDOFF ok=1");
}

void Report(LogFn log)
{
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    if (!hero) return;
    const bCVector& h = hero->GetWorldPosition();
    const float heroAt[3] = {h.GetX(), h.GetY(), h.GetZ()};
    const bTObjArray<Entity> all = Entity::GetNPCs();
    char line[400];
    for (int i = 0; i < all.GetCount(); ++i) {
        Entity npc = all.GetAt(i);
        eCEntity* instance = npc.GetInstance();
        if (!instance || instance == hero || remote::IsPuppet(instance)) continue;
        const bCVector& p = instance->GetWorldPosition();
        const float at[3] = {p.GetX(), p.GetY(), p.GetZ()};
        if (body::Distance(at, heroAt) > InterestRadius) continue;
        uint8_t guid[16];
        Guid(instance, guid);
        char text[48];
        FormatGuid(guid, text, sizeof(text));
        auto mirror = mirrors.find(Key(guid));
        Format(line, "NPC guid=%s name=%s pos=%.1f,%.1f,%.1f hp=%d dead=%d mirrored=%d task=%s repacified=%u last_foreign=%s owner=%u", text,
                  npc.GetName().GetText(), at[0], at[1], at[2], static_cast<int>(npc.DamageReceiver.HitPoints), npc.IsDead(),
                  mirror != mirrors.end() && mirror->second.body.pacified, npc.Routine.GetCurrentTask().GetText(),
                  mirror != mirrors.end() ? mirror->second.body.repacified : 0,
                  mirror != mirrors.end() && *mirror->second.body.lastForeign ? mirror->second.body.lastForeign : "-",
                  mirror != mirrors.end() ? mirror->second.owner : 0);
        log(line);
    }
    Format(line, "NPCSYNC claims_refused=%u host_wins=%u host_wins_rule=%d join_refused=%u join_grace=%d contested_refused=%u contested=%zu contest_rule=%d reclaims=%u reclaim_refused=%u reclaimed=%zu reclaim_rule=%d",
              claimsRefused, hostWins, hostWinsRule ? 1 : 0, joinRefused, joinGraceRule ? 1 : 0, contestedRefused, contested.size(), contestRule ? 1 : 0,
              reclaims, reclaimRefused, reclaimed.size(), reclaimRule ? 1 : 0);
    log(line);
}

// QA (host, stage EdgeClaim): the NPCs this machine processes in its band, between InterestRadius and KeepRadius of the
// nearest player, with why its last snapshots left them out.
void EdgeReport(LogFn log)
{
    const bTObjArray<Entity> all = Entity::GetNPCs();
    char line[300];
    for (int i = 0; i < all.GetCount(); ++i) {
        Entity npc = all.GetAt(i);
        eCEntity* instance = npc.GetInstance();
        if (!instance || instance == gCSession::GetInstance().GetHero() || remote::IsPuppet(instance) || npc.IsDead()) continue;
        const bCVector& p = instance->GetWorldPosition();
        const float at[3] = {p.GetX(), p.GetY(), p.GetZ()};
        float nearest = 1e30f;
        for (int k = 0; k < lastPlayerCount; ++k) nearest = std::min(nearest, body::Distance(at, lastPlayers[k]));
        if (nearest <= InterestRadius || nearest > KeepRadius) continue;
        uint8_t guid[16];
        Guid(instance, guid);
        char text[48];
        FormatGuid(guid, text, sizeof(text));
        const auto note = sendNotes.find(Key(guid));
        Format(line, "EDGENPC guid=%s name=%s nearest_m=%.1f reason=%s age_ms=%d mirror=%d", text, npc.GetName().GetText(), nearest / 100.0f,
                  note != sendNotes.end() ? note->second.reason : "never", note != sendNotes.end() ? static_cast<int>(net::Now() - note->second.at) : -1,
                  mirrors.count(Key(guid)) ? 1 : 0);
        log(line);
    }
    log("EDGENPC end");
}

void SendInfo(char const* name, LogFn log)
{
    // A GUID too (stage EdgeClaim: scavengers share one name).
    unsigned d1, d2, d3, b[8];
    Entity npc;
    if (std::strlen(name) == 36 &&
        sscanf_s(name, "%8X-%4X-%4X-%2X%2X-%2X%2X%2X%2X%2X%2X", &d1, &d2, &d3, &b[0], &b[1], &b[2], &b[3], &b[4], &b[5], &b[6], &b[7]) == 11) {
        _GUID raw;
        raw.Data1 = d1;
        raw.Data2 = static_cast<unsigned short>(d2);
        raw.Data3 = static_cast<unsigned short>(d3);
        for (int i = 0; i < 8; ++i) raw.Data4[i] = static_cast<unsigned char>(b[i]);
        uint8_t bytes[16];
        std::memcpy(bytes, &raw, 16);
        npc = Resolve(bytes);
    } else {
        npc = Entity::GetEntity(bCString(name));
    }
    char line[300];
    if (!npc.GetInstance()) { log("REFUSED npcsend: no such NPC"); return; }
    uint8_t guid[16];
    Guid(npc.GetInstance(), guid);
    const std::string key = Key(guid);
    const auto note = sendNotes.find(key);
    const auto back = givenBack.find(key);
    char text[48];
    FormatGuid(guid, text, sizeof(text));
    Format(line, "NPCSEND name=%s guid=%s reason=%s age_ms=%d myclaim=%d mirror=%d givenback_ms=%d", name, text,
              note != sendNotes.end() ? note->second.reason : "never",
              note != sendNotes.end() ? static_cast<int>(net::Now() - note->second.at) : -1, myClaims.count(key) ? 1 : 0,
              mirrors.count(key) ? 1 : 0, back != givenBack.end() ? static_cast<int>(back->second - net::Now()) : 0);
    log(line);
}

void JoinGraceOff(LogFn log)
{
    joinGraceRule = false;
    log("JOINGRACEOFF ok=1");
}

void ContestOff(LogFn log)
{
    contestRule = false;
    log("CONTESTOFF ok=1");
}

void HostWinsOff(LogFn log)
{
    hostWinsRule = false;
    log("HOSTWINSOFF ok=1");
}
}

namespace g3t::npcs {
// Client: the host hands back a companion from its save. Recruit it again here exactly as the dialog command
// JoinPlayer true does (Script_Game.dll 0xB7F10: stop, SlaveryEnabled, the script JoinPlayerSlavery, which sets leader =
// the local hero, not waiting, type Slave; then ZS_FollowPlayer). The game's own script rather than the fields by hand:
// whether the leader's member list needs more than SetPartyLeader is unknown (static analysis, re/findings/party-members.md).
void Adopt(uint8_t const guid[16])
{
    Entity e = Resolve(guid);
    if (!IsNpc(e.GetInstance())) return;
    Claim(e.GetInstance());
    if (guide::Readopt(e.GetInstance())) { ++companionsAdopted; return; }  // a guide of a running follow quest (P9)
    if (arena::Readopt(e.GetInstance())) return;  // an arena opponent taken over for a fight: the host's again
    e.Routine.FullStop();
    e.Dialog.SlaveryEnabled = GETrue;
    GetScriptAdminExt().CallScript(bCString("JoinPlayerSlavery"), e.GetInstance(), nullptr, 0);
    e.Routine.SetTask(bCString("ZS_FollowPlayer"));
    ++companionsAdopted;
    Logf("npcs: %s is our companion again (from the host's save)", e.GetName().GetText());
}

unsigned WriteCompanions(FILE* file)
{
    unsigned n = 0;
    for (auto const& [key, name] : companions) {
        fprintf(file, "companion ");
        for (unsigned char c : key) fprintf(file, "%02x", c);
        fprintf(file, " %s\n", name.c_str());
        ++n;
    }
    return n;
}

unsigned ReadCompanions(FILE* file)
{
    companions.clear();
    offered.clear();
    char line[256];
    unsigned n = 0;
    while (fgets(line, sizeof(line), file)) {
        // The player's name is the rest of the line (names may contain spaces).
        char hex[33] = {};
        int consumed = 0;
        if (sscanf_s(line, "companion %32s %n", hex, 33u, &consumed) != 1 || !consumed) continue;
        std::string name = line + consumed;
        while (!name.empty() && (name.back() == '\n' || name.back() == '\r')) name.pop_back();
        if (name.empty()) continue;
        std::string key(16, '\0');
        for (int i = 0; i < 16; ++i) {
            unsigned v = 0;
            sscanf_s(hex + 2 * i, "%2x", &v);
            key[i] = static_cast<char>(v);
        }
        companions[key] = name;
        ++n;
    }
    return n;
}

void ClearCompanions()
{
    companions.clear();
    offered.clear();
}
}

namespace g3t::npcs {
void CompanionsOff(LogFn log)
{
    offering = false;
    log("COMPANIONSOFF ok=1");
}
}

namespace g3t::npcs {
void StyleOff(LogFn log)
{
    style = false;
    log("STYLEOFF ok=1");
}

void MirrorRetryOff(LogFn log)
{
    retryMissing = false;
    log("MIRRORRETRYOFF ok=1 (a mirror missing at its first lookup stays missing and stays, as before 23.10.)");
}

void MirrorStats(LogFn log)
{
    unsigned missing = 0;
    for (auto const& entry : mirrors)
        if (entry.second.resolved && (entry.second.missing || !entry.second.entity.GetInstance())) ++missing;
    char line[200];
    Format(line, "MIRRORSTATS mirrors=%zu missing=%u found_later=%u expired=%u lookups=%u retry=%d", mirrors.size(), missing,
              mirrorsFoundLater, mirrorsExpired, mirrorLookups, retryMissing ? 1 : 0);
    log(line);
}
}

namespace g3t::npcs {
unsigned HoldChanges() { return holdChanges; }
}

namespace g3t::npcs {
void RecvOn(LogFn log)
{
    receiving = true;
    log("NPCRECVON ok=1");
}

void RecvOff(LogFn log)
{
    receiving = false;
    log("NPCRECVOFF ok=1");
}
}

namespace g3t::npcs {
void DeadPoseOff(LogFn log)
{
    deadPose = false;
    log("DEADPOSEOFF ok=1");
}

void DeadPoseReport(LogFn log)
{
    char line[700];
    // Per mirror: the motion on slot 0, whether it runs, and the height of the animated body (lying: low).
    unsigned alive = 0;
    auto report = [&](Mirror& m, char const* kind) {
        char shows[128], slots[400];
        Lies(m.entity.GetInstance(), shows, sizeof(shows), slots, sizeof(slots));
        Format(line, "DEADPOSE mirror=%s killed=%d running=%d height=%.0f ani=%d motion=%s slots=%s kind=%s", m.entity.GetName().GetText(),
                  m.killed ? 1 : 0, body::MotionRunning(m.entity.GetInstance()) ? 1 : 0, body::BoundaryHeight(m.entity.GetInstance()),
                  static_cast<int>(m.entity.Routine.AniState), *shows ? shows : "-", slots, kind);
        log(line);
    };
    for (auto& entry : mirrors) {
        Mirror& m = entry.second;
        if (!m.entity.GetInstance() || (!m.killed && alive++ >= 3)) continue;
        report(m, "mirror");
    }
    for (auto& entry : corpses) {  // the "mirror=" key stays: the suites parse it
        Mirror& c = entry.second;
        c.entity = Resolve(c.latest.guid);
        if (c.entity.GetInstance()) report(c, "journal");
    }
    Format(line, "DEADPOSE enabled=%d laid=%u loops=%u slots_stopped=%u", deadPose ? 1 : 0, corpsesLaid, corpseLoops, slotsStopped);
    log(line);
}
// QA negative controls of the play-evening fixes (27.10.): each switches its fix off (on: clientfameon).
bool Switch(char const* action, LogFn log)
{
    if (!std::strcmp(action, "localhitsoff")) { localHits = false; log("SWITCH localhitsoff ok=1"); return true; }
    if (!std::strcmp(action, "fastnpcsoff")) { fastNpcs = false; log("SWITCH fastnpcsoff ok=1"); return true; }
    if (!std::strcmp(action, "deadstaysoff")) { deadStays = false; log("SWITCH deadstaysoff ok=1"); return true; }
    if (!std::strcmp(action, "deadnewlifeoff")) { newLife = false; log("SWITCH deadnewlifeoff ok=1"); return true; }
    if (!std::strcmp(action, "companionnearoff")) { companionNear = false; log("SWITCH companionnearoff ok=1"); return true; }
    if (!std::strcmp(action, "hysteresisoff")) { hysteresis = false; log("SWITCH hysteresisoff ok=1"); return true; }
    if (!std::strcmp(action, "companionwinsoff")) { companionWins = false; log("SWITCH companionwinsoff ok=1"); return true; }
    if (!std::strcmp(action, "reclaimoff")) { reclaimRule = false; log("SWITCH reclaimoff ok=1"); return true; }
    if (!std::strcmp(action, "lagblockoff")) { lagBlocks = false; log("SWITCH lagblockoff ok=1"); return true; }
    if (!std::strcmp(action, "lagblockon")) { lagBlocks = true; log("SWITCH lagblockon ok=1"); return true; }
    return false;
}
}
