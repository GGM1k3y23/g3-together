// SPDX-License-Identifier: GPL-3.0-or-later
#include "world.h"
#include "log.h"

#include "body.h"
#include "remote.h"

#include <Engine.h>
#include <Game.h>
#include <Script.h>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace g3t::world {
namespace {
void PositionOf(eCEntity* entity, float out[3])
{
    const bCVector& at = entity->GetWorldPosition();
    out[0] = at.GetX();
    out[1] = at.GetY();
    out[2] = at.GetZ();
}

// Every entity the engine processes around the camera, static ones included (doors, ore veins, campfires);
// Entity::GetEntities lists only dynamic ones (NPCs, items: 243 at the start, no door).
bTSmallArray<eCEntity*> const* Processed()
{
    auto* scene = static_cast<eCSceneAdmin*>(eCModuleAdmin::GetInstance().FindModule(bCString("eCSceneAdmin")));
    return scene ? &scene->GetAccessToEntityAdmin().GetEntitiesInProcessingRange() : nullptr;
}

}

bTSmallArray<eCEntity*> const* ProcessedEntities() { return Processed(); }

namespace {
bool listAll = false;  // QA: usables all -> every entity with an Interaction set that is no NPC

bool Usable(eCEntity* entity)
{
    if (!entity || !entity->GetPropertySet(eEPropertySetType_Interaction)) return false;
    if (entity->GetPropertySet(eEPropertySetType_NPC)) return false;
    if (listAll) return true;
    if (entity->GetPropertySet(eEPropertySetType_Item)) return false;
    Entity e(entity);
    const bCString use = e.Interaction.UseFunc;
    return use.GetLength() > 0 || entity->GetPropertySet(eEPropertySetType_Door);
}

// The nearest entity of that name to the controlled body (names of interact objects repeat: every door is a door).
eCEntity* Nearest(char const* name)
{
    eCEntity* body = remote::ControlledBody();
    if (!body) return nullptr;
    float from[3];
    PositionOf(body, from);
    eCEntity* best = nullptr;
    float bestDistance = 1e12f;
    // A GUID ("8-4-4-4-12") picks that one entity: chests all share one name.
    const bool byGuid = std::strlen(name) == 36 && name[8] == '-' && name[13] == '-';
    bTSmallArray<eCEntity*> const* every = Processed();
    for (GEU16 i = 0; every && i < every->GetCount(); ++i) {
        eCEntity* instance = every->GetAt(i);
        if (!instance) continue;
        if (byGuid) {
            const eCEntityProxy proxy(instance);
            _GUID raw;
            std::memcpy(&raw, &proxy.GetEntityID().GetCore(), 16);
            char text[40];
            Format(text, "%08lX-%04X-%04X-%02X%02X-%02X%02X%02X%02X%02X%02X", raw.Data1, raw.Data2, raw.Data3, raw.Data4[0], raw.Data4[1],
                      raw.Data4[2], raw.Data4[3], raw.Data4[4], raw.Data4[5], raw.Data4[6], raw.Data4[7]);
            if (_stricmp(text, name) != 0) continue;
        } else if (std::strcmp(instance->GetName().GetText(), name) != 0) {
            continue;
        }
        float p[3];
        PositionOf(instance, p);
        const float d = body::Distance(p, from);
        if (d < bestDistance) best = instance, bestDistance = d;
    }
    return best;
}

void Describe(eCEntity* entity, char* line, size_t size)
{
    Entity e(entity);
    float p[3];
    PositionOf(entity, p);
    const bool door = entity->GetPropertySet(eEPropertySetType_Door) != nullptr;
    const bool lock = entity->GetPropertySet(eEPropertySetType_Lock) != nullptr;
    const bCString use = e.Interaction.UseFunc;
    Format(line, size, "name=%s pos=%.0f_%.0f_%.0f usefunc=%s usetype=%d item=%d door=%d open=%d lock=%d locked=%d user=%s", entity->GetName().GetText(), p[0], p[1],
              p[2], *use.GetText() ? use.GetText() : "-", static_cast<int>(e.Interaction.GetUseType()), entity->GetPropertySet(eEPropertySetType_Item) ? 1 : 0, door ? 1 : 0, door && e.Door.IsOpen() ? 1 : 0, lock ? 1 : 0, lock && e.Lock.IsLocked() ? 1 : 0,
              e.Interaction.GetUser().GetInstance() ? e.Interaction.GetUser().GetName().GetText() : "-");
}
}

