// SPDX-License-Identifier: GPL-3.0-or-later
#include "explore.h"

#include "ai.h"
#include "body.h"
#include "guards.h"
#include "npcs.h"
#include "remote.h"
#include "net.h"
#include "persist.h"
#include "infocmd.h"

#include <Game.h>
#include <Script.h>
#include <Windows.h>
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <string>
#include <vector>

namespace g3t::explore {
namespace {
// QA scriptwatch: count the calls of up to eight scripts (by the script table), with the last call's self and other.
constexpr int WatchMax = 8;
struct Watched {
    std::string name, self, other;
    gFScript original = nullptr;
    unsigned calls = 0;
};
Watched watched[WatchMax];
int watchedCount = 0;

template <int I>
GEInt GE_STDCALL WatchWrap(gCScriptProcessingUnit* spu, GELPVoid self, GELPVoid other, GEInt args)
{
    Watched& w = watched[I];
    ++w.calls;
    Entity s = self ? *static_cast<Entity*>(self) : Entity(spu ? spu->GetSelfEntity() : nullptr);
    Entity o = other ? *static_cast<Entity*>(other) : Entity(spu ? spu->GetOtherEntity() : nullptr);
    w.self = s.GetInstance() ? s.GetName().GetText() : "-";
    w.other = o.GetInstance() ? o.GetName().GetText() : "-";
    return w.original(spu, self, other, args);
}
const gFScript watchWrappers[WatchMax] = {&WatchWrap<0>, &WatchWrap<1>, &WatchWrap<2>, &WatchWrap<3>,
                                          &WatchWrap<4>, &WatchWrap<5>, &WatchWrap<6>, &WatchWrap<7>};

bool ParseXyz(char const* text, float out[3])
{
    return sscanf_s(text, "%f_%f_%f", &out[0], &out[1], &out[2]) == 3;
}

void PositionOf(eCEntity* entity, float out[3])
{
    const bCVector& p = entity->GetWorldPosition();
    out[0] = p.GetX();
    out[1] = p.GetY();
    out[2] = p.GetZ();
}
}

void Summary(LogFn log)
{
    char line[220];
    eCEntity* hero = gCSession::GetInstance().GetHero();
    if (!hero) { log("REFUSED npcsummary: no hero"); return; }
    float at[3];
    PositionOf(hero, at);
    struct Row { float distance; Entity npc; };
    std::vector<Row> rows;
    const bTObjArray<Entity> all = Entity::GetNPCs();
    for (int i = 0; i < all.GetCount(); ++i) {
        Entity npc = all.GetAt(i);
        eCEntity* instance = npc.GetInstance();
        if (!instance || instance == hero || remote::IsPuppet(instance)) continue;
        float p[3];
        PositionOf(instance, p);
        rows.push_back({body::Distance(p, at), npc});
    }
    std::sort(rows.begin(), rows.end(), [](Row const& a, Row const& b) { return a.distance < b.distance; });
    sprintf_s(line, "NPCSUMMARY npcs=%zu entities=%d farthest_m=%.0f", rows.size(), Entity::GetEntities().GetCount(),
              rows.empty() ? 0.0f : rows.back().distance / 100.0f);
    log(line);
    // About 200 samples spread over all distances: enough to pick targets at any range.
    for (size_t i = 0; i < rows.size(); i += (rows.size() > 200 ? rows.size() / 200 : 1)) {
        float p[3];
        PositionOf(rows[i].npc.GetInstance(), p);
        sprintf_s(line, "NPCAT name=%s distance_m=%.0f pos=%.0f_%.0f_%.0f dead=%d", rows[i].npc.GetName().GetText(),
                  rows[i].distance / 100.0f, p[0], p[1], p[2], rows[i].npc.IsDead() ? 1 : 0);
        log(line);
    }
    log("NPCSUMMARY end");
}

void Teleport(char const* xyz, LogFn log)
{
    char line[200];
    float target[3];
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    if (!hero || !ParseXyz(xyz, target)) { log("REFUSED teleport: no hero or bad X_Y_Z"); return; }
    const bCQuaternion q = hero->GetWorldRotation();
    const float rotation[4] = {q.GetX(), q.GetY(), q.GetZ(), q.GetW()};
    Entity(hero).MoveTo(body::Pose(*hero, target, rotation));
    float now[3];
    PositionOf(hero, now);
    sprintf_s(line, "TELEPORT target=%s pos=%.0f_%.0f_%.0f", xyz, now[0], now[1], now[2]);
    log(line);
}

void Attitude(char const* name, LogFn log)
{
    char line[260];
    eCEntity* hero = gCSession::GetInstance().GetHero();
    Entity npc = Entity::GetEntity(bCString(name));
    if (!hero || !npc.GetInstance()) { log("REFUSED attitude: no hero or no such NPC"); return; }
    auto& admin = GetScriptAdminExt();
    const GEInt toHero = admin.CallScript(bCString("GetAttitude"), npc.GetInstance(), hero, 0);
    eCEntity* puppet = remote::PuppetOf(2);
    const GEInt toPuppet = puppet ? admin.CallScript(bCString("GetAttitude"), npc.GetInstance(), puppet, 0) : -1;
    // P7 (26.10.): the parts the game asks for its own player, as this machine's hero sees them (the focus name's colour).
    const GEInt toPlayer = admin.CallScript(bCString("GetAttitudeToPlayer"), npc.GetInstance(), hero, 0);
    const GEInt focus = admin.CallScript(bCString("GetFocusNameAttitude"), npc.GetInstance(), hero, 0);
    sprintf_s(line, "ATTITUDE name=%s hero=%d puppet=%d task=%s to_player=%d focus=%d", name, toHero, toPuppet, npc.Routine.GetCurrentTask().GetText(),
              toPlayer, focus);
    log(line);
}

void HeroPos(LogFn log)
{
    char line[200];
    eCEntity* body = remote::ControlledBody();
    if (!body) { log("REFUSED heropos: no hero"); return; }
    float at[3];
    PositionOf(body, at);
    Entity zone = Entity(body).GetZone();
    sprintf_s(line, "HEROPOS pos=%.1f,%.1f,%.1f zone=%s ani_state=%d", at[0], at[1], at[2], zone.GetInstance() ? zone.GetName().GetText() : "-",
              static_cast<int>(Entity(body).Routine.AniState));
    log(line);
}

// QA: the hero runs to a point ("X_Y_Z" in cm) the game's way (AI goto, as walkto): moving, NPCs near it can hear it.
void RunTo(char const* argument, LogFn log)
{
    char line[200];
    float target[3];
    eCEntity* body = remote::ControlledBody();
    if (!body || sscanf_s(argument, "%f_%f_%f", &target[0], &target[1], &target[2]) != 3) { log("REFUSED runto: no hero or not X_Y_Z"); return; }
    const GEBool started = Entity(body).StartGoto(bCVector(target[0], target[1], target[2]), gEWalkMode_Run);
    sprintf_s(line, "RUNTO started=%d target=%.0f,%.0f,%.0f", started ? 1 : 0, target[0], target[1], target[2]);
    log(line);
}

// QA: set one of the hero's attributes ("STR_300": tag and base value, the tags of hero.cpp), e.g. for a strength test.
void SetAttribute(char const* argument, LogFn log)
{
    char line[160], tag[8] = {};
    int value = 0;
    eCEntity* hero = gCSession::GetInstance().GetHero();
    if (!hero || sscanf_s(argument, "%3[A-Z]_%d", tag, static_cast<unsigned>(sizeof(tag)), &value) != 2) { log("REFUSED setattr: no hero or not TAG_value"); return; }
    Entity self(hero);
    self.PlayerMemory.SetBaseValue(bCString(tag), value);
    sprintf_s(line, "SETATTR tag=%s value=%d strength=%d", tag, value, static_cast<int>(self.PlayerMemory.GetStrength()));
    log(line);
}

// QA: look at a named entity: the hero 4 m away from it on the line from where it stands, facing it; the camera behind
// the hero then shows the entity (the engine animates bodies on screen; deaddiag measures corpses this way).
void LookAt(char const* name, LogFn log)
{
    char line[200];
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    // "puppet<id>": the puppet of that player (its template name, PC_Hero, finds the hero itself).
    Entity target = !std::strncmp(name, "puppet", 6) ? Entity(remote::PuppetOf(std::atoi(name + 6))) : Entity::GetEntity(bCString(name));
    if (!hero || !target.GetInstance()) { log("REFUSED lookat: no hero or no such entity"); return; }
    float from[3], at[3];
    PositionOf(hero, from);
    PositionOf(target.GetInstance(), at);
    float dx = at[0] - from[0], dz = at[2] - from[2];
    const float length = std::sqrt(dx * dx + dz * dz);
    if (length < 1.0f) dx = 0.0f, dz = 1.0f;
    else dx /= length, dz /= length;
    const float yaw = std::atan2(dx, dz);
    const float rotation[4] = {0.0f, std::sin(yaw / 2), 0.0f, std::cos(yaw / 2)};
    const float place[3] = {at[0] - dx * 400.0f, at[1] + 50.0f, at[2] - dz * 400.0f};
    Entity(hero).MoveTo(body::Pose(*hero, place, rotation));
    sprintf_s(line, "LOOKAT name=%s pos=%.0f_%.0f_%.0f", name, place[0], place[1], place[2]);
    log(line);
}

void Near(char const* xyz, LogFn log)
{
    char line[260];
    float at[3];
    if (!ParseXyz(xyz, at)) { log("REFUSED near: bad X_Y_Z"); return; }
    eCEntity* hero = gCSession::GetInstance().GetHero();
    const bTObjArray<Entity> all = Entity::GetNPCs();
    int count = 0;
    for (int i = 0; i < all.GetCount(); ++i) {
        Entity npc = all.GetAt(i);
        eCEntity* instance = npc.GetInstance();
        if (!instance || instance == hero || remote::IsPuppet(instance)) continue;
        float p[3];
        PositionOf(instance, p);
        if (body::Distance(p, at) > 4000.0f) continue;
        sprintf_s(line, "NEAR name=%s pos=%.0f_%.0f_%.0f task=%s hp=%d dead=%d mirrored=%d", npc.GetName().GetText(), p[0], p[1], p[2],
                  npc.Routine.GetCurrentTask().GetText(), static_cast<int>(npc.DamageReceiver.HitPoints), npc.IsDead() ? 1 : 0,
                  npcs::IsMirrored(instance) ? 1 : 0);
        log(line);
        ++count;
    }
    int entities = 0;
    const bTObjArray<Entity> every = Entity::GetEntities();
    for (int i = 0; i < every.GetCount(); ++i) {
        eCEntity* instance = every.GetAt(i).GetInstance();
        if (!instance) continue;
        float p[3];
        PositionOf(instance, p);
        if (body::Distance(p, at) <= 4000.0f) ++entities;
    }
    sprintf_s(line, "NEARSUM at=%s npcs=%d entities=%d", xyz, count, entities);
    log(line);
    log("NEAR end");
}
}

