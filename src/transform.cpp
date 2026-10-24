// SPDX-License-Identifier: GPL-3.0-or-later
#include "transform.h"

#include "body.h"
#include "log.h"
#include "remote.h"

#include <Game.h>
#include <Script.h>
#include <cmath>
#include <cstdio>

namespace g3t::transform {
namespace {
Entity original;  // the hero's own body while it is transformed (QA bookkeeping)

int HitPoints(Entity& e)
{
    eCEntity* instance = e.GetInstance();
    if (!instance) return -1;
    if (instance->GetPropertySet(eEPropertySetType_PlayerMemory)) return e.PlayerMemory.GetHitPoints();
    return instance->GetPropertySet(eEPropertySetType_DamageReceiver) ? static_cast<int>(e.DamageReceiver.HitPoints) : -1;
}

void Line(char const* prefix, LogFn log)
{
    char line[460], motion[96] = "-";
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    Entity player = Entity::GetPlayer();
    if (hero) body::CurrentMotion(hero, motion, sizeof(motion));
    Entity self(hero);
    sprintf_s(line, "%s hero=%s player=%s same=%d original=%s original_shown=%d transformed=%d remain=%.0f hero_hp=%d player_hp=%d motion=%s", prefix,
              hero ? self.GetName().GetText() : "-", player.GetInstance() ? player.GetName().GetText() : "-",
              hero && player.GetInstance() == hero ? 1 : 0, original.GetInstance() ? original.GetName().GetText() : "-",
              original.GetInstance() && original.GetInstance()->IsEnabled() ? 1 : 0,
              hero && hero->GetPropertySet(eEPropertySetType_NPC) ? (self.NPC.IsTransformed() ? 1 : 0) : -1,
              original.GetInstance() ? static_cast<float>(original.PlayerMemory.SecondsTransformRemain) : -1.0f, HitPoints(self),
              HitPoints(player), motion);
    log(line);
}
}

// MagicTransform (Script_Game.dll RVA 0x57880): spawn the spell's creature at the caster's pose, the caster's
// StartTransform(creature, seconds, false), the creature takes the task PS_Melee.
void Start(char const* creatureTemplate, LogFn log)
{
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    const Template shape(creatureTemplate);
    if (!hero || !shape.IsValid()) {
        char line[160];
        sprintf_s(line, "TRANSFORM refused template=%s valid=%d", creatureTemplate, shape.IsValid() ? 1 : 0);
        log(line);
        return;
    }
    Entity self(hero);
    Entity creature = Entity::Spawn(shape, hero->GetWorldMatrix());
    if (!creature.GetInstance()) { log("TRANSFORM refused spawn failed"); return; }
    original = self;
    self.StartTransform(creature, 120.0f, GEFalse);
    creature.Routine.SetTask(bCString("PS_Melee"));
    Logf("transform: %s turned into %s", self.GetName().GetText(), creature.GetName().GetText());
    Line("TRANSFORM", log);
}

// TransformBack (RVA 0x58610) moves the hero's own body to the creature and calls EndTransform.
void Back(LogFn log)
{
    Entity player = Entity::GetPlayer();
    GetScriptAdminExt().CallScript(bCString("TransformBack"), player.GetInstance(), player.GetInstance(), 0);
    Line("TRANSFORMBACK", log);
    original = Entity();
}

void Report(LogFn log) { Line("TRANSFORMSTATE", log); }

void Walk(int direction, LogFn log)
{
    char line[200];
    eCEntity* self = gCSession::GetInstance().GetHero() ? remote::ControlledBody() : nullptr;
    if (!self) { log("WALKBY refused no hero"); return; }
    const bCVector& at = self->GetWorldPosition();
    const float angle = direction * 0.785398f;
    const bCVector goal(at.GetX() + 600.0f * std::cos(angle), at.GetY(), at.GetZ() + 600.0f * std::sin(angle));
    Entity body(self);
    // A movement goal does nothing on the body the player steers; the game's AI goto (as walkto for the hero) does.
    const GEBool started = body.StartGoto(goal, gEWalkMode_Run);
    sprintf_s(line, "WALKBY body=%s started=%d from=%.0f,%.0f,%.0f goal=%.0f,%.0f,%.0f", body.GetName().GetText(), started ? 1 : 0, at.GetX(),
              at.GetY(), at.GetZ(), goal.GetX(), goal.GetY(), goal.GetZ());
    log(line);
}
}
