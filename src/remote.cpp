// SPDX-License-Identifier: GPL-3.0-or-later
#include "remote.h"

#include "ai.h"
#include "body.h"
#include "config.h"
#include "log.h"
#include "net.h"
#include "npcs.h"
#include "parity.h"

#include <Game.h>
#include <Script.h>
#include <Windows.h>
#include <cmath>
#include <cstdio>
#include <string>

namespace g3t::remote {
namespace {
struct Puppet
{
    Entity entity;
    bool spawned = false;
    std::string heldRight, heldLeft;  // what the puppet holds (item names)
    uint32_t holdChecked = 0;
    uint32_t world = 0;
    body::State body;
    std::string shape;        // the shape it stands for (the player's hero template, or its creature)
};
Puppet puppets[net::MaxPlayers];
bool started = false;
bool trace = false;  // test copies only: 5 Hz trace of hero and puppet motion
uint32_t lastTrace = 0;
bool templateWarned = false;
bool haveAnchor = false;
bCVector anchor;

void LogMotions(char const* who, Entity& entity, LogFn log)
{
    eCEntity* engine = entity.GetInstance();
    eCVisualAnimation_PS* animation = engine ? static_cast<eCVisualAnimation_PS*>(engine->GetPropertySet(eEPropertySetType_Animation)) : nullptr;
    char line[900];
    int length = sprintf_s(line, "MOTIONS who=%s", who);
    for (int type = 0; animation && type < 6 && length > 0; ++type) {
        char name[128] = {};
        const bool ok = body::MotionName(animation, type, name, sizeof(name));
        length += sprintf_s(line + length, sizeof(line) - length, " %d=%s", type, *name ? name : "-");
        if (!ok) break;
    }
    log(line);
}

bool LocalState(net::PlayerState& state)
{
    gCSession& session = gCSession::GetInstance();
    eCDynamicEntity* own = session.GetHero();
    if (!own || !session.IsGameRunning()) return false;
    // Transformed, the player controls a creature and the hero's own body stays hidden where the spell was cast
    // (transformdiag 19.10.: the other machine showed a human standing still while the wolf ran about).
    eCEntity* hero = ControlledBody();
    if (hero != own) strncpy_s(state.shape, Entity(hero).GetName().GetText(), _TRUNCATE);
    const bCVector& p = hero->GetWorldPosition();
    const bCQuaternion q = hero->GetWorldRotation();
    state.world = CurrentWorld();
    state.position[0] = p.GetX();
    state.position[1] = p.GetY();
    state.position[2] = p.GetZ();
    state.rotation[0] = q.GetX();
    state.rotation[1] = q.GetY();
    state.rotation[2] = q.GetZ();
    state.rotation[3] = q.GetW();
    state.flags = 1 | (Entity(own).IsDown() ? 2 : 0) | (Entity(own).IsDead() ? 4 : 0);  // bit 1: knocked out, bit 2: dead
    state.movementMode = static_cast<uint8_t>(Entity(hero).CharacterMovement.GetMovementMode());
    state.aniState = static_cast<uint8_t>(static_cast<int>(Entity(hero).Routine.AniState));
    body::CurrentMotion(hero, state.animation, sizeof(state.animation));
    Entity self(hero);
    const Entity right = self.Inventory.GetItemFromSlot(gESlot_RightHand), left = self.Inventory.GetItemFromSlot(gESlot_LeftHand);
    strncpy_s(state.heldRight, right.GetInstance() ? right.GetName().GetText() : "", _TRUNCATE);
    strncpy_s(state.heldLeft, left.GetInstance() ? left.GetName().GetText() : "", _TRUNCATE);
    return true;
}

// NPCs that fight a puppet hold it as target, attacker or spell target, and their AI states read it every frame: a
// client crashed (access violation under an attack state, stage E2) when the host left mid-fight and its puppet was
// destroyed. Every NPC lets go of the puppet first; the ones that were after it start their routine again.
void Forget(eCEntity* entity)
{
    if (!entity) return;
    const bTObjArray<Entity> all = Entity::GetNPCs();
    int released = 0;
    for (int i = 0; i < all.GetCount(); ++i) {
        Entity npc = all.GetAt(i);
        eCEntity* instance = npc.GetInstance();
        if (!instance || instance == entity || !instance->GetPropertySet(eEPropertySetType_NPC)) continue;
        bool after = false;
        if (npc.NPC.GetCurrentTarget().GetInstance() == entity) { npc.NPC.SetCurrentTarget(Entity()); after = true; }
        if (npc.NPC.GetAlternativeTarget().GetInstance() == entity) npc.NPC.SetAlternativeTarget(Entity());
        if (npc.NPC.GetCurrentAttacker().GetInstance() == entity) { npc.NPC.SetCurrentAttacker(Entity()); after = true; }
        if (npc.NPC.GetLastAttacker().GetInstance() == entity) npc.NPC.SetLastAttacker(Entity());
        if (npc.NPC.GetSpellTarget().GetInstance() == entity) { npc.NPC.SetSpellTarget(Entity()); after = true; }
        if (npc.DamageReceiver.GetLastInflictor().GetInstance() == entity) { Entity none; npc.DamageReceiver.SetLastInflictor(none); }
        if (after && !npcs::IsMirrored(instance) && !npc.IsDead()) {
            npc.Routine.FullStop();
            npc.Routine.ContinueRoutine();
        }
        if (after) ++released;
    }
    if (released) Logf("remote: %d NPCs let go of a removed puppet", released);
}

// The puppet used to join the local hero's party so enemies would attack it; that had no effect on GetAttitude
// (re/findings/target-selection.md) and put the puppet into the hero's follower list. Enemies now judge puppets
// through the GetAttitude wrapper (ai.cpp). Kept switchable for comparison only.
bool puppetParty = false;  // QA: PuppetPartyOff (now the default)
bool puppetHold = true;   // QA negative control: PuppetHoldOff
bool puppetCombat = true; // QA negative control: PuppetCombatOff

// The stack of an item (by name) in the puppet's inventory; created if missing (a puppet's inventory holds only
// what its template gave it).
GEInt StackFor(Entity& body, char const* name)
{
    if (!name || !*name) return -1;
    for (int pass = 0; pass < 2; ++pass) {
        const GEInt count = body.Inventory.GetStackCount();
        for (GEInt i = 0; i < count; ++i)
            if (Entity item = body.Inventory.GetTemplateItem(i); item.GetInstance() && !std::strcmp(item.GetName().GetText(), name)) return i;
        if (pass == 0) body.Inventory.CreateItems(Template(name), 0, 1);
    }
    return -1;
}

// The player's weapons in the puppet's hands: the host saw the client's character unarmed (first human look).
// What the puppet really holds is checked every 250 ms (stopping its AI puts weapons away).
template <typename P>
void Hold(P& puppet, char const* right, char const* left, uint32_t now)
{
    if (!puppetHold || static_cast<int32_t>(now - puppet.holdChecked) < 250) return;
    puppet.holdChecked = now;
    const Entity inRight = puppet.entity.Inventory.GetItemFromSlot(gESlot_RightHand), inLeft = puppet.entity.Inventory.GetItemFromSlot(gESlot_LeftHand);
    const std::string haveRight = inRight.GetInstance() ? inRight.GetName().GetText() : "";
    const std::string haveLeft = inLeft.GetInstance() ? inLeft.GetName().GetText() : "";
    if (haveRight == right && haveLeft == left) return;
    puppet.entity.Inventory.HoldStacks(StackFor(puppet.entity, left), StackFor(puppet.entity, right));
}

void Despawn(int id, char const* reason)
{
    Puppet& puppet = puppets[id];
    if (!puppet.spawned) return;
    eCEntity* entity = puppet.entity.GetInstance();
    gCWorld* current = gCWorld::GetCurrentWorld();
    if (entity && current && puppet.world == CurrentWorld()) {
        // Out of the local hero's party first, or the party keeps a member that no longer exists.
        if (puppet.entity.Party.GetPartyLeader().GetInstance()) {
            puppet.entity.Party.SetPartyLeader(Entity());
            puppet.entity.Party.PartyMemberType = gEPartyMemberType_None;
        }
        Forget(entity);
    }
    if (entity && current && puppet.world == CurrentWorld())
        current->DestroyDynamicEntity(static_cast<eCDynamicEntity*>(entity));
    puppet = Puppet{};
    Logf("remote: puppet %d removed (%s)", id, reason);
}

}

void DespawnAll(char const* reason)
{
    for (int id = 1; id < net::MaxPlayers; ++id) Despawn(id, reason);
}

namespace {
void UpdatePuppet(int id, net::Remote const& remote, eCEntity const& hero)
{
    Puppet& puppet = puppets[id];
    // A client's loop stops during videos and loading (the intro once removed and respawned a puppet after 5 s),
    // so only a long silence removes the puppet. Stamps come from the network clock; GetTickCount() runs a few ms apart and made fresh states look
    // ancient after unsigned underflow (the puppet was removed and respawned hundreds of times).
    const uint32_t now = net::Now();
    const bool fresh = remote.connected && remote.latestTicks && static_cast<int32_t>(now - remote.latestTicks) < 15000;
    const bool sameWorld = (remote.latest.flags & 1) && remote.latest.world == CurrentWorld();
    if (!fresh || !sameWorld) {
        Despawn(id, !fresh ? "no recent state" : "other world");
        return;
    }
    // A transformed player is shown as its creature: a human body played the wolf's motions (or none) before.
    char const* requested = *remote.latest.shape ? remote.latest.shape : GetConfig().puppetTemplate;
    if (puppet.spawned && puppet.shape != requested) {
        const std::string from = puppet.shape;
        Despawn(id, "changed shape");
        Logf("remote: player %d changes shape: %s -> %s", id, from.c_str(), requested);
    }
    if (!puppet.spawned) {
        char const* wanted = requested;
        Template shape(wanted);
        if (!shape.IsValid() && *remote.latest.shape) {  // a creature unknown here: show the hero rather than nothing
            Logf("remote: no template %s for player %d's shape; showing its hero", wanted, id);
            wanted = GetConfig().puppetTemplate;
            shape = Template(wanted);
        }
        if (!shape.IsValid()) {
            if (!templateWarned) Logf("remote: puppet template %s not found", GetConfig().puppetTemplate);
            templateWarned = true;
            return;
        }
        puppet.entity = Entity::Spawn(shape, body::Pose(hero, remote.latest.position, remote.latest.rotation));
        if (!puppet.entity.GetInstance()) { Log("remote: spawn failed"); return; }
        // Collision stays on: with it off (stage 2, when puppets were snapped every frame) no weapon could touch a
        // puppet, so the host's NPCs attacked players without ever hitting them (aggrodiag: 0 hits in 20 s of
        // Topork's power attacks on a puppet).
        body::Pacify(puppet.entity, puppet.body);
        // A puppet must never die: its template may carry a starting inventory that would drop as loot.
        if (gCNPC_PS* npc = static_cast<gCNPC_PS*>(puppet.entity.GetInstance()->GetPropertySet(eEPropertySetType_NPC)))
            npc->AccessImmortal() = GETrue;
        // Enemies chose only this machine's hero, never a puppet (first human test: the host's orcs attacked the host 39
        // times, the client's puppet 0 times). Experiment: the puppet joins the local hero's party, as companions do,
        // whom the player's enemies attack too.
        if (puppetParty) {
            puppet.entity.Party.SetPartyLeader(Entity(&hero));
            puppet.entity.Party.PartyMemberType = gEPartyMemberType_Party;
            puppet.entity.Party.Waiting = GEFalse;
        }
        puppet.spawned = true;
        puppet.world = CurrentWorld();
        puppet.shape = requested;
        Logf("remote: puppet %d spawned for %s (%s)", id, remote.latest.name, wanted);
    }
    if (!puppet.entity.GetInstance()) { Despawn(id, "body gone"); return; }
    const uint32_t interval = remote.latestTicks - remote.previousTicks;
    const float speed = interval ? body::Distance(remote.latest.position, remote.previous.position) * 1000.0f / interval : 0.0f;
    const body::Target target{remote.latest.position, remote.latest.rotation, remote.latest.movementMode, remote.latest.animation, speed,
                              remote.latest.aniState};
    if (!*remote.latest.shape) Hold(puppet, remote.latest.heldRight, remote.latest.heldLeft, now);  // a creature holds nothing
    // The player's swings replay as the game's combat moves too (owner's look: players' attacks hardly visible);
    // a body whose move faults goes back to motion names (body.cpp).
    puppet.body.combatMoves = puppetCombat;
    body::Drive(puppet.entity, puppet.body, target, now);
}
}

unsigned CurrentWorld()
{
    gCWorld* current = gCWorld::GetCurrentWorld();
    return current ? net::HashWorld(current->GetFileBaseName().GetText()) : 0;
}

bool followShape = true;  // QA negative control: shapeoff (a transformed player is sent as its hidden hero again)

eCEntity* ControlledBody()
{
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    eCEntity* player = Entity::GetPlayer().GetInstance();
    return followShape && hero && player && player != hero ? player : hero;
}

void ShapeOff(LogFn log)
{
    followShape = false;
    log("SHAPEOFF transformed players are sent as their hero");
}

bool IsPuppet(eCEntity const* entity)
{
    for (auto& puppet : puppets)
        if (puppet.spawned && puppet.entity.GetInstance() == entity) return true;
    return false;
}

int PuppetOwner(eCEntity const* entity)
{
    for (int id = 1; id < net::MaxPlayers; ++id)
        if (entity && puppets[id].spawned && puppets[id].entity.GetInstance() == entity) return id;
    return 0;
}

eCEntity* PuppetOf(int playerId)
{
    if (playerId <= 0 || playerId >= net::MaxPlayers || !puppets[playerId].spawned) return nullptr;
    return puppets[playerId].entity.GetInstance();
}

bool TraceEnabled() { return trace; }

void Start()
{
    char flag[MAX_PATH] = {};
    HMODULE self = nullptr;
    if (GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                           reinterpret_cast<LPCSTR>(&Start), &self) && GetModuleFileNameA(self, flag, MAX_PATH)) {
        for (int part = 0; part != 2; ++part) { char* sep = strrchr(flag, '\\'); if (sep) *sep = '\0'; }
        strcat_s(flag, "\\qa-enable.txt");
        trace = GetFileAttributesA(flag) != INVALID_FILE_ATTRIBUTES;
    }
    Config const& config = GetConfig();
    parity::Compute();
    started = net::Start(config.host, config.address, config.port, config.name);
}

