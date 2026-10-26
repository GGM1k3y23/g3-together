// SPDX-License-Identifier: GPL-3.0-or-later
#include "social.h"
#include "log.h"

#include "body.h"
#include "infocmd.h"
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
    Format(out, size, "%08X-%04X-%04X-%02X%02X-%02X%02X%02X%02X%02X%02X", core.Data1, core.Data2, core.Data3, core.Data4[0],
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
    Format(text, "enclave=%s crimes=%s suspect=%s known=%d fame=%d", Entity(enclaveEntity).GetName().GetText(),
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
        Format(line, "OWNED guid=%s name=%s distance_cm=%.0f pos=%.0f_%.0f_%.0f zone=%s zone_owner=%s security=%d", guid,
                  entity.GetName().GetText(), distance, at.GetX(), at.GetY(), at.GetZ(), zone.GetName().GetText(),
                  zoneOwner.GetInstance() ? zoneOwner.GetName().GetText() : "-", security);
        log(line);
        ++found;
    }
    Format(line, "OWNED end found=%d", found);
    log(line);
}

// QA (P7 attitudes, 26.10. pair 1): items within `radius` cm of the hero in an indoor owner zone, as `owned`, and for each
// living NPC within 15 m of the item whether it minds the local player taking it (IsBotheredByPlayerTheftOf, the step of
// AssessTakeItem before the witness judges, re/out/p7-half-20261026-b.c). Only items with at least one such NPC.
void Bothered(char const* radius, LogFn log)
{
    const float within = radius && *radius ? static_cast<float>(std::atof(radius)) : 3000.0f;
    char line[700], guid[48];
    const bTObjArray<Entity> all = Entity::GetEntities();
    const bTObjArray<Entity> people = Entity::GetNPCs();
    int found = 0;
    for (int i = 0; i < all.GetCount(); ++i) {
        Entity entity = all.GetAt(i);
        eCEntity* thing = entity.GetInstance();
        // Items lying about, and interact objects (chests: AssessUseInteractObject asks the same, 26.10.).
        const bool object = thing && !entity.IsItem() && thing->GetPropertySet(eEPropertySetType_Interaction) &&
                            !thing->GetPropertySet(eEPropertySetType_NPC);
        if (!thing || (!entity.IsItem() && !object) || (!object && entity.GetOwner().GetInstance())) continue;
        const float distance = DistanceToHero(entity.GetInstance());
        if (distance > within) continue;
        Entity zone = entity.GetIndoorOwnerZone(100.0f);
        if (!zone.GetInstance()) continue;
        const bCVector& at = entity.GetInstance()->GetWorldPosition();
        std::string who;
        for (int k = 0; k < people.GetCount(); ++k) {
            Entity npc = people.GetAt(k);
            eCEntity* instance = npc.GetInstance();
            if (!instance || npc.IsDead() || remote::IsPuppet(instance) || instance == gCSession::GetInstance().GetHero()) continue;
            const bCVector& p = instance->GetWorldPosition();
            const float dx = p.GetX() - at.GetX(), dy = p.GetY() - at.GetY(), dz = p.GetZ() - at.GetZ();
            if (dx * dx + dy * dy + dz * dz > 1500.0f * 1500.0f) continue;
            if (!GetScriptAdminExt().CallScript(bCString("IsBotheredByPlayerTheftOf"), instance, entity.GetInstance(), 0)) continue;
            if (!who.empty()) who += ',';
            who += npc.GetName().GetText();
            if (who.size() > 300) break;
        }
        if (who.empty()) continue;
        Entity zoneOwner = zone.GetAIZoneOwner();
        FormatGuid(entity.GetInstance(), guid, sizeof(guid));
        Format(line, "BOTHERED guid=%s name=%s distance_cm=%.0f pos=%.0f_%.0f_%.0f zone=%s zone_owner=%s security=%d kind=%s usetype=%d npcs=%s", guid,
                  entity.GetName().GetText(), distance, at.GetX(), at.GetY(), at.GetZ(), zone.GetName().GetText(),
                  zoneOwner.GetInstance() ? zoneOwner.GetName().GetText() : "-", static_cast<int>(zone.GetAIZoneSecurityLevel()),
                  object ? "object" : "item", object ? static_cast<int>(entity.Interaction.UseType) : -1, who.c_str());
        log(line);
        ++found;
    }
    Format(line, "BOTHERED end found=%d", found);
    log(line);
}

