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
    uint32_t sent = 0;    // the sender's net::Now() when it went out: speed from send times, not arrival (23.10.)
    uint32_t world = 0;   // FNV-1a of the world's file base name
    float position[3] = {};
    float rotation[4] = {};  // quaternion x, y, z, w
    uint8_t flags = 0;       // bit 0: in game (hero present, session running), bit 1: knocked out, bit 2: dead,
                             // bit 3: waiting for help (a death turned into a knock-out, downed.cpp), bit 4: knocked out
                             // by the game's own rules (a guard, not a death): the winner may rob it (ransack.cpp)
    uint8_t movementMode = 0;  // gECharMovementMode of the hero: stand, sneak, walk, run, sprint, jump, swim
    uint8_t aniState = 0;      // gEAniState of the hero (stand 2, lying knocked out 16, ...): which motions fit
    char name[24] = {};
    char animation[96] = {};  // the hero's current movement animation (an existing game motion)
    char heldRight[40] = {};  // item names in the hero's hands (drawn weapon, shield, bow); empty = nothing
    char heldLeft[40] = {};
    char shape[32] = {};     // template of the body the player controls instead of its hero (transformed: Wolf), empty = its hero
    char armor[40] = {};     // item names the hero wears in its body and helmet slots (stacks only, not its default body);
                             // empty = none (23.10.: not shown before)
    char helmet[40] = {};
    char look[32] = {};      // template the player chose to be seen as (INI look); empty = the receiver's puppet_template
    int32_t gold = 0;        // the hero's gold while flag bit 4 is set (what a robber finds on its puppet), else 0
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
void Reconnect(void (*log)(char const*));  // QA, client (stage W1): leave the host and connect again at once
// Service the socket and, at most every 50 ms, send the local state. Call once per frame.
void Tick(PlayerState const& local, bool haveLocal);
uint8_t LocalId();
// Host: always. Client: welcomed by the host and in the host's world (SetInWorld); gameplay pauses otherwise.
bool Connected();
Remote const* Remotes();  // indexed by player id, size MaxPlayers; a client outside the host's world is not connected here
constexpr int MaxPlayers = 8;
uint32_t HashWorld(char const* name);
uint32_t HashName(char const* name);
bool IsHost();
// Increments whenever a client enters the host's world (after a join, or after loading the host's save again); the host
// resends its motion-name table and container contents then.
uint32_t JoinCount();

// The world gate (persist.cpp, PLAN-2026-10-22 0.1/0.2): gameplay messages flow only between the host and clients that
// are in the host's world. Joining, the save transfer and MsgWorld itself always pass.
void SetInWorld(uint8_t id, bool in);  // host: that client entered (left) the host's world; client: id = LocalId(), this machine
bool InWorld(uint8_t id);
bool Joined();                   // client: welcomed by the host, in its world or not
uint32_t ConnectingFor();        // client: ms since it began to connect, while it never reached the host; else 0
char const* RefusalReason();     // client: why the host refused this machine (empty if it did not), as the host wrote it
void ClaimVersionForTest(uint16_t version);  // QA stage W7 only: the next hello claims this protocol version (0: ours)
uint32_t WelcomeSerial();        // client: counts the welcomes this machine got, local only (persist binds an autoload to one)
void AdvanceWelcomeSerialForTest();  // QA stage W6 only: simulate a new local welcome without a wire/server change
bool PeerJoined(uint8_t id);     // host: a client with this id is connected, in the host's world or not
// Host: which connection holds slot id, counted up at every hello it accepts there. A player who leaves and joins again
// within one frame keeps its slot, so PeerJoined never turns false in between (Codex review 22.10. R7).
uint32_t PeerGeneration(uint8_t id);
char const* PeerName(uint8_t id);
void GateCounts(unsigned& in, unsigned& out);  // gameplay messages dropped at the gate so far
// World identity, reliable. Host -> one client: WorldHost, the world the host plays (epoch changes with every load, new
// game and return to the menu; campaign: the save a player loads to be in it). Client -> host: WorldReady (this player is
// in that world now) or WorldOther (it is not: another save, a new game of its own, or no game), or WorldWaiting (it is in
// the host's save, but its own hero record was refused; it waits until the record loads, owner 23.10.; hosts before it
// ignore the op).
enum : uint8_t { WorldHost = 1, WorldReady = 2, WorldOther = 3, WorldWaiting = 4 };
enum : uint8_t { KindMenu = 0, KindNew = 1, KindSaved = 2 };
enum : uint8_t { WorldSyncOff = 1 };  // flags: QA negative control, the host passes no world change on (the old behaviour)
#pragma pack(push, 1)
struct WorldNote {
    uint8_t op = 0;
    uint8_t kind = 0;      // host's world: KindMenu, KindNew (not saved yet), KindSaved; client: its own
    uint8_t flags = 0;
    uint32_t epoch = 0;    // the host's world; a client answers with the epoch it means
    char campaign[64] = {};
};
#pragma pack(pop)
void SendWorld(WorldNote const& note, uint8_t to);  // host: to client `to`; client: to the host (to ignored)
using WorldHandler = void (*)(uint8_t from, WorldNote const& note);
void SetWorldHandler(WorldHandler handler);

