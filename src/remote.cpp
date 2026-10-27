// SPDX-License-Identifier: GPL-3.0-or-later
#include "remote.h"
#include "occupancy.h"

#include "ai.h"
#include "body.h"
#include "config.h"
#include "downed.h"
#include "log.h"
#include "net.h"
#include "npcs.h"
#include "parity.h"

#include <Game.h>
#include <Script.h>
#include <Windows.h>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <string>
#include <unordered_set>
#include <vector>

namespace g3t::remote {
namespace {
struct Puppet
{
    Entity entity;
    bool spawned = false;
    std::string heldRight, heldLeft;  // what the puppet holds (item names)
    uint32_t holdChecked = 0;
    uint32_t wearChecked = 0;
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
    int length = Format(line, "MOTIONS who=%s", who);
    for (int type = 0; animation && type < 6 && length > 0; ++type) {
        char name[128] = {};
        const bool ok = body::MotionName(animation, type, name, sizeof(name));
        length += Format(line + length, sizeof(line) - length, " %d=%s", type, *name ? name : "-");
        if (!ok) break;
    }
    log(line);
}

bool badStates = false;  // QA (stage X1): the next states carry a NaN position and strings without terminator

bool LocalState(net::PlayerState& state)
{
    gCSession& session = gCSession::GetInstance();
    eCDynamicEntity* own = session.GetHero();
    if (!own || !session.IsGameRunning()) return false;
    if (badStates) {
        state.position[0] = std::nanf("");
        std::memset(state.name, 'X', sizeof(state.name));
        std::memset(state.shape, 'Y', sizeof(state.shape));
        state.flags = 1;
        return true;
    }
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
    // bit 1: knocked out, bit 2: dead, bit 3: waiting for help (an intercepted death, downed.cpp), bit 4: knocked out by
    // the game's own rules, with the gold a robber would find (ransack.cpp)
    const bool robbable = downed::NativeDown();
    state.flags = 1 | (Entity(own).IsDown() ? 2 : 0) | (Entity(own).IsDead() ? 4 : 0) | (downed::Held() ? 8 : 0) | (robbable ? 16 : 0);
    state.gold = robbable ? Entity(own).Inventory.GetItemAmount(Template("It_Gold")) : 0;
    state.movementMode = static_cast<uint8_t>(Entity(hero).CharacterMovement.GetMovementMode());
    state.aniState = static_cast<uint8_t>(static_cast<int>(Entity(hero).Routine.AniState));
    body::CurrentMotion(hero, state.animation, sizeof(state.animation));
    Entity self(hero);
    const Entity right = self.Inventory.GetItemFromSlot(gESlot_RightHand), left = self.Inventory.GetItemFromSlot(gESlot_LeftHand);
    strncpy_s(state.heldRight, right.GetInstance() ? right.GetName().GetText() : "", _TRUNCATE);
    strncpy_s(state.heldLeft, left.GetInstance() ? left.GetName().GetText() : "", _TRUNCATE);
    Entity mine(own);
    // A worn armor is a Body_* item in the body slot 17 (UseType 114); slot 9 stays empty (re/findings/armor-equip-c.md).
    const Entity armor = mine.Inventory.GetItemFromSlot(gESlot_Body), helmet = mine.Inventory.GetItemFromSlot(gESlot_Helmet);
    strncpy_s(state.armor, armor.GetInstance() ? armor.GetName().GetText() : "", _TRUNCATE);
    strncpy_s(state.helmet, helmet.GetInstance() ? helmet.GetName().GetText() : "", _TRUNCATE);
    strncpy_s(state.look, GetConfig().look, _TRUNCATE);
    return true;
}

// NPCs that dealt with each puppet (judged it in GetAttitude, were hit by it, hit it, attacked it after a dialog), by
// GUID, taken while the NPC was in hand. Forget clears the puppet out of exactly these, looked up again by GUID: its
// walk through Entity::GetNPCs() met a freed NPC still in that list, a per-frame copy of raw entity pointers (access
// violation while the host saved, run 20261022-080317; re/findings/save-forget-npclist-c.md).
std::unordered_set<std::string> contacts[net::MaxPlayers];
bool byContacts = true;  // QA negative control: forgetnpclist (the walk through Entity::GetNPCs, as before 22.10.)
bool noting = true;      // QA negative control: contactsoff (nothing is noted, so Forget lets nobody go)
unsigned contactsNoted = 0, contactsLookedUp = 0, contactsGone = 0;

std::string KeyOf(eCEntity* entity)
{
    const eCEntityProxy proxy(entity);
    return std::string(reinterpret_cast<char const*>(&proxy.GetEntityID().GetCore()), 16);
}

Entity ByKey(std::string const& key)
{
    _GUID raw;
    std::memcpy(&raw, key.data(), 16);
    return Entity(bCPropertyID(bCGuid(raw)));
}

// NPCs that fight a puppet hold it as target, attacker or spell target, and their AI states read it every frame: a
// client crashed (access violation under an attack state, stage E2) when the host left mid-fight and its puppet was
// destroyed. Every NPC that dealt with the puppet lets go of it first; the ones that were after it start their routine
// again.
void Forget(int id, eCEntity* entity)
{
    if (!entity) return;
    occupancy::Forget(id, entity);  // a stool or bench it sat on (the object's user)
    std::vector<Entity> candidates;
    if (byContacts) {
        for (auto const& key : contacts[id]) {
            ++contactsLookedUp;
            Entity npc = ByKey(key);
            if (npc.GetInstance()) candidates.push_back(npc);
            else ++contactsGone;
        }
    } else {
        const bTObjArray<Entity> all = Entity::GetNPCs();
        for (int i = 0; i < all.GetCount(); ++i) candidates.push_back(all.GetAt(i));
    }
    contacts[id].clear();
    int released = 0;
    for (Entity& npc : candidates) {
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
    Logf("remote: %d NPCs let go of removed puppet %d (%zu %s)", released, id, candidates.size(), byContacts ? "known contacts" : "from Entity::GetNPCs");
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

// The player's armor and helmet on the puppet (owner 23.10.: a new armor did not show on the other machine). Like the
// hands, checked every 250 ms; an empty slot takes off what the puppet's template wore.
constexpr gESlot WornSlots[2] = {gESlot_Body, gESlot_Helmet};
bool puppetWear = true;  // QA negative control: wearoff
bool remoteLooks = true;  // QA negative control: lookoff (every player shown as puppet_template, as before 23.10.)
unsigned wearChanges = 0, wearEquipped = 0, wearLinked = 0, wearFailed = 0;

void TakeOff(Entity& body, gESlot slot)
{
    for (GEInt i = 0; i < body.Inventory.GetStackCount(); ++i)
        if (body.Inventory.GetSlotFromStack(i) == slot) body.Inventory.UnEquipStack(i);
}

void Wear(Puppet& puppet, char const* armor, char const* helmet, uint32_t now)
{
    if (!puppetWear || static_cast<int32_t>(now - puppet.wearChecked) < 250) return;
    puppet.wearChecked = now;
    char const* const wanted[2] = {armor, helmet};
    for (int i = 0; i < 2; ++i) {
        const Entity worn = puppet.entity.Inventory.GetItemFromSlot(WornSlots[i]);
        const std::string have = worn.GetInstance() ? worn.GetName().GetText() : "";
        if (have == wanted[i]) continue;
        if (!have.empty()) TakeOff(puppet.entity, WornSlots[i]);
        const GEInt stack = StackFor(puppet.entity, wanted[i]);
        if (stack < 0) continue;
        ++wearChanges;
        // EquipStackToSlot left the puppet bare (23.10.: 25 tries, helmet slot still empty). Equip it as the game does for
        // the hero; where the puppet's template cannot equip it (requirements), link it to the slot: it is only shown.
        if (puppet.entity.Inventory.EquipStack(stack) && puppet.entity.Inventory.GetSlotFromStack(stack) == WornSlots[i]) {
            ++wearEquipped;
            continue;
        }
        auto* inventory = static_cast<gCInventory_PS*>(puppet.entity.GetInstance()->GetPropertySet(eEPropertySetType_Inventory));
        if (inventory && inventory->LinkStackToSlot(stack, WornSlots[i], GETrue)) ++wearLinked;
        else ++wearFailed;
    }
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
        Forget(id, entity);
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
    // A transformed player is shown as its creature: a human body played the wolf's motions (or none) before. Otherwise
    // as the look it chose in its INI (owner 21.10.: each player picks how the others see it), else as puppet_template.
    char const* requested = *remote.latest.shape ? remote.latest.shape
                          : remoteLooks && *remote.latest.look ? remote.latest.look : GetConfig().puppetTemplate;
    if (puppet.spawned && puppet.shape != requested) {
        const std::string from = puppet.shape;
        Despawn(id, "changed shape");
        Logf("remote: player %d changes shape: %s -> %s", id, from.c_str(), requested);
    }
    if (!puppet.spawned) {
        char const* wanted = requested;
        Template shape(wanted);
        if (!shape.IsValid() && std::strcmp(wanted, GetConfig().puppetTemplate) != 0) {  // unknown here: show the hero rather than nothing
            Logf("remote: no template %s for player %d; showing %s", wanted, id, GetConfig().puppetTemplate);
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
        puppet.body.puppet = true;  // QA (glide): counted apart from mirrored NPCs
        puppet.world = CurrentWorld();
        puppet.shape = requested;
        Logf("remote: puppet %d spawned for %s (%s)", id, remote.latest.name, wanted);
    }
    if (!puppet.entity.GetInstance()) { Despawn(id, "body gone"); return; }
    // A name of its own: under its template's name the dialogs of that NPC were offered on it, and one started by itself
    // (look = Diego, puppettalkdiag 23.10.); as PC_Hero a lookup by name could find it instead of the hero. Set right after
    // the spawn, the game named it after its template again, so it is checked on every update.
    char own[32];
    Format(own, "G3T_Player%d", id);
    if (std::strcmp(puppet.entity.GetInstance()->GetName().GetText(), own) != 0) puppet.entity.GetInstance()->SetName(bCString(own));
    // Trading between players (owner 23.10.): addressed, a puppet offers the game's own trade option ("Zeig mir deine
    // Ware!"), which opens the trade window with it (trade.cpp, offer.cpp). Checked like the name.
    if (!puppet.entity.Dialog.TradeEnabled) puppet.entity.Dialog.TradeEnabled = GETrue;
    const float speed = body::Speed(remote.previous.position, remote.latest.position, remote.previous.sent, remote.latest.sent,
                                    remote.previousTicks, remote.latestTicks);
    const body::Target target{remote.latest.position, remote.latest.rotation, remote.latest.movementMode, remote.latest.animation, speed,
                              remote.latest.aniState};
    if (!*remote.latest.shape) {  // a creature holds and wears nothing
        Hold(puppet, remote.latest.heldRight, remote.latest.heldLeft, now);
        Wear(puppet, remote.latest.armor, remote.latest.helmet, now);
    }
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
    started = net::Start(config.host, config.address, config.port, config.name, config.steam);
}

void OnWorldDeactivate()
{
    for (auto& puppet : puppets) puppet = Puppet{};
    for (auto& set : contacts) set.clear();
    haveAnchor = false;
}

void NoteContact(eCEntity* npc, eCEntity* puppet)
{
    const int id = PuppetOwner(puppet);
    if (!noting || !id || !npc || npc == puppet) return;
    if (contacts[id].insert(KeyOf(npc)).second) ++contactsNoted;
}

void BadStates(char const* argument, LogFn log)
{
    badStates = std::strcmp(argument, "off") != 0;
    log(badStates ? "BADSTATES on" : "BADSTATES off");
}

void ContactsOff(LogFn log)
{
    noting = false;
    log("CONTACTSOFF ok=1");
}

void ForgetNpcListOn(LogFn log)
{
    byContacts = false;
    log("FORGETNPCLIST ok=1 (puppets are forgotten by walking Entity::GetNPCs, as before)");
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
    Format(line, "NET local_id=%u connected=%d world=%08x perception_filter=%d blocked=%u wrapped=%u skipped=%u unknown=%u", net::LocalId(),
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
        Format(line, "REMOTE id=%d name=%s world=%08x same_world=%d age_ms=%u pos=%.1f,%.1f,%.1f puppet=%d puppet_pos=%.1f,%.1f,%.1f",
                  id, r.latest.name, r.latest.world, r.latest.world == CurrentWorld(), r.latestTicks ? now - r.latestTicks : 0,
                  r.latest.position[0], r.latest.position[1], r.latest.position[2], entity != nullptr,
                  puppetPos[0], puppetPos[1], puppetPos[2]);
        log(line);
        Format(line, "ANIM id=%d remote=%s requested=%s", id, *r.latest.animation ? r.latest.animation : "-",
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
            Format(line, "PUPPETPOSE id=%d remote=%s shows=%s run0=%d shape=%s body=%s", id, *r.latest.animation ? r.latest.animation : "-",
                      *shows ? shows : "-", running, *r.latest.shape ? r.latest.shape : "-",
                      // body: the template it is made from (its entity is named G3T_Player<id> since 23.10.)
                      puppets[id].spawned && puppets[id].entity.GetInstance() ? puppets[id].shape.c_str() : "-");
            log(line);
        }
        {
            Entity body(entity);
            const Entity held = entity ? body.Inventory.GetItemFromSlot(gESlot_RightHand) : Entity();
            Format(line, "HELD id=%d remote=%s puppet=%s", id, *r.latest.heldRight ? r.latest.heldRight : "-",
                      held.GetInstance() ? held.GetName().GetText() : "-");
            log(line);
            const Entity armor = entity ? body.Inventory.GetItemFromSlot(gESlot_Body) : Entity();
            const Entity helmet = entity ? body.Inventory.GetItemFromSlot(gESlot_Helmet) : Entity();
            Format(line, "WEAR id=%d remote_armor=%s remote_helmet=%s puppet_armor=%s puppet_helmet=%s look=%s template=%s changes=%u"
                            " equipped=%u linked=%u failed=%u", id,
                      *r.latest.armor ? r.latest.armor : "-", *r.latest.helmet ? r.latest.helmet : "-",
                      armor.GetInstance() ? armor.GetName().GetText() : "-", helmet.GetInstance() ? helmet.GetName().GetText() : "-",
                      *r.latest.look ? r.latest.look : "-", puppets[id].spawned ? puppets[id].shape.c_str() : "-", wearChanges,
                      wearEquipped, wearLinked, wearFailed);
            log(line);
        }
        if (entity) {
            char who[16];
            Format(who, "puppet%d", id);
            LogMotions(who, puppets[id].entity, log);
        }
    }
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    if (hero) {
        // A puppet may use the hero's own template (PC_Hero). The game must still find the real player.
        Format(line, "PLAYER_CHECK getplayer_is_hero=%d by_name_is_hero=%d",
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
    Format(line, "WALKTO index=%d started=%d target=%.1f,%.1f,%.1f", index, ok, target.GetX(), target.GetY(), target.GetZ());
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
    Format(line, "STEP index=%d target=%.1f,%.1f,%.1f pos=%.1f,%.1f,%.1f", index, position[0], position[1], position[2],
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

// QA (stage Wear): the local hero puts on an item by name (armor or helmet, by its use type); "-armor" / "-helmet" takes
// that slot off.
void WearHero(char const* argument, LogFn log)
{
    char line[1000];  // equipped= lists every filled slot (up to 900 characters)
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    if (!hero || !argument || !*argument) { log("REFUSED wear: no hero or no item"); return; }
    Entity self(hero);
    if (!std::strcmp(argument, "-armor") || !std::strcmp(argument, "-helmet")) {
        TakeOff(self, argument[1] == 'a' ? gESlot_Body : gESlot_Helmet);
    } else {
        const bool known = Template(argument).IsValid();
        const GEInt stack = StackFor(self, argument);
        // The game picks the slot itself: a Body_* armor goes to slot 17, a helmet to 18. (It_Armor_* are the packets one
        // picks up, use type 0: EquipStack refuses them, and UseStack answers true without equipping, armor-equip-c.md.)
        const GEBool ok = stack >= 0 ? self.Inventory.EquipStack(stack) : GEFalse;
        if (!ok) {
            // Why not: what the game says is missing.
            int length = Format(line, "REFUSED wear: %s template=%d stack=%d not equipped", argument, known, stack);
            auto* inventory = static_cast<gCInventory_PS*>(hero->GetPropertySet(eEPropertySetType_Inventory));
            if (inventory && stack >= 0) {
                bTObjArray<gCInventory_PS::SMissingAttrib> attribs;
                bTValArray<gCInventory_PS::SMissingSkill> skills;
                const GEBool can = inventory->CanEquip(stack, attribs, skills);
                length += Format(line + length, sizeof(line) - length, " can=%d", can ? 1 : 0);
                for (GEInt i = 0; i < attribs.GetCount() && i < 6 && length < 800; ++i)
                    length += Format(line + length, sizeof(line) - length, " attrib:%.24s-%d", attribs[i].MissingAttribName.GetText(),
                                        attribs[i].MissingAttribValue);
                for (GEInt i = 0; i < skills.GetCount() && i < 6 && length < 800; ++i)
                    length += Format(line + length, sizeof(line) - length, " skill:%.40s",
                                        skills[i].MissingSkill ? skills[i].MissingSkill->GetName().GetText() : "?");
                Format(line + length, sizeof(line) - length, " use=%d", static_cast<int>(self.Inventory.GetUseType(stack)));
            }
            log(line);
            return;
        }
    }
    const Entity armor = self.Inventory.GetItemFromSlot(gESlot_Body), helmet = self.Inventory.GetItemFromSlot(gESlot_Helmet);
    int length = Format(line, "WEARHERO armor=%s helmet=%s equipped=", armor.GetInstance() ? armor.GetName().GetText() : "-",
                           helmet.GetInstance() ? helmet.GetName().GetText() : "-");
    for (GEInt i = 0; i < self.Inventory.GetStackCount() && length > 0 && length < 900; ++i) {
        const int slot = static_cast<int>(self.Inventory.GetSlotFromStack(i));
        const Entity item = slot ? self.Inventory.GetTemplateItem(i) : Entity();
        if (item.GetInstance()) length += Format(line + length, sizeof(line) - length, "%d:%.40s,", slot, item.GetName().GetText());
    }
    log(line);
}

// QA (stage Wear): armor and helmet items NPCs within 100 m carry (worn or not; by use type or the armor category),
// to pick real names; for the first five NPCs every filled slot too (WORNSLOTS), to see where their armor is.
void WornScan(LogFn log)
{
    char line[1000];  // WORNSLOTS: up to 18 slots of 45 characters
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    int count = 0, slotted = 0;
    if (hero) {
        const bCVector& h = hero->GetWorldPosition();
        const float at[3] = {h.GetX(), h.GetY(), h.GetZ()};
        const bTObjArray<Entity> all = Entity::GetNPCs();
        for (int i = 0; i < all.GetCount() && count < 40; ++i) {
            Entity npc = all.GetAt(i);
            eCEntity* instance = npc.GetInstance();
            if (!instance || IsPuppet(instance)) continue;
            const bCVector& p = instance->GetWorldPosition();
            const float there[3] = {p.GetX(), p.GetY(), p.GetZ()};
            if (body::Distance(at, there) > 10000.0f) continue;
            if (slotted < 5) {
                ++slotted;
                int length = Format(line, "WORNSLOTS npc=%s stacks=%d", npc.GetName().GetText(), npc.Inventory.GetStackCount());
                for (int slot = 1; slot <= 18 && length > 0; ++slot) {
                    const Entity in = npc.Inventory.GetItemFromSlot(static_cast<gESlot>(slot));
                    if (in.GetInstance()) length += Format(line + length, sizeof(line) - length, " %d:%.40s", slot, in.GetName().GetText());
                }
                log(line);
            }
            for (GEInt k = 0; k < npc.Inventory.GetStackCount() && count < 40; ++k) {
                Entity item = npc.Inventory.GetTemplateItem(k);
                if (!item.GetInstance()) continue;
                const int use = static_cast<int>(item.Interaction.GetUseType());
                const bool armorCategory = static_cast<int>(item.Item.GetCategory()) == gEItemCategory_Armor;
                if (use != gEUseType_Armor && use != gEUseType_Helmet && !armorCategory) continue;
                Format(line, "WORN npc=%s item=%s kind=%s slot=%d use=%d", npc.GetName().GetText(), item.GetName().GetText(),
                          use == gEUseType_Helmet ? "helmet" : "armor", static_cast<int>(npc.Inventory.GetSlotFromStack(k)), use);
                log(line);
                ++count;
            }
        }
    }
    Format(line, "WORN end count=%d", count);
    log(line);
}

void WearOff(LogFn log)
{
    puppetWear = false;
    log("WEAROFF ok");
}

void LookOff(LogFn log)
{
    remoteLooks = false;
    log("LOOKOFF ok");
}

// QA (phase 2, ENTWURF-HANDEL): "<id>_<Info>": player <id>'s puppet gets a name of its own (G3T_Player<id>; an option owned
// by its template name would show on that NPC too) and a copy of the named dialog option, owned by it.
void PuppetInfo(char const* argument, LogFn log)
{
    char line[400], source[96] = {};
    const int id = std::atoi(argument);
    char const* sep = std::strchr(argument, '_');
    if (sep) strncpy_s(source, sep + 1, _TRUNCATE);
    eCEntity* puppet = PuppetOf(id);
    gCInfoManager_PS* manager = gCSession::GetInstance().IsGameRunning() ? gCInfoManager_PS::GetInstance() : nullptr;
    gCInfo_PS* from = manager && *source ? manager->GetInfo(bCString(source)) : nullptr;
    if (!puppet || !from) { log("REFUSED puppetinfo: <id>_<Info> with a puppet and a known info"); return; }
    char own[32], name[128];
    Format(own, "G3T_Player%d", id);
    Format(name, "G3T_%s_%d", source, id);
    puppet->SetName(bCString(own));
    gCInfo_PS* info = manager->GetInfo(bCString(name));
    const bool added = !info;
    if (!info) info = manager->AddInfo(bCString(name));
    if (!info) { log("REFUSED puppetinfo: AddInfo failed"); return; }
    info->CopyFrom(*from);
    info->AccessName() = bCString(name);
    info->AccessOwner() = bCString(own);
    info->AccessInfoGiven() = GEFalse;
    info->AccessPermanent() = GETrue;
    // CopyFrom left the condition type at General (puppettalkdiag 23.10.); a trader's own option is Trade, and its NPC
    // has trading switched on.
    info->AccessConditionType() = static_cast<gEInfoCondType const&>(from->AccessConditionType());
    info->AccessType() = static_cast<gEInfoType const&>(from->AccessType());
    Entity body(puppet);
    body.Dialog.TradeEnabled = GETrue;
    gCInfoScript_PS* script = info->AccessInfoScript().GetObject();
    Format(line, "PUPPETINFO id=%d puppet=%s by_name=%d info=%s added=%d cond_type=%d type=%d owner=%s lines=%d infos=%d has_info=%d trade=%d", id,
              puppet->GetName().GetText(), Entity::GetEntity(bCString(own)).GetInstance() == puppet ? 1 : 0, name, added ? 1 : 0,
              static_cast<int>(static_cast<gEInfoCondType const&>(info->AccessConditionType())),
              static_cast<int>(static_cast<gEInfoType const&>(info->AccessType())), info->AccessOwner().GetText(),
              script ? script->AccessInfoScriptLines().GetCount() : -1, manager->GetInfos().GetCount(), manager->HasInfos(puppet) ? 1 : 0,
              body.Dialog.TradeEnabled ? 1 : 0);
    log(line);
}

// QA (phase 2): trading switched on for player <id>'s puppet; the game then offers its own trade option (FILLER27, "Zeig mir
// deine Ware!") and ENDE when the player addresses it (puppettalkdiag 23.10.).
void PuppetTradeOn(char const* argument, LogFn log)
{
    char line[200];
    eCEntity* puppet = PuppetOf(std::atoi(argument));
    if (!puppet) { log("REFUSED puppettradeon: no such puppet"); return; }
    Entity body(puppet);
    body.Dialog.TradeEnabled = GETrue;
    gCInfoManager_PS* manager = gCSession::GetInstance().IsGameRunning() ? gCInfoManager_PS::GetInstance() : nullptr;
    Format(line, "PUPPETTRADE id=%s name=%s trade=%d has_info=%d", argument, puppet->GetName().GetText(), body.Dialog.TradeEnabled ? 1 : 0,
              manager && manager->HasInfos(puppet) ? 1 : 0);
    log(line);
}
}