void OnWorldDeactivate()
{
    for (auto& puppet : puppets) puppet = Puppet{};
    haveAnchor = false;
}

void Tick()
{
    if (!started) return;
    net::PlayerState local;
    const bool haveLocal = LocalState(local);
    net::Tick(local, haveLocal);
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    net::Remote const* remotes = net::Remotes();
    for (int id = 1; id < net::MaxPlayers; ++id) {
        if (id == net::LocalId()) continue;
        if (haveLocal && hero) UpdatePuppet(id, remotes[id], *hero);
        else if (puppets[id].spawned) Despawn(id, "local game not running");
    }
    const uint32_t now = GetTickCount();
    if (!trace || now - lastTrace < 200) return;
    lastTrace = now;
    if (haveLocal) {
        // Diagnostic (soak crash): gCNavigationMap::DetectEvades takes an entity from the walker's navigation
        // (+0xac, else +200) and asks it for its nav zone without a null check; which entities are these?
        char zone[64] = "-", alt[64] = "-";
        eCDynamicEntity* self = gCSession::GetInstance().GetHero();
        if (auto* nav = self ? reinterpret_cast<unsigned char*>(self->GetPropertySet(eEPropertySetType_Navigation)) : nullptr) {
            if (eCEntity* z = reinterpret_cast<eCEntityProxy*>(nav + 0xac)->GetEntity()) strncpy_s(zone, z->GetName().GetText(), _TRUNCATE);
            if (eCEntity* z = reinterpret_cast<eCEntityProxy*>(nav + 200)->GetEntity()) strncpy_s(alt, z->GetName().GetText(), _TRUNCATE);
        }
        Logf("TRACE hero pos=%.0f,%.0f,%.0f motion=%s navzone=%s navalt=%s", local.position[0], local.position[1], local.position[2],
             local.animation, zone, alt);
    }
    for (int id = 1; id < net::MaxPlayers; ++id) {
        Puppet& puppet = puppets[id];
        eCEntity* instance = puppet.spawned ? puppet.entity.GetInstance() : nullptr;
        if (!instance) continue;
        char motion[128] = {};
        body::CurrentMotion(instance, motion, sizeof(motion));
        const bCVector& at = instance->GetWorldPosition();
        const float current[3] = {at.GetX(), at.GetY(), at.GetZ()};
        Logf("TRACE puppet%d pos=%.0f,%.0f,%.0f error=%.0f walking=%d mode=%d/%d pacified=%u task=%s state=%s motion=%s remote_motion=%s", id,
             current[0], current[1], current[2], body::Distance(current, remotes[id].latest.position), puppet.body.walking,
             static_cast<int>(puppet.entity.CharacterMovement.GetMovementMode()), remotes[id].latest.movementMode,
             puppet.body.repacified, puppet.entity.Routine.GetCurrentTask().GetText(),
             puppet.entity.Routine.GetCurrentState().GetText(), motion, remotes[id].latest.animation);
    }
}

