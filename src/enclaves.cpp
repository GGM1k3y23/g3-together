// SPDX-License-Identifier: GPL-3.0-or-later
#include "enclaves.h"
#include "ai.h"
#include "infocmd.h"

#include "config.h"
#include "items.h"
#include "log.h"
#include "net.h"

#include <Game.h>
#include <Script.h>
#include <Windows.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <string>
#include <vector>

namespace g3t::enclaves {
namespace {
enum Mode : uint8_t { Shared = 0, Together = 1, Personal = 2 };
constexpr uint32_t ScanEvery = 500;    // ms
constexpr uint32_t TheftWait = 3000;   // ms a client holds a theft back until its take is settled
constexpr int Crimes = gEPlayerCrime_Count;

struct Counters {
    int32_t crime[Crimes] = {}, suspect[Crimes] = {};
    int32_t known = 0, fame = 0;
};
bool Same(Counters const& a, Counters const& b) { return !std::memcmp(&a, &b, sizeof(a)); }

struct Pending {
    std::string name;
    net::EnclaveNote delta;
    uint32_t at = 0;
    unsigned returned = 0;  // items::Returned() when the theft was counted
    unsigned begun = 0, done = 0;  // items::TakesBegun()/TakesDone() then
};

std::map<std::string, Counters> seen;  // what this machine last saw or was told, per enclave
std::vector<Pending> pending;          // client: thefts waiting for the take's outcome
Mode mode = Shared;                    // host: from the INI; client: from the host's notes
bool sync = true;                      // QA negative control
bool wasConnected = false;
uint32_t lastScan = 0;
uint32_t lastTakeTask = 0;  // when this hero was last in PS_Normal_Take (where the game counts a theft)
unsigned sent = 0, received = 0, applied = 0, deltas = 0, dropped = 0, keptLocal = 0, credits = 0;
// A town's status (routine, attack, flee: freed) turns where its defenders die: NotifyEnclave runs on the machine that
// simulates them (the host, or a client for NPCs in its region). The counters above travel, the status did not: Ardea freed
// on the host stayed under attack on the client (stageLiberate 24.10.), and a raider the client simulated, killed last,
// turned it only there. Every machine sends a changed status as the enclave's world state (infocmd kind 1: status, raid,
// revolution; a client's goes to the host, which applies it and passes it on), and the host all of them to a player who joins.
std::map<std::string, int> statusSeen;
bool statusSync = true;  // QA negative control: townstatusoff
unsigned statusSent = 0;
bool keepLocal = true;        // QA negative control: KeepLocalOff (the host's counters overwrite unreported deeds)
// Fame comes from quest rewards, which run on every machine: the host has counted it already, a client's delta counted it
// twice (Reddock at 100 early, playtest 27.10.). true: the host adds a client's fame again, as before.
bool clientFame = false;

// Political crimes (PlayerMemory.PoliticalCrimeCount per alignment): the game counts one only in ChangeEnclaveStatus, when a
// town turns to Flee (freed; not Ardea), for GetPlayer of the machine where it turns (re/out/world-scripts-c.c 1340). A boss
// offers its news while that count is above PoliticalSuspectComment (HasBossNews 0xbd050): after Montera was freed only
// the liberating machine's hero heard it from Kan (p4-dialog 26.10.). Owner 26.10.: as the reputation setting. Every machine
// watches its hero's counts; a rise goes to the others (EnclavePolitical) in shared and together, which add it to their
// own hero; personal keeps it with the liberator, and a rise from another player's kill of the town's defender goes to that
// player's hero instead (NotifyFreeing).
constexpr int Alignments = 13;  // gEPoliticalAlignment 0..12
int32_t politicalSeen[Alignments] = {};
eCEntity* politicalHero = nullptr;  // whose counts politicalSeen holds (a loaded or new hero starts over)
bool politicalSync = true;          // QA negative control: politicaloff
unsigned politicalSent = 0, politicalApplied = 0, politicalMoved = 0;

bool ReadPolitical(int32_t out[Alignments])
{
    eCEntity* hero = gCSession::GetInstance().GetHero();
    if (!hero || !hero->GetPropertySet(eEPropertySetType_PlayerMemory)) return false;
    Entity self(hero);
    for (int i = 0; i < Alignments; ++i) out[i] = self.PlayerMemory.GetPoliticalCrimeCount(static_cast<gEPoliticalAlignment>(i));
    return true;
}

// This hero's political crime count of one alignment changed by `by` (a note, or a move to the killer); the baseline follows
// so that the watch does not send it on.
void AddPolitical(int alignment, int32_t by)
{
    eCEntity* hero = gCSession::GetInstance().GetHero();
    if (!hero || !hero->GetPropertySet(eEPropertySetType_PlayerMemory) || alignment < 0 || alignment >= Alignments) return;
    Entity self(hero);
    const auto a = static_cast<gEPoliticalAlignment>(alignment);
    const int32_t value = self.PlayerMemory.GetPoliticalCrimeCount(a) + by;
    self.PlayerMemory.SetPoliticalCrimeCount(a, value < 0 ? 0 : value);
    if (politicalHero == hero) politicalSeen[alignment] = self.PlayerMemory.GetPoliticalCrimeCount(a);
}

net::EnclaveNote PoliticalNote(int alignment, int32_t count)
{
    net::EnclaveNote n;
    n.op = net::EnclavePolitical;
    n.mode = mode;
    strncpy_s(n.name, "political", _TRUNCATE);
    n.known = alignment;
    n.fame = count;
    return n;
}

// To every other player: a client's goes to the host, the host's to every client but the one it came from.
void SendPolitical(net::EnclaveNote const& note, uint8_t except)
{
    if (!net::IsHost()) {
        net::SendEnclave(note, 0);
        ++politicalSent;
        return;
    }
    for (int id = 2; id <= net::MaxPlayers; ++id) {
        if (id == except || !net::InWorld(static_cast<uint8_t>(id))) continue;
        net::SendEnclave(note, static_cast<uint8_t>(id));
        ++politicalSent;
    }
}

// The watch (every scan): a rise of this hero's counts is this machine's liberation.
void WatchPolitical()
{
    int32_t now[Alignments];
    eCEntity* hero = gCSession::GetInstance().GetHero();
    if (!ReadPolitical(now)) return;
    if (hero != politicalHero) {
        politicalHero = hero;
        std::memcpy(politicalSeen, now, sizeof(now));
        return;
    }
    for (int i = 0; i < Alignments; ++i) {
        if (now[i] <= politicalSeen[i]) { politicalSeen[i] = now[i]; continue; }
        const int32_t rise = now[i] - politicalSeen[i];
        politicalSeen[i] = now[i];
        if (!politicalSync || mode == Personal) continue;
        SendPolitical(PoliticalNote(i, rise), 0);
        Logf("enclaves: this player committed a political crime against alignment %d (+%d, now %d); sent %s", i, rise, now[i],
             net::IsHost() ? "to everyone" : "to the host");
    }
}
uint32_t scanPausedUntil = 0;  // QA: no scan before this (GetTickCount), to widen the 500 ms window between deed and scan

Mode FromConfig()
{
    char const* r = GetConfig().reputation;
    if (!_stricmp(r, "together")) return Together;
    if (!_stricmp(r, "personal")) return Personal;
    return Shared;
}

char const* ModeName(Mode m) { return m == Together ? "together" : m == Personal ? "personal" : "shared"; }

gCEnclave_PS* Native(Entity& e)
{
    eCEntity* instance = e.GetInstance();
    return instance ? static_cast<gCEnclave_PS*>(instance->GetPropertySet(eEPropertySetType_Enclave)) : nullptr;
}

// The game's own members for reading (the script wrappers look properties up by name, see infocmd.cpp).
Counters Read(gCEnclave_PS* enclave)
{
    Counters c;
    bTValArray<GEI32> const& crime = enclave->AccessCrimeCount();
    bTValArray<GEI32> const& suspect = enclave->AccessSuspectComment();
    for (int i = 0; i < Crimes; ++i) {
        c.crime[i] = i < crime.GetCount() ? crime.GetAt(i) : 0;
        c.suspect[i] = i < suspect.GetCount() ? suspect.GetAt(i) : 0;
    }
    c.known = static_cast<int32_t>(static_cast<gEPlayerCrime const&>(enclave->AccessKnownPlayerCrime()));
    c.fame = enclave->AccessPlayerFame();
    return c;
}

// Writing through the game's setters (SetCrimeCount/SetSuspectComment, as AddEnclaveTheftCount does).
void Write(Entity& e, Counters const& c)
{
    gCEnclave_PS* enclave = Native(e);
    if (!enclave) return;
    for (int i = 0; i < Crimes; ++i) {
        e.Enclave.SetCrimeCount(static_cast<gEPlayerCrime>(i), c.crime[i]);
        e.Enclave.SetSuspectComment(static_cast<gEPlayerCrime>(i), c.suspect[i]);
    }
    enclave->AccessKnownPlayerCrime() = static_cast<gEPlayerCrime>(c.known);
    enclave->AccessPlayerFame() = c.fame;
}

net::EnclaveNote Note(uint8_t op, std::string const& name, Counters const& c)
{
    net::EnclaveNote n;
    n.op = op;
    n.mode = mode;
    strncpy_s(n.name, name.c_str(), _TRUNCATE);
    std::memcpy(n.crime, c.crime, sizeof(n.crime));
    std::memcpy(n.suspect, c.suspect, sizeof(n.suspect));
    n.known = c.known;
    n.fame = c.fame;
    return n;
}

// Every enclave of the world by name.
template <typename Fn> void ForEach(Fn fn)
{
    const GEInt count = PSEnclave::GetEnclaveCount();
    for (GEInt i = 0; i < count; ++i) {
        Entity e = PSEnclave::GetEnclaveAt(i);
        if (gCEnclave_PS* enclave = Native(e)) fn(std::string(e.GetName().GetText()), e, enclave);
    }
}

void SendState(std::string const& name, Counters const& c, uint8_t to)
{
    net::SendEnclave(Note(net::EnclaveState, name, c), to);
    ++sent;
}

// Client: what this player's deeds changed since the last look. Counts and fame as additions; acknowledgements and
// the known crime as the new values (the host keeps the higher).
void SendDelta(std::string const& name, Counters const& before, Counters const& now)
{
    Counters d = now;
    for (int i = 0; i < Crimes; ++i) d.crime[i] = now.crime[i] - before.crime[i];
    d.fame = now.fame - before.fame;
    Pending p{name, Note(net::EnclaveDelta, name, d), GetTickCount(), items::Returned(), items::TakesBegun(), items::TakesDone()};
    if (d.crime[gEPlayerCrime_Theft] > 0) {  // wait: the take this theft was counted for may go to someone else
        pending.push_back(p);
        return;
    }
    net::SendEnclave(p.delta, 0);
    ++sent;
}

void SettlePending()
{
    const uint32_t now = GetTickCount();
    for (size_t i = 0; i < pending.size();) {
        Pending& p = pending[i];
        // The game counts a theft before the hero takes the item (PS_Normal_Take). It was for nothing when the host gave the
        // item to someone else, or when this hero reached for an item and got none (the other player's take removed it
        // first, theftdiag both 20.10.). A theft without a take (from an NPC) goes on as counted.
        const bool reached = items::TakesBegun() > p.begun || items::TakesDone() > p.done ||
                             (lastTakeTask && static_cast<int32_t>(lastTakeTask - p.at) > -2000);
        const bool lost = items::Returned() > p.returned || (now - p.at > TheftWait && reached && items::TakesDone() == p.done);
        if (lost) {
            // Take it back here; the rest of the change (if any) still goes to the host.
            Entity e = PSEnclave::GetEnclave(bCString(p.name.c_str()));
            if (gCEnclave_PS* enclave = Native(e)) {
                Counters c = Read(enclave);
                c.crime[gEPlayerCrime_Theft] -= p.delta.crime[gEPlayerCrime_Theft];
                Write(e, c);
                seen[p.name] = c;
            }
            p.delta.crime[gEPlayerCrime_Theft] = 0;
            ++dropped;
            Logf("enclaves: theft at %s taken back (someone else got the item)", p.name.c_str());
            net::SendEnclave(p.delta, 0);
            ++sent;
            pending.erase(pending.begin() + static_cast<std::ptrdiff_t>(i));
        } else if (now - p.at > TheftWait) {
            net::SendEnclave(p.delta, 0);
            ++sent;
            pending.erase(pending.begin() + static_cast<std::ptrdiff_t>(i));
        } else {
            ++i;
        }
    }
}

void OnNote(uint8_t from, net::EnclaveNote const& note)
{
    if (!sync) return;
    ++received;
    if (note.op == net::EnclavePolitical) {  // another player freed a town (shared, together), or this one did (personal: moved)
        if (!politicalSync) return;
        AddPolitical(note.known, note.fame);
        ++politicalApplied;
        Logf("enclaves: player %u: political crime against alignment %d (+%d) for this hero too (%s)", from, note.known, note.fame,
             ModeName(static_cast<Mode>(note.mode <= Personal ? note.mode : Shared)));
        if (net::IsHost() && note.mode != Personal) SendPolitical(note, from);  // and on to the other clients
        return;
    }
    if (note.op == net::EnclaveCredit) {  // personal: a murder by this machine's player, counted where the victim was simulated
        Entity e = PSEnclave::GetEnclave(bCString(note.name));
        gCEnclave_PS* enclave = Native(e);
        if (!enclave || mode != Personal) return;
        Counters c = Read(enclave);
        c.crime[gEPlayerCrime_Murder] += note.crime[gEPlayerCrime_Murder];
        Write(e, c);
        ++credits;
        Logf("enclaves: player %u counted a murder of this player at %s (murders %d)", from, note.name, c.crime[gEPlayerCrime_Murder]);
        return;
    }
    if (net::IsHost()) {
        if (note.op == net::EnclaveRequest) {
            ForEach([&](std::string const& name, Entity& e, gCEnclave_PS* enclave) {
                SendState(name, Read(enclave), from);
                if (statusSync) infocmd::Changed(e.GetInstance());  // its status too (to everyone; the others have it already)
            });
            return;
        }
        if (note.op != net::EnclaveDelta || mode == Personal) return;
        Entity e = PSEnclave::GetEnclave(bCString(note.name));
        gCEnclave_PS* enclave = Native(e);
        if (!enclave) return;
        Counters c = Read(enclave);
        for (int i = 0; i < Crimes; ++i) {
            c.crime[i] += note.crime[i];
            if (note.suspect[i] > c.suspect[i]) c.suspect[i] = note.suspect[i];
        }
        if (note.known > c.known) c.known = note.known;
        if (clientFame) c.fame += note.fame;
        Write(e, c);
        const std::string name(note.name);
        seen[name] = Read(enclave);
        ++deltas;
        Logf("enclaves: player %u changed %s (theft +%d, murder +%d)", from, note.name, note.crime[gEPlayerCrime_Theft],
             note.crime[gEPlayerCrime_Murder]);
        SendState(name, seen[name], 0);
        return;
    }
    if (note.op != net::EnclaveState) return;
    mode = static_cast<Mode>(note.mode <= Personal ? note.mode : Shared);
    if (mode == Personal) return;
    Entity e = PSEnclave::GetEnclave(bCString(note.name));
    gCEnclave_PS* enclave = Native(e);
    if (!enclave) return;
    // This player's deeds since the last scan (every 500 ms) are not in the host's counters yet: report them first, or
    // the host's values would overwrite them and the shared counter lose them (release-review-c.md P1, 21.10.).
    const auto last = seen.find(note.name);
    if (keepLocal && last != seen.end()) {
        const Counters now = Read(enclave);
        if (!Same(now, last->second)) {
            const Counters before = last->second;
            last->second = now;
            SendDelta(note.name, before, now);
            ++keptLocal;
        }
    }
    Counters c;
    std::memcpy(c.crime, note.crime, sizeof(c.crime));
    std::memcpy(c.suspect, note.suspect, sizeof(c.suspect));
    c.known = note.known;
    c.fame = note.fame;
    // Thefts held back until their take is settled are not in the host's counters either: keep them on top here
    // (SettlePending takes one back from this sum if its take was lost).
    if (keepLocal)
        for (Pending const& p : pending)
            if (p.name == note.name) c.crime[gEPlayerCrime_Theft] += p.delta.crime[gEPlayerCrime_Theft];
    Write(e, c);
    seen[note.name] = Read(enclave);
    ++applied;
}
}

namespace {
constexpr uintptr_t NotifyRva = 0x5ef70;  // NotifyEnclave in Script_Game.dll (CP 1.75.14), C death-callers-c.c
constexpr uint32_t RemoteKillWait = 60000;  // ms a marked victim waits for its Kill (the death task runs a frame later)
struct RemoteKill {
    uint8_t player = 0;
    uint32_t generation = 0;  // host: that slot's connection when it struck (net::PeerGeneration)
    std::string actor;  // who struck: the player's puppet or a companion (the game's Montera filter checks the killer)
    uint32_t at = 0;
};
std::map<std::string, RemoteKill> remoteKills;  // by victim GUID
gFScript originalNotify = nullptr;
bool notifyInstalled = false, notifyNative = false, remoteMurders = true;
uint32_t lastNotifyTry = 0;
unsigned remoteCounted = 0, remoteCredited = 0, remoteNative = 0, remoteNotCounted = 0, remoteExpired = 0, remoteMoved = 0;
// personal: a credit for a killer who is not in the host's world just then (loading) waits for that same connection to come
// back in (C p7-implementation-review-20261024-c.md 3); one that left (another connection in its slot) is dropped.
struct HeldCredit {
    uint8_t player = 0;
    uint32_t generation = 0, at = 0;
    std::string town, victim;
};
std::vector<HeldCredit> heldCredits;
uint32_t creditHoldUntil = 0;  // QA (credithold <ms>): the killer counts as out of the world until then
bool creditQueue = true;       // QA negative control (creditqueueoff): such a credit is lost, as before 24.10.
unsigned creditsHeld = 0, creditsLate = 0, creditsLost = 0;

std::string Key(uint8_t const guid[16]) { return std::string(reinterpret_cast<char const*>(guid), 16); }

std::string KeyOf(eCEntity* entity)
{
    const eCEntityProxy proxy(entity);
    return Key(reinterpret_cast<uint8_t const*>(&proxy.GetEntityID().GetCore()));
}

unsigned char* NativeNotify()
{
    static const unsigned char expected[] = {0x55, 0x8b, 0xec, 0x83, 0xe4, 0xf8, 0x81, 0xec, 0x84, 0x08, 0x00, 0x00};
    HMODULE module = GetModuleHandleA("Script_Game.dll");
    if (!module) return nullptr;
    auto* code = reinterpret_cast<unsigned char*>(module) + NotifyRva;
    return std::memcmp(code, expected, sizeof(expected)) == 0 ? code : nullptr;
}

// The game's conditions of NotifyEnclave mode 0 for the victim and its town (C 12:28, section 2); null: it counts.
char const* NotCounted(eCEntity* victim, Entity& town, gCEnclave_PS* enclave, char const* actor)
{
    if (!enclave) return "no town";
    char const* name = victim->GetName().GetText();
    if (!std::strcmp(town.GetName().GetText(), "Faring") &&
        (!std::strcmp(name, "Orc_Scout_05") || !std::strcmp(name, "Orc_Scout_06") || !std::strcmp(name, "Orc_Warrior_12")))
        return "Faring exception";
    if (static_cast<gEEnclaveStatus>(enclave->AccessStatus()) == gEEnclaveStatus_Flee) return "town flees";
    if (GetScriptAdminExt().CallScript(bCString("GetPoliticalAttitude"), victim, town.GetInstance(), 0) != 1) return "not political";
    for (char const* orc : {"Montera_Orc_02", "Montera_Orc_03", "Montera_Orc_04", "Montera_Orc_05"})
        if (!std::strcmp(actor, orc)) return "excepted killer";
    return nullptr;
}

// personal: one murder into the killer's own counters, on its machine.
void Credit(RemoteKill const& kill, char const* town, char const* victim)
{
    net::EnclaveNote credit;
    credit.op = net::EnclaveCredit;
    credit.mode = mode;
    strncpy_s(credit.name, town, _TRUNCATE);
    credit.crime[gEPlayerCrime_Murder] = 1;
    // A client reaches the host only; the host only the connection that struck (a slot taken again is someone else, C
    // p7-implementation-review-20261024-c.md 4). Not kept for later: a killer loading or gone just then loses it (3, open).
    if ((!net::IsHost() && kill.player != 1) || (net::IsHost() && net::PeerGeneration(kill.player) != kill.generation)) {
        ++remoteNotCounted;
        Logf("enclaves: player %u murdered %s at %s; personal: not counted (that player is not reachable)", kill.player, victim, town);
        return;
    }
    if (net::IsHost() && (!net::InWorld(kill.player) || static_cast<int32_t>(GetTickCount() - creditHoldUntil) < 0)) {
        if (!creditQueue) {
            ++creditsLost;
            Logf("enclaves: player %u murdered %s at %s; personal: lost (that player is not in the world)", kill.player, victim, town);
            return;
        }
        HeldCredit held;
        held.player = kill.player;
        held.generation = kill.generation;
        held.at = GetTickCount();
        held.town = town;
        held.victim = victim;
        heldCredits.push_back(held);
        ++creditsHeld;
        Logf("enclaves: player %u murdered %s at %s; personal: held until that player is in the world", kill.player, victim, town);
        return;
    }
    net::SendEnclave(credit, net::IsHost() ? kill.player : 0);
    ++sent;
    ++remoteCredited;
    Logf("enclaves: player %u murdered %s at %s; personal: counted on that player's machine", kill.player, victim, town);
}

// NotifyEnclave for another player's kill: if the town turns to Flee here, the game booked the political crime on this
// machine's hero; personal: it is the killer's (as a murder is), so it moves to that player's hero.
GEInt NotifyFreeing(gCScriptProcessingUnit* spu, GELPVoid self, GELPVoid other, GEInt args, RemoteKill const& kill)
{
    int32_t before[Alignments], after[Alignments];
    const bool watched = mode == Personal && politicalSync && ReadPolitical(before);
    const GEInt result = originalNotify(spu, self, other, args);
    if (!watched || !ReadPolitical(after)) return result;
    for (int i = 0; i < Alignments; ++i) {
        if (after[i] <= before[i]) continue;
        const int32_t rise = after[i] - before[i];
        AddPolitical(i, -rise);
        ++politicalMoved;
        if ((!net::IsHost() && kill.player != 1) || (net::IsHost() && net::PeerGeneration(kill.player) != kill.generation)) {
            Logf("enclaves: player %u freed a town (alignment %d); personal: not counted (that player is not reachable)", kill.player, i);
            continue;
        }
        net::SendEnclave(PoliticalNote(i, rise), net::IsHost() ? kill.player : 0);
        ++politicalSent;
        Logf("enclaves: player %u freed a town (alignment %d, +%d); personal: counted on that player's hero", kill.player, i, rise);
    }
    return result;
}

// Whether the game counts this murder itself: its killer (the victim's CurrentAttacker) is our hero or of its party.
bool CountsNatively(eCEntity* killer)
{
    eCEntity* hero = gCSession::GetInstance().GetHero();
    return killer && hero && (killer == hero || (killer->GetPropertySet(eEPropertySetType_Party) &&
                                                 Entity(killer).Party.GetPartyLeader().GetInstance() == hero));
}

void CountRemote(eCEntity* victim, RemoteKill const& kill)
{
    Entity dead(victim);
    Entity town = dead.GetEnclave();
    gCEnclave_PS* enclave = Native(town);
    if (char const* why = NotCounted(victim, town, enclave, kill.actor.c_str())) {
        ++remoteNotCounted;
        Logf("enclaves: player %u killed %s: no murder (%s)", kill.player, dead.GetName().GetText(), why);
        return;
    }
    if (!remoteMurders) { ++remoteNotCounted; return; }
    char const* name = town.GetName().GetText();
    if (mode == Personal) {
        Credit(kill, name, dead.GetName().GetText());
        return;
    }
    Counters c = Read(enclave);  // the scan carries it on (host: to everyone; client: as this machine's delta)
    ++c.crime[gEPlayerCrime_Murder];
    Write(town, c);
    ++remoteCounted;
    Logf("enclaves: player %u murdered %s at %s (murders %d)", kill.player, dead.GetName().GetText(), name, c.crime[gEPlayerCrime_Murder]);
}

GEInt GE_STDCALL Notify(gCScriptProcessingUnit* spu, GELPVoid self, GELPVoid other, GEInt args)
{
    if (args != 0 || remoteKills.empty()) return originalNotify(spu, self, other, args);
    eCEntity* killer = self ? static_cast<Entity*>(self)->GetInstance() : (spu ? spu->GetSelfEntity() : nullptr);
    eCEntity* victim = other ? static_cast<Entity*>(other)->GetInstance() : (spu ? spu->GetOtherEntity() : nullptr);
    if (!victim) return originalNotify(spu, self, other, args);
    const auto it = remoteKills.find(KeyOf(victim));
    if (it == remoteKills.end()) return originalNotify(spu, self, other, args);
    const RemoteKill kill = it->second;
    remoteKills.erase(it);  // once per death
    if (!CountsNatively(killer)) {
        CountRemote(victim, kill);  // first, as the game counts before its other steps
        return NotifyFreeing(spu, self, other, args, kill);
    }
    ++remoteNative;
    if (mode != Personal || !remoteMurders) return NotifyFreeing(spu, self, other, args, kill);
    // personal: the game books it on this machine's player (its killer is an old CurrentAttacker, this hero's knock-out),
    // but the deadly blow was the other player's: that one murder goes to the other's record (C review 2).
    Entity town = Entity(victim).GetEnclave();
    gCEnclave_PS* enclave = Native(town);
    const int before = enclave ? Read(enclave).crime[gEPlayerCrime_Murder] : 0;
    const GEInt result = NotifyFreeing(spu, self, other, args, kill);
    if (enclave && Read(enclave).crime[gEPlayerCrime_Murder] > before) {
        Counters c = Read(enclave);
        --c.crime[gEPlayerCrime_Murder];
        Write(town, c);
        ++remoteMoved;
        Credit(kill, town.GetName().GetText(), Entity(victim).GetName().GetText());
    }
    return result;
}

void InstallNotify()
{
    if (notifyInstalled) return;
    const uint32_t now = GetTickCount();
    if (now - lastNotifyTry < 1000) return;
    lastNotifyTry = now;
    gSScript* script = nullptr;
    if (!GetScriptAdminExt().GetScripts().GetAt(bCString("NotifyEnclave"), script) || !script || !script->m_funcScript) return;
    // Kill calls it directly, not by name: only the jump in the native function sees those calls.
    if (unsigned char* code = NativeNotify(); code && reinterpret_cast<void*>(script->m_funcScript) == code) {
        originalNotify = reinterpret_cast<gFScript>(ai::JumpTo(code, reinterpret_cast<void*>(&Notify)));
        notifyNative = originalNotify != nullptr;
    }
    notifyInstalled = true;
    Logf("enclaves: NotifyEnclave %s", notifyNative ? "wrapped (native jump)" : "not wrapped (another build): murders by other players count nowhere");
}
}

void NoteRemoteKill(uint8_t const victim[16], uint8_t player, eCEntity* actor)
{
    if (!notifyNative) return;
    const uint32_t now = GetTickCount();
    for (auto it = remoteKills.begin(); it != remoteKills.end();) {
        if (now - it->second.at > RemoteKillWait) { ++remoteExpired; it = remoteKills.erase(it); }
        else ++it;
    }
    RemoteKill& kill = remoteKills[Key(victim)];
    kill.player = player;
    kill.generation = net::IsHost() ? net::PeerGeneration(player) : 0;
    kill.actor = actor ? actor->GetName().GetText() : "";
    kill.at = now;
}

void PoliticalOff(LogFn log)
{
    politicalSync = false;
    log("POLITICALOFF ok=1");
}

void TownStatusOff(LogFn log)
{
    statusSync = false;
    log("TOWNSTATUSOFF ok=1");
}

void RemoteMurdersOff(LogFn log)
{
    remoteMurders = false;
    log("REMOTEMURDERSOFF ok=1");
}

void Start()
{
    mode = FromConfig();
    net::SetEnclaveHandler(&OnNote);
}

// Host: held credits for players back in the world (same connection) go out; for players gone they are dropped.
void SendHeldCredits()
{
    const uint32_t now = GetTickCount();
    for (auto it = heldCredits.begin(); it != heldCredits.end();) {
        const bool same = net::PeerJoined(it->player) && net::PeerGeneration(it->player) == it->generation;
        if (!same || now - it->at > 600000) {
            ++creditsLost;
            Logf("enclaves: a held murder of %s at %s for player %u dropped (%s)", it->victim.c_str(), it->town.c_str(), it->player,
                 same ? "ten minutes passed" : "that player left");
            it = heldCredits.erase(it);
            continue;
        }
        if (!net::InWorld(it->player) || static_cast<int32_t>(now - creditHoldUntil) < 0) { ++it; continue; }
        net::EnclaveNote credit;
        credit.op = net::EnclaveCredit;
        credit.mode = mode;
        strncpy_s(credit.name, it->town.c_str(), _TRUNCATE);
        credit.crime[gEPlayerCrime_Murder] = 1;
        net::SendEnclave(credit, it->player);
        ++sent;
        ++remoteCredited;
        ++creditsLate;
        Logf("enclaves: the held murder of %s at %s sent to player %u", it->victim.c_str(), it->town.c_str(), it->player);
        it = heldCredits.erase(it);
    }
}

void CreditQueueOff(LogFn log)
{
    creditQueue = false;
    log("CREDITQUEUEOFF ok=1");
}

void CreditHold(char const* ms, LogFn log)
{
    creditHoldUntil = GetTickCount() + static_cast<uint32_t>(std::atoi(ms));
    log("CREDITHOLD ok=1");
}

void Tick()
{
    if (gCSession::GetInstance().IsGameRunning()) InstallNotify();
    if (net::IsHost() && !heldCredits.empty()) SendHeldCredits();
    if (!sync || !net::Connected() || !gCSession::GetInstance().IsGameRunning()) { wasConnected = false; return; }
    if (!wasConnected) {
        wasConnected = true;
        if (net::IsHost()) {
            mode = FromConfig();
        } else {  // joined (or joined again): the host's counters replace this machine's
            net::EnclaveNote ask;
            ask.op = net::EnclaveRequest;
            net::SendEnclave(ask, 0);
            ++sent;
        }
    }
    if (!net::IsHost()) {
        if (eCDynamicEntity* hero = gCSession::GetInstance().GetHero()) {
            if (Entity(hero).Routine.GetCurrentTask() == bCString("PS_Normal_Take")) lastTakeTask = GetTickCount();
        }
        SettlePending();
    }
    const uint32_t now = GetTickCount();
    if (now - lastScan < ScanEvery || static_cast<int32_t>(now - scanPausedUntil) < 0) return;
    lastScan = now;
    ForEach([&](std::string const& name, Entity& e, gCEnclave_PS* enclave) {
        if (statusSync) {
            const int status = static_cast<int>(static_cast<gEEnclaveStatus const&>(enclave->AccessStatus()));
            const auto known = statusSeen.find(name);
            if (known == statusSeen.end()) {
                statusSeen[name] = status;
            } else if (known->second != status) {
                Logf("enclaves: %s turned from status %d to %d; sent %s", name.c_str(), known->second, status,
                     net::IsHost() ? "to everyone" : "to the host");
                known->second = status;
                infocmd::Changed(e.GetInstance());
                ++statusSent;
            }
        }
        const Counters c = Read(enclave);
        auto it = seen.find(name);
        if (it == seen.end()) { seen[name] = c; return; }
        if (Same(it->second, c)) return;
        const Counters before = it->second;
        it->second = c;
        if (net::IsHost()) SendState(name, c, 0);
        else if (mode != Personal) SendDelta(name, before, c);
    });
    WatchPolitical();
}

void OnWorldChange()
{
    politicalHero = nullptr;
    seen.clear();
    statusSeen.clear();
    pending.clear();
    remoteKills.clear();
    wasConnected = false;
}

void Report(LogFn log)
{
    char line[640];
    Format(line, "ENCLAVESYNC sync=%d mode=%s config=%s sent=%u received=%u applied=%u deltas=%u dropped=%u pending=%u seen=%u kept_local=%u "
              "notify=%d remote_kills=%u remote_counted=%u remote_credited=%u remote_native=%u remote_not_counted=%u remote_expired=%u credits=%u "
              "remote_moved=%u status_sync=%d status_sent=%u credits_held=%u credits_late=%u credits_lost=%u political_sync=%d political_sent=%u "
              "political_applied=%u political_moved=%u",
              sync ? 1 : 0, ModeName(mode), GetConfig().reputation, sent, received, applied, deltas, dropped,
              static_cast<unsigned>(pending.size()), static_cast<unsigned>(seen.size()), keptLocal, notifyNative ? 1 : 0,
              static_cast<unsigned>(remoteKills.size()), remoteCounted, remoteCredited, remoteNative, remoteNotCounted, remoteExpired, credits,
              remoteMoved, statusSync ? 1 : 0, statusSent, creditsHeld, creditsLate, creditsLost, politicalSync ? 1 : 0, politicalSent,
              politicalApplied, politicalMoved);
    log(line);
}

bool Together() { return mode == Mode::Together; }

void SyncOff(LogFn log)
{
    sync = false;
    log("ENCLAVESYNCOFF ok=1");
}

void KeepLocalOff(LogFn log)
{
    keepLocal = false;
    log("KEEPLOCALOFF ok=1");
}

void PauseScan(char const* ms, LogFn log)
{
    scanPausedUntil = GetTickCount() + static_cast<uint32_t>(std::atoi(ms));
    log("ENCLAVESCANPAUSE ok=1");
}

void MurderCandidates(char const* name, LogFn log)
{
    char line[400];
    Entity e = PSEnclave::GetEnclave(bCString(name));
    gCEnclave_PS* enclave = Native(e);
    if (!enclave) { log("REFUSED murdercand: no such enclave"); return; }
    const int status = static_cast<int>(static_cast<gEEnclaveStatus const&>(enclave->AccessStatus()));
    const Counters c = Read(enclave);
    bTObjArray<eCEntityProxy> members = enclave->GetMembers();
    int shown = 0;
    for (GEInt m = 0; m < members.GetCount() && shown < 40; ++m) {
        eCEntity* member = members.AccessAt(m).GetEntity();
        if (!member || !member->GetPropertySet(eEPropertySetType_NPC)) continue;
        Entity npc(member);
        char const* who = member->GetName().GetText();
        const GEInt political = GetScriptAdminExt().CallScript(bCString("GetPoliticalAttitude"), member, e.GetInstance(), 0);
        char const* why = NotCounted(member, e, enclave, "");
        const eCEntityProxy proxy(member);
        bCPropertyID::bSCore g;  // for a claim on a member far from every player (stageLiberate)
        std::memcpy(&g, &proxy.GetEntityID().GetCore(), 16);
        Format(line, "MURDERCAND enclave=%s status=%d murders=%d known=%d member=%s alive=%d political=%d excepted=%d counts=%d "
                  "guid=%08X-%04X-%04X-%02X%02X-%02X%02X%02X%02X%02X%02X", name, status, c.crime[gEPlayerCrime_Murder], c.known, who,
                  npc.IsDead() ? 0 : 1, political, why && !std::strcmp(why, "Faring exception") ? 1 : 0, why ? 0 : 1, g.Data1, g.Data2,
                  g.Data3, g.Data4[0], g.Data4[1], g.Data4[2], g.Data4[3], g.Data4[4], g.Data4[5], g.Data4[6], g.Data4[7]);
        log(line);
        ++shown;
    }
    Format(line, "MURDERCAND end enclave=%s status=%d murders=%d members=%d", name, status, c.crime[gEPlayerCrime_Murder], members.GetCount());
    log(line);
}

void TownFarKill(char const* name, LogFn log)
{
    char line[200];
    Entity e = PSEnclave::GetEnclave(bCString(name));
    gCEnclave_PS* enclave = Native(e);
    eCEntity* hero = gCSession::GetInstance().GetHero();
    if (!enclave || !hero) { log("REFUSED townfarkill: no such enclave or no hero"); return; }
    const bCVector& h = hero->GetWorldPosition();
    bTObjArray<eCEntityProxy> members = enclave->GetMembers();
    int killed = 0, kept = 0, alive = 0;
    for (GEInt m = 0; m < members.GetCount(); ++m) {
        eCEntity* member = members.AccessAt(m).GetEntity();
        if (!member || !member->GetPropertySet(eEPropertySetType_NPC) || !member->GetPropertySet(eEPropertySetType_DamageReceiver)) continue;
        Entity npc(member);
        if (npc.IsDead()) continue;
        ++alive;
        const bCVector& p = member->GetWorldPosition();
        const float dx = p.GetX() - h.GetX(), dy = p.GetY() - h.GetY(), dz = p.GetZ() - h.GetZ();
        if (dx * dx + dy * dy + dz * dz < 4000.0f * 4000.0f) { ++kept; continue; }
        auto& admin = GetScriptAdminExt();
        admin.CallScript(bCString("AddHitPoints"), member, hero, -static_cast<GEInt>(npc.DamageReceiver.HitPoints));
        admin.CallScript(bCString("OnCheatKill"), hero, member, 0);
        ++killed;
    }
    Format(line, "TOWNFARKILL enclave=%s members=%d alive=%d killed=%d near=%d", name, members.GetCount(), alive, killed, kept);
    log(line);
}

void CheatKill(char const* name, LogFn log)
{
    char line[300];
    Entity npc = Entity::GetEntity(bCString(name));
    eCEntity* hero = gCSession::GetInstance().GetHero();
    if (!npc.GetInstance() || !hero) { log("REFUSED cheatkill: no such NPC or no hero"); return; }
    const GEInt result = GetScriptAdminExt().CallScript(bCString("OnCheatKill"), hero, npc.GetInstance(), 0);  // self kills other
    Format(line, "CHEATKILL name=%s result=%d dead=%d task=%s", name, result, npc.IsDead() ? 1 : 0, npc.Routine.GetCurrentTask().GetText());
    log(line);
}

// QA stand-in for a deed: "<enclave>-<crime>-<n>" (the QA channel passes only [A-Za-z0-9_-]) adds n to that crime here.
void AddCrime(char const* argument, LogFn log)
{
    char name[64] = {};
    int index = -1, n = 0;
    if (sscanf_s(argument, "%63[^-]-%d-%d", name, static_cast<unsigned>(sizeof(name)), &index, &n) != 3 || index < 0 || index >= Crimes) {
        log("REFUSED crimeadd: <enclave>-<crime 0..4>-<n>");
        return;
    }
    Entity e = PSEnclave::GetEnclave(bCString(name));
    gCEnclave_PS* enclave = Native(e);
    if (!enclave) { log("REFUSED crimeadd: no such enclave"); return; }
    Counters c = Read(enclave);
    c.crime[index] += n;
    Write(e, c);
    char line[120];
    Format(line, "CRIMEADD enclave=%s crime=%d now=%d", name, index, c.crime[index]);
    log(line);
}

// QA (guard news, 26.10.): how many deeds of a kind the town's guards have commented on (SuspectComment; crimeadd left it
// at the new count, crimenewsdiag 26.10.). A guard has news while the count is above it (HasGuardNews, re/out/news2-b.c).
void SetSuspect(char const* argument, LogFn log)
{
    char name[64] = {};
    int index = -1, n = 0;
    if (sscanf_s(argument, "%63[^-]-%d-%d", name, static_cast<unsigned>(sizeof(name)), &index, &n) != 3 || index < 0 || index >= Crimes) {
        log("REFUSED crimesuspect: <enclave>-<crime 0..4>-<n>");
        return;
    }
    Entity e = PSEnclave::GetEnclave(bCString(name));
    gCEnclave_PS* enclave = Native(e);
    if (!enclave) { log("REFUSED crimesuspect: no such enclave"); return; }
    Counters c = Read(enclave);
    c.suspect[index] = n;
    Write(e, c);
    const Counters now = Read(enclave);
    char line[120];
    Format(line, "CRIMESUSPECT enclave=%s crime=%d suspect=%d count=%d", name, index, now.suspect[index], now.crime[index]);
    log(line);
}

// QA (guard news, 26.10.): what a town knows of the player ("<enclave>-<gEPlayerCrime>", 3 theft, 4 murder), as
// MemorizePlayerCrime leaves it; shared like any change of the counters.
void SetKnown(char const* argument, LogFn log)
{
    char name[64] = {};
    int value = -1;
    if (sscanf_s(argument, "%63[^-]-%d", name, static_cast<unsigned>(sizeof(name)), &value) != 2 || value < 0 || value >= Crimes) {
        log("REFUSED crimeknown: <enclave>-<crime 0..4>");
        return;
    }
    Entity e = PSEnclave::GetEnclave(bCString(name));
    gCEnclave_PS* enclave = Native(e);
    if (!enclave) { log("REFUSED crimeknown: no such enclave"); return; }
    Counters c = Read(enclave);
    c.known = value;
    Write(e, c);
    char line[120];
    Format(line, "CRIMEKNOWN enclave=%s known=%d", name, value);
    log(line);
}
// QA negative controls of the play-evening fixes (27.10.): each switches its fix off (on: clientfameon).
bool Switch(char const* action, LogFn log)
{
    if (!std::strcmp(action, "clientfameon")) { clientFame = true; log("SWITCH clientfameon ok=1"); return true; }
    return false;
}
}
