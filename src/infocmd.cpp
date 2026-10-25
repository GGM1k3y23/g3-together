// SPDX-License-Identifier: GPL-3.0-or-later
#include "doors.h"
#include "pickpocket.h"
#include "occupancy.h"
#include "respawn.h"
#include "address.h"
#include "infocmd.h"

#include "body.h"
#include "downed.h"
#include "guide.h"
#include "log.h"
#include "net.h"
#include "npcs.h"
#include "remote.h"
#include "talk.h"

#include <Game.h>
#include <Script.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

namespace g3t::infocmd {
namespace {
unsigned observed = 0, distinct = 0, snapshotsSent = 0, snapshotsApplied = 0, sectorsSent = 0, sectorsApplied = 0;
std::string lastKey;
std::string lastText = "-";
bool sharing = true;  // QA negative control
// Owner's decision 21.10. (play evening): experience a dialog gives (handing in a quest) goes to every player, gold
// only to the one who hands in. The dialog runs on the speaker's machine only (GiveXP takes Entity::GetPlayer(),
// re/findings/info-commands.md); every other machine runs the same reward for its own hero (world state kind 11).
bool sharingXp = true;  // QA negative control `dialogxpoff`
bool givebackPlace = true;  // QA negative control `givebackposoff`
unsigned xpSent = 0, xpApplied = 0;
constexpr uint8_t DialogXp = 11;

// Commands whose result is a state of their first entity (static analysis of IS_Running_ProcessInfo: dialog
// flags, guard status, class, political alignment, immortality, routine, position, enclave raid/revolution).
// The result, not the command, is passed on: read 0.7 s later, so its argument formats do not matter.
char const* const StateCommands[] = {"SetClass", "SetPAL", "SetTeachEnabled", "SetTradeEnabled", "SetPartyEnabled", "SetMobEnabled",
                                     "SetSlaveryEnabled", "SetRevolutionEnabled", "SetRaidEnabled", "SetSleepingPoint", "SetWorkingPoint",
                                     "SetRelaxingPoint", "FullStop", "SetRoutine", "SetGuardStatus", "MoveTo", "StartRevolution", "Humanize",
                                     "JoinNPC", "JoinPlayer"};
constexpr uint32_t ReadAfter = 700;

struct Pending { uint8_t guid[16]; std::string sector; uint32_t due; bool moved = false; bool follow = false; };
std::vector<Pending> pending;

// Host: fights that dialogs on clients started with the host's NPCs, waiting until the dialog hold ends.
// joins: net::JoinCount() at the request. Player IDs are slots that a later join reuses, so a request outlived by a
// join (or by a world change) is dropped rather than risk attacking someone else.
struct Fight { uint8_t attacker[16]; uint8_t player; std::string reason; uint32_t until; uint32_t joins; };
std::vector<Fight> fights;
unsigned fightsSent = 0, fightsStarted = 0;

// The dialog command Attack runs the perception script AssessTarget (attack reason, enclave notice, target,
// attack task; static analysis of IS_Running_ProcessInfo). Its reason names map to gEAttackReason.
int ReasonOf(std::string const& name, eCEntity* attacker, eCEntity* target)
{
    if (!_stricmp(name.c_str(), "Duel")) return gEAttackReason_Duel;
    if (!_stricmp(name.c_str(), "ReactToWeapon")) return gEAttackReason_ReactToWeapon;
    if (!_stricmp(name.c_str(), "Revolution")) return gEAttackReason_Revolution;
    if (!_stricmp(name.c_str(), "Guard"))
        return GetScriptAdminExt().CallScript(bCString("GetGuardAttackReason"), attacker, target, 0);
    return gEAttackReason_Enemy;
}

void StartFights()
{
    const uint32_t now = net::Now();
    for (auto it = fights.begin(); it != fights.end();) {
        _GUID raw;
        std::memcpy(&raw, it->attacker, 16);
        Entity attacker = Entity(bCPropertyID(bCGuid(raw)));
        eCEntity* target = remote::PuppetOf(it->player);
        if (static_cast<int32_t>(now - it->until) > 0 || it->joins != net::JoinCount() || !net::Remotes()[it->player].connected) {
            it = fights.erase(it);
            continue;
        }
        if (!attacker.GetInstance() || !target) { ++it; continue; }  // not loaded here yet, or the puppet not spawned
        if (talk::IsHeld(attacker.GetInstance())) { ++it; continue; }  // still standing in the client's dialog
        const int reason = ReasonOf(it->reason, attacker.GetInstance(), target);
        GetScriptAdminExt().CallScript(bCString("AssessTarget"), attacker.GetInstance(), target, reason);
        remote::NoteContact(attacker.GetInstance(), target);
        ++fightsStarted;
        Logf("infocmd: %s attacks player %u (%s, reason %d) after a dialog", attacker.GetName().GetText(), it->player, it->reason.c_str(), reason);
        it = fights.erase(it);
    }
}

bool IsStateCommand(char const* command)
{
    for (char const* c : StateCommands)
        if (!std::strcmp(c, command)) return true;
    return false;
}

void GuidOf(eCEntity* entity, uint8_t out[16])
{
    const eCEntityProxy proxy(entity);
    std::memcpy(out, &proxy.GetEntityID().GetCore(), 16);
}

void Snapshot(Entity& e, net::WorldState& s)
{
    eCEntity* instance = e.GetInstance();
    s.kind = 1;
    GuidOf(instance, s.guid);
    if (instance->GetPropertySet(eEPropertySetType_Dialog)) {
        s.dialog = (e.Dialog.TeachEnabled ? 1 : 0) | (e.Dialog.TradeEnabled ? 2 : 0) | (e.Dialog.PartyEnabled ? 4 : 0) |
                   (e.Dialog.MobEnabled ? 8 : 0) | (e.Dialog.SlaveryEnabled ? 16 : 0) | 0x80;
    }
    if (instance->GetPropertySet(eEPropertySetType_NPC)) {
        s.npc = 1;
        s.guard = static_cast<uint8_t>(static_cast<int>(e.NPC.GuardStatus));
        s.npcClass = static_cast<uint8_t>(static_cast<int>(e.NPC.Class));
        s.alignment = static_cast<uint8_t>(static_cast<int>(e.NPC.PoliticalAlignment));
        s.immortal = e.NPC.Immortal ? 1 : 0;
    }
    if (auto* nav = static_cast<gCNavigation_PS*>(instance->GetPropertySet(eEPropertySetType_Navigation)))
        strncpy_s(s.routine, nav->AccessRoutine().GetText(), _TRUNCATE);
    // The game's own members, not the script wrapper: PSEnclave looks its properties up by name and this build
    // has no 'Status' under that name ("Cannot get property 'Status' in propertyset 'gCEnclave_PS'", then an
    // access violation in Game.dll during the soak test).
    if (auto* enclave = static_cast<gCEnclave_PS*>(instance->GetPropertySet(eEPropertySetType_Enclave))) {
        s.enclave = 1 | (enclave->AccessRaid() ? 2 : 0) | (enclave->AccessRevolution() ? 4 : 0);
        s.enclaveStatus = static_cast<uint8_t>(static_cast<gEEnclaveStatus const&>(enclave->AccessStatus()));
    }
    const bCVector& p = instance->GetWorldPosition();
    s.position[0] = p.GetX(); s.position[1] = p.GetY(); s.position[2] = p.GetZ();
    const bCQuaternion q = instance->GetWorldRotation();
    s.rotation[0] = q.GetX(); s.rotation[1] = q.GetY(); s.rotation[2] = q.GetZ(); s.rotation[3] = q.GetW();
}

void Apply(net::WorldState const& s, uint8_t from)
{
    if (s.kind == 10) {  // a door or lock state (doors.cpp)
        doors::OnState(from, s);
        return;
    }
    if (s.kind == 12) {  // a picked pocket (pickpocket.cpp)
        pickpocket::OnState(from, s);
        return;
    }
    if (s.kind == 13) {  // an object in use (occupancy.cpp)
        occupancy::OnState(from, s);
        return;
    }
    if (s.kind == 15) {  // a client's dialog partner gestures there (talk.cpp)
        talk::OnGesture(from, s.guid, static_cast<uint32_t>(std::strtoul(s.routine, nullptr, 16)), s.rotation);
        return;
    }
    if (s.kind == 14) {  // another machine rolled for a dead NPC's return (respawn.cpp)
        respawn::OnState(from, s);
        return;
    }
    if (s.kind == 16) {  // a client asks for an NPC with an important info for its hero (address.cpp)
        address::OnState(from, s);
        return;
    }
    if (s.kind == DialogXp) {  // another player's dialog gave experience: the same reward for this player's hero
        eCEntity* hero = gCSession::GetInstance().GetHero();
        const int amount = std::atoi(s.routine);
        // The game's dialogs give 10 to 2000 (Infos.pak); far more is no dialog of this game (Fable's review 22.10. 1.9).
        if (!hero || amount <= 0 || amount > 5000 || !gCSession::GetInstance().IsGameRunning()) return;
        Entity self(hero);
        const int before = static_cast<int>(self.PlayerMemory.XP);
        // As the dialog command does: no victim, the hero earns it; the game turns the amount into experience.
        GetScriptAdminExt().CallScript(bCString("GiveXP"), nullptr, hero, amount);
        ++xpApplied;
        Logf("infocmd: dialog experience %d from player %u: xp %d -> %d", amount, from, before, static_cast<int>(self.PlayerMemory.XP));
        return;
    }
    if (s.kind == 4 || s.kind == 5) {  // a client took over (4) or gave back (5) a companion
        if (net::IsHost() && npcs::OnClaim(s.guid, from, s.kind == 4) && s.kind == 5 && s.moved) npcs::PlaceGivenBack(s.guid, s.position, s.rotation);
        return;
    }
    if (s.kind == 7) {  // host: this NPC from its save is the companion of player `guard`
        if (!net::IsHost() && s.guard == net::LocalId()) npcs::Adopt(s.guid);
        return;
    }
    if (s.kind == 8 || s.kind == 9) {  // to player `guard` from player `npcClass`: helped you up (8) / has no potion (9)
        if (s.guard == net::LocalId()) {
            if (s.kind == 8) downed::Revived(s.npcClass);
            else downed::HelperHasNoPotion(s.npcClass);
        }
        return;
    }
    if (s.kind == 6) {  // host: the claim of player `guard` was refused or ended
        if (!net::IsHost() && s.guard == net::LocalId()) npcs::Revoked(s.guid);
        return;
    }
    if (s.kind == 3) {
        if (!net::IsHost()) return;
        const uint8_t player = s.guard ? s.guard : from;
        if (player != from || player >= net::MaxPlayers) return;  // a client starts fights against its own puppet only (Fable 1.3)
        Fight f{{}, player, s.routine, net::Now() + 15000, net::JoinCount()};
        std::memcpy(f.attacker, s.guid, 16);
        fights.push_back(f);
        return;
    }
    if (s.kind == 2) {
        world.SetSectorStatus(bCString(s.routine), s.enclave != 0);  // routine holds the sector name, enclave its status
        ++sectorsApplied;
        Logf("infocmd: sector %s %s", s.routine, s.enclave ? "on" : "off");
        return;
    }
    _GUID raw;
    std::memcpy(&raw, s.guid, 16);
    Entity e = Entity(bCPropertyID(bCGuid(raw)));
    eCEntity* instance = e.GetInstance();
    if (!instance) return;
    if (s.dialog & 0x80 && instance->GetPropertySet(eEPropertySetType_Dialog)) {
        e.Dialog.TeachEnabled = (s.dialog & 1) != 0;
        e.Dialog.TradeEnabled = (s.dialog & 2) != 0;
        e.Dialog.PartyEnabled = (s.dialog & 4) != 0;
        e.Dialog.MobEnabled = (s.dialog & 8) != 0;
        e.Dialog.SlaveryEnabled = (s.dialog & 16) != 0;
    }
    if (s.npc && instance->GetPropertySet(eEPropertySetType_NPC)) {
        e.NPC.GuardStatus = static_cast<gEGuardStatus>(s.guard);
        e.NPC.Class = static_cast<gEClass>(s.npcClass);
        e.NPC.PoliticalAlignment = static_cast<gEPoliticalAlignment>(s.alignment);
        e.NPC.Immortal = s.immortal != 0;
    }
    auto* enclave = static_cast<gCEnclave_PS*>(instance->GetPropertySet(eEPropertySetType_Enclave));
    if (s.enclave && enclave) {
        enclave->AccessRaid() = (s.enclave & 2) != 0;
        enclave->AccessRevolution() = (s.enclave & 4) != 0;
        enclave->AccessStatus() = static_cast<gEEnclaveStatus>(s.enclaveStatus);
    }
    const bool mirrored = npcs::IsMirrored(instance);  // its body follows the host's snapshots anyway
    // Positions only after a MoveTo command: an NPC's position belongs to whoever simulates it. Taking every
    // snapshot's position let a client 60 m away (with a stale copy) put the host's walking NPCs back to old
    // places (soak test; the host crashed in its navigation code 25 s later, cause not proven).
    if (auto* nav = static_cast<gCNavigation_PS*>(instance->GetPropertySet(eEPropertySetType_Navigation))) {
        if (*s.routine && std::strcmp(nav->AccessRoutine().GetText(), s.routine) != 0) {
            e.Navigation.SetRoutine(bCString(s.routine));
            if (!mirrored) { e.Routine.FullStop(); e.Routine.ContinueRoutine(); }
        }
    }
    const bCVector& p = instance->GetWorldPosition();
    const float here[3] = {p.GetX(), p.GetY(), p.GetZ()};
    if (s.moved && !mirrored && body::Distance(here, s.position) > 200.0f) e.MoveTo(body::Pose(*instance, s.position, s.rotation));
    ++snapshotsApplied;
    Logf("infocmd: applied the state of %s (routine %s)", e.GetName().GetText(), s.routine);
}

void OnWorldState(uint8_t from, net::WorldState const& s)
{
    if (net::IsHost() && s.kind != 3 && s.kind != 16) net::SendWorldState(s, from);  // pass it on to everyone else
    Apply(s, from);
}

void Send(net::WorldState const& s)
{
    net::SendWorldState(s, 0);
}

// A companion joins (true) or leaves (false) this client. Joining: the host's NPC is taken over here (the follow
// state follows the local hero; follow false: the caller sets its task, a guide of P9). Leaving: handed back to the
// host. False when nothing changes hands.
bool Join(eCEntity* target, bool join, bool follow = true)
{
    if (join && npcs::IsMirrored(target)) {
        npcs::Claim(target);
        net::WorldState s{};
        s.kind = 4;
        GuidOf(target, s.guid);
        Send(s);
        if (follow) {
            Pending p{{}, {}, net::Now() + ReadAfter};
            GuidOf(target, p.guid);
            p.follow = true;
            pending.push_back(p);
        }
        return true;
    }
    if (!join && npcs::Claimed(target)) {
        npcs::Unclaim(target);
        net::WorldState s{};
        s.kind = 5;
        GuidOf(target, s.guid);
        if (givebackPlace) {  // where it stands here: the host's copy far away stayed where the host last saw it (escortfardiag)
            const bCVector& p = target->GetWorldPosition();
            s.position[0] = p.GetX(); s.position[1] = p.GetY(); s.position[2] = p.GetZ();
            const bCQuaternion q = target->GetWorldRotation();
            s.rotation[0] = q.GetX(); s.rotation[1] = q.GetY(); s.rotation[2] = q.GetZ(); s.rotation[3] = q.GetW();
            s.moved = 1;
        }
        Send(s);
        return true;
    }
    return false;
}
}

void Start() { net::SetWorldStateHandler(&OnWorldState); }

void Observe(gCScriptProcessingUnit* spu)
{
    ++observed;
    eCEntity* self = spu ? spu->GetSelfEntity() : nullptr;
    // The info manager lives on the world entity (World_MCP runs the dialog script); the unit's entity first.
    Entity owner(self);
    PSInfoScriptCommand command;
    if (owner.GetInstance() && owner.GetInstance()->GetPropertySet(eEPropertySetType_InfoManager)) command = owner.InfoManager.GetCurrentCommand();
    if (!command.IsValid()) command = Entity::GetWorldEntity().InfoManager.GetCurrentCommand();
    if (!command.IsValid()) return;
    const Entity e1 = command.GetEntity1(), e2 = command.GetEntity2();
    char key[400];
    sprintf_s(key, "%s|%s|%s|%s|%s", command.GetCommand().GetText(), e1.GetInstance() ? e1.GetName().GetText() : "-",
              e2.GetInstance() ? e2.GetName().GetText() : "-", command.GetID1().GetText(), command.GetID2().GetText());
    if (lastKey == key) return;
    lastKey = key;
    lastText = key;
    ++distinct;
    Logf("infocmd: %s (self=%s)", key, self ? Entity(self).GetName().GetText() : "-");
    // The game's joins (info conditions 14, 28, 29) have no JoinPlayer line: the player's word marks them (P9).
    if (!std::strcmp(command.GetCommand().GetText(), "SaySVM") && e2.GetInstance() &&
        (!_stricmp(command.GetID1().GetText(), "CmdPartyJoin") || !_stricmp(command.GetID1().GetText(), "CmdMobJoin") ||
         !_stricmp(command.GetID1().GetText(), "CmdSlaveJoin")))
        guide::Joined(e2.GetInstance());
    if (!sharing || !net::Connected()) return;
    char const* name = command.GetCommand().GetText();
    if (!std::strcmp(name, "SetSectorStatus")) {
        pending.push_back({{}, command.GetID1().GetText(), net::Now() + ReadAfter});
        return;
    }
    if (!std::strcmp(name, "GiveXP")) {
        if (!sharingXp || std::atoi(command.GetID1().GetText()) <= 0) return;
        net::WorldState s{};
        s.kind = DialogXp;
        strncpy_s(s.routine, command.GetID1().GetText(), _TRUNCATE);
        Send(s);
        ++xpSent;
        Logf("infocmd: dialog experience %s passed on to the other players", s.routine);
        return;
    }
    eCEntity* target = e1.GetInstance();
    if (!std::strcmp(name, "Attack") && !net::IsHost() && target && npcs::IsMirrored(target) &&
        e2.GetInstance() == gCSession::GetInstance().GetHero()) {
        // The host's NPC: here it is driven and its attack would not run; the host starts it against our puppet.
        net::WorldState s{};
        s.kind = 3;
        GuidOf(target, s.guid);
        s.guard = net::LocalId();
        strncpy_s(s.routine, command.GetID1().GetText(), _TRUNCATE);
        Send(s);
        ++fightsSent;
        Logf("infocmd: dialog fight with %s (%s) sent to the host", e1.GetName().GetText(), s.routine);
        return;
    }
    if (!std::strcmp(name, "JoinPlayer") && !net::IsHost() && target) {
        const bool join = !_stricmp(command.GetID1().GetText(), "true") || !std::strcmp(command.GetID1().GetText(), "1");
        if (Join(target, join)) return;
    }
    if (!IsStateCommand(name) || !target || target == gCSession::GetInstance().GetHero() || remote::IsPuppet(target)) return;
    Pending p{{}, {}, net::Now() + ReadAfter, std::strcmp(name, "MoveTo") == 0};
    GuidOf(target, p.guid);
    pending.push_back(p);
}

void Changed(eCEntity* entity)
{
    if (!entity || !sharing) return;
    Pending p{{}, {}, net::Now() + ReadAfter};
    GuidOf(entity, p.guid);
    pending.push_back(p);
}

void Tick()
{
    if (!fights.empty()) StartFights();
    if (pending.empty()) return;
    const uint32_t now = net::Now();
    for (auto it = pending.begin(); it != pending.end();) {
        if (static_cast<int32_t>(now - it->due) < 0) { ++it; continue; }
        net::WorldState s{};
        if (!it->sector.empty()) {
            s.kind = 2;
            strncpy_s(s.routine, it->sector.c_str(), _TRUNCATE);
            s.enclave = world.GetSectorStatus(bCString(it->sector.c_str())) ? 1 : 0;
            ++sectorsSent;
            Send(s);
        } else {
            _GUID raw;
            std::memcpy(&raw, it->guid, 16);
            Entity e = Entity(bCPropertyID(bCGuid(raw)));
            if (e.GetInstance() && it->follow) {
                // Released from the mirror its routine came back; a companion follows this player.
                e.Routine.FullStop();
                e.Routine.SetTask(bCString("ZS_FollowPlayer"));
                Logf("infocmd: %s follows this player", e.GetName().GetText());
            } else if (e.GetInstance()) {
                Snapshot(e, s);
                s.moved = it->moved ? 1 : 0;
                ++snapshotsSent;
                Send(s);
                Logf("infocmd: sent the state of %s after a dialog command", e.GetName().GetText());
            }
        }
        it = pending.erase(it);
    }
}

void Off(LogFn log)
{
    sharing = false;
    log("INFOCMDOFF ok=1");
}

void GivebackPlaceOff(LogFn log)
{
    givebackPlace = false;
    log("GIVEBACKPOSOFF ok=1");
}

void DialogXpOff(LogFn log)
{
    sharingXp = false;
    log("DIALOGXPOFF ok=1");
}

void Report(LogFn log)
{
    char line[600];
    sprintf_s(line, "INFOCMD xp_sent=%u xp_applied=%u observed=%u distinct=%u sent=%u applied=%u sectors_sent=%u sectors_applied=%u fights_sent=%u fights_started=%u last=%s", xpSent, xpApplied, observed, distinct,
              snapshotsSent, snapshotsApplied, sectorsSent, sectorsApplied, fightsSent, fightsStarted, lastText.c_str());
    log(line);
}
}

namespace g3t::infocmd {
// QA, stage E3: "join Name_1" / "join Name_0" on a client, the same path as the dialog command JoinPlayer.
bool TakeOver(eCEntity* target, bool join, bool follow) { return Join(target, join, follow); }

void JoinQa(char const* argument, LogFn log)
{
    char line[200];
    std::string arg = argument ? argument : "";
    const size_t cut = arg.rfind('_');
    if (cut == std::string::npos || net::IsHost()) { log("REFUSED join: use Name_1 or Name_0 on a client"); return; }
    Entity npc = Entity::GetEntity(bCString(arg.substr(0, cut).c_str()));
    const bool join = arg.substr(cut + 1) == "1";
    const bool changed = npc.GetInstance() && Join(npc.GetInstance(), join);
    sprintf_s(line, "JOIN name=%s join=%d found=%d changed=%d", arg.substr(0, cut).c_str(), join ? 1 : 0, npc.GetInstance() ? 1 : 0, changed ? 1 : 0);
    log(line);
}
}

namespace g3t::infocmd {
void OnWorldChange()
{
    fights.clear();
    pending.clear();
}
}
