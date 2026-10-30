// SPDX-License-Identifier: GPL-3.0-or-later
// Combined with Valve's steam_api.dll under the additional permission in COPYING.STEAMWORKS (GPLv3 section 7).
// Steam transport, see steam.h. steam_api.dll (32-bit, Steamworks.NET Standalone 2025.164.1, SHA-256
// fdafb7c65ec7d4182b42bf92cc8671c68e4dc547cc6e613899d2618a47eb65da) is loaded at run time and driven through its flat C
// API with manual callback dispatch: no Steamworks headers or import libraries. The flat API table, the lobby and session
// handling and the hello/welcome/ping link are adapted from Jericho Co-op's native/steam_net.c (MIT, Copyright (c) 2026
// Jericho Co-op contributors, thirdparty/licenses/jericho-coop-LICENSE), which a real two-account test confirmed
// (2026-10-21: the sender identity at offset 12 in the 32-bit DLL, LobbyCreated twice, no overlay in a D3D9 game).
//
// On top of NetworkingMessages:
//   channel 1 (control): 'H' hello (client -> lobby owner until welcomed), 'W' welcome, 'P'+time ping and 'Q'+time pong
//            every second (round trip for the status), 'B' bye.
//   channel 0 (data): one ENet datagram per message, sent unreliable (ENet does its own reliability).
// The relay: a client's ENet connects to a 127.0.0.1 socket of this file, which passes each datagram to the host's
// SteamID. On the host each linked friend gets its own 127.0.0.1 socket that talks to the host's ENet port, so ENet sees
// every friend as a separate local peer (coop-anything's HXCoop pattern) and needs no change. The host's ENet still
// listens on its UDP port too: friends over Steam and players over an IP address can join the same game.
#include <winsock2.h>
#include <windows.h>

#include "steam.h"

