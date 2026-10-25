// SPDX-License-Identifier: GPL-3.0-or-later
#include "pickpocket.h"

#include "ai.h"
#include "log.h"
#include "net.h"
#include "npcs.h"

#include <Game.h>
#include <Script.h>
#include <Windows.h>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace g3t::pickpocket {
namespace {
// PickPocket (Script_Game.dll 0x3dd70, self = thief, other = victim; re/out/p8-decompiled.c): the dialog command calls the
// function directly, not by name, so only the jump into the function itself sees it.
constexpr uintptr_t PickRva = 0x3dd70;
constexpr uint8_t KindPicked = 12;  // net::WorldState kind
gFScript originalPick = nullptr;
bool installed = false, native = false;
DWORD lastTry = 0;
bool enabled = true;  // QA negative control: pickpocketoff
unsigned tries = 0, successes = 0, sent = 0, applied = 0, already = 0;

unsigned char* NativePick()
{
    static const unsigned char expected[] = {0x55, 0x8b, 0xec, 0x83, 0xe4, 0xf8, 0x81, 0xec, 0xcc, 0x02, 0x00, 0x00};
    HMODULE module = GetModuleHandleA("Script_Game.dll");
    if (!module) return nullptr;
    auto* code = reinterpret_cast<unsigned char*>(module) + PickRva;
    return std::memcmp(code, expected, sizeof(expected)) == 0 ? code : nullptr;
}

bool HasDialog(eCEntity* entity) { return entity && entity->GetPropertySet(eEPropertySetType_Dialog); }

bool Picked(eCEntity* entity) { return HasDialog(entity) && Entity(entity).Dialog.PickedPocket; }

void GuidOf(eCEntity* entity, uint8_t out[16])
{
    const eCEntityProxy proxy(entity);
    std::memcpy(out, &proxy.GetEntityID().GetCore(), 16);
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

Entity ByGuid(char const* text)
{
    _GUID raw;
    return ParseGuid(text, raw) ? Entity(bCPropertyID(bCGuid(raw))) : Entity();
}

int ItemCount(Entity& who)
{
    int total = 0;
    for (GEInt i = 0; i < who.Inventory.GetStackCount(); ++i) total += who.Inventory.GetAmount(i);
    return total;
}

GEInt GE_STDCALL Detour(gCScriptProcessingUnit* spu, GELPVoid self, GELPVoid other, GEInt args)
{
    eCEntity* victim = other ? static_cast<Entity*>(other)->GetInstance() : (spu ? spu->GetOtherEntity() : nullptr);
    const bool before = Picked(victim);
    const GEInt result = originalPick(spu, self, other, args);
    ++tries;
    if (!victim || before || !Picked(victim)) return result;
    ++successes;
    if (!enabled || !net::Connected()) return result;
    net::WorldState s{};
    s.kind = KindPicked;
    GuidOf(victim, s.guid);
    s.guard = net::LocalId();
    net::SendWorldState(s, 0);
    ++sent;
    Logf("pickpocket: this player picked %s's pocket; the other copies are marked", Entity(victim).GetName().GetText());
    return result;
}
}

void Tick()
{
    if (installed || !gCSession::GetInstance().IsGameRunning()) return;
    const DWORD now = GetTickCount();
    if (now - lastTry < 1000) return;
    lastTry = now;
    gSScript* script = nullptr;
    if (!GetScriptAdminExt().GetScripts().GetAt(bCString("PickPocket"), script) || !script || !script->m_funcScript) return;
    if (unsigned char* code = NativePick(); code && reinterpret_cast<void*>(script->m_funcScript) == code) {
        originalPick = reinterpret_cast<gFScript>(ai::JumpTo(code, reinterpret_cast<void*>(&Detour)));
        native = originalPick != nullptr;
    }
    installed = true;
    Logf("pickpocket: PickPocket %s", native ? "wrapped (native jump)" : "not wrapped (another build): a picked pocket stays on this copy");
}

void OnState(uint8_t from, net::WorldState const& s)
{
    _GUID raw;
    std::memcpy(&raw, s.guid, 16);
    Entity npc = Entity(bCPropertyID(bCGuid(raw)));
    eCEntity* instance = npc.GetInstance();
    if (!HasDialog(instance)) return;
    if (npc.Dialog.PickedPocket) { ++already; return; }  // the game never takes the mark back
    npc.Dialog.PickedPocket = GETrue;
    ++applied;
    Logf("pickpocket: player %u picked %s's pocket; marked here too", s.guard ? s.guard : from, npc.GetName().GetText());
}

void Report(LogFn log)
{
    char line[200];
    sprintf_s(line, "PICKSYNC enabled=%d installed=%d native=%d tries=%u successes=%u sent=%u applied=%u already=%u", enabled ? 1 : 0,
              installed ? 1 : 0, native ? 1 : 0, tries, successes, sent, applied, already);
    log(line);
}

void Off(LogFn log)
{
    enabled = false;
    log("PICKPOCKETOFF ok=1");
}

void Candidates(LogFn log)
{
    char line[300];
    eCEntity* hero = gCSession::GetInstance().GetHero();
    if (!hero) { log("PICKCANDS end count=0"); return; }
    const bCVector& h = hero->GetWorldPosition();
    const bTObjArray<Entity> all = Entity::GetNPCs();
    int count = 0;
    for (GEInt i = 0; i < all.GetCount() && count < 40; ++i) {
        Entity npc = all.GetAt(i);
        eCEntity* instance = npc.GetInstance();
        if (!HasDialog(instance) || instance == hero || !instance->GetPropertySet(eEPropertySetType_NPC) || npc.IsDead()) continue;
        const bCVector& p = instance->GetWorldPosition();
        const float dx = p.GetX() - h.GetX(), dy = p.GetY() - h.GetY(), dz = p.GetZ() - h.GetZ();
        const float dist = sqrtf(dx * dx + dy * dy + dz * dz);
        if (dist > 3000.0f) continue;
        const eCEntityProxy proxy(instance);
        bCPropertyID::bSCore core;
        std::memcpy(&core, &proxy.GetEntityID().GetCore(), 16);
        sprintf_s(line, "PICKCAND guid=%08X-%04X-%04X-%02X%02X-%02X%02X%02X%02X%02X%02X name=%s level=%u picked=%d dist=%.0f mirrored=%d down=%d",
                  core.Data1, core.Data2, core.Data3, core.Data4[0], core.Data4[1], core.Data4[2], core.Data4[3], core.Data4[4],
                  core.Data4[5], core.Data4[6], core.Data4[7], npc.GetName().GetText(), static_cast<unsigned>(npc.NPC.LevelMax),
                  npc.Dialog.PickedPocket ? 1 : 0, dist, npcs::IsMirrored(instance) ? 1 : 0, npc.IsDown() ? 1 : 0);
        log(line);
        ++count;
    }
    sprintf_s(line, "PICKCANDS end count=%d", count);
    log(line);
}

void Info(char const* guid, LogFn log)
{
    char line[200];
    Entity npc = ByGuid(guid);
    if (!HasDialog(npc.GetInstance())) { log("REFUSED pickinfo: no such NPC"); return; }
    sprintf_s(line, "PICKINFO guid=%s name=%s picked=%d", guid, npc.GetName().GetText(), npc.Dialog.PickedPocket ? 1 : 0);
    log(line);
}

// PickPocket succeeds unless a random 0..99 exceeds theft / 2 - level + 85 (level: the victim's LevelMax, at most 50, which
// needs Perk_PickPocket_3; theft: PlayerMemory.GetTheft, FUN_100339f0).
void ThiefPrep(LogFn log)
{
    char line[160];
    eCEntity* hero = gCSession::GetInstance().GetHero();
    if (!hero) { log("REFUSED thiefprep: no hero"); return; }
    Entity self(hero);
    const Template perk("Perk_PickPocket_3");
    if (!self.Inventory.HasLearnedSkill(perk)) self.Inventory.TeachSkill(perk);
    if (self.PlayerMemory.GetTheft() < 150) self.PlayerMemory.SetTheft(150);  // 150 / 2 - 50 + 85 = 110 > 99
    sprintf_s(line, "THIEFPREP theft=%d perk=%d", self.PlayerMemory.GetTheft(), self.Inventory.IsSkillActive(perk) ? 1 : 0);
    log(line);
}

// QA (learnperk <Perk_...>): this machine's hero learns that skill (P1 stations: mining needs Perk_Mining).
void LearnPerk(char const* name, LogFn log)
{
    char line[160];
    eCEntity* hero = gCSession::GetInstance().GetHero();
    const Template perk(name);
    if (!hero || !perk.IsValid()) { sprintf_s(line, "REFUSED learnperk %s: no hero or no such skill", name); log(line); return; }
    Entity self(hero);
    if (!self.Inventory.HasLearnedSkill(perk)) self.Inventory.TeachSkill(perk);
    sprintf_s(line, "LEARNPERK name=%s learned=%d", name, self.Inventory.HasLearnedSkill(perk) ? 1 : 0);
    log(line);
}

void Pick(char const* guid, LogFn log)
{
    char line[300];
    Entity npc = ByGuid(guid);
    eCEntity* hero = gCSession::GetInstance().GetHero();
    if (!HasDialog(npc.GetInstance()) || !hero) { log("REFUSED pickpocket: no such NPC or no hero"); return; }
    Entity thief(hero);
    const int before = ItemCount(thief);
    const bool pickedBefore = npc.Dialog.PickedPocket;
    const GEInt result = GetScriptAdminExt().CallScript(bCString("PickPocket"), hero, npc.GetInstance(), 0);  // the dialog command's call
    sprintf_s(line, "PICKPOCKET guid=%s name=%s result=%d picked_before=%d picked_after=%d items_before=%d items_after=%d", guid,
              npc.GetName().GetText(), result, pickedBefore ? 1 : 0, npc.Dialog.PickedPocket ? 1 : 0, before, ItemCount(thief));
    log(line);
}
}
