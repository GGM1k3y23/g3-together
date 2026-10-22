// SPDX-License-Identifier: GPL-3.0-or-later
#include "containers.h"

#include "body.h"
#include "log.h"
#include "net.h"
#include "remote.h"

#include <Game.h>
#include <Script.h>
#include <Windows.h>
#include <array>
#include <cstdarg>
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
constexpr float PreRollRadius = 1500.0f;  // a client this close to an unrolled chest: the host rolls it

bool disabled = false;
bool traderFillOff = false;  // QA negative control: the host does not fill traders before a dialog
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

// A chest: an inventory the game rolls from treasure sets when a player first opens it (PS_Interact_Chest), on
// every machine by itself unless told it already did.
bool IsChest(Entity& entity)
{
    eCEntity* instance = entity.GetInstance();
    return HasInventory(instance) && !entity.IsItem() && !instance->GetPropertySet(eEPropertySetType_NPC) &&
           instance != gCSession::GetInstance().GetHero();
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

// A trader: a living NPC with a treasure set the game fills its trade inventory from (in ZS_Talk, on the machine where
// the dialog runs; C container-populate-c.md).
bool IsTrader(Entity& entity)
{
    eCEntity* instance = entity.GetInstance();
    if (!HasInventory(instance) || !instance->GetPropertySet(eEPropertySetType_NPC) || entity.IsDead() ||
        instance == gCSession::GetInstance().GetHero())
        return false;
    Entity const sets[5] = {entity.Inventory.GetTreasureSet1(), entity.Inventory.GetTreasureSet2(), entity.Inventory.GetTreasureSet3(),
                            entity.Inventory.GetTreasureSet4(), entity.Inventory.GetTreasureSet5()};
    for (Entity set : sets) {
        if (!set.GetInstance()) continue;
        const gETreasureDistribution kind = set.TreasureSet.TreasureDistribution;
        if (kind == gETreasureDistribution_Trade_Generate || kind == gETreasureDistribution_Trade_Refresh) return true;
    }
    return false;
}

// What players share through the host: chests, corpses and NPCs they trade with (any NPC the game lets them trade
// with, Diego too). Never the hero, a puppet or an item, whatever GUID arrives (Fable review 22.10.: a transaction
// with the host hero's GUID could change the host's own inventory).
bool Shared(Entity& entity)
{
    eCEntity* instance = entity.GetInstance();
    return HasInventory(instance) && !entity.IsItem() && instance != gCSession::GetInstance().GetHero() && !remote::IsPuppet(instance);
}

bool Corpse(Entity& entity)
{
    eCEntity* instance = entity.GetInstance();
    return instance && instance->GetPropertySet(eEPropertySetType_NPC) && entity.IsDead();
}

// A stack that is part of the contents: not in a slot (equipped or held).
bool Counted(Entity& entity, int stack)
{
    if (entity.Inventory.GetSlotFromStack(stack) != gESlot_None) return false;
    Entity item = entity.Inventory.GetTemplateItem(stack);
    // "Fist" is the bare-hand weapon an unarmed NPC dies holding (host only, never plunder): counted, a client
    // that cannot have it drifted every 2 s and a quick loot sent "Fist:-1" every poll (stageQuickLoot 22.10.).
    return item.GetInstance() && std::strcmp(item.GetName().GetText(), "Fist") != 0;
}

Content Read(Entity& entity)
{
    Content content;
    for (int i = 0; i < entity.Inventory.GetStackCount(); ++i) {
        if (!Counted(entity, i)) continue;
        Entity item = entity.Inventory.GetTemplateItem(i);
        const std::string key = TemplateKey(entity, i);
        names[key] = item.GetName().GetText();
        content[key] += entity.Inventory.GetAmount(i);
    }
    return content;
}

void Write(Entity& entity, Content const& content)
{
    // On the host a dying NPC puts what it held into its inventory (a slave's rice), while its mirror here still holds
    // it: new items of that kind would join the held stack and never count (Slave_08's It_FakeRice drifted every 2 s,
    // stageQuickLoot 22.10.). On a corpse such a held stack goes too. A weapon the host's NPC dropped is not in the
    // contents and stays in the mirror's hand.
    const bool corpse = Corpse(entity);
    for (int i = entity.Inventory.GetStackCount() - 1; i >= 0; --i)
        if (entity.Inventory.GetSlotFromStack(i) == gESlot_None || (corpse && content.count(TemplateKey(entity, i))))
            entity.Inventory.DeleteStack(i);
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

// Log lines about containers (PLAN-2026-10-22 1.6: the logs of the first play evening showed nothing about chests,
// corpses and traders). Capped, since a host passes many corpses and chests.
unsigned notes = 0;
constexpr unsigned MaxNotes = 600;

void Note(char const* format, ...)
{
    if (notes > MaxNotes) return;
    if (notes++ == MaxNotes) { Log("containers: log limit reached; no more container lines this session"); return; }
    char line[700];
    va_list args;
    va_start(args, format);
    _vsnprintf_s(line, sizeof(line), _TRUNCATE, format, args);  // as Logf: never end the process on a long line
    va_end(args);
    Log(line);
}

std::string NameOf(Entity& entity) { return entity.GetInstance() ? entity.GetName().GetText() : "?"; }

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
    auto before = lastSent.find(key);
    if (watched.count(key) || (before != lastSent.end() && before->second != content)) {  // first sends near a player are many
        Entity entity = Resolve(key);
        Note("containers: contents of %s sent: %s%s", NameOf(entity).c_str(), Describe(content).c_str(), watched.count(key) ? " (trader watched)" : "");
    }
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
    std::vector<std::array<float, 3>> clients;
    for (int id = 2; id < net::MaxPlayers; ++id) {
        if (!remotes[id].connected) continue;
        anyClient = true;
        if (remotes[id].latest.flags & 1) {
            players.push_back({remotes[id].latest.position[0], remotes[id].latest.position[1], remotes[id].latest.position[2]});
            clients.push_back(players.back());
        }
    }
    if (!anyClient) return;
    for (auto it = watched.begin(); it != watched.end();) it = static_cast<int32_t>(it->second - now) < 0 ? watched.erase(it) : ++it;
    const bTObjArray<Entity> all = Entity::GetEntities();
    for (int i = 0; i < all.GetCount(); ++i) {
        Entity entity = all.GetAt(i);
        eCEntity* instance = entity.GetInstance();
        if (!instance) continue;
        // Distance first, one position read: telling chests, corpses and traders apart takes several property
        // lookups per entity, too many for every entity of the world each second (Fable review 22.10.).
        bool nearPlayer = false;
        for (auto const& at : players) nearPlayer = nearPlayer || DistanceTo(instance, at.data()) <= InterestRadius;
        if (!nearPlayer) continue;
        uint8_t guid[16];
        GuidOf(instance, guid);
        const std::string key = Key(guid);
        // A chest a client comes close to is rolled here before the client can open it: its loot screen then
        // shows the host's contents from the start (the watch on opening stays as a fallback).
        if (IsChest(entity) && !entity.Inventory.IsGeneratedPlunder()) {
            bool close = false;
            for (auto const& at : clients) close = close || DistanceTo(instance, at.data()) <= PreRollRadius;
            if (close) {
                entity.Inventory.GeneratePlunderInvFromTreasureSets();
                entity.Inventory.SetGeneratedPlunder(GETrue);
                Logf("containers: a player came close to chest %s, rolled here: %s", entity.GetName().GetText(), Describe(Read(entity)).c_str());
            }
        }
        // A trader likewise: only the host fills it (a client's dialog runs on the host's NPC and fills nothing, so
        // whoever talked first saw goods and the other none, playtest 21.10.). The first dialog also tops it up from the
        // refresh sets, as ZS_Talk does.
        const bool trader = IsTrader(entity);
        bool clientClose = false;
        if (trader)
            for (auto const& at : clients) clientClose = clientClose || DistanceTo(instance, at.data()) <= PreRollRadius;
        if (trader && clientClose && !traderFillOff && !entity.Inventory.IsGeneratedTrade()) {
            entity.Inventory.GenerateTradeInvFromTreasureSets();
            if (static_cast<GEFloat>(entity.Dialog.EndDialogTimestamp) == 0.0f) entity.Inventory.RefreshTradeInvFromTreasureSets();
            Logf("containers: a player came close to trader %s, filled here: %s", entity.GetName().GetText(), Describe(Read(entity)).c_str());
        }
        bool wanted = (watched.count(key) && HasInventory(instance)) || IsContainer(entity);  // near a player (above)
        // Its goods go out while a client is close, so the trade screen shows them from the start.
        if (!wanted && trader && clientClose && entity.Inventory.IsGeneratedTrade()) wanted = true;
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
    bool ok = container.GetInstance() && Shared(container);
    Content have = ok ? Read(container) : Content{};
    constexpr int32_t MaxAmount = 1000000;  // more than any stack in the game; keeps -amount from overflowing
    for (int i = 0; i < count && ok; ++i)
        if (entries[i].amount < -MaxAmount || entries[i].amount > MaxAmount ||
            (entries[i].amount < 0 && have[KeyOf(entries[i])] < -entries[i].amount))
            ok = false;
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
    Logf("containers: transaction %u from player %u %s (%d entries: %s) at %s; now %s", seq, from, ok ? "applied" : "refused", count,
         Describe(FromEntries(entries, count)).c_str(), NameOf(container).c_str(), container.GetInstance() ? Describe(Read(container)).c_str() : "-");
}

void OnWatch(uint8_t from, uint8_t const guid[16])
{
    if (disabled) return;
    Entity chest = Resolve(guid);
    if (chest.GetInstance() && !Shared(chest)) return;
    watched[Key(guid)] = GetTickCount() + 120000;
    Note("containers: player %u opened %s (%s); here %s", from, NameOf(chest).c_str(), IsChest(chest) ? "chest" : "trade",
         chest.GetInstance() && HasInventory(chest.GetInstance()) ? Describe(Read(chest)).c_str() : "(not here)");
    // A client opened a chest: its contents are rolled here, once, as if the host had opened it.
    if (!IsChest(chest) || chest.Inventory.IsGeneratedPlunder()) return;
    chest.Inventory.GeneratePlunderInvFromTreasureSets();
    chest.Inventory.SetGeneratedPlunder(GETrue);
    const Content content = Read(chest);
    Broadcast(Key(guid), content);
    Logf("containers: player %u opened chest %s, rolled here: %s", from, chest.GetName().GetText(), Describe(content).c_str());
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
    Note("containers: transaction %u sent for %s: %s", seq, NameOf(container).c_str(), Describe(deltas).c_str());
    hostContent = local;  // optimistic until the result arrives
}

void OnContents(uint8_t const guid[16], net::ContentEntry const* entries, int count)
{
    if (disabled) return;
    ++contentsReceived;
    const std::string key = Key(guid);
    Entity container = Resolve(guid);
    if (container.GetInstance() && !Shared(container)) return;
    if (key == active && container.GetInstance() && !PendingFor(key) && known.count(key)) {
        // A change by this player that the poll has not reported yet must not be overwritten (that lost the
        // take and doubled the item): report it against the contents it was made on; the host decides.
        ReportChange(container, known[key]);
    }
    const bool first = !known.count(key);
    known[key] = FromEntries(entries, count);
    if (IsChest(container) && !container.Inventory.IsGeneratedPlunder()) container.Inventory.SetGeneratedPlunder(GETrue);
    if (key == active && first)
        Note("containers: host contents of the open %s: %s; here %s", NameOf(container).c_str(), Describe(known[key]).c_str(),
             container.GetInstance() ? Describe(Read(container)).c_str() : "(not here)");
    if (key == active && PendingFor(key)) return;  // the transaction result decides first
    if (container.GetInstance()) {
        const Content here = Read(container);
        if (here != known[key]) {
            Note("containers: %s%s set to the host's contents: %s (was %s)", NameOf(container).c_str(), key == active ? " (open)" : "",
                 Describe(known[key]).c_str(), Describe(here).c_str());
            Write(container, known[key]);
        }
    }
}

void OnTxResult(uint8_t const guid[16], uint32_t seq, bool ok)
{
    auto it = pending.find(seq);
    if (it == pending.end()) return;
    const Pending done = it->second;
    pending.erase(it);
    Note("containers: transaction %u %s by the host", seq, ok ? "applied" : "refused");
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
    // Chests open through the action key (PS_Interact -> PS_Interact_Chest), not PlayerOpenLoot: the chest in use is
    // this client's open container, and the host rolls its contents.
    Entity self(hero);
    if (std::strncmp(self.Routine.GetCurrentState().GetText(), "PS_Interact_Chest", 17) == 0) {
        Entity chest = self.Focus.GetFocusEntity();
        if (IsChest(chest) && chest.Interaction.GetUser().GetInstance() == hero) {
            uint8_t guid[16];
            GuidOf(chest.GetInstance(), guid);
            if (Key(guid) != active) {
                active = Key(guid);
                activeSince = now;
                net::SendWatch(guid);
                Logf("containers: opened chest %s (host contents known=%d)", chest.GetName().GetText(), known.count(active) ? 1 : 0);
            }
        }
    }
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
    // Chests near this player count as rolled here: only the host rolls them (a chest the host has not sent yet too).
    const bCVector& h = hero->GetWorldPosition();
    const float at[3] = {h.GetX(), h.GetY(), h.GetZ()};
    const bTObjArray<Entity> all = Entity::GetEntities();
    for (int i = 0; i < all.GetCount(); ++i) {
        Entity entity = all.GetAt(i);
        if (entity.GetInstance() && DistanceTo(entity.GetInstance(), at) <= InterestRadius && IsChest(entity) && !entity.Inventory.IsGeneratedPlunder())
            entity.Inventory.SetGeneratedPlunder(GETrue);
    }
    // Anything else that drifted from the host's contents (a corpse rolling its own plunder) is put back.
    for (auto const& [key, content] : known) {
        if (key == active) continue;
        Entity container = Resolve(key);
        // Far ones are skipped, not forgotten: the host sends a container again only when it changes.
        if (!container.GetInstance() || DistanceTo(container.GetInstance(), at) > InterestRadius) continue;
        const Content here = Read(container);
        if (here == content) continue;
        Note("containers: %s drifted; set back to the host's contents: %s (was %s)", NameOf(container).c_str(), Describe(content).c_str(),
             Describe(here).c_str());
        Write(container, content);
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
    auto host = known.find(active);
    Note("containers: this player opened %s (%s); here %s; host %s", NameOf(partner).c_str(), trade ? "trade" : "loot",
         Describe(Read(partner)).c_str(), host == known.end() ? "not known yet" : Describe(host->second).c_str());
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

// Quick Loot (option quick_loot): PS_Normal_Loop takes the stacks of the focused chest or corpse with one key
// press, calling PSInventory::TransferAmountTo directly (Script_Game.dll+0x81ced, through the import table; C
// quick-loot-c.md), without PlayerOpenLoot. On a client that call site goes through QuickTake: the container
// becomes this client's open container before anything leaves, so the next poll reports the take to the host
// as one transaction; without the host's contents to compare against, nothing is taken. ZS_RansackBody uses the
// same import from another call site and is not touched.
constexpr uintptr_t QuickLootCall = 0x81ced;
bool quickHooked = false, quickHookFailed = false;
unsigned quickTakes = 0, quickRefused = 0;

GEBool __fastcall QuickTake(PSInventory* self, void*, GEInt stack, GEInt amount, Entity& to)
{
    eCEntity* owner = nullptr;
    if (auto* set = reinterpret_cast<EntityPropertySet*>(self)->m_pEngineEntityPropertySet) owner = set->GetEntity();
    if (!net::IsHost() && !disabled && net::Connected() && owner && to.GetInstance() == gCSession::GetInstance().GetHero()) {
        uint8_t guid[16];
        GuidOf(owner, guid);
        const std::string key = Key(guid);
        Entity container(owner);
        if (!known.count(key)) {
            ++quickRefused;
            net::SendWatch(guid);
            Note("containers: quick loot from %s refused: the host's contents are not here yet", NameOf(container).c_str());
            return GEFalse;
        }
        if (key != active) {
            // Whatever the player changed in the container open before still goes out first.
            if (!active.empty() && !PendingFor(active) && known.count(active)) {
                Entity before = Resolve(active);
                if (before.GetInstance()) ReportChange(before, known[active]);
            }
            active = key;
            activeSince = GetTickCount();
            Note("containers: quick loot from %s; host %s", NameOf(container).c_str(), Describe(known[key]).c_str());
        }
        ++quickTakes;
    }
    return self->TransferAmountTo(stack, amount, to);
}

void HookQuickLoot()
{
    auto* base = reinterpret_cast<uint8_t*>(GetModuleHandleA("Script_Game.dll"));
    if (!base) return;
    uint8_t* site = base + QuickLootCall;
    // Expect "call dword ptr [Script_Game.dll+0xe4500]" (ff 15 <address>); anything else: another build, leave it.
    uint32_t slot = 0;
    std::memcpy(&slot, site + 2, 4);
    if (site[0] != 0xFF || site[1] != 0x15 || slot != reinterpret_cast<uintptr_t>(base) + 0xe4500) {
        quickHookFailed = true;
        Log("containers: quick loot call site not as expected; quick loot is not synchronized");
        return;
    }
    DWORD old = 0;
    if (!VirtualProtect(site, 6, PAGE_EXECUTE_READWRITE, &old)) { quickHookFailed = true; return; }
    site[0] = 0xE8;  // call rel32 QuickTake, then a nop: same length, same stack
    const int32_t to = static_cast<int32_t>(reinterpret_cast<intptr_t>(&QuickTake) - reinterpret_cast<intptr_t>(site + 5));
    std::memcpy(site + 1, &to, 4);
    site[5] = 0x90;
    VirtualProtect(site, 6, old, &old);
    FlushInstructionCache(GetCurrentProcess(), site, 6);
    quickHooked = true;
    Log("containers: quick loot hook installed");
}

// QA (1.2): the game's own quick loot input. Use2 in PS_Normal with QuickLoot on appends action 0x90 to the
// script's action list (Script_Game.dll+0x118d54: entries, count, capacity); PS_Normal_Loop takes it and loots
// the focused corpse or chest. The list's append helper (+0xcb90) takes the list header in EAX and the entry on
// the stack and cleans it up itself (C quick-loot-input-c.md).
struct QuickAction {
    GEU32 action;
    GEU32 aux;
    Entity target;
};
static_assert(sizeof(QuickAction) == 0xb0, "native action layout");
constexpr uintptr_t ActionList = 0x118d54, ActionAppend = 0xcb90;

void AppendAction(void* list, QuickAction* entry, void* append)
{
    __asm {
        mov eax, list
        push entry
        call append
    }
}
}

void QuickLoot(LogFn log)
{
    char line[300];
    auto* base = reinterpret_cast<uint8_t*>(GetModuleHandleA("Script_Game.dll"));
    Entity hero(gCSession::GetInstance().GetHero());
    char const* task = hero.GetInstance() ? hero.Routine.GetCurrentState().GetText() : "-";
    const int count = base ? *reinterpret_cast<int*>(base + ActionList + 4) : -1;
    const bool setting = eCApplication::GetInstance().GetEngineSetup().QuickLoot != GEFalse;
    char const* refused = !base ? "no_script_game" : !hero.GetInstance() ? "no_hero" : !setting ? "quick_loot_off"
                          : std::strcmp(task, "PS_Normal") && std::strcmp(task, "PS_Normal_Loop") ? "not_ps_normal" : count != 0 ? "actions_pending" : nullptr;
    if (refused) {
        sprintf_s(line, "QUICKLOOT queued=0 reason=%s task=%s actions=%d quick_loot=%d", refused, task, count, setting);
        log(line);
        return;
    }
    QuickAction entry{0x90, 0, Entity()};
    AppendAction(base + ActionList, &entry, base + ActionAppend);
    Entity focus = hero.Focus.GetFocusEntity();
    sprintf_s(line, "QUICKLOOT queued=1 task=%s actions_before=%d actions_after=%d focus=%s", task, count,
              *reinterpret_cast<int*>(base + ActionList + 4), NameOf(focus).c_str());
    log(line);
}

void QuickLootActions(LogFn log)
{
    auto* base = reinterpret_cast<uint8_t*>(GetModuleHandleA("Script_Game.dll"));
    char line[80];
    sprintf_s(line, "QUICKACTIONS count=%d", base ? *reinterpret_cast<int*>(base + ActionList + 4) : -1);
    log(line);
}

namespace {
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
    if (!quickHooked && !quickHookFailed) HookQuickLoot();
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
        if (!Counted(container, i)) continue;
        Entity item = container.Inventory.GetTemplateItem(i);
        if (wanted && std::strcmp(item.GetName().GetText(), wanted) != 0) continue;
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
    char line[400];
    sprintf_s(line, "CONTAINERSYNC hooked=%d quick_hooked=%d quick_takes=%u quick_refused=%u active=%d known=%zu pending=%zu contents_sent=%u contents_received=%u rewrites=%u tx_sent=%u tx_applied=%u tx_refused=%u tx_denied=%u reverted=%u",
              hooked, quickHooked, quickTakes, quickRefused, !active.empty(), known.size(), pending.size(), contentsSent, contentsReceived, rewrites, txSent, txApplied,
              txRefused, txDenied, reverted);
    log(line);
}

void Disable(char const* what, LogFn log)
{
    if (!std::strcmp(what, "trader")) {
        traderFillOff = true;
        log("CONTAINERSYNCOFF ok=1 trader_fill=0");
        return;
    }
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
        if (!Counted(container, i)) continue;
        Entity item = container.Inventory.GetTemplateItem(i);
        if (wanted && std::strcmp(item.GetName().GetText(), wanted) != 0) continue;
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

namespace g3t::containers {
namespace {
std::string InvStateOf(Entity& entity)
{
    std::string sets;
    Entity const all[5] = {entity.Inventory.GetTreasureSet1(), entity.Inventory.GetTreasureSet2(), entity.Inventory.GetTreasureSet3(),
                           entity.Inventory.GetTreasureSet4(), entity.Inventory.GetTreasureSet5()};
    for (Entity const& set : all)
        if (set.GetInstance()) sets += (sets.empty() ? "" : ",") + std::string(set.GetName().GetText());
    char flags[80];
    sprintf_s(flags, " gen_plunder=%d gen_trade=%d generated_stacks=%d", entity.Inventory.IsGeneratedPlunder() ? 1 : 0,
              entity.Inventory.IsGeneratedTrade() ? 1 : 0, entity.Inventory.GetGeneratedStackCount());
    const int locked = entity.GetInstance()->GetPropertySet(eEPropertySetType_Lock) ? (entity.Lock.IsLocked() ? 1 : 0) : -1;
    return std::string(" name=") + entity.GetName().GetText() + flags + " locked=" + std::to_string(locked) + " sets=" + (sets.empty() ? "-" : sets) +
           " items=" + Describe(Read(entity));
}
}

// QA: whether the game has rolled an inventory from its treasure sets yet (plunder or trade), and what it holds.
void InvState(char const* argument, LogFn log)
{
    char const* unused = nullptr;
    Entity entity = Target(argument, &unused);
    if (!entity.GetInstance() || !HasInventory(entity.GetInstance())) { log("REFUSED invstate: no inventory"); return; }
    const std::string line = "INVSTATE guid=" + std::string(argument, 36) + InvStateOf(entity);
    log(line.c_str());
}

// QA: chests near the hero (an inventory, no NPC, no item), nearest first, with their roll state.
void Chests(LogFn log)
{
    eCEntity* hero = gCSession::GetInstance().GetHero();
    if (!hero) { log("CHESTS end count=0"); return; }
    const bCVector& h = hero->GetWorldPosition();
    const float at[3] = {h.GetX(), h.GetY(), h.GetZ()};
    std::multimap<float, std::string> found;
    const bTObjArray<Entity> all = Entity::GetEntities();
    for (int i = 0; i < all.GetCount(); ++i) {
        Entity entity = all.GetAt(i);
        eCEntity* instance = entity.GetInstance();
        if (!HasInventory(instance) || entity.IsItem() || instance == hero || instance->GetPropertySet(eEPropertySetType_NPC)) continue;
        const float distance = DistanceTo(instance, at);
        if (distance > InterestRadius) continue;
        char guid[48];
        const eCEntityProxy proxy(instance);
        _GUID raw;
        std::memcpy(&raw, &proxy.GetEntityID().GetCore(), 16);
        sprintf_s(guid, "%08lX-%04X-%04X-%02X%02X-%02X%02X%02X%02X%02X%02X", raw.Data1, raw.Data2, raw.Data3, raw.Data4[0], raw.Data4[1],
                  raw.Data4[2], raw.Data4[3], raw.Data4[4], raw.Data4[5], raw.Data4[6], raw.Data4[7]);
        char dist[32];
        sprintf_s(dist, " distance_cm=%d", static_cast<int>(distance));
        found.emplace(distance, std::string("CHEST guid=") + guid + dist + InvStateOf(entity));
    }
    for (auto const& [distance, line] : found) log(line.c_str());
    const std::string end = "CHESTS end count=" + std::to_string(found.size());
    log(end.c_str());
}
}
