// SPDX-License-Identifier: GPL-3.0-or-later
#include "combat.h"
#include "body.h"

#include "enclaves.h"
#include "log.h"
#include "net.h"
#include "npcs.h"
#include "remote.h"
#include "witness.h"

#include <Game.h>
#include <Script.h>
#include <Windows.h>
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

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
bool bypass = false;     // negative control: the filter lets everything through
bool forwarded = false;  // client: applying a hit the host forwarded, judged here (Filter lets it through)
DWORD paradeUntil = 0;   // QA guard: the hero holds its parade until then, as with the block key held down
bool guarded = false;    // QA guard was used: forwarded blows are logged with their cost
std::vector<std::pair<eCEntity*, DWORD>> npcParades;  // QA npcguard: NPCs (here simulated) holding their parade until then
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
// A kill counts once per victim and world (Codex review 22.10. R9): IsDead() turns true only once the death task sets its
// AI mode, so a second lethal hit in the same network tick passed and was rewarded again; the client's GiveXP had no check.
std::unordered_set<std::string> killedHere;  // host: victims killed here for a client's hit or its companion's
std::unordered_set<std::string> rewarded;    // client: victims this player was rewarded for
bool killOnce = true;  // QA negative control: killonceoff
unsigned killsRepeated = 0, rewardsRepeated = 0;
// A kill here that did not take kept its victim in killedHere: no later hit of any client counted on it (stageLiberate
// 24.10.: three raiders far from both players, killed by claims, were not dead 2 s later, without a task at 0 hit points or
// fighting on, and took claims for 14 rounds; the host's game probably does not process NPCs that far, not proven). Each
// such kill is looked at again 2 s later; a victim alive and not in a death task counts as not killed.
struct Unconfirmed { uint8_t guid[16]; DWORD due; };
std::vector<Unconfirmed> unconfirmed;
unsigned killsUndone = 0;
bool confirmKills = true;  // QA negative control: killconfirmoff
std::string KillKey(uint8_t const guid[16]) { return std::string(reinterpret_cast<char const*>(guid), 16); }
// Arena and duel (owner's decision 22.10., variant b; re/findings/defeat-arena-c.md): an NPC fighting a duel or an arena
// bout (attack reason Duel or Arena) takes hits only from its opponent there; a second player's hits do not count. Its
// opponent's last hit ends the bout as the game does, with a knock-out (Defeat), never with the kill cheat: a client's
// transferred hit at 0 hit points went to OnCheatKill and counted as murder (spieltest 21.10., Reddock, twice).
bool arenaRule = true;  // QA negative control: arenaruleoff
unsigned duelHitsRefused = 0, duelKnockouts = 0;
// P7 (24.10., murderdiag): the game knocks a human or orc out where a player's hit takes its last hit points, and only a
// blow on the one lying there kills it (the host's hero: Muntasir down for five hits, the sixth killed). A client's lethal
// claim went straight to OnCheatKill: every fight the joining player won was a death. The claim now carries what the
// game decided for that hit on the sender's machine (HitOutcome), and the owner knocks out where the game did.
bool knockOuts = true;  // QA negative control: remotekooff (a lethal claim kills, as before)
bool shapeHits = true;  // QA negative control: shapehitsoff (an animal form's hits go out as one of our NPCs', as before)
// P4, the hit judgement (AssessHit): a puppet carries its player's AniState (body.cpp), so a player who parries is a puppet in
// parade here, and our judgement of a blow on it took the block (ParadeStumble, no damage recorded, AddHitPoints never called):
// the blow never reached the player, who blocked for free (stageShieldBlock 25.10.: no stamina, no stumble, the shield talent
// unused; alone the client pays 17 stamina a blocked blow). The parade belongs to the player's machine, which judges the blow
// against its real hero (block, shield talent, stamina, stumble). So the judgement here sees the puppet standing, the blow
// goes on to AddHitPoints and to the player (rule 4). The scripts that judge a blow, wrapped like AddHitPoints.
char const* const Judgements[] = {"DoLogicalDamage", "OnDamage"};
gFScript judge[2] = {};
bool puppetParade = true;  // QA negative control: puppetparadeoff (the puppet's parade counts here, as before)
unsigned paradesPassedOn = 0;  // blows on a parrying puppet judged as unblocked here, so that its player judges the block
// The other way round (stageNpcBlock 25.10.): an NPC of the host parries a client's blow. The client judged it against its
// mirror, whose parade and turn come with the snapshots, late: 2 of 3 blows hurt the parrying NPC (20 hit points each), the
// third was blocked on the mirror only (no claim; on the host no stumble, no stamina). Alone, the host's blow was blocked
// every time (ZS_ParadeStumble, 40 stamina, no hit points). The block belongs to the NPC's owner: the client judges the blow
// with its mirror standing and claims the damage, the owner decides from its NPC whether it blocks and applies the game's
// block for an NPC (AssessHit SG+2c5a0..2c70b: half the damage from stamina, a quarter less with a shield in hand, what
// stamina lacks from hit points; ZS_ParadeStumble).
bool npcBlocks = true;  // QA negative control: npcblockoff (judged on the mirror, as before)
unsigned mirrorParadesPassedOn = 0, npcBlocksApplied = 0;
unsigned knockedOut = 0, finishRefused = 0;
uint8_t lastOutcome = 0;  // client: the outcome of the last claim sent (QA HITNPC line)
std::unordered_map<std::string, DWORD> knockedAt;  // owner: when a claim knocked the victim out (IsDown follows a frame later)

bool InBout(Entity& npc)
{
    if (!npc.GetInstance() || !npc.GetInstance()->GetPropertySet(eEPropertySetType_NPC)) return false;
    const gEAttackReason reason = npc.NPC.AttackReason;
    return reason == gEAttackReason_Duel || reason == gEAttackReason_Arena;
}

// A hit by `attacker` on an NPC in a bout with someone else: not counted.
bool OutsideBout(Entity& npc, eCEntity* attacker)
{
    return arenaRule && attacker && InBout(npc) && npc.NPC.GetCurrentTarget().GetInstance() != attacker;
}

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

