// SPDX-License-Identifier: GPL-3.0-or-later
#include "social.h"

#include "body.h"
#include "remote.h"

#include <Game.h>
#include <Script.h>
#include <Windows.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

namespace g3t::social {
namespace {
float DistanceToHero(eCEntity* entity)
{
    eCEntity* hero = gCSession::GetInstance().GetHero();
    if (!hero || !entity) return 1e9f;
    const bCVector& a = entity->GetWorldPosition();
    const bCVector& b = hero->GetWorldPosition();
    const float p[3] = {a.GetX(), a.GetY(), a.GetZ()}, q[3] = {b.GetX(), b.GetY(), b.GetZ()};
    return body::Distance(p, q);
}

void FormatGuid(eCEntity* entity, char* out, size_t size)
{
    const eCEntityProxy proxy(entity);
    bCPropertyID::bSCore core;
    std::memcpy(&core, &proxy.GetEntityID().GetCore(), 16);
    sprintf_s(out, size, "%08X-%04X-%04X-%02X%02X-%02X%02X%02X%02X%02X%02X", core.Data1, core.Data2, core.Data3, core.Data4[0],
              core.Data4[1], core.Data4[2], core.Data4[3], core.Data4[4], core.Data4[5], core.Data4[6], core.Data4[7]);
}

// Who an NPC's target is, as the tests need it: this machine's hero, the puppet of player N, or the entity's name.
std::string Who(Entity const& target)
{
    eCEntity* instance = target.GetInstance();
    if (!instance) return "-";
    if (instance == gCSession::GetInstance().GetHero()) return "hero";
    for (int id = 1; id <= 8; ++id)
        if (remote::PuppetOf(id) == instance) return "puppet" + std::to_string(id);
    return target.GetName().GetText();
}

// "a,b,c,d,e" for crimes 0..4 of an enclave array (fewer if the game keeps fewer).
std::string Counts(bTValArray<GEI32> const& values)
{
    std::string out;
    for (GEInt i = 0; i < values.GetCount() && i < gEPlayerCrime_Count; ++i) out += (i ? "," : "") + std::to_string(values.GetAt(i));
    return out.empty() ? "-" : out;
}

// "enclave=<name> crimes=... suspect=... known=.. fame=.." for an enclave entity, "enclave=-" without one.
std::string EnclaveText(eCEntity* enclaveEntity)
{
    auto* enclave = enclaveEntity ? static_cast<gCEnclave_PS*>(enclaveEntity->GetPropertySet(eEPropertySetType_Enclave)) : nullptr;
    if (!enclave) return "enclave=-";
    char text[300];
    sprintf_s(text, "enclave=%s crimes=%s suspect=%s known=%d fame=%d", Entity(enclaveEntity).GetName().GetText(),
              Counts(enclave->AccessCrimeCount()).c_str(), Counts(enclave->AccessSuspectComment()).c_str(),
              static_cast<int>(static_cast<gEPlayerCrime const&>(enclave->AccessKnownPlayerCrime())),
              static_cast<int>(enclave->AccessPlayerFame()));
    return text;
}
}

void Owned(char const* radius, LogFn log)
{
    const float within = radius && *radius ? static_cast<float>(std::atof(radius)) : 3000.0f;
    char line[320], guid[48];
    const bTObjArray<Entity> all = Entity::GetEntities();
    int found = 0;
    for (int i = 0; i < all.GetCount(); ++i) {
        Entity entity = all.GetAt(i);
        if (!entity.GetInstance() || !entity.IsItem() || entity.GetOwner().GetInstance()) continue;
        const float distance = DistanceToHero(entity.GetInstance());
        if (distance > within) continue;
        // As AddEnclaveTheftCount asks (helper SG 0xaf20): the item's indoor owner zone within 100 cm, then that zone's
        // security level and owner (an enclave or an NPC).
        Entity zone = entity.GetIndoorOwnerZone(100.0f);
        if (!zone.GetInstance()) continue;
        Entity zoneOwner = zone.GetAIZoneOwner();
        const int security = static_cast<int>(zone.GetAIZoneSecurityLevel());
        FormatGuid(entity.GetInstance(), guid, sizeof(guid));
        const bCVector& at = entity.GetInstance()->GetWorldPosition();
        sprintf_s(line, "OWNED guid=%s name=%s distance_cm=%.0f pos=%.0f_%.0f_%.0f zone=%s zone_owner=%s security=%d", guid,
                  entity.GetName().GetText(), distance, at.GetX(), at.GetY(), at.GetZ(), zone.GetName().GetText(),
                  zoneOwner.GetInstance() ? zoneOwner.GetName().GetText() : "-", security);
        log(line);
        ++found;
    }
    sprintf_s(line, "OWNED end found=%d", found);
    log(line);
}

void Crime(char const* name, LogFn log)
{
    char line[600];
    Entity npc = Entity::GetEntity(bCString(name));
    eCEntity* instance = npc.GetInstance();
    auto* ps = instance ? static_cast<gCNPC_PS*>(instance->GetPropertySet(eEPropertySetType_NPC)) : nullptr;
    if (!ps) { log("REFUSED crime: no such NPC"); return; }
    // The game's own members, not the script wrappers (they look properties up by name; see infocmd.cpp).
    const int lastCrime = static_cast<int>(static_cast<gEPlayerCrime const&>(ps->AccessLastPlayerCrime()));
    const int reason = static_cast<int>(static_cast<gEAttackReason const&>(ps->AccessAttackReason()));
    const int lastAr = static_cast<int>(static_cast<gEAttackReason const&>(ps->AccessLastPlayerAR()));
    const std::string enclaveText = EnclaveText(ps->GetEnclaveProxy().GetEntity());
    sprintf_s(line, "CRIME name=%s last_crime=%d reason=%d last_player_ar=%d target=%s task=%s %s", name, lastCrime, reason, lastAr,
              Who(npc.NPC.GetCurrentTarget()).c_str(), npc.Routine.GetCurrentTask().GetText(), enclaveText.c_str());
    log(line);
}

void Enclaves(LogFn log)
{
    char line[600];
    const GEInt count = PSEnclave::GetEnclaveCount();
    for (GEInt i = 0; i < count; ++i) {
        Entity e = PSEnclave::GetEnclaveAt(i);
        eCEntity* instance = e.GetInstance();
        auto* enclave = instance ? static_cast<gCEnclave_PS*>(instance->GetPropertySet(eEPropertySetType_Enclave)) : nullptr;
        if (!enclave) continue;
        bTObjArray<eCEntityProxy> members = enclave->GetMembers();
        char const* first = "-";
        for (GEInt m = 0; m < members.GetCount(); ++m)
            if (eCEntity* member = members.AccessAt(m).GetEntity()) { first = member->GetName().GetText(); break; }
        sprintf_s(line, "ENCLAVE members=%d first=%s status=%d %s", members.GetCount(), first,
                  static_cast<int>(static_cast<gEEnclaveStatus const&>(enclave->AccessStatus())), EnclaveText(instance).c_str());
        log(line);
    }
    sprintf_s(line, "ENCLAVES end count=%d", count);
    log(line);
}

void Unarm(LogFn log)
{
    char line[160];
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    if (!hero) { log("REFUSED unarm: no hero"); return; }
    Entity self(hero);
    const GEBool put = self.Inventory.HoldStacks(-1, -1);
    Entity inHand = self.Inventory.GetItemFromSlot(gESlot_RightHand);
    sprintf_s(line, "UNARM put=%d right_hand=%s", put ? 1 : 0, inHand.GetInstance() ? inHand.GetName().GetText() : "none");
    log(line);
}
}
