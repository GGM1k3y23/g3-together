// SPDX-License-Identifier: GPL-3.0-or-later
#include "items.h"

#include "body.h"
#include "log.h"
#include "net.h"

#include <Game.h>
#include <Windows.h>
#include <Script.h>
#include <cstdio>
#include <cstring>
#include <map>
#include <string>

namespace g3t::items {
namespace {
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

void FormatGuid(eCEntity* entity, char* out, size_t size)
{
    const eCEntityProxy proxy(entity);
    bCPropertyID::bSCore core;
    std::memcpy(&core, &proxy.GetEntityID().GetCore(), 16);
    sprintf_s(out, size, "%08X-%04X-%04X-%02X%02X-%02X%02X%02X%02X%02X%02X", core.Data1, core.Data2, core.Data3, core.Data4[0],
              core.Data4[1], core.Data4[2], core.Data4[3], core.Data4[4], core.Data4[5], core.Data4[6], core.Data4[7]);
}

float DistanceToHero(eCEntity* entity)
{
    eCEntity* hero = gCSession::GetInstance().GetHero();
    if (!hero || !entity) return 1e9f;
    const bCVector& a = entity->GetWorldPosition();
    const bCVector& b = hero->GetWorldPosition();
    const float p[3] = {a.GetX(), a.GetY(), a.GetZ()}, q[3] = {b.GetX(), b.GetY(), b.GetZ()};
    return body::Distance(p, q);
}

void Line(char const* tag, char const* guid, Entity& item, LogFn log)
{
    char line[300];
    Entity owner = item.GetInstance() ? item.GetOwner() : Entity();
    sprintf_s(line, "%s guid=%s exists=%d name=%s item=%d owner=%s distance_cm=%.0f", tag, guid, item.GetInstance() != nullptr,
              item.GetInstance() ? item.GetName().GetText() : "-", item.GetInstance() && item.IsItem() ? 1 : 0,
              owner.GetInstance() ? owner.GetName().GetText() : "-", DistanceToHero(item.GetInstance()));
    log(line);
}
}

void List(LogFn log)
{
    char line[300], guid[48];
    const bTObjArray<Entity> all = Entity::GetEntities();
    int items = 0;
    for (int i = 0; i < all.GetCount(); ++i) {
        Entity entity = all.GetAt(i);
        if (!entity.GetInstance() || !entity.IsItem() || entity.GetOwner().GetInstance()) continue;
        const float distance = DistanceToHero(entity.GetInstance());
        if (distance > 3000.0f) continue;
        FormatGuid(entity.GetInstance(), guid, sizeof(guid));
        sprintf_s(line, "ITEM guid=%s name=%s distance_cm=%.0f", guid, entity.GetName().GetText(), distance);
        log(line);
        ++items;
    }
    sprintf_s(line, "ITEMS entities=%d world_items_near=%d", all.GetCount(), items);
    log(line);
    log("ITEMS end");
}

void Take(char const* guid, LogFn log)
{
    _GUID raw;
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    if (!hero || !ParseGuid(guid, raw)) { log("REFUSED itemtake: no hero or bad GUID"); return; }
    Entity item = Entity(bCPropertyID(bCGuid(raw)));
    if (!item.GetInstance()) { log("REFUSED itemtake: no such entity"); return; }
    const GEInt stack = Entity(hero).Inventory.InsertStack(item);
    char line[120];
    sprintf_s(line, "ITEMTAKE guid=%s stack=%d", guid, stack);
    log(line);
    Entity again = Entity(bCPropertyID(bCGuid(raw)));
    Line("ITEMAFTER", guid, again, log);
}

void Decay(char const* guid, LogFn log)
{
    _GUID raw;
    if (!ParseGuid(guid, raw)) { log("REFUSED itemdecay: bad GUID"); return; }
    Entity item = Entity(bCPropertyID(bCGuid(raw)));
    if (!item.GetInstance()) { log("REFUSED itemdecay: no such entity"); return; }
    item.Decay();
    Entity again = Entity(bCPropertyID(bCGuid(raw)));
    Line("ITEMDECAY", guid, again, log);
}

void Describe(char const* guid, LogFn log)
{
    _GUID raw;
    if (!ParseGuid(guid, raw)) { log("REFUSED iteminfo: bad GUID"); return; }
    Entity item = Entity(bCPropertyID(bCGuid(raw)));
    Line("ITEMINFO", guid, item, log);
}
}

