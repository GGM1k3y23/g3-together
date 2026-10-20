// SPDX-License-Identifier: GPL-3.0-or-later
#include "combat.h"
#include "body.h"

#include "log.h"
#include "net.h"
#include "npcs.h"
#include "remote.h"

#include <Game.h>
#include <Script.h>
#include <Windows.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace g3t::combat {
eCEntity* DamageSource(eCDynamicEntity* hero, char const** name);  // below
namespace {
// Every hit in the game ends in the AddHitPoints script, called through the script admin (hit-path probe in
// the opening raid, 2026-10-17): weapon touch damage (gCTouchDamage_PS::TriggerTarget), the AI combat loop
// (after OnHit) and gCEntity::DoDamage. Each first judges the hit, which only records the damage in the
// victim's DamageReceiver.DamageAmount, then calls AddHitPoints(victim, attacker, -amount). That call is the
// one place where host authority is enforced. (Some judgement outcomes, e.g. parries, call AddHitPoints
// directly inside Script_Game.dll and bypass the admin; they are not covered.)
bool installed = false;
bool bypass = false;
bool forwarded = false;  // client: applying a hit the host forwarded, judged here (Filter lets it through)  // negative control: the filter lets everything through
DWORD lastTry = 0;
gFScript original = nullptr;
unsigned filterCalls = 0;
unsigned dropped = 0;  // client: hits between host-owned bodies, which the host simulates itself
unsigned kills = 0;  // host: client hits that killed (OnCheatKill)
unsigned xpAwarded = 0;                     // client: kills of this player's hits rewarded
unsigned claimsSent = 0, claimsApplied = 0, playerHitsSent = 0, playerHitsTaken = 0;
unsigned npcHitsSent = 0, npcHitsApplied = 0, npcKills = 0, npcHitsUnresolved = 0, partyKills = 0;
bool partyXp = true;  // QA negative control: PartyXpOff
bool reactHits = true;  // QA negative control: ReactHitsOff
unsigned reactionsStarted = 0;  // host: client hits that made the NPC judge its attacker (AssessTarget)
bool npcHits = true;  // QA negative control: NpcHitsOff
unsigned forwardedJudged = 0, forwardedBare = 0;  // client: forwarded hits judged here / applied as bare hit points

template <typename T>
T* Find(bTValMap<bCString, T*>& map, char const* name)
{
    T* found = nullptr;
    return map.GetAt(bCString(name), found) ? found : nullptr;
}

// The hero and puppets made from his template keep hit points in PlayerMemory, NPCs in DamageReceiver.
bool HasPlayerMemory(eCEntity* entity) { return entity && entity->GetPropertySet(eEPropertySetType_PlayerMemory); }

int HitPoints(Entity& entity)
{
    if (HasPlayerMemory(entity.GetInstance())) return entity.PlayerMemory.GetHitPoints();
    return entity.DamageReceiver.HitPoints;
}

bool HasDamageReceiver(eCEntity* entity) { return entity && entity->GetPropertySet(eEPropertySetType_DamageReceiver); }

int Pending(Entity& entity) { return HasDamageReceiver(entity.GetInstance()) ? static_cast<int>(entity.DamageReceiver.DamageAmount) : 0; }

GEInt CallScript(char const* name, eCEntity* self, eCEntity* other, GEInt args)
{
    return GetScriptAdminExt().CallScript(bCString(name), self, other, args);
}

void Guid(eCEntity* entity, uint8_t out[16])
{
    const eCEntityProxy proxy(entity);
    std::memcpy(out, &proxy.GetEntityID().GetCore(), 16);
}

// Who dealt the hit: a weapon's Interaction owner, else the damage source itself.
eCEntity* Attacker(gCScriptProcessingUnit* spu, GELPVoid other)
{
    Entity source = other ? *static_cast<Entity*>(other) : Entity(spu ? spu->GetOtherEntity() : nullptr);
    if (!source.GetInstance()) return nullptr;
    Entity owner = source.Interaction.GetOwner();
    return owner.GetInstance() ? owner.GetInstance() : source.GetInstance();
}

GEInt GE_STDCALL Filter(gCScriptProcessingUnit* spu, GELPVoid self, GELPVoid other, GEInt args)
{
    ++filterCalls;
    if (bypass || forwarded || args >= 0) return original(spu, self, other, args);
    Entity victim = self ? *static_cast<Entity*>(self) : Entity(spu ? spu->GetSelfEntity() : nullptr);
    eCEntity* instance = victim.GetInstance();
    eCEntity* hero = gCSession::GetInstance().GetHero();
    eCEntity* attacker = Attacker(spu, other);
    // Every NPC is simulated on one machine, its owner (the host near the host, a client in its own region); the
    // others mirror it. Same rules on every machine:
    // 1. A hit by a body simulated elsewhere (another player's puppet, a mirrored NPC) is decided where that body
    //    really acts: the player's own hits arrive as claims, an NPC owner sends its NPC's hits on our hero.
    if (attacker && attacker != hero && (remote::IsPuppet(attacker) || npcs::IsMirrored(attacker))) {
        ++dropped;
        return GETrue;
    }
    // 2. A hit on an NPC simulated elsewhere goes to its owner, who applies it (and kills); the result comes back
    //    through mirroring. Our hero's hits, and our own NPCs' hits (fights where two players' regions meet).
    if (const uint8_t owner = npcs::OwnerOf(instance); owner && npcs::IsMirrored(instance)) {
        if (attacker && attacker != hero && npcHits && attacker->GetPropertySet(eEPropertySetType_NPC)) {
            uint8_t guid[16], npc[16], flags[16] = {};
            Guid(instance, guid);
            Guid(attacker, npc);
            // P3: a companion or summon of our hero (its party leader). For NPC hits the weapon field carries flags;
            // byte 0 = 1: a kill pays this player (the game pays the player for its party's kills in its death script).
            if (partyXp && attacker->GetPropertySet(eEPropertySetType_Party) && Entity(attacker).Party.GetPartyLeader().GetInstance() == hero)
                flags[0] = 1;
            net::SendHitNpc(owner, guid, args, flags, npc);
            ++npcHitsSent;
            return GETrue;
        }
        if (attacker != hero) { ++dropped; return GETrue; }
        uint8_t guid[16], weapon[16] = {};
        Guid(instance, guid);
        // The weapon in the right hand, so that the owner can judge a lethal hit with the same weapon.
        Entity self(hero);
        const GEInt held = self.Inventory.FindStackIndex(gESlot_RightHand);
        if (held >= 0) {
            const Template item = self.Inventory.GetTemplate(held);
            bCPropertyID const& id = item;
            std::memcpy(weapon, &id.GetCore(), 16);
        }
        net::SendHitNpc(owner, guid, args, weapon);
        ++claimsSent;
        return GETrue;
    }
    // 3. A hit on another player's puppet: that player's real hero takes it, judged on that player's machine
    //    against its own hero (armour, stumble, knock-out), so the attacker goes along.
    if (const int owner = remote::PuppetOwner(instance)) {
        uint8_t guid[16] = {};
        uint8_t attackerPlayer = 0;
        if (attacker && attacker == hero) attackerPlayer = net::LocalId();
        else if (attacker) Guid(attacker, guid);
        net::SendHitPlayer(static_cast<uint8_t>(owner), args, guid, attackerPlayer);
        ++playerHitsSent;
        return GETrue;
    }
    return original(spu, self, other, args);
}

// A hit for QA, the way the game's hit paths do it: judge (through OnDamage, which runs the same judgement
// as the paths above but without their swing-in-progress gates), then AddHitPoints(-recorded damage).
// The weapon's damage type is None outside a swing (the game sets it for the swing); set it for this hit.
GEInt GameHit(Entity& victim, eCEntity* source, int* pending)
{
    Entity weapon(source);
    const bool armed = source && source->GetPropertySet(eEPropertySetType_Damage);
    const gEDamageType type = armed ? static_cast<gEDamageType>(weapon.Damage.DamageType) : gEDamageType_None;
    if (armed && type == gEDamageType_None) weapon.Damage.DamageType = gEDamageType_Blade;
    const GEInt result = CallScript("OnDamage", victim.GetInstance(), source, 0);
    *pending = Pending(victim);
    if (result) CallScript("AddHitPoints", victim.GetInstance(), source, -*pending);
    if (armed) weapon.Damage.DamageType = type;
    return result;
}

// Owner of the NPC: another machine's NPC hit it there (where their regions meet). Applied with that NPC (our
// mirror of it) as the attacker, so this one turns on it. A lethal hit kills like a player's claim, but nobody
// earns experience: marked as defeated, the death script pays no one (static analysis of Kill).
void OnNpcHit(uint8_t from, Entity& target, int32_t delta, uint8_t const npc[16], bool party)
{
    _GUID raw;
    std::memcpy(&raw, npc, 16);
    Entity attacker = Entity(bCPropertyID(bCGuid(raw)));
    // An attacker not found here (an entity spawned only on the sender's machine, such as a weapon without an owner)
    // still hurts: dropping those hits lost all 14 of one fight (stage E4). The victim stands in as the source then.
    if (!attacker.GetInstance()) ++npcHitsUnresolved;
    ++npcHitsApplied;
    struct Pass { Pass() { forwarded = true; } ~Pass() { forwarded = false; } } pass;
    eCEntity* victim = target.GetInstance();
    if (HitPoints(target) + delta > 0) {
        CallScript("AddHitPoints", victim, attacker.GetInstance() ? attacker.GetInstance() : victim, delta);
        return;
    }
    target.NPC.DefeatedByPlayer = GETrue;
    CallScript("AddHitPoints", victim, victim, -HitPoints(target));
    CallScript("OnCheatKill", victim, victim, 0);
    ++npcKills;
    if (party) {  // the sender's companion or summon killed it: the sender's hero earns the experience
        uint8_t guid[16];
        Guid(victim, guid);
        net::SendKilled(from, guid);
        ++partyKills;
    }
    Logf("combat: %s of player %u killed %s", attacker.GetInstance() ? attacker.GetName().GetText() : "an NPC", from, target.GetName().GetText());
}

// Host: a client's hit on one of the host's NPCs.
void OnHitNpc(uint8_t from, uint8_t const guid[16], int32_t delta, uint8_t const weapon[16], uint8_t const npc[16])
{
    _GUID raw;
    std::memcpy(&raw, guid, 16);
    Entity target = Entity(bCPropertyID(bCGuid(raw)));
    if (!target.GetInstance() || target.IsDead() || delta >= 0) return;
    static const uint8_t zeros[16] = {};
    if (std::memcmp(npc, zeros, 16)) {
        OnNpcHit(from, target, delta, npc, weapon[0] == 1);
        return;
    }
    eCEntity* attacker = remote::PuppetOf(from);
    ++claimsApplied;
    // Applied with the sender's puppet as attacker; the filter's rule for hits by puppets must not drop it.
    struct Pass { Pass() { forwarded = true; } ~Pass() { forwarded = false; } } pass;
    if (HitPoints(target) + delta > 0 || !attacker) {
        CallScript("AddHitPoints", target.GetInstance(), attacker, delta);
        // The game's hit path (DoLogicalDamage) also decides how the NPC reacts; AddHitPoints alone left a friendly NPC
        // talking on while the client struck it (friendlydiag 20.10.). Let it judge the attacker as the game does when an
        // NPC takes damage (AssessAttack -> AssessTarget with reason ReactToDamage): fight back, flee or call for help.
        // Not again while it already has the puppet as its target.
        if (reactHits && attacker && target.NPC.GetCurrentTarget().GetInstance() != attacker && !target.IsDead() && !target.IsDown()) {
            CallScript("AssessTarget", target.GetInstance(), attacker, gEAttackReason_ReactToDamage);
            ++reactionsStarted;
        }
        return;
    }
    // Lethal. Not a hit judged by the game: for any attacker but the local player the hit judgement caps the
    // damage and never kills (the puppet's hit left the NPC standing at 0 hit points). Not the death task set
    // by hand either (a raider killed that way in mid-fight crashed the host in GetCombatMoveLength the next
    // frame). The game's own way to kill an NPC from outside is its kill cheat, OnCheatKill (stop, clear the
    // target, ZS_RagDollDead or ZS_Dead by species); the dying NPC's Kill script then does the bookkeeping.
    // The NPC itself as the killer, not the sender's puppet: its death states keep the killer, and a puppet removed
    // right after (its player left) made them use a deleted entity (eCEntity::QueryEntityProxyInternal crash on a
    // client in stage E2). The real killer is rewarded on its own machine (SendKilled).
    eCEntity* victim = target.GetInstance();
    target.NPC.DefeatedByPlayer = GETrue;
    CallScript("AddHitPoints", victim, victim, -HitPoints(target));
    CallScript("OnCheatKill", victim, victim, 0);  // like the console: self = the one who kills, other = the target
    ++kills;
    net::SendKilled(from, guid);  // the killer earns the experience on its own machine
    Logf("combat: player %u killed %s (OnCheatKill)", from, target.GetName().GetText());  // ZS_RagDollDead takes effect next frame
}

// Client: this player's hit killed a host NPC: the game's own reward (GiveXP computes it from the NPC's level).
void OnKilled(uint8_t const guid[16])
{
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    _GUID raw;
    std::memcpy(&raw, guid, 16);
    Entity victim = Entity(bCPropertyID(bCGuid(raw)));
    if (!hero || !victim.GetInstance()) return;
    Entity self(hero);
    const int before = static_cast<int>(self.PlayerMemory.XP);
    const GEInt result = CallScript("GiveXP", victim.GetInstance(), hero, -1);  // like the death script: self = the NPC, other = who earns it
    ++xpAwarded;
    Logf("combat: rewarded the kill of %s: GiveXP=%d xp %d -> %d", victim.GetName().GetText(), result, before,
         static_cast<int>(self.PlayerMemory.XP));
}


// Client: something on the host hit this player's puppet; the real hero takes the damage.
void OnHitPlayer(int32_t delta, uint8_t const attacker[16], uint8_t attackerPlayer)
{
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    if (!hero || delta >= 0) return;
    ++playerHitsTaken;
    eCEntity* by = nullptr;
    if (attackerPlayer) {
        by = remote::PuppetOf(attackerPlayer);
    } else {
        _GUID raw;
        std::memcpy(&raw, attacker, 16);
        static const uint8_t none[16] = {};
        if (std::memcmp(attacker, none, 16)) by = Entity(bCPropertyID(bCGuid(raw))).GetInstance();
    }
    if (!by || by == hero) {
        // Bare hit points, as before: without the hit judgement the hero neither stumbles nor falls, and at 0 hit
        // points kept walking (stage A exploration).
        CallScript("AddHitPoints", hero, nullptr, delta);
        ++forwardedBare;
        return;
    }
    // The hit judged here against this hero, with the attacker's weapon: the game's own stumble, knock-out and
    // death decisions (a death becomes a knock-out, downed.cpp).
    char const* kind = "";
    Entity self(hero);
    int pending = 0;
    forwarded = true;
    GameHit(self, DamageSource(static_cast<eCDynamicEntity*>(by), &kind), &pending);
    forwarded = false;
    ++forwardedJudged;
}
}