void Usables(char const* radiusText, LogFn log)
{
    eCEntity* body = remote::ControlledBody();
    if (!body) { log("USABLE end count=0 no_hero=1"); return; }
    listAll = radiusText && !std::strncmp(radiusText, "all", 3);
    if (listAll) radiusText += 3;
    const float radius = radiusText && *radiusText ? static_cast<float>(std::atof(radiusText)) : 3000.0f;
    float from[3];
    PositionOf(body, from);
    char line[400], text[360];
    int count = 0, total = 0, nearby = 0, interaction = 0, doors = 0;
    bTSmallArray<eCEntity*> const* every = Processed();
    total = every ? every->GetCount() : -1;
    for (GEU16 i = 0; every && i < every->GetCount() && count < 200; ++i) {
        eCEntity* instance = every->GetAt(i);
        if (!instance) continue;
        float p[3];
        PositionOf(instance, p);
        const float d = body::Distance(p, from);
        if (d > radius) continue;
        ++nearby;
        interaction += instance->GetPropertySet(eEPropertySetType_Interaction) != nullptr;
        doors += instance->GetPropertySet(eEPropertySetType_Door) != nullptr;
        if (!Usable(instance)) continue;
        Describe(instance, text, sizeof(text));
        Format(line, "USABLE %s distance=%.0f", text, d);
        log(line);
        ++count;
    }
    Format(line, "USABLE end count=%d entities=%d nearby=%d interaction=%d doors=%d", count, total, nearby, interaction, doors);
    log(line);
}

void UseInfo(char const* name, LogFn log)
{
    char line[560], text[360];  // text and the hero's part: more than 400 (sprintf_s ends the game on overflow)
    eCEntity* target = Nearest(name);
    if (!target) { log("REFUSED useinfo: no such entity"); return; }
    Describe(target, text, sizeof(text));
    eCEntity* hero = remote::ControlledBody();
    Entity self(hero);
    float at[3] = {};
    if (hero) PositionOf(hero, at);
    Entity focus = hero ? self.Focus.GetFocusEntity() : Entity();
    // The game's own rule for what the hero may choose (-1: never; an interact object with a user, re/out/p1-focus.c).
    const GEInt priority = hero ? GetScriptAdminExt().CallScript(bCString("GetFocusEntityPriority"), hero, target, 0) : -2;
    const eCEntityProxy proxy(target);
    bCPropertyID::bSCore g;  // for the loot QA (contents, loottake) on an interact object (treasurediag)
    std::memcpy(&g, &proxy.GetEntityID().GetCore(), 16);
    Format(line, "USEINFO %s hero_task=%s hero_state=%s hero_pos=%.0f_%.0f_%.0f hero_ani=%d focus=%s focus_is_it=%d focus_priority=%d "
              "guid=%08X-%04X-%04X-%02X%02X-%02X%02X%02X%02X%02X%02X", text, self.Routine.GetCurrentTask().GetText(),
              self.Routine.GetCurrentState().GetText(), at[0], at[1], at[2], hero ? static_cast<int>(self.Routine.AniState) : -1,
              focus.GetInstance() ? focus.GetName().GetText() : "-", focus.GetInstance() == target ? 1 : 0, priority, g.Data1, g.Data2,
              g.Data3, g.Data4[0], g.Data4[1], g.Data4[2], g.Data4[3], g.Data4[4], g.Data4[5], g.Data4[6], g.Data4[7]);
    log(line);
}

void FindEntities(char const* prefix, LogFn log)
{
    char line[200];
    int count = 0;
    bTSmallArray<eCEntity*> const* every = Processed();
    const size_t length = std::strlen(prefix);
    for (GEU16 i = 0; every && i < every->GetCount() && count < 30; ++i) {
        eCEntity* instance = every->GetAt(i);
        if (!instance || std::strncmp(instance->GetName().GetText(), prefix, length) != 0) continue;
        float p[3];
        PositionOf(instance, p);
        Format(line, "FINDENT name=%s pos=%.0f_%.0f_%.0f", instance->GetName().GetText(), p[0], p[1], p[2]);
        log(line);
        ++count;
    }
    Format(line, "FINDENTS end count=%d", count);
    log(line);
}

