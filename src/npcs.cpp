// SPDX-License-Identifier: GPL-3.0-or-later
#include "npcs.h"

#include "body.h"
#include "guide.h"
#include "arena.h"
#include "log.h"
#include "net.h"
#include "remote.h"
#include "journal.h"
#include "talk.h"

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

namespace g3t::npcs {
namespace {
constexpr float InterestRadius = 4000.0f;  // cm around any player
constexpr uint32_t SendInterval = 100;     // ms
constexpr uint32_t StaleAfter = 3000;      // ms without a snapshot: give the NPC its own AI back

// Owned NPCs (the host's processing area; a client's own region): sent to the others
uint32_t lastSend = 0;
uint32_t knownJoins = 0;
bool wasConnected = false;
std::unordered_set<uint32_t> namesSent;
std::unordered_set<std::string> owned;  // GUIDs this machine sent in its last update
bool owning = true;                     // QA negative control (client): send nothing of its own
unsigned claimsRefused = 0;             // host: a client's snapshots of NPCs the host simulates itself
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

void OnMotionName(uint32_t hash, char const* name)
{
    if (motionNames.size() < 50000 || motionNames.count(hash)) motionNames[hash] = name;  // the game has some 20000 motions
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
        if (claimedBy.count(Key(guid))) { note.reason = "claimed"; continue; }
        const auto back = givenBack.find(Key(guid));
        if (back != givenBack.end() && static_cast<int32_t>(net::Now() - back->second) < 0) { note.reason = "givenback"; continue; }  // handed back
        const bCVector& p = instance->GetWorldPosition();
        const float at[3] = {p.GetX(), p.GetY(), p.GetZ()};
        float nearest = 1e30f;
        for (int k = 0; k < playerCount; ++k) nearest = std::min(nearest, body::Distance(at, players[k]));
        bool inRange = nearest <= InterestRadius;
        if (!inRange) {
            const auto held = contested.find(Key(guid));
            if (held != contested.end() && nearest <= KeepRadius) inRange = true;  // contested: kept here in the band
            else if (held != contested.end()) contested.erase(held);
        }
        if (!inRange) { note.reason = "range"; continue; }
        note.reason = "sent";
        net::NpcRecord& r = batch[n];
        r = net::NpcRecord{};
        std::memcpy(r.guid, guid, 16);
        owned.insert(Key(guid));
        std::memcpy(r.position, at, sizeof(at));
        const bCQuaternion q = instance->GetWorldRotation();
        r.rotation[0] = q.GetX(); r.rotation[1] = q.GetY(); r.rotation[2] = q.GetZ(); r.rotation[3] = q.GetW();
        Entity self(instance);
        r.movementMode = static_cast<uint8_t>(self.CharacterMovement.GetMovementMode());
        r.flags = (self.IsDead() ? 1 : 0) | (self.IsDown() ? 2 : 0);
        r.hitPoints = self.DamageReceiver.HitPoints;
        r.aniState = static_cast<uint8_t>(static_cast<int>(self.Routine.AniState));
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
        }
        if (++n == net::NpcsPerSend) { net::SendNpcs(batch, n); n = 0; }
    }
    if (n) net::SendNpcs(batch, n);
}

void HostTick()
{
    const uint32_t now = net::Now();
    if (now - lastSend < SendInterval) return;
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
    if (now - lastSend < SendInterval) return;
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
            if (!Resolve(reinterpret_cast<uint8_t const*>(key.data())).GetInstance()) break;
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
        const body::Target target{m.latest.position, m.latest.rotation, m.latest.movementMode,
                                  name != motionNames.end() ? name->second.c_str() : nullptr, speed,
                                  style && m.latest.aniState ? static_cast<int>(m.latest.aniState) : -1};
        m.body.combatMoves = true;  // attacks replay as the game's combat moves (body.cpp)
        body::Drive(m.entity, m.body, target, now);
        ++it;
    }
}
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
}

void CorpseTick(uint32_t now)
{
    for (auto it = corpses.begin(); it != corpses.end();) {
        Mirror& c = it->second;
        c.entity = Resolve(c.latest.guid);
        if (!c.entity.GetInstance()) { ++it; continue; }  // not in this part of the world now; again when it is
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

bool HostTake(eCEntity* entity)
{
    if (!entity || !net::IsHost()) return false;
    uint8_t guid[16];
    Guid(entity, guid);
    const std::string key = Key(guid);
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
    Format(line, "NPCSYNC claims_refused=%u host_wins=%u host_wins_rule=%d join_refused=%u join_grace=%d contested_refused=%u contested=%zu contest_rule=%d",
              claimsRefused, hostWins, hostWinsRule ? 1 : 0, joinRefused, joinGraceRule ? 1 : 0, contestedRefused, contested.size(), contestRule ? 1 : 0);
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
}