// What the game's hit judgement (AssessHit, before AddHitPoints) decided for the victim: the task it set.
uint8_t Outcome(Entity& victim)
{
    const bCString task = victim.Routine.GetCurrentTask();
    for (char const* ko : {"ZS_Unconscious", "ZS_LieKnockDown", "ZS_PiercedKO"})
        if (task == bCString(ko)) return net::HitKnockOut;
    for (char const* dead : {"ZS_RagDollDead", "ZS_PiercedDead", "ZS_Dead"})
        if (task == bCString(dead)) return net::HitDeath;
    return net::HitUnjudged;
}

void Expect(uint8_t const guid[16])
{
    if (!confirmKills) return;
    Unconfirmed u{{}, GetTickCount() + 2000};
    std::memcpy(u.guid, guid, 16);
    unconfirmed.push_back(u);
}

void ConfirmKills()
{
    const DWORD now = GetTickCount();
    for (auto it = unconfirmed.begin(); it != unconfirmed.end();) {
        if (static_cast<int32_t>(now - it->due) < 0) { ++it; continue; }
        Entity victim(npcs::NpcByGuid(it->guid));
        if (victim.GetInstance() && !victim.IsDead() && Outcome(victim) != net::HitDeath) {
            killedHere.erase(KillKey(it->guid));
            ++killsUndone;
            Logf("combat: the kill of %s did not take (task %s, %d hit points); its next hit counts again", victim.GetName().GetText(),
                 victim.Routine.GetCurrentTask().GetText(), HitPoints(victim));
        }
        it = unconfirmed.erase(it);
    }
}