namespace g3t::explore {
// QA: NPCs anywhere in the world, found through the owners of dialog options (names resolve even far away?),
// as teleport targets on solid ground. FARNPC lines, then "FARNPC end".
void FarNpcs(LogFn log)
{
    char line[220];
    eCEntity* hero = gCSession::GetInstance().GetHero();
    gCInfoManager_PS* infos = gCInfoManager_PS::GetInstance();
    if (!hero || !infos) { log("REFUSED farnpcs: no hero or info manager"); return; }
    float at[3];
    PositionOf(hero, at);
    std::vector<std::string> owners;
    auto& all = infos->AccessInfos();
    for (int i = 0; i < all.GetCount(); ++i)
        if (gCInfo_PS* info = all.AccessAt(i)) {
            std::string owner = info->AccessOwner().GetText();
            if (!owner.empty() && std::find(owners.begin(), owners.end(), owner) == owners.end()) owners.push_back(owner);
        }
    int resolved = 0;
    struct Row { float distance; std::string name; float pos[3]; };
    std::vector<Row> rows;
    for (auto const& owner : owners) {
        Entity npc = Entity::GetEntity(bCString(owner.c_str()));
        if (!npc.GetInstance()) continue;
        ++resolved;
        Row row{0, owner, {}};
        PositionOf(npc.GetInstance(), row.pos);
        row.distance = body::Distance(row.pos, at);
        rows.push_back(row);
    }
    std::sort(rows.begin(), rows.end(), [](Row const& a, Row const& b) { return a.distance < b.distance; });
    sprintf_s(line, "FARNPCS owners=%zu resolved=%d", owners.size(), resolved);
    log(line);
    for (size_t i = 0; i < rows.size(); i += (rows.size() > 60 ? rows.size() / 60 : 1)) {
        sprintf_s(line, "FARNPC name=%s distance_m=%.0f pos=%.0f_%.0f_%.0f", rows[i].name.c_str(), rows[i].distance / 100.0f,
                  rows[i].pos[0], rows[i].pos[1], rows[i].pos[2]);
        log(line);
    }
    log("FARNPC end");
}
}

