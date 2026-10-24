// SPDX-License-Identifier: GPL-3.0-or-later
#include "mapmarks.h"

#include "log.h"
#include "net.h"
#include "remote.h"

#include <GFC.h>
#include <Game.h>
#include <Script.h>
#include <Windows.h>
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>

namespace g3t::mapmarks {
namespace {
// Game.dll as C examined it (SHA-256 1b87da18...): the map control gCHUDStaticMap and the compass gCHUDStaticCompass, their
// vtables and the paint (CFFGFCWnd::OnPaint) each holds in the slot at byte 0xb0. Hooked only if the slots hold exactly these.
constexpr uintptr_t MapVtable = 0x2d8d94, MapPaint = 0xa87d0;
constexpr uintptr_t CompassVtable = 0x2d825c, CompassPaint = 0xa6940;
constexpr int PaintSlot = 0xb0 / 4;
// Fields of the map control, as its own paint uses them (C): the shown map's property set, the symbol size, the player's arrow
// bitmap and the two colors of its markers' text (the second one by pointer). Its marker bitmap (+0x30, G3_HUD_Marker.tga)
// drew nothing on the world map (24.10.: that image is in none of the GUI's atlases), so the other players are arrows too.
constexpr size_t ShownMap = 0x10, SymbolSize = 0x18, ArrowBitmap = 0x28, TextColor = 0x38, TextColor2 = 0x4c;
constexpr unsigned TextFlags = 0x25;  // centered, vertically centered, one line (as the game's markers)
constexpr size_t CompassNeedle = 0x10;  // the compass control's needle bitmap (C)

struct Mark {
    bool shown = false;
    float position[3] = {};
    float heading = 0;  // where the player looks: radians clockwise from north (+z), as the game turns its arrow
    wchar_t name[32] = {};
};
Mark marks[net::MaxPlayers];

using PaintFn = void(__thiscall*)(CFFGFCWnd*);
PaintFn originalMap = nullptr, originalCompass = nullptr;
bool enabled = true;  // QA negative control: mapmarksoff
bool started = false;
CFFGFCBitmap* compassMark = nullptr;  // the arrow, made at the first compass paint and kept (never destroyed: nothing runs at exit)
bool compassMarkFailed = false;
unsigned mapPaints = 0, compassPaints = 0, mapDrawn = 0, compassDrawn = 0, offMap = 0;
// The last drawing of player i, for QA: on the map (u, v, pixels) and the compass (angle from the view, pixels).
struct Drawn {
    bool onMap = false, onCompass = false;
    float u = 0, v = 0, degrees = 0;
    int mapX = 0, mapY = 0, compassX = 0, compassY = 0;
};
Drawn drawn[net::MaxPlayers];
float viewX = 0, viewZ = 0;  // the camera's horizontal direction at the last compass paint

void DrawOnMap(CFFGFCWnd* control)
{
    auto* bytes = reinterpret_cast<unsigned char*>(control);
    gCMap_PS* map = *reinterpret_cast<gCMap_PS**>(bytes + ShownMap);
    if (!map) return;
    bCRect client;
    control->GetClientRect(client);
    const int width = client.GetWidth(), height = client.GetHeight();
    if (width <= 0 || height <= 0) return;
    const int half = std::clamp(*reinterpret_cast<int*>(bytes + SymbolSize) * 2 / 5, 6, 32);  // a little smaller than one's own arrow
    CFFGFCDeviceContext dc(control);
    for (int i = 1; i < net::MaxPlayers; ++i) {
        Mark const& mark = marks[i];
        drawn[i].onMap = false;
        if (!mark.shown) continue;
        float u = 0, v = 0;
        // CalcUV maps any place, one off this map too (C): only places on it are drawn.
        if (!map->CalcUV(bCVector(mark.position[0], mark.position[1], mark.position[2]), u, v) || !(u >= 0 && u <= 1 && v >= 0 && v <= 1)) {
            ++offMap;
            continue;
        }
        const int x = client.GetLeft() + static_cast<int>(width * u), y = client.GetTop() + static_cast<int>(height * v);
        bCRect box(x - half, y - half, x + half, y + half);
        dc.DrawBitmap(reinterpret_cast<CFFGFCBitmap*>(bytes + ArrowBitmap), &box, 0, mark.heading);
        bCRect label(x - 150, y + half, x + 150, y + half + 24);
        dc.DrawTextA(0, mark.name, -1, &label, TextFlags, *reinterpret_cast<bCFloatAlphaColor*>(bytes + TextColor),
                     reinterpret_cast<bCFloatAlphaColor*>(bytes + TextColor2));
        drawn[i] = Drawn{true, drawn[i].onCompass, u, v, drawn[i].degrees, x, y, drawn[i].compassX, drawn[i].compassY};
        ++mapDrawn;
    }
}

// The compass turns with the camera (its needle points north as seen from it, C): a player ahead of the camera sits at the
// top of the ring, one to its right at the right, one behind it at the bottom.
void DrawOnCompass(CFFGFCWnd* control)
{
    gCSession& session = gCSession::GetInstance();
    gCCameraAI_PS* camera = session.GetCamera_PS();
    eCEntity* eye = camera ? camera->GetEntity() : nullptr;
    eCDynamicEntity* hero = session.GetHero();
    if (!eye || !hero) return;
    float const* matrix = reinterpret_cast<float const*>(&eye->GetWorldMatrix());
    float fx = matrix[8], fz = matrix[10];  // its z axis, as the needle reads it (+0x20, +0x28)
    const float length = std::sqrt(fx * fx + fz * fz);
    if (!(length > 1e-4f)) return;
    fx /= length;
    fz /= length;
    viewX = fx;
    viewZ = fz;
    bCRect client;
    control->GetClientRect(client);
    const int width = client.GetWidth(), height = client.GetHeight();
    if (width <= 0 || height <= 0) return;
    if (!compassMark && !compassMarkFailed) {
        // As the map control makes its arrow (C, OnInitDialog). G3_HUD_Marker.tga gave create=0 attached=1 and drew nothing
        // (24.10.), so Create's answer counts: without it the needle copy below stands in.
        compassMark = new CFFGFCBitmap();
        const GEBool created = compassMark->Create(bCString("G3_HUD_Arrow.tga"));
        const bool attached = created && compassMark->IsAttached() && compassMark->GetHandle();
        Logf("mapmarks: compass arrow bitmap G3_HUD_Arrow.tga: create=%d attached=%d", created ? 1 : 0, attached ? 1 : 0);
        if (!attached) {
            compassMarkFailed = true;
            Log("mapmarks: no compass mark bitmap; a small copy of the needle points to each player instead");
        }
    }
    // An arrow at the ring pointing out towards the player; without one, a small copy of the needle (the game turns clockwise).
    CFFGFCBitmap* symbol = compassMarkFailed ? reinterpret_cast<CFFGFCBitmap*>(reinterpret_cast<unsigned char*>(control) + CompassNeedle) : compassMark;
    const int side = std::min(width, height);
    const int half = std::max(5, side / 8);
    const float radius = side * 0.5f - half;
    const float cx = client.GetLeft() + width * 0.5f, cy = client.GetTop() + height * 0.5f;
    const bCVector& at = hero->GetWorldPosition();
    CFFGFCDeviceContext dc(control);
    for (int i = 1; i < net::MaxPlayers; ++i) {
        Mark const& mark = marks[i];
        drawn[i].onCompass = false;
        if (!mark.shown) continue;
        const float dx = mark.position[0] - at.GetX(), dz = mark.position[2] - at.GetZ();
        if (dx * dx + dz * dz < 100.0f * 100.0f) continue;  // within a metre: no direction worth showing
        const float right = fz * dx - fx * dz, ahead = fx * dx + fz * dz;
        const float theta = std::atan2(right, ahead);
        const int x = static_cast<int>(cx + radius * std::sin(theta)), y = static_cast<int>(cy - radius * std::cos(theta));
        bCRect box(x - half, y - half, x + half, y + half);
        dc.DrawBitmap(symbol, &box, 0, theta);
        drawn[i].onCompass = true;
        drawn[i].degrees = theta * 57.29578f;
        drawn[i].compassX = x;
        drawn[i].compassY = y;
        ++compassDrawn;
    }
}

void __fastcall MapPaintDetour(CFFGFCWnd* self, void*)
{
    originalMap(self);
    ++mapPaints;
    if (enabled) DrawOnMap(self);
}

void __fastcall CompassPaintDetour(CFFGFCWnd* self, void*)
{
    originalCompass(self);
    ++compassPaints;
    if (enabled) DrawOnCompass(self);
}

// Puts the detour into a paint slot that holds the expected function; returns what was there, nullptr if not hooked.
void* Patch(unsigned char* game, uintptr_t vtable, uintptr_t paint, void* detour)
{
    void** slot = reinterpret_cast<void**>(game + vtable) + PaintSlot;
    if (*slot != game + paint) return nullptr;
    DWORD old = 0;
    if (!VirtualProtect(slot, sizeof(void*), PAGE_READWRITE, &old)) return nullptr;
    void* original = *slot;
    *slot = detour;
    VirtualProtect(slot, sizeof(void*), old, &old);
    return original;
}
}

void Start()
{
    if (started) return;
    started = true;
    auto* game = reinterpret_cast<unsigned char*>(GetModuleHandleA("Game.dll"));
    if (!game) return;
    originalMap = reinterpret_cast<PaintFn>(Patch(game, MapVtable, MapPaint, reinterpret_cast<void*>(&MapPaintDetour)));
    originalCompass = reinterpret_cast<PaintFn>(Patch(game, CompassVtable, CompassPaint, reinterpret_cast<void*>(&CompassPaintDetour)));
    Logf("mapmarks: map paint %s, compass paint %s", originalMap ? "hooked" : "NOT hooked (other Game.dll)",
         originalCompass ? "hooked" : "NOT hooked (other Game.dll)");
}

void Tick()
{
    net::Remote const* remotes = net::Remotes();
    for (int i = 1; i < net::MaxPlayers; ++i) {
        Mark& mark = marks[i];
        // Shown while its puppet is here (fresh state, same world): the puppet's place, smoothed as everyone sees it.
        eCEntity* puppet = i != net::LocalId() ? remote::PuppetOf(i) : nullptr;
        mark.shown = puppet != nullptr;
        if (!puppet) continue;
        const bCVector& p = puppet->GetWorldPosition();
        mark.position[0] = p.GetX();
        mark.position[1] = p.GetY();
        mark.position[2] = p.GetZ();
        float const* matrix = reinterpret_cast<float const*>(&puppet->GetWorldMatrix());  // its z axis, as the game's arrow reads it
        if (matrix[8] != 0.0f || matrix[10] != 0.0f) mark.heading = std::atan2(matrix[8], matrix[10]);
        MultiByteToWideChar(CP_UTF8, 0, remotes[i].latest.name, -1, mark.name, 31);
        mark.name[31] = 0;
    }
}

void Report(LogFn log)
{
    char line[700];
    int at = sprintf_s(line, "MAPMARKS map_hooked=%d compass_hooked=%d enabled=%d map_paints=%u compass_paints=%u map_drawn=%u compass_drawn=%u"
                             " off_map=%u compass_mark=%d view=%.3f,%.3f",
                       originalMap ? 1 : 0, originalCompass ? 1 : 0, enabled ? 1 : 0, mapPaints, compassPaints, mapDrawn, compassDrawn, offMap,
                       compassMark && !compassMarkFailed ? 1 : compassMarkFailed ? -1 : 0, viewX, viewZ);
    for (int i = 1; i < net::MaxPlayers && at > 0; ++i) {
        if (!marks[i].shown) continue;
        Drawn const& d = drawn[i];
        at += sprintf_s(line + at, sizeof(line) - at, " p%d=%.0f,%.0f map=%d:%.3f,%.3f@%d,%d compass=%d:%.0f@%d,%d", i, marks[i].position[0],
                        marks[i].position[2], d.onMap ? 1 : 0, d.u, d.v, d.mapX, d.mapY, d.onCompass ? 1 : 0, d.degrees, d.compassX, d.compassY);
    }
    log(line);
}

void Off(LogFn log)
{
    enabled = false;
    log("MAPMARKSOFF ok");
}

void OpenMap(LogFn log)
{
    char line[200];
    eCDynamicEntity* self = gCSession::GetInstance().GetHero();
    if (!self) { log("REFUSED mapopen: no hero"); return; }
    Entity hero(self);
    int stack = -1;
    for (int i = 0; i < hero.Inventory.GetStackCount() && stack < 0; ++i)
        if (Entity item = hero.Inventory.GetTemplateItem(i); item.GetInstance() && !std::strcmp(item.GetName().GetText(), "It_Map_World")) stack = i;
    const bool given = stack < 0;
    if (given) stack = hero.Inventory.CreateItems(Template("It_Map_World"), 0, 1);
    if (stack < 0) { log("REFUSED mapopen: no world map (It_Map_World)"); return; }
    Entity map = hero.Inventory.GetTemplateItem(stack);
    const GEBool opened = gui2.OpenMap(map);
    sprintf_s(line, "MAPOPEN stack=%d given=%d opened=%d page=%d", stack, given ? 1 : 0, opened ? 1 : 0, static_cast<int>(gui2.GetActivePage()));
    log(line);
}

void CloseMap(LogFn log)
{
    char line[100];
    const GEBool closed = gui2.ClosePage();
    sprintf_s(line, "MAPCLOSE closed=%d page=%d", closed ? 1 : 0, static_cast<int>(gui2.GetActivePage()));
    log(line);
}
}