// QA (P7, using other people's things): interact objects within `radius` cm of the hero that stand in an indoor owner zone,
// as AssessUseInteractObject's helper asks (SG 0x30840 via 0xaf20), with their use function, type and user.
void OwnedObjects(char const* radius, LogFn log)
{
    const float within = radius && *radius ? static_cast<float>(std::atof(radius)) : 3000.0f;
    char line[400], guid[48];
    const bTObjArray<Entity> all = Entity::GetEntities();
    int found = 0;
    for (int i = 0; i < all.GetCount(); ++i) {
        Entity entity = all.GetAt(i);
        eCEntity* instance = entity.GetInstance();
        if (!instance || !instance->GetPropertySet(eEPropertySetType_Interaction) || instance->GetPropertySet(eEPropertySetType_NPC)) continue;
        const float distance = DistanceToHero(instance);
        if (distance > within) continue;
        Entity zone = entity.GetIndoorOwnerZone(100.0f);
        if (!zone.GetInstance()) continue;
        Entity zoneOwner = zone.GetAIZoneOwner();
        FormatGuid(instance, guid, sizeof(guid));
        const bCVector& at = instance->GetWorldPosition();
        Format(line, "OWNEDOBJ guid=%s name=%s distance_cm=%.0f pos=%.0f_%.0f_%.0f usetype=%d usefunc=%s user=%s zone=%s zone_owner=%s security=%d", guid,
                  entity.GetName().GetText(), distance, at.GetX(), at.GetY(), at.GetZ(), static_cast<int>(entity.Interaction.UseType),
                  entity.Interaction.UseFunc.IsEmpty() ? "-" : entity.Interaction.UseFunc.GetText(), entity.Interaction.GetUser().GetInstance() ? entity.Interaction.GetUser().GetName().GetText() : "-",
                  zone.GetName().GetText(), zoneOwner.GetInstance() ? zoneOwner.GetName().GetText() : "-", static_cast<int>(zone.GetAIZoneSecurityLevel()));
        log(line);
        ++found;
    }
    Format(line, "OWNEDOBJ end found=%d", found);
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
    Format(line, "CRIME name=%s last_crime=%d reason=%d last_player_ar=%d target=%s task=%s %s", name, lastCrime, reason, lastAr,
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
        Format(line, "ENCLAVE members=%d first=%s status=%d %s", members.GetCount(), first,
                  static_cast<int>(static_cast<gEEnclaveStatus const&>(enclave->AccessStatus())), EnclaveText(instance).c_str());
        log(line);
    }
    Format(line, "ENCLAVES end count=%d", count);
    log(line);
}

// QA (revolution <Town>): what the dialog command SetRevolutionEnabled <Town> true does (Phil's SKALVERAM3133 in Kap Dun):
// the town's enclave takes the revolution flag; passed on as a dialog command's result is (infocmd).
void Revolution(char const* town, LogFn log)
{
    char line[200];
    for (GEInt i = 0; i < PSEnclave::GetEnclaveCount(); ++i) {
        Entity e = PSEnclave::GetEnclaveAt(i);
        eCEntity* instance = e.GetInstance();
        auto* enclave = instance ? static_cast<gCEnclave_PS*>(instance->GetPropertySet(eEPropertySetType_Enclave)) : nullptr;
        if (!enclave || std::strcmp(instance->GetName().GetText(), town)) continue;
        enclave->AccessRevolution() = GETrue;
        infocmd::Changed(instance);
        Format(line, "REVOLUTION town=%s revolution=%d status=%d", town, enclave->AccessRevolution() ? 1 : 0,
                  static_cast<int>(static_cast<gEEnclaveStatus const&>(enclave->AccessStatus())));
        log(line);
        return;
    }
    Format(line, "REFUSED revolution: no town %s", town);
    log(line);
}

// QA (startrevolution <Town>; coop evening 26.10.): what the dialog command StartRevolution <member> does (Braga, Mora Sul,
// Finley's ambush; Script_Game command 0x37): the member's town takes Raid and status Attack (1); passed on as a dialog
// command's result is (infocmd). Kap Dun's own dialog only enables the revolution; this is the story's shortcut to the fight.
void StartRevolution(char const* town, LogFn log)
{
    char line[200];
    for (GEInt i = 0; i < PSEnclave::GetEnclaveCount(); ++i) {
        Entity e = PSEnclave::GetEnclaveAt(i);
        eCEntity* instance = e.GetInstance();
        auto* enclave = instance ? static_cast<gCEnclave_PS*>(instance->GetPropertySet(eEPropertySetType_Enclave)) : nullptr;
        if (!enclave || std::strcmp(instance->GetName().GetText(), town)) continue;
        enclave->AccessRaid() = GETrue;
        enclave->AccessStatus() = static_cast<gEEnclaveStatus>(1);
        infocmd::Changed(instance);
        Format(line, "STARTREVOLUTION town=%s raid=%d revolution=%d status=%d", town, enclave->AccessRaid() ? 1 : 0,
                  enclave->AccessRevolution() ? 1 : 0, static_cast<int>(static_cast<gEEnclaveStatus const&>(enclave->AccessStatus())));
        log(line);
        return;
    }
    Format(line, "REFUSED startrevolution: no town %s", town);
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
    Format(line, "UNARM put=%d right_hand=%s", put ? 1 : 0, inHand.GetInstance() ? inHand.GetName().GetText() : "none");
    log(line);
}
}