namespace g3t::explore {
// QA: one named entity wherever it is: position, task, distance to the hero.
void Find(char const* name, LogFn log)
{
    char line[240];
    eCEntity* hero = gCSession::GetInstance().GetHero();
    Entity npc = Entity::GetEntity(bCString(name));
    if (!hero || !npc.GetInstance()) { sprintf_s(line, "FIND name=%s found=0", name); log(line); return; }
    float p[3], h[3];
    PositionOf(npc.GetInstance(), p);
    PositionOf(hero, h);
    const bool damageable = npc.GetInstance()->GetPropertySet(eEPropertySetType_DamageReceiver) != nullptr;
    Entity target = npc.GetInstance()->GetPropertySet(eEPropertySetType_NPC) ? npc.NPC.GetCurrentTarget() : Entity();
    sprintf_s(line, "FIND name=%s found=1 pos=%.0f_%.0f_%.0f task=%s distance_m=%.0f mirrored=%d hp=%d dead=%d state=%s target=%s ai_mode=%d target_puppet=%d target_hero=%d owner=%u", name,
              p[0], p[1], p[2], npc.Routine.GetCurrentTask().GetText(), body::Distance(p, h) / 100.0f, npcs::IsMirrored(npc.GetInstance()) ? 1 : 0,
              damageable ? static_cast<int>(npc.DamageReceiver.HitPoints) : -1, npc.IsDead() ? 1 : 0, npc.Routine.GetCurrentState().GetText(),
              target.GetInstance() ? target.GetName().GetText() : "-", static_cast<int>(npc.Routine.AIMode),
              remote::PuppetOwner(target.GetInstance()), target.GetInstance() && target.GetInstance() == hero ? 1 : 0,
              npcs::OwnerOf(npc.GetInstance()));
    log(line);
}

// QA, regional-authority spike: kill a named NPC wherever it is, the way the host kills for a client
// (hit points to 0, then the kill cheat with the hero as killer). Far NPCs are not processed by the engine.
void FarKill(char const* name, LogFn log)
{
    char line[200];
    eCEntity* hero = gCSession::GetInstance().GetHero();
    Entity npc = Entity::GetEntity(bCString(name));
    unsigned d1, d2, d3, b[8];  // a GUID instead of a name: that one NPC (stageTownFree: a town's orcs share names)
    if (sscanf_s(name, "%8X-%4X-%4X-%2X%2X-%2X%2X%2X%2X%2X%2X", &d1, &d2, &d3, &b[0], &b[1], &b[2], &b[3], &b[4], &b[5], &b[6], &b[7]) == 11) {
        _GUID raw{d1, static_cast<unsigned short>(d2), static_cast<unsigned short>(d3), {}};
        for (int i = 0; i < 8; ++i) raw.Data4[i] = static_cast<unsigned char>(b[i]);
        npc = Entity(bCPropertyID(bCGuid(raw)));
    }
    if (!hero || !npc.GetInstance() || npc.IsDead() || !npc.GetInstance()->GetPropertySet(eEPropertySetType_DamageReceiver)) {
        sprintf_s(line, "REFUSED farkill %s: no hero, no such NPC, already dead or not damageable", name);
        log(line);
        return;
    }
    auto& admin = GetScriptAdminExt();
    admin.CallScript(bCString("AddHitPoints"), npc.GetInstance(), hero, -static_cast<GEInt>(npc.DamageReceiver.HitPoints));
    admin.CallScript(bCString("OnCheatKill"), hero, npc.GetInstance(), 0);
    sprintf_s(line, "FARKILL name=%s hp=%d dead=%d task=%s", name, static_cast<int>(npc.DamageReceiver.HitPoints), npc.IsDead() ? 1 : 0,
              npc.Routine.GetCurrentTask().GetText());
    log(line);
}

// QA (main story): the chapter lives in the local hero's PlayerMemory (SetChapter, info command 53); GetAttitude reads it
// through Entity::GetPlayer (chapter 2: the chosen god's enemies, re/out/notify-enclave-predicate-20261024-c.c).
void Chapter(LogFn log)
{
    char line[80];
    eCEntity* hero = gCSession::GetInstance().GetHero();
    sprintf_s(line, "CHAPTER value=%d", hero ? static_cast<int>(Entity(hero).PlayerMemory.Chapter) : -1);
    log(line);
}

// QA (P2, PS_QuickUse): the item on quick slot 1 and the Community Patch's key handler for it (OnPlayerUseQuickSlot, Script_paru
// 0xd450: FindStackIndexByQuickSlot, ExecuteItem), as pressing the slot's key does.
void QuickUse(char const* item, LogFn log)
{
    char line[200];
    eCEntity* hero = gCSession::GetInstance().GetHero();
    if (!hero) { log("REFUSED quickuse: no hero"); return; }
    Entity self(hero);
    const GEInt stack = self.Inventory.FindStackIndex(Template(item));
    if (stack < 0) { sprintf_s(line, "REFUSED quickuse %s: not carried", item); log(line); return; }
    const GEInt before = self.Inventory.GetAmount(stack);
    const GEBool assigned = self.Inventory.AssignStackToQuickSlot(stack, 1);
    const GEInt result = GetScriptAdminExt().CallScript(bCString("OnPlayerUseQuickSlot"), hero, nullptr, 1);
    sprintf_s(line, "QUICKUSE item=%s stack=%d before=%d assigned=%d result=%d task=%s", item, stack, before, assigned ? 1 : 0, result,
              self.Routine.GetCurrentTask().GetText());
    log(line);
}

// QA (P2, PS_Swim): gECharMovementMode of the hero and of each puppet (7 swim, 8 dive, 10 wade).
void MoveMode(LogFn log)
{
    char line[200];
    eCEntity* hero = gCSession::GetInstance().GetHero();
    int n = sprintf_s(line, "MOVEMODE hero=%d", hero ? static_cast<int>(Entity(hero).CharacterMovement.GetMovementMode()) : -1);
    for (int id = 1; id <= 4 && n > 0; ++id)
        if (eCEntity* puppet = remote::PuppetOf(id))
            n += sprintf_s(line + n, sizeof(line) - n, " puppet%d=%d", id, static_cast<int>(Entity(puppet).CharacterMovement.GetMovementMode()));
    log(line);
}

void Sector(char const* name, LogFn log)
{
    char line[160];
    sprintf_s(line, "SECTOR name=%s on=%d", name, world.GetSectorStatus(bCString(name)) ? 1 : 0);
    log(line);
}

// QA (stageEscort): a named NPC wherever it is gets its hit points back (the weak escaped slave died to wolves on the
// way to Freman, 25.10.).
void FarHeal(char const* name, LogFn log)
{
    char line[200];
    eCEntity* hero = gCSession::GetInstance().GetHero();
    Entity npc = Entity::GetEntity(bCString(name));
    if (!hero || !npc.GetInstance() || npc.IsDead() || !npc.GetInstance()->GetPropertySet(eEPropertySetType_DamageReceiver)) {
        sprintf_s(line, "REFUSED farheal %s: no hero, no such NPC, dead or not damageable", name);
        log(line);
        return;
    }
    const int before = static_cast<int>(npc.DamageReceiver.HitPoints);
    GetScriptAdminExt().CallScript(bCString("AddHitPoints"), npc.GetInstance(), hero, 100000);
    sprintf_s(line, "FARHEAL name=%s before=%d hp=%d", name, before, static_cast<int>(npc.DamageReceiver.HitPoints));
    log(line);
}

// QA, regional-authority spike: move a named entity wherever it is ("X_Y_Z" in cm), keeping its rotation.
void FarMove(char const* argument, LogFn log)
{
    char line[200], name[96] = {};
    float target[3];
    // QA commands take one token of [A-Za-z0-9_-]: X_Y_Z_<name>, the name after the third underscore.
    int consumed = 0;
    if (sscanf_s(argument, "%f_%f_%f_%n", &target[0], &target[1], &target[2], &consumed) != 3 || !consumed ||
        strcpy_s(name, argument + consumed)) {
        log("REFUSED farmove: X_Y_Z_<name>");
        return;
    }
    Entity entity = Entity::GetEntity(bCString(name));
    if (!entity.GetInstance()) { sprintf_s(line, "REFUSED farmove %s: not found", name); log(line); return; }
    const bCQuaternion q = entity.GetInstance()->GetWorldRotation();
    const float rotation[4] = {q.GetX(), q.GetY(), q.GetZ(), q.GetW()};
    entity.MoveTo(body::Pose(*entity.GetInstance(), target, rotation));
    float now[3];
    PositionOf(entity.GetInstance(), now);
    sprintf_s(line, "FARMOVE name=%s target=%.0f_%.0f_%.0f pos=%.0f_%.0f_%.0f", name, target[0], target[1], target[2], now[0], now[1],
              now[2]);
    log(line);
}
}

namespace g3t::explore {
// QA: the game's own teleport of the hero to a named entity (as teleport stones do), not a bare MoveTo.
void TeleportTo(char const* name, LogFn log)
{
    char line[200];
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    Entity target = Entity::GetEntity(bCString(name));
    if (!hero || !target.GetInstance()) { log("REFUSED teleportto: no hero or target"); return; }
    const GEBool ok = Entity(hero).Teleport(target);
    float p[3];
    PositionOf(hero, p);
    sprintf_s(line, "TELEPORTTO name=%s ok=%d pos=%.0f_%.0f_%.0f", name, ok ? 1 : 0, p[0], p[1], p[2]);
    log(line);
}
}

