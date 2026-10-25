// SPDX-License-Identifier: GPL-3.0-or-later
#include "spells.h"

#include "infocmd.h"
#include "log.h"
#include "net.h"
#include "npcs.h"
#include "remote.h"
#include "world.h"

#include <Game.h>
#include <Script.h>
#include <Windows.h>
#include <intrin.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

namespace g3t::spells {
namespace {
// What a wrapped script acts on, and so where its cast has to go:
// - OnPlayer: its target (other); a cast on another player's puppet is replayed on that player's own hero.
// - OnNpc: its target; a cast on an NPC another machine simulates (a mirror here) runs there, not here.
// - AroundCaster: the NPCs around the caster; a client's cast runs on the host (which simulates them), round the
//   client's puppet (re/findings/npc-spells-c.md).
// - AroundCasterAt: as AroundCaster, with a target the NPCs are sent at (call animals: up to 3 animals near the caster
//   attack it; a client's cast found only mirrors and did nothing, callanimals2diag 21.10., re/findings/summon-calls-b.md).
// - TakeOver: the target joins the caster's party (word of control, tame animal: type Controlled, then it follows).
//   The scripts insist on the local player as caster, so a client's cast stays here: the client first takes the
//   mirrored NPC over as a companion (as a dialog's JoinPlayer does), then casts; if the NPC did not join, it goes back.
enum Kind { OnPlayer, OnNpc, AroundCaster, AroundCasterAt, TakeOver };
struct Spec {
    char const* name;
    Kind kind;
    char const* item;  // the script reads the caster's spell (CanBePoisoned): the replay gives the puppet this one
};
const Spec Specs[] = {{"MagicHealOther", OnPlayer},  {"MagicSleep", OnNpc},           {"MagicFear", OnNpc},
                      {"MagicCharm", OnNpc},         {"MagicStun", AroundCaster},     {"MagicHypnosis", AroundCaster},
                      {"MagicWordOfControl", TakeOver}, {"MagicTameAnimal", TakeOver}, {"MagicPoison", OnNpc, "It_Spell_Poison"},
                      {"MagicDisease", OnNpc, "It_Spell_Disease"}, {"MagicCallAnimals", AroundCasterAt},
                      // P6: bloodlust sets the target on someone near it (AssessTarget, duel), or on the caster (re/out/p6-bloodlust.c)
                      {"MagicBloodlust", OnNpc}};
constexpr int Count = sizeof(Specs) / sizeof(Specs[0]);
gFScript originals[Count] = {};
bool installed = false, mirror = true;
DWORD lastTry = 0;
unsigned sent = 0, received = 0, replayed = 0, refused = 0, noHero = 0, npcSent = 0, npcReceived = 0, npcApplied = 0, npcRefused = 0,
         takenOver = 0, givenBack = 0;
// Poison and burning (re/findings/poison-plan-c.md): the game's OnTick (Script_Game.dll+0xb0ef0, not in the script table)
// takes their hit points straight through AddHitPoints, past the combat filter, and then decides KO or death by itself.
// On a mirror or a puppet that is the owner's to do (its snapshots bring hit points and death), so the tick is skipped
// there. QA tickguardoff lets mirrors tick again (negative control).
constexpr uintptr_t OnTickRva = 0xb0ef0;
gFScript originalTick = nullptr;
// GiveXP (re/findings/xp-attributes-c.md): self = the victim or World/None, other = who earns it; with no one as other
// it pays the local hero. Hooked to log every reward with its caller, and so that a client's hero earns nothing for a
// death without a killer of an NPC the client does not simulate (stage 3c).
constexpr uintptr_t GiveXpRva = 0x4e160;
gFScript originalGiveXp = nullptr;
bool tickTried = false, tickGuard = true;
unsigned ticksRun = 0, ticksSkipped = 0, poisonedSkipped = 0;

template <typename T>
T* Find(bTValMap<bCString, T*>& map, char const* name)
{
    T* found = nullptr;
    return map.GetAt(bCString(name), found) ? found : nullptr;
}

int IndexOf(char const* name)
{
    for (int i = 0; i < Count; ++i)
        if (!std::strcmp(Specs[i].name, name)) return i;
    return -1;
}

void Guid(eCEntity* entity, uint8_t out[16])
{
    const eCEntityProxy proxy(entity);
    std::memcpy(out, &proxy.GetEntityID().GetCore(), 16);
}

bool ParseGuidText(char const* text, _GUID& out)
{
    unsigned d1 = 0, d2 = 0, d3 = 0, b[8] = {};
    if (std::sscanf(text, "%8x-%4x-%4x-%2x%2x-%2x%2x%2x%2x%2x%2x", &d1, &d2, &d3, &b[0], &b[1], &b[2], &b[3], &b[4], &b[5], &b[6], &b[7]) != 11)
        return false;
    out.Data1 = d1;
    out.Data2 = static_cast<unsigned short>(d2);
    out.Data3 = static_cast<unsigned short>(d3);
    for (int i = 0; i < 8; ++i) out.Data4[i] = static_cast<unsigned char>(b[i]);
    return true;
}

Entity ByGuid(uint8_t const guid[16])
{
    static const uint8_t none[16] = {};
    if (!std::memcmp(guid, none, 16)) return Entity();
    _GUID raw;
    std::memcpy(&raw, guid, 16);
    return Entity(bCPropertyID(bCGuid(raw)));
}

// One wrapper per script: the index picks the original, the name to send and what it acts on.
template <int I>
GEInt GE_STDCALL Wrap(gCScriptProcessingUnit* spu, GELPVoid self, GELPVoid other, GEInt args)
{
    eCEntity* caster = self ? static_cast<Entity*>(self)->GetInstance() : (spu ? spu->GetSelfEntity() : nullptr);
    eCEntity* target = other ? static_cast<Entity*>(other)->GetInstance() : (spu ? spu->GetOtherEntity() : nullptr);
    const bool own = mirror && caster && caster == remote::ControlledBody();
    if (own && Specs[I].kind == OnNpc && target && npcs::IsMirrored(target) && npcs::OwnerOf(target)) {
        uint8_t guid[16];
        Guid(target, guid);
        net::SendNpcSpell(npcs::OwnerOf(target), Specs[I].name, guid, args);
        ++npcSent;
        Logf("spells: %s on %s, simulated by player %u: sent", Specs[I].name, Entity(target).GetName().GetText(), npcs::OwnerOf(target));
        return 1;
    }
    if (own && Specs[I].kind == TakeOver && target && npcs::IsMirrored(target) && infocmd::TakeOver(target, true)) {
        const GEInt result = originals[I](spu, self, other, args);
        Entity npc(target);
        const bool joined = npc.GetInstance()->GetPropertySet(eEPropertySetType_Party) && npc.Party.GetPartyLeader().GetInstance() == caster;
        if (joined) {
            ++takenOver;
            Logf("spells: %s took %s over (now ours, in our party)", Specs[I].name, npc.GetName().GetText());
        } else {
            infocmd::TakeOver(target, false);
            ++givenBack;
            Logf("spells: %s did not take %s, given back", Specs[I].name, npc.GetName().GetText());
        }
        return result;
    }
    if (own && (Specs[I].kind == AroundCaster || Specs[I].kind == AroundCasterAt) && !net::IsHost()) {
        uint8_t guid[16] = {};
        if (Specs[I].kind == AroundCasterAt && target) Guid(target, guid);
        net::SendNpcSpell(1, Specs[I].name, guid, args);
        ++npcSent;
        Logf("spells: %s around us: sent to the host", Specs[I].name);
        return 1;
    }
    const GEInt result = originals[I](spu, self, other, args);
    const int player = target ? remote::PuppetOwner(target) : 0;
    if (own && Specs[I].kind == OnPlayer && player) {
        net::SendPlayerSpell(static_cast<uint8_t>(player), Specs[I].name);
        ++sent;
        Logf("spells: %s on player %d's puppet, sent", Specs[I].name, player);
    }
    return result;
}

const gFScript wrappers[] = {&Wrap<0>, &Wrap<1>, &Wrap<2>, &Wrap<3>, &Wrap<4>, &Wrap<5>, &Wrap<6>, &Wrap<7>, &Wrap<8>, &Wrap<9>, &Wrap<10>, &Wrap<11>};
// One wrapper per spec: a missing one put a null function into the script table (a client crashed casting bloodlust, 24.10.).
static_assert(sizeof(wrappers) / sizeof(wrappers[0]) == Count, "one Wrap<I> per entry of Specs");

// Jump from `target` to `detour`; the returned trampoline runs the 6 stolen bytes and continues in the original.
void* JumpTo(unsigned char* target, void* detour)
{
    constexpr size_t Stolen = 6;
    auto* trampoline = static_cast<unsigned char*>(VirtualAlloc(nullptr, 32, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE));
    if (!trampoline) return nullptr;
    std::memcpy(trampoline, target, Stolen);
    trampoline[Stolen] = 0xE9;
    const int32_t back = static_cast<int32_t>(reinterpret_cast<intptr_t>(target + Stolen) - reinterpret_cast<intptr_t>(trampoline + Stolen + 5));
    std::memcpy(trampoline + Stolen + 1, &back, 4);
    DWORD old = 0;
    if (!VirtualProtect(target, Stolen, PAGE_EXECUTE_READWRITE, &old)) return nullptr;
    target[0] = 0xE9;
    const int32_t to = static_cast<int32_t>(reinterpret_cast<intptr_t>(detour) - reinterpret_cast<intptr_t>(target + 5));
    std::memcpy(target + 1, &to, 4);
    target[5] = 0x90;
    VirtualProtect(target, Stolen, old, &old);
    FlushInstructionCache(GetCurrentProcess(), target, Stolen);
    return trampoline;
}

GEInt GE_STDCALL HookTick(gCScriptProcessingUnit* spu, GELPVoid self, GELPVoid other, GEInt args)
{
    eCEntity* body = self ? static_cast<Entity*>(self)->GetInstance() : (spu ? spu->GetSelfEntity() : nullptr);
    if (tickGuard && body && (remote::IsPuppet(body) || npcs::IsMirrored(body))) {
        ++ticksSkipped;
        if (body->GetPropertySet(eEPropertySetType_NPC) && (Entity(body).NPC.StatusEffects & gEStatusEffect_Poisoned)) ++poisonedSkipped;
        return 1;
    }
    ++ticksRun;
    return originalTick(spu, self, other, args);
}

void InstallGiveXp(unsigned char* base);

// OnTick starts with push ebp; mov ebp,esp; and esp,-8; sub esp,0x41c (re/out/poison-prologs-c.txt) and returns with
// ret 0x10; it is hooked in place only if all 16 bytes match.
void InstallTick()
{
    tickTried = true;
    auto* base = reinterpret_cast<unsigned char*>(GetModuleHandleA("Script_Game.dll"));
    static const unsigned char expected[] = {0x55, 0x8b, 0xec, 0x83, 0xe4, 0xf8, 0x81, 0xec, 0x1c, 0x04, 0x00, 0x00, 0x53, 0x56, 0x8b, 0x35};
    if (base && std::memcmp(base + OnTickRva, expected, sizeof(expected)) == 0)
        originalTick = reinterpret_cast<gFScript>(JumpTo(base + OnTickRva, reinterpret_cast<void*>(&HookTick)));
    Log(originalTick ? "spells: OnTick hooked (mirrors and puppets do not tick poison)" : "spells: OnTick not hooked (prologue differs)");
    InstallGiveXp(base);
}

GEInt GE_STDCALL HookGiveXp(gCScriptProcessingUnit* spu, GELPVoid self, GELPVoid other, GEInt args)
{
    const uintptr_t caller = reinterpret_cast<uintptr_t>(_ReturnAddress()) - reinterpret_cast<uintptr_t>(GetModuleHandleA("Script_Game.dll"));
    eCEntity* victim = self ? static_cast<Entity*>(self)->GetInstance() : (spu ? spu->GetSelfEntity() : nullptr);
    eCEntity* earner = other ? static_cast<Entity*>(other)->GetInstance() : (spu ? spu->GetOtherEntity() : nullptr);
    char victimName[64], earnerName[64];
    strncpy_s(victimName, victim ? Entity(victim).GetName().GetText() : "-", _TRUNCATE);
    strncpy_s(earnerName, earner ? Entity(earner).GetName().GetText() : "-", _TRUNCATE);
    if (!earner && victim && victim->GetPropertySet(eEPropertySetType_NPC) && !npcs::PaysDeathWithoutKiller(victim)) {
        Logf("xp: GiveXP self=%s other=- value=%d refused: not simulated here, the host pays its own hero", victimName, args);
        return 0;
    }
    const GEInt result = originalGiveXp(spu, self, other, args);
    Logf("xp: GiveXP self=%s other=%s value=%d result=%d caller=SG+0x%x", victimName, earnerName, args, result, static_cast<unsigned>(caller));
    return result;
}

// GiveXP starts like OnTick: push ebp; mov ebp,esp; and esp,-8; then sub esp,0x37c (re/out/area-xp-main-c.asm).
void InstallGiveXp(unsigned char* base)
{
    static const unsigned char expected[] = {0x55, 0x8b, 0xec, 0x83, 0xe4, 0xf8, 0x81, 0xec, 0x7c, 0x03, 0x00, 0x00};
    if (base && std::memcmp(base + GiveXpRva, expected, sizeof(expected)) == 0)
        originalGiveXp = reinterpret_cast<gFScript>(JumpTo(base + GiveXpRva, reinterpret_cast<void*>(&HookGiveXp)));
    Log(originalGiveXp ? "spells: GiveXP hooked (rewards logged)" : "spells: GiveXP not hooked (prologue differs)");
}

void OnPlayerSpell(uint8_t from, char const* script)
{
    ++received;
    const int index = IndexOf(script);
    if (index < 0 || Specs[index].kind != OnPlayer || !originals[index]) {
        ++refused;
        Logf("spells: %s from player %u refused (not replayed here)", script, from);
        return;
    }
    eCEntity* hero = remote::ControlledBody();
    if (!hero) {
        ++noHero;
        return;
    }
    // self = the caster's puppet when there is one (the scripts read the target, other), other = our own hero.
    eCEntity* caster = remote::PuppetOf(from);
    Entity self(caster ? caster : hero), target(hero);
    originals[index](nullptr, &self, &target, 0);
    ++replayed;
    Logf("spells: %s from player %u replayed on our hero", script, from);
}

// A spell another player cast on NPCs this machine simulates: the game's own script, cast by that player's puppet,
// on the NPC itself (or round the puppet).
void OnNpcSpell(uint8_t from, char const* script, uint8_t const guid[16], int32_t args)
{
    ++npcReceived;
    const int index = IndexOf(script);
    eCEntity* puppet = remote::PuppetOf(from);
    Entity target = ByGuid(guid);
    const bool known = index >= 0 && Specs[index].kind != OnPlayer && Specs[index].kind != TakeOver && originals[index];
    const bool onNpc = known && Specs[index].kind == OnNpc;
    const bool at = known && Specs[index].kind == AroundCasterAt;
    if (!known || !puppet || (onNpc && (!target.GetInstance() || npcs::IsMirrored(target.GetInstance()))) || (at && !target.GetInstance())) {
        ++npcRefused;
        Logf("spells: %s from player %u refused (unknown, no puppet, or the NPC is not simulated here)", script, from);
        return;
    }
    Entity self(puppet);
    Entity other = onNpc || at ? target : Entity(puppet);
    // Poison and disease judge the target by the caster's spell (re/findings/poison-plan-c.md): the puppet holds none, so
    // it gets the spell's item and that spell for the replay, as copies of spell projectiles do (shot.cpp).
    Entity held;
    bool lent = false;
    if (Specs[index].item) {
        const Template item(Specs[index].item);
        if (item.IsValid() && self.Inventory.GetItemAmount(item) < 1) self.Inventory.CreateItems(item, 0, 1);
        const GEInt stack = item.IsValid() ? self.Inventory.FindStackIndex(item) : -1;
        const Entity spell = stack >= 0 ? self.Inventory.GetTemplateItem(stack).Item.GetSpell() : Entity();
        if (spell.GetInstance()) {
            held = self.Interaction.GetSpell();
            self.Interaction.SetSpell(spell);
            lent = true;
        }
    }
    originals[index](nullptr, &self, &other, args);
    if (lent) self.Interaction.SetSpell(held);
    ++npcApplied;
    Logf("spells: %s from player %u applied%s%s", script, from, onNpc || at ? " on " : "", onNpc || at ? target.GetName().GetText() : "");
}
}

void Start()
{
    net::SetPlayerSpellHandler(&OnPlayerSpell);
    net::SetNpcSpellHandler(&OnNpcSpell);
}

void Tick()
{
    const DWORD now = GetTickCount();
    if (installed || !gCSession::GetInstance().IsGameRunning() || now - lastTry < 1000) return;
    lastTry = now;
    auto& scripts = GetScriptAdminExt().GetScripts();
    for (int i = 0; i < Count; ++i) {
        gSScript* script = Find(scripts, Specs[i].name);
        if (!script) return;  // tried again later
        if (!originals[i]) {
            originals[i] = script->m_funcScript;
            script->m_funcScript = wrappers[i];
        }
    }
    installed = true;
    Log("spells: wrappers installed (MagicHealOther; MagicSleep, MagicFear, MagicCharm, MagicStun, MagicHypnosis; MagicWordOfControl, "
        "MagicTameAnimal; MagicPoison, MagicDisease; MagicCallAnimals; MagicBloodlust)");
    if (!tickTried) InstallTick();
}

void Report(LogFn log)
{
    char line[520];
    sprintf_s(line, "SPELLSTATE installed=%d mirror=%d sent=%u received=%u replayed=%u refused=%u no_hero=%u npc_sent=%u npc_received=%u "
                    "npc_applied=%u npc_refused=%u taken_over=%u given_back=%u tick_hooked=%d tick_guard=%d ticks_run=%u ticks_skipped=%u "
                    "poisoned_skipped=%u",
              installed, mirror, sent, received, replayed, refused, noHero, npcSent, npcReceived, npcApplied, npcRefused, takenOver, givenBack,
              originalTick != nullptr, tickGuard, ticksRun, ticksSkipped, poisonedSkipped);
    log(line);
}

void MirrorOff(LogFn log)
{
    mirror = false;
    log("SPELLMIRROROFF casts on puppets and on other machines' NPCs are not sent");
}

void PuppetGuid(char const* player, LogFn log)
{
    char line[120];
    eCEntity* puppet = remote::PuppetOf(std::atoi(player));
    if (!puppet) { log("PUPPET refused no puppet of that player here"); return; }
    const eCEntityProxy proxy(puppet);
    bCPropertyID::bSCore core;
    std::memcpy(&core, &proxy.GetEntityID().GetCore(), 16);
    sprintf_s(line, "PUPPET player=%s guid=%08X-%04X-%04X-%02X%02X-%02X%02X%02X%02X%02X%02X", player, core.Data1, core.Data2, core.Data3,
              core.Data4[0], core.Data4[1], core.Data4[2], core.Data4[3], core.Data4[4], core.Data4[5], core.Data4[6], core.Data4[7]);
    log(line);
}

// QA: an NPC from a template 4 m in front of the local hero (an animal to tame, for instance), with its GUID.
void SpawnNpc(char const* name, LogFn log)
{
    char line[200];
    eCEntity* hero = remote::ControlledBody();
    const Template t(name);
    if (!hero || !t.IsValid()) { sprintf_s(line, "SPAWNNPC refused template %s valid=%d", name, t.IsValid() ? 1 : 0); log(line); return; }
    bCMatrix pose = hero->GetWorldMatrix();
    pose.AccessTranslation() = hero->GetWorldPosition() + hero->GetWorldMatrix().GetZAxis() * 400.0f;
    Entity npc = Entity::Spawn(t, pose);
    if (!npc.GetInstance()) { log("SPAWNNPC refused spawn failed"); return; }
    const eCEntityProxy proxy(npc.GetInstance());
    bCPropertyID::bSCore g;
    std::memcpy(&g, &proxy.GetEntityID().GetCore(), 16);
    sprintf_s(line, "SPAWNNPC name=%s guid=%08X-%04X-%04X-%02X%02X-%02X%02X%02X%02X%02X%02X", npc.GetName().GetText(), g.Data1, g.Data2,
              g.Data3, g.Data4[0], g.Data4[1], g.Data4[2], g.Data4[3], g.Data4[4], g.Data4[5], g.Data4[6], g.Data4[7]);
    log(line);
}

// QA: an NPC's status effects (1 burning, 4 poisoned, 8 diseased ...) and hit points, and whether it is mirrored here.
void StatusOf(char const* guid, LogFn log)
{
    char line[420];
    _GUID raw;
    uint8_t bytes[16];
    if (!ParseGuidText(guid, raw)) { log("STATUSOF refused bad GUID"); return; }
    std::memcpy(bytes, &raw, 16);
    Entity npc = ByGuid(bytes);
    if (!npc.GetInstance() || !npc.GetInstance()->GetPropertySet(eEPropertySetType_NPC)) { log("STATUSOF refused no such NPC"); return; }
    const bCVector at = npc.GetPosition();
    sprintf_s(line, "STATUSOF name=%s bits=%u hp=%d hp_max=%d poisoned_at=%u mirrored=%d owner=%u dead=%d ai_mode=%d pos=%.0f,%.0f,%.0f task=%s state=%s",
              npc.GetName().GetText(), static_cast<unsigned>(npc.NPC.StatusEffects), static_cast<int>(npc.DamageReceiver.HitPoints),
              static_cast<int>(npc.DamageReceiver.HitPointsMax), static_cast<unsigned>(npc.NPC.TimeStampPoisoned),
              npcs::IsMirrored(npc.GetInstance()), npcs::OwnerOf(npc.GetInstance()), npc.IsDead() ? 1 : 0,
              static_cast<int>(npc.Routine.AIMode), at.GetX(), at.GetY(), at.GetZ(), npc.Routine.GetCurrentTask().GetText(),
              npc.Routine.GetCurrentState().GetText());
    log(line);
}

// QA: set a local NPC's hit points ("GUID_HP"), so that the next hit of a test kills it through the game's damage path.
void NpcHp(char const* argument, LogFn log)
{
    char guid[64] = {};
    int hp = 0;
    _GUID raw;
    uint8_t bytes[16];
    char const* sep = std::strrchr(argument, '_');
    if (!sep || sep - argument >= static_cast<ptrdiff_t>(sizeof(guid)) || (hp = std::atoi(sep + 1)) < 1) { log("NPCHP refused GUID_HP"); return; }
    std::memcpy(guid, argument, sep - argument);
    if (!ParseGuidText(guid, raw)) { log("NPCHP refused bad GUID"); return; }
    std::memcpy(bytes, &raw, 16);
    Entity npc = ByGuid(bytes);
    if (!npc.GetInstance() || !npc.GetInstance()->GetPropertySet(eEPropertySetType_NPC) || npc.IsDead()) { log("NPCHP refused no living NPC"); return; }
    npc.DamageReceiver.HitPoints = hp;
    StatusOf(guid, log);
}

void TickGuardOff(LogFn log)
{
    tickGuard = false;
    log("TICKGUARDOFF mirrors and puppets run OnTick again");
}

// QA: the local hero's party, with each member's type (4 controlled, 5 summoned ...).
void Party(LogFn log)
{
    char line[260];
    eCEntity* hero = remote::ControlledBody();
    if (hero && hero->GetPropertySet(eEPropertySetType_Party)) {
        const bTValArray<eCEntity*> members = Entity(hero).Party.GetMembers(GEFalse);
        for (int i = 0; i < members.GetCount(); ++i) {
            Entity m(members.GetAt(i));
            if (!m.GetInstance()) continue;
            const eCEntityProxy proxy(m.GetInstance());
            bCPropertyID::bSCore g;
            std::memcpy(&g, &proxy.GetEntityID().GetCore(), 16);
            const bCVector& at = m.GetInstance()->GetWorldPosition();
            sprintf_s(line, "MEMBER name=%s guid=%08X-%04X-%04X-%02X%02X-%02X%02X%02X%02X%02X%02X type=%d task=%s at=%.0f_%.0f_%.0f",
                      m.GetName().GetText(), g.Data1, g.Data2, g.Data3, g.Data4[0], g.Data4[1], g.Data4[2], g.Data4[3], g.Data4[4],
                      g.Data4[5], g.Data4[6], g.Data4[7], static_cast<int>(static_cast<gEPartyMemberType>(m.Party.PartyMemberType)),
                      m.Routine.GetCurrentTask().GetText(), at.GetX(), at.GetY(), at.GetZ());
            log(line);
        }
    }
    log("PARTY end");
}

// QA for charm: what an NPC remembers of the player (MagicCharm sets all of it to 0: re/findings/npc-spells-c.md).
void NpcMemory(char const* guid, LogFn log)
{
    char line[220];
    _GUID raw;
    uint8_t bytes[16];
    if (!ParseGuidText(guid, raw)) { log("NPCMEMORY refused bad GUID"); return; }
    std::memcpy(bytes, &raw, 16);
    Entity npc = ByGuid(bytes);
    if (!npc.GetInstance() || !npc.GetInstance()->GetPropertySet(eEPropertySetType_NPC)) { log("NPCMEMORY refused no such NPC"); return; }
    sprintf_s(line, "NPCMEMORY name=%s attitude=%d last_player_ar=%d attack_reason=%d last_fight=%d last_crime=%d last_fight_time=%.0f",
              npc.GetName().GetText(), static_cast<int>(static_cast<gEAttitude>(npc.NPC.AttitudeToPlayer2)),
              static_cast<int>(static_cast<gEAttackReason>(npc.NPC.LastPlayerAR)), static_cast<int>(static_cast<gEAttackReason>(npc.NPC.AttackReason)),
              static_cast<int>(static_cast<gELastFightAgainstPlayer>(npc.NPC.LastFightAgainstPlayer)),
              static_cast<int>(static_cast<gEPlayerCrime>(npc.NPC.LastPlayerCrime)), static_cast<float>(npc.NPC.LastFightTimestamp));
    log(line);
}

// QA for charm: the NPC holds a grudge against the player (hostile, attacked, a lost fight), as after a quarrel.
void NpcGrudge(char const* guid, LogFn log)
{
    _GUID raw;
    uint8_t bytes[16];
    if (!ParseGuidText(guid, raw)) { log("NPCGRUDGE refused bad GUID"); return; }
    std::memcpy(bytes, &raw, 16);
    Entity npc = ByGuid(bytes);
    if (!npc.GetInstance() || !npc.GetInstance()->GetPropertySet(eEPropertySetType_NPC)) { log("NPCGRUDGE refused no such NPC"); return; }
    npc.NPC.AttitudeToPlayer2 = gEAttitude_Angry;
    npc.NPC.LastPlayerAR = gEAttackReason_ReactToWeapon;
    npc.NPC.LastPlayerCrime = gEPlayerCrime_Attack;
    npc.NPC.LastFightAgainstPlayer = gELastFightAgainstPlayer_Lost;
    log("NPCGRUDGE set");
}

void SetHp(char const* value, LogFn log)
{
    char line[120];
    eCEntity* hero = gCSession::GetInstance().GetHero();
    if (!hero) { log("SETHP refused no hero"); return; }
    Entity self(hero);
    const int before = self.PlayerMemory.GetHitPoints();
    self.PlayerMemory.SetHitPoints(std::atoi(value));
    sprintf_s(line, "SETHP before=%d after=%d max=%d", before, self.PlayerMemory.GetHitPoints(), self.PlayerMemory.GetHitPointsMax());
    log(line);
}

void Locks(LogFn log)
{
    char line[300];
    eCEntity* hero = gCSession::GetInstance().GetHero();
    if (!hero) { log("LOCKS end refused no hero"); return; }
    const bCVector at = hero->GetWorldPosition();
    unsigned count = 0;
    bTSmallArray<eCEntity*> const* every = world::ProcessedEntities();
    for (GEU16 i = 0; every && i < every->GetCount(); ++i) {
        eCEntity* e = every->GetAt(i);
        if (!e || !e->GetPropertySet(eEPropertySetType_Lock) || e->GetPropertySet(eEPropertySetType_NPC) ||
            e->GetPropertySet(eEPropertySetType_Item))
            continue;
        const float distance = (e->GetWorldPosition() - at).GetMagnitude();
        if (distance > 3000.0f) continue;
        Entity lock(e);
        const Template key = lock.Lock.GetKey();
        sprintf_s(line, "LOCK name=%s distance=%.0f door=%d locked=%d difficulty=%d key=%d", e->GetName().GetText(), distance,
                  e->GetPropertySet(eEPropertySetType_Door) ? 1 : 0, lock.Lock.IsLocked() ? 1 : 0,
                  static_cast<int>(lock.Lock.Difficulty), key.IsValid() ? 1 : 0);
        log(line);
        ++count;
    }
    sprintf_s(line, "LOCKS end count=%u", count);
    log(line);
}
// QA (plan 3.6): which scripts a spell item runs, "It_Spell_A,It_Spell_B": SPELLFUNC lines. A missing item is lent to
// the hero for the look and taken back.
void SpellFunc(char const* names, LogFn log)
{
    Entity hero(gCSession::GetInstance().GetHero());
    std::string list = names;
    for (size_t start = 0; start < list.size();) {
        size_t end = list.find(',', start);
        if (end == std::string::npos) end = list.size();
        const std::string name = list.substr(start, end - start);
        start = end + 1;
        char line[300];
        const Template item(bCString(name.c_str()));
        if (!item.IsValid() || !hero.GetInstance()) {
            sprintf_s(line, "SPELLFUNC item=%s valid=0", name.c_str());
            log(line);
            continue;
        }
        const bool lent = hero.Inventory.GetItemAmount(item) < 1;
        if (lent) hero.Inventory.CreateItems(item, 0, 1);
        const GEInt stack = hero.Inventory.FindStackIndex(item);
        Entity spell = stack >= 0 ? hero.Inventory.GetTemplateItem(stack).Item.GetSpell() : Entity();
        const bCString cast = spell.GetInstance() ? static_cast<bCString>(spell.Magic.FuncOnCast) : bCString("-");
        const bCString hit = spell.GetInstance() ? static_cast<bCString>(spell.Magic.FuncOnTargetHit) : bCString("-");
        if (lent && stack >= 0) hero.Inventory.DeleteItems(stack, 1);
        sprintf_s(line, "SPELLFUNC item=%s valid=1 on_cast=%s on_target_hit=%s", name.c_str(), cast.GetText(), hit.GetText());
        log(line);
    }
    log("SPELLFUNC end");
}
}