void Start()
{
    net::SetHitHandlers(&OnHitNpc, &OnHitPlayer);
    net::SetKilledHandler(&OnKilled);
}

void Tick()
{
    if (installed || !gCSession::GetInstance().IsGameRunning()) return;
    const DWORD now = GetTickCount();
    if (now - lastTry < 1000) return;
    lastTry = now;
    gSScript* script = Find(GetScriptAdminExt().GetScripts(), "AddHitPoints");
    if (!script) return;
    original = script->m_funcScript;
    script->m_funcScript = &Filter;
    installed = true;
    Log("combat: AddHitPoints filter installed");
}

// The damage source the game itself uses: the weapon in the right hand (its Damage properties set the
// amount, its Interaction owner is the attacker). Falls back to the hero for bare hands.
eCEntity* DamageSource(eCDynamicEntity* hero, char const** name)
{
    Entity weapon = Entity(hero).Inventory.GetItemFromSlot(gESlot_RightHand);
    if (!weapon.GetInstance()) weapon = Entity(hero).GetWeapon(GEFalse);
    *name = weapon.GetInstance() ? "weapon" : "hands";
    return weapon.GetInstance() ? weapon.GetInstance() : hero;
}

bool ParseGuid(char const* text, _GUID& out)
{
    unsigned d1, d2, d3, b[8];
    if (sscanf_s(text, "%8X-%4X-%4X-%2X%2X-%2X%2X%2X%2X%2X%2X", &d1, &d2, &d3, &b[0], &b[1], &b[2], &b[3], &b[4], &b[5], &b[6],
                 &b[7]) != 11)
        return false;
    out.Data1 = d1;
    out.Data2 = static_cast<unsigned short>(d2);
    out.Data3 = static_cast<unsigned short>(d3);
    for (int i = 0; i < 8; ++i) out.Data4[i] = static_cast<unsigned char>(b[i]);
    return true;
}