namespace g3t::explore {
// QA: the GUI pages that may pause the game.
void Page(char const* what, LogFn log)
{
    char line[200];
    gCSession& session = gCSession::GetInstance();
    gCGUIManager* gui = session.GetGUIManager();
    if (!gui) { log("REFUSED page: no GUI manager"); return; }
    GEBool ok = GETrue;
    if (!std::strcmp(what, "inventory")) ok = gui->OpenInventory();
    else if (!std::strcmp(what, "menu")) session.OpenMenu();
    else if (!std::strcmp(what, "close")) { ok = gui->ClosePage(); gui->CloseMenu(); }
    else if (std::strcmp(what, "state")) { log("REFUSED page: inventory|menu|close|state"); return; }
    sprintf_s(line, "PAGE what=%s ok=%d paused=%d active_page=%d", what, ok ? 1 : 0, session.IsPaused() ? 1 : 0,
              static_cast<int>(gui->GetActivePage()));
    log(line);
}
}

namespace g3t::explore {
namespace {
// Game.dll internals of this exact build (Steam 1037023, CP 1.75.14): gCHUDFileManager's singleton getter,
// SaveGame(name) and LoadGame(name), both __stdcall(this, name) (the prologue reads `this` from [ebp+8] and
// checks its fields; a __thiscall call made the game look for an empty file name). The first bytes are checked before every call, so a
// different build is refused instead of jumped into.
struct Internal { uintptr_t rva; char const* bytes; };
const Internal FileManagerGetter{0xa92d0, "b80100000084"};
const Internal SaveGameFn{0xa9e90, "558bec83e4f883ec1c535657"};
const Internal LoadGameFn{0xa9a10, "558bec83e4f88b450883ec5c83780400"};

void* Resolve(Internal const& fn)
{
    HMODULE game = GetModuleHandleA("Game.dll");
    if (!game) return nullptr;
    auto const* code = reinterpret_cast<unsigned char const*>(reinterpret_cast<uintptr_t>(game) + fn.rva);
    for (size_t i = 0; fn.bytes[2 * i]; ++i) {
        unsigned value = 0;
        sscanf_s(fn.bytes + 2 * i, "%2x", &value);
        if (code[i] != value) return nullptr;
    }
    return const_cast<unsigned char*>(code);
}

using Getter = void*(__cdecl*)();
using FileFn = bool(__stdcall*)(void* self, bCString const* name);
}

void NativeSave(char const* name, LogFn log)
{
    char line[200];
    auto getter = reinterpret_cast<Getter>(Resolve(FileManagerGetter));
    // Once hooked (persist.cpp) the entry starts with our jump; calling it goes through the hook, like the menu.
    auto save = reinterpret_cast<FileFn>(persist::SaveTarget() ? persist::SaveTarget() : Resolve(SaveGameFn));
    if (!getter || !save) { log("REFUSED nativesave: Game.dll does not match this build"); return; }
    // Puppets are entities from the hero's template; saved with the world they make the game abort on load
    // ("current world has more than one hero!"). Remove them first; they come back with the next state.
    remote::DespawnAll("native save");
    const bCString slot(name);
    const bool ok = save(getter(), &slot);
    sprintf_s(line, "NATIVESAVE name=%s ok=%d folder=%s", name, ok ? 1 : 0, gCWorld::GetSaveGameFolder().GetText());
    log(line);
}

void NativeLoad(char const* name, LogFn log)
{
    char line[200];
    auto getter = reinterpret_cast<Getter>(Resolve(FileManagerGetter));
    auto load = reinterpret_cast<FileFn>(persist::LoadTarget() ? persist::LoadTarget() : Resolve(LoadGameFn));
    if (!getter || !load) { log("REFUSED nativeload: Game.dll does not match this build"); return; }
    const bCString slot(name);
    // Loading a slot that does not exist stops the session and leaves the game hanging: check first.
    gCWorld* world = gCWorld::GetCurrentWorld();
    if (!world || !world->ExistsSaveGame(slot)) { sprintf_s(line, "NATIVELOAD name=%s ok=0 exists=0", name); log(line); return; }
    sprintf_s(line, "NATIVELOAD name=%s calling=1", name);
    log(line);
    const bool ok = load(getter(), &slot);
    sprintf_s(line, "NATIVELOAD name=%s ok=%d", name, ok ? 1 : 0);
    log(line);
}
}

namespace g3t::explore {
// QA, stage H2: dialog-relevant state of a named NPC; with `change`, flip its trade flag and guard status the way
// SetTradeEnabled / SetGuardStatus would, and pass the result on like after a dialog command.
void NpcState(char const* name, bool change, LogFn log)
{
    char line[300];
    Entity npc = Entity::GetEntity(bCString(name));
    eCEntity* instance = npc.GetInstance();
    if (!instance || !instance->GetPropertySet(eEPropertySetType_Dialog) || !instance->GetPropertySet(eEPropertySetType_NPC)) {
        sprintf_s(line, "REFUSED npcstate %s: no such NPC with a dialog", name);
        log(line);
        return;
    }
    if (change) {
        npc.Dialog.TradeEnabled = !npc.Dialog.TradeEnabled;
        npc.NPC.GuardStatus = npc.NPC.GuardStatus == gEGuardStatus_Active ? gEGuardStatus_Inactive : gEGuardStatus_Active;
        infocmd::Changed(instance);
    }
    auto* nav = static_cast<gCNavigation_PS*>(instance->GetPropertySet(eEPropertySetType_Navigation));
    sprintf_s(line, "NPCSTATE name=%s trade=%d teach=%d guard=%d routine=%s", name, npc.Dialog.TradeEnabled ? 1 : 0,
              npc.Dialog.TeachEnabled ? 1 : 0, static_cast<int>(npc.NPC.GuardStatus), nav ? nav->AccessRoutine().GetText() : "-");
    log(line);
}
}