#include "config.h"
#include "log.h"
#include "net.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace g3t::steam {
namespace {
using u64 = unsigned long long;
using sbool = unsigned char;  // C++ bool, returned in AL
using HSteamPipe = int;

// The flat API: plain __cdecl functions (the mod's own default is __stdcall, /Gz), the interface first.
struct Api
{
    int(__cdecl* InitFlat)(char* error);
    HSteamPipe(__cdecl* GetHSteamPipe)();
    void(__cdecl* DispatchInit)();
    void(__cdecl* DispatchRunFrame)(HSteamPipe);
    sbool(__cdecl* DispatchNext)(HSteamPipe, void* message);
    void(__cdecl* DispatchFree)(HSteamPipe);
    sbool(__cdecl* DispatchResult)(HSteamPipe, u64 call, void* buffer, int size, int callback, sbool* failed);
    void*(__cdecl* User)();
    u64(__cdecl* UserSteamId)(void*);
    void*(__cdecl* Matchmaking)();
    u64(__cdecl* CreateLobby)(void*, int type, int maxMembers);
    u64(__cdecl* JoinLobby)(void*, u64 lobby);
    void(__cdecl* LeaveLobby)(void*, u64 lobby);
    sbool(__cdecl* SetLobbyData)(void*, u64 lobby, char const* key, char const* value);
    char const*(__cdecl* GetLobbyData)(void*, u64 lobby, char const* key);
    u64(__cdecl* GetLobbyOwner)(void*, u64 lobby);
    int(__cdecl* MemberCount)(void*, u64 lobby);
    u64(__cdecl* MemberByIndex)(void*, u64 lobby, int index);
    void*(__cdecl* Friends)();
    char const*(__cdecl* PersonaName)(void*);
    int(__cdecl* FriendCount)(void*, int flags);
    u64(__cdecl* FriendByIndex)(void*, int index, int flags);
    sbool(__cdecl* FriendGamePlayed)(void*, u64 user, void* info);
    char const*(__cdecl* FriendPersonaName)(void*, u64 user);
    sbool(__cdecl* SetRichPresence)(void*, char const* key, char const* value);
    void*(__cdecl* Messages)();
    int(__cdecl* SendToUser)(void*, void const* identity, void const* data, unsigned size, int flags, int channel);
    int(__cdecl* ReceiveOnChannel)(void*, int channel, void** messages, int max);
    sbool(__cdecl* AcceptSession)(void*, void const* identity);
    sbool(__cdecl* CloseSession)(void*, void const* identity);
    void(__cdecl* IdentitySetSteamId)(void* identity, u64 id);
    u64(__cdecl* IdentityGetSteamId)(void const* identity);
    void(__cdecl* MessageRelease)(void* message);
    void*(__cdecl* Utils)();
    void(__cdecl* InitRelayNetworkAccess)(void*);
} api;

struct Export { void** slot; char const* name; };
Export const Exports[] = {
    {reinterpret_cast<void**>(&api.InitFlat), "SteamAPI_InitFlat"},
    {reinterpret_cast<void**>(&api.GetHSteamPipe), "SteamAPI_GetHSteamPipe"},
    {reinterpret_cast<void**>(&api.DispatchInit), "SteamAPI_ManualDispatch_Init"},
    {reinterpret_cast<void**>(&api.DispatchRunFrame), "SteamAPI_ManualDispatch_RunFrame"},
    {reinterpret_cast<void**>(&api.DispatchNext), "SteamAPI_ManualDispatch_GetNextCallback"},
    {reinterpret_cast<void**>(&api.DispatchFree), "SteamAPI_ManualDispatch_FreeLastCallback"},
    {reinterpret_cast<void**>(&api.DispatchResult), "SteamAPI_ManualDispatch_GetAPICallResult"},
    {reinterpret_cast<void**>(&api.User), "SteamAPI_SteamUser_v023"},
    {reinterpret_cast<void**>(&api.UserSteamId), "SteamAPI_ISteamUser_GetSteamID"},
    {reinterpret_cast<void**>(&api.Matchmaking), "SteamAPI_SteamMatchmaking_v009"},
    {reinterpret_cast<void**>(&api.CreateLobby), "SteamAPI_ISteamMatchmaking_CreateLobby"},
    {reinterpret_cast<void**>(&api.JoinLobby), "SteamAPI_ISteamMatchmaking_JoinLobby"},
    {reinterpret_cast<void**>(&api.LeaveLobby), "SteamAPI_ISteamMatchmaking_LeaveLobby"},
    {reinterpret_cast<void**>(&api.SetLobbyData), "SteamAPI_ISteamMatchmaking_SetLobbyData"},
    {reinterpret_cast<void**>(&api.GetLobbyData), "SteamAPI_ISteamMatchmaking_GetLobbyData"},
    {reinterpret_cast<void**>(&api.GetLobbyOwner), "SteamAPI_ISteamMatchmaking_GetLobbyOwner"},
    {reinterpret_cast<void**>(&api.MemberCount), "SteamAPI_ISteamMatchmaking_GetNumLobbyMembers"},
    {reinterpret_cast<void**>(&api.MemberByIndex), "SteamAPI_ISteamMatchmaking_GetLobbyMemberByIndex"},
    {reinterpret_cast<void**>(&api.Friends), "SteamAPI_SteamFriends_v018"},
    {reinterpret_cast<void**>(&api.PersonaName), "SteamAPI_ISteamFriends_GetPersonaName"},
    {reinterpret_cast<void**>(&api.FriendCount), "SteamAPI_ISteamFriends_GetFriendCount"},
    {reinterpret_cast<void**>(&api.FriendByIndex), "SteamAPI_ISteamFriends_GetFriendByIndex"},
    {reinterpret_cast<void**>(&api.FriendGamePlayed), "SteamAPI_ISteamFriends_GetFriendGamePlayed"},
    {reinterpret_cast<void**>(&api.FriendPersonaName), "SteamAPI_ISteamFriends_GetFriendPersonaName"},
    {reinterpret_cast<void**>(&api.SetRichPresence), "SteamAPI_ISteamFriends_SetRichPresence"},
    {reinterpret_cast<void**>(&api.Messages), "SteamAPI_SteamNetworkingMessages_SteamAPI_v002"},
    {reinterpret_cast<void**>(&api.SendToUser), "SteamAPI_ISteamNetworkingMessages_SendMessageToUser"},
    {reinterpret_cast<void**>(&api.ReceiveOnChannel), "SteamAPI_ISteamNetworkingMessages_ReceiveMessagesOnChannel"},
    {reinterpret_cast<void**>(&api.AcceptSession), "SteamAPI_ISteamNetworkingMessages_AcceptSessionWithUser"},
    {reinterpret_cast<void**>(&api.CloseSession), "SteamAPI_ISteamNetworkingMessages_CloseSessionWithUser"},
    {reinterpret_cast<void**>(&api.IdentitySetSteamId), "SteamAPI_SteamNetworkingIdentity_SetSteamID64"},
    {reinterpret_cast<void**>(&api.IdentityGetSteamId), "SteamAPI_SteamNetworkingIdentity_GetSteamID64"},
    {reinterpret_cast<void**>(&api.MessageRelease), "SteamAPI_SteamNetworkingMessage_t_Release"},
    {reinterpret_cast<void**>(&api.Utils), "SteamAPI_SteamNetworkingUtils_SteamAPI_v004"},
    {reinterpret_cast<void**>(&api.InitRelayNetworkAccess), "SteamAPI_ISteamNetworkingUtils_InitRelayNetworkAccess"},
};

// Callback ids and the 32-bit layouts (playbook, "Struct layouts"): read with memcpy at these offsets.
enum : int {
    CbLobbyJoinRequested = 333,  // GameLobbyJoinRequested_t { u64 lobby; u64 friend; }: an invite accepted in the game
    CbRichJoinRequested = 337,   // GameRichPresenceJoinRequested_t { u64 friend; char connect[256]; }: "Join game"
    CbLobbyEnter = 504,          // LobbyEnter_t { u64 lobby; u32 perms; bool locked; u32 response at 16; } 1 = success
    CbLobbyChatUpdate = 506,     // LobbyChatUpdate_t { u64 lobby; u64 changed; u64 by; u32 state at 24; }
    CbLobbyCreated = 513,        // LobbyCreated_t { int result; u64 lobby at 8; }
    CbCallCompleted = 703,       // SteamAPICallCompleted_t { u64 call; int callback; u32 size; }
    CbSessionRequest = 1251,     // SteamNetworkingMessagesSessionRequest_t { identity at 0; }
    CbSessionFailed = 1252,      // SteamNetworkingMessagesSessionFailed_t { connection info, its identity at 0; }
};
enum : int { SendUnreliable = 0, SendNoNagle = 1, SendReliable = 8, SendAutoRestart = 32 };
enum : int { ResultOk = 1 };
enum : int { ChannelData = 0, ChannelControl = 1 };
constexpr int LobbyFriendsOnly = 1;
constexpr int FriendFlagImmediate = 4;
constexpr uint32_t SpacewarAppId = 480;
char const LobbyTag[] = "g3together";  // lobby data that marks a G3 Together game (480 lobbies are everyone's)
// No silence timeout below ENet's own: a world load stops the game loop (and so Pump) for many seconds, and net.cpp gives
// ENet peers up to 180 s for it. A friend who quits leaves the lobby, which ends the link at once.
constexpr uint32_t SilenceMs = 180000;
constexpr uint32_t ScanMs = 3000;       // client: how often the friends list is searched for a host
constexpr uint32_t JoinWaitMs = 15000;  // client: a lobby that does not answer the join in this time is skipped for a while
constexpr int MaxRemotes = net::MaxPlayers;

struct Remote
{
    u64 id = 0;
    bool linked = false;   // host: said hello while in the lobby; client: the host welcomed us
    uint32_t heard = 0, lastPing = 0;
    int rtt = -1;
    SOCKET socket = INVALID_SOCKET;  // host: this friend's 127.0.0.1 socket to the host's ENet port
    unsigned toEnet = 0, toSteam = 0;
};

struct Skipped { u64 lobby; uint32_t until; };  // until 0: for good (not a G3 Together lobby)

bool ready = false, tried = false;
char error[256] = "";
HSteamPipe pipe = 0;
void *user = nullptr, *matchmaking = nullptr, *friends = nullptr, *messages = nullptr;
u64 me = 0;
char persona[64] = "";

bool isHost = false;
unsigned short enetPort = 0;
u64 lobby = 0, owner = 0;
uint32_t lastCreate = 0;
Remote remotes[MaxRemotes];
int senderOffset = 0;  // where a message's sender identity sits: 12 in the real 32-bit DLL, 16 by the headers; 0: unknown

// Client
SOCKET relay = INVALID_SOCKET;   // 127.0.0.1:<RelayPort>, the address the client's ENet connects to
unsigned short relayPort = 0;
sockaddr_in enetAddress{};       // where the client's ENet sends from (learnt from its first datagram)
bool haveEnetAddress = false;
u64 wantedLobby = 0;             // from steam_lobby in the INI or +connect_lobby on the command line
bool wantedWaitLogged = false;
u64 pendingLobby = 0;            // an invite or "Join game" accepted through Steam
uint32_t joinedAt = 0, lastScan = 0, lastHello = 0;
char hostName[64] = "";
Skipped skipped[16] = {};
bool searchLogged = false;

uint32_t Now() { return GetTickCount(); }
char const* Text(char const* text) { return text ? text : ""; }  // Steam's strings, never null for the CRT

void Identity(char (&buffer)[160], u64 id)
{
    std::memset(buffer, 0, sizeof(buffer));  // a SteamNetworkingIdentity is 136 bytes; filled only through SetSteamID64
    api.IdentitySetSteamId(buffer, id);
}

int RawSend(u64 to, int flags, int channel, void const* data, unsigned size)
{
    char identity[160];
    Identity(identity, to);
    return api.SendToUser(messages, identity, data, size, flags | SendNoNagle | SendAutoRestart, channel);
}

void Control(u64 to, char what, bool reliable)
{
    char buffer[9] = {what};
    const double time = Now() / 1000.0;
    std::memcpy(buffer + 1, &time, sizeof(time));
    RawSend(to, reliable ? SendReliable : SendUnreliable, ChannelControl, buffer, what == 'P' ? 9 : 1);
}

SOCKET LoopbackSocket(unsigned short* port)
{
    SOCKET s = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (s == INVALID_SOCKET) return s;
    sockaddr_in local{};
    local.sin_family = AF_INET;
    local.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    int size = sizeof(local);
    u_long nonBlocking = 1;
    // Windows reports an earlier datagram to a closed port as WSAECONNRESET on the next receive; turn that off.
    DWORD off = FALSE, bytes = 0;
    constexpr DWORD UdpConnReset = 0x9800000C;  // SIO_UDP_CONNRESET
    if (bind(s, reinterpret_cast<sockaddr*>(&local), sizeof(local)) != 0 || ioctlsocket(s, FIONBIO, &nonBlocking) != 0 ||
        getsockname(s, reinterpret_cast<sockaddr*>(&local), &size) != 0) {
        closesocket(s);
        return INVALID_SOCKET;
    }
    WSAIoctl(s, UdpConnReset, &off, sizeof(off), nullptr, 0, &bytes, nullptr, nullptr);
    if (port) *port = ntohs(local.sin_port);
    return s;
}

Remote* Find(u64 id)
{
    for (auto& r : remotes) if (r.id == id) return &r;
    return nullptr;
}

Remote* Get(u64 id)
{
    if (Remote* r = Find(id)) return r;
    for (auto& r : remotes)
        if (!r.id) {
            r = Remote{};
            r.id = id;
            r.heard = Now();
            return &r;
        }
    return nullptr;
}

bool InLobby(u64 id)
{
    const int count = api.MemberCount(matchmaking, lobby);
    for (int i = 0; i < count; ++i)
        if (api.MemberByIndex(matchmaking, lobby, i) == id) return true;
    return false;
}

void LeaveLobby();

void Drop(Remote* r, char const* why)
{
    if (!r || !r->id) return;
    Logf("steam: %llu dropped (%s; %u datagrams to ENet, %u to Steam)", r->id, why, r->toEnet, r->toSteam);
    char identity[160];
    Identity(identity, r->id);
    api.CloseSession(messages, identity);
    if (r->socket != INVALID_SOCKET) closesocket(r->socket);
    const bool wasHost = !isHost && r->id == owner;
    *r = Remote{};
    if (wasHost) LeaveLobby();  // the client looks for a host again; its ENet notices the loss by its own timeout
}

void Skip(u64 id, uint32_t forMs)
{
    Skipped* slot = &skipped[0];
    for (auto& s : skipped) {
        if (s.lobby == id) { slot = &s; break; }
        if (!s.lobby || (s.until && static_cast<int32_t>(s.until - slot->until) < 0)) slot = &s;
    }
    slot->lobby = id;
    slot->until = forMs ? (Now() + forMs) | 1 : 0;
}

bool IsSkipped(u64 id)
{
    for (auto& s : skipped)
        if (s.lobby == id) return !s.until || static_cast<int32_t>(s.until - Now()) > 0;
    return false;
}

// ---- lobby ----

void CreateLobby()
{
    lastCreate = Now();
    api.CreateLobby(matchmaking, LobbyFriendsOnly, net::MaxPlayers);
    Log("steam: creating a friends-only lobby");
}

void Join(u64 id, char const* why)
{
    LeaveLobby();
    lobby = id;
    owner = 0;
    joinedAt = Now();
    api.JoinLobby(matchmaking, id);
    Logf("steam: joining lobby %llu (%s)", id, why);
}

void LeaveLobby()
{
    if (!lobby) return;
    for (auto& r : remotes)
        if (r.id && (isHost || r.id == owner)) {
            Control(r.id, 'B', true);
            char identity[160];
            Identity(identity, r.id);
            api.CloseSession(messages, identity);
            if (r.socket != INVALID_SOCKET) closesocket(r.socket);
            r = Remote{};
        }
    api.LeaveLobby(matchmaking, lobby);
    if (isHost) {
        api.SetRichPresence(friends, "connect", "");
        api.SetRichPresence(friends, "status", "");
    }
    Logf("steam: left lobby %llu", lobby);
    lobby = owner = 0;
    hostName[0] = '\0';
    searchLogged = false;
}

void OnLobbyCreated(char const* p)
{
    int result = 0;
    u64 id = 0;
    std::memcpy(&result, p, 4);
    std::memcpy(&id, p + 8, 8);
    if (!isHost || (lobby && lobby == id)) return;  // arrives twice on real Steam: as the call result and as a broadcast
    if (result != ResultOk) {
        Format(error, "Steam lobby not created (result %d)", result);
        Logf("steam: %s; trying again in 15 s", error);
        return;
    }
    error[0] = '\0';
    lobby = id;
    owner = me;
    char connect[64];
    Format(connect, "+connect_lobby %llu", id);
    api.SetLobbyData(matchmaking, id, LobbyTag, "1");
    api.SetLobbyData(matchmaking, id, "name", GetConfig().name);
    api.SetRichPresence(friends, "status", "G3 Together");
    api.SetRichPresence(friends, "connect", connect);
    Logf("steam: lobby %llu open; friends join over Steam (friends list: Join game, or steam_lobby = %llu)", id, id);
}

void OnLobbyEnter(char const* p)
{
    u64 id = 0;
    unsigned response = 0;
    std::memcpy(&id, p, 8);
    std::memcpy(&response, p + 16, 4);
    if (isHost || id != lobby || owner) return;
    char const* tag = api.GetLobbyData(matchmaking, id, LobbyTag);
    if (response != 1 || !tag || std::strcmp(tag, "1")) {
        Logf("steam: lobby %llu refused (response %u, %s)", id, response, tag && *tag ? "G3 Together" : "not a G3 Together lobby");
        Format(error, response != 1 ? "the Steam lobby is full or closed" : "that Steam lobby is not a G3 Together game");
        const bool ours = tag && !std::strcmp(tag, "1");
        api.LeaveLobby(matchmaking, id);
        lobby = 0;
        Skip(id, ours ? 30000 : 0);
        return;
    }
    error[0] = '\0';
    owner = api.GetLobbyOwner(matchmaking, id);
    strncpy_s(hostName, Text(api.FriendPersonaName(friends, owner)), _TRUNCATE);
    if (Remote* r = Get(owner)) r->heard = Now();
    lastHello = 0;
    Logf("steam: in lobby %llu of %s (%llu); saying hello", id, hostName, owner);
}

void ParseConnect(char const* text)
{
    char const* key = text ? std::strstr(text, "+connect_lobby ") : nullptr;
    if (key) pendingLobby = _strtoui64(key + 15, nullptr, 10);
}

void OnCallback(int callback, char const* p, int size)
{
    u64 a = 0, b = 0;
    unsigned state = 0;
    switch (callback) {
    case CbLobbyCreated:
        OnLobbyCreated(p);
        break;
    case CbLobbyEnter:
        OnLobbyEnter(p);
        break;
    case CbLobbyChatUpdate:
        std::memcpy(&a, p, 8);
        std::memcpy(&b, p + 8, 8);
        std::memcpy(&state, p + 24, 4);
        if (a == lobby && (state & 0x1e)) Drop(Find(b), "left the lobby");  // left, disconnected, kicked, banned
        break;
    case CbLobbyJoinRequested:
        std::memcpy(&pendingLobby, p, 8);
        Logf("steam: invite accepted for lobby %llu", pendingLobby);
        break;
    case CbRichJoinRequested:
        if (size >= 8 + 16) ParseConnect(p + 8);
        Logf("steam: Join game -> lobby %llu", pendingLobby);
        break;
    case CbSessionRequest:
        a = api.IdentityGetSteamId(p);
        // Host: members of its lobby only (code review 28.10. P2: anyone's session was accepted and took one of the slots).
        if ((isHost && lobby && InLobby(a)) || (!isHost && owner && a == owner)) {
            api.AcceptSession(messages, p);
            Logf("steam: session with %llu accepted", a);
        } else {
            Logf("steam: session from %llu ignored", a);
        }
        break;
    case CbSessionFailed:
        Drop(Find(api.IdentityGetSteamId(p)), "session failed");
        break;
    default:
        break;
    }
}

// ---- messages ----

void OnControl(Remote& r, char const* data, int size)
{
    switch (data[0]) {
    case 'H':
        if (isHost && lobby && !r.linked && InLobby(r.id)) {
            if (LinkedCount() + 1 >= net::MaxPlayers) {
                Logf("steam: %llu refused, the game is full", r.id);
                break;
            }
            r.socket = LoopbackSocket(nullptr);
            if (r.socket == INVALID_SOCKET) {
                Logf("steam: no local socket for %llu (%d)", r.id, WSAGetLastError());
                break;
            }
            r.linked = true;
            Logf("steam: %s (%llu) linked to this game", Text(api.FriendPersonaName(friends, r.id)), r.id);
        }
        if (r.linked) Control(r.id, 'W', true);
        break;
    case 'W':
        if (!isHost && r.id == owner && !r.linked) {
            r.linked = true;
            Logf("steam: welcomed by the host %s; ENet goes over Steam now", hostName);
        }
        break;
    case 'P':
        if (size >= 9) {
            char pong[9];
            std::memcpy(pong, data, 9);
            pong[0] = 'Q';
            RawSend(r.id, SendUnreliable, ChannelControl, pong, 9);
        }
        break;
    case 'Q':
        if (size >= 9) {
            double sent = 0;
            std::memcpy(&sent, data + 1, 8);
            r.rtt = static_cast<int>((Now() / 1000.0 - sent) * 1000.0 + 0.5);
        }
        break;
    case 'B':
        Drop(&r, "said bye");
        break;
    default:
        break;
    }
}

void OnData(Remote& r, char const* data, int size)
{
    if (!r.linked || size <= 0) return;
    if (isHost) {
        sockaddr_in to{};
        to.sin_family = AF_INET;
        to.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        to.sin_port = htons(enetPort);
        sendto(r.socket, data, size, 0, reinterpret_cast<sockaddr*>(&to), sizeof(to));
    } else if (haveEnetAddress) {
        sendto(relay, data, size, 0, reinterpret_cast<sockaddr*>(&enetAddress), sizeof(enetAddress));
    }
    ++r.toEnet;
}

u64 Sender(char const* message)
{
    if (senderOffset) return api.IdentityGetSteamId(message + senderOffset);
    // GetSteamID64 answers 0 unless the identity's type field says SteamID, so the wrong offset reads 0 (Jericho's order).
    const u64 at16 = api.IdentityGetSteamId(message + 16), at12 = api.IdentityGetSteamId(message + 12);
    if (at16 || at12) {
        senderOffset = at16 ? 16 : 12;
        Logf("steam: a message's sender identity is at offset %d", senderOffset);
    }
    return at16 ? at16 : at12;
}

void Receive(int channel)
{
    void* batch[32];
    for (int rounds = 0; rounds < 16; ++rounds) {
        const int count = api.ReceiveOnChannel(messages, channel, batch, 32);
        if (count <= 0) return;
        for (int i = 0; i < count; ++i) {
            char const* m = static_cast<char const*>(batch[i]);
            char const* data = nullptr;
            int size = 0;
            std::memcpy(&data, m, sizeof(data));  // SteamNetworkingMessage_t (32-bit): data at 0, size at 4
            std::memcpy(&size, m + 4, sizeof(size));
            const u64 from = Sender(m);
            // Host: a slot only for a member of its lobby (checked before Get reserves one), or one it has already.
            Remote* r = !from || (!isHost && from != owner) ? nullptr : isHost && !Find(from) && !(lobby && InLobby(from)) ? nullptr : Get(from);
            if (r && size > 0) {
                r->heard = Now();
                if (channel == ChannelControl) OnControl(*r, data, size);
                else OnData(*r, data, size);
            }
            api.MessageRelease(batch[i]);
        }
    }
}

// ENet -> Steam: what ENet sent to the relay sockets since the last Pump.
void Forward()
{
    char datagram[4096];
    sockaddr_in from{};
    int fromSize = sizeof(from);
    if (isHost) {
        for (auto& r : remotes) {
            if (!r.linked || r.socket == INVALID_SOCKET) continue;
            for (int n = 0; n < 256; ++n) {
                fromSize = sizeof(from);
                const int size = recvfrom(r.socket, datagram, sizeof(datagram), 0, reinterpret_cast<sockaddr*>(&from), &fromSize);
                if (size < 0 && WSAGetLastError() == WSAECONNRESET) continue;
                if (size <= 0) break;
                if (ntohs(from.sin_port) != enetPort) continue;
                RawSend(r.id, SendUnreliable, ChannelData, datagram, static_cast<unsigned>(size));
                ++r.toSteam;
            }
        }
        return;
    }
    if (relay == INVALID_SOCKET) return;
    Remote* host = owner ? Find(owner) : nullptr;
    for (int n = 0; n < 256; ++n) {
        fromSize = sizeof(from);
        const int size = recvfrom(relay, datagram, sizeof(datagram), 0, reinterpret_cast<sockaddr*>(&from), &fromSize);
        if (size < 0 && WSAGetLastError() == WSAECONNRESET) continue;
        if (size <= 0) break;
        enetAddress = from;
        haveEnetAddress = true;
        if (!host || !host->linked) continue;  // no host yet: ENet keeps trying to connect until there is one
        RawSend(owner, SendUnreliable, ChannelData, datagram, static_cast<unsigned>(size));
        ++host->toSteam;
    }
}

// Client without a lobby: an accepted invite, the INI's lobby, else the first friend hosting a G3 Together game.
void Search()
{
    if (lobby) {
        if (!owner && Now() - joinedAt > JoinWaitMs) {
            Logf("steam: lobby %llu did not answer the join; trying others", lobby);
            Skip(lobby, 30000);
            api.LeaveLobby(matchmaking, lobby);
            lobby = 0;
        }
        return;
    }
    if (pendingLobby) {
        const u64 id = pendingLobby;
        pendingLobby = 0;
        Join(id, "accepted through Steam");
        return;
    }
    if (Now() - lastScan < ScanMs) return;
    lastScan = Now();
    // steam_lobby set: that lobby only. Refused or silent, it is tried again after its pause; the friends' games are not
    // searched instead (code review 28.10. P2: that could join another friend's host).
    if (wantedLobby) {
        if (!IsSkipped(wantedLobby)) Join(wantedLobby, "steam_lobby");
        else if (!wantedWaitLogged) {
            wantedWaitLogged = true;
            Logf("steam: steam_lobby %llu refused or did not answer; waiting for it (no other host)", wantedLobby);
        }
        return;
    }
    char const* wantedHost = GetConfig().steamHost;
    const int count = api.FriendCount(friends, FriendFlagImmediate);
    for (int i = 0; i < count; ++i) {
        const u64 id = api.FriendByIndex(friends, i, FriendFlagImmediate);
        // FriendGameInfo_t { CGameID game; u32 ip; u16 port, queryPort; CSteamID lobby at 16; }
        struct { u64 game; uint32_t ip; uint16_t port, queryPort; u64 lobby; } info{};
        if (!api.FriendGamePlayed(friends, id, &info) || (info.game & 0xffffff) != SpacewarAppId || !info.lobby) continue;
        if (IsSkipped(info.lobby)) continue;
        char const* name = Text(api.FriendPersonaName(friends, id));
        if (*wantedHost && _stricmp(name, wantedHost)) continue;
        Join(info.lobby, *name ? name : "a friend");
        return;
    }
    if (!searchLogged) {
        Logf("steam: no friend hosting a G3 Together game yet%s%s; looking every %u s", *wantedHost ? " named " : "", wantedHost,
             ScanMs / 1000);
        searchLogged = true;
    }
}

bool Fail(char const* text)
{
    strncpy_s(error, text, _TRUNCATE);
    Logf("steam: not used: %s", error);
    return false;
}

// <game>\G3Together\steam_api.dll (the mod's own copy, never one the game may have), or G3T_STEAM_DLL (QA: the
// stand-in tools/fakesteam, Steam over 127.0.0.1).
bool Load()
{
    char path[MAX_PATH] = {};
    if (GetEnvironmentVariableA("G3T_STEAM_DLL", path, MAX_PATH) == 0 || !*path) {
        HMODULE self = nullptr;
        if (!GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                                reinterpret_cast<LPCSTR>(&Load), &self) ||
            !GetModuleFileNameA(self, path, MAX_PATH))
            return Fail("no game folder");
        for (int part = 0; part != 2; ++part) {
            char* separator = std::strrchr(path, '\\');
            if (separator) *separator = '\0';
        }
        strcat_s(path, "\\G3Together\\steam_api.dll");
    }
    // Spacewar: steam_api takes the app id from the environment before steam_appid.txt, also when Steam started the game
    // as Gothic 3 (39500): the mod must not act as THQ Nordic's app.
    SetEnvironmentVariableA("SteamAppId", "480");
    SetEnvironmentVariableA("SteamGameId", "480");
    HMODULE dll = LoadLibraryExA(path, nullptr, LOAD_WITH_ALTERED_SEARCH_PATH);
    if (!dll) {
        Logf("steam: %s not loaded (%lu)", path, GetLastError());
        return Fail("steam_api.dll not found (G3Together folder in the game folder)");
    }
    for (auto const& e : Exports) {
        FARPROC function = GetProcAddress(dll, e.name);
        if (!function) {
            char text[160];
            Format(text, "steam_api.dll too old (no %s)", e.name);
            return Fail(text);
        }
        *e.slot = reinterpret_cast<void*>(function);
    }
    Logf("steam: loaded %s", path);
    return true;
}
}