void HitNpc(char const* guid, LogFn log)
{
    char line[300];
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    _GUID raw;
    if (!ParseGuid(guid, raw)) { log("REFUSED hitnpc: bad GUID"); return; }
    Entity target = Entity(bCPropertyID(bCGuid(raw)));
    if (!hero || !target.GetInstance()) { log("REFUSED hitnpc: no hero or no such NPC"); return; }
    char const* kind = "";
    eCEntity* source = DamageSource(hero, &kind);
    const int before = HitPoints(target);
    const unsigned sentBefore = claimsSent, callsBefore = filterCalls;
    int pending = 0;
    const GEInt result = GameHit(target, source, &pending);
    sprintf_s(line, "HITNPC guid=%s name=%s source=%s:%s owner=%s result=%#x filtered=%u pending=%d task=%s before=%d after=%d dead=%d claim_sent=%d", guid,
              target.GetName().GetText(), kind, Entity(source).GetName().GetText(), Entity(source).Interaction.GetOwner().GetName().GetText(),
              result, filterCalls - callsBefore, pending, target.Routine.GetCurrentTask().GetText(), before, HitPoints(target),
              target.IsDead() ? 1 : 0, claimsSent != sentBefore);
    log(line);
}

// QA: the local hero hits another player's puppet through the game's hit path (judgement and stumble included).
void HitPuppet(char const* id, LogFn log)
{
    char line[200];
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    eCEntity* puppet = remote::PuppetOf(static_cast<uint8_t>(std::atoi(id)));
    if (!hero || !puppet) { log("REFUSED hitpuppet: no hero or no such puppet"); return; }
    Entity target(puppet);
    char const* kind = "";
    eCEntity* source = DamageSource(hero, &kind);
    int pending = 0;
    const unsigned sentBefore = playerHitsSent;
    const GEInt result = GameHit(target, source, &pending);
    sprintf_s(line, "HITPUPPET id=%s result=%#x task=%s sent=%d", id, result, target.Routine.GetCurrentTask().GetText(),
              playerHitsSent != sentBefore);
    log(line);
}

