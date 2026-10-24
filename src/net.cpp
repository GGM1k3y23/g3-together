// SPDX-License-Identifier: GPL-3.0-or-later
#include <enet/enet.h>

#include "net.h"

#include "log.h"
#include "parity.h"

#include <cmath>
#include <cstdio>
#include <cstring>

namespace g3t::net {
namespace {
constexpr uint16_t ProtocolVersion = 48;  // 48: HitNpcMsg.outcome (knock-outs by another machine's player), EnclaveCredit; 47: robbery (MsgRansack 34, gold and flag bit 4 in PlayerState), witnesses (MsgWitness 35), subtitles (DialogLineNote.text); 46: item quality in goods and offers; 45: send times in PlayerState and NpcsHeader (glide), armor/helmet/look, MsgGoods (31) and A's offers (32, 33); 36: MsgSummon; 37: MsgShot; 38: spell projectiles, MsgPlayerSpell; 39: MsgNpcSpell; 40: creatures; 41: town reputation (MsgEnclave); 42: world gate (MsgWorld); 43: MsgDialogLine; 44: up to 250 container entries
enum : uint8_t { MsgHello = 1, MsgWelcome = 2, MsgState = 3, MsgLeave = 4, MsgNpcs = 5, MsgMotionName = 6, MsgHitNpc = 7, MsgHitPlayer = 8, MsgStates = 9, MsgTalk = 10, MsgItem = 11, MsgContents = 12, MsgContainerTx = 13, MsgTxResult = 14, MsgWatch = 15, MsgKilled = 16, MsgRefused = 17, MsgDeath = 18, MsgCampaign = 19, MsgFileChunk = 20, MsgFileDone = 21, MsgClock = 22, MsgWorldState = 23, MsgSummon = 24, MsgShot = 25, MsgPlayerSpell = 26, MsgNpcSpell = 27, MsgEnclave = 28, MsgWorld = 29, MsgDialogLine = 30, MsgGoods = 31 };
// UDP payload per datagram (ENet default 1392 = up to 1420-byte IP packets): VPNs such as Hamachi carry about 1400, so
// stay well below (C, re/findings/hamachi-net-c.md, 21.10.). ENet splits larger messages into fragments of this size.
constexpr enet_uint32 Mtu = 1200;
enum : uint8_t { ChannelState = 0, ChannelReliable = 1, ChannelNpcs = 2, ChannelCount = 3 };

#pragma pack(push, 1)
struct Hello { uint8_t type; uint16_t version; char name[24]; uint64_t parity[parity::ItemCount]; };
struct RefusedMsg { uint8_t type; char reason[160]; };
struct CampaignMsg { uint8_t type; uint8_t kind; char name[64]; };
struct ClockMsg { uint8_t type; ClockState state; };
struct WorldStateMsg { uint8_t type; WorldState state; };
struct FileChunkHeader { uint8_t type; uint8_t file; uint32_t total; uint32_t offset; uint16_t length; };
struct FileDoneMsg { uint8_t type; char name[64]; uint32_t size[FileCount]; uint32_t crc[FileCount]; };
struct Welcome { uint8_t type; uint8_t id; };
struct State { uint8_t type; PlayerState state; };
struct Leave { uint8_t type; uint8_t id; };
struct NpcsHeader { uint8_t type; uint32_t sequence; uint32_t sent; uint8_t count; uint8_t owner; };
struct MotionNameMsg { uint8_t type; uint32_t hash; char name[96]; };
// to: the player who applies it (the NPC's owner, the hit player); from: who sent it (set by the host).
struct HitNpcMsg { uint8_t type; uint8_t guid[16]; int32_t delta; uint8_t weapon[16]; uint8_t to; uint8_t from; uint8_t npc[16]; uint8_t outcome; };
struct HitPlayerMsg { uint8_t type; int32_t delta; uint8_t attacker[16]; uint8_t attackerPlayer; uint8_t to; };
struct StatesHeader { uint8_t type; uint8_t kind; uint8_t count; };
struct TalkMsg { uint8_t type; uint8_t begin; uint8_t guid[16]; };
struct ItemMsg { uint8_t type; ItemNote note; };
struct ContentsHeader { uint8_t type; uint8_t guid[16]; uint8_t count; };
// What a player can trade (trade.cpp): its goods and its gold. player: set by the host to the sender.
struct GoodsHeader { uint8_t type; uint8_t player; uint32_t serial; int32_t gold; uint8_t count; };
struct TxHeader { uint8_t type; uint8_t guid[16]; uint32_t seq; uint8_t count; };
struct TxResultMsg { uint8_t type; uint8_t guid[16]; uint32_t seq; uint8_t ok; };
struct WatchMsg { uint8_t type; uint8_t guid[16]; };
struct KilledMsg { uint8_t type; uint8_t guid[16]; uint8_t to; };
struct DeathMsg { uint8_t type; uint8_t guid[16]; uint8_t by; };
struct SummonMsg { uint8_t type; uint8_t from; SummonNote note; };
struct ShotMsg { uint8_t type; uint8_t from; ShotNote note; };
struct DialogLineMsg { uint8_t type; uint8_t from; DialogLineNote note; };
enum : uint8_t { MsgOffer = 32, MsgOfferResult = 33 };  // offer.cpp (A); 31 is B's MsgGoods
struct OfferMsg { uint8_t type; OfferNote note; };      // 1164 bytes: under the 1200-byte datagram
struct OfferResultMsg { uint8_t type; OfferResultNote note; };
struct PlayerSpellMsg { uint8_t type; uint8_t from; uint8_t to; char script[32]; };
enum : uint8_t { MsgRansack = 34, MsgWitness = 35 };  // ransack.cpp, witness.cpp
struct RansackMsg { uint8_t type; uint8_t from; uint8_t to; RansackNote note; };
struct WitnessMsg { uint8_t type; uint8_t from; uint8_t to; WitnessNote note; };
struct NpcSpellMsg { uint8_t type; uint8_t from; uint8_t to; char script[32]; uint8_t target[16]; int32_t args; };
struct EnclaveMsg { uint8_t type; EnclaveNote note; };
struct WorldMsg { uint8_t type; WorldNote note; };
#pragma pack(pop)

ENetHost* host = nullptr;
ENetPeer* server = nullptr;  // client side: the connection to the host
ENetAddress target{};        // client side: where to (re)connect
uint32_t lastConnectAttempt = 0;
uint32_t startedAt = 0;      // client: when it began to connect (enet_time_get)
bool everConnected = false;  // client: an ENet connection to the host came up once (owner 24.10.: "host not reachable")
bool isHost = false;
uint8_t localId = 0;
bool connected = false;
uint32_t welcomes = 0;  // client side: welcomes so far (WelcomeSerial), never sent
bool refused = false;  // client side: the host refused us; do not reconnect
char refusalReason[160] = {};  // client side: the host's reason, for the screen (persist)
uint16_t helloVersion = 0;     // QA (stage W7): the next hello claims this protocol version, 0: ours
char localName[24] = {};
uint32_t sequence = 0;
uint32_t lastSend = 0;
Remote remotes[MaxPlayers] = {};
uint32_t joins = 0;
uint32_t npcSequence = 0;
NpcHandler npcHandler = nullptr;
MotionNameHandler motionNameHandler = nullptr;
HitNpcHandler hitNpcHandler = nullptr;
HitPlayerHandler hitPlayerHandler = nullptr;
StateHandler stateHandler = nullptr;
TalkHandler talkHandler = nullptr;
ItemHandler itemHandler = nullptr;
ContentsHandler contentsHandler = nullptr;
GoodsHandler goodsHandler = nullptr;
ContainerTxHandler txHandler = nullptr;
TxResultHandler txResultHandler = nullptr;
WatchHandler watchHandler = nullptr;
KilledHandler killedHandler = nullptr;
DeathHandler deathHandler = nullptr;
CampaignHandler campaignHandler = nullptr;
ClockHandler clockHandler = nullptr;
WorldStateHandler worldStateHandler = nullptr;
FileChunkHandler fileChunkHandler = nullptr;
FileDoneHandler fileDoneHandler = nullptr;
ENetPeer* peers[MaxPlayers] = {};  // host side: peer for each client id
SummonHandler summonHandler = nullptr;
ShotHandler shotHandler = nullptr;
DialogLineHandler dialogLineHandler = nullptr;
OfferHandler offerHandler = nullptr;
OfferResultHandler offerResultHandler = nullptr;
PlayerSpellHandler playerSpellHandler = nullptr;
RansackHandler ransackHandler = nullptr;
WitnessHandler witnessHandler = nullptr;
NpcSpellHandler npcSpellHandler = nullptr;
EnclaveHandler enclaveHandler = nullptr;
WorldHandler worldHandler = nullptr;
bool inWorld = false;                  // client side: this machine is in the host's world (persist.cpp)
bool clientInWorld[MaxPlayers] = {};   // host side: that client is in the host's world
uint32_t generations[MaxPlayers] = {}; // host side: counts the connections a slot was given (PeerGeneration)
unsigned gatedIn = 0, gatedOut = 0;

// What passes the world gate: joining, the save transfer and the world messages. Motion names wait for the world too
// (Fable's review 22.10. 1.8: they were open to a peer that never said Hello); the host resends them all on entry.
bool Gated(uint8_t type)
{
    switch (type) {
    case MsgHello: case MsgWelcome: case MsgRefused: case MsgLeave: case MsgFileChunk: case MsgFileDone: case MsgWorld:
        return false;
    default:
        return true;
    }
}

bool Open(ENetPeer* peer)
{
    if (!isHost) return inWorld;
    const uintptr_t id = reinterpret_cast<uintptr_t>(peer->data);
    return id > 1 && id < MaxPlayers && clientInWorld[id];
}

void Send(ENetPeer* peer, void const* data, size_t size, uint8_t channel, bool reliable)
{
    if (size && Gated(static_cast<uint8_t const*>(data)[0]) && !Open(peer)) { ++gatedOut; return; }
    ENetPacket* packet = enet_packet_create(data, size, reliable ? ENET_PACKET_FLAG_RELIABLE : 0);
    if (!packet) return;  // out of memory
    if (enet_peer_send(peer, channel, packet) < 0) enet_packet_destroy(packet);
}

// A player's state from the network (Fable's review 22.10. 1.2, 1.6): its strings may lack their terminator and its floats
// may be anything; the puppet code feeds them to %s, strstr, Template(...) and MoveTo.
bool Sane(PlayerState& state)
{
    state.name[sizeof(state.name) - 1] = 0;
    state.animation[sizeof(state.animation) - 1] = 0;
    state.heldRight[sizeof(state.heldRight) - 1] = 0;
    state.heldLeft[sizeof(state.heldLeft) - 1] = 0;
    state.shape[sizeof(state.shape) - 1] = 0;
    state.armor[sizeof(state.armor) - 1] = 0;
    state.helmet[sizeof(state.helmet) - 1] = 0;
    state.look[sizeof(state.look) - 1] = 0;
    if (state.gold < 0) state.gold = 0;
    for (float v : state.position) if (!std::isfinite(v) || std::fabs(v) > 1e7f) return false;
    for (float v : state.rotation) if (!std::isfinite(v) || std::fabs(v) > 2.0f) return false;
    return true;
}

void Store(PlayerState const& state)
{
    if (state.id == 0 || state.id >= MaxPlayers || state.id == localId) return;
    Remote& r = remotes[state.id];
    if (r.latestTicks && state.sequence <= r.latest.sequence) return;  // late or duplicate
    r.previous = r.latestTicks ? r.latest : state;
    r.previousTicks = r.latestTicks ? r.latestTicks : enet_time_get();
    r.latest = state;
    r.latestTicks = enet_time_get();
    r.connected = true;
}

void Drop(uint8_t id)
{
    if (id == 0 || id >= MaxPlayers) return;
    remotes[id] = Remote{};
    peers[id] = nullptr;
    clientInWorld[id] = false;
}

void OnReceive(ENetPeer* peer, ENetPacket* packet)
{
    if (packet->dataLength < 1) return;
    const uint8_t type = packet->data[0];
    if (Gated(type) && !Open(peer)) { ++gatedIn; return; }  // from another world (a client still loading, or the host before a load)
    if (type == MsgHello && isHost && packet->dataLength >= 3) {
        if (peer->data) { Log("net: a second hello from a player who joined already; ignored"); return; }  // it took a new slot each time
        Hello hello{};
        std::memcpy(&hello, packet->data, packet->dataLength < sizeof(hello) ? packet->dataLength : sizeof(hello));
        hello.name[sizeof(hello.name) - 1] = '\0';
        RefusedMsg refusal{MsgRefused, {}};
        if (hello.version != ProtocolVersion || packet->dataLength != sizeof(Hello)) {
            sprintf_s(refusal.reason, "different G3 Together version (protocol %u, host %u)", hello.version, ProtocolVersion);
        } else {
            uint64_t const* ours = parity::Local();
            for (int i = 0; i != parity::ItemCount; ++i)
                if (hello.parity[i] != ours[i]) {
                    strcat_s(refusal.reason, refusal.reason[0] ? ", " : "install differs from the host: ");
                    strcat_s(refusal.reason, parity::ItemName(i));
                }
        }
        if (refusal.reason[0]) {
            Logf("net: refused %s: %s", hello.name, refusal.reason);
            Send(peer, &refusal, sizeof(refusal), ChannelReliable, true);
            enet_peer_disconnect_later(peer, 1);
            return;
        }
        uint8_t id = 0;
        for (uint8_t i = 2; i < MaxPlayers && !id; ++i) if (!peers[i]) id = i;
        if (!id) { enet_peer_disconnect(peer, 2); return; }
        peers[id] = peer;
        ++generations[id];
        peer->data = reinterpret_cast<void*>(static_cast<uintptr_t>(id));
        remotes[id].connected = false;  // until it is in the host's world (SetInWorld)
        clientInWorld[id] = false;
        strncpy_s(remotes[id].latest.name, hello.name, _TRUNCATE);
        const Welcome welcome{MsgWelcome, id};
        Send(peer, &welcome, sizeof(welcome), ChannelReliable, true);
        Logf("net: %s joined as player %u", hello.name, id);
    } else if (type == MsgRefused && !isHost && packet->dataLength == sizeof(RefusedMsg)) {
        RefusedMsg refusal;
        std::memcpy(&refusal, packet->data, sizeof(refusal));
        refusal.reason[sizeof(refusal.reason) - 1] = '\0';
        refused = true;
        strncpy_s(refusalReason, refusal.reason, _TRUNCATE);
        Logf("net: REFUSED by host: %s", refusal.reason);
    } else if (type == MsgWelcome && !isHost && packet->dataLength == sizeof(Welcome)) {
        Welcome welcome;
        std::memcpy(&welcome, packet->data, sizeof(welcome));
        if (welcome.id < 2 || welcome.id >= MaxPlayers) { Logf("net: welcome with player id %u; ignored", welcome.id); return; }
        localId = welcome.id;
        connected = true;
        inWorld = false;  // until the host's world is known to be this one (persist.cpp)
        ++welcomes;
        Logf("net: welcomed as player %u (welcome %u)", localId, welcomes);
    } else if (type == MsgState && packet->dataLength == sizeof(State)) {
        State message;
        std::memcpy(&message, packet->data, sizeof(message));
        if (!Sane(message.state)) return;
        if (isHost) {
            // The host decides who sent it; a client cannot speak for another player.
            message.state.id = static_cast<uint8_t>(reinterpret_cast<uintptr_t>(peer->data));
            Store(message.state);
            for (uint8_t i = 2; i < MaxPlayers; ++i)
                if (peers[i] && i != message.state.id) Send(peers[i], &message, sizeof(message), ChannelState, false);
        } else {
            Store(message.state);
        }
    } else if (type == MsgNpcs && packet->dataLength >= sizeof(NpcsHeader)) {
        NpcsHeader header;
        std::memcpy(&header, packet->data, sizeof(header));
        if (packet->dataLength != sizeof(header) + header.count * sizeof(NpcRecord)) return;
        static NpcRecord records[MaxNpcsPerMessage];
        if (header.count > MaxNpcsPerMessage) return;
        std::memcpy(records, packet->data + sizeof(header), header.count * sizeof(NpcRecord));
        if (isHost) {
            // A client's own NPCs (its region): the host decides who sent them and passes them on.
            header.owner = static_cast<uint8_t>(reinterpret_cast<uintptr_t>(peer->data));
            if (!header.owner) return;
            std::memcpy(packet->data, &header, sizeof(header));
            for (uint8_t i = 2; i < MaxPlayers; ++i)
                if (peers[i] && i != header.owner) Send(peers[i], packet->data, packet->dataLength, ChannelNpcs, false);
        }
        if (npcHandler) npcHandler(records, header.count, enet_time_get(), header.sent, header.owner ? header.owner : 1);
    } else if (type == MsgMotionName && packet->dataLength == sizeof(MotionNameMsg)) {
        MotionNameMsg message;
        std::memcpy(&message, packet->data, sizeof(message));
        message.name[sizeof(message.name) - 1] = '\0';
        if (isHost) {
            const uint8_t from = static_cast<uint8_t>(reinterpret_cast<uintptr_t>(peer->data));
            for (uint8_t i = 2; i < MaxPlayers; ++i)
                if (peers[i] && i != from) Send(peers[i], &message, sizeof(message), ChannelReliable, true);
        }
        if (motionNameHandler) motionNameHandler(message.hash, message.name);
    } else if (type == MsgHitNpc && packet->dataLength == sizeof(HitNpcMsg)) {
        HitNpcMsg message;
        std::memcpy(&message, packet->data, sizeof(message));
        if (isHost) {
            message.from = static_cast<uint8_t>(reinterpret_cast<uintptr_t>(peer->data));
            if (message.to > 1 && message.to < MaxPlayers && peers[message.to]) {  // another client owns the NPC
                Send(peers[message.to], &message, sizeof(message), ChannelReliable, true);
                return;
            }
        }
        if (hitNpcHandler && message.from)
            hitNpcHandler(message.from, message.guid, message.delta, message.weapon, message.npc, message.outcome);
    } else if (type == MsgHitPlayer && packet->dataLength == sizeof(HitPlayerMsg)) {
        HitPlayerMsg message;
        std::memcpy(&message, packet->data, sizeof(message));
        if (message.delta < -1000000 || message.delta > 0) return;  // damage only, no overflow
        if (isHost && message.attackerPlayer) message.attackerPlayer = static_cast<uint8_t>(reinterpret_cast<uintptr_t>(peer->data));
        if (isHost && message.to > 1 && message.to < MaxPlayers && peers[message.to]) {
            Send(peers[message.to], &message, sizeof(message), ChannelReliable, true);
            return;
        }
        if (hitPlayerHandler) hitPlayerHandler(message.delta, message.attacker, message.attackerPlayer);
    } else if (type == MsgStates && packet->dataLength >= sizeof(StatesHeader)) {
        StatesHeader header;
        std::memcpy(&header, packet->data, sizeof(header));
        const uint8_t from = isHost ? static_cast<uint8_t>(reinterpret_cast<uintptr_t>(peer->data)) : 1;
        if (header.count <= MaxStatesPerMessage && packet->dataLength == sizeof(header) + header.count * sizeof(StateEntry) &&
            stateHandler && from) {
            StateEntry entries[MaxStatesPerMessage];
            if (header.count) std::memcpy(entries, packet->data + sizeof(header), header.count * sizeof(StateEntry));
            for (int i = 0; i < header.count; ++i) entries[i].name[sizeof(entries[i].name) - 1] = 0;
            stateHandler(from, header.kind, entries, header.count);
        }
    } else if (type == MsgTalk && isHost && packet->dataLength == sizeof(TalkMsg)) {
        TalkMsg message;
        std::memcpy(&message, packet->data, sizeof(message));
        const uint8_t from = static_cast<uint8_t>(reinterpret_cast<uintptr_t>(peer->data));
        if (talkHandler && from) talkHandler(from, message.begin != 0, message.guid);
    } else if (type == MsgSummon && packet->dataLength == sizeof(SummonMsg)) {
        SummonMsg message;
        std::memcpy(&message, packet->data, sizeof(message));
        message.note.spellItem[sizeof(message.note.spellItem) - 1] = 0;
        message.note.spawn[sizeof(message.note.spawn) - 1] = 0;
        if (isHost) {  // from a client: the caster is that client; pass it on to the others
            message.from = static_cast<uint8_t>(reinterpret_cast<uintptr_t>(peer->data));
            for (uint8_t i = 2; i < MaxPlayers; ++i)
                if (peers[i] && i != message.from) Send(peers[i], &message, sizeof(message), ChannelReliable, true);
        }
        if (summonHandler && message.from) summonHandler(message.from, message.note);
    } else if (type == MsgEnclave && packet->dataLength == sizeof(EnclaveMsg)) {
        EnclaveMsg message;
        std::memcpy(&message, packet->data, sizeof(message));
        message.note.name[sizeof(message.note.name) - 1] = 0;
        const uint8_t from = isHost ? static_cast<uint8_t>(reinterpret_cast<uintptr_t>(peer->data)) : 1;
        if (enclaveHandler && from) enclaveHandler(from, message.note);
    } else if (type == MsgPlayerSpell && packet->dataLength == sizeof(PlayerSpellMsg)) {
        PlayerSpellMsg message;
        std::memcpy(&message, packet->data, sizeof(message));
        message.script[sizeof(message.script) - 1] = 0;
        if (isHost) {
            message.from = static_cast<uint8_t>(reinterpret_cast<uintptr_t>(peer->data));
            if (message.to > 1 && message.to < MaxPlayers && peers[message.to]) {
                Send(peers[message.to], &message, sizeof(message), ChannelReliable, true);
                return;
            }
        }
        if (playerSpellHandler && message.from && message.to == localId) playerSpellHandler(message.from, message.script);
    } else if (type == MsgRansack && packet->dataLength == sizeof(RansackMsg)) {
        RansackMsg message;
        std::memcpy(&message, packet->data, sizeof(message));
        if (isHost) {
            message.from = static_cast<uint8_t>(reinterpret_cast<uintptr_t>(peer->data));
            if (message.to > 1 && message.to < MaxPlayers && peers[message.to]) {
                Send(peers[message.to], &message, sizeof(message), ChannelReliable, true);
                return;
            }
        }
        if (ransackHandler && message.from && message.to == localId) ransackHandler(message.from, message.note);
    } else if (type == MsgWitness && packet->dataLength == sizeof(WitnessMsg)) {
        WitnessMsg message;
        std::memcpy(&message, packet->data, sizeof(message));
        if (isHost) {
            message.from = static_cast<uint8_t>(reinterpret_cast<uintptr_t>(peer->data));
            if (message.to > 1 && message.to < MaxPlayers) {
                if (peers[message.to]) Send(peers[message.to], &message, sizeof(message), ChannelReliable, true);
                return;
            }
            if (message.to == 0)  // to everybody: the other clients too, and the host itself below
                for (uint8_t i = 2; i < MaxPlayers; ++i)
                    if (peers[i] && i != message.from) Send(peers[i], &message, sizeof(message), ChannelReliable, true);
        }
        if (witnessHandler && message.from && (message.to == 0 || message.to == localId)) witnessHandler(message.from, message.note);
    } else if (type == MsgNpcSpell && packet->dataLength == sizeof(NpcSpellMsg)) {
        NpcSpellMsg message;
        std::memcpy(&message, packet->data, sizeof(message));
        message.script[sizeof(message.script) - 1] = 0;
        if (isHost) {
            message.from = static_cast<uint8_t>(reinterpret_cast<uintptr_t>(peer->data));
            if (message.to > 1 && message.to < MaxPlayers && peers[message.to]) {
                Send(peers[message.to], &message, sizeof(message), ChannelReliable, true);
                return;
            }
        }
        if (npcSpellHandler && message.from && message.to == localId) npcSpellHandler(message.from, message.script, message.target, message.args);
    } else if (type == MsgShot && packet->dataLength == sizeof(ShotMsg)) {
        ShotMsg message;
        std::memcpy(&message, packet->data, sizeof(message));
        message.note.arrow[sizeof(message.note.arrow) - 1] = 0;
        message.note.spellItem[sizeof(message.note.spellItem) - 1] = 0;
        if (isHost) {  // from a client: the shooter is that client; pass it on to the others
            message.from = static_cast<uint8_t>(reinterpret_cast<uintptr_t>(peer->data));
            for (uint8_t i = 2; i < MaxPlayers; ++i)
                if (peers[i] && i != message.from) Send(peers[i], &message, sizeof(message), ChannelReliable, true);
        }
        if (shotHandler && message.from) shotHandler(message.from, message.note);
    } else if (type == MsgDialogLine && packet->dataLength == sizeof(DialogLineMsg)) {
        DialogLineMsg message;
        std::memcpy(&message, packet->data, sizeof(message));
        message.note.sample[sizeof(message.note.sample) - 1] = 0;
        if (isHost) {  // from a client: pass it on to the others
            message.from = static_cast<uint8_t>(reinterpret_cast<uintptr_t>(peer->data));
            for (uint8_t i = 2; i < MaxPlayers; ++i)
                if (peers[i] && i != message.from) Send(peers[i], &message, sizeof(message), ChannelReliable, true);
        }
        if (dialogLineHandler && message.from) dialogLineHandler(message.from, message.note);
    } else if (type == MsgOffer && packet->dataLength == sizeof(OfferMsg)) {
        OfferMsg message;
        std::memcpy(&message, packet->data, sizeof(message));
        if (message.note.count > MaxOfferEntries) return;
        for (auto& entry : message.note.entries) entry.name[sizeof(entry.name) - 1] = 0;
        if (isHost) {
            message.note.buyer = static_cast<uint8_t>(reinterpret_cast<uintptr_t>(peer->data));  // who sent it
            if (message.note.seller != 1) {  // another client's goods: on to it
                const uint8_t to = message.note.seller;
                if (to > 1 && to < MaxPlayers && peers[to] && to != message.note.buyer) Send(peers[to], &message, sizeof(message), ChannelReliable, true);
                return;
            }
        } else if (message.note.seller != localId) {
            return;
        }
        if (offerHandler && message.note.buyer) offerHandler(message.note);
    } else if (type == MsgOfferResult && packet->dataLength == sizeof(OfferResultMsg)) {
        OfferResultMsg message;
        std::memcpy(&message, packet->data, sizeof(message));
        if (isHost) {
            message.note.seller = static_cast<uint8_t>(reinterpret_cast<uintptr_t>(peer->data));
            if (message.note.buyer != 1) {
                const uint8_t to = message.note.buyer;
                if (to > 1 && to < MaxPlayers && peers[to] && to != message.note.seller) Send(peers[to], &message, sizeof(message), ChannelReliable, true);
                return;
            }
        } else if (message.note.buyer != localId) {
            return;
        }
        if (offerResultHandler && message.note.seller) offerResultHandler(message.note);
    } else if (type == MsgItem && packet->dataLength == sizeof(ItemMsg)) {
        ItemMsg message;
        std::memcpy(&message, packet->data, sizeof(message));
        message.note.name[sizeof(message.note.name) - 1] = 0;
        const uint8_t from = isHost ? static_cast<uint8_t>(reinterpret_cast<uintptr_t>(peer->data)) : 1;
        if (itemHandler && from) itemHandler(from, message.note);
    } else if (type == MsgGoods && packet->dataLength >= sizeof(GoodsHeader)) {
        GoodsHeader header;
        std::memcpy(&header, packet->data, sizeof(header));
        if (header.count > MaxGoodsEntries || packet->dataLength != sizeof(header) + header.count * sizeof(GoodsEntry) || header.gold < 0)
            return;
        if (isHost) {
            // The host decides who sent it and passes it on to the others.
            header.player = static_cast<uint8_t>(reinterpret_cast<uintptr_t>(peer->data));
            if (!header.player) return;
            std::memcpy(packet->data, &header, sizeof(header));
            for (uint8_t i = 2; i < MaxPlayers; ++i)
                if (peers[i] && i != header.player) Send(peers[i], packet->data, packet->dataLength, ChannelReliable, true);
        }
        if (header.player == 0 || header.player >= MaxPlayers || header.player == localId || !goodsHandler) return;
        GoodsEntry entries[MaxGoodsEntries];
        if (header.count) std::memcpy(entries, packet->data + sizeof(header), header.count * sizeof(GoodsEntry));
        for (int i = 0; i < header.count; ++i) {
            entries[i].name[sizeof(entries[i].name) - 1] = 0;
            if (entries[i].amount <= 0 || entries[i].amount > 1000000 || entries[i].quality >= 1024) return;
        }
        goodsHandler(header.player, header.serial, header.gold, entries, header.count);
    } else if (type == MsgContents && !isHost && packet->dataLength >= sizeof(ContentsHeader)) {
        ContentsHeader header;
        std::memcpy(&header, packet->data, sizeof(header));
        if (header.count <= MaxContentEntries && packet->dataLength == sizeof(header) + header.count * sizeof(ContentEntry) &&
            contentsHandler) {
            ContentEntry entries[MaxContentEntries];
            if (header.count) std::memcpy(entries, packet->data + sizeof(header), header.count * sizeof(ContentEntry));
            for (int i = 0; i < header.count; ++i) entries[i].name[sizeof(entries[i].name) - 1] = 0;
            contentsHandler(header.guid, entries, header.count);
        }
    } else if (type == MsgContainerTx && isHost && packet->dataLength >= sizeof(TxHeader)) {
        TxHeader header;
        std::memcpy(&header, packet->data, sizeof(header));
        const uint8_t from = static_cast<uint8_t>(reinterpret_cast<uintptr_t>(peer->data));
        if (header.count <= MaxContentEntries && packet->dataLength == sizeof(header) + header.count * sizeof(ContentEntry) &&
            txHandler && from) {
            ContentEntry entries[MaxContentEntries];
            if (header.count) std::memcpy(entries, packet->data + sizeof(header), header.count * sizeof(ContentEntry));
            for (int i = 0; i < header.count; ++i) entries[i].name[sizeof(entries[i].name) - 1] = 0;
            txHandler(from, header.guid, header.seq, entries, header.count);
        }
    } else if (type == MsgTxResult && !isHost && packet->dataLength == sizeof(TxResultMsg)) {
        TxResultMsg message;
        std::memcpy(&message, packet->data, sizeof(message));
        if (txResultHandler) txResultHandler(message.guid, message.seq, message.ok != 0);
    } else if (type == MsgWatch && isHost && packet->dataLength == sizeof(WatchMsg)) {
        WatchMsg message;
        std::memcpy(&message, packet->data, sizeof(message));
        const uint8_t from = static_cast<uint8_t>(reinterpret_cast<uintptr_t>(peer->data));
        if (watchHandler && from) watchHandler(from, message.guid);
    } else if (type == MsgFileChunk && !isHost && packet->dataLength >= sizeof(FileChunkHeader)) {
        FileChunkHeader header;
        std::memcpy(&header, packet->data, sizeof(header));
        if (packet->dataLength == sizeof(header) + header.length && fileChunkHandler)
            fileChunkHandler(header.file, header.total, header.offset, packet->data + sizeof(header), header.length);
    } else if (type == MsgFileDone && !isHost && packet->dataLength == sizeof(FileDoneMsg)) {
        FileDoneMsg message;
        std::memcpy(&message, packet->data, sizeof(message));
        message.name[sizeof(message.name) - 1] = 0;
        if (fileDoneHandler) fileDoneHandler(message.name, message.size, message.crc);
    } else if (type == MsgWorldState && packet->dataLength == sizeof(WorldStateMsg)) {
        WorldStateMsg message;
        std::memcpy(&message, packet->data, sizeof(message));
        message.state.routine[sizeof(message.state.routine) - 1] = 0;
        const uint8_t from = isHost ? static_cast<uint8_t>(reinterpret_cast<uintptr_t>(peer->data)) : 1;
        if (worldStateHandler && from) worldStateHandler(from, message.state);
    } else if (type == MsgClock && packet->dataLength == sizeof(ClockMsg)) {
        ClockMsg message;
        std::memcpy(&message, packet->data, sizeof(message));
        if (clockHandler) clockHandler(message.state);
    } else if (type == MsgCampaign && !isHost && packet->dataLength == sizeof(CampaignMsg)) {
        CampaignMsg message;
        std::memcpy(&message, packet->data, sizeof(message));
        message.name[sizeof(message.name) - 1] = 0;
        if (campaignHandler) campaignHandler(message.kind, message.name);
    } else if (type == MsgDeath && packet->dataLength == sizeof(DeathMsg)) {
        DeathMsg message;
        std::memcpy(&message, packet->data, sizeof(message));
        // The host decides who reported it; a client only hears it from the host (relayed or the host's own).
        const uint8_t by = isHost ? static_cast<uint8_t>(reinterpret_cast<uintptr_t>(peer->data)) : message.by;
        if (deathHandler && by) deathHandler(by, message.guid);
    } else if (type == MsgKilled && packet->dataLength == sizeof(KilledMsg)) {
        KilledMsg message;
        std::memcpy(&message, packet->data, sizeof(message));
        if (isHost && message.to > 1 && message.to < MaxPlayers && peers[message.to]) {
            Send(peers[message.to], &message, sizeof(message), ChannelReliable, true);
            return;
        }
        if (killedHandler) killedHandler(message.guid);
    } else if (type == MsgLeave && !isHost && packet->dataLength == sizeof(Leave)) {
        Leave leave;
        std::memcpy(&leave, packet->data, sizeof(leave));
        Logf("net: player %u left", leave.id);
        Drop(leave.id);
    } else if (type == MsgWorld && packet->dataLength == sizeof(WorldMsg)) {
        WorldMsg message;
        std::memcpy(&message, packet->data, sizeof(message));
        message.note.campaign[sizeof(message.note.campaign) - 1] = 0;
        const uint8_t from = isHost ? static_cast<uint8_t>(reinterpret_cast<uintptr_t>(peer->data)) : 1;
        if (worldHandler && from) worldHandler(from, message.note);
    }
}
}

uint32_t HashWorld(char const* name)
{
    uint32_t hash = 2166136261u;
    for (; *name; ++name) hash = (hash ^ static_cast<uint8_t>(*name)) * 16777619u;
    return hash;
}

uint32_t Now() { return enet_time_get(); }
uint8_t LocalId() { return localId; }
bool IsHost() { return isHost; }
uint32_t JoinCount() { return joins; }

void SetInWorld(uint8_t id, bool in)
{
    if (!isHost) {
        if (inWorld == in) return;
        inWorld = in;
        if (!in) for (uint8_t i = 0; i < MaxPlayers; ++i) remotes[i] = Remote{};  // the others' puppets are of that world
        Logf("net: this player is %s the host's world", in ? "in" : "out of");
        return;
    }
    if (id < 2 || id >= MaxPlayers || !peers[id] || clientInWorld[id] == in) return;
    clientInWorld[id] = in;
    if (in) {
        ++joins;  // like a join for everything that sends a newcomer the whole state
    } else {
        Remote cleared{};
        strncpy_s(cleared.latest.name, remotes[id].latest.name, _TRUNCATE);
        remotes[id] = cleared;  // its puppet goes; it comes back with the first state from the host's world
    }
    Logf("net: player %u (%s) is %s the host's world", id, remotes[id].latest.name, in ? "in" : "out of");
}

bool InWorld(uint8_t id) { return isHost ? (id == 1 || (id < MaxPlayers && clientInWorld[id])) : inWorld; }
bool Joined() { return connected; }
char const* RefusalReason() { return refusalReason; }
uint32_t ConnectingFor() { return isHost || everConnected || !host ? 0 : enet_time_get() - startedAt; }
void ClaimVersionForTest(uint16_t version) { helloVersion = version; }
uint32_t WelcomeSerial() { return welcomes; }
void AdvanceWelcomeSerialForTest() { ++welcomes; }
bool PeerJoined(uint8_t id) { return isHost && id > 1 && id < MaxPlayers && peers[id]; }
uint32_t PeerGeneration(uint8_t id) { return id < MaxPlayers ? generations[id] : 0; }
char const* PeerName(uint8_t id) { return id < MaxPlayers ? remotes[id].latest.name : ""; }
void GateCounts(unsigned& in, unsigned& out) { in = gatedIn; out = gatedOut; }
void SetWorldHandler(WorldHandler handler) { worldHandler = handler; }

void SendWorld(WorldNote const& note, uint8_t to)
{
    if (!host) return;
    const WorldMsg message{MsgWorld, note};
    if (isHost) {
        if (to > 1 && to < MaxPlayers && peers[to]) Send(peers[to], &message, sizeof(message), ChannelReliable, true);
    } else if (server && connected) {
        Send(server, &message, sizeof(message), ChannelReliable, true);
    }
}

uint32_t HashName(char const* name) { return HashWorld(name); }

uint32_t NextDeedId()
{
    static uint32_t last = 0;
    return ++last;
}

void SetNpcHandlers(NpcHandler npcs, MotionNameHandler names)
{
    npcHandler = npcs;
    motionNameHandler = names;
}

void SendNpcs(NpcRecord const* records, int count)
{
    if (!host || count <= 0 || count > MaxNpcsPerMessage) return;
    static uint8_t buffer[sizeof(NpcsHeader) + MaxNpcsPerMessage * sizeof(NpcRecord)];
    const NpcsHeader header{MsgNpcs, ++npcSequence, enet_time_get(), static_cast<uint8_t>(count), isHost ? uint8_t(1) : localId};
    std::memcpy(buffer, &header, sizeof(header));
    std::memcpy(buffer + sizeof(header), records, count * sizeof(NpcRecord));
    if (!isHost) {
        if (server && connected) Send(server, buffer, sizeof(header) + count * sizeof(NpcRecord), ChannelNpcs, false);
        return;
    }
    for (uint8_t i = 2; i < MaxPlayers; ++i)
        if (peers[i]) Send(peers[i], buffer, sizeof(header) + count * sizeof(NpcRecord), ChannelNpcs, false);
}

void SetHitHandlers(HitNpcHandler npc, HitPlayerHandler player)
{
    hitNpcHandler = npc;
    hitPlayerHandler = player;
}

void SendHitNpc(uint8_t to, uint8_t const guid[16], int32_t delta, uint8_t const weapon[16], uint8_t const npc[16], uint8_t outcome)
{
    if (!host || to == localId) return;
    HitNpcMsg message{MsgHitNpc, {}, delta, {}, to, localId, {}, outcome};
    std::memcpy(message.guid, guid, 16);
    std::memcpy(message.weapon, weapon, 16);
    if (npc) std::memcpy(message.npc, npc, 16);
    if (!isHost) { if (server && connected) Send(server, &message, sizeof(message), ChannelReliable, true); return; }
    if (to < MaxPlayers && peers[to]) Send(peers[to], &message, sizeof(message), ChannelReliable, true);
}

void SendHitPlayer(uint8_t playerId, int32_t delta, uint8_t const attacker[16], uint8_t attackerPlayer)
{
    if (!host || playerId == localId || playerId >= MaxPlayers) return;
    HitPlayerMsg message{MsgHitPlayer, delta, {}, attackerPlayer, playerId};
    std::memcpy(message.attacker, attacker, 16);
    if (!isHost) { if (server && connected) Send(server, &message, sizeof(message), ChannelReliable, true); return; }
    if (peers[playerId]) Send(peers[playerId], &message, sizeof(message), ChannelReliable, true);
}

void SetStateHandler(StateHandler handler) { stateHandler = handler; }

void SetTalkHandler(TalkHandler handler) { talkHandler = handler; }

void SetItemHandler(ItemHandler handler) { itemHandler = handler; }

void SetKilledHandler(KilledHandler handler) { killedHandler = handler; }

void SetDeathHandler(DeathHandler handler) { deathHandler = handler; }

void SetCampaignHandler(CampaignHandler handler) { campaignHandler = handler; }

void SetClockHandler(ClockHandler handler) { clockHandler = handler; }

void SetWorldStateHandler(WorldStateHandler handler) { worldStateHandler = handler; }

void SendWorldState(WorldState const& state, uint8_t except)
{
    if (!host) return;
    const WorldStateMsg message{MsgWorldState, state};
    if (!isHost) {
        if (server && connected) Send(server, &message, sizeof(message), ChannelReliable, true);
        return;
    }
    for (uint8_t i = 2; i < MaxPlayers; ++i)
        if (peers[i] && i != except) Send(peers[i], &message, sizeof(message), ChannelReliable, true);
}

void SendClock(ClockState const& state)
{
    if (!host) return;
    const ClockMsg message{MsgClock, state};
    if (!isHost) {
        if (server && connected) Send(server, &message, sizeof(message), ChannelReliable, true);
        return;
    }
    for (uint8_t i = 2; i < MaxPlayers; ++i)
        if (peers[i]) Send(peers[i], &message, sizeof(message), ChannelReliable, true);
}

void SetFileHandlers(FileChunkHandler chunk, FileDoneHandler done)
{
    fileChunkHandler = chunk;
    fileDoneHandler = done;
}

bool SendFileChunk(uint8_t to, uint8_t file, uint32_t total, uint32_t offset, void const* data, uint16_t length)
{
    if (!host || !isHost || to >= MaxPlayers || !peers[to]) return false;
    static uint8_t buffer[sizeof(FileChunkHeader) + MaxChunk];
    if (length > MaxChunk) return false;
    const FileChunkHeader header{MsgFileChunk, file, total, offset, length};
    std::memcpy(buffer, &header, sizeof(header));
    std::memcpy(buffer + sizeof(header), data, length);
    Send(peers[to], buffer, sizeof(header) + length, ChannelReliable, true);
    return true;
}

bool SendFileDone(uint8_t to, char const* name, uint32_t const size[FileCount], uint32_t const crc[FileCount])
{
    if (!host || !isHost || to >= MaxPlayers || !peers[to]) return false;
    FileDoneMsg message{MsgFileDone, {}, {}, {}};
    strncpy_s(message.name, name, _TRUNCATE);
    std::memcpy(message.size, size, sizeof(message.size));
    std::memcpy(message.crc, crc, sizeof(message.crc));
    Send(peers[to], &message, sizeof(message), ChannelReliable, true);
    return true;
}

void SendCampaign(uint8_t kind, char const* name)
{
    if (!host || !isHost) return;
    CampaignMsg message{MsgCampaign, kind, {}};
    strncpy_s(message.name, name, _TRUNCATE);
    for (uint8_t i = 2; i < MaxPlayers; ++i)
        if (peers[i]) Send(peers[i], &message, sizeof(message), ChannelReliable, true);
}

void SendDeath(uint8_t const guid[16], uint8_t by, uint8_t except)
{
    if (!host) return;
    DeathMsg message{MsgDeath, {}, by};
    std::memcpy(message.guid, guid, 16);
    if (!isHost) {
        if (server && connected) Send(server, &message, sizeof(message), ChannelReliable, true);
        return;
    }
    for (uint8_t i = 2; i < MaxPlayers; ++i)
        if (peers[i] && i != except) Send(peers[i], &message, sizeof(message), ChannelReliable, true);
}

void SendKilled(uint8_t to, uint8_t const guid[16])
{
    if (!host || to == localId || to >= MaxPlayers) return;
    KilledMsg message{MsgKilled, {}, to};
    std::memcpy(message.guid, guid, 16);
    if (!isHost) { if (server && connected) Send(server, &message, sizeof(message), ChannelReliable, true); return; }
    if (peers[to]) Send(peers[to], &message, sizeof(message), ChannelReliable, true);
}

bool SendGoods(uint32_t serial, int32_t gold, GoodsEntry const* entries, int count)
{
    if (!host || count < 0 || count > MaxGoodsEntries || gold < 0) return false;
    uint8_t buffer[sizeof(GoodsHeader) + MaxGoodsEntries * sizeof(GoodsEntry)];
    const GoodsHeader header{MsgGoods, localId, serial, gold, static_cast<uint8_t>(count)};
    std::memcpy(buffer, &header, sizeof(header));
    if (count) std::memcpy(buffer + sizeof(header), entries, count * sizeof(GoodsEntry));
    const size_t size = sizeof(header) + count * sizeof(GoodsEntry);
    if (!isHost) {
        if (!server || !connected) return false;
        Send(server, buffer, size, ChannelReliable, true);
        return true;
    }
    for (uint8_t i = 2; i < MaxPlayers; ++i)
        if (peers[i]) Send(peers[i], buffer, size, ChannelReliable, true);
    return true;
}

void SetGoodsHandler(GoodsHandler goods) { goodsHandler = goods; }

void SetContainerHandlers(ContentsHandler contents, ContainerTxHandler tx, TxResultHandler result, WatchHandler watch)
{
    contentsHandler = contents;
    txHandler = tx;
    txResultHandler = result;
    watchHandler = watch;
}

bool SendContents(uint8_t const guid[16], ContentEntry const* entries, int count)
{
    if (!host || !isHost || count < 0 || count > MaxContentEntries) return false;
    uint8_t buffer[sizeof(ContentsHeader) + MaxContentEntries * sizeof(ContentEntry)];
    ContentsHeader header{MsgContents, {}, static_cast<uint8_t>(count)};
    std::memcpy(header.guid, guid, 16);
    std::memcpy(buffer, &header, sizeof(header));
    if (count) std::memcpy(buffer + sizeof(header), entries, count * sizeof(ContentEntry));
    for (uint8_t i = 2; i < MaxPlayers; ++i)
        if (peers[i]) Send(peers[i], buffer, sizeof(header) + count * sizeof(ContentEntry), ChannelReliable, true);
    return true;
}

bool SendContainerTx(uint8_t const guid[16], uint32_t seq, ContentEntry const* entries, int count)
{
    if (!host || isHost || !server || !connected || count < 0 || count > MaxContentEntries) return false;
    uint8_t buffer[sizeof(TxHeader) + MaxContentEntries * sizeof(ContentEntry)];
    TxHeader header{MsgContainerTx, {}, seq, static_cast<uint8_t>(count)};
    std::memcpy(header.guid, guid, 16);
    std::memcpy(buffer, &header, sizeof(header));
    if (count) std::memcpy(buffer + sizeof(header), entries, count * sizeof(ContentEntry));
    Send(server, buffer, sizeof(header) + count * sizeof(ContentEntry), ChannelReliable, true);
    return true;
}

void SendContainerTxResult(uint8_t to, uint8_t const guid[16], uint32_t seq, bool ok)
{
    if (!host || !isHost || to >= MaxPlayers || !peers[to]) return;
    TxResultMsg message{MsgTxResult, {}, seq, static_cast<uint8_t>(ok ? 1 : 0)};
    std::memcpy(message.guid, guid, 16);
    Send(peers[to], &message, sizeof(message), ChannelReliable, true);
}

void SendWatch(uint8_t const guid[16])
{
    if (!host || isHost || !server || !connected) return;
    WatchMsg message{MsgWatch, {}};
    std::memcpy(message.guid, guid, 16);
    Send(server, &message, sizeof(message), ChannelReliable, true);
}

void SendItem(ItemNote const& note, uint8_t to)
{
    if (!host) return;
    ItemMsg message{MsgItem, note};
    if (!isHost) {
        if (server && connected) Send(server, &message, sizeof(message), ChannelReliable, true);
        return;
    }
    for (uint8_t i = 2; i < MaxPlayers; ++i)
        if (peers[i] && (to == 0 || to == i)) Send(peers[i], &message, sizeof(message), ChannelReliable, true);
}

void SetSummonHandler(SummonHandler handler) { summonHandler = handler; }
void SetShotHandler(ShotHandler handler) { shotHandler = handler; }
void SetDialogLineHandler(DialogLineHandler handler) { dialogLineHandler = handler; }
void SetOfferHandlers(OfferHandler offer, OfferResultHandler result)
{
    offerHandler = offer;
    offerResultHandler = result;
}
void SetPlayerSpellHandler(PlayerSpellHandler handler) { playerSpellHandler = handler; }
void SetRansackHandler(RansackHandler handler) { ransackHandler = handler; }
void SetWitnessHandler(WitnessHandler handler) { witnessHandler = handler; }
void SetNpcSpellHandler(NpcSpellHandler handler) { npcSpellHandler = handler; }

void SendNpcSpell(uint8_t to, char const* script, uint8_t const target[16], int32_t args)
{
    if (!host || to == localId || to >= MaxPlayers) return;
    NpcSpellMsg message{MsgNpcSpell, localId, to, {}, {}, args};
    strncpy_s(message.script, script, _TRUNCATE);
    std::memcpy(message.target, target, 16);
    if (!isHost) { if (server && connected) Send(server, &message, sizeof(message), ChannelReliable, true); return; }
    if (peers[to]) Send(peers[to], &message, sizeof(message), ChannelReliable, true);
}

void SetEnclaveHandler(EnclaveHandler handler) { enclaveHandler = handler; }

void SendEnclave(EnclaveNote const& note, uint8_t to)
{
    if (!host) return;
    const EnclaveMsg message{MsgEnclave, note};
    if (!isHost) {
        if (server && connected) Send(server, &message, sizeof(message), ChannelReliable, true);
        return;
    }
    for (uint8_t i = 2; i < MaxPlayers; ++i)
        if (peers[i] && (to == 0 || to == i)) Send(peers[i], &message, sizeof(message), ChannelReliable, true);
}

void SendPlayerSpell(uint8_t to, char const* script)
{
    if (!host || to == localId || to >= MaxPlayers) return;
    PlayerSpellMsg message{MsgPlayerSpell, localId, to, {}};
    strncpy_s(message.script, script, _TRUNCATE);
    if (!isHost) { if (server && connected) Send(server, &message, sizeof(message), ChannelReliable, true); return; }
    if (peers[to]) Send(peers[to], &message, sizeof(message), ChannelReliable, true);
}

void SendRansack(uint8_t to, RansackNote const& note)
{
    if (!host || to == localId || to >= MaxPlayers) return;
    const RansackMsg message{MsgRansack, localId, to, note};
    if (!isHost) { if (server && connected) Send(server, &message, sizeof(message), ChannelReliable, true); return; }
    if (peers[to]) Send(peers[to], &message, sizeof(message), ChannelReliable, true);
}

void SendWitness(uint8_t to, WitnessNote const& note)
{
    if (!host || to == localId || to >= MaxPlayers) return;
    const WitnessMsg message{MsgWitness, localId, to, note};
    if (!isHost) { if (server && connected) Send(server, &message, sizeof(message), ChannelReliable, true); return; }
    for (uint8_t i = 2; i < MaxPlayers; ++i)
        if (peers[i] && (to == 0 || to == i)) Send(peers[i], &message, sizeof(message), ChannelReliable, true);
}

void SendShot(ShotNote const& note)
{
    if (!host) return;
    const ShotMsg message{MsgShot, localId, note};
    if (!isHost) {
        if (server && connected) Send(server, &message, sizeof(message), ChannelReliable, true);
        return;
    }
    for (uint8_t i = 2; i < MaxPlayers; ++i)
        if (peers[i]) Send(peers[i], &message, sizeof(message), ChannelReliable, true);
}

void SendDialogLine(DialogLineNote const& note)
{
    if (!host) return;
    const DialogLineMsg message{MsgDialogLine, localId, note};
    if (!isHost) {
        if (server && connected) Send(server, &message, sizeof(message), ChannelReliable, true);
        return;
    }
    for (uint8_t i = 2; i < MaxPlayers; ++i)
        if (peers[i]) Send(peers[i], &message, sizeof(message), ChannelReliable, true);
}

void SendOffer(OfferNote const& note)
{
    if (!host) return;
    OfferMsg message{MsgOffer, note};
    message.note.buyer = localId;
    if (!isHost) {
        if (server && connected) Send(server, &message, sizeof(message), ChannelReliable, true);
        return;
    }
    if (note.seller > 1 && note.seller < MaxPlayers && peers[note.seller]) Send(peers[note.seller], &message, sizeof(message), ChannelReliable, true);
}

void SendOfferResult(OfferResultNote const& note)
{
    if (!host) return;
    OfferResultMsg message{MsgOfferResult, note};
    message.note.seller = localId;
    if (!isHost) {
        if (server && connected) Send(server, &message, sizeof(message), ChannelReliable, true);
        return;
    }
    if (note.buyer > 1 && note.buyer < MaxPlayers && peers[note.buyer]) Send(peers[note.buyer], &message, sizeof(message), ChannelReliable, true);
}

void SendSummon(SummonNote const& note)
{
    if (!host) return;
    const SummonMsg message{MsgSummon, localId, note};
    if (!isHost) {
        if (server && connected) Send(server, &message, sizeof(message), ChannelReliable, true);
        return;
    }
    for (uint8_t i = 2; i < MaxPlayers; ++i)
        if (peers[i]) Send(peers[i], &message, sizeof(message), ChannelReliable, true);
}

void SendTalk(bool begin, uint8_t const guid[16])
{
    if (!host || isHost || !server || !connected) return;
    TalkMsg message{MsgTalk, static_cast<uint8_t>(begin ? 1 : 0), {}};
    std::memcpy(message.guid, guid, 16);
    Send(server, &message, sizeof(message), ChannelReliable, true);
}

void SendStates(uint8_t kind, StateEntry const* entries, int count)
{
    if (!host || count < 0 || count > MaxStatesPerMessage || (count == 0 && isHost)) return;
    uint8_t buffer[sizeof(StatesHeader) + MaxStatesPerMessage * sizeof(StateEntry)];
    const StatesHeader header{MsgStates, kind, static_cast<uint8_t>(count)};
    std::memcpy(buffer, &header, sizeof(header));
    if (count) std::memcpy(buffer + sizeof(header), entries, count * sizeof(StateEntry));
    const size_t size = sizeof(header) + count * sizeof(StateEntry);
    if (isHost) {
        for (uint8_t i = 2; i < MaxPlayers; ++i)
            if (peers[i]) Send(peers[i], buffer, size, ChannelReliable, true);
    } else if (server && connected) {
        Send(server, buffer, size, ChannelReliable, true);
    }
}

void SendMotionName(uint32_t hash, char const* name)
{
    if (!host) return;
    MotionNameMsg message{MsgMotionName, hash, {}};
    strncpy_s(message.name, name, _TRUNCATE);
    if (!isHost) {
        if (server && connected) Send(server, &message, sizeof(message), ChannelReliable, true);
        return;
    }
    for (uint8_t i = 2; i < MaxPlayers; ++i)
        if (peers[i]) Send(peers[i], &message, sizeof(message), ChannelReliable, true);
}
bool Connected() { return isHost ? connected : connected && inWorld; }
Remote const* Remotes() { return remotes; }

void Stop()
{
    // Tell the others right away; otherwise a closed game only shows up after the long peer timeout.
    if (!host) return;
    if (isHost) {
        for (uint8_t i = 2; i < MaxPlayers; ++i)
            if (peers[i]) enet_peer_disconnect_now(peers[i], 0);
    } else if (server) {
        enet_peer_disconnect_now(server, 0);
    }
    enet_host_flush(host);
}

bool Start(bool asHost, char const* address, unsigned short port, char const* name)
{
    if (enet_initialize() != 0) { Log("net: enet_initialize failed"); return false; }
    isHost = asHost;
    strncpy_s(localName, name, _TRUNCATE);
    if (isHost) {
        ENetAddress bind{};
        bind.host = ENET_HOST_ANY;
        bind.port = port;
        host = enet_host_create(&bind, MaxPlayers - 1, ChannelCount, 0, 0);
        if (host) host->mtu = Mtu;
        localId = 1;
        connected = true;
        Logf("net: hosting on UDP %u: %s", port, host ? "ok" : "FAILED");
    } else {
        host = enet_host_create(nullptr, 1, ChannelCount, 0, 0);
        if (host) host->mtu = Mtu;  // before connecting: the connect proposes it, both sides keep the smaller
        enet_address_set_host(&target, address);
        target.port = port;
        server = host ? enet_host_connect(host, &target, ChannelCount, 0) : nullptr;
        lastConnectAttempt = enet_time_get();
        startedAt = enet_time_get();
        Logf("net: connecting to %s:%u: %s", address, port, server ? "pending" : "FAILED");
    }
    return host != nullptr;
}

void Tick(PlayerState const& local, bool haveLocal)
{
    if (!host) return;
    ENetEvent event;
    // At most 512 events a frame: a flood must not stall the game (Fable's review 22.10. 1.9); the rest waits for the next.
    for (int events = 0; events < 512 && enet_host_service(host, &event, 0) > 0; ++events) {
        switch (event.type) {
        case ENET_EVENT_TYPE_CONNECT:
            // World loads and the intro video stop the engine loop (and so this service call) for
            // many seconds; the default ENet timeouts dropped a loading player after about 9 s.
            enet_peer_timeout(event.peer, 0, 60000, 180000);
            // A new connection has not said hello yet. ENet hands out the peer of a player who left before, and its old
            // player id made the hello guard refuse every rejoin (stageU1, 22.10.).
            event.peer->data = nullptr;
            if (!isHost) {
                everConnected = true;
                Hello hello{MsgHello, helloVersion ? helloVersion : ProtocolVersion, {}, {}};
                strncpy_s(hello.name, localName, _TRUNCATE);
                std::memcpy(hello.parity, parity::Local(), sizeof(hello.parity));
                Send(event.peer, &hello, sizeof(hello), ChannelReliable, true);
                Log("net: connected, hello sent");
            }
            break;
        case ENET_EVENT_TYPE_RECEIVE:
            OnReceive(event.peer, event.packet);
            enet_packet_destroy(event.packet);
            break;
        case ENET_EVENT_TYPE_DISCONNECT:
            if (isHost) {
                const uint8_t id = static_cast<uint8_t>(reinterpret_cast<uintptr_t>(event.peer->data));
                Logf("net: player %u disconnected", id);
                Drop(id);
                event.peer->data = nullptr;
                const Leave leave{MsgLeave, id};
                for (uint8_t i = 2; i < MaxPlayers; ++i) if (peers[i]) Send(peers[i], &leave, sizeof(leave), ChannelReliable, true);
            } else {
                Log("net: disconnected from host; will reconnect");
                connected = false;
                inWorld = false;
                server = nullptr;
                for (uint8_t i = 0; i < MaxPlayers; ++i) Drop(i);
            }
            break;
        default:
            break;
        }
    }
    const uint32_t now = enet_time_get();
    if (!isHost && !server && !refused && now - lastConnectAttempt > 3000) {
        lastConnectAttempt = now;
        server = enet_host_connect(host, &target, ChannelCount, 0);
        Log(server ? "net: reconnecting" : "net: reconnect failed");
    }
    if (!connected || !haveLocal || now - lastSend < 50) return;
    lastSend = now;
    State message{MsgState, local};
    message.state.id = localId;
    message.state.sequence = ++sequence;
    message.state.sent = now;
    strncpy_s(message.state.name, localName, _TRUNCATE);
    if (isHost) {
        for (uint8_t i = 2; i < MaxPlayers; ++i) if (peers[i]) Send(peers[i], &message, sizeof(message), ChannelState, false);
    } else if (server) {
        Send(server, &message, sizeof(message), ChannelState, false);
    }
    enet_host_flush(host);
}

// QA, client (stage W1): leave the host and connect again at once, as a player whose connection dropped and came back.
void Reconnect(void (*log)(char const*))
{
    if (isHost || !server || !connected) { log("REFUSED reconnect: a connected client only"); return; }
    enet_peer_disconnect_now(server, 0);
    connected = false;
    inWorld = false;
    for (uint8_t i = 0; i < MaxPlayers; ++i) Drop(i);
    server = enet_host_connect(host, &target, ChannelCount, 0);
    lastConnectAttempt = enet_time_get();
    Log("net: QA reconnect: left the host and connecting again");
    log(server ? "RECONNECT ok=1" : "RECONNECT ok=0");
}
}
