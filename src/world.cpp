// SPDX-License-Identifier: GPL-3.0-or-later
#include "world.h"
#include "log.h"

#include "body.h"
#include "ai.h"
#include "config.h"
#include "net.h"
#include "remote.h"

#include <Engine.h>
#include <Game.h>
#include <Script.h>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <Windows.h>

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

// Host: its processing range as host_roi says (owner 27.10., "80 m"). The engine keeps the radius in the entity admin
// (EntityAdmin+0x10, SetROISphere; the centre follows the camera every frame) and read it at start from Entity.ROI in the
// engine setup, a float there although the SDK declares an int (re/findings/range-guid-clock.md). Both are set, then a full
// rebuild of the processing list; checked every 2 s, since a load may put the setup's value back.
uint32_t lastRoiCheck = 0;
float roiLogged = 0.0f;

// Stufe 2, host_sim = elastic: the host's processing sphere takes in every player (re/findings/stufe2-elastic-roi-c.md).
// eCEntityAdmin::Process (Engine.dll 0x230a70) takes the current camera's position (camera +0x8c) as the sphere's centre in
// every frame and builds its lists anew once that moved 2.5 m; SetROISphere's centre does not last. Process is wrapped: for
// its run the camera stands in the middle of the players and the radius (+0x10) reaches all of them plus a margin, then the
// camera goes back before anything renders. Only what the prefetcher loaded around the camera (its view distance, camera
// +0xbc) can be processed: a player farther away keeps the machines' own split (the client simulates its surroundings).
constexpr float ElasticMargin = 4000.0f;   // cm beyond the farthest player
constexpr float ElasticMax = 15000.0f;     // cm, largest radius
using ProcessFn = void(__thiscall*)(eCEntityAdmin*);
ProcessFn originalProcess = nullptr;
bool elasticTried = false, elasticActive = false;
unsigned elasticFrames = 0, elasticTooFar = 0;
float elasticRadius = 0.0f, elasticCentre[3] = {};
char const* elasticWhy = "not yet";
uint32_t elasticLogged = 0;

bool ElasticSphere(eCEntity* camera, float centre[3], float& radius)
{
    eCEntity* hero = gCSession::GetInstance().GetHero();
    if (!hero || !camera) { elasticWhy = "no hero"; return false; }
    float points[net::MaxPlayers][3];
    int n = 0;
    const bCVector& h = remote::ControlledBody() ? remote::ControlledBody()->GetWorldPosition() : hero->GetWorldPosition();
    points[n][0] = h.GetX(); points[n][1] = h.GetY(); points[n][2] = h.GetZ(); ++n;
    net::Remote const* remotes = net::Remotes();
    for (int id = 2; id < net::MaxPlayers; ++id) {
        net::Remote const& r = remotes[id];
        if (!r.connected || !(r.latest.flags & 1) || r.latest.world != remote::CurrentWorld()) continue;
        std::memcpy(points[n++], r.latest.position, sizeof(float) * 3);
    }
    if (n < 2) { elasticWhy = "alone"; return false; }
    float lo[3], hi[3];
    for (int k = 0; k < 3; ++k) lo[k] = hi[k] = points[0][k];
    for (int i = 1; i < n; ++i)
        for (int k = 0; k < 3; ++k) lo[k] = std::fmin(lo[k], points[i][k]), hi[k] = std::fmax(hi[k], points[i][k]);
    for (int k = 0; k < 3; ++k) centre[k] = (lo[k] + hi[k]) * 0.5f;
    float farthest = 0.0f;
    for (int i = 0; i < n; ++i) farthest = std::fmax(farthest, body::Distance(centre, points[i]));
    radius = std::fmax(GetConfig().hostRoi, farthest + ElasticMargin);
    // The prefetcher's sphere around the real camera: a centre that leaves a player outside it would process nothing there.
    const float loaded = *reinterpret_cast<float const*>(reinterpret_cast<char const*>(camera) + 0xbc);
    float fromCamera = 0.0f;
    const float* at = reinterpret_cast<float const*>(reinterpret_cast<char const*>(camera) + 0x8c);
    for (int i = 0; i < n; ++i) fromCamera = std::fmax(fromCamera, body::Distance(at, points[i]));
    if (radius > ElasticMax || (loaded > 0.0f && fromCamera > loaded - 1000.0f)) { ++elasticTooFar; elasticWhy = "too far apart"; return false; }
    elasticWhy = "on";
    return true;
}