// QA: read the bCString global at this RVA of Game.dll (the script names gCEntity::DoDamage calls).
void GameString(char const* rvaText, LogFn log)
{
    char line[200];
    const uintptr_t rva = std::strtoul(rvaText, nullptr, 16);
    HMODULE game = GetModuleHandleA("Game.dll");
    char text[64] = "?";
    if (game && rva) {
        __try {
            char const* value = *reinterpret_cast<char const* const*>(reinterpret_cast<uintptr_t>(game) + rva);
            if (value) strncpy_s(text, value, _TRUNCATE);
        } __except (EXCEPTION_EXECUTE_HANDLER) {
            strcpy_s(text, "<fault>");
        }
    }
    sprintf_s(line, "GAMESTR rva=%#lx text=%s", static_cast<unsigned long>(rva), text);
    log(line);
}

void HitPuppet(int playerId, LogFn log)
{
    char line[240];
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    eCEntity* puppet = remote::PuppetOf(playerId);
    if (!hero || !puppet) { log("REFUSED hitpuppet: no hero or no puppet"); return; }
    char const* kind = "";
    eCEntity* source = DamageSource(hero, &kind);
    const unsigned sentBefore = playerHitsSent;
    Entity victim(puppet);
    const int before = HitPoints(victim);
    int pending = 0;
    const GEInt result = GameHit(victim, source, &pending);
    sprintf_s(line, "HITPUPPET id=%d source=%s:%s result=%#x pending=%d before=%d after=%d sent=%d", playerId, kind,
              Entity(source).GetName().GetText(), result, pending, before, HitPoints(victim), playerHitsSent != sentBefore);
    log(line);
}