namespace g3t::explore {
// QA (followdiag): the NPC stops and chooses its routine again, as at a dialog's end (FullStop, ContinueRoutine): with a
// follow quest running, SelectRoutineTask starts its guiding.
void NpcRoutine(char const* name, LogFn log)
{
    char line[200];
    Entity npc = Entity::GetEntity(bCString(name));
    if (!npc.GetInstance() || !npc.GetInstance()->GetPropertySet(eEPropertySetType_NPC)) { log("REFUSED npcroutine: no such NPC"); return; }
    npc.Routine.FullStop();
    npc.Routine.ContinueRoutine();
    sprintf_s(line, "NPCROUTINE name=%s task=%s", name, npc.Routine.GetCurrentTask().GetText());
    log(line);
}

// QA, stage H3: every dialog option whose script contains `command` (Give, Attack, JoinNPC, Spawn, ...), with
// its owner's distance from the hero, nearest first: candidates for tests with real dialogs.
void InfoScan(char const* command, LogFn log)
{
    char line[600];
    gCInfoManager_PS* manager = gCSession::GetInstance().IsGameRunning() ? gCInfoManager_PS::GetInstance() : nullptr;
    eCEntity* hero = gCSession::GetInstance().GetHero();
    if (!manager || !hero) { log("REFUSED infoscan: no game"); return; }
    struct Row { float distance; std::string text; };
    std::vector<Row> rows;
    float h[3];
    PositionOf(hero, h);
    auto& infos = manager->AccessInfos();
    // "type_<n>": every info of that condition type (17 = trade), one row each.
    const int condType = !std::strncmp(command, "type_", 5) ? std::atoi(command + 5) : -1;
    // "commands": every command name of every info's script, with how many lines use it and one example info.
    if (!std::strcmp(command, "commands")) {
        std::map<std::string, std::pair<int, std::string>> counts;
        for (int i = 0; i < infos.GetCount(); ++i) {
            gCInfo_PS* info = infos.AccessAt(i);
            gCInfoScript_PS* script = info ? info->AccessInfoScript().GetObject() : nullptr;
            for (int k = 0; script && k < script->AccessInfoScriptLines().GetCount(); ++k)
                if (auto* l = static_cast<gCInfoScriptLine_PS*>(script->AccessInfoScriptLines().AccessAt(k))) {
                    auto& c = counts[l->AccessCommand().GetText()];
                    if (!c.first++) c.second = info->AccessName().GetText();
                }
        }
        for (auto const& [name, c] : counts) {
            sprintf_s(line, "INFOSCAN command=%s lines=%d example=%s", name.c_str(), c.first, c.second.c_str());
            log(line);
        }
        sprintf_s(line, "INFOSCAN end command=commands matches=%zu", counts.size());
        log(line);
        return;
    }
    // "info_<name>": every script line of that info (command, entities, IDs), in order.
    char const* infoName = !std::strncmp(command, "info_", 5) ? command + 5 : nullptr;
    for (int i = 0; i < infos.GetCount(); ++i) {
        gCInfo_PS* info = infos.AccessAt(i);
        if (!info) continue;
        if (infoName) {
            gCInfoScript_PS* script = _stricmp(info->AccessName().GetText(), infoName) ? nullptr : info->AccessInfoScript().GetObject();
            for (int k = 0; script && k < script->AccessInfoScriptLines().GetCount(); ++k) {
                gCInfoScriptLine_PS* l = static_cast<gCInfoScriptLine_PS*>(script->AccessInfoScriptLines().AccessAt(k));
                if (!l) continue;
                sprintf_s(line, "INFOSCAN info=%s owner=%s line=%d cmd=%s e1=%s e2=%s id1=%s id2=%s type=%d quest=%s", infoName,
                          info->AccessOwner().GetText(), k, l->AccessCommand().GetText(), l->AccessEntity1().GetText(),
                          l->AccessEntity2().GetText(), l->AccessID1().GetText(), l->AccessID2().GetText(),
                          static_cast<int>(static_cast<gEInfoCondType const&>(info->AccessConditionType())), info->AccessQuest().GetText());
                rows.push_back({static_cast<float>(k), line});
            }
            continue;
        }
        if (condType >= 0) {
            if (static_cast<int>(static_cast<gEInfoCondType const&>(info->AccessConditionType())) != condType) continue;
            Entity owner = Entity::GetEntity(info->AccessOwner());
            float distance = 1e9f;
            if (owner.GetInstance()) { float p[3]; PositionOf(owner.GetInstance(), p); distance = body::Distance(p, h) / 100.0f; }
            std::string knows;
            auto const& cond = info->AccessConditionPlayerKnows();
            for (int c = 0; c < cond.GetCount(); ++c) knows += std::string(knows.empty() ? "" : "|") + cond.GetAt(c).GetText();
            sprintf_s(line, "INFOSCAN info=%s owner=%s distance_m=%.0f given=%d permanent=%d quest=%s parent=%s knows=%s",
                      info->AccessName().GetText(), info->AccessOwner().GetText(), distance, info->AccessInfoGiven() ? 1 : 0,
                      info->AccessPermanent() ? 1 : 0, info->AccessQuest().GetText(), info->AccessParent().GetText(),
                      knows.empty() ? "-" : knows.substr(0, 200).c_str());
            rows.push_back({distance, line});
            continue;
        }
        gCInfoScript_PS* script = info->AccessInfoScript().GetObject();
        if (!script) continue;
        auto& lines = script->AccessInfoScriptLines();
        for (int k = 0; k < lines.GetCount(); ++k) {
            gCInfoScriptLine_PS* l = static_cast<gCInfoScriptLine_PS*>(lines.AccessAt(k));
            if (!l || _stricmp(l->AccessCommand().GetText(), command) != 0) continue;
            Entity owner = Entity::GetEntity(info->AccessOwner());
            float distance = 1e9f;
            if (owner.GetInstance()) { float p[3]; PositionOf(owner.GetInstance(), p); distance = body::Distance(p, h) / 100.0f; }
            sprintf_s(line, "INFOSCAN info=%s owner=%s distance_m=%.0f given=%d quest=%s line=%d e1=%s e2=%s id1=%s id2=%s",
                      info->AccessName().GetText(), info->AccessOwner().GetText(), distance, info->AccessInfoGiven() ? 1 : 0,
                      info->AccessQuest().GetText(), k, l->AccessEntity1().GetText(), l->AccessEntity2().GetText(),
                      l->AccessID1().GetText(), l->AccessID2().GetText());
            rows.push_back({distance, line});
        }
    }
    std::sort(rows.begin(), rows.end(), [](Row const& a, Row const& b) { return a.distance < b.distance; });
    for (size_t i = 0; i < rows.size() && i < 25; ++i) log(rows[i].text.c_str());
    sprintf_s(line, "INFOSCAN end command=%s matches=%zu", command, rows.size());
    log(line);
}
}

namespace g3t::explore {
// QA, stage H3: let a named NPC attack player 2's puppet ("Name_Reason", reason a gEAttackReason number), the way
// the dialog command Attack does (the perception script AssessTarget).
void Aggro(char const* argument, LogFn log)
{
    char line[300], name[96] = {};
    char const* sep = std::strrchr(argument, '_');
    if (!sep || sep - argument >= static_cast<ptrdiff_t>(sizeof(name))) { log("REFUSED aggro: Name_Reason"); return; }
    std::memcpy(name, argument, sep - argument);
    const int reason = std::atoi(sep + 1);
    Entity npc = Entity::GetEntity(bCString(name));
    eCEntity* puppet = remote::PuppetOf(net::IsHost() ? 2 : 1);  // the other player of a two-player test
    if (!npc.GetInstance() || !puppet) { log("REFUSED aggro: no such NPC or no puppet"); return; }
    const GEInt result = GetScriptAdminExt().CallScript(bCString("AssessTarget"), npc.GetInstance(), puppet, reason);
    sprintf_s(line, "AGGRO name=%s reason=%d result=%d task=%s", name, reason, result, npc.Routine.GetCurrentTask().GetText());
    log(line);
}

namespace {
// One TSV field: UTF-8, without tabs or line breaks.
std::string Field(wchar_t const* text)
{
    char utf8[512] = {};
    if (text) WideCharToMultiByte(CP_UTF8, 0, text, -1, utf8, sizeof(utf8) - 1, nullptr, nullptr);
    std::string s(utf8);
    for (char& c : s)
        if (c == '\t' || c == '\r' || c == '\n') c = ' ';
    return s;
}

std::string Join(bTObjArray<bCString> const& list)
{
    std::string out;
    for (int i = 0; i < list.GetCount(); ++i) out += std::string(out.empty() ? "" : "|") + list.GetAt(i).GetText();
    return out.empty() ? "-" : out;
}
}

// Plan P0 (24.10.): every quest the game knows, with its journal folder (region or town), title in this game's language,
// type, status, experience, prerequisites and delivery targets: the base of the chapter checklist.
void QuestDump(char const* path, LogFn log)
{
    gCQuestManager_PS* manager = gCSession::GetInstance().IsGameRunning() ? gCQuestManager_PS::GetInstance() : nullptr;
    if (!manager) { log("QUESTDUMP refused no game running"); return; }
    FILE* file = nullptr;
    if (fopen_s(&file, path, "wb") || !file) { log("QUESTDUMP refused cannot write the file"); return; }
    std::fputs("name\tfolder\ttopic\ttype\tstatus\txp\tenclave_success\tdestination\tfinished_quests\tdelivery\r\n", file);
    auto const& quests = manager->GetQuests();
    int count = 0;
    for (GEInt qi = 0; qi < quests.GetCount(); ++qi) {
        gCQuest_PS const* quest = quests.GetAt(qi).operator->();
        if (!quest) continue;
        const bCUnicodeString topic = quest->GetLogTopic();
        std::fprintf(file, "%s\t%s\t%s\t%d\t%d\t%u\t%s\t%s\t%s\t%s\r\n", quest->GetName().GetText(), quest->GetFolder().GetText(),
                     Field(reinterpret_cast<wchar_t const*>(topic.GetText())).c_str(), static_cast<int>(quest->GetType().GetValue()),
                     static_cast<int>(quest->GetStatus().GetValue()), static_cast<unsigned>(quest->GetExperiencePoints()),
                     quest->GetEnclaveSuccess().GetText(), quest->GetDestinationEntity().GetText(), Join(quest->GetFinishedQuests()).c_str(),
                     Join(quest->GetDeliveryEntities()).c_str());
        ++count;
    }
    std::fclose(file);
    char line[120];
    sprintf_s(line, "QUESTDUMP quests=%d", count);
    log(line);
}

// The same against this machine's own hero (stage Ransack: the host's baseline of a robbery).
void AssessHero(char const* argument, LogFn log)
{
    char line[300], name[96] = {};
    char const* sep = std::strrchr(argument, '_');
    if (!sep || sep - argument >= static_cast<ptrdiff_t>(sizeof(name))) { log("REFUSED assesshero: Name_Reason"); return; }
    std::memcpy(name, argument, sep - argument);
    const int reason = std::atoi(sep + 1);
    Entity npc = Entity::GetEntity(bCString(name));
    eCEntity* hero = gCSession::GetInstance().GetHero();
    if (!npc.GetInstance() || !hero) { log("REFUSED assesshero: no such NPC or no hero"); return; }
    const GEInt result = GetScriptAdminExt().CallScript(bCString("AssessTarget"), npc.GetInstance(), hero, reason);
    sprintf_s(line, "ASSESSHERO name=%s reason=%d result=%d task=%s", name, reason, result, npc.Routine.GetCurrentTask().GetText());
    log(line);
}
}

