// SPDX-License-Identifier: GPL-3.0-or-later
#include "summon.h"

#include "body.h"
#include "log.h"
#include "net.h"
#include "remote.h"

#include <Game.h>
#include <Script.h>
#include <Windows.h>
#include <cstdio>
#include <cstring>
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
         copiesBound = 0, copiesUnbound = 0, goneApplied = 0, copiesEndedAlone = 0;

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

void Install()
{
    const DWORD now = GetTickCount();
    if ((installed && probeInstalled) || !gCSession::GetInstance().IsGameRunning() || now - lastTry < 1000) return;
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

void Tick()
{
    Install();
    if (!pending.empty()) Resolve();
    CheckKnown();
}

void Report(LogFn log)
{
    char line[400];
    sprintf_s(line, "SUMMONSTATE installed=%d watch=%d mirror=%d copy_owner=%d announced=%u unresolved=%u gone_sent=%u received=%u replayed=%u "
                    "spawned_direct=%u no_puppet=%u copies_bound=%u copies_unbound=%u gone_applied=%u copies_ended_alone=%u known=%u "
                    "hits_applied=%u hits_blocked=%u",
              installed, probeInstalled, mirror, copyOwner, announced, unresolved, goneSent, received, replayed, spawnedDirect, noPuppet,
              copiesBound, copiesUnbound, goneApplied, copiesEndedAlone, static_cast<unsigned>(known.size()), hitsApplied, hitsBlocked);
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
}