void Arm(char const* item, LogFn log)
{
    char line[200];
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    if (!hero || !*item) { log("REFUSED arm: no hero or no item"); return; }
    Entity self(hero);
    const GEInt created = self.Inventory.CreateItems(Template(item), 0, 1);
    const GEInt stack = self.Inventory.FindStackIndex(Template(item));
    const GEBool held = stack >= 0 ? self.Inventory.HoldStacks(-1, stack) : GEFalse;
    Entity inHand = self.Inventory.GetItemFromSlot(gESlot_RightHand);
    sprintf_s(line, "ARM item=%s created=%d stack=%d held=%d right_hand=%s", item, created, stack, held ? 1 : 0,
              inHand.GetInstance() ? inHand.GetName().GetText() : "none");
    log(line);
}

void Uninstall(LogFn log)
{
    // Pass-through rather than restoring the pointer: other wrappers (the hit-path probe) may sit on top.
    bypass = installed;
    log(bypass ? "COMBATOFF ok=1" : "COMBATOFF ok=0");
}

void Report(LogFn log)
{
    char line[400];
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    Entity self(hero);
    sprintf_s(line, "HP hero=%d max=%d installed=%d bypass=%d filter_calls=%u dropped=%u kills=%u xp_awarded=%u xp=%d claims_sent=%u claims_applied=%u player_hits_sent=%u player_hits_taken=%u forwarded_judged=%u forwarded_bare=%u npc_hits_sent=%u npc_hits_applied=%u npc_kills=%u npc_hits_unresolved=%u party_kills=%u reactions=%u",
              hero ? self.PlayerMemory.GetHitPoints() : -1, hero ? self.PlayerMemory.GetHitPointsMax() : -1, installed, bypass,
              filterCalls, dropped, kills, xpAwarded, hero ? static_cast<int>(self.PlayerMemory.XP) : -1, claimsSent, claimsApplied, playerHitsSent, playerHitsTaken,
              forwardedJudged, forwardedBare, npcHitsSent, npcHitsApplied, npcKills, npcHitsUnresolved, partyKills, reactionsStarted);
    log(line);
}