namespace g3t::explore {
namespace {
std::string Join(bTObjArray<bCString>& list)
{
    std::string out;
    for (int i = 0; i < list.GetCount(); ++i) out += std::string(out.empty() ? "" : ",") + list.AccessAt(i).GetText();
    return out.empty() ? "-" : out;
}
}

// QA, stage E3: a dialog option's conditions and its script lines, to see why it cannot run.
// QA (stage Q2): dialog options whose script gives experience (GiveXP) and whose only conditions are simple (no quest, no
// knowledge, no items): name, owner, condition type, parent, told; the owner's distance from the hero in metres.
void XpInfos(LogFn log)
{
    char line[400];
    gCInfoManager_PS* manager = gCSession::GetInstance().IsGameRunning() ? gCInfoManager_PS::GetInstance() : nullptr;
    eCEntity* hero = gCSession::GetInstance().GetHero();
    if (!manager || !hero) { log("REFUSED xpinfos: no game"); return; }
    auto& all = manager->AccessInfos();
    unsigned found = 0;
    for (int i = 0; i < all.GetCount(); ++i) {
        gCInfo_PS* info = all.AccessAt(i);
        if (!info || info->AccessInfoGiven() || info->AccessQuest().GetLength() || info->AccessConditionPlayerKnows().GetCount() ||
            info->AccessCondItems().GetCount() || info->AccessCondSecondaryNPC().GetCount())
            continue;
        gCInfoScript_PS* script = info->AccessInfoScript().GetObject();
        int xp = 0;
        for (GEInt c = 0; script && c < script->GetCommandCount(); ++c)
            if (gCInfoScriptLine_PS* command = script->GetCommand(c))
                if (command->AccessCommand() == bCString("GiveXP")) xp = std::atoi(command->AccessID1().GetText());
        if (xp <= 0) continue;
        Entity owner = Entity::GetEntity(info->AccessOwner());
        float distance = -1.0f;
        if (owner.GetInstance()) {
            const bCVector& a = owner.GetInstance()->GetWorldPosition();
            const bCVector& b = hero->GetWorldPosition();
            distance = std::sqrt((a.GetX() - b.GetX()) * (a.GetX() - b.GetX()) + (a.GetY() - b.GetY()) * (a.GetY() - b.GetY()) +
                                 (a.GetZ() - b.GetZ()) * (a.GetZ() - b.GetZ())) / 100.0f;
        }
        sprintf_s(line, "XPINFO name=%s owner=%s xp=%d cond_type=%d parent=%s distance_m=%.0f", info->AccessName().GetText(),
                  info->AccessOwner().GetText(), xp, static_cast<int>(static_cast<gEInfoCondType const&>(info->AccessConditionType())),
                  info->AccessParent().GetLength() ? info->AccessParent().GetText() : "-", distance);
        log(line);
        ++found;
    }
    sprintf_s(line, "XPINFOS end found=%u", found);
    log(line);
}

void InfoDescribe(char const* name, LogFn log)
{
    char line[900];
    gCInfoManager_PS* manager = gCSession::GetInstance().IsGameRunning() ? gCInfoManager_PS::GetInstance() : nullptr;
    gCInfo_PS* info = manager ? manager->GetInfo(bCString(name)) : nullptr;
    if (!info) { sprintf_s(line, "INFODESC name=%s found=0", name); log(line); return; }
    sprintf_s(line, "INFODESC name=%s found=1 owner=%s parent=%s quest=%s given=%d permanent=%d cond_type=%d knows=%s knows_not=%s items=%s secondary=%s party=%s near=%s gold=%d",
              name, info->AccessOwner().GetText(), info->AccessParent().GetText(), info->AccessQuest().GetText(), info->AccessInfoGiven() ? 1 : 0,
              info->AccessPermanent() ? 1 : 0, static_cast<int>(static_cast<gEInfoCondType const&>(info->AccessConditionType())),
              Join(info->AccessConditionPlayerKnows()).c_str(), Join(info->AccessConditionPlayerKnowsNot()).c_str(), Join(info->AccessCondItems()).c_str(),
              Join(info->AccessCondSecondaryNPC()).c_str(), Join(info->AccessCondPlayerPartyMember()).c_str(), info->AccessConditionOwnerNearEntity().GetText(),
              static_cast<int>(info->AccessGoldCost()));
    log(line);
    if (gCInfoScript_PS* script = info->AccessInfoScript().GetObject()) {
        auto& lines = script->AccessInfoScriptLines();
        for (int k = 0; k < lines.GetCount(); ++k) {
            gCInfoScriptLine_PS* l = static_cast<gCInfoScriptLine_PS*>(lines.AccessAt(k));
            if (!l) continue;
            sprintf_s(line, "INFOLINE %d %s e1=%s e2=%s id1=%s id2=%s text=%s", k, l->AccessCommand().GetText(), l->AccessEntity1().GetText(),
                      l->AccessEntity2().GetText(), l->AccessID1().GetText(), l->AccessID2().GetText(), l->AccessText().GetID().GetText());
            log(line);
        }
    }
    log("INFODESC end");
}
}

