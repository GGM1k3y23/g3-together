// SPDX-License-Identifier: GPL-3.0-or-later
#include "summon.h"

#include "body.h"
#include "log.h"
#include "net.h"
#include "npcs.h"
#include "remote.h"

#include <Game.h>
#include <Script.h>
#include <Windows.h>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <algorithm>
#include <vector>

namespace g3t::summon {
namespace {
bool installed = false, probeInstalled = false;
DWORD lastTry = 0, lastCheck = 0;
gFScript originalSummon = nullptr;
gFScript originalHit = nullptr;
bool mirror = true;      // QA negative control: MirrorOff
bool copyOwner = true;   // QA negative control: CopyHeroOff (the copy belongs to our own hero, as a naive copy would)
uint32_t counter = 0;
unsigned announced = 0, unresolved = 0, goneSent = 0, received = 0, replayed = 0, spawnedDirect = 0, noPuppet = 0,
         copiesBound = 0, copiesUnbound = 0, goneApplied = 0, copiesEndedAlone = 0, areaTicksSkipped = 0, areaTicksRun = 0;
// Area spells (hailstorm, fire rain, meteor: re/findings/area-spell-xp-c.md): their states call MagicAreaDamageTick
// directly, which picks targets by its own dice and judges the hits through DoLogicalDamage and AssessHit, before our
// AddHitPoints filter: a copy could kill an NPC there (ZS_RagDollDead) and the caster's kill went unpaid. A copy's tick
// is skipped; its weather and effects go on. QA areatickon lets copies tick again (negative control).
gFScript originalAreaTick = nullptr;
bool areaInstalled = false, areaSkip = true;

// Summoned creatures (re/findings/summons-b.md, summon-lifecycle-c.md): the scripts spawn NPCs into the caster's party
// as Summoned (5). The caster announces each new one with its GUID; the other machine spawns a copy and gives it that
// GUID with eCNode::SetID (it re-registers the entity under the new ID: entity-guids-c.md), so npcs.cpp mirrors it like
// any NPC from the snapshots of the machine that simulates it. A client's creature is its own, as a companion it took
// over (npcs::Claim); the host marks it so (npcs::OnClaim) and mirrors it from that client.
char const* const CreatureScripts[] = {"MagicSummonSkeleton", "MagicSummonDemon", "MagicSummonGolem", "MagicSummonGoblin",
                                       "MagicSummonMonster", "MagicSummonArmyOfDarkness", "MagicSummonArmyOfCuteness",
                                       "MagicSummonCompanion"};
constexpr int CreatureCount = sizeof(CreatureScripts) / sizeof(CreatureScripts[0]);
gFScript creatureOriginals[CreatureCount] = {};
bool creaturesInstalled = false;
unsigned creaturesAnnounced = 0, creatureCopies = 0, creatureCopyFailed = 0, creatureCopiesRemoved = 0;
// A copy follows its original through the snapshots. When they stop (the original was replaced, dismissed, left the
// area or vanished), npcs.cpp gives a stale mirror its own AI back: right for an NPC of the world, wrong for a copy,
// which would live on as a free creature. So a copy goes once it is no longer mirrored (and one never mirrored goes
// after a while). Its death comes as a snapshot: the copy dies as a corpse, then goes the same way.
struct CreatureCopy {
    uint8_t guid[16];
    DWORD created = 0;
    DWORD mirroredSeen = 0;  // last time npcs.cpp drove it
};
std::vector<CreatureCopy> creatureCopyList;
DWORD lastCopyCheck = 0;
// eCNode::SetID(bCPropertyID) is __thiscall with the 20-byte ID by value, ret 14h; __fastcall (node, edx, id) takes the
// same registers and stack, and the callee destroys the argument in both.
using SetIdFn = void(__fastcall*)(void* node, void* edx, bCPropertyID id);
SetIdFn setId = nullptr;

// Waiting for a summon to show up among the world's entities (it does a frame after MagicSummon spawned it).
struct Pending {
    eCEntity* owner = nullptr;  // the caster (own summon) or the caster's puppet (copy)
    uint32_t netId = 0;         // 0 while it is our own summon without an ID yet
    uint8_t target[16] = {};
    char spellItem[40] = {};
    char spawn[40] = {};
    DWORD since = 0;
};
std::vector<Pending> pending;

struct Known {
    uint32_t netId;
    uint8_t guid[16];
    bool own;
    DWORD removeAt = 0;  // copy: the caster's ended; remove it then if it has not ended by itself
};
std::vector<Known> known;

// QA: every hit while a summon acts (until 4 s after the last one appeared), with the hit points before and after;
// the latest MaxHitLines of them.
DWORD watchUntil = 0;
constexpr int MaxHitLines = 24;
char hitLines[MaxHitLines][200];
int hitLineCount = 0;
unsigned hitsApplied = 0, hitsBlocked = 0;

template <typename T>
T* Find(bTValMap<bCString, T*>& map, char const* name)
{
    T* found = nullptr;
    return map.GetAt(bCString(name), found) ? found : nullptr;
}

void Guid(eCEntity* entity, uint8_t out[16])
{
    const eCEntityProxy proxy(entity);
    std::memcpy(out, &proxy.GetEntityID().GetCore(), 16);
}

Entity ByGuid(uint8_t const guid[16])
{
    static const uint8_t none[16] = {};
    if (!std::memcmp(guid, none, 16)) return Entity();
    _GUID raw;
    std::memcpy(&raw, guid, 16);
    return Entity(bCPropertyID(bCGuid(raw)));
}

bool IsKnown(eCEntity* entity)
{
    uint8_t guid[16];
    Guid(entity, guid);
    for (Known const& k : known)
        if (!std::memcmp(k.guid, guid, 16)) return true;
    return false;
}

bool InOurModule(void const* address)
{
    HMODULE ours = nullptr, theirs = nullptr;
    const DWORD flags = GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT;
    GetModuleHandleExA(flags, reinterpret_cast<LPCSTR>(&InOurModule), &ours);
    GetModuleHandleExA(flags, static_cast<LPCSTR>(address), &theirs);
    return ours && ours == theirs;
}

int HitPoints(Entity& e)
{
    eCEntity* instance = e.GetInstance();
    if (!instance) return -1;
    if (instance->GetPropertySet(eEPropertySetType_PlayerMemory)) return e.PlayerMemory.GetHitPoints();
    if (instance->GetPropertySet(eEPropertySetType_DamageReceiver)) return e.DamageReceiver.HitPoints;
    return -1;
}

char const* NameOf(Entity const& e) { return e.GetInstance() ? e.GetName().GetText() : "-"; }

GEInt GE_STDCALL WrapSummon(gCScriptProcessingUnit* spu, GELPVoid self, GELPVoid other, GEInt args)
{
    Entity caster = self ? *static_cast<Entity*>(self) : Entity(spu ? spu->GetSelfEntity() : nullptr);
    Entity target = other ? *static_cast<Entity*>(other) : Entity(spu ? spu->GetOtherEntity() : nullptr);
    Pending p;
    const bool own = mirror && caster.GetInstance() && caster.GetInstance() == remote::ControlledBody();
    if (own) {
        p.owner = caster.GetInstance();
        if (target.GetInstance()) Guid(target.GetInstance(), p.target);
        const Entity item = caster.Inventory.GetItemFromSlot(gESlot_RightHand);
        strncpy_s(p.spellItem, NameOf(item), _TRUNCATE);
        const Entity spell = caster.Interaction.GetSpell();
        const Entity spawn = spell.GetInstance() ? spell.Magic.GetSpawn() : Entity();
        strncpy_s(p.spawn, NameOf(spawn), _TRUNCATE);
        p.since = GetTickCount();
    }
    const GEInt result = originalSummon(spu, self, other, args);
    if (own && *p.spawn != '-') {
        pending.push_back(p);
        watchUntil = GetTickCount() + 4000;
    }
    return result;
}

// Sits behind the combat filter and only watches: was a hit applied here (hit points changed) or not.
GEInt GE_STDCALL WatchHit(gCScriptProcessingUnit* spu, GELPVoid self, GELPVoid other, GEInt args)
{
    if (args >= 0 || static_cast<int32_t>(GetTickCount() - watchUntil) > 0) return originalHit(spu, self, other, args);
    Entity victim = self ? *static_cast<Entity*>(self) : Entity(spu ? spu->GetSelfEntity() : nullptr);
    Entity source = other ? *static_cast<Entity*>(other) : Entity(spu ? spu->GetOtherEntity() : nullptr);
    const int before = HitPoints(victim);
    const GEInt result = originalHit(spu, self, other, args);
    const int after = HitPoints(victim);
    const bool applied = before != after;
    applied ? ++hitsApplied : ++hitsBlocked;
    {
        const Entity owner = source.GetInstance() ? source.Interaction.GetOwner() : Entity();
        sprintf_s(hitLines[hitLineCount++ % MaxHitLines], "SMNHIT victim=%s source=%s owner=%s owner_puppet_of=%d delta=%d hp=%d..%d applied=%d",
                  NameOf(victim), NameOf(source), NameOf(owner), owner.GetInstance() ? remote::PuppetOwner(owner.GetInstance()) : 0,
                  args, before, after, applied ? 1 : 0);
    }
    return result;
}

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

std::vector<eCEntity*> SummonedOf(eCEntity* caster)
{
    std::vector<eCEntity*> out;
    if (!caster || !caster->GetPropertySet(eEPropertySetType_Party)) return out;
    const bTValArray<eCEntity*> members = Entity(caster).Party.GetMembers(GEFalse);
    for (int i = 0; i < members.GetCount(); ++i) {
        eCEntity* m = members.GetAt(i);
        if (m && m->GetPropertySet(eEPropertySetType_Party) &&
            static_cast<gEPartyMemberType>(Entity(m).Party.PartyMemberType) == gEPartyMemberType_Summoned)
            out.push_back(m);
    }
    return out;
}

void AnnounceCreature(eCEntity* creature)
{
    net::SummonNote note;
    note.op = net::SummonCreature;
    note.netId = (static_cast<uint32_t>(net::LocalId()) << 24) | (++counter & 0xFFFFFF);
    Guid(creature, note.guid);
    const bCVector& at = creature->GetWorldPosition();
    const bCQuaternion q = creature->GetWorldRotation();
    note.position[0] = at.GetX(), note.position[1] = at.GetY(), note.position[2] = at.GetZ();
    note.rotation[0] = q.GetX(), note.rotation[1] = q.GetY(), note.rotation[2] = q.GetZ(), note.rotation[3] = q.GetW();
    strncpy_s(note.spawn, NameOf(Entity(Entity(creature).GetTemplate())), _TRUNCATE);
    if (!net::IsHost()) npcs::Claim(creature);  // ours: we send it, the host's snapshots of it are ignored here
    net::SendSummon(note);
    ++creaturesAnnounced;
    Logf("summon: own creature %s is net %08X", note.spawn, note.netId);
}

template <int I>
GEInt GE_STDCALL WrapCreature(gCScriptProcessingUnit* spu, GELPVoid self, GELPVoid other, GEInt args)
{
    eCEntity* caster = self ? static_cast<Entity*>(self)->GetInstance() : (spu ? spu->GetSelfEntity() : nullptr);
    const bool own = mirror && caster && caster == remote::ControlledBody();
    const std::vector<eCEntity*> before = own ? SummonedOf(caster) : std::vector<eCEntity*>();
    const GEInt result = creatureOriginals[I](spu, self, other, args);
    if (own)
        for (eCEntity* creature : SummonedOf(caster))
            if (std::find(before.begin(), before.end(), creature) == before.end()) AnnounceCreature(creature);
    return result;
}

const gFScript creatureWrappers[CreatureCount] = {&WrapCreature<0>, &WrapCreature<1>, &WrapCreature<2>, &WrapCreature<3>,
                                                  &WrapCreature<4>, &WrapCreature<5>, &WrapCreature<6>, &WrapCreature<7>};

// The other machine's creature: a copy under its GUID, which npcs.cpp then drives from that machine's snapshots.
void CopyCreature(uint8_t from, net::SummonNote const& note)
{
    _GUID raw;
    std::memcpy(&raw, note.guid, 16);
    if (ByGuid(note.guid).GetInstance()) {  // that GUID is taken here: SetID would hide the other entity
        ++creatureCopyFailed;
        Logf("summon: creature net %08X from player %u: its GUID exists here already", note.netId, from);
        return;
    }
    const Template creatureTemplate(note.spawn);
    eCEntity* base = remote::PuppetOf(from) ? remote::PuppetOf(from) : gCSession::GetInstance().GetHero();
    if (!setId || !creatureTemplate.IsValid() || !base) {
        ++creatureCopyFailed;
        Logf("summon: creature net %08X from player %u: no copy (template %s, SetID %d)", note.netId, from, note.spawn, setId != nullptr);
        return;
    }
    Entity copy = Entity::Spawn(creatureTemplate, body::Pose(*base, note.position, note.rotation));
    if (!copy.GetInstance()) {
        ++creatureCopyFailed;
        return;
    }
    setId(copy.GetInstance(), nullptr, bCPropertyID(bCGuid(raw)));
    if (ByGuid(note.guid).GetInstance() != copy.GetInstance()) {
        ++creatureCopyFailed;
        Logf("summon: creature net %08X: SetID did not take, copy removed", note.netId);
        copy.Kill();
        return;
    }
    if (net::IsHost()) npcs::OnClaim(note.guid, from, true);  // the client simulates it and sends it
    npcs::Foreign(note.guid);  // before its first snapshot arrives it must not go out as one of ours
    npcs::Rebind(note.guid);
    CreatureCopy c;
    std::memcpy(c.guid, note.guid, 16);
    c.created = GetTickCount();
    creatureCopyList.push_back(c);
    ++creatureCopies;
    Logf("summon: creature net %08X from player %u: %s copy under its GUID", note.netId, from, note.spawn);
}

GEInt GE_STDCALL HookAreaTick(gCScriptProcessingUnit* spu, GELPVoid self, GELPVoid other, GEInt args)
{
    Entity cloud = self ? *static_cast<Entity*>(self) : Entity(spu ? spu->GetSelfEntity() : nullptr);
    const Entity owner = cloud.GetInstance() ? cloud.Interaction.GetOwner() : Entity();
    if (areaSkip && owner.GetInstance() && remote::PuppetOwner(owner.GetInstance())) {
        ++areaTicksSkipped;
        return 0;
    }
    ++areaTicksRun;
    return originalAreaTick(spu, self, other, args);
}

void Install()
{
    const DWORD now = GetTickCount();
    if ((installed && probeInstalled && areaInstalled && creaturesInstalled) || !gCSession::GetInstance().IsGameRunning() || now - lastTry < 1000) return;
    lastTry = now;
    auto& scripts = GetScriptAdminExt().GetScripts();
    if (!installed) {
        if (gSScript* script = Find(scripts, "MagicSummon")) {
            originalSummon = script->m_funcScript;
            script->m_funcScript = &WrapSummon;
            installed = true;
            Log("summon: MagicSummon wrapper installed");
        }
    }
    // Behind the combat filter (combat.cpp installs it first), so that it sees what the filter let through.
    if (!probeInstalled) {
        gSScript* script = Find(scripts, "AddHitPoints");
        if (script && InOurModule(reinterpret_cast<void const*>(script->m_funcScript))) {
            originalHit = script->m_funcScript;
            script->m_funcScript = &WatchHit;
            probeInstalled = true;
            Log("summon: AddHitPoints watch installed behind the combat filter");
        }
    }
    if (!creaturesInstalled) {
        for (int i = 0; i < CreatureCount; ++i) {
            gSScript* script = Find(scripts, CreatureScripts[i]);
            if (script && !creatureOriginals[i]) {
                creatureOriginals[i] = script->m_funcScript;
                script->m_funcScript = creatureWrappers[i];
            }
        }
        setId = reinterpret_cast<SetIdFn>(GetProcAddress(GetModuleHandleA("Engine.dll"), "?SetID@eCNode@@IAEXVbCPropertyID@@@Z"));
        creaturesInstalled = true;
        Logf("summon: creature summon wrappers installed, SetID %s", setId ? "found" : "missing");
    }
    // MagicAreaDamageTick (FUN_1005a0a0 in this build) starts with push ebp; mov ebp,esp; and esp,-8; sub esp,0x41c and
    // returns with ret 0x10; the states call it directly, so it is hooked in place.
    if (!areaInstalled) {
        if (gSScript* script = Find(scripts, "MagicAreaDamageTick")) {
            static const unsigned char expected[] = {0x55, 0x8b, 0xec, 0x83, 0xe4, 0xf8, 0x81, 0xec, 0x1c, 0x04, 0x00, 0x00};
            auto* code = reinterpret_cast<unsigned char*>(script->m_funcScript);
            if (code && std::memcmp(code, expected, sizeof(expected)) == 0)
                originalAreaTick = reinterpret_cast<gFScript>(JumpTo(code, reinterpret_cast<void*>(&HookAreaTick)));
            areaInstalled = true;
            Log(originalAreaTick ? "summon: MagicAreaDamageTick hooked (copies do no damage)" : "summon: MagicAreaDamageTick not hooked");
        }
    }
}

void Announce(Pending const& p, eCEntity* entity)
{
    net::SummonNote note;
    note.op = net::SummonSpawned;
    note.netId = (static_cast<uint32_t>(net::LocalId()) << 24) | (++counter & 0xFFFFFF);
    std::memcpy(note.target, p.target, 16);
    const bCVector& at = entity->GetWorldPosition();
    const bCQuaternion q = entity->GetWorldRotation();
    const float position[3] = {at.GetX(), at.GetY(), at.GetZ()};
    const float rotation[4] = {q.GetX(), q.GetY(), q.GetZ(), q.GetW()};
    std::memcpy(note.position, position, sizeof(position));
    std::memcpy(note.rotation, rotation, sizeof(rotation));
    strncpy_s(note.spellItem, p.spellItem, _TRUNCATE);
    strncpy_s(note.spawn, p.spawn, _TRUNCATE);
    net::SendSummon(note);
    Known k{note.netId, {}, true};
    Guid(entity, k.guid);
    known.push_back(k);
    ++announced;
    Logf("summon: own %s (%s) is net %08X at %.0f %.0f %.0f", p.spawn, p.spellItem, note.netId, at.GetX(), at.GetY(), at.GetZ());
}

void Bind(Pending const& p, eCEntity* entity)
{
    Known k{p.netId, {}, false};
    Guid(entity, k.guid);
    known.push_back(k);
    ++copiesBound;
    if (!copyOwner) Entity(entity).Interaction.SetOwner(Entity(gCSession::GetInstance().GetHero()));
    Logf("summon: copy of net %08X is %s, owner %s", p.netId, p.spawn, NameOf(Entity(entity).Interaction.GetOwner()));
}

// Find each pending summon among the world's entities: its template's name, its owner, not known yet.
void Resolve()
{
    const DWORD now = GetTickCount();
    const bTObjArray<Entity> all = Entity::GetEntities();
    for (size_t i = 0; i < pending.size();) {
        Pending const& p = pending[i];
        eCEntity* found = nullptr;
        for (int j = 0; j < all.GetCount() && !found; ++j) {
            Entity e = all.GetAt(j);
            eCEntity* instance = e.GetInstance();
            if (!instance || std::strcmp(e.GetName().GetText(), p.spawn) || !instance->GetPropertySet(eEPropertySetType_Interaction)) continue;
            if (e.Interaction.GetOwner().GetInstance() != p.owner || IsKnown(instance)) continue;
            found = instance;
        }
        if (found) {
            p.netId ? Bind(p, found) : Announce(p, found);
        } else if (now - p.since < 2000) {
            ++i;
            continue;
        } else if (p.netId) {
            ++copiesUnbound;
            Logf("summon: no copy of net %08X (%s) showed up", p.netId, p.spawn);
        } else {
            ++unresolved;
            Logf("summon: own %s did not show up", p.spawn);
        }
        pending.erase(pending.begin() + i);
    }
}

// Every 250 ms: own summons that ended are announced as gone; copies that ended by themselves are forgotten.
void CheckKnown()
{
    const DWORD now = GetTickCount();
    if (known.empty() || now - lastCheck < 250) return;
    lastCheck = now;
    for (size_t i = 0; i < known.size();) {
        Entity e = ByGuid(known[i].guid);
        if (e.GetInstance() && known[i].removeAt && static_cast<int32_t>(now - known[i].removeAt) >= 0) {
            e.Kill();
            ++goneApplied;
            known.erase(known.begin() + i);
            continue;
        }
        if (e.GetInstance()) { ++i; continue; }
        if (known[i].own) {
            net::SummonNote note;
            note.op = net::SummonGone;
            note.netId = known[i].netId;
            net::SendSummon(note);
            ++goneSent;
        } else {
            ++copiesEndedAlone;
        }
        known.erase(known.begin() + i);
    }
}

void OnSummon(uint8_t from, net::SummonNote const& note)
{
    ++received;
    // A position from the network becomes a spawn pose: no NaN or infinity (Fable review 22.10.).
    bool finite = true;
    for (float v : note.position) finite = finite && std::isfinite(v);
    for (float v : note.rotation) finite = finite && std::isfinite(v);
    if (!finite) {
        Logf("summon: net %08X from player %u: not a number in its pose", note.netId, from);
        return;
    }
    if (note.op == net::SummonCreature) {
        CopyCreature(from, note);
        return;
    }
    if (note.op == net::SummonGone) {
        for (size_t i = 0; i < known.size(); ++i) {
            // The copy began a little later than the caster's: let it play on for a moment (a meteor's fall).
            if (!known[i].own && known[i].netId == note.netId) known[i].removeAt = GetTickCount() + 1500;
        }
        return;
    }
    eCEntity* puppetBody = remote::PuppetOf(from);
    if (!puppetBody) {
        ++noPuppet;
        Logf("summon: net %08X from player %u: no puppet here", note.netId, from);
        return;
    }
    // The copy is the caster's: the game's own MagicSummon on the puppet, with the puppet holding the same spell.
    Entity puppet(puppetBody);
    const Template item(note.spellItem);
    if (item.IsValid() && puppet.Inventory.GetItemAmount(item) < 1) puppet.Inventory.CreateItems(item, 0, 1);
    const GEInt stack = item.IsValid() ? puppet.Inventory.FindStackIndex(item) : -1;
    const Entity spell = stack >= 0 ? puppet.Inventory.GetTemplateItem(stack).Item.GetSpell() : Entity();
    Entity target = ByGuid(note.target);
    Pending p;
    p.owner = puppetBody;
    p.netId = note.netId;
    std::memcpy(p.target, note.target, 16);
    strncpy_s(p.spellItem, note.spellItem, _TRUNCATE);
    strncpy_s(p.spawn, note.spawn, _TRUNCATE);
    p.since = GetTickCount();
    if (spell.GetInstance() && target.GetInstance()) {
        puppet.Interaction.SetSpell(spell);
        GetScriptAdminExt().CallScript(bCString("MagicSummon"), puppetBody, target.GetInstance(), stack);
        ++replayed;
    } else {
        // No target here (out of this machine's range) or no spell: the summon's template where the caster's was.
        Entity copy = Entity::Spawn(Template(note.spawn), body::Pose(*puppetBody, note.position, note.rotation));
        if (copy.GetInstance()) {
            copy.Interaction.SetOwner(puppet);
            if (spell.GetInstance()) copy.Interaction.SetSpell(spell);
        }
        ++spawnedDirect;
    }
    pending.push_back(p);
    watchUntil = GetTickCount() + 4000;
    Logf("summon: net %08X from player %u: %s (%s) target=%s", note.netId, from, note.spawn, note.spellItem, NameOf(target));
}
}

void Start() { net::SetSummonHandler(&OnSummon); }

void WatchHits() { watchUntil = GetTickCount() + 4000; }

void CheckCreatureCopies()
{
    const DWORD now = GetTickCount();
    if (creatureCopyList.empty() || now - lastCopyCheck < 250) return;
    lastCopyCheck = now;
    for (size_t i = 0; i < creatureCopyList.size();) {
        CreatureCopy& c = creatureCopyList[i];
        Entity copy = ByGuid(c.guid);
        if (!copy.GetInstance()) {
            creatureCopyList.erase(creatureCopyList.begin() + i);
            continue;
        }
        if (npcs::IsMirrored(copy.GetInstance())) c.mirroredSeen = now;
        const bool lost = c.mirroredSeen ? now - c.mirroredSeen > 1000 : now - c.created > 8000;
        if (lost) {
            Logf("summon: creature copy %s removed (its original is no longer sent)", NameOf(copy));
            copy.Kill();
            ++creatureCopiesRemoved;
            creatureCopyList.erase(creatureCopyList.begin() + i);
            continue;
        }
        ++i;
    }
}

void Tick()
{
    Install();
    if (!pending.empty()) Resolve();
    CheckKnown();
    CheckCreatureCopies();
}

void Report(LogFn log)
{
    char line[640];
    sprintf_s(line, "SUMMONSTATE installed=%d watch=%d mirror=%d copy_owner=%d announced=%u unresolved=%u gone_sent=%u received=%u replayed=%u "
                    "spawned_direct=%u no_puppet=%u copies_bound=%u copies_unbound=%u gone_applied=%u copies_ended_alone=%u known=%u "
                    "hits_applied=%u hits_blocked=%u area_hooked=%d area_ticks_skipped=%u area_ticks_run=%u creatures_announced=%u "
                    "creature_copies=%u creature_copy_failed=%u creature_copies_removed=%u",
              installed, probeInstalled, mirror, copyOwner, announced, unresolved, goneSent, received, replayed, spawnedDirect, noPuppet,
              copiesBound, copiesUnbound, goneApplied, copiesEndedAlone, static_cast<unsigned>(known.size()), hitsApplied, hitsBlocked,
              originalAreaTick != nullptr, areaTicksSkipped, areaTicksRun, creaturesAnnounced, creatureCopies, creatureCopyFailed,
              creatureCopiesRemoved);
    log(line);
    for (int i = hitLineCount > MaxHitLines ? hitLineCount - MaxHitLines : 0; i < hitLineCount; ++i) log(hitLines[i % MaxHitLines]);
    log("SUMMONSTATE end");
}

void CopyHeroOff(LogFn log)
{
    copyOwner = false;
    log("COPYHEROOFF copies of others' summons belong to our own hero");
}

void MirrorOff(LogFn log)
{
    mirror = false;
    log("SUMMONMIRROROFF own summons are not announced");
}

void Creatures(LogFn log)
{
    char line[300];
    eCEntity* hero = remote::ControlledBody();
    for (eCEntity* c : SummonedOf(hero)) {
        const eCEntityProxy proxy(c);
        bCPropertyID::bSCore g;
        std::memcpy(&g, &proxy.GetEntityID().GetCore(), 16);
        const bCVector& at = c->GetWorldPosition();
        sprintf_s(line, "CREATURE own name=%s guid=%08X-%04X-%04X-%02X%02X-%02X%02X%02X%02X%02X%02X at=%.0f_%.0f_%.0f dead=%d", NameOf(Entity(c)),
                  g.Data1, g.Data2, g.Data3, g.Data4[0], g.Data4[1], g.Data4[2], g.Data4[3], g.Data4[4], g.Data4[5], g.Data4[6], g.Data4[7],
                  at.GetX(), at.GetY(), at.GetZ(), Entity(c).IsDead() ? 1 : 0);
        log(line);
    }
    log("CREATURES end");
}

void AreaTickOn(LogFn log)
{
    areaSkip = false;
    log("AREATICKON copies of area spells pick targets and judge hits again");
}
}