// The species the game knocks out instead of killing (OnCheatDefeat: human and orc go to ZS_Unconscious).
bool KnocksOut(Entity& npc)
{
    const gESpecies species = npc.NPC.Species;
    return species == gESpecies_Human || species == gESpecies_Orc;
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

GEInt Judge(int which, gCScriptProcessingUnit* spu, GELPVoid self, GELPVoid other, GEInt args)
{
    Entity victim = self ? *static_cast<Entity*>(self) : Entity(spu ? spu->GetSelfEntity() : nullptr);
    if (bypass || victim.Routine.AniState != gEAniState_Parade) return judge[which](spu, self, other, args);
    eCEntity* hero = gCSession::GetInstance().GetHero();
    eCEntity* attacker = Attacker(spu, other);
    const bool puppet = puppetParade && remote::PuppetOwner(victim.GetInstance());
    const bool mirror = npcBlocks && npcs::IsMirrored(victim.GetInstance()) && attacker && (attacker == hero || attacker == remote::ControlledBody());
    if (!puppet && !mirror) return judge[which](spu, self, other, args);
    victim.Routine.AniState = gEAniState_Stand;
    const GEInt result = judge[which](spu, self, other, args);
    if (victim.Routine.AniState == gEAniState_Stand) victim.Routine.AniState = gEAniState_Parade;  // the snapshots set it anyway
    ++(puppet ? paradesPassedOn : mirrorParadesPassedOn);
    return result;
}

// Owner of the NPC: whether it blocks a player's blow, as AssessHit asks (IsBlocking SG+d480): parrying, able to parry that
// move, facing the attacker.
bool Blocks(Entity& npc, eCEntity* attacker)
{
    return CallScript("IsInParadeMode", npc.GetInstance(), nullptr, 0) && CallScript("CanParadeMoveOf", npc.GetInstance(), attacker, 0) &&
           npc.IsInFOV(Entity(attacker));
}

// The game's block for an NPC (AssessHit, the victim not the player): half the blow's damage from stamina, a quarter of that
// less with a shield in the left hand, what stamina lacks from hit points; the NPC stumbles back (ZS_ParadeStumble).
void Block(Entity& npc, eCEntity* attacker, int32_t delta)
{
    int cost = delta / 2;
    Entity left = npc.Inventory.GetItemFromSlot(gESlot_LeftHand);
    if (left.GetInstance() && left.Interaction.GetUseType() == gEUseType_Shield) cost -= cost / 4;
    const int stamina = static_cast<int>(npc.DamageReceiver.StaminaPoints), hitPoints = HitPoints(npc);
    CallScript("AddStaminaPoints", npc.GetInstance(), nullptr, cost);
    if (stamina + cost < 0) CallScript("AddHitPoints", npc.GetInstance(), attacker, stamina + cost);
    npc.Routine.FullStop();
    npc.Routine.SetTask(bCString("ZS_ParadeStumble"));
    ++npcBlocksApplied;
    Logf("combat: %s blocked a player's blow of %d: stamina %d -> %d hit points %d -> %d", npc.GetName().GetText(), -delta, stamina,
         static_cast<int>(npc.DamageReceiver.StaminaPoints), hitPoints, HitPoints(npc));
}

GEInt GE_STDCALL JudgeLogical(gCScriptProcessingUnit* spu, GELPVoid self, GELPVoid other, GEInt args) { return Judge(0, spu, self, other, args); }
GEInt GE_STDCALL JudgeOnDamage(gCScriptProcessingUnit* spu, GELPVoid self, GELPVoid other, GEInt args) { return Judge(1, spu, self, other, args); }

GEInt GE_STDCALL Filter(gCScriptProcessingUnit* spu, GELPVoid self, GELPVoid other, GEInt args)
{
    ++filterCalls;
    if (bypass || forwarded || args >= 0) return original(spu, self, other, args);
    Entity victim = self ? *static_cast<Entity*>(self) : Entity(spu ? spu->GetSelfEntity() : nullptr);
    eCEntity* instance = victim.GetInstance();
    eCEntity* hero = gCSession::GetInstance().GetHero();
    eCEntity* attacker = Attacker(spu, other);
    // P6, an animal form: the player fights as the creature it controls (Entity::GetPlayer), the human body (GetHero) stays
    // hidden; the creature's hits are the player's, as the game counts them, not those of one of our NPCs (no experience,
    // no knock-out judgement, no murder for the player before).
    if (attacker && attacker != hero && shapeHits && attacker == remote::ControlledBody()) attacker = hero;
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
        lastOutcome = Outcome(victim);
        net::SendHitNpc(owner, guid, args, weapon, nullptr, lastOutcome);
        ++claimsSent;
        return GETrue;
    }
    // 3. An NPC this machine simulates in a duel or arena bout with someone else: our hero's hit does not count.
    if (attacker && attacker == hero && OutsideBout(victim, hero)) {
        ++duelHitsRefused;
        return GETrue;
    }
    // 4. A hit on another player's puppet: that player's real hero takes it, judged on that player's machine
    //    against its own hero (armour, stumble, knock-out), so the attacker goes along.
    if (const int owner = remote::PuppetOwner(instance)) {
        if (attacker && attacker != hero) remote::NoteContact(attacker, instance);
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
    // QA guard: the local hero still parries (the block key held); the parade state alone falls back to standing.
    if (paradeUntil && static_cast<int32_t>(GetTickCount() - paradeUntil) < 0 && victim.GetInstance() == gCSession::GetInstance().GetHero()) {
        victim.Routine.AniState = gEAniState_Parade;
        Entity by(source);
        Entity owner = by.Interaction.GetOwner();
        if (owner.GetInstance()) by = owner;
        Logf("combat: guard judged: parade_mode=%d fov=%d attacker=%s", CallScript("IsInParadeMode", victim.GetInstance(), nullptr, 0),
             victim.IsInFOV(by) ? 1 : 0, by.GetInstance() ? by.GetName().GetText() : "none");
    }
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
    Entity attacker(npcs::NpcByGuid(npc));
    // An attacker not found here (an entity spawned only on the sender's machine, such as a weapon without an owner)
    // still hurts: dropping those hits lost all 14 of one fight (stage E4). The victim stands in as the source then.
    if (!attacker.GetInstance()) ++npcHitsUnresolved;
    ++npcHitsApplied;
    struct Pass { Pass() { forwarded = true; } ~Pass() { forwarded = false; } } pass;
    eCEntity* victim = target.GetInstance();
    if (HitPoints(target) + delta > 0) {
        CallScript("AddHitPoints", victim, attacker.GetInstance() ? attacker.GetInstance() : victim, delta);
        witness::HitSeen(victim, attacker.GetInstance());  // those around hear the fight (P7)
        return;
    }
    uint8_t guid[16];
    Guid(victim, guid);
    killedHere.insert(KillKey(guid));  // before the death call: OnHitNpc drops further hits on it (R9)
    if (party) enclaves::NoteRemoteKill(guid, from, attacker.GetInstance());  // a murder by the sender's party counts (P7)
    witness::MurderSeen(victim, attacker.GetInstance());  // and those around saw the killing, as where it was decided
    target.NPC.DefeatedByPlayer = GETrue;
    CallScript("AddHitPoints", victim, victim, -HitPoints(target));
    CallScript("OnCheatKill", victim, victim, 0);
    Expect(guid);
    ++npcKills;
    if (party) {  // the sender's companion or summon killed it: the sender's hero earns the experience
        net::SendKilled(from, guid);
        ++partyKills;
    }
    Logf("combat: %s of player %u killed %s", attacker.GetInstance() ? attacker.GetName().GetText() : "an NPC", from, target.GetName().GetText());
}

// Host: a client's hit on one of the host's NPCs.
void OnHitNpc(uint8_t from, uint8_t const guid[16], int32_t delta, uint8_t const weapon[16], uint8_t const npc[16], uint8_t outcome)
{
    Entity target(npcs::NpcByGuid(guid));  // an NPC only: no chest, door, hero or puppet (Fable 1.7)
    if (!target.GetInstance() || target.IsDead() || delta >= 0 || delta < -1000000) return;
    if (killOnce && killedHere.count(KillKey(guid))) {  // killed already, its death task not running yet (R9)
        ++killsRepeated;
        return;
    }
    static const uint8_t zeros[16] = {};
    if (std::memcmp(npc, zeros, 16)) {
        OnNpcHit(from, target, delta, npc, weapon[0] == 1);
        return;
    }
    eCEntity* attacker = remote::PuppetOf(from);
    remote::NoteContact(target.GetInstance(), attacker);  // it may turn on the puppet now
    if (OutsideBout(target, attacker)) {
        ++duelHitsRefused;
        Logf("combat: player %u hit %s, who fights a duel or arena bout with someone else; not counted", from, target.GetName().GetText());
        return;
    }
    ++claimsApplied;
    // Applied with the sender's puppet as attacker; the filter's rule for hits by puppets must not drop it.
    struct Pass { Pass() { forwarded = true; } ~Pass() { forwarded = false; } } pass;
    if (attacker && target.Routine.AniState == gEAniState_Parade)
        Logf("combat: a player's blow on %s parrying: parade_mode=%d can_parade=%d fov=%d", target.GetName().GetText(),
             CallScript("IsInParadeMode", target.GetInstance(), nullptr, 0), CallScript("CanParadeMoveOf", target.GetInstance(), attacker, 0),
             target.IsInFOV(Entity(attacker)) ? 1 : 0);
    if (npcBlocks && attacker && !target.IsDown() && !target.IsDead() && Blocks(target, attacker)) {
        Block(target, attacker, delta);
        if (reactHits && target.NPC.GetCurrentTarget().GetInstance() != attacker) {
            CallScript("AssessTarget", target.GetInstance(), attacker, gEAttackReason_ReactToDamage);
            ++reactionsStarted;
        }
        witness::HitSeen(target.GetInstance(), attacker);
        return;
    }
    // P7: a blow on one lying knocked out is the game's finishing blow, whatever its hit points (the host's hero: five blows
    // without effect while it fell, then dead). Judged here: the sender's copy is not down there, so its game judged every
    // such blow a new knock-out (stageMurder 24.10.). The fall is timed for our own knock-outs only (2 s).
    bool finishing = false;
    if (knockOuts && attacker) {
        const auto at = knockedAt.find(KillKey(guid));
        const DWORD since = at != knockedAt.end() ? GetTickCount() - at->second : 0xffffffff;
        if (target.IsDown() || since < 3000) {
            if (since < 2000 || (arenaRule && InBout(target))) {
                ++finishRefused;  // still falling, or the end of a bout (never a murder there)
                return;
            }
            finishing = true;
        }
    }
    if (!finishing && (HitPoints(target) + delta > 0 || !attacker)) {
        CallScript("AddHitPoints", target.GetInstance(), attacker, delta);
        // The game's hit path (DoLogicalDamage) also decides how the NPC reacts; AddHitPoints alone left a friendly NPC
        // talking on while the client struck it (friendlydiag 20.10.). Let it judge the attacker as the game does when an
        // NPC takes damage (AssessAttack -> AssessTarget with reason ReactToDamage): fight back, flee or call for help.
        // Not again while it already has the puppet as its target.
        if (reactHits && attacker && target.NPC.GetCurrentTarget().GetInstance() != attacker && !target.IsDead() && !target.IsDown()) {
            CallScript("AssessTarget", target.GetInstance(), attacker, gEAttackReason_ReactToDamage);
            ++reactionsStarted;
        }
        witness::HitSeen(target.GetInstance(), attacker);  // and those around hear the fight (P7)
        return;
    }
    if (arenaRule && InBout(target)) {
        // The bout's end, as the game ends it: knocked out by its opponent (ZS_Unconscious takes CurrentAttacker and runs
        // Defeat: AIMode 8, the bout's quest and victory notice), not killed.
        eCEntity* victim = target.GetInstance();
        target.NPC.SetCurrentAttacker(Entity(attacker));
        CallScript("AddHitPoints", victim, attacker, -HitPoints(target));
        target.Routine.FullStop();
        target.Routine.SetTask(bCString("ZS_Unconscious"));
        ++duelKnockouts;
        Logf("combat: player %u won the duel or arena bout against %s (knocked out, not killed)", from, target.GetName().GetText());
        return;
    }
    if (knockOuts && !finishing) {
        // As the game decided on the sender's machine; a hit judged there as neither (that machine's copy had more hit points
        // left) by the species rule.
        if (outcome == net::HitKnockOut || (outcome == net::HitUnjudged && KnocksOut(target))) {
            // The game's way, as in the bout above: ZS_Unconscious runs Defeat(CurrentAttacker, victim). The puppet as attacker;
            // remote.cpp lets go of it when the puppet goes. The experience on the sender's machine, once per victim (OnKilled).
            eCEntity* victim = target.GetInstance();
            target.NPC.SetCurrentAttacker(Entity(attacker));
            target.NPC.DefeatedByPlayer = GETrue;
            CallScript("AddHitPoints", victim, attacker, -HitPoints(target));
            target.Routine.FullStop();
            target.Routine.SetTask(bCString("ZS_Unconscious"));
            knockedAt[KillKey(guid)] = GetTickCount();
            witness::KnockOutSeen(victim, attacker);
            ++knockedOut;
            net::SendKilled(from, guid);
            Logf("combat: player %u knocked %s out (outcome %u)", from, target.GetName().GetText(), outcome);
            return;
        }
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
    killedHere.insert(KillKey(guid));  // before the death call (R9)
    knockedAt.erase(KillKey(guid));
    enclaves::NoteRemoteKill(guid, from, attacker);  // its Kill counts the murder for the sender (P7)
    witness::MurderSeen(victim, attacker);  // those around saw it, as the finishing blow tells them
    target.NPC.DefeatedByPlayer = GETrue;
    CallScript("AddHitPoints", victim, victim, -HitPoints(target));
    CallScript("OnCheatKill", victim, victim, 0);  // like the console: self = the one who kills, other = the target
    Expect(guid);
    ++kills;
    net::SendKilled(from, guid);  // the killer earns the experience on its own machine
    Logf("combat: player %u killed %s (OnCheatKill%s)", from, target.GetName().GetText(), finishing ? ", finishing blow" : "");  // ZS_RagDollDead takes effect next frame
}

// Client: this player's hit killed a host NPC: the game's own reward (GiveXP computes it from the NPC's level).
void OnKilled(uint8_t const guid[16])
{
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    _GUID raw;
    std::memcpy(&raw, guid, 16);
    Entity victim = Entity(bCPropertyID(bCGuid(raw)));
    if (!hero || !victim.GetInstance()) return;
    if (killOnce && !rewarded.insert(KillKey(guid)).second) {  // R9: once per victim, whatever the host sends
        ++rewardsRepeated;
        Logf("combat: the kill of %s was rewarded already; not again", victim.GetName().GetText());
        return;
    }
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
    // P6: in an animal form the creature the player controls takes it, not the hidden human body (GetHero).
    eCEntity* hero = gCSession::GetInstance().GetHero() ? (shapeHits ? remote::ControlledBody() : gCSession::GetInstance().GetHero()) : nullptr;
    if (!hero || delta >= 0) return;
    ++playerHitsTaken;
    eCEntity* by = nullptr;
    if (attackerPlayer) {
        by = remote::PuppetOf(attackerPlayer);
    } else {
        static const uint8_t none[16] = {};
        if (std::memcmp(attacker, none, 16)) by = npcs::NpcByGuid(attacker);  // its weapon is read below: an NPC only
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
    const int hpBefore = self.PlayerMemory.GetHitPoints(), spBefore = self.PlayerMemory.GetStaminaPoints();
    forwarded = true;
    const GEInt result = GameHit(self, DamageSource(static_cast<eCDynamicEntity*>(by), &kind), &pending);
    forwarded = false;
    ++forwardedJudged;
    if (guarded)  // QA guard
        Logf("combat: forwarded blow judged here: result=%#x pending=%d hp %d -> %d sp %d -> %d", result, pending, hpBefore,
             self.PlayerMemory.GetHitPoints(), spBefore, self.PlayerMemory.GetStaminaPoints());
}
}

void Start()
{
    net::SetHitHandlers(&OnHitNpc, &OnHitPlayer);
    net::SetKilledHandler(&OnKilled);
}

void Tick()
{
    if (!unconfirmed.empty()) ConfirmKills();
    for (auto it = npcParades.begin(); it != npcParades.end();) {  // QA npcguard
        if (static_cast<int32_t>(GetTickCount() - it->second) >= 0) { it = npcParades.erase(it); continue; }
        Entity(it->first).Routine.AniState = gEAniState_Parade;
        ++it;
    }
    if (paradeUntil) {  // QA guard: the block key held down, every frame (the snapshots carry it to the puppet)
        eCEntity* hero = gCSession::GetInstance().GetHero();
        if (static_cast<int32_t>(GetTickCount() - paradeUntil) >= 0) paradeUntil = 0;
        else if (hero) Entity(hero).Routine.AniState = gEAniState_Parade;
    }
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
    const gFScript wrappers[] = {&JudgeLogical, &JudgeOnDamage};
    for (int i = 0; i < 2; ++i) {
        gSScript* judgement = Find(GetScriptAdminExt().GetScripts(), Judgements[i]);
        if (!judgement) { Logf("combat: no %s script; a parrying puppet is judged here", Judgements[i]); continue; }
        judge[i] = judgement->m_funcScript;
        judgement->m_funcScript = wrappers[i];
    }
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
    eCEntity* body = remote::ControlledBody();
    eCEntity* source = body && body != hero ? body : DamageSource(hero, &kind);  // an animal form strikes itself
    if (source == body && body != hero) kind = "shape";
    const int before = HitPoints(target);
    const int spBefore = HasDamageReceiver(target.GetInstance()) ? static_cast<int>(target.DamageReceiver.StaminaPoints) : -1;
    const unsigned sentBefore = claimsSent, callsBefore = filterCalls;
    int pending = 0;
    const GEInt result = GameHit(target, source, &pending);
    sprintf_s(line, "HITNPC guid=%s name=%s source=%s:%s owner=%s result=%#x filtered=%u pending=%d task=%s before=%d after=%d dead=%d claim_sent=%d outcome=%u sp_before=%d sp_after=%d", guid,
              target.GetName().GetText(), kind, Entity(source).GetName().GetText(), Entity(source).Interaction.GetOwner().GetName().GetText(),
              result, filterCalls - callsBefore, pending, target.Routine.GetCurrentTask().GetText(), before, HitPoints(target),
              target.IsDead() ? 1 : 0, claimsSent != sentBefore, claimsSent != sentBefore ? lastOutcome : 0u, spBefore,
              HasDamageReceiver(target.GetInstance()) ? static_cast<int>(target.DamageReceiver.StaminaPoints) : -1);
    log(line);
}

// QA, client (stage W5): "<guid>_<count>": that many lethal hit claims on a host NPC in one frame, as a burst of valid hits
// that reaches the host within one network tick (the bot's arguments hold no spaces).
void HitNpcBurst(char const* argument, LogFn log)
{
    char guid[64] = {};
    char const* cut = std::strrchr(argument, '_');
    const int count = cut ? std::atoi(cut + 1) : 0;
    if (cut && cut - argument < static_cast<ptrdiff_t>(sizeof(guid))) std::memcpy(guid, argument, cut - argument);
    _GUID raw;
    if (net::IsHost() || count < 1 || count > 10 || !ParseGuid(guid, raw)) {
        log("REFUSED hitnpcburst: client only, \"<guid>_<1-10>\"");
        return;
    }
    uint8_t bytes[16];
    std::memcpy(bytes, &raw, 16);
    static const uint8_t none[16] = {};
    for (int i = 0; i < count; ++i) net::SendHitNpc(1, bytes, -100000, none, nullptr, net::HitDeath);  // hits the game judged deaths
    claimsSent += count;
    char line[160];
    sprintf_s(line, "HITNPCBURST guid=%s sent=%d", guid, count);
    log(line);
}

void KillOnceOff(LogFn log)
{
    killOnce = false;
    log("KILLONCEOFF ok=1 (kills and rewards are not counted per victim, as before 23.10.)");
}

void KillConfirmOff(LogFn log)
{
    confirmKills = false;
    log("KILLCONFIRMOFF ok=1 (a kill here that did not take keeps its victim from further hits, as before 24.10.)");
}

void KillReport(LogFn log)
{
    char line[200];
    sprintf_s(line, "KILLONCE killed=%zu rewarded=%zu repeated_kills=%u repeated_rewards=%u xp_awarded=%u kills=%u undone=%u",
              killedHere.size(), rewarded.size(), killsRepeated, rewardsRepeated, xpAwarded, kills, killsUndone);
    log(line);
}

// A load brings the dead back as they were saved: they can die once more (R9).
void OnWorldChange()
{
    killedHere.clear();
    unconfirmed.clear();
    rewarded.clear();
    knockedAt.clear();
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
    sprintf_s(line, "HP hero=%d max=%d installed=%d bypass=%d filter_calls=%u dropped=%u kills=%u xp_awarded=%u xp=%d claims_sent=%u claims_applied=%u player_hits_sent=%u player_hits_taken=%u forwarded_judged=%u forwarded_bare=%u npc_hits_sent=%u npc_hits_applied=%u npc_kills=%u npc_hits_unresolved=%u party_kills=%u reactions=%u knocked_out=%u finish_refused=%u knockouts=%d",
              hero ? self.PlayerMemory.GetHitPoints() : -1, hero ? self.PlayerMemory.GetHitPointsMax() : -1, installed, bypass,
              filterCalls, dropped, kills, xpAwarded, hero ? static_cast<int>(self.PlayerMemory.XP) : -1, claimsSent, claimsApplied, playerHitsSent, playerHitsTaken,
              forwardedJudged, forwardedBare, npcHitsSent, npcHitsApplied, npcKills, npcHitsUnresolved, partyKills, reactionsStarted,
              knockedOut, finishRefused, knockOuts ? 1 : 0);
    log(line);
}

void PuppetParadeOff(LogFn log)
{
    puppetParade = false;
    log("PUPPETPARADEOFF ok=1 (a parrying puppet's block is judged here, as before 25.10.)");
}

void ParadeReport(LogFn log)
{
    char line[200];
    sprintf_s(line, "PARADE wrapped=%d,%d passed_on=%u on=%d mirror_passed_on=%u npc_blocks=%u npc_blocks_on=%d", judge[0] ? 1 : 0,
              judge[1] ? 1 : 0, paradesPassedOn, puppetParade ? 1 : 0, mirrorParadesPassedOn, npcBlocksApplied, npcBlocks ? 1 : 0);
    log(line);
}

void NpcBlockOff(LogFn log)
{
    npcBlocks = false;
    log("NPCBLOCKOFF ok=1 (a client's blow on a parrying NPC is judged on its mirror, as before 25.10.)");
}

void ShapeHitsOff(LogFn log)
{
    shapeHits = false;
    log("SHAPEHITSOFF ok=1 (an animal form's hits count as one of our NPCs', as before 24.10.)");
}

void RemoteKnockOutsOff(LogFn log)
{
    knockOuts = false;
    log("REMOTEKOOFF ok=1 (a lethal claim kills, as before 24.10.)");
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
    sprintf_s(line, "HERO hp=%d max=%d dead=%d down=%d task=%s state=%s ani_state=%d ai_mode=%d paused=%d running=%d page=%d sp=%d action=%d",
              self.PlayerMemory.GetHitPoints(), self.PlayerMemory.GetHitPointsMax(), self.IsDead() ? 1 : 0, self.IsDown() ? 1 : 0,
              self.Routine.GetCurrentTask().GetText(), self.Routine.GetCurrentState().GetText(),
              static_cast<int>(self.Routine.AniState), static_cast<int>(self.Routine.AIMode), session.IsPaused() ? 1 : 0,
              session.IsGameRunning() ? 1 : 0, gui ? static_cast<int>(gui->GetActivePage()) : -1, self.PlayerMemory.GetStaminaPoints(),
              static_cast<int>(self.Routine.Action));
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
    const int before = self.PlayerMemory.GetHitPoints(), spBefore = self.PlayerMemory.GetStaminaPoints();
    int pending = 0;
    // The blow as the game alone judges it on this machine, also from a mirrored NPC (whose hits rule 1 drops otherwise).
    forwarded = true;
    const GEInt result = GameHit(self, source, &pending);
    forwarded = false;
    sprintf_s(line, "HURTHERO attacker=%s source=%s:%s result=%#x pending=%d before=%d after=%d dead=%d down=%d task=%s sp_before=%d sp_after=%d",
              attacker.GetName().GetText(), kind, Entity(source).GetName().GetText(), result, pending, before,
              self.PlayerMemory.GetHitPoints(), self.IsDead() ? 1 : 0, self.IsDown() ? 1 : 0, self.Routine.GetCurrentTask().GetText(),
              spBefore, self.PlayerMemory.GetStaminaPoints());
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

// QA, P4 hit judgement: the local hero holds a shield (template, created) in the left hand beside its weapon and
// parries the way the block key does (PS_Melee_Parade), with full stamina, so that the next hit is judged as a block.
void Guard(char const* shield, LogFn log)
{
    char line[400];
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    if (!hero) { log("REFUSED guard: no hero"); return; }
    Entity self(hero);
    GEInt left = -1;
    if (shield && *shield) {
        self.Inventory.CreateItems(Template(shield), 0, 1);
        left = self.Inventory.FindStackIndex(Template(shield));
    }
    const GEInt right = self.Inventory.FindStackIndex(gESlot_RightHand);
    const GEBool held = left >= 0 ? self.Inventory.HoldStacks(left, right) : GEFalse;
    if (std::strcmp(self.Routine.GetCurrentTask().GetText(), "PS_Melee") != 0) self.Routine.SetTask(bCString("PS_Melee"));
    self.Routine.SetState(bCString("PS_Melee_Parade"));
    self.Routine.AniState = gEAniState_Parade;  // what IsInParadeMode asks
    paradeUntil = GetTickCount() + 15000;
    guarded = true;
    self.PlayerMemory.SetStaminaPoints(self.PlayerMemory.GetStaminaPointsMax());
    Entity inLeft = self.Inventory.GetItemFromSlot(gESlot_LeftHand);
    sprintf_s(line, "GUARD shield=%s stack=%d held=%d left_hand=%s task=%s state=%s action=%d shield_2=%d sp=%d hp=%d",
              shield ? shield : "-", left, held ? 1 : 0, inLeft.GetInstance() ? inLeft.GetName().GetText() : "none",
              self.Routine.GetCurrentTask().GetText(), self.Routine.GetCurrentState().GetText(), static_cast<int>(self.Routine.Action),
              self.Inventory.IsSkillActive(Template("Perk_Shield_2")) ? 1 : 0, self.PlayerMemory.GetStaminaPoints(),
              self.PlayerMemory.GetHitPoints());
    log(line);
}

// QA, P4 hit judgement: "<guid>_<who>" (hero or puppet<id>): the NPC, simulated here, turns to face that one and parries for
// 15 s with full stamina, so that blows from there are judged as blocked.
void NpcGuard(char const* argument, LogFn log)
{
    char line[300], guid[64] = {};
    char const* cut = std::strrchr(argument, '_');
    if (cut && cut - argument < static_cast<ptrdiff_t>(sizeof(guid))) std::memcpy(guid, argument, cut - argument);
    _GUID raw;
    if (!cut || !ParseGuid(guid, raw)) { log("REFUSED npcguard: \"<guid>_<hero|puppetN>\""); return; }
    Entity npc = Entity(bCPropertyID(bCGuid(raw)));
    eCEntity* who = !std::strcmp(cut + 1, "hero") ? gCSession::GetInstance().GetHero()
                    : !std::strncmp(cut + 1, "puppet", 6) ? remote::PuppetOf(static_cast<uint8_t>(std::atoi(cut + 7))) : nullptr;
    if (!npc.GetInstance() || !who || !HasDamageReceiver(npc.GetInstance())) { log("REFUSED npcguard: no such NPC or no one to face"); return; }
    const bCVector at = npc.GetInstance()->GetWorldPosition(), to = who->GetWorldPosition();
    float dx = to.GetX() - at.GetX(), dz = to.GetZ() - at.GetZ();
    const float yaw = std::atan2(dx, dz);
    const float place[3] = {at.GetX(), at.GetY(), at.GetZ()}, rotation[4] = {0.0f, std::sin(yaw / 2), 0.0f, std::cos(yaw / 2)};
    npc.MoveTo(body::Pose(*npc.GetInstance(), place, rotation));
    npc.DamageReceiver.StaminaPoints = npc.DamageReceiver.StaminaPointsMax;
    npc.Routine.AniState = gEAniState_Parade;
    npcParades.emplace_back(npc.GetInstance(), GetTickCount() + 15000);
    sprintf_s(line, "NPCGUARD name=%s facing=%s fov=%d parade_mode=%d sp=%d hp=%d mirrored=%d", npc.GetName().GetText(), Entity(who).GetName().GetText(),
              npc.IsInFOV(Entity(who)) ? 1 : 0, CallScript("IsInParadeMode", npc.GetInstance(), nullptr, 0),
              static_cast<int>(npc.DamageReceiver.StaminaPoints), HitPoints(npc), npcs::IsMirrored(npc.GetInstance()) ? 1 : 0);
    log(line);
}

// QA, P4 hit judgement: the NPC (GUID), simulated here, stops what it does (its fight too) and stands idle, so that it keeps
// its place and turn for the next blow.
void NpcStill(char const* guid, LogFn log)
{
    char line[200];
    _GUID raw;
    if (!ParseGuid(guid, raw)) { log("REFUSED npcstill: bad GUID"); return; }
    Entity npc = Entity(bCPropertyID(bCGuid(raw)));
    if (!npc.GetInstance() || npc.IsDead() || npcs::IsMirrored(npc.GetInstance())) { log("REFUSED npcstill: no such NPC simulated here"); return; }
    npc.Routine.FullStop();
    npc.NPC.SetCurrentTarget(Entity());
    npc.Routine.SetTask(bCString(body::IdleTask()));
    sprintf_s(line, "NPCSTILL name=%s task=%s", npc.GetName().GetText(), npc.Routine.GetCurrentTask().GetText());
    log(line);
}

// QA, P4 hit judgement: how this machine sees player 2's puppet against an NPC (GUID): parrying, facing it.
void PuppetParade(char const* guid, LogFn log)
{
    char line[200];
    _GUID raw;
    eCEntity* puppet = remote::PuppetOf(2);
    if (!ParseGuid(guid, raw) || !puppet) { log("REFUSED puppetparade: bad GUID or no puppet"); return; }
    Entity npc = Entity(bCPropertyID(bCGuid(raw)));
    if (!npc.GetInstance()) { log("REFUSED puppetparade: no such NPC"); return; }
    Entity victim(puppet);
    sprintf_s(line, "PUPPETPARADE parade=%d fov=%d ani_state=%d", CallScript("IsInParadeMode", puppet, nullptr, 0),
              victim.IsInFOV(npc) ? 1 : 0, static_cast<int>(victim.Routine.AniState));
    log(line);
}

// QA: how this machine shows player 2's puppet: PUPPETSTATE ani_state task motion (P1: boulders, stations).
void PuppetState(LogFn log)
{
    char line[300];
    eCEntity* puppet = remote::PuppetOf(2);
    if (!puppet) { log("REFUSED puppetstate: no puppet"); return; }
    Entity body(puppet);
    char motion[160] = "-";
    body::CurrentMotion(puppet, motion, sizeof(motion));
    sprintf_s(line, "PUPPETSTATE ani_state=%d task=%s motion=%s", static_cast<int>(body.Routine.AniState),
              body.Routine.GetCurrentTask().GetText(), motion);
    log(line);
}

// QA: an NPC's state as the judgement reads it here: NPCSTATE name task ani_state parade_mode sp hp.
void NpcState(char const* guid, LogFn log)
{
    char line[300];
    _GUID raw;
    if (!ParseGuid(guid, raw)) { log("REFUSED npcstate: bad GUID"); return; }
    Entity npc = Entity(bCPropertyID(bCGuid(raw)));
    if (!npc.GetInstance() || !HasDamageReceiver(npc.GetInstance())) { log("REFUSED npcstate: no such NPC"); return; }
    sprintf_s(line, "NPCSTATE name=%s task=%s ani_state=%d parade_mode=%d sp=%d hp=%d", npc.GetName().GetText(),
              npc.Routine.GetCurrentTask().GetText(), static_cast<int>(npc.Routine.AniState),
              CallScript("IsInParadeMode", npc.GetInstance(), nullptr, 0), static_cast<int>(npc.DamageReceiver.StaminaPoints), HitPoints(npc));
    log(line);
}

// QA, P4 hit judgement: what the game's own scripts say on this machine about a blow of an NPC (GUID) on the local hero:
// parrying (IsInParadeMode), able to parry that move (CanParadeMoveOf), the attacker's strength and the hero's
// protection against its weapon.
void HitProbe(char const* guid, LogFn log)
{
    char line[400];
    _GUID raw;
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    if (!ParseGuid(guid, raw) || !hero) { log("REFUSED hitprobe: bad GUID or no hero"); return; }
    Entity attacker = Entity(bCPropertyID(bCGuid(raw)));
    if (!attacker.GetInstance()) { log("REFUSED hitprobe: no such NPC"); return; }
    char const* kind = "";
    eCEntity* source = DamageSource(static_cast<eCDynamicEntity*>(attacker.GetInstance()), &kind);
    Entity self(hero);
    Entity weapon(source);
    if (paradeUntil && static_cast<int32_t>(GetTickCount() - paradeUntil) < 0) self.Routine.AniState = gEAniState_Parade;
    sprintf_s(line, "HITPROBE attacker=%s source=%s parade_mode=%d can_parade=%d strength=%d level=%d current_level=%d hero_level=%d "
              "protection=%d hero_strength=%d ani_state=%d action=%d attacker_action=%d damage=%d type=%d fov=%d",
              attacker.GetName().GetText(), kind, CallScript("IsInParadeMode", hero, nullptr, 0),
              CallScript("CanParadeMoveOf", hero, attacker.GetInstance(), 0), CallScript("GetStrength", attacker.GetInstance(), nullptr, 0),
              static_cast<int>(attacker.NPC.Level), CallScript("GetCurrentLevel", attacker.GetInstance(), nullptr, 0),
              static_cast<int>(self.NPC.Level), CallScript("GetProtection", hero, source, 0),
              CallScript("GetStrength", hero, nullptr, 0), static_cast<int>(self.Routine.AniState), static_cast<int>(self.Routine.Action),
              static_cast<int>(attacker.Routine.Action),
              source && source->GetPropertySet(eEPropertySetType_Damage) ? static_cast<int>(weapon.Damage.DamageAmount) : -1,
              source && source->GetPropertySet(eEPropertySetType_Damage) ? static_cast<int>(weapon.Damage.DamageType) : -1,
              self.IsInFOV(attacker) ? 1 : 0);
    log(line);
}

// QA, stage A: an NPC (GUID) hits player 2's puppet on the host through the game's hit judgement.
void HitPuppetBy(char const* argument, LogFn log)
{
    char line[500], guid[64] = {};
    // "<guid>_parade": the puppet parries at that moment, as the snapshots of a parrying player show it (P4, stageShieldBlock).
    char const* cut = std::strstr(argument, "_parade");
    const bool parade = cut != nullptr;
    std::memcpy(guid, argument, cut && cut - argument < 63 ? cut - argument : std::min<size_t>(std::strlen(argument), 63));
    _GUID raw;
    eCEntity* puppet = remote::PuppetOf(2);
    if (!ParseGuid(guid, raw) || !puppet) { log("REFUSED hitpuppetby: bad GUID or no puppet"); return; }
    Entity attacker = Entity(bCPropertyID(bCGuid(raw)));
    if (!attacker.GetInstance()) { log("REFUSED hitpuppetby: no such NPC"); return; }
    char const* kind = "";
    eCEntity* source = DamageSource(static_cast<eCDynamicEntity*>(attacker.GetInstance()), &kind);
    const unsigned sentBefore = playerHitsSent;
    Entity victim(puppet);
    if (parade) victim.Routine.AniState = gEAniState_Parade;
    // How the host sees the puppet when the blow comes: parrying (IsInParadeMode), facing the attacker, its motion.
    const GEInt parrying = CallScript("IsInParadeMode", puppet, nullptr, 0);
    const bool fov = victim.IsInFOV(attacker);
    const int aniState = static_cast<int>(victim.Routine.AniState);
    char motion[128] = "-";
    body::CurrentMotion(puppet, motion, sizeof(motion));
    int pending = 0;
    const GEInt result = GameHit(victim, source, &pending);
    sprintf_s(line, "HITPUPPETBY attacker=%s source=%s:%s result=%#x pending=%d sent=%d puppet_parade=%d puppet_fov=%d puppet_ani_state=%d motion=%s",
              attacker.GetName().GetText(), kind, Entity(source).GetName().GetText(), result, pending, playerHitsSent != sentBefore,
              parrying, fov ? 1 : 0, aniState, motion);
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

void ArenaRuleOff(LogFn log)
{
    arenaRule = false;
    log("ARENARULEOFF ok=1");
}

// QA: "Name_HP" sets that NPC's hit points (a tough duel partner, stage A4).
void NpcHp(char const* argument, LogFn log)
{
    char line[200];
    std::string arg = argument ? argument : "";
    const size_t cut = arg.rfind('_');
    Entity npc = cut == std::string::npos ? Entity() : Entity::GetEntity(bCString(arg.substr(0, cut).c_str()));
    if (!npc.GetInstance() || !HasDamageReceiver(npc.GetInstance())) { log("REFUSED npchp: Name_HP of an NPC"); return; }
    const int before = HitPoints(npc);
    npc.DamageReceiver.HitPoints = std::atoi(arg.substr(cut + 1).c_str());
    sprintf_s(line, "NPCHP name=%s before=%d after=%d", arg.substr(0, cut).c_str(), before, HitPoints(npc));
    log(line);
}

void ArenaReport(char const* name, LogFn log)
{
    char line[300];
    Entity npc = Entity::GetEntity(bCString(name));
    if (!npc.GetInstance() || !npc.GetInstance()->GetPropertySet(eEPropertySetType_NPC)) { log("REFUSED arena: no such NPC"); return; }
    const Entity target = npc.NPC.GetCurrentTarget();
    // The player the target belongs to: this machine's hero, another player's puppet, or 0 (an NPC, nobody).
    const int player = !target.GetInstance() ? 0 : target.GetInstance() == gCSession::GetInstance().GetHero() ? net::LocalId()
                                                                                                           : remote::PuppetOwner(target.GetInstance());
    sprintf_s(line, "ARENA npc=%s reason=%d in_bout=%d target=%s target_player=%d hp=%d down=%d dead=%d refused=%u knockouts=%u rule=%d", name,
              static_cast<int>(static_cast<gEAttackReason>(npc.NPC.AttackReason)), InBout(npc) ? 1 : 0,
              target.GetInstance() ? target.GetName().GetText() : "-", player, HitPoints(npc), npc.IsDown() ? 1 : 0, npc.IsDead() ? 1 : 0,
              duelHitsRefused, duelKnockouts, arenaRule ? 1 : 0);
    log(line);
}
}