void Report(LogFn log)
{
    char line[400];
    sprintf_s(line, "NET local_id=%u connected=%d world=%08x perception_filter=%d blocked=%u wrapped=%u skipped=%u unknown=%u", net::LocalId(),
              net::Connected(), CurrentWorld(), ai::PerceptionFiltered(), ai::BlockedCount(), ai::WrappedCount(), ai::SkippedCount(),
              ai::UnknownCount());
    log(line);
    net::Remote const* remotes = net::Remotes();
    const uint32_t now = net::Now();
    for (int id = 1; id < net::MaxPlayers; ++id) {
        net::Remote const& r = remotes[id];
        if (id == net::LocalId() || !r.connected) continue;
        float puppetPos[3] = {};
        eCEntity* entity = puppets[id].spawned ? puppets[id].entity.GetInstance() : nullptr;
        if (entity) {
            const bCVector& p = entity->GetWorldPosition();
            puppetPos[0] = p.GetX(); puppetPos[1] = p.GetY(); puppetPos[2] = p.GetZ();
        }
        sprintf_s(line, "REMOTE id=%d name=%s world=%08x same_world=%d age_ms=%u pos=%.1f,%.1f,%.1f puppet=%d puppet_pos=%.1f,%.1f,%.1f",
                  id, r.latest.name, r.latest.world, r.latest.world == CurrentWorld(), r.latestTicks ? now - r.latestTicks : 0,
                  r.latest.position[0], r.latest.position[1], r.latest.position[2], entity != nullptr,
                  puppetPos[0], puppetPos[1], puppetPos[2]);
        log(line);
        sprintf_s(line, "ANIM id=%d remote=%s requested=%s", id, *r.latest.animation ? r.latest.animation : "-",
                  *puppets[id].body.playing ? puppets[id].body.playing : "-");
        log(line);
        {
            // What the puppet really shows: the motion on slot 0 and whether it runs (nothing running = bind pose).
            char shows[128] = "-";
            int running = -1;
            if (entity) {
                body::CurrentMotion(entity, shows, sizeof(shows));
                if (auto* animation = static_cast<eCVisualAnimation_PS*>(entity->GetPropertySet(eEPropertySetType_Animation)))
                    if (eCWrapper_emfx2Actor* actor = animation->GetActor())
                        running = actor->IsMotionRunning(static_cast<eCWrapper_emfx2Actor::eEMotionType>(0)) ? 1 : 0;
            }
            sprintf_s(line, "PUPPETPOSE id=%d remote=%s shows=%s run0=%d shape=%s body=%s", id, *r.latest.animation ? r.latest.animation : "-",
                      *shows ? shows : "-", running, *r.latest.shape ? r.latest.shape : "-",
                      puppets[id].spawned && puppets[id].entity.GetInstance() ? puppets[id].entity.GetName().GetText() : "-");
            log(line);
        }
        {
            Entity body(entity);
            const Entity held = entity ? body.Inventory.GetItemFromSlot(gESlot_RightHand) : Entity();
            sprintf_s(line, "HELD id=%d remote=%s puppet=%s", id, *r.latest.heldRight ? r.latest.heldRight : "-",
                      held.GetInstance() ? held.GetName().GetText() : "-");
            log(line);
        }
        if (entity) {
            char who[16];
            sprintf_s(who, "puppet%d", id);
            LogMotions(who, puppets[id].entity, log);
        }
    }
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    if (hero) {
        // A puppet may use the hero's own template (PC_Hero). The game must still find the real player.
        sprintf_s(line, "PLAYER_CHECK getplayer_is_hero=%d by_name_is_hero=%d",
                  Entity::GetPlayer().GetInstance() == hero, Entity::GetEntity(bCString("PC_Hero")).GetInstance() == hero);
        log(line);
        Entity self(hero);
        LogMotions("hero", self, log);
    }
}