bool Start(bool host, unsigned short port)
{
    if (ready || tried) return ready;
    tried = true;
    isHost = host;
    enetPort = port;
    WSADATA wsa;
    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) return Fail("winsock not started");
    if (!Load()) return false;
    char text[1024] = "";
    const int result = api.InitFlat(text);
    if (result != 0) {
        Logf("steam: InitFlat %d: %s", result, text);
        return Fail(result == 2 ? "Steam is not running" : result == 3 ? "the Steam client is too old" : *text ? text : "Steam did not start");
    }
    api.DispatchInit();
    pipe = api.GetHSteamPipe();
    user = api.User();
    matchmaking = api.Matchmaking();
    friends = api.Friends();
    messages = api.Messages();
    if (!user || !matchmaking || !friends || !messages) return Fail("Steam interfaces missing");
    if (void* utils = api.Utils()) api.InitRelayNetworkAccess(utils);  // the relay network is ready by the first message
    me = api.UserSteamId(user);
    strncpy_s(persona, Text(api.PersonaName(friends)), _TRUNCATE);
    ready = true;
    Logf("steam: ready as Spacewar (480), %s (%llu), %s", persona, me, isHost ? "host" : "client");
    if (isHost) {
        CreateLobby();
        return true;
    }
    relay = LoopbackSocket(&relayPort);
    if (relay == INVALID_SOCKET) {
        ready = false;
        return Fail("no local relay socket");
    }
    wantedLobby = _strtoui64(GetConfig().steamLobby, nullptr, 10);
    ParseConnect(GetCommandLineA());
    Logf("steam: relay on 127.0.0.1:%u%s", relayPort, wantedLobby ? " (steam_lobby set)" : "");
    return true;
}