void FindAll(char const* name, LogFn log)
{
    char line[200];
    auto* scene = static_cast<eCSceneAdmin*>(eCModuleAdmin::GetInstance().FindModule(bCString("eCSceneAdmin")));
    bTPtrArray<eCEntity*> found;
    if (scene) scene->GetEntitiesByName(bCString(name), found);
    int count = 0;
    for (GEInt i = 0; i < found.GetCount() && count < 40; ++i) {
        eCEntity* instance = found.GetAt(i);
        if (!instance) continue;
        float p[3];
        PositionOf(instance, p);
        Entity e(instance);
        Format(line, "FINDALL name=%s pos=%.0f_%.0f_%.0f dead=%d", name, p[0], p[1], p[2], e.IsDead() ? 1 : 0);
        log(line);
        ++count;
    }
    Format(line, "FINDALL end count=%d", count);
    log(line);
}

void Unlock(char const* name, LogFn log)
{
    char line[200];
    eCEntity* target = Nearest(name);
    if (!target || !target->GetPropertySet(eEPropertySetType_Lock)) { log("REFUSED unlock: no such entity with a lock"); return; }
    Entity e(target);
    const bool before = e.Lock.IsLocked() != GEFalse;
    e.Lock.Status = gELockStatus_Unlocked;
    Format(line, "UNLOCK name=%s was_locked=%d locked=%d", name, before ? 1 : 0, e.Lock.IsLocked() ? 1 : 0);
    log(line);
}

namespace {
// Stand in front of the entity, facing it, as a player who walks up to it; the anchor point if it has one.
Entity Approach(eCEntity* hero, eCEntity* target)
{
    // Stand 1.2 m in front of it, facing it, as a player who walks up to it.
    float from[3], at[3];
    PositionOf(hero, from);
    PositionOf(target, at);
    float dx = from[0] - at[0], dz = from[2] - at[2];
    const float length = std::sqrt(dx * dx + dz * dz);
    if (length < 1.0f) dx = 0.0f, dz = 1.0f;
    else dx /= length, dz /= length;
    float place[3] = {at[0] + dx * 120.0f, from[1], at[2] + dz * 120.0f};
    // The object's anchor point is where PS_Interact walks the user to first (_AI_Goto_Ext); from anywhere else the
    // walk did not always arrive (a bed: 25 s in PS_Interact). Start there.
    Entity anchor = Entity(target).Interaction.GetAnchorPoint();
    if (anchor.GetInstance() && anchor.GetInstance() != target) {
        float a[3];
        PositionOf(anchor.GetInstance(), a);
        if (body::Distance(a, at) < 1000.0f) {
            place[0] = a[0], place[1] = a[1], place[2] = a[2];
            dx = a[0] - at[0], dz = a[2] - at[2];
            const float l = std::sqrt(dx * dx + dz * dz);
            if (l > 1.0f) dx /= l, dz /= l;
        }
    }
    const float yaw = std::atan2(-dx, -dz);
    const float rotation[4] = {0.0f, std::sin(yaw / 2), 0.0f, std::cos(yaw / 2)};
    Entity(hero).MoveTo(body::Pose(*hero, place, rotation));
    return anchor;
}
}

void Use(char const* name, LogFn log)
{
    char line[400];
    eCEntity* hero = remote::ControlledBody();
    eCEntity* target = Nearest(name);
    if (!hero || !target) { log("REFUSED use: no hero or no such entity"); return; }
    Entity anchor = Approach(hero, target);
    Entity self(hero);
    Entity object(target);
    self.Focus.SetFocusEntity(object);
    self.NPC.SetCurrentTarget(object);
    self.Routine.SetTask(bCString("PS_Interact"));
    const bCString use = object.Interaction.UseFunc;
    Format(line, "USE name=%s usefunc=%s hero_task=%s anchor=%s", name, use.GetText(), self.Routine.GetCurrentTask().GetText(),
              anchor.GetInstance() ? anchor.GetName().GetText() : "-");
    log(line);
}
}

namespace g3t::world {
// QA: stand in front of an entity and face it, without using it (the game's own focus then picks it).
void Face(char const* name, LogFn log)
{
    eCEntity* hero = remote::ControlledBody();
    eCEntity* target = Nearest(name);
    if (!hero || !target) { log("REFUSED face: no hero or no such entity"); return; }
    Approach(hero, target);
    char line[200];
    Format(line, "FACE name=%s", name);
    log(line);
}
}