// NPC snapshots (unreliable) and motion names (reliable): host -> every client; client -> host (its own region,
// passed on by the host).
constexpr int MaxNpcsPerMessage = 48;  // receiving limit
constexpr int NpcsPerSend = 18;         // sent per message: 11 + 18 x 63 bytes fit one 1200-byte datagram (hamachi-net-c.md)
void SendNpcs(NpcRecord const* records, int count);
void SendMotionName(uint32_t hash, char const* name);
// Client side: received NPC snapshots and motion names are handed to these callbacks.
// owner: the player whose machine simulates these NPCs (1 = host; a client sends its own region's NPCs).
// sent: the sender's net::Now() when the snapshot went out.
using NpcHandler = void (*)(NpcRecord const* records, int count, uint32_t now, uint32_t sent, uint8_t owner);
using MotionNameHandler = void (*)(uint32_t hash, char const* name);
void SetNpcHandlers(NpcHandler npcs, MotionNameHandler names);

// Damage, reliable. Client -> host: this client hit an NPC (by GUID) for delta (negative) hit points.
// Host -> one client: an NPC hit that player's puppet for delta.
// Any machine -> the NPC's owner `to` (through the host): this player hit it. weapon: template ID, zeros = none.
// npc: the GUID of one of the sender's NPCs that hit it (zeros or null: the sender's hero). outcome: what the game decided
// for this hit of the sender's hero on the sender's machine (its hit judgement runs before AddHitPoints), see HitOutcome.
enum HitOutcome : uint8_t { HitUnjudged = 0, HitKnockOut = 1, HitDeath = 2 };
void SendHitNpc(uint8_t to, uint8_t const guid[16], int32_t delta, uint8_t const weapon[16], uint8_t const npc[16] = nullptr,
                uint8_t outcome = HitUnjudged);