// Stufe 2 in full, host_sim = spheres (re/findings/stufe2-spheres-plan.md): one sphere around each other player besides the
// camera's own. The engine asks only three functions whether a box lies in its processing range: CullProcessingRangeRegion
// (Engine.dll 0x2317d0: the box against the range's box at EntityAdmin+0x64..+0x78, then its sphere at +0x20: 0 outside, 1
// inside, 2 partly), CullOldProcessingRangeRegion (0x231840, the same against the range before the last rebuild, +0x7c..+0x90
// and +0x30) and IntersectsProcessingRangeRegion (0x2318c0, the box only). Their first instructions are longer than JumpTo
// takes (7 and 10 bytes), so they are rebuilt here whole and the spheres added: in one of them counts as in range. The
// spheres change only when the engine rebuilds its lists (the camera moved 2.5 m, +0x40) or a player moved that far (then
// +0xfd asks for the rebuild); the spheres before are the old range then, for whatever left it. Only what the prefetcher loaded
// around the camera can be processed: a player farther away keeps the machines' own split, as with elastic.
struct Sphere
{
    float at[3];
    float radius;
};
Sphere spheres[net::MaxPlayers], oldSpheres[net::MaxPlayers];
int sphereCount = 0, oldSphereCount = 0;
bool spheresHooked = false, spheresActive = false;
unsigned sphereRebuilds = 0, sphereTooFar = 0;
eCEntityAdmin* lastAdmin = nullptr;

float const* Field(eCEntityAdmin const* admin, int offset) { return reinterpret_cast<float const*>(reinterpret_cast<char const*>(admin) + offset); }

// As the engine compares: the box (min x y z, max x y z) meets the range's box lo..hi.
bool BoxMeets(float const* box, float const* lo, float const* hi)
{
    return box[0] <= hi[0] && lo[0] <= box[3] && box[1] <= hi[1] && lo[1] <= box[4] && box[2] <= hi[2] && lo[2] <= box[5];
}

int CullSpheres(bCBox const& box, Sphere const* list, int count, int result)
{
    float const* b = reinterpret_cast<float const*>(&box);
    for (int i = 0; i < count && result != 1; ++i) {
        Sphere const& s = list[i];
        const float lo[3] = {s.at[0] - s.radius, s.at[1] - s.radius, s.at[2] - s.radius};
        const float hi[3] = {s.at[0] + s.radius, s.at[1] + s.radius, s.at[2] + s.radius};
        if (!BoxMeets(b, lo, hi)) continue;
        result = bCSphere(bCVector(s.at[0], s.at[1], s.at[2]), s.radius).Contains(box) ? 1 : 2;
    }
    return result;
}

int Cull(eCEntityAdmin const* admin, bCBox const& box, int range, int sphere)
{
    if (!BoxMeets(reinterpret_cast<float const*>(&box), Field(admin, range), Field(admin, range + 0xc))) return 0;
    return reinterpret_cast<bCSphere const*>(Field(admin, sphere))->Contains(box) ? 1 : 2;
}

int __fastcall SpheresCull(eCEntityAdmin* admin, void*, bCBox const& box)
{
    return CullSpheres(box, spheres, sphereCount, Cull(admin, box, 0x64, 0x20));
}

int __fastcall SpheresCullOld(eCEntityAdmin* admin, void*, bCBox const& box)
{
    return CullSpheres(box, oldSpheres, oldSphereCount, Cull(admin, box, 0x7c, 0x30));
}

bool __fastcall SpheresIntersect(eCEntityAdmin* admin, void*, bCBox const& box)
{
    float const* b = reinterpret_cast<float const*>(&box);
    if (BoxMeets(b, Field(admin, 0x64), Field(admin, 0x70))) return true;
    for (int i = 0; i < sphereCount; ++i) {
        Sphere const& s = spheres[i];
        const float lo[3] = {s.at[0] - s.radius, s.at[1] - s.radius, s.at[2] - s.radius};
        const float hi[3] = {s.at[0] + s.radius, s.at[1] + s.radius, s.at[2] + s.radius};
        if (BoxMeets(b, lo, hi)) return true;
    }
    return false;
}

