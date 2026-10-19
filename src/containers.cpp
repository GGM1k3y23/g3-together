// SPDX-License-Identifier: GPL-3.0-or-later
#include "containers.h"

#include "body.h"
#include "log.h"
#include "net.h"

#include <Game.h>
#include <Script.h>
#include <Windows.h>
#include <array>
#include <cstdio>
#include <cstring>
#include <map>
#include <string>
#include <unordered_map>
#include <vector>

namespace g3t::containers {
namespace {
using Content = std::map<std::string, int>;  // item template ID (16 bytes) -> amount, equipped stacks left out
std::unordered_map<std::string, std::string> names;  // template ID -> item name, for logs and QA
constexpr float InterestRadius = 4000.0f;

bool disabled = false;
unsigned contentsSent = 0, contentsReceived = 0, rewrites = 0, txSent = 0, txApplied = 0, txRefused = 0, txDenied = 0,
         reverted = 0;

std::string Key(uint8_t const guid[16]) { return std::string(reinterpret_cast<char const*>(guid), 16); }

void GuidOf(eCEntity* entity, uint8_t out[16])
{
    const eCEntityProxy proxy(entity);
    std::memcpy(out, &proxy.GetEntityID().GetCore(), 16);
}

Entity Resolve(uint8_t const guid[16])
{
    _GUID raw;
    std::memcpy(&raw, guid, 16);
    return Entity(bCPropertyID(bCGuid(raw)));
}

Entity Resolve(std::string const& key) { return Resolve(reinterpret_cast<uint8_t const*>(key.data())); }

bool ParseGuid(char const* text, uint8_t out[16])
{
    unsigned d1, d2, d3, b[8];
    if (sscanf_s(text, "%8X-%4X-%4X-%2X%2X-%2X%2X%2X%2X%2X%2X", &d1, &d2, &d3, &b[0], &b[1], &b[2], &b[3], &b[4], &b[5], &b[6],
                 &b[7]) != 11)
        return false;
    _GUID raw{d1, static_cast<unsigned short>(d2), static_cast<unsigned short>(d3), {}};
    for (int i = 0; i < 8; ++i) raw.Data4[i] = static_cast<unsigned char>(b[i]);
    std::memcpy(out, &raw, 16);
    return true;
}

bool HasInventory(eCEntity* entity) { return entity && entity->GetPropertySet(eEPropertySetType_Inventory); }

// Chests and corpses: an inventory, not an item, not a living NPC.
bool IsContainer(Entity& entity)
{
    eCEntity* instance = entity.GetInstance();
    if (!HasInventory(instance) || entity.IsItem() || instance == gCSession::GetInstance().GetHero()) return false;
    return !instance->GetPropertySet(eEPropertySetType_NPC) || entity.IsDead();
}

std::string TemplateKey(Entity& entity, int stack)
{
    const Template item = entity.Inventory.GetTemplate(stack);
    bCPropertyID const& id = item;
    return std::string(reinterpret_cast<char const*>(&id.GetCore()), 16);
}

Template TemplateOf(std::string const& key)
{
    _GUID raw;
    std::memcpy(&raw, key.data(), 16);
    return Template(bCPropertyID(bCGuid(raw)));
}

char const* NameOf(std::string const& key)
{
    auto it = names.find(key);
    return it == names.end() ? "?" : it->second.c_str();
}

Content Read(Entity& entity)
{
    Content content;
    for (int i = 0; i < entity.Inventory.GetStackCount(); ++i) {
        if (entity.Inventory.GetSlotFromStack(i) != gESlot_None) continue;
        Entity item = entity.Inventory.GetTemplateItem(i);
        if (!item.GetInstance()) continue;
        const std::string key = TemplateKey(entity, i);
        names[key] = item.GetName().GetText();
        content[key] += entity.Inventory.GetAmount(i);
    }
    return content;
}

void Write(Entity& entity, Content const& content)
{
    for (int i = entity.Inventory.GetStackCount() - 1; i >= 0; --i)
        if (entity.Inventory.GetSlotFromStack(i) == gESlot_None) entity.Inventory.DeleteStack(i);
    for (auto const& [key, amount] : content)
        if (amount > 0) entity.Inventory.CreateItems(TemplateOf(key), 0, amount);
    ++rewrites;
}

std::vector<net::ContentEntry> Entries(Content const& content)
{
    std::vector<net::ContentEntry> out;
    for (auto const& [key, amount] : content) {
        net::ContentEntry entry{{}, {}, amount};
        std::memcpy(entry.id, key.data(), 16);
        strncpy_s(entry.name, NameOf(key), _TRUNCATE);
        out.push_back(entry);
    }
    return out;
}

std::string KeyOf(net::ContentEntry const& entry) { return std::string(reinterpret_cast<char const*>(entry.id), 16); }

Content FromEntries(net::ContentEntry const* entries, int count)
{
    Content content;
    for (int i = 0; i < count; ++i) {
        const std::string key = KeyOf(entries[i]);
        names.emplace(key, entries[i].name);
        content[key] += entries[i].amount;
    }
    return content;
}

std::string Describe(Content const& content)
{
    std::map<std::string, int> byName;
    for (auto const& [key, amount] : content) byName[NameOf(key)] += amount;
    std::string out;
    for (auto const& [name, amount] : byName) out += (out.empty() ? "" : ",") + name + ":" + std::to_string(amount);
    return out.empty() ? "-" : out;
}

float DistanceTo(eCEntity* entity, float const at[3])
{
    const bCVector& p = entity->GetWorldPosition();
    const float q[3] = {p.GetX(), p.GetY(), p.GetZ()};
    return body::Distance(q, at);
}

// --- host ---
std::unordered_map<std::string, Content> lastSent;
std::unordered_map<std::string, DWORD> watched;  // traders a client deals with: key -> until
uint32_t lastJoins = 0;
DWORD lastHostPoll = 0;

void Broadcast(std::string const& key, Content const& content)
{
    const std::vector<net::ContentEntry> entries = Entries(content);
    net::SendContents(reinterpret_cast<uint8_t const*>(key.data()), entries.data(), static_cast<int>(entries.size()));
    lastSent[key] = content;
    ++contentsSent;
}

void HostTick()
{
    const DWORD now = GetTickCount();
    if (now - lastHostPoll < 1000) return;
    lastHostPoll = now;
    if (net::JoinCount() != lastJoins) {
        lastJoins = net::JoinCount();
        lastSent.clear();  // a newcomer needs everything
    }
    std::vector<std::array<float, 3>> players;
    if (eCEntity* hero = gCSession::GetInstance().GetHero()) {
        const bCVector& p = hero->GetWorldPosition();
        players.push_back({p.GetX(), p.GetY(), p.GetZ()});
    }
    net::Remote const* remotes = net::Remotes();
    bool anyClient = false;
    for (int id = 2; id < net::MaxPlayers; ++id) {
        if (!remotes[id].connected) continue;
        anyClient = true;
        if (remotes[id].latest.flags & 1)
            players.push_back({remotes[id].latest.position[0], remotes[id].latest.position[1], remotes[id].latest.position[2]});
    }
    if (!anyClient) return;
    for (auto it = watched.begin(); it != watched.end();) it = static_cast<int32_t>(it->second - now) < 0 ? watched.erase(it) : ++it;
    const bTObjArray<Entity> all = Entity::GetEntities();
    for (int i = 0; i < all.GetCount(); ++i) {
        Entity entity = all.GetAt(i);
        eCEntity* instance = entity.GetInstance();
        if (!instance) continue;
        uint8_t guid[16];
        GuidOf(instance, guid);
        const std::string key = Key(guid);
        bool wanted = watched.count(key) && HasInventory(instance);
        if (!wanted && IsContainer(entity))
            for (auto const& at : players) wanted = wanted || DistanceTo(instance, at.data()) <= InterestRadius;
        if (!wanted) continue;
        const Content content = Read(entity);
        auto sent = lastSent.find(key);
        if (sent == lastSent.end() || sent->second != content) Broadcast(key, content);
    }
}

void OnTx(uint8_t from, uint8_t const guid[16], uint32_t seq, net::ContentEntry const* entries, int count)
{
    if (disabled) return;
    Entity container = Resolve(guid);
    bool ok = container.GetInstance() && HasInventory(container.GetInstance());
    Content have = ok ? Read(container) : Content{};
    for (int i = 0; i < count && ok; ++i)
        if (entries[i].amount < 0 && have[KeyOf(entries[i])] < -entries[i].amount) ok = false;
    if (ok) {
        // All or nothing: every removal is still possible, so apply the whole transaction.
        for (int i = 0; i < count; ++i) {
            const Template item = TemplateOf(KeyOf(entries[i]));
            if (entries[i].amount < 0) {
                const GEInt stack = container.Inventory.FindStackIndex(item);
                if (stack >= 0) container.Inventory.DeleteItems(stack, -entries[i].amount);
            } else if (entries[i].amount > 0) {
                container.Inventory.CreateItems(item, 0, entries[i].amount);
            }
        }
        ++txApplied;
    } else {
        ++txRefused;
    }
    net::SendContainerTxResult(from, guid, seq, ok);
    if (container.GetInstance()) Broadcast(Key(guid), Read(container));
    Logf("containers: transaction %u from player %u %s (%d entries)", seq, from, ok ? "applied" : "refused", count);
}

void OnWatch(uint8_t, uint8_t const guid[16])
{
    if (!disabled) watched[Key(guid)] = GetTickCount() + 120000;
}

// --- client ---
std::unordered_map<std::string, Content> known;  // the host's contents
std::string active;                              // the container this client's player opened
DWORD activeSince = 0;
struct Pending {
    std::string key;
    Content deltas;  // container side: negative = the hero took, positive = the hero put in
};
std::map<uint32_t, Pending> pending;
uint32_t nextSeq = 1;
DWORD lastClientPoll = 0, lastResync = 0;

bool PendingFor(std::string const& key)
{
    for (auto const& entry : pending)
        if (entry.second.key == key) return true;
    return false;
}

// What this client's player changed in the open container since the host's last contents: one transaction.
void ReportChange(Entity& container, Content& hostContent)
{
    const Content local = Read(container);
    if (local == hostContent) return;
    Content deltas;
    for (auto const& [name, amount] : local) deltas[name] += amount;
    for (auto const& [name, amount] : hostContent) deltas[name] -= amount;
    for (auto it = deltas.begin(); it != deltas.end();) it = it->second == 0 ? deltas.erase(it) : ++it;
    const uint32_t seq = nextSeq++;
    pending[seq] = Pending{active, deltas};
    const std::vector<net::ContentEntry> entries = Entries(deltas);
    net::SendContainerTx(reinterpret_cast<uint8_t const*>(active.data()), seq, entries.data(), static_cast<int>(entries.size()));
    ++txSent;
    hostContent = local;  // optimistic until the result arrives
}

void OnContents(uint8_t const guid[16], net::ContentEntry const* entries, int count)
{
    if (disabled) return;
    ++contentsReceived;
    const std::string key = Key(guid);
    Entity container = Resolve(guid);
    if (key == active && container.GetInstance() && !PendingFor(key) && known.count(key)) {
        // A change by this player that the poll has not reported yet must not be overwritten (that lost the
        // take and doubled the item): report it against the contents it was made on; the host decides.
        ReportChange(container, known[key]);
    }
    known[key] = FromEntries(entries, count);
    if (key == active && PendingFor(key)) return;  // the transaction result decides first
    if (container.GetInstance() && Read(container) != known[key]) Write(container, known[key]);
}

void OnTxResult(uint8_t const guid[16], uint32_t seq, bool ok)
{
    auto it = pending.find(seq);
    if (it == pending.end()) return;
    const Pending done = it->second;
    pending.erase(it);
    if (ok) return;
    ++txDenied;
    // Undo what this hero gained or gave in that transaction, and in anything done since that was not
    // reported yet; the container then follows the host's contents.
    Entity container = Resolve(guid);
    Content undo = done.deltas;
    if (auto optimistic = known.find(done.key); container.GetInstance() && optimistic != known.end()) {
        for (auto const& [name, amount] : Read(container)) undo[name] += amount;
        for (auto const& [name, amount] : optimistic->second) undo[name] -= amount;
    }
    if (eCDynamicEntity* hero = gCSession::GetInstance().GetHero()) {
        Entity self(hero);
        for (auto const& [key, delta] : undo) {
            const Template item = TemplateOf(key);
            if (delta < 0) {
                const GEInt stack = self.Inventory.FindStackIndex(item);
                if (stack >= 0) self.Inventory.DeleteItems(stack, -delta);
            } else if (delta > 0) {
                self.Inventory.CreateItems(item, 0, delta);
            }
        }
        ++reverted;
    }
    auto host = known.find(done.key);
    if (container.GetInstance() && host != known.end()) Write(container, host->second);
    Logf("containers: transaction %u refused by the host; undone", seq);
}

void ClientTick()
{
    const DWORD now = GetTickCount();
    if (now - lastClientPoll < 250) return;
    lastClientPoll = now;
    eCEntity* hero = gCSession::GetInstance().GetHero();
    if (!hero || !net::Connected()) return;
    if (!active.empty()) {
        Entity container = Resolve(active);
        auto host = known.find(active);
        if (container.GetInstance() && host != known.end() && !PendingFor(active)) ReportChange(container, host->second);
        // Only after reporting what changed: the player walked away, or the container is gone.
        const bCVector& h = hero->GetWorldPosition();
        const float at[3] = {h.GetX(), h.GetY(), h.GetZ()};
        if (!container.GetInstance() || DistanceTo(container.GetInstance(), at) > 800.0f || now - activeSince > 120000) active.clear();
    }
    if (now - lastResync < 2000) return;
    lastResync = now;
    // Anything else that drifted from the host's contents (a corpse rolling its own plunder) is put back.
    for (auto const& [key, content] : known) {
        if (key == active) continue;
        Entity container = Resolve(key);
        if (container.GetInstance() && Read(container) != content) Write(container, content);
    }
}

// The game's own entry points for opening loot and trade: remember what this client's player opened.
gFScript originalLoot = nullptr, originalTrade = nullptr;
bool hooked = false;
DWORD lastHookTry = 0;

void Opened(gCScriptProcessingUnit* spu, GELPVoid other, bool trade)
{
    if (net::IsHost() || disabled) return;
    Entity partner = other ? *static_cast<Entity*>(other) : Entity(spu ? spu->GetOtherEntity() : nullptr);
    if (!partner.GetInstance()) return;
    uint8_t guid[16];
    GuidOf(partner.GetInstance(), guid);
    active = Key(guid);
    activeSince = GetTickCount();
    if (trade) net::SendWatch(guid);  // a living trader is not sent unless someone deals with him
}

GEInt GE_STDCALL OpenLoot(gCScriptProcessingUnit* spu, GELPVoid self, GELPVoid other, GEInt args)
{
    Opened(spu, other, false);
    return originalLoot(spu, self, other, args);
}

GEInt GE_STDCALL OpenTrade(gCScriptProcessingUnit* spu, GELPVoid self, GELPVoid other, GEInt args)
{
    Opened(spu, other, true);
    return originalTrade(spu, self, other, args);
}
}

void Start() { net::SetContainerHandlers(&OnContents, &OnTx, &OnTxResult, &OnWatch); }

void Tick()
{
    if (disabled || !gCSession::GetInstance().IsGameRunning()) return;
    if (!hooked && GetTickCount() - lastHookTry > 1000) {
        lastHookTry = GetTickCount();
        gSScript *loot = nullptr, *trade = nullptr;
        auto& scripts = GetScriptAdminExt().GetScripts();
        if (scripts.GetAt(bCString("PlayerOpenLoot"), loot) && loot && scripts.GetAt(bCString("PlayerOpenTrade"), trade) && trade) {
            originalLoot = loot->m_funcScript;
            loot->m_funcScript = &OpenLoot;
            originalTrade = trade->m_funcScript;
            trade->m_funcScript = &OpenTrade;
            hooked = true;
            Log("containers: loot and trade hooks installed");
        }
    }
    if (net::IsHost()) HostTick();
    else ClientTick();
}

void Contents(char const* guid, LogFn log)
{
    uint8_t raw[16];
    Entity container = ParseGuid(guid, raw) ? Resolve(raw) : Entity();
    if (!container.GetInstance() || !HasInventory(container.GetInstance())) { log("REFUSED contents: no container"); return; }
    const std::string line = "CONTENTS guid=" + std::string(guid) + " items=" + Describe(Read(container));
    log(line.c_str());
}

void Open(char const* guid, LogFn log)
{
    uint8_t raw[16];
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    Entity container = ParseGuid(guid, raw) ? Resolve(raw) : Entity();
    if (!hero || !container.GetInstance()) { log("REFUSED lootopen: no hero or container"); return; }
    // Stand next to it first, as a player does.
    const bCVector& c = container.GetInstance()->GetWorldPosition();
    const float position[3] = {c.GetX() + 100.0f, c.GetY(), c.GetZ()};
    const bCQuaternion q = hero->GetWorldRotation();
    const float rotation[4] = {q.GetX(), q.GetY(), q.GetZ(), q.GetW()};
    Entity(hero).MoveTo(body::Pose(*hero, position, rotation));
    const GEInt result = GetScriptAdminExt().CallScript(bCString("PlayerOpenLoot"), hero, container.GetInstance(), 0);
    char line[160];
    sprintf_s(line, "LOOTOPEN guid=%s result=%d active=%d", guid, result, !active.empty());
    log(line);
}

void Take(char const* argument, LogFn log)
{
    // "GUID" takes the first stack, "GUID_Template" the stack of that item template.
    uint8_t raw[16];
    char guid[40] = {};
    strncpy_s(guid, argument, 36);
    char const* wanted = std::strlen(argument) > 37 && argument[36] == '_' ? argument + 37 : nullptr;
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    Entity container = ParseGuid(guid, raw) ? Resolve(raw) : Entity();
    if (!hero || !container.GetInstance()) { log("REFUSED loottake: no hero or container"); return; }
    for (int i = 0; i < container.Inventory.GetStackCount(); ++i) {
        if (container.Inventory.GetSlotFromStack(i) != gESlot_None) continue;
        Entity item = container.Inventory.GetTemplateItem(i);
        if (wanted && (!item.GetInstance() || std::strcmp(item.GetName().GetText(), wanted) != 0)) continue;
        const GEInt amount = container.Inventory.GetAmount(i);
        Entity self(hero);
        const GEBool ok = container.Inventory.TransferAmountTo(i, amount, self);
        char line[200];
        sprintf_s(line, "LOOTTAKE guid=%s name=%s amount=%d ok=%d", guid, item.GetInstance() ? item.GetName().GetText() : "-", amount, ok ? 1 : 0);
        log(line);
        return;
    }
    log("REFUSED loottake: empty");
}

void Report(LogFn log)
{
    char line[300];
    sprintf_s(line, "CONTAINERSYNC hooked=%d active=%d known=%zu pending=%zu contents_sent=%u contents_received=%u rewrites=%u tx_sent=%u tx_applied=%u tx_refused=%u tx_denied=%u reverted=%u",
              hooked, !active.empty(), known.size(), pending.size(), contentsSent, contentsReceived, rewrites, txSent, txApplied,
              txRefused, txDenied, reverted);
    log(line);
}

void Disable(LogFn log)
{
    disabled = true;
    log("CONTAINERSYNCOFF ok=1");
}
}