namespace g3t::explore {
// QA, stage E4: "Attacker_Target": an NPC attacks another NPC (AssessTarget, reason Enemy). The target's name is the
// part after the last underscore; "hero": this machine's hero (upsetdiag 26.10., the single-player reference).
void NpcFight(char const* argument, LogFn log)
{
    char line[300];
    std::string arg = argument ? argument : "";
    const size_t cut = arg.rfind('_');
    if (cut == std::string::npos) { log("REFUSED npcfight: Attacker_Target"); return; }
    Entity attacker = Entity::GetEntity(bCString(arg.substr(0, cut).c_str()));
    Entity target = arg.substr(cut + 1) == "hero" ? Entity(gCSession::GetInstance().GetHero())
                                                  : Entity::GetEntity(bCString(arg.substr(cut + 1).c_str()));
    if (!target.GetInstance()) {  // the target by GUID (some names do not resolve, e.g. QP_Fisher in stage P3a)
        unsigned long d1 = 0;
        unsigned d2 = 0, d3 = 0, b[8] = {};
        if (sscanf_s(arg.substr(cut + 1).c_str(), "%8lx-%4x-%4x-%2x%2x-%2x%2x%2x%2x%2x%2x", &d1, &d2, &d3, &b[0], &b[1], &b[2], &b[3],
                     &b[4], &b[5], &b[6], &b[7]) == 11) {
            _GUID raw{d1, static_cast<unsigned short>(d2), static_cast<unsigned short>(d3), {}};
            for (int i = 0; i < 8; ++i) raw.Data4[i] = static_cast<unsigned char>(b[i]);
            target = Entity(bCPropertyID(bCGuid(raw)));
        }
    }
    if (!attacker.GetInstance() || !target.GetInstance()) { log("REFUSED npcfight: no such NPC"); return; }
    const GEInt result = GetScriptAdminExt().CallScript(bCString("AssessTarget"), attacker.GetInstance(), target.GetInstance(), gEAttackReason_Enemy);
    sprintf_s(line, "NPCFIGHT attacker=%s target=%s result=%d task=%s", arg.substr(0, cut).c_str(), arg.substr(cut + 1).c_str(), result,
              attacker.Routine.GetCurrentTask().GetText());
    log(line);
}

// The game's script for an NPC that gives up a fight (CancelAttack 0x1a4d0: LastFightAgainstPlayer Cancel and the time for
// its player, back to its routine), against its current target: where the chase would end it (upsetdiag 26.10.).
void CancelAttack(char const* name, LogFn log)
{
    char line[300];
    Entity npc = Entity::GetEntity(bCString(name));
    if (!npc.GetInstance() || !npc.GetInstance()->GetPropertySet(eEPropertySetType_NPC)) { log("REFUSED cancelattack: no such NPC"); return; }
    eCEntity* target = npc.NPC.GetCurrentTarget().GetInstance();
    const GEInt result = GetScriptAdminExt().CallScript(bCString("CancelAttack"), npc.GetInstance(), target, 0);
    sprintf_s(line, "CANCELATTACK name=%s target=%s result=%d task=%s last=%d", name, target ? Entity(target).GetName().GetText() : "-", result,
              npc.Routine.GetCurrentTask().GetText(), static_cast<int>(npc.NPC.LastFightAgainstPlayer));
    log(line);
}

// As a duel starts (attack reason Duel): the game ends it with a knock-out (Defeat), not a death (PLAN-2026-10-22 2.2).
void DuelHero(char const* name, LogFn log)
{
    char line[300];
    Entity attacker = Entity::GetEntity(bCString(name));
    eCEntity* hero = gCSession::GetInstance().GetHero();
    if (!attacker.GetInstance() || !hero) { log("REFUSED duelhero: no such NPC or no hero"); return; }
    if (npcs::IsMirrored(attacker.GetInstance())) {  // the host's NPC: its owner starts the duel, as for a dialog's duel
        net::WorldState s{};
        s.kind = 3;
        const eCEntityProxy proxy(attacker.GetInstance());
        std::memcpy(s.guid, &proxy.GetEntityID().GetCore(), 16);
        s.guard = net::LocalId();
        strncpy_s(s.routine, "Duel", _TRUNCATE);
        net::SendWorldState(s, 0);
        sprintf_s(line, "DUELHERO attacker=%s result=sent_to_owner task=%s", name, attacker.Routine.GetCurrentTask().GetText());
        log(line);
        return;
    }
    const GEInt result = GetScriptAdminExt().CallScript(bCString("AssessTarget"), attacker.GetInstance(), hero, gEAttackReason_Duel);
    sprintf_s(line, "DUELHERO attacker=%s result=%d task=%s", name, result, attacker.Routine.GetCurrentTask().GetText());
    log(line);
}
}

namespace g3t::explore {
// QA, stage Q: kill and defeat quests (or the one named), their status and, per target name, counter/amount.
void QuestCounters(char const* name, LogFn log)
{
    char line[600];
    gCQuestManager_PS* manager = gCSession::GetInstance().IsGameRunning() ? gCQuestManager_PS::GetInstance() : nullptr;
    if (!manager) { log("REFUSED questcounters: no game running"); return; }
    auto const& quests = manager->GetQuests();
    for (GEInt qi = 0; qi < quests.GetCount(); ++qi) {
        gCQuest_PS const* quest = quests.GetAt(qi).operator->();
        if (!quest) continue;
        const int type = static_cast<int>(quest->GetType().GetValue());
        if (name && *name) {
            if (_stricmp(quest->GetName().GetText(), name)) continue;
        } else if (type != gEQuestType_Kill && type != gEQuestType_Defeat) {
            continue;
        }
        auto const& targets = quest->GetDeliveryEntities();
        auto const& counter = quest->GetDeliveryCounter();
        auto const& amounts = quest->GetDeliveryAmounts();
        std::string list;
        for (GEInt i = 0; i < targets.GetCount(); ++i) {
            char item[160];
            sprintf_s(item, "%s%s:%d/%d", list.empty() ? "" : ",", targets.GetAt(i).GetText(),
                      i < counter.GetCount() ? static_cast<int>(counter.GetAt(i)) : -1,
                      i < amounts.GetCount() ? static_cast<int>(amounts.GetAt(i)) : -1);
            list += item;
        }
        sprintf_s(line, "QUESTCOUNTER name=%s type=%d status=%d targets=%s", quest->GetName().GetText(), type,
                  static_cast<int>(quest->GetStatus().GetValue()), list.empty() ? "-" : list.c_str());
        log(line);
    }
    log("QUESTCOUNTER end");
}
}