void WalkTo(int index, LogFn log)
{
    char line[200];
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    if (!hero || !gCSession::GetInstance().IsGameRunning()) { log("REFUSED walkto: no hero"); return; }
    if (!haveAnchor) { anchor = hero->GetWorldPosition(); haveAnchor = true; }
    const float angle = index * 0.785398f;
    const bCVector target(anchor.GetX() + 1500.0f * std::cos(angle), anchor.GetY(), anchor.GetZ() + 1500.0f * std::sin(angle));
    const GEBool ok = Entity(hero).StartGoto(target, gEWalkMode_Run);
    sprintf_s(line, "WALKTO index=%d started=%d target=%.1f,%.1f,%.1f", index, ok, target.GetX(), target.GetY(), target.GetZ());
    log(line);
}

void Step(int index, LogFn log)
{
    char line[200];
    eCEntity* hero = gCSession::GetInstance().GetHero() ? ControlledBody() : nullptr;  // transformed: the creature
    if (!hero || !gCSession::GetInstance().IsGameRunning()) { log("REFUSED step: no hero"); return; }
    if (!haveAnchor) { anchor = hero->GetWorldPosition(); haveAnchor = true; }
    const float angle = index * 0.785398f;  // 45 degrees per step
    const float position[3] = {anchor.GetX() + 300.0f * std::cos(angle), anchor.GetY(), anchor.GetZ() + 300.0f * std::sin(angle)};
    const bCQuaternion q = hero->GetWorldRotation();
    const float rotation[4] = {q.GetX(), q.GetY(), q.GetZ(), q.GetW()};
    Entity(hero).MoveTo(body::Pose(*hero, position, rotation));
    const bCVector& p = hero->GetWorldPosition();
    sprintf_s(line, "STEP index=%d target=%.1f,%.1f,%.1f pos=%.1f,%.1f,%.1f", index, position[0], position[1], position[2],
              p.GetX(), p.GetY(), p.GetZ());
    log(line);
}
}

namespace g3t::remote {
void PuppetPartyOff(LogFn log)
{
    puppetParty = false;
    for (int id = 1; id < net::MaxPlayers; ++id)
        if (puppets[id].spawned && puppets[id].entity.GetInstance()) {
            puppets[id].entity.Party.SetPartyLeader(Entity());
            puppets[id].entity.Party.PartyMemberType = gEPartyMemberType_None;
        }
    log("PUPPETPARTYOFF ok=1");
}
}

namespace g3t::remote {
void PuppetHoldOff(LogFn log)
{
    puppetHold = false;
    log("PUPPETHOLDOFF ok=1");
}

void PuppetCombatOff(LogFn log)
{
    puppetCombat = false;
    log("PUPPETCOMBATOFF ok=1");
}
}
