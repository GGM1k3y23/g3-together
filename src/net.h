// SPDX-License-Identifier: GPL-3.0-or-later
// Transport: ENet over UDP. One player hosts (id 1), others connect. Kept free of SDK headers so that
// winsock2 and the game's headers never meet in one translation unit.
#pragma once

#include <cstdint>

namespace g3t::net {
#pragma pack(push, 1)
struct PlayerState
{
    uint8_t id = 0;
    uint32_t sequence = 0;
    uint32_t world = 0;   // FNV-1a of the world's file base name
    float position[3] = {};
    float rotation[4] = {};  // quaternion x, y, z, w
    uint8_t flags = 0;       // bit 0: in game (hero present, session running), bit 1: knocked out, bit 2: dead
    uint8_t movementMode = 0;  // gECharMovementMode of the hero: stand, sneak, walk, run, sprint, jump, swim
    uint8_t aniState = 0;      // gEAniState of the hero (stand 2, lying knocked out 16, ...): which motions fit
    char name[24] = {};
    char animation[96] = {};  // the hero's current movement animation (an existing game motion)
    char heldRight[40] = {};  // item names in the hero's hands (drawn weapon, shield, bow); empty = nothing
    char heldLeft[40] = {};
    char shape[32] = {};     // template of the body the player controls instead of its hero (transformed: Wolf), empty = its hero
};
#pragma pack(pop)

// One NPC as the host sees it. Identity is the entity's GUID, which is the same in every copy of the world.
#pragma pack(push, 1)
struct NpcRecord
{
    uint8_t guid[16] = {};
    float position[3] = {};
    float rotation[4] = {};
    uint8_t movementMode = 0;
    uint8_t flags = 0;       // bit 0: dead, bit 1: down (knocked out)
    int32_t hitPoints = 0;
    uint32_t motion = 0;     // hash of the visible motion's name; names travel once via SendMotionName
    uint8_t aniState = 0;    // gEAniState (2 standing, 4 fighting stance ...); 0 = unknown
    uint32_t heldRight = 0;  // HashName of the item in the right hand (a drawn weapon), 0 = empty
    uint32_t heldLeft = 0;   // the same for the left hand (shield, bow)
};
#pragma pack(pop)

struct Remote
{
    PlayerState previous;
    PlayerState latest;
    uint32_t previousTicks = 0;  // net::Now() when previous/latest arrived
    uint32_t latestTicks = 0;
    bool connected = false;
};

bool Start(bool host, char const* address, unsigned short port, char const* name);
void Stop();  // at shutdown: disconnect now so the others notice at once
// Service the socket and, at most every 50 ms, send the local state. Call once per frame.
void Tick(PlayerState const& local, bool haveLocal);
uint8_t LocalId();
bool Connected();
Remote const* Remotes();  // indexed by player id, size MaxPlayers
constexpr int MaxPlayers = 8;
uint32_t HashWorld(char const* name);
uint32_t HashName(char const* name);
bool IsHost();
// Increments whenever a client joins; the host resends its motion-name table then.
uint32_t JoinCount();

// NPC snapshots (unreliable) and motion names (reliable): host -> every client; client -> host (its own region,
// passed on by the host).
constexpr int MaxNpcsPerMessage = 48;
void SendNpcs(NpcRecord const* records, int count);
void SendMotionName(uint32_t hash, char const* name);
// Client side: received NPC snapshots and motion names are handed to these callbacks.
// owner: the player whose machine simulates these NPCs (1 = host; a client sends its own region's NPCs).
using NpcHandler = void (*)(NpcRecord const* records, int count, uint32_t now, uint8_t owner);
using MotionNameHandler = void (*)(uint32_t hash, char const* name);
void SetNpcHandlers(NpcHandler npcs, MotionNameHandler names);

// Damage, reliable. Client -> host: this client hit an NPC (by GUID) for delta (negative) hit points.
// Host -> one client: an NPC hit that player's puppet for delta.
// Any machine -> the NPC's owner `to` (through the host): this player hit it. weapon: template ID, zeros = none.
// npc: the GUID of one of the sender's NPCs that hit it (zeros or null: the sender's hero).
void SendHitNpc(uint8_t to, uint8_t const guid[16], int32_t delta, uint8_t const weapon[16], uint8_t const npc[16] = nullptr);
// attacker: the hitting NPC (GUID), or zeros with attackerPlayer = the player whose hero hit.
void SendHitPlayer(uint8_t playerId, int32_t delta, uint8_t const attacker[16], uint8_t attackerPlayer);
using HitNpcHandler = void (*)(uint8_t from, uint8_t const guid[16], int32_t delta, uint8_t const weapon[16], uint8_t const npc[16]);
using HitPlayerHandler = void (*)(int32_t delta, uint8_t const attacker[16], uint8_t attackerPlayer);
void SetHitHandlers(HitNpcHandler npc, HitPlayerHandler player);
// Host -> one client: your hit killed the NPC with this GUID (the killer earns the experience).
void SendKilled(uint8_t to, uint8_t const guid[16]);
using KilledHandler = void (*)(uint8_t const guid[16]);
void SetKilledHandler(KilledHandler handler);
// An NPC died where nobody mirrored it (journal.cpp). Client: to the host. Host: to every client except
// `except`, with `by` = the player in whose region it died.
void SendDeath(uint8_t const guid[16], uint8_t by, uint8_t except);
using DeathHandler = void (*)(uint8_t by, uint8_t const guid[16]);
void SetDeathHandler(DeathHandler handler);
// Host -> every client: the campaign was saved under `name` (kind 1).
void SendCampaign(uint8_t kind, char const* name);
using CampaignHandler = void (*)(uint8_t kind, char const* name);
void SetCampaignHandler(CampaignHandler handler);
// Host -> one client: the campaign save files (.g3savcpx, .g3savcpxdat, .g3t) in chunks, then a summary
// with sizes and CRC-32s (persist.cpp).
constexpr int FileCount = 3;
constexpr uint16_t MaxChunk = 16000;
bool SendFileChunk(uint8_t to, uint8_t file, uint32_t total, uint32_t offset, void const* data, uint16_t length);
bool SendFileDone(uint8_t to, char const* name, uint32_t const size[FileCount], uint32_t const crc[FileCount]);
using FileChunkHandler = void (*)(uint8_t file, uint32_t total, uint32_t offset, uint8_t const* data, uint16_t length);
using FileDoneHandler = void (*)(char const* name, uint32_t const size[FileCount], uint32_t const crc[FileCount]);
void SetFileHandlers(FileChunkHandler chunk, FileDoneHandler done);
// Host -> every client: the game clock (clock.cpp).
#pragma pack(push, 1)
struct ClockState { uint32_t timestamp = 0; uint16_t year = 0; uint16_t day = 0; uint8_t hour = 0; uint8_t minute = 0; };
#pragma pack(pop)
void SendClock(ClockState const& state);  // host: to every client; client: a request to the host (a sleep)
using ClockHandler = void (*)(ClockState const& state);
void SetClockHandler(ClockHandler handler);
// A world state after a dialog command (infocmd.cpp). kind 1: an entity's state by GUID; kind 2: a sector
// (name in routine, status in enclave); kind 3: a dialog started a fight (guid = attacker, guard = the player
// attacked, routine = the attack reason's name); kind 4/5: a client took over / gave back a companion (guid); kind 6: host to player `guard`: that claim is refused
// or ended; kind 7: host to player `guard`: this NPC from the host's save is your companion.
// Client: to the host; host: to every client except `except`.
#pragma pack(push, 1)
struct WorldState {
    uint8_t kind = 0;
    uint8_t guid[16] = {};
    uint8_t dialog = 0;     // bits: teach, trade, party, mob, slavery; 0x80: has a dialog
    uint8_t npc = 0;        // 1: has the NPC fields below
    uint8_t guard = 0, npcClass = 0, alignment = 0, immortal = 0;
    uint8_t enclave = 0;    // bits: has an enclave, raid, revolution
    uint8_t enclaveStatus = 0;
    uint8_t moved = 0;      // 1: a MoveTo command moved it; only then is the position taken
    char routine[48] = {};
    float position[3] = {};
    float rotation[4] = {};
};
#pragma pack(pop)
void SendWorldState(WorldState const& state, uint8_t except);
using WorldStateHandler = void (*)(uint8_t from, WorldState const& state);
void SetWorldStateHandler(WorldStateHandler handler);

// Named story state (story.cpp), reliable. Host -> all clients: the host's values. Client -> host: values this
// player changed; an empty message asks the host for its full state of that kind. A message holds at most
// MaxStatesPerMessage entries; callers split larger sets.
enum : uint8_t { StateQuests = 1, StateInfos = 2, StateEvents = 3 };
struct StateEntry {
    uint8_t value;
    char name[63];
};
constexpr int MaxStatesPerMessage = 64;
void SendStates(uint8_t kind, StateEntry const* entries, int count);
using StateHandler = void (*)(uint8_t from, uint8_t kind, StateEntry const* entries, int count);  // from: client id, 1 = host
void SetStateHandler(StateHandler handler);

// Dialogs, reliable, client -> host: this client's hero began (or ended) a dialog with the NPC with this GUID.
void SendTalk(bool begin, uint8_t const guid[16]);
using TalkHandler = void (*)(uint8_t from, bool begin, uint8_t const guid[16]);
void SetTalkHandler(TalkHandler handler);

// World items, reliable. Client -> host: ItemTook (this player's hero took that world item). Host -> clients:
// ItemGone (remove it from the world). Host -> one client: ItemDenied (it was gone already; give it back).
enum : uint8_t { ItemTook = 1, ItemGone = 2, ItemDenied = 3 };
struct ItemNote {
    uint8_t op;
    uint8_t guid[16];
    uint32_t amount;
    char name[48];  // item template
};
void SendItem(ItemNote const& note, uint8_t to);  // client: to the host (to ignored); host: to that player, 0 = all
using ItemHandler = void (*)(uint8_t from, ItemNote const& note);
void SetItemHandler(ItemHandler handler);

// Containers (containers.cpp), reliable. Host -> all: a container's contents. Client -> host: a transaction
// (container-side deltas). Host -> one client: its result. Client -> host: watch this trader for a while.
struct ContentEntry {
    uint8_t id[16];  // item template ID (names are not unique: "Appow" and "Arrow" merged when recreated by name)
    char name[28];   // for logs
    int32_t amount;
};
constexpr int MaxContentEntries = 48;
void SendContents(uint8_t const guid[16], ContentEntry const* entries, int count);
void SendContainerTx(uint8_t const guid[16], uint32_t seq, ContentEntry const* entries, int count);
void SendContainerTxResult(uint8_t to, uint8_t const guid[16], uint32_t seq, bool ok);
void SendWatch(uint8_t const guid[16]);
using ContentsHandler = void (*)(uint8_t const guid[16], ContentEntry const* entries, int count);
using ContainerTxHandler = void (*)(uint8_t from, uint8_t const guid[16], uint32_t seq, ContentEntry const* entries, int count);
using TxResultHandler = void (*)(uint8_t const guid[16], uint32_t seq, bool ok);
using WatchHandler = void (*)(uint8_t from, uint8_t const guid[16]);
void SetContainerHandlers(ContentsHandler contents, ContainerTxHandler tx, TxResultHandler result, WatchHandler watch);
// Summoned things (summon.cpp, P1), reliable. A player's own summon got a net ID (caster id in the top byte); the
// others replay it on that player's puppet. op 1: spawned; op 2: gone. Client -> host; host -> every other client
// (relayed, `from` = the caster).
enum : uint8_t { SummonSpawned = 1, SummonGone = 2 };
#pragma pack(push, 1)
struct SummonNote {
    uint8_t op = 0;
    uint32_t netId = 0;
    uint8_t target[16] = {};  // GUID of the spell's target (zeros: none)
    float position[3] = {};
    float rotation[4] = {};
    char spellItem[40] = {};  // the item cast from (It_Spell_Meteor, a scroll ...)
    char spawn[40] = {};      // the summoned entity's template (Smn_Meteor ...)
};
#pragma pack(pop)
void SendSummon(SummonNote const& note);
using SummonHandler = void (*)(uint8_t from, SummonNote const& note);
void SetSummonHandler(SummonHandler handler);
// Shots (shot.cpp, P2), reliable: a player's own arrow or spell projectile as it set off; the others fly a copy that belongs to that
// player's puppet. Client -> host; host -> every other client (relayed, `from` = the shooter).
#pragma pack(push, 1)
struct ShotNote {
    uint32_t netId = 0;
    uint8_t target[16] = {};  // GUID of the shooter's target (zeros: none)
    float position[3] = {};
    float rotation[4] = {};
    float direction[3] = {};  // PSProjectile.TargetDirection
    float velocity = 0.0f;    // PSProjectile.ShootVelocity
    uint8_t path = 0;         // gEProjectilePath
    uint8_t touch = 0;        // gEProjectileTouchBehavior
    int32_t damage = 0;       // PSDamage.DamageAmount (arrow plus bow)
    float multiplier = 0.0f;  // PSDamage.DamageHitMultiplier
    char arrow[40] = {};      // the arrow's template (or a spell's projectile: Mis_Fireball ...)
    char spellItem[40] = {};  // a spell's projectile: the item cast from (empty for arrows)
};
#pragma pack(pop)
void SendShot(ShotNote const& note);
using ShotHandler = void (*)(uint8_t from, ShotNote const& note);
void SetShotHandler(ShotHandler handler);
// A spell a player cast on another player's puppet (spells.cpp): the target player replays the script on its own hero.
// Any machine -> player `to` (through the host). script: the script's name (only a fixed list is replayed).
void SendPlayerSpell(uint8_t to, char const* script);
using PlayerSpellHandler = void (*)(uint8_t from, char const* script);
void SetPlayerSpellHandler(PlayerSpellHandler handler);
// The clock that stamps received states (ENet's). Compare stamps only against this clock.
uint32_t Now();
}