// attacker: the hitting NPC (GUID), or zeros with attackerPlayer = the player whose hero hit.
void SendHitPlayer(uint8_t playerId, int32_t delta, uint8_t const attacker[16], uint8_t attackerPlayer);
using HitNpcHandler = void (*)(uint8_t from, uint8_t const guid[16], int32_t delta, uint8_t const weapon[16], uint8_t const npc[16],
                               uint8_t outcome);
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
// or ended; kind 7: host to player `guard`: this NPC from the host's save is your companion; kind 8/9: downed.cpp;
// kind 10: a door or lock (doors.cpp: guard = open, alignment = locked, npcClass bits 1 door, 2 lock); kind 16: client to host,
// player `guard` asks for NPC guid that has an important info for its hero (address.cpp); kind 18: for player `guard`, NPC
// guid now remembers its fight with that player as `npc` (LastFightAgainstPlayer, fightmem.cpp).
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
// The count goes as one byte. 48 was too few for a big trader: the contents were silently not sent (Codex review R4,
// 22.10.). One message, up to 250 * 48 bytes; ENet splits a reliable packet that size itself and it arrives whole.
constexpr int MaxContentEntries = 250;
bool SendContents(uint8_t const guid[16], ContentEntry const* entries, int count);  // false: not sent
bool SendContainerTx(uint8_t const guid[16], uint32_t seq, ContentEntry const* entries, int count);  // false: not sent
void SendContainerTxResult(uint8_t to, uint8_t const guid[16], uint32_t seq, bool ok);
void SendWatch(uint8_t const guid[16]);
// Player trade (trade.cpp): what this player can trade, sent whenever it changes; the host passes it on. On receipt
// player is the sender (set by the host). Items of one template with different qualities (sharp, blessed, forged ...) are
// entries of their own; the size stays that of a ContentEntry.
struct GoodsEntry {
    uint8_t id[16];    // item template ID
    uint16_t quality;  // gEItemQuality flags (below 1024, as a hero record's)
    char name[26];     // for logs
    int32_t amount;
};
constexpr int MaxGoodsEntries = MaxContentEntries;
bool SendGoods(uint32_t serial, int32_t gold, GoodsEntry const* entries, int count);  // false: not sent
using GoodsHandler = void (*)(uint8_t player, uint32_t serial, int32_t gold, GoodsEntry const* entries, int count);
void SetGoodsHandler(GoodsHandler goods);
using ContentsHandler = void (*)(uint8_t const guid[16], ContentEntry const* entries, int count);
using ContainerTxHandler = void (*)(uint8_t from, uint8_t const guid[16], uint32_t seq, ContentEntry const* entries, int count);
using TxResultHandler = void (*)(uint8_t const guid[16], uint32_t seq, bool ok);
using WatchHandler = void (*)(uint8_t from, uint8_t const guid[16]);
void SetContainerHandlers(ContentsHandler contents, ContainerTxHandler tx, TxResultHandler result, WatchHandler watch);
// Summoned things (summon.cpp, P1), reliable. A player's own summon got a net ID (caster id in the top byte); the
// others replay it on that player's puppet. op 1: spawned; op 2: gone. Client -> host; host -> every other client
// (relayed, `from` = the caster).
enum : uint8_t { SummonSpawned = 1, SummonGone = 2, SummonCreature = 3 };  // 3: a summoned creature (an NPC), see guid
#pragma pack(push, 1)
struct SummonNote {
    uint8_t op = 0;
    uint32_t netId = 0;
    uint8_t target[16] = {};  // GUID of the spell's target (zeros: none)
    float position[3] = {};
    float rotation[4] = {};
    char spellItem[40] = {};  // the item cast from (It_Spell_Meteor, a scroll ...)
    char spawn[40] = {};      // the summoned entity's template (Smn_Meteor ...)
    uint8_t guid[16] = {};    // SummonCreature: the creature's GUID, which its copy takes over (SetID)
};
#pragma pack(pop)
void SendSummon(SummonNote const& note);
using SummonHandler = void (*)(uint8_t from, SummonNote const& note);
void SetSummonHandler(SummonHandler handler);
// Who did a deed and with what (A0/B0, agreed by agents A and B on 20.10.): the context a message about an arrow, a
// spell, a summon or a controlled creature carries, so that witnesses, crimes and reputation (enclaves.cpp) can blame
// the right player. Not on the wire yet: whoever sends it first adds it to that message and raises the protocol.
#pragma pack(push, 1)
struct DeedContext {
    char player[24] = {};   // the responsible player's name (PlayerState::name, config `name`): the same in every session
    uint8_t playerId = 0;   // that player's net ID: this session only
    uint32_t deedId = 0;    // NextDeedId() on the player's machine: a (player, deedId) counts once, repeats are dropped
    uint8_t body[16] = {};  // GUID of the body that acted: zeros for the player's own hero, else a creature it controls
    uint8_t source[16] = {};  // GUID of what did it (an arrow, a summon ...); zeros: the body itself
    uint8_t target[16] = {};  // GUID of what it was done to; zeros: nothing or an area
};
#pragma pack(pop)
uint32_t NextDeedId();  // counts up from 1 per session on this machine
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
// A spell a player cast on NPCs another machine simulates (spells.cpp): it runs there. Any machine -> player `to`
// (through the host). target: the NPC's GUID (zeros: the NPCs round the caster); args: the script's int argument.
void SendNpcSpell(uint8_t to, char const* script, uint8_t const target[16], int32_t args);
using NpcSpellHandler = void (*)(uint8_t from, char const* script, uint8_t const target[16], int32_t args);
void SetNpcSpellHandler(NpcSpellHandler handler);
// Robbery (ransack.cpp; owner 24.10.: a guard robs a knocked-out partner as in single-player), reliable, through the host.
// The machine whose NPC robs a player's puppet -> that player: RansackHold (the robber is at work: stay down, as the game
// resets a robbed body's knock-out timer), RansackTake (it took `amount` gold from the puppet: pay it). The robbed player
// -> that machine: RansackPaid (`amount` paid for `seq`; less if the hero no longer had that much).
enum : uint8_t { RansackHold = 1, RansackTake = 2, RansackPaid = 3 };
#pragma pack(push, 1)
struct RansackNote {
    uint8_t op = 0;
    uint32_t seq = 0;         // the robbing machine's number for one robbery (take and paid)
    int32_t amount = 0;       // gold taken (take) or paid (paid)
    uint8_t robber[16] = {};  // GUID of the robbing NPC
};
#pragma pack(pop)
void SendRansack(uint8_t to, RansackNote const& note);
using RansackHandler = void (*)(uint8_t from, RansackNote const& note);
void SetRansackHandler(RansackHandler handler);
// Witnesses (witness.cpp, owner 24.10.), reliable, through the host. WitnessSaw -> the machine that runs the NPC (`to`):
// it saw the sender's player commit a crime (value: the attack reason its game chose); it reacts there, to the sender's
// puppet. WitnessHolds -> everyone (`to` 0; reputation = together): the NPC now holds crime `value` (gEPlayerCrime)
// against the sender's player, and so against everybody's. WitnessJudge -> the machine of the player it judged (P7): an
// NPC judged that player's puppet a criminal here (value: the attack reason, Murder for the witness of a murder this
// machine applied for the other player); there it judges that player's hero, so its game remembers the crime (and an NPC
// run there reacts).
enum : uint8_t { WitnessSaw = 1, WitnessHolds = 2, WitnessJudge = 3 };
#pragma pack(push, 1)
struct WitnessNote {
    uint8_t op = 0;
    uint8_t npc[16] = {};  // GUID of the witness
    int32_t value = 0;
};
#pragma pack(pop)
void SendWitness(uint8_t to, WitnessNote const& note);  // to 0: everybody else
using WitnessHandler = void (*)(uint8_t from, WitnessNote const& note);
void SetWitnessHandler(WitnessHandler handler);
// Town reputation (enclaves.cpp), reliable. Host -> clients (`to`, 0 = all): EnclaveState, the host's counters of one
// enclave, and the host's reputation mode. Client -> host: EnclaveDelta (what this player's own deeds added to the
// counters; suspect/known: the new values) or EnclaveRequest (send me every enclave). Personal mode, the machine that
// simulates a victim -> the killer's machine (client -> host, host -> that client): EnclaveCredit, a murder by that player
// for its own counters (crime: added). EnclavePolitical (protocol 56): a political crime of the sender's player (a town it
// freed; known = gEPoliticalAlignment, fame = how many), for the receiving hero's own memory (enclaves.cpp).
enum : uint8_t { EnclaveState = 1, EnclaveDelta = 2, EnclaveRequest = 3, EnclaveCredit = 4, EnclavePolitical = 5 };
#pragma pack(push, 1)
struct EnclaveNote {
    uint8_t op = 0;
    uint8_t mode = 0;          // host's reputation mode (config.h Reputation)
    char name[32] = {};        // the enclave entity's name (Ardea, Silden ...)
    int32_t crime[5] = {};     // CrimeCount per gEPlayerCrime (state: values; delta: added)
    int32_t suspect[5] = {};   // SuspectComment per crime (values)
    int32_t known = 0;         // KnownPlayerCrime
    int32_t fame = 0;          // PlayerFame (state: value; delta: added)
};
#pragma pack(pop)
void SendEnclave(EnclaveNote const& note, uint8_t to);
using EnclaveHandler = void (*)(uint8_t from, EnclaveNote const& note);
void SetEnclaveHandler(EnclaveHandler handler);
// A line of a player's dialog (dialogaudio.cpp): the other players play the same voice file at the speaker. Any machine
// -> everyone else (through the host), reliable and in order, so a line's end never arrives before its start.
// DialogOptions (phase 5, owner 25.10.: the one who talks chooses, the other sees the choice): one note per option of the
// sender's dialog menu, reason = its number from 1, lineId = how many, text = the option's text id; DialogOptionsGone: the
// menu is gone (an option was chosen, or the dialog ended).
enum : uint8_t { DialogLineStart = 1, DialogLineEnd = 2, DialogOptions = 3, DialogOptionsGone = 4 };
enum : uint8_t { DialogSpeakerNpc = 1, DialogSpeakerPlayer = 2 };
#pragma pack(push, 1)
struct DialogLineNote {
    uint8_t kind = 0;           // DialogLineStart / DialogLineEnd
    uint8_t reason = 0;         // end: 1 finished or skipped, 2 replaced by the next line, 3 dialog over
    uint8_t speakerKind = 0;    // DialogSpeakerNpc: speaker is the NPC's GUID; DialogSpeakerPlayer: the sender's hero
    uint32_t lineId = 0;        // per sender, counts up
    uint8_t speaker[16] = {};
    char sample[128] = {};      // the voice file the game resolved (INFO_..._GER.wav), start only
    char text[64] = {};         // start: the line's text id; its text is INFO_<text> in the receiver's language (empty: none)
};
#pragma pack(pop)
void SendDialogLine(DialogLineNote const& note);
using DialogLineHandler = void (*)(uint8_t from, DialogLineNote const& note);
void SetDialogLineHandler(DialogLineHandler handler);
// A trade between two players (offer.cpp, owner 23.10., point 2): the buyer, who traded at the seller's puppet, sends what
// it took and gave; the seller accepts or declines it within 30 s and answers. Through the host, reliable; the host stamps
// who sent it.
constexpr int MaxOfferEntries = 24;
enum : uint8_t { OfferAccepted = 1, OfferDeclined = 2, OfferExpired = 3, OfferUnavailable = 4, OfferBusy = 5 };
#pragma pack(push, 1)
struct OfferEntry {
    uint8_t item[16] = {};      // template ID
    int32_t amount = 0;         // + goes to the buyer, - from the buyer
    uint16_t quality = 0;       // gEItemQuality flags of what goes (protocol 46; the name gave 2 bytes, the message stays 1164)
    char name[26] = {};         // the buyer's name of it (its focus name, UTF-8), for the seller's screen
};
struct OfferNote {
    uint8_t buyer = 0, seller = 0;
    uint8_t count = 0;
    uint32_t serial = 0;        // the buyer's
    int32_t gold = 0;           // + goes to the buyer, - from the buyer
    OfferEntry entries[MaxOfferEntries];
};
struct OfferResultNote {
    uint8_t buyer = 0, seller = 0;
    uint8_t result = 0;         // OfferAccepted .. OfferBusy
    uint32_t serial = 0;
};
#pragma pack(pop)
void SendOffer(OfferNote const& note);              // to note.seller (through the host)
void SendOfferResult(OfferResultNote const& note);  // to note.buyer (through the host)
using OfferHandler = void (*)(OfferNote const& note);
using OfferResultHandler = void (*)(OfferResultNote const& note);
void SetOfferHandlers(OfferHandler offer, OfferResultHandler result);
// The clock that stamps received states (ENet's). Compare stamps only against this clock.
uint32_t Now();
}