namespace g3t::items {
namespace {
// The item a hero's _AI_TakeItem is working on: its GUID disappears once the game puts it into a stack.
uint8_t takingGuid[16] = {};
char takingName[64] = "";
bool taking = false;
uint32_t takingAmount = 1;
std::map<std::string, int> carriedBefore;  // the hero's inventory when the take began: item template -> amount

std::map<std::string, int> Carried(eCDynamicEntity* hero)
{
    std::map<std::string, int> out;
    Entity self(hero);
    for (int i = 0; i < self.Inventory.GetStackCount(); ++i) {
        Entity item = self.Inventory.GetTemplateItem(i);
        if (item.GetInstance()) out[item.GetName().GetText()] += self.Inventory.GetAmount(i);
    }
    return out;
}
bool disabled = false;  // QA negative control
unsigned takesBegun = 0;
unsigned tookCount = 0, sent = 0, received = 0, granted = 0, denials = 0, removed = 0, returned = 0;
char lastTook[200] = "-";
}

void BeforeTake(gCScriptProcessingUnit* spu)
{
    eCEntity* item = spu ? spu->GetTargetEntity() : nullptr;
    if (!item) item = spu ? spu->GetOtherEntity() : nullptr;
    taking = item != nullptr;
    if (!taking) return;
    ++takesBegun;
    const eCEntityProxy proxy(item);
    std::memcpy(takingGuid, &proxy.GetEntityID().GetCore(), 16);
    strncpy_s(takingName, Entity(item).GetName().GetText(), _TRUNCATE);
    takingAmount = Entity(item).Item.GetAmount();
    carriedBefore = Carried(gCSession::GetInstance().GetHero());
}

void AfterTake(gCScriptProcessingUnit*)
{
    if (!taking) return;
    _GUID raw;
    std::memcpy(&raw, takingGuid, 16);
    if (Entity(bCPropertyID(bCGuid(raw))).GetInstance()) return;  // not taken yet (the animation runs over frames)
    taking = false;
    ++tookCount;
    // What actually arrived can differ from the world item (a gold stack turns into coins): compare.
    for (auto const& [name, amount] : Carried(gCSession::GetInstance().GetHero())) {
        auto it = carriedBefore.find(name);
        const int gained = amount - (it == carriedBefore.end() ? 0 : it->second);
        if (gained <= 0) continue;
        strncpy_s(takingName, name.c_str(), _TRUNCATE);
        takingAmount = static_cast<uint32_t>(gained);
        break;
    }
    char guid[48];
    bCPropertyID::bSCore core;
    std::memcpy(&core, takingGuid, 16);
    sprintf_s(guid, "%08X-%04X-%04X-%02X%02X-%02X%02X%02X%02X%02X%02X", core.Data1, core.Data2, core.Data3, core.Data4[0],
              core.Data4[1], core.Data4[2], core.Data4[3], core.Data4[4], core.Data4[5], core.Data4[6], core.Data4[7]);
    sprintf_s(lastTook, "guid=%s name=%s amount=%u", guid, takingName, takingAmount);
    Logf("items: hero took %s", lastTook);
    if (disabled) return;
    net::ItemNote note{net::IsHost() ? net::ItemGone : net::ItemTook, {}, takingAmount, {}};
    std::memcpy(note.guid, takingGuid, 16);
    strncpy_s(note.name, takingName, _TRUNCATE);
    net::SendItem(note, 0);  // host: gone for everyone; client: a claim to the host
    ++sent;
}

namespace {
// The world item with this GUID, if it still lies in the world on this machine.
Entity WorldItem(uint8_t const guid[16])
{
    _GUID raw;
    std::memcpy(&raw, guid, 16);
    Entity item = Entity(bCPropertyID(bCGuid(raw)));
    if (item.GetInstance() && item.IsItem() && !item.GetOwner().GetInstance()) return item;
    return Entity();
}

void OnItem(uint8_t from, net::ItemNote const& note)
{
    if (disabled) return;
    ++received;
    if (net::IsHost() && note.op == net::ItemTook) {
        Entity item = WorldItem(note.guid);
        if (item.GetInstance()) {
            // First to take it: gone from the host's world and from every other player's.
            item.Decay();
            net::ItemNote gone = note;
            gone.op = net::ItemGone;
            net::SendItem(gone, 0);
            ++granted;
            Logf("items: player %u took %s", from, note.name);
        } else {
            net::ItemNote denied = note;
            denied.op = net::ItemDenied;
            net::SendItem(denied, from);
            ++denials;
            Logf("items: player %u was too late for %s", from, note.name);
        }
        return;
    }
    if (net::IsHost()) return;
    if (note.op == net::ItemGone) {
        if (Entity item = WorldItem(note.guid); item.GetInstance()) {
            item.Decay();
            ++removed;
        }
    } else if (note.op == net::ItemDenied) {
        // Someone else had it first: give back what this hero took.
        eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
        if (!hero) return;
        Entity self(hero);
        const GEInt stack = self.Inventory.FindStackIndex(Template(note.name));
        if (stack >= 0) self.Inventory.DeleteItems(stack, note.amount ? static_cast<GEInt>(note.amount) : 1);
        ++returned;
        Logf("items: returned %s (someone else took it first)", note.name);
    }
}
}

void Start() { net::SetItemHandler(&OnItem); }

void Pick(char const* guid, LogFn log)
{
    _GUID raw;
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    if (!hero || !ParseGuid(guid, raw)) { log("REFUSED itempick: no hero or bad GUID"); return; }
    Entity item = Entity(bCPropertyID(bCGuid(raw)));
    if (!item.GetInstance()) { log("REFUSED itempick: no such entity"); return; }
    Entity self(hero);
    self.NPC.SetCurrentTarget(item);
    self.Routine.FullStop();
    self.Routine.SetTask(bCString("PS_Normal_Take"));
    char line[200];
    sprintf_s(line, "ITEMPICK guid=%s task=%s", guid, self.Routine.GetCurrentTask().GetText());
    log(line);
}

void TookReport(LogFn log)
{
    char line[320];
    sprintf_s(line, "ITEMTOOK count=%u sent=%u received=%u granted=%u denials=%u removed=%u returned=%u last=%s", tookCount, sent,
              received, granted, denials, removed, returned, lastTook);
    log(line);
}
}