void NpcHitsOff(LogFn log)
{
    npcHits = false;
    log("NPCHITSOFF ok=1");
}
}

namespace g3t::combat {
// QA: kill an NPC the game's way: down to 1 hit point, then one real hit with the hero's weapon, so that the
// game's own hit judgement decides the death. (Setting the death task from outside crashed the host twice
// when the NPC was in the middle of a fight.)
bool LethalHit(eCEntity* victim)
{
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    Entity target(victim);
    if (!hero || !victim || target.IsDead()) return false;
    const int hp = HitPoints(target);
    if (hp > 1) CallScript("AddHitPoints", victim, hero, 1 - hp);
    char const* kind = "";
    int pending = 0;
    GameHit(target, DamageSource(hero, &kind), &pending);
    return true;
}
// QA: the game's own reward for an NPC (GUID), as OnKilled gives it, with the result.
void GiveXp(char const* guid, LogFn log)
{
    char line[200];
    _GUID raw;
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    if (!ParseGuid(guid, raw) || !hero) { log("REFUSED givexp: bad GUID or no hero"); return; }
    Entity victim = Entity(bCPropertyID(bCGuid(raw)));
    if (!victim.GetInstance()) { log("REFUSED givexp: no such NPC"); return; }
    Entity self(hero);
    const int before = static_cast<int>(self.PlayerMemory.XP);
    const GEInt result = CallScript("GiveXP", victim.GetInstance(), hero, -1);  // like the death script: self = the NPC, other = who earns it
    sprintf_s(line, "GIVEXP name=%s result=%d xp_before=%d xp_after=%d level=%u dead=%d", victim.GetName().GetText(), result, before,
              static_cast<int>(self.PlayerMemory.XP), static_cast<unsigned>(victim.NPC.Level), victim.IsDead() ? 1 : 0);
    log(line);
}

// QA, stage A: the local hero's state, as the game sees it.
void HeroState(LogFn log)
{
    char line[400];
    gCSession& session = gCSession::GetInstance();
    eCDynamicEntity* hero = session.GetHero();
    if (!hero) { log("HERO none"); return; }
    Entity self(hero);
    gCGUIManager* gui = session.GetGUIManager();
    sprintf_s(line, "HERO hp=%d max=%d dead=%d down=%d task=%s state=%s ani_state=%d ai_mode=%d paused=%d running=%d page=%d",
              self.PlayerMemory.GetHitPoints(), self.PlayerMemory.GetHitPointsMax(), self.IsDead() ? 1 : 0, self.IsDown() ? 1 : 0,
              self.Routine.GetCurrentTask().GetText(), self.Routine.GetCurrentState().GetText(),
              static_cast<int>(self.Routine.AniState), static_cast<int>(self.Routine.AIMode), session.IsPaused() ? 1 : 0,
              session.IsGameRunning() ? 1 : 0, gui ? static_cast<int>(gui->GetActivePage()) : -1);
    log(line);
}

// QA, stage A: an NPC (GUID) hits the local hero with its weapon, through the game's own hit judgement.
void HurtHero(char const* guid, LogFn log)
{
    char line[300];
    _GUID raw;
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    if (!ParseGuid(guid, raw) || !hero) { log("REFUSED hurthero: bad GUID or no hero"); return; }
    Entity attacker = Entity(bCPropertyID(bCGuid(raw)));
    if (!attacker.GetInstance()) { log("REFUSED hurthero: no such NPC"); return; }
    char const* kind = "";
    eCEntity* source = DamageSource(static_cast<eCDynamicEntity*>(attacker.GetInstance()), &kind);
    Entity self(hero);
    const int before = self.PlayerMemory.GetHitPoints();
    int pending = 0;
    const GEInt result = GameHit(self, source, &pending);
    sprintf_s(line, "HURTHERO attacker=%s source=%s:%s result=%#x pending=%d before=%d after=%d dead=%d down=%d task=%s",
              attacker.GetName().GetText(), kind, Entity(source).GetName().GetText(), result, pending, before,
              self.PlayerMemory.GetHitPoints(), self.IsDead() ? 1 : 0, self.IsDown() ? 1 : 0, self.Routine.GetCurrentTask().GetText());
    log(line);
}

// QA, stage A: the game's kill cheat on the local hero, with an NPC (GUID) as the killer: the death path.
void KillHero(char const* guid, LogFn log)
{
    char line[300];
    _GUID raw;
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    if (!ParseGuid(guid, raw) || !hero) { log("REFUSED killhero: bad GUID or no hero"); return; }
    Entity killer = Entity(bCPropertyID(bCGuid(raw)));
    if (!killer.GetInstance()) { log("REFUSED killhero: no such NPC"); return; }
    const GEInt result = CallScript("OnCheatKill", killer.GetInstance(), hero, 0);
    Entity self(hero);
    sprintf_s(line, "KILLHERO killer=%s result=%d hp=%d dead=%d down=%d task=%s", killer.GetName().GetText(), result,
              self.PlayerMemory.GetHitPoints(), self.IsDead() ? 1 : 0, self.IsDown() ? 1 : 0, self.Routine.GetCurrentTask().GetText());
    log(line);
}

// QA, stage V4: the local hero swings its weapon the way a key press does (OnPlayerAction sets these states while
// the hero is in the melee task). `state` defaults to PS_Melee_Attack.
void Swing(char const* state, LogFn log)
{
    char line[300];
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    if (!hero) { log("REFUSED swing: no hero"); return; }
    Entity self(hero);
    const bCString before = self.Routine.GetCurrentTask();
    if (std::strcmp(before.GetText(), "PS_Melee") != 0) self.Routine.SetTask(bCString("PS_Melee"));
    self.Routine.SetState(bCString(state && *state ? state : "PS_Melee_Attack"));
    char motion[128] = "-";
    body::CurrentMotion(hero, motion, sizeof(motion));
    sprintf_s(line, "SWING task_before=%s task=%s state=%s motion=%s", before.GetText(), self.Routine.GetCurrentTask().GetText(),
              self.Routine.GetCurrentState().GetText(), motion);
    log(line);
}

// QA, stage A: an NPC (GUID) hits player 2's puppet on the host through the game's hit judgement.
void HitPuppetBy(char const* guid, LogFn log)
{
    char line[300];
    _GUID raw;
    eCEntity* puppet = remote::PuppetOf(2);
    if (!ParseGuid(guid, raw) || !puppet) { log("REFUSED hitpuppetby: bad GUID or no puppet"); return; }
    Entity attacker = Entity(bCPropertyID(bCGuid(raw)));
    if (!attacker.GetInstance()) { log("REFUSED hitpuppetby: no such NPC"); return; }
    char const* kind = "";
    eCEntity* source = DamageSource(static_cast<eCDynamicEntity*>(attacker.GetInstance()), &kind);
    const unsigned sentBefore = playerHitsSent;
    Entity victim(puppet);
    int pending = 0;
    const GEInt result = GameHit(victim, source, &pending);
    sprintf_s(line, "HITPUPPETBY attacker=%s source=%s:%s result=%#x pending=%d sent=%d", attacker.GetName().GetText(), kind,
              Entity(source).GetName().GetText(), result, pending, playerHitsSent != sentBefore);
    log(line);
}

// QA, stage V5: an NPC (GUID) flees from player 2's puppet, the way AssessTarget sends it off on Panic (target, then
// ZS_Flee), and says how it judges that puppet now: Panic (6) while it flees.
void FleeFrom(char const* guid, LogFn log)
{
    char line[300];
    _GUID raw;
    eCEntity* puppet = remote::PuppetOf(2);
    if (!ParseGuid(guid, raw) || !puppet) { log("REFUSED flee: bad GUID or no puppet"); return; }
    Entity npc = Entity(bCPropertyID(bCGuid(raw)));
    if (!npc.GetInstance() || npc.IsDead()) { log("REFUSED flee: no such NPC"); return; }
    npc.Routine.FullStop();
    npc.NPC.SetCurrentTarget(Entity(puppet));
    npc.Routine.AIMode = gEAIMode_Flee;
    npc.Routine.SetTask(bCString("ZS_Flee"));
    const GEInt attitude = CallScript("GetAttitude", npc.GetInstance(), puppet, 0);
    sprintf_s(line, "FLEE name=%s attitude=%d aimode=%d task=%s", npc.GetName().GetText(), attitude,
              static_cast<int>(npc.Routine.AIMode), npc.Routine.GetCurrentTask().GetText());
    log(line);
}
}

namespace g3t::combat {
void PartyXpOff(LogFn log)
{
    partyXp = false;
    log("PARTYXPOFF ok=1");
}

void ReactHitsOff(LogFn log)
{
    reactHits = false;
    log("REACTHITSOFF ok=1");
}
}
