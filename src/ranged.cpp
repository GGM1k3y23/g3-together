// SPDX-License-Identifier: GPL-3.0-or-later
#include "ranged.h"

#include "body.h"
#include "log.h"
#include "remote.h"
#include "summon.h"
#include "world.h"

#include <Game.h>
#include <Script.h>
#include <Windows.h>
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <new>
#include <vector>

namespace g3t::ranged {
bool homing = true;
int reloadFallbacks = 0;

namespace {
bool ParseGuid(char const* text, _GUID& out)
{
    unsigned long d1 = 0;
    unsigned d2 = 0, d3 = 0, b[8] = {};
    if (sscanf_s(text, "%8lx-%4x-%4x-%2x%2x-%2x%2x%2x%2x%2x%2x", &d1, &d2, &d3, &b[0], &b[1], &b[2], &b[3], &b[4], &b[5], &b[6], &b[7]) != 11)
        return false;
    out.Data1 = d1;
    out.Data2 = static_cast<unsigned short>(d2);
    out.Data3 = static_cast<unsigned short>(d3);
    for (int i = 0; i < 8; ++i) out.Data4[i] = static_cast<unsigned char>(b[i]);
    return true;
}

Entity Player()
{
    eCEntity* body = gCSession::GetInstance().GetHero() ? remote::ControlledBody() : nullptr;
    return Entity(body);
}

GEInt StackWithUse(Entity& self, gEUseType use)
{
    for (GEInt i = 0; i < self.Inventory.GetStackCount(); ++i) {
        Entity item = self.Inventory.GetTemplateItem(i);
        if (item.GetInstance() && item.Interaction.GetUseType() == use && self.Inventory.GetAmount(i) > 0) return i;
    }
    return -1;
}

char const* HeldName(Entity& self, gESlot slot, char* out, size_t size)
{
    const Entity held = self.Inventory.GetItemFromSlot(slot);
    strncpy_s(out, size, held.GetInstance() ? held.GetName().GetText() : "-", _TRUNCATE);
    return out;
}

Entity EntityAt(char const* guid)
{
    _GUID raw;
    if (ParseGuid(guid, raw)) return Entity(bCPropertyID(bCGuid(raw)));
    // Not a GUID: the nearest processed entity of that name (doors and chests have no GUID in the QA output, and chests
    // share names; MagicOpenLocks).
    eCEntity* hero = gCSession::GetInstance().GetHero();
    eCEntity* best = nullptr;
    float bestDistance = 1e30f;
    bTSmallArray<eCEntity*> const* every = world::ProcessedEntities();
    for (GEU16 i = 0; hero && every && i < every->GetCount(); ++i) {
        eCEntity* e = every->GetAt(i);
        if (!e || std::strcmp(e->GetName().GetText(), guid)) continue;
        const float distance = (e->GetWorldPosition() - hero->GetWorldPosition()).GetMagnitude();
        if (distance < bestDistance) best = e, bestDistance = distance;
    }
    return best ? Entity(best) : Entity();
}

GEInt ManaOf(eCEntity* body)
{
    return body == gCSession::GetInstance().GetHero() ? Entity(body).PlayerMemory.GetManaPoints() : -1;
}
}

void Inventory(LogFn log)
{
    char line[300];
    Entity self = Player();
    if (!self.GetInstance()) { log("INV end count=0"); return; }
    for (GEInt i = 0; i < self.Inventory.GetStackCount(); ++i) {
        Entity item = self.Inventory.GetTemplateItem(i);
        if (!item.GetInstance()) continue;
        sprintf_s(line, "INV name=%s amount=%d use=%d category=%d", item.GetName().GetText(), self.Inventory.GetAmount(i),
                  static_cast<int>(item.Interaction.GetUseType()), static_cast<int>(item.Item.GetCategory()));
        log(line);
    }
    char right[64], left[64];
    sprintf_s(line, "INV end count=%d right=%s left=%s", self.Inventory.GetStackCount(), HeldName(self, gESlot_RightHand, right, sizeof(right)),
              HeldName(self, gESlot_LeftHand, left, sizeof(left)));
    log(line);
}

// A bow goes into the left hand with arrows in the right (both held, as drawing the bow does); anything else (a
// spell, a weapon) into the right hand.
void Ready(char const* item, LogFn log)
{
    char line[400], right[64], left[64], ammoInfo[160] = "";
    Entity self = Player();
    const Template templ(item);
    if (!self.GetInstance() || !templ.IsValid()) { sprintf_s(line, "READY refused item=%s valid=%d", item, templ.IsValid() ? 1 : 0); log(line); return; }
    if (self.Inventory.GetItemAmount(templ) < 1) self.Inventory.CreateItems(templ, 0, 1);
    const GEInt stack = self.Inventory.FindStackIndex(templ);
    Entity carried = stack >= 0 ? self.Inventory.GetTemplateItem(stack) : Entity();
    const gEUseType use = carried.GetInstance() ? carried.Interaction.GetUseType() : gEUseType_None;
    GEBool held = GEFalse;
    if (use == gEUseType_Bow || use == gEUseType_CrossBow) {
        const gEUseType ammo = use == gEUseType_Bow ? gEUseType_Arrow : gEUseType_Bolt;
        GEInt arrows = StackWithUse(self, ammo);
        if (arrows < 0) {
            self.Inventory.CreateItems(Template(use == gEUseType_Bow ? "Arrow" : "Bolt"), 0, 30);  // the game's ammo templates
            arrows = StackWithUse(self, ammo);
        }
        // After a shot the bow is still held and the right hand empty; holding the same stacks again changes nothing
        // then, so let go of both first.
        if (!self.Inventory.GetItemFromSlot(gESlot_RightHand).GetInstance()) self.Inventory.HoldStacks(-1, -1);
        held = self.Inventory.HoldStacks(stack, arrows);
        if (!self.Inventory.GetItemFromSlot(gESlot_RightHand).GetInstance()) {
            ++reloadFallbacks;
            held = self.Inventory.HoldRightStack(arrows);
        }
        sprintf_s(ammoInfo, " ammo_stack=%d ammo_amount=%d ammo_slot=%d right_stack=%d fallbacks=%d", arrows,
                  arrows >= 0 ? self.Inventory.GetAmount(arrows) : -1, arrows >= 0 ? static_cast<int>(self.Inventory.GetSlotFromStack(arrows)) : -1,
                  self.Inventory.FindStackIndex(gESlot_RightHand), reloadFallbacks);
    } else {
        held = self.Inventory.HoldStacks(-1, stack);
    }
    // Held things collide with nothing, as _AI_Reload leaves the arrow it puts in the hand: HoldStacks plays no
    // drawing animation and left the bow solid, and the arrows stopped in it right where they were spawned (19.10.).
    for (gESlot slot : {gESlot_LeftHand, gESlot_RightHand}) {
        Entity inHand = self.Inventory.GetItemFromSlot(slot);
        if (inHand.GetInstance()) inHand.EnableCollision(GEFalse);
    }
    sprintf_s(line, "READY item=%s use=%d stack=%d held=%d right=%s left=%s%s", item, static_cast<int>(use), stack, held ? 1 : 0,
              HeldName(self, gESlot_RightHand, right, sizeof(right)), HeldName(self, gESlot_LeftHand, left, sizeof(left)), ammoInfo);
    log(line);
}

void Target(char const* guid, LogFn log)
{
    char line[300];
    Entity self = Player();
    Entity target = EntityAt(guid);
    if (!self.GetInstance() || !target.GetInstance()) { log("TARGET refused bad GUID, no such entity or no player"); return; }
    eCEntity* body = self.GetInstance();
    const bCVector& from = body->GetWorldPosition();
    const bCVector& to = target.GetInstance()->GetWorldPosition();
    const float half = std::atan2(to.GetX() - from.GetX(), to.GetZ() - from.GetZ()) * 0.5f;
    const float position[3] = {from.GetX(), from.GetY(), from.GetZ()};
    const float rotation[4] = {0.0f, std::sin(half), 0.0f, std::cos(half)};  // about the up axis, as NEARX yaw reads it
    self.MoveTo(body::Pose(*body, position, rotation));
    self.SetAlignmentTarget(target);
    self.Focus.SetFocusEntity(target);
    const float dx = to.GetX() - from.GetX(), dz = to.GetZ() - from.GetZ();
    sprintf_s(line, "TARGET name=%s distance_cm=%.0f focus=%s", target.GetName().GetText(), std::sqrt(dx * dx + dz * dz),
              self.Focus.GetFocusEntity().GetInstance() ? self.Focus.GetFocusEntity().GetName().GetText() : "-");
    log(line);
}

void Act(char const* taskAndState, LogFn log)
{
    char line[300], task[64] = {}, motion[128] = "-";
    Entity self = Player();
    char const* dash = std::strchr(taskAndState, '-');
    if (!self.GetInstance() || !dash || dash - taskAndState >= static_cast<ptrdiff_t>(sizeof(task))) { log("ACT refused TASK-STATE"); return; }
    std::memcpy(task, taskAndState, dash - taskAndState);
    const bCString before = self.Routine.GetCurrentTask();
    if (std::strcmp(before.GetText(), task) != 0) self.Routine.SetTask(bCString(task));
    self.Routine.SetState(bCString(dash + 1));
    body::CurrentMotion(self.GetInstance(), motion, sizeof(motion));
    sprintf_s(line, "ACT task_before=%s task=%s state=%s motion=%s", before.GetText(), self.Routine.GetCurrentTask().GetText(),
              self.Routine.GetCurrentState().GetText(), motion);
    log(line);
}

// Shoot with the game's own script "Shoot" (PS_Ranged.cpp; re/findings/ranged-ai-paths-b.md), which spawns the arrow,
// sets its damage, takes it from the quiver and puts the next one in the hand. For the player it aims at the camera's
// screen centre, which a bot does not steer; so the arrow is then replaced by one sent the way the script sends an
// NPC's: in a line, with the target set. With homing off it keeps the camera's aim (what a real player gets). The new arrow only
// shows up among the entities in a later frame: Tick finds it there and writes the SHOOTAT line.
namespace {
struct PendingShot {
    eCEntity* shooter = nullptr;
    eCEntity* target = nullptr;
    std::vector<eCEntity*> before;
    GEInt result = 0;
    DWORD since = 0;
    LogFn log = nullptr;
    int mode = 0;
    float heldMultiplier = 0.0f;  // the held arrow's DamageHitMultiplier before Shoot (the draw; velocity = it x 200)
    char partsOff[120] = "";      // mode 5: the body parts whose collision was off for the shot
    float heldOffset = 0.0f;      // how far the held arrow was from the hero
} pending;

eCEntity* lastArrow = nullptr;  // QA: the arrow of the latest shootat, for LASTARROW
bCPropertyID lastArrowId;       // looked up by ID: an arrow that sticks in something is moved under it and drops out of
                                // Entity::GetEntities (gCProjectile_PS::OnTouch, MoveToWorldNode)

void SetLastArrow(eCEntity* e)
{
    lastArrow = e;
    lastArrowId = eCEntityProxy(e).GetEntityID();
}

// The latest shootat arrow if it still exists.
Entity LastArrowEntity()
{
    if (!lastArrow) return Entity();
    return Entity(lastArrowId);
}
eCEntity* lastTarget = nullptr;

// QA: where the latest shootat arrow was in its first 1.5 s, relative to the shooter at the shot, one sample per 100 ms
// (a later LASTARROW only shows where it ended up).
struct Trace {
    DWORD start = 0;
    bCVector origin;
    std::vector<std::pair<DWORD, bCVector>> points;
} trace;

void TraceTick()
{
    if (!trace.start || !lastArrow) return;
    const DWORD t = GetTickCount() - trace.start;
    if (t > 1500) { trace.start = 0; return; }
    if (!trace.points.empty() && t < trace.points.back().first + 100) return;
    if (Entity arrow = LastArrowEntity(); arrow.GetInstance()) {
        trace.points.emplace_back(t, arrow.GetPosition() - trace.origin);
        return;
    }
    if (t < 300) return;  // a fresh arrow only shows up among the entities a frame later
    trace.points.emplace_back(t, bCVector(0.0f, -99999.0f, 0.0f));  // gone
    trace.start = 0;
}

bool IsProjectile(eCEntity* e)
{
    return e && e->GetPropertySet(eEPropertySetType_Projectile);
}

// QA mode 5: for the player, Shoot aims at PSCamera::GetScreenCenterPickPoint, and a bot steers no camera. While a
// QA shot runs, Script_Game.dll's import of it answers the target's chest instead, so the script's own shot (physics
// path, speed, everything else) flies at the target. bCVector GetScreenCenterPickPoint() const is __thiscall with a
// hidden result pointer; __fastcall (this, edx, out) takes the same registers and stack.
using PickFn = bCVector*(__fastcall*)(void const* camera, void* edx, bCVector* out);
PickFn realPick = nullptr;
bool aimActive = false;
bCVector aimPoint;

bCVector* __fastcall FakePick(void const* camera, void* edx, bCVector* out)
{
    if (!aimActive) return realPick(camera, edx, out);
    return new (out) bCVector(aimPoint);
}

// Points one import of `module` (by DLL and function name) at `replacement`; returns what it pointed at.
void* PatchImport(HMODULE module, char const* dll, char const* function, void* replacement)
{
    auto* base = reinterpret_cast<unsigned char*>(module);
    auto* dos = reinterpret_cast<IMAGE_DOS_HEADER*>(base);
    auto* nt = reinterpret_cast<IMAGE_NT_HEADERS*>(base + dos->e_lfanew);
    const IMAGE_DATA_DIRECTORY& dir = nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    for (auto* imp = reinterpret_cast<IMAGE_IMPORT_DESCRIPTOR*>(base + dir.VirtualAddress); imp->Name; ++imp) {
        if (_stricmp(reinterpret_cast<char const*>(base + imp->Name), dll)) continue;
        auto* names = reinterpret_cast<IMAGE_THUNK_DATA*>(base + (imp->OriginalFirstThunk ? imp->OriginalFirstThunk : imp->FirstThunk));
        auto* slots = reinterpret_cast<IMAGE_THUNK_DATA*>(base + imp->FirstThunk);
        for (; names->u1.AddressOfData; ++names, ++slots) {
            if (IMAGE_SNAP_BY_ORDINAL(names->u1.Ordinal)) continue;
            auto* byName = reinterpret_cast<IMAGE_IMPORT_BY_NAME*>(base + names->u1.AddressOfData);
            if (std::strcmp(reinterpret_cast<char const*>(byName->Name), function)) continue;
            DWORD old = 0;
            if (!VirtualProtect(&slots->u1.Function, sizeof(slots->u1.Function), PAGE_READWRITE, &old)) return nullptr;
            void* previous = reinterpret_cast<void*>(slots->u1.Function);
            slots->u1.Function = reinterpret_cast<DWORD>(replacement);
            VirtualProtect(&slots->u1.Function, sizeof(slots->u1.Function), old, &old);
            return previous;
        }
    }
    return nullptr;
}

// QA mode 5: the bot shoots without the aiming motion's bow-forward pose, and the new arrow can start inside the
// hero's own body-part entities (Body_Player, Head_Player), whose collision Shoot does not switch off. Their collision
// is off for the shot and back on 400 ms later.
std::vector<eCEntity*> partsOff;
DWORD partsOnAt = 0;

bool PartOf(eCEntity* e, eCEntity* hero)
{
    for (eCEntity* p = e->GetParent(); p; p = p->GetParent())
        if (p == hero) return true;
    const char* name = Entity(e).GetName().GetText();
    const size_t n = std::strlen(name);
    return n > 7 && !std::strcmp(name + n - 7, "_Player");
}

int BodyPartsOff(Entity& self, char* names, size_t size)
{
    eCEntity* hero = self.GetInstance();
    const bTObjArray<Entity> all = Entity::GetEntities();
    for (int i = 0; i < all.GetCount(); ++i) {
        Entity e = all.GetAt(i);
        eCEntity* o = e.GetInstance();
        if (!o || o == hero || !o->HasCollision() || (e.GetPosition() - self.GetPosition()).GetMagnitude() > 250.0f) continue;
        if (!PartOf(o, hero)) continue;
        o->EnableCollision(GEFalse);
        partsOff.push_back(o);
        strncat_s(names, size, e.GetName().GetText(), _TRUNCATE);
        strncat_s(names, size, ",", _TRUNCATE);
    }
    partsOnAt = GetTickCount() + 400;
    return static_cast<int>(partsOff.size());
}

void BodyPartsOn()
{
    if (partsOff.empty() || static_cast<int32_t>(GetTickCount() - partsOnAt) < 0) return;
    const bTObjArray<Entity> all = Entity::GetEntities();  // only parts that still exist
    for (int i = 0; i < all.GetCount(); ++i)
        if (std::find(partsOff.begin(), partsOff.end(), all.GetAt(i).GetInstance()) != partsOff.end())
            all.GetAt(i).GetInstance()->EnableCollision(GETrue);
    partsOff.clear();
}

bool AimHooked()
{
    static bool tried = false;
    if (!tried) {
        tried = true;
        if (HMODULE game = GetModuleHandleA("Script_Game.dll"))
            realPick = reinterpret_cast<PickFn>(
                PatchImport(game, "Script.dll", "?GetScreenCenterPickPoint@PSCamera@@QBE?AVbCVector@@XZ", reinterpret_cast<void*>(&FakePick)));
        Log(realPick ? "ranged: QA aim hook on GetScreenCenterPickPoint" : "ranged: QA aim hook not installed");
    }
    return realPick != nullptr;
}
}

void ShootAt(char const* argument, LogFn log)
{
    // "GUID", "GUID-mode" or "GUID-mode-draw" (QA, while comparing): mode 0 re-aim the script's arrow in a line, 1 leave
    // it (camera aim), 2 replace it by a fresh arrow sent as the NPC branch does, 3 turn its physics path to the target,
    // 4 as 3 with the target set, 5 the script aims at the target itself (see FakePick), with the hero's body parts out
    // of the way (see BodyPartsOff), 6 as 5 with the body parts left alone, 7 as 5 but three times as far and 6 m
    // higher (over what stands about in the village: an arrow that flies, for P2). For the player the script
    // shoots with ShootVelocity = the held arrow's DamageHitMultiplier x 200, which aiming raises while the bow is
    // drawn; "draw" (in percent, > 0; commands take no '.') sets that multiplier first.
    char guid[64] = {};
    strncpy_s(guid, argument, _TRUNCATE);
    int mode = homing ? 0 : 1;
    float draw = 0.0f;
    if (std::strlen(guid) > 36 && guid[36] == '-') {
        mode = std::atoi(guid + 37);
        if (char const* dash = std::strchr(guid + 37, '-')) draw = static_cast<float>(std::atoi(dash + 1)) / 100.0f;
        guid[36] = 0;
    }
    Entity self = Player();
    Entity target = EntityAt(guid);
    if (!self.GetInstance() || !target.GetInstance()) { log("SHOOTAT refused bad GUID, no such entity or no player"); return; }
    if (!self.Inventory.GetItemFromSlot(gESlot_RightHand).GetInstance()) { log("SHOOTAT refused nothing in the right hand"); return; }
    if (pending.shooter) { log("SHOOTAT refused the previous shot is still pending"); return; }
    Entity held = self.Inventory.GetItemFromSlot(gESlot_RightHand);
    if (draw > 0.0f) held.Damage.DamageHitMultiplier = draw;
    pending.heldMultiplier = static_cast<GEFloat>(held.Damage.DamageHitMultiplier);
    pending.heldOffset = (held.GetPosition() - self.GetPosition()).GetMagnitude();  // Shoot spawns the arrow there
    pending.before.clear();
    const bTObjArray<Entity> old = Entity::GetEntities();
    for (int i = 0; i < old.GetCount(); ++i)
        if (IsProjectile(old.GetAt(i).GetInstance())) pending.before.push_back(old.GetAt(i).GetInstance());
    pending.partsOff[0] = 0;
    if ((mode == 5 || mode == 6 || mode == 7) && AimHooked()) {
        aimPoint = target.GetPosition() + bCVector(0.0f, 100.0f, 0.0f);
        if (mode == 7) aimPoint = self.GetPosition() + (target.GetPosition() - self.GetPosition()) * 3.0f + bCVector(0.0f, 600.0f, 0.0f);
        aimActive = true;
        if (mode != 6) BodyPartsOff(self, pending.partsOff, sizeof(pending.partsOff));
    }
    pending.result = GetScriptAdminExt().CallScript(bCString("Shoot"), self.GetInstance(), target.GetInstance(), 0);
    aimActive = false;
    pending.shooter = self.GetInstance();
    pending.target = target.GetInstance();
    pending.since = GetTickCount();
    pending.log = log;
    pending.mode = mode;
    summon::WatchHits();
}

void Tick()
{
    TraceTick();
    BodyPartsOn();
    if (!pending.shooter) return;
    char line[900], right[64];  // around and parts_off included; Log takes 1024
    Entity self(pending.shooter), target(pending.target);
    int spawned = 0, flying = 0, path = -1, group = -1;
    char around[160] = "", others[120] = "";  // with parts_off the SHOOTAT line stays under 900
    float distance = 0.0f, velocity = -1.0f;
    const bTObjArray<Entity> now = Entity::GetEntities();
    for (int i = 0; i < now.GetCount(); ++i) {
        Entity arrow = now.GetAt(i);
        eCEntity* e = arrow.GetInstance();
        if (!IsProjectile(e) || std::find(pending.before.begin(), pending.before.end(), e) != pending.before.end()) continue;
        if (arrow.Interaction.GetOwner().GetInstance() != pending.shooter) {
            if (std::strlen(others) < sizeof(others) - 40) {  // new, but someone else's (or nobody's)
                strncat_s(others, arrow.Interaction.GetOwner().GetInstance() ? arrow.Interaction.GetOwner().GetName().GetText() : "none", _TRUNCATE);
                strncat_s(others, ",", _TRUNCATE);
            }
            continue;
        }
        ++spawned;
        lastTarget = pending.target;
        SetLastArrow(e);
        path = static_cast<int>(static_cast<gEProjectilePath>(arrow.Projectile.PathStyle));
        // What the new arrow could have touched at once: entities with collision within 150 cm (QA, while comparing).
        for (int j = 0; j < now.GetCount(); ++j) {
            Entity other = now.GetAt(j);
            eCEntity* o = other.GetInstance();
            if (!o || o == e || o == pending.shooter || !o->HasCollision()) continue;
            if ((other.GetPosition() - arrow.GetPosition()).GetMagnitude() > 150.0f) continue;
            if (std::strlen(around) < sizeof(around) - 40) {
                strncat_s(around, other.GetName().GetText(), _TRUNCATE);
                strncat_s(around, ",", _TRUNCATE);
            }
        }
        group = static_cast<int>(e->GetCollisionGroup());  // 6 once gCProjectile_PS::OnTouch has handled a touch
        velocity = static_cast<float>(arrow.Projectile.ShootVelocity);
        const bCVector chest = target.GetPosition() + bCVector(0.0f, 100.0f, 0.0f);
        if (pending.mode == 0) {
            distance = (chest - arrow.GetPosition()).GetMagnitude();
            arrow.Projectile.PathStyle = gEProjectilePath_Line;
            arrow.Projectile.TargetDirection = (chest - arrow.GetPosition()).GetNormalized();
            arrow.Projectile.SetTarget(target);
            if (pending.heldMultiplier > 0.0f) arrow.Projectile.ShootVelocity = pending.heldMultiplier * 200.0f;
            arrow.Projectile.Shoot();
        } else if (pending.mode == 3 || pending.mode == 4) {
            // The script's own physics path, only turned from the camera's aim to the target's chest (4: with the
            // target set, as the NPC branch does).
            distance = (chest - arrow.GetPosition()).GetMagnitude();
            arrow.Projectile.TargetDirection = (chest - arrow.GetPosition()).GetNormalized();
            if (pending.mode == 4) arrow.Projectile.SetTarget(target);
            arrow.Projectile.Shoot();
        } else if (pending.mode == 2) {
            // The script's NPC branch, on a fresh arrow from the same template with the player's damage.
            Entity shot = Entity::Spawn(arrow.GetTemplate(), arrow.GetPose());
            if (!shot.GetInstance()) continue;
            shot.Damage.DamageAmount = static_cast<GEI32>(arrow.Damage.DamageAmount);
            shot.Damage.DamageHitMultiplier = static_cast<GEFloat>(arrow.Damage.DamageHitMultiplier);
            shot.Damage.ManaUsed = 0;
            shot.Interaction.SetOwner(self);
            shot.Strip.Enabled = GETrue;
            shot.CollisionShape.CreateShape(eECollisionShapeType_Point, eEShapeGroup_Projectile, bCVector(0.0f, 0.0f, 0.0f), bCVector(0.0f, 0.0f, 0.0f));
            shot.EnableCollisionWith(self, GEFalse);
            if (Entity bow = self.Inventory.GetItemFromSlot(gESlot_LeftHand); bow.GetInstance()) shot.EnableCollisionWith(bow, GEFalse);
            distance = (chest - shot.GetPosition()).GetMagnitude();
            shot.Projectile.PathStyle = gEProjectilePath_Line;
            shot.Projectile.TouchBehavior = gEProjectileTouchBehavior_MaterialDependant;
            shot.Projectile.TargetDirection = (chest - shot.GetPosition()).GetNormalized();
            shot.Projectile.SetTarget(target);
            shot.Projectile.Shoot();
            arrow.Kill();
            SetLastArrow(shot.GetInstance());
            if (shot.Projectile.IsFlying()) ++flying;
            continue;
        }
        if (arrow.Projectile.IsFlying()) ++flying;
    }
    const DWORD waited = GetTickCount() - pending.since;
    if (!spawned && waited < 2000) return;
    sprintf_s(line,
              "SHOOTAT result=%d spawned=%d flying=%d mode=%d waited_ms=%lu target=%s distance_cm=%.0f right=%s held_multiplier=%.2f "
              "script_path=%d script_velocity=%.0f script_group=%d around=%s parts_off=%s others=%s held_cm=%.0f",
              pending.result, spawned, flying, pending.mode, waited, target.GetName().GetText(), distance,
              HeldName(self, gESlot_RightHand, right, sizeof(right)), pending.heldMultiplier, path, velocity, group, around, *pending.partsOff ? pending.partsOff : "-", *others ? others : "-", pending.heldOffset);
    pending.log(line);
    pending.shooter = nullptr;
    trace.points.clear();
    trace.origin = self.GetPosition();
    trace.start = spawned ? GetTickCount() : 0;
}

// Where the latest shootat arrow is. Only for an arrow found among the world's entities (it may have been removed).
void LastArrow(LogFn log)
{
    char line[900], path[560] = "";  // at most 16 samples of 33 chars; Log takes 1024
    for (auto const& [t, at] : trace.points) {
        char point[48];
        sprintf_s(point, at.GetY() < -99998.0f ? "%lu:gone," : "%lu:%.0f_%.0f_%.0f,", t, at.GetX(), at.GetY(), at.GetZ());
        strncat_s(path, point, _TRUNCATE);
    }
    if (Entity arrow = LastArrowEntity(); arrow.GetInstance()) {
        const bCVector at = arrow.GetPosition();
        const float toTarget = lastTarget ? (Entity(lastTarget).GetPosition() - at).GetMagnitude() : -1.0f;
        sprintf_s(line, "LASTARROW at=%.0f_%.0f_%.0f flying=%d path=%d velocity=%.0f to_target_cm=%.0f group=%d trace=%s", at.GetX(), at.GetY(),
                  at.GetZ(), arrow.Projectile.IsFlying() ? 1 : 0, static_cast<int>(static_cast<gEProjectilePath>(arrow.Projectile.PathStyle)),
                  static_cast<float>(arrow.Projectile.ShootVelocity), toTarget, static_cast<int>(arrow.GetInstance()->GetCollisionGroup()), path);
        log(line);
        return;
    }
    sprintf_s(line, "LASTARROW gone trace=%s", path);
    log(line);
}

// The way _AI_StartCastPhase casts (re/findings/ranged-ai-paths-b.md): phase 1 and 2 put the spell and the target on
// the caster and pay the minimum mana, phase 5 runs the spell's FuncOnCast, or by spell type MagicProjectile,
// MagicInstant or MagicSummon, with self = the caster, other = the target and the spell's inventory stack.
void CastAt(char const* guid, LogFn log)
{
    char line[400];
    Entity self = Player();
    Entity target = EntityAt(guid);
    if (!self.GetInstance() || !target.GetInstance()) { log("CASTAT refused bad GUID, no such entity or no player"); return; }
    const Entity item = self.Inventory.GetItemFromSlot(gESlot_RightHand);
    Entity spell = item.GetInstance() ? item.Item.GetSpell() : Entity();
    if (!spell.GetInstance()) { log("CASTAT refused no spell in the right hand"); return; }
    const GEInt stack = self.Inventory.FindStackIndex(item.GetTemplate());
    const GEInt cost = spell.Magic.MinManaCost;
    const GEInt manaBefore = ManaOf(self.GetInstance());
    self.Interaction.SetSpell(spell);
    self.NPC.SetSpellTarget(target);
    self.NPC.LastSpell = spell.GetName();
    self.NPC.ManaUsed = cost;
    gCScriptAdminExt& admin = GetScriptAdminExt();
    if (item.Item.GetCategory() != 4) admin.CallScript(bCString("AddManaPoints"), self.GetInstance(), nullptr, -cost);
    bCString script = spell.Magic.FuncOnCast;
    const gESpellType type = spell.Magic.SpellType;
    if (script.IsEmpty())
        script = type == gESpellType_Projectile ? "MagicProjectile" : type == gESpellType_Target ? "MagicInstant" : "MagicSummon";
    const Entity spawn = spell.Magic.GetSpawn();
    const GEInt result = admin.CallScript(script, self.GetInstance(), target.GetInstance(), stack);
    sprintf_s(line, "CASTAT spell=%s type=%d script=%s spawn=%s stack=%d cost=%d result=%d mana=%d..%d target=%s", spell.GetName().GetText(),
              static_cast<int>(type), script.GetText(), spawn.GetInstance() ? spawn.GetName().GetText() : "-", stack, cost, result,
              manaBefore, ManaOf(self.GetInstance()), target.GetName().GetText());
    log(line);
}

// QA: every summoned entity (Smn_*) in the world: where it is, who owns it and which property sets it has.
void Summons(LogFn log)
{
    char line[600];
    int count = 0;
    const bTObjArray<Entity> all = Entity::GetEntities();
    for (int i = 0; i < all.GetCount(); ++i) {
        Entity e = all.GetAt(i);
        eCEntity* instance = e.GetInstance();
        if (!instance || std::strncmp(e.GetName().GetText(), "Smn_", 4)) continue;
        ++count;
        char sets[200] = "", number[8];
        for (int t = 1; t < 120; ++t)
            if (instance->GetPropertySet(static_cast<eEPropertySetType>(t))) { sprintf_s(number, "%d,", t); strcat_s(sets, number); }
        const eCEntityProxy proxy(instance);
        const _GUID& g = reinterpret_cast<_GUID const&>(proxy.GetEntityID().GetCore());
        const bCVector at = e.GetPosition();
        const Entity owner = e.Interaction.GetOwner();
        sprintf_s(line, "SUMMON name=%s guid=%08lX-%04X-%04X at=%.0f_%.0f_%.0f owner=%s puppet_owner=%d flying=%d sets=%s", e.GetName().GetText(),
                  g.Data1, g.Data2, g.Data3, at.GetX(), at.GetY(), at.GetZ(), owner.GetInstance() ? owner.GetName().GetText() : "-",
                  owner.GetInstance() ? remote::PuppetOwner(owner.GetInstance()) : 0,
                  instance->GetPropertySet(eEPropertySetType_Projectile) && e.Projectile.IsFlying() ? 1 : 0, sets);
        log(line);
    }
    sprintf_s(line, "SUMMONS end count=%d", count);
    log(line);
}

void Mana(LogFn log)
{
    char line[120];
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    if (!hero) { log("MANA refused"); return; }
    Entity self(hero);
    if (self.PlayerMemory.GetManaPointsMax() < 200) self.PlayerMemory.SetManaPointsMax(200);
    self.PlayerMemory.SetManaPoints(self.PlayerMemory.GetManaPointsMax());
    sprintf_s(line, "MANA now=%d max=%d", self.PlayerMemory.GetManaPoints(), self.PlayerMemory.GetManaPointsMax());
    log(line);
}

void Flying(LogFn log)
{
    char line[300];
    int projectiles = 0, meteors = 0;
    char first[64] = "-", where[64] = "-";
    const bTObjArray<Entity> all = Entity::GetEntities();
    for (int i = 0; i < all.GetCount(); ++i) {
        Entity e = all.GetAt(i);
        eCEntity* instance = e.GetInstance();
        if (!instance) continue;
        if (instance->GetPropertySet(eEPropertySetType_Projectile)) {
            if (!projectiles) {
                strncpy_s(first, e.GetName().GetText(), _TRUNCATE);
                const bCVector& at = instance->GetWorldPosition();
                sprintf_s(where, "%.0f_%.0f_%.0f", at.GetX(), at.GetY(), at.GetZ());
            }
            ++projectiles;
        }
    }
    for (int i = 0; i < all.GetCount(); ++i)  // the meteor is a spawned entity (Smn_Meteor, MagicSummon), not the spell item
        if (all.GetAt(i).GetInstance() && !std::strncmp(all.GetAt(i).GetName().GetText(), "Smn_", 4)) ++meteors;
    sprintf_s(line, "FLYING projectiles=%d first=%s at=%s meteors=%d", projectiles, first, where, meteors);
    log(line);
}
}