void HookSpheres()
{
    HMODULE engine = GetModuleHandleA("Engine.dll");
    struct Hook
    {
        char const* name;
        unsigned char start[10];
        size_t length;
        void* detour;
    } const hooks[] = {
        {"?CullProcessingRangeRegion@eCEntityAdmin@@QAE?AW4eEProcessingRangeCull@1@ABVbCBox@@@Z", {0x8b, 0x54, 0x24, 0x04, 0xd9, 0x41, 0x70}, 7,
         reinterpret_cast<void*>(&SpheresCull)},
        {"?CullOldProcessingRangeRegion@eCEntityAdmin@@QAE?AW4eEProcessingRangeCull@1@ABVbCBox@@@Z",
         {0x8b, 0x54, 0x24, 0x04, 0xd9, 0x81, 0x88, 0x00, 0x00, 0x00}, 10, reinterpret_cast<void*>(&SpheresCullOld)},
        {"?IntersectsProcessingRangeRegion@eCEntityAdmin@@QAE_NABVbCBox@@@Z", {0x8b, 0x54, 0x24, 0x04, 0xd9, 0x41, 0x70}, 7,
         reinterpret_cast<void*>(&SpheresIntersect)},
    };
    unsigned char* code[3] = {};
    for (int i = 0; i < 3; ++i) {
        code[i] = engine ? reinterpret_cast<unsigned char*>(GetProcAddress(engine, hooks[i].name)) : nullptr;
        if (!code[i] || std::memcmp(code[i], hooks[i].start, hooks[i].length)) {
            Logf("world: %s %s; host_sim spheres stays off", hooks[i].name, code[i] ? "has other code" : "not found");
            return;
        }
    }
    for (int i = 0; i < 3; ++i)
        if (!ai::JumpTo(code[i], hooks[i].detour)) {  // no way back needed: the detours are the whole function
            Logf("world: %s could not be redirected; host_sim spheres stays off", hooks[i].name);
            return;  // the ones before answer as the engine would, without spheres
        }
    spheresHooked = true;
    Logf("world: processing range functions redirected (host_sim spheres)");
}

// Before Process: the spheres for this frame (only at a rebuild, see above).
void PlaceSpheres(eCEntityAdmin* admin, eCEntity* camera)
{
    lastAdmin = admin;
    Sphere wanted[net::MaxPlayers];
    int n = 0;
    float const* at = camera ? reinterpret_cast<float const*>(reinterpret_cast<char const*>(camera) + 0x8c) : nullptr;
    const float step = *Field(admin, 0x40);
    if (spheresHooked && at && net::IsHost()) {
        const float loaded = *reinterpret_cast<float const*>(reinterpret_cast<char const*>(camera) + 0xbc);
        net::Remote const* remotes = net::Remotes();
        for (int id = 2; id < net::MaxPlayers; ++id) {
            net::Remote const& r = remotes[id];
            if (!r.connected || !(r.latest.flags & 1) || r.latest.world != remote::CurrentWorld()) continue;
            if (loaded > 0.0f && body::Distance(at, r.latest.position) > loaded - 1000.0f) { ++sphereTooFar; continue; }
            std::memcpy(wanted[n].at, r.latest.position, sizeof(wanted[n].at));
            wanted[n++].radius = GetConfig().hostRoi + step;
        }
    }
    bool changed = n != sphereCount;
    for (int i = 0; i < n && !changed; ++i)
        changed = body::Distance(wanted[i].at, spheres[i].at) > step || wanted[i].radius != spheres[i].radius;
    auto* bytes = reinterpret_cast<unsigned char*>(admin);
    const bool rebuild = bytes[0xfd] == 1 || (at && body::Distance(Field(admin, 0x24), at) > step);
    if (changed || rebuild) {
        std::memcpy(oldSpheres, spheres, sizeof(spheres));
        oldSphereCount = sphereCount;
        std::memcpy(spheres, wanted, sizeof(wanted));
        sphereCount = n;
        if (changed) {
            bytes[0xfd] = 1;
            ++sphereRebuilds;
        }
    }
    spheresActive = sphereCount > 0;
}

void __fastcall ElasticProcess(eCEntityAdmin* admin, void*)
{
    eCEntity* camera = nullptr;
    camera = reinterpret_cast<eCEntity*>(&eCApplication::GetInstance().GetCurrentCamera());  // Process reads the same one
    if (GetConfig().hostSimSpheres) {
        PlaceSpheres(admin, camera);
        originalProcess(admin);
        return;
    }
    float centre[3], radius = 0.0f, saved[3];
    float* at = camera ? reinterpret_cast<float*>(reinterpret_cast<char*>(camera) + 0x8c) : nullptr;
    elasticActive = GetConfig().hostSimElastic && net::IsHost() && at && ElasticSphere(camera, centre, radius);
    if (elasticActive) {
        std::memcpy(saved, at, sizeof(saved));
        std::memcpy(at, centre, sizeof(saved));
        *reinterpret_cast<float*>(reinterpret_cast<char*>(admin) + 0x10) = radius;
        std::memcpy(elasticCentre, centre, sizeof(centre));
        elasticRadius = radius;
        ++elasticFrames;
    }
    originalProcess(admin);
    if (elasticActive) std::memcpy(at, saved, sizeof(saved));
}