namespace g3t::explore {
// QA, first human test: every NPC within 40 m of the hero with its target and the motion it shows, to compare what
// the two machines show. NEARX lines, then "NEARX end".
void NearNpcs(LogFn log)
{
    char line[1000];
    eCEntity* hero = gCSession::GetInstance().GetHero();
    if (!hero) { log("NEARX end"); return; }
    float h[3];
    PositionOf(hero, h);
    const bTObjArray<Entity> all = Entity::GetNPCs();
    int puppetsListed = 0, puppetAlignment = -1, puppetSpecies = -1, puppetAIMode = -1;
    for (int i = 0; i < all.GetCount(); ++i) {
        Entity npc = all.GetAt(i);
        eCEntity* instance = npc.GetInstance();
        if (instance && remote::IsPuppet(instance)) {
            // The game's perception scanner only looks at Entity::GetNPCs(): a puppet missing here is invisible to NPCs.
            ++puppetsListed;
            if (instance->GetPropertySet(eEPropertySetType_NPC)) {
                puppetAlignment = static_cast<int>(static_cast<gEPoliticalAlignment>(npc.NPC.PoliticalAlignment));
                puppetSpecies = static_cast<int>(static_cast<gESpecies>(npc.NPC.Species));
            }
            puppetAIMode = body::AIModeOf(instance);
        }
        if (!instance || instance == hero || remote::IsPuppet(instance)) continue;
        float p[3];
        PositionOf(instance, p);
        if (body::Distance(p, h) > 4000.0f) continue;
        Entity target = instance->GetPropertySet(eEPropertySetType_NPC) ? npc.NPC.GetCurrentTarget() : Entity();
        char motion[128] = "-";
        body::CurrentMotion(instance, motion, sizeof(motion));
        const eCEntityProxy proxy(instance);
        bCPropertyID::bSCore core;
        std::memcpy(&core, &proxy.GetEntityID().GetCore(), 16);
        const Entity held = npc.Inventory.GetItemFromSlot(gESlot_RightHand);
        // Is slot 0 really playing? A body with nothing running there stands in its bind pose (T-pose).
        int running = -1, weight = -1;
        if (auto* animation = static_cast<eCVisualAnimation_PS*>(instance->GetPropertySet(eEPropertySetType_Animation)))
            if (eCWrapper_emfx2Actor* actor = animation->GetActor()) {
                const auto slot0 = static_cast<eCWrapper_emfx2Actor::eEMotionType>(0);
                running = actor->IsMotionRunning(slot0) ? 1 : 0;
                weight = static_cast<int>(actor->GetTargetWeight(slot0) * 100.0f);
            }
        // Facing: the body's forward vector as a compass angle, to compare both machines.
        const bCVector forward = instance->GetAtVector();
        const int yaw = static_cast<int>(std::atan2(forward.GetX(), forward.GetZ()) * 57.29578f);
        sprintf_s(line, "NEARX guid=%08X%04X name=%s dead=%d mirrored=%d owner=%u target=%s target_hero=%d target_puppet=%d task=%s ani=%d hand=%s motion=%s state=%s run0=%d weight0=%d yaw=%d",
                  core.Data1, core.Data2, npc.GetName().GetText(), npc.IsDead() ? 1 : 0, npcs::IsMirrored(instance) ? 1 : 0,
                  npcs::OwnerOf(instance), target.GetInstance() ? target.GetName().GetText() : "-",
                  target.GetInstance() && target.GetInstance() == hero ? 1 : 0, remote::PuppetOwner(target.GetInstance()),
                  npc.Routine.GetCurrentTask().GetText(), static_cast<int>(npc.Routine.AniState),
                  held.GetInstance() ? held.GetName().GetText() : "-", *motion ? motion : "-",
                  npc.Routine.GetCurrentState().GetText(), running, weight, yaw);
        log(line);
    }
    sprintf_s(line, "NEARX end actions_started=%u actions_shown=%u actions_played=%u holds=%u pacifies=%u owner_releases=%u combat_moves=%u combat_move_faults=%u motion_owner=%d "
                    "attitude_installed=%d attitude_native=%d attitude_asked=%u attitude_redirected=%u attitude_flee_kept=%u puppets_in_npcs=%d puppet_alignment=%d "
                    "puppet_species=%d puppet_aimode=%d replay_started=%u/%u/%u replay_played=%u/%u/%u replay_shown=%u/%u/%u script_anis=%u script_ani_faults=%u loop_restarts=%u",
              body::actionsStarted, body::actionsShown, body::actionsPlayed, npcs::HoldChanges(), body::pacifies, body::ownerReleases, body::combatMoves, body::combatMoveFaults, body::motionOwner,
              ai::AttitudeInstalled() ? 1 : 0, ai::AttitudeNative() ? 1 : 0, ai::AttitudeAsked(), ai::AttitudeRedirected(), ai::AttitudeFleeKept(), puppetsListed, puppetAlignment,
              puppetSpecies, puppetAIMode, body::kindStarted[0], body::kindStarted[1], body::kindStarted[2], body::kindPlayed[0],
              body::kindPlayed[1], body::kindPlayed[2], body::kindShown[0], body::kindShown[1], body::kindShown[2],
              body::scriptAnis, body::scriptAniFaults, body::loopRestarts);
    log(line);
}

void ScriptWatch(char const* name, LogFn log)
{
    char line[300];
    if (!name || !*name) {
        for (int i = 0; i < watchedCount; ++i) {
            sprintf_s(line, "SCRIPTWATCH name=%s calls=%u self=%s other=%s", watched[i].name.c_str(), watched[i].calls,
                      watched[i].self.c_str(), watched[i].other.c_str());
            log(line);
        }
        log("SCRIPTWATCH end");
        return;
    }
    for (int i = 0; i < watchedCount; ++i)
        if (watched[i].name == name) { log("SCRIPTWATCH refused already watched"); return; }
    gSScript* script = nullptr;
    if (watchedCount >= WatchMax || !GetScriptAdminExt().GetScripts().GetAt(bCString(name), script) || !script || !script->m_funcScript) {
        sprintf_s(line, "SCRIPTWATCH refused name=%s (no such script or %d watched)", name, watchedCount);
        log(line);
        return;
    }
    Watched& w = watched[watchedCount];
    w.name = name;
    w.original = script->m_funcScript;
    script->m_funcScript = watchWrappers[watchedCount++];
    sprintf_s(line, "SCRIPTWATCH name=%s watched=1", name);
    log(line);
}

// QA (P7, GetZoneAttitudeToPlayer): an NPC's zone attitude and attitude to the player (the scripts read GetPlayer's zone,
// security level and fame) for the hero, and for puppet 2 asked plainly and with the puppet as the game's player.
void ZoneAttitude(char const* name, LogFn log)
{
    char line[500];
    eCEntity* hero = gCSession::GetInstance().GetHero();
    Entity npc = Entity::GetEntity(bCString(name));
    if (!hero || !npc.GetInstance()) { log("REFUSED zoneatt: no hero or no such NPC"); return; }
    auto& admin = GetScriptAdminExt();
    eCEntity* puppet = remote::PuppetOf(2);
    auto zoneOf = [](eCEntity* e, char* out, size_t size) {
        Entity zone = e ? Entity(e).GetZone() : Entity();
        sprintf_s(out, size, "%s/%d", zone.GetInstance() ? zone.GetName().GetText() : "-",
                  zone.GetInstance() ? static_cast<int>(zone.GetAIZoneSecurityLevel()) : -1);
    };
    char heroZone[96], puppetZone[96];
    zoneOf(hero, heroZone, sizeof(heroZone));
    zoneOf(puppet, puppetZone, sizeof(puppetZone));
    const GEInt heroZoneAtt = admin.CallScript(bCString("GetZoneAttitudeToPlayer"), npc.GetInstance(), hero, 0);
    const GEInt heroAtt = admin.CallScript(bCString("GetAttitudeToPlayer"), npc.GetInstance(), hero, 0);
    GEInt plainZoneAtt = -9, actingZoneAtt = -9, actingAtt = -9, puppetAtt = -9;
    if (puppet) {
        plainZoneAtt = admin.CallScript(bCString("GetZoneAttitudeToPlayer"), npc.GetInstance(), puppet, 0);
        puppetAtt = admin.CallScript(bCString("GetAttitude"), npc.GetInstance(), puppet, 0);
        guards::Acting acting(puppet);
        actingZoneAtt = admin.CallScript(bCString("GetZoneAttitudeToPlayer"), npc.GetInstance(), puppet, 0);
        actingAtt = admin.CallScript(bCString("GetAttitudeToPlayer"), npc.GetInstance(), puppet, 0);
    }
    sprintf_s(line, "ZONEATT name=%s hero_zone=%s hero_zoneatt=%d hero_att=%d puppet_zone=%s puppet_zoneatt=%d puppet_zoneatt_acting=%d"
                    " puppet_att_acting=%d puppet_attitude=%d", name, heroZone, heroZoneAtt, heroAtt, puppetZone, plainZoneAtt, actingZoneAtt,
              actingAtt, puppetAtt);
    log(line);
}

}