namespace g3t::containers {
// QA (host): the first stack of a container leaves without a contents message, as if someone took it
// unseen; a client still showing it gets its take refused.
void Silent(char const* argument, LogFn log)
{
    uint8_t raw[16];
    char guid[40] = {};
    strncpy_s(guid, argument, 36);
    char const* wanted = std::strlen(argument) > 37 && argument[36] == '_' ? argument + 37 : nullptr;
    Entity container = ParseGuid(guid, raw) ? Resolve(raw) : Entity();
    if (!net::IsHost() || !container.GetInstance()) { log("REFUSED lootsilent: not the host or no container"); return; }
    for (int i = 0; i < container.Inventory.GetStackCount(); ++i) {
        if (container.Inventory.GetSlotFromStack(i) != gESlot_None) continue;
        Entity item = container.Inventory.GetTemplateItem(i);
        if (wanted && (!item.GetInstance() || std::strcmp(item.GetName().GetText(), wanted) != 0)) continue;
        char line[200];
        sprintf_s(line, "LOOTSILENT guid=%s name=%s amount=%d", guid, item.GetInstance() ? item.GetName().GetText() : "-",
                  container.Inventory.GetAmount(i));
        container.Inventory.DeleteStack(i);
        lastSent[Key(raw)] = Read(container);
        log(line);
        return;
    }
    log("REFUSED lootsilent: empty");
}
}