unsigned short RelayPort() { return relayPort; }

void Pump()
{
    if (!ready) return;
    api.DispatchRunFrame(pipe);
    struct { int user; int callback; char* param; int size; } message{};  // CallbackMsg_t
    while (api.DispatchNext(pipe, &message)) {
        if (message.callback == CbCallCompleted) {
            u64 call = 0;
            int callback = 0;
            unsigned size = 0;
            std::memcpy(&call, message.param, 8);
            std::memcpy(&callback, message.param + 8, 4);
            std::memcpy(&size, message.param + 12, 4);
            char result[512];
            sbool failed = 0;
            if (size <= sizeof(result) && api.DispatchResult(pipe, call, result, static_cast<int>(size), callback, &failed) && !failed)
                OnCallback(callback, result, static_cast<int>(size));
        } else {
            OnCallback(message.callback, message.param, message.size);
        }
        api.DispatchFree(pipe);
    }
    if (isHost && !lobby && Now() - lastCreate > 15000) CreateLobby();  // the first try failed: again every 15 s
    if (!isHost) Search();
    // Data before control: a friend who quits sends ENet's goodbye and then 'B'; the other order closed its socket first.
    Receive(ChannelData);
    Receive(ChannelControl);
    Forward();
    const uint32_t now = Now();
    if (!isHost && owner) {
        Remote* host = Find(owner);
        if (host && !host->linked && now - lastHello > 1000) {
            Control(owner, 'H', true);
            lastHello = now;
        }
    }
    for (auto& r : remotes) {
        if (!r.id) continue;
        // A stranger's session (any Steam user may write to a host) or a hello that never got through goes after 60 s.
        if (now - r.heard > (r.linked ? SilenceMs : 60000)) {
            Drop(&r, r.linked ? "silent for 180 s" : "not linked after 60 s");
            continue;
        }
        if (r.linked && now - r.lastPing > 1000) {
            Control(r.id, 'P', false);
            r.lastPing = now;
        }
    }
}

void Stop()
{
    if (!ready) return;
    Forward();  // ENet's goodbye to the others
    LeaveLobby();
    if (relay != INVALID_SOCKET) closesocket(relay);
    relay = INVALID_SOCKET;
    ready = false;
    Log("steam: stopped");
}

bool Ready() { return ready; }
char const* Error() { return error; }
char const* HostName() { return hostName; }

bool HostLinked()
{
    Remote const* host = owner && !isHost ? Find(owner) : nullptr;
    return host && host->linked;
}

int LinkedCount()
{
    int count = 0;
    for (auto const& r : remotes) count += r.linked ? 1 : 0;
    return count;
}

void Status(void (*log)(char const*))
{
    char line[512];
    int n = Format(line, "STEAMSTATUS ready=%d role=%s lobby=%llu owner=%llu host=%s linked=%d offset=%d error=%s", ready ? 1 : 0,
                   isHost ? "host" : "client", lobby, owner, hostName, LinkedCount(), senderOffset, *error ? error : "-");
    for (auto const& r : remotes)
        if (r.id) n += Format(line + n, sizeof(line) - n, " peer=%llu:%d:%u:%u:%d", r.id, r.linked ? 1 : 0, r.toEnet, r.toSteam, r.rtt);
    log(line);
}
}