void HookElastic()
{
    if (elasticTried || !(GetConfig().hostSimElastic || GetConfig().hostSimSpheres)) return;
    elasticTried = true;
    if (GetConfig().hostSimSpheres) HookSpheres();
    auto* code = reinterpret_cast<unsigned char*>(GetProcAddress(GetModuleHandleA("Engine.dll"), "?Process@eCEntityAdmin@@QAEXXZ"));
    static const unsigned char prologue[] = {0x55, 0x8b, 0xec, 0x83, 0xe4, 0xf8};
    if (!code || std::memcmp(code, prologue, sizeof(prologue))) {
        Logf("world: eCEntityAdmin::Process %s; host_sim stays off", code ? "has another prologue" : "not found");
        return;
    }
    originalProcess = reinterpret_cast<ProcessFn>(ai::JumpTo(code, reinterpret_cast<void*>(&ElasticProcess)));
    Logf("world: eCEntityAdmin::Process %s", originalProcess ? "wrapped (host_sim)" : "could not be wrapped");
}

bool Extended() { return elasticActive || spheresActive; }

bool HostProcesses(float const at[3], float margin)
{
    if (elasticActive) return body::Distance(at, elasticCentre) <= elasticRadius - margin;
    if (!spheresActive || !lastAdmin) return false;
    if (body::Distance(at, Field(lastAdmin, 0x24)) <= *Field(lastAdmin, 0x10) - margin) return true;  // the camera's own
    const float step = *Field(lastAdmin, 0x40);
    for (int i = 0; i < sphereCount; ++i)
        if (body::Distance(at, spheres[i].at) <= spheres[i].radius - step - margin) return true;
    return false;
}

void Tick()
{
    if (!net::IsHost()) return;
    HookElastic();
    if (GetConfig().hostSimSpheres && net::Now() - elasticLogged >= 5000) {
        elasticLogged = net::Now();
        if (spheresActive)
            Logf("world: spheres %d around players (first %.0f,%.0f,%.0f radius %.0f cm), %u rebuilds, %u times too far", sphereCount,
                 spheres[0].at[0], spheres[0].at[1], spheres[0].at[2], spheres[0].radius, sphereRebuilds, sphereTooFar);
        else if (spheresHooked)
            Logf("world: spheres none now (%u times too far)", sphereTooFar);
    }
    if (GetConfig().hostSimElastic && net::Now() - elasticLogged >= 5000) {
        elasticLogged = net::Now();
        if (elasticActive)
            Logf("world: elastic range centre %.0f,%.0f,%.0f radius %.0f cm (%u frames)", elasticCentre[0], elasticCentre[1], elasticCentre[2],
                 elasticRadius, elasticFrames);
        else if (originalProcess)
            Logf("world: elastic range off now (%s; %u times too far apart)", elasticWhy, elasticTooFar);
    }
    if (elasticActive) return;  // the radius is set every frame then; host_roi below only when it is not
    const uint32_t now = net::Now();
    if (now - lastRoiCheck < 2000) return;
    lastRoiCheck = now;
    if (!gCSession::GetInstance().IsGameRunning()) return;
    auto* scene = static_cast<eCSceneAdmin*>(eCModuleAdmin::GetInstance().FindModule(bCString("eCSceneAdmin")));
    if (!scene) return;
    eCEntityAdmin& admin = scene->GetAccessToEntityAdmin();
    const float wanted = GetConfig().hostRoi;
    bCSphere sphere = admin.GetROISphere();
    const float before = sphere.GetRadius();
    if (std::fabs(before - wanted) < 1.0f) return;
    auto& setup = const_cast<eSSetupEngine&>(eCApplication::GetInstance().GetEngineSetup());
    std::memcpy(&setup.Entity_ROI, &wanted, sizeof(float));
    sphere.SetRadius(wanted);
    admin.SetROISphere(sphere);
    admin.FourceFullROIUpdate();
    const float after = admin.GetROISphere().GetRadius();
    if (after != roiLogged) {
        roiLogged = after;
        Logf("world: processing range %.0f -> %.0f cm (host_roi %.0f)", before, after, wanted);
    }
}

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