namespace g3t::items {
void Count(char const* templateName, LogFn log)
{
    char line[160];
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    const GEInt amount = hero ? Entity(hero).Inventory.GetItemAmount(Template(templateName)) : -1;
    sprintf_s(line, "INVCOUNT name=%s amount=%d", templateName, amount);
    log(line);
}

void Disable(LogFn log)
{
    disabled = true;
    log("ITEMSYNCOFF ok=1");
}

unsigned Returned() { return returned; }
unsigned TakesBegun() { return takesBegun; }
unsigned TakesDone() { return tookCount; }
}

namespace g3t::items {
// QA diagnostic: entities near the hero that carry an inventory and are not living NPCs (chests, corpses),
// with their contents as "template:amount" lists.
void Containers(LogFn log)
{
    char guid[48];
    std::string line;
    const bTObjArray<Entity> all = Entity::GetEntities();
    eCEntity* hero = gCSession::GetInstance().GetHero();
    int found = 0;
    for (int i = 0; i < all.GetCount(); ++i) {
        Entity entity = all.GetAt(i);
        eCEntity* instance = entity.GetInstance();
        if (!instance || instance == hero || entity.IsItem() || !instance->GetPropertySet(eEPropertySetType_Inventory)) continue;
        const bool npc = instance->GetPropertySet(eEPropertySetType_NPC) != nullptr;
        if (npc && !entity.IsDead()) continue;
        if (DistanceToHero(instance) > 4000.0f) continue;
        FormatGuid(instance, guid, sizeof(guid));
        line = std::string("CONTAINER guid=") + guid + " name=" + entity.GetName().GetText() + (npc ? " kind=corpse" : " kind=other") +
               " items=";
        std::map<std::string, int> contents;
        for (int s = 0; s < entity.Inventory.GetStackCount(); ++s) {
            Entity item = entity.Inventory.GetTemplateItem(s);
            if (item.GetInstance()) contents[item.GetName().GetText()] += entity.Inventory.GetAmount(s);
        }
        bool first = true;
        for (auto const& [name, amount] : contents) {
            line += (first ? "" : ",") + name + ":" + std::to_string(amount);
            first = false;
        }
        if (first) line += "-";
        log(line.c_str());
        ++found;
    }
    log("CONTAINERS end");
}
}