namespace g3t::containers {
namespace {
// "GUID" or "GUID_Template" (the QA argument alphabet has no other separator).
Entity Target(char const* argument, char const** wanted)
{
    uint8_t raw[16];
    char guid[40] = {};
    strncpy_s(guid, argument, 36);
    *wanted = std::strlen(argument) > 37 && argument[36] == '_' ? argument + 37 : nullptr;
    return ParseGuid(guid, raw) ? Resolve(raw) : Entity();
}

int StackOf(Entity& entity, char const* wanted, bool skipGold)
{
    for (int i = 0; i < entity.Inventory.GetStackCount(); ++i) {
        if (entity.Inventory.GetSlotFromStack(i) != gESlot_None) continue;
        Entity item = entity.Inventory.GetTemplateItem(i);
        if (!item.GetInstance()) continue;
        char const* name = item.GetName().GetText();
        if (wanted ? std::strcmp(name, wanted) == 0 : !(skipGold && std::strcmp(name, "It_Gold") == 0)) return i;
    }
    return -1;
}
}

// QA: open trade with an NPC the way a trade dialog option does (PlayerOpenTrade), standing next to him.
void TradeOpen(char const* argument, LogFn log)
{
    char const* unused = nullptr;
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    Entity trader = Target(argument, &unused);
    if (!hero || !trader.GetInstance()) { log("REFUSED tradeopen: no hero or trader"); return; }
    const bCVector& c = trader.GetInstance()->GetWorldPosition();
    const float position[3] = {c.GetX() + 120.0f, c.GetY(), c.GetZ()};
    const bCQuaternion q = hero->GetWorldRotation();
    const float rotation[4] = {q.GetX(), q.GetY(), q.GetZ(), q.GetW()};
    Entity(hero).MoveTo(body::Pose(*hero, position, rotation));
    const GEInt result = GetScriptAdminExt().CallScript(bCString("PlayerOpenTrade"), hero, trader.GetInstance(), 0);
    char line[200];
    sprintf_s(line, "TRADEOPEN trader=%s result=%d active=%d", trader.GetName().GetText(), result, !active.empty());
    log(line);
}

// QA: buy one piece (the first non-gold stack, or the named template) and pay its gold value, as the trade
// screen moves the item one way and the gold the other.
void TradeBuy(char const* argument, LogFn log)
{
    char const* wanted = nullptr;
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    Entity trader = Target(argument, &wanted);
    if (!hero || !trader.GetInstance()) { log("REFUSED tradebuy: no hero or trader"); return; }
    Entity self(hero);
    const int stack = StackOf(trader, wanted, true);
    if (stack < 0) { log("REFUSED tradebuy: nothing to buy"); return; }
    Entity item = trader.Inventory.GetTemplateItem(stack);
    const std::string name = item.GetName().GetText();
    const int price = item.Item.GetGoldValue() > 0 ? item.Item.GetGoldValue() : 1;
    const int gold = StackOf(self, "It_Gold", false);
    if (gold < 0 || self.Inventory.GetAmount(gold) < price) { log("REFUSED tradebuy: not enough gold"); return; }
    const GEBool got = trader.Inventory.TransferAmountTo(stack, 1, self);
    const GEBool paid = self.Inventory.TransferAmountTo(StackOf(self, "It_Gold", false), price, trader);
    char line[200];
    sprintf_s(line, "TRADEBUY trader=%s name=%s price=%d got=%d paid=%d", trader.GetName().GetText(), name.c_str(), price, got ? 1 : 0, paid ? 1 : 0);
    log(line);
}

// QA: inventory contents of any entity (living NPCs included), equipped stacks left out.
void Goods(char const* argument, LogFn log)
{
    char const* unused = nullptr;
    Entity entity = Target(argument, &unused);
    if (!entity.GetInstance() || !HasInventory(entity.GetInstance())) { log("REFUSED goods: no inventory"); return; }
    const std::string line = std::string("GOODS guid=") + std::string(argument, 36) + " items=" + Describe(Read(entity));
    log(line.c_str());
}
}

namespace g3t::containers {
// QA (host): put one piece of an item into an NPC's inventory ("GUID_Template"), to set up a trade.
void Stock(char const* argument, LogFn log)
{
    char const* wanted = nullptr;
    Entity entity = Target(argument, &wanted);
    if (!entity.GetInstance() || !wanted || !HasInventory(entity.GetInstance())) { log("REFUSED stock: need GUID_Template"); return; }
    const GEInt stack = entity.Inventory.CreateItems(Template(wanted), 0, 1);
    char line[160];
    sprintf_s(line, "STOCK name=%s stack=%d", wanted, stack);
    log(line);
}
}
