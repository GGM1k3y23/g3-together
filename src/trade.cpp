// SPDX-License-Identifier: GPL-3.0-or-later
#include "trade.h"

#include "config.h"
#include "hud.h"
#include "i18n.h"
#include "log.h"
#include "net.h"
#include "offer.h"
#include "partner.h"
#include "remote.h"

#include <Game.h>
#include <Script.h>
#include <Windows.h>
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <string>
#include <unordered_map>
#include <vector>

namespace g3t::trade {
namespace {
using Content = std::map<std::string, int>;  // stack key (StackKey: template ID and quality) -> amount
std::unordered_map<std::string, std::string> names;  // stack key -> item name, for logs and QA
char const* const Gold = "It_Gold";

struct Wares {
    bool known = false;
    uint32_t serial = 0;
    int32_t gold = 0;
    Content items;
};
Wares received[net::MaxPlayers];  // what each other player can trade, as it last sent it
int32_t filledGold[net::MaxPlayers] = {};  // the gold the last Fill put on each player's puppet (RestoreGold)

// This machine's own goods as last sent.
Content sentItems;
int32_t sentGold = -1;
uint32_t serial = 0;
DWORD lastScan = 0, lastSent = 0;

// The trade with a puppet that is open here: its player and what the puppet was filled with (or holds since the last swap).
int partner = 0;
Content expected;
int32_t expectedGold = 0;
DWORD idleSince = 0;

unsigned goodsSent = 0, goodsReceived = 0, fills = 0, swaps = 0;
// The swaps of the trade window open now (owner 23.10.: one offer per window): what the buyer got (+) and gave (-).
Content windowDelta;
int32_t windowGold = 0;
bool windowOpen = false;
bool holdBack = true;  // QA negative control: tradeholdoff (the buyer keeps what it got before the answer)
bool keepQuality = true;  // QA negative control: tradequalityoff (what the buyer gets and gives arrives at quality 0, as before 24.10.)
// Offers sent when a window closed, waiting for the seller's answer. Meanwhile the buyer has neither what it gets (held
// back) nor what it gives (it went to the puppet): nothing can be used before the answer (C trade-screen-c.md 5.).
struct Pending {
    uint32_t serial;
    int seller;
    Content delta;
    int32_t gold;
};
std::vector<Pending> pending;
unsigned offersSent = 0, offersAccepted = 0, offersDeclined = 0, undone = 0, notSent = 0;
// Free prices (owner 27.10., A): in the window the buyer sets the offer's gold with price_up_key / price_down_key (default
// Num+ / Num-, 10 gold a press, 100 with Shift), starting from what the screen moved at the items' values; the seller sees
// that gold in the offer and accepts or declines. Up is a higher price: more to pay when the buyer buys (the screen moved
// gold to the seller), more to get when it sells.
int32_t priceAdjust = 0;  // gold the buyer gives on top of the screen's (+), or gets on top of it (-)
bool freePrices = true;   // QA negative control: tradeadjustoff (the offer carries the screen's gold, as before 27.10.)
bool upWasDown = false, downWasDown = false, hintShown = false;
DWORD priceShowAt = 0;    // the price is shown once the presses settle (one message a press filled the message list)
unsigned priceChanges = 0, priceClamped = 0;
int32_t lastOfferGold = 0, lastScreenGold = 0;
// Prices between players (owner 23.10.: the same price both ways). The trade screen asks every price through the script
// OnPlayerGetGoldCost (self: the other side, other: the owner of the stack, args: its index; C trade-screen-c.md 3.); a
// trader buys at 30 % of an item's value and sells at 100 %. With a puppet on either side, both ways are the item's value.
gFScript originalCost = nullptr;
bool costHooked = false;
bool equalPrices = true;  // QA negative control: tradepricesoff
DWORD lastCostHookTry = 0;
unsigned costCalls = 0, costEqual = 0, costNoEntity = 0, costNoPuppet = 0;

GEInt GE_STDCALL GoldCost(gCScriptProcessingUnit* spu, GELPVoid self, GELPVoid other, GEInt args)
{
    ++costCalls;
    // As the SDK's INIT_SCRIPT_EXT: the wrappers when given, else the processing unit's self and other (the screen's calls
    // came without wrappers: every call was "no entity", 23.10.).
    Entity counterpart, owner;
    if (self) counterpart = *static_cast<Entity*>(self);
    else if (spu) counterpart.AttachTo(spu->GetSelfEntity());
    if (other) owner = *static_cast<Entity*>(other);
    else if (spu) owner.AttachTo(spu->GetOtherEntity());
    if (!counterpart.GetInstance() || !owner.GetInstance()) ++costNoEntity;
    else if (!remote::PuppetOwner(counterpart.GetInstance()) && !remote::PuppetOwner(owner.GetInstance())) ++costNoPuppet;
    else if (equalPrices && args >= 0 && args < owner.Inventory.GetStackCount()) {
        Entity item = owner.Inventory.GetTemplateItem(args);
        if (item.GetInstance()) {
            ++costEqual;
            const GEInt value = item.Item.GetGoldValue();
            return value > 1 ? value : 1;
        }
    }
    return originalCost(spu, self, other, args);
}

void HookCost()
{
    if (costHooked || GetTickCount() - lastCostHookTry < 1000) return;
    lastCostHookTry = GetTickCount();
    gSScript* cost = nullptr;
    if (GetScriptAdminExt().GetScripts().GetAt(bCString("OnPlayerGetGoldCost"), cost) && cost && cost->m_funcScript) {
        originalCost = cost->m_funcScript;
        cost->m_funcScript = &GoldCost;
        costHooked = true;
        Log("trade: price hook installed (OnPlayerGetGoldCost)");
    }
}

// A dialog with a puppet ran: when it is over, the hero must be out of its talk task again.
bool puppetDialog = false;
DWORD dialogOverSince = 0;
unsigned heroReleases = 0;
bool fillOff = false;  // QA negative control: tradefilloff (the puppet keeps what its template carries)

// Goods are told apart by template and quality (gEItemQuality flags: sharp, blessed, forged ...; C's list 23.10.: traded
// items arrived at quality 0): the key is the template ID (16 bytes) and the quality (2 bytes).
std::string KeyOf(void const* id, uint16_t quality)
{
    std::string key(static_cast<char const*>(id), 16);
    key.append(reinterpret_cast<char const*>(&quality), 2);
    return key;
}

std::string StackKey(Entity& entity, int stack)
{
    const Template item = entity.Inventory.GetTemplate(stack);
    bCPropertyID const& id = item;
    return KeyOf(&id.GetCore(), static_cast<uint16_t>(entity.Inventory.GetQuality(stack) & 1023));
}

Template TemplateOf(std::string const& key)
{
    _GUID raw;
    std::memcpy(&raw, key.data(), 16);
    return Template(bCPropertyID(bCGuid(raw)));
}

// The quality as sent and received (QA reads it so; the negative control drops it only where items are made or offered).
uint16_t RawQuality(std::string const& key)
{
    uint16_t quality = 0;
    if (key.size() >= 18) std::memcpy(&quality, key.data() + 16, 2);
    return quality;
}

uint16_t QualityOf(std::string const& key) { return keepQuality ? RawQuality(key) : 0; }

char const* NameOf(std::string const& key)
{
    auto it = names.find(key);
    return it == names.end() ? "?" : it->second.c_str();
}

// A stack the player can trade: not equipped or held, no mission item (owner 23.10.: quest items stay), no skill (perk items
// the hero carries, learned or not: A 23.10.), not the bare hand. Gold goes apart.
bool Tradable(Entity& self, int stack, bool& gold)
{
    gold = false;
    if (self.Inventory.GetSlotFromStack(stack) != gESlot_None) return false;
    Entity item = self.Inventory.GetTemplateItem(stack);
    if (!item.GetInstance()) return false;
    char const* name = item.GetName().GetText();
    if (!std::strcmp(name, "Fist")) return false;
    if (!std::strcmp(name, Gold)) { gold = true; return false; }
    if (item.Item.IsMissionItem()) return false;
    return item.Item.GetCategory() != gEItemCategory_Skill;
}

void Read(Entity& self, Content& items, int32_t& gold)
{
    items.clear();
    gold = 0;
    for (int i = 0; i < self.Inventory.GetStackCount(); ++i) {
        bool isGold = false;
        const bool tradable = Tradable(self, i, isGold);
        if (isGold) gold += self.Inventory.GetAmount(i);
        if (!tradable) continue;
        const std::string key = StackKey(self, i);
        names[key] = self.Inventory.GetTemplateItem(i).GetName().GetText();
        items[key] += self.Inventory.GetAmount(i);
    }
}

// Create goods on a puppet as merchandise: the trade screen lists only stacks of that type for a partner who is not the
// player (C trade-screen-c.md: Game+0xa1c80; PSInventory::CreateItems makes Normal stacks, so the seller looked empty).
void CreateMerchandise(Entity& puppet, eCTemplateEntityProxy& item, uint16_t quality, int amount)
{
    auto* inventory = static_cast<gCInventory_PS*>(puppet.GetInstance()->GetPropertySet(eEPropertySetType_Inventory));
    eCTemplateEntity* entity = item.GetEntity();
    if (inventory && entity && amount > 0) inventory->CreateItems(entity, quality, static_cast<GEU32>(amount), gEStackType_Merchandise);
}

// The puppet's unequipped stacks become the goods (its equipped armor, helmet and held weapons stay).
void Fill(Entity& puppet, Wares const& goods)
{
    for (int i = puppet.Inventory.GetStackCount() - 1; i >= 0; --i)
        if (puppet.Inventory.GetSlotFromStack(i) == gESlot_None) puppet.Inventory.DeleteStack(i);
    for (auto const& [key, amount] : goods.items) {
        _GUID raw;
        std::memcpy(&raw, key.data(), 16);
        eCTemplateEntityProxy item;
        item.SetEntity(bCPropertyID(bCGuid(raw)));
        CreateMerchandise(puppet, item, QualityOf(key), amount);
    }
    eCTemplateEntityProxy gold;
    gold.SetEntity(bCString(Gold));
    CreateMerchandise(puppet, gold, 0, goods.gold);
    if (const int id = remote::PuppetOwner(puppet.GetInstance())) filledGold[id] = goods.gold;
    ++fills;
}

void SendOwn(Content const& items, int32_t gold)
{
    std::vector<net::GoodsEntry> entries;
    for (auto const& [key, amount] : items) {
        if (entries.size() >= static_cast<size_t>(net::MaxGoodsEntries)) break;  // the rest is not offered
        net::GoodsEntry entry{};
        std::memcpy(entry.id, key.data(), 16);
        entry.quality = QualityOf(key);
        strncpy_s(entry.name, NameOf(key), _TRUNCATE);
        entry.amount = amount;
        entries.push_back(entry);
    }
    if (!net::SendGoods(serial + 1, gold, entries.data(), static_cast<int>(entries.size()))) return;
    ++serial;
    ++goodsSent;
    sentItems = items;
    sentGold = gold;
    lastSent = GetTickCount();
}

void ReadPuppet(Entity& puppet, Content& items, int32_t& gold);

void OnGoods(uint8_t player, uint32_t serialIn, int32_t gold, net::GoodsEntry const* entries, int count)
{
    Wares& goods = received[player];
    goods.known = true;
    goods.serial = serialIn;
    goods.gold = gold;
    goods.items.clear();
    for (int i = 0; i < count; ++i) {
        const std::string key = KeyOf(entries[i].id, entries[i].quality);
        names.emplace(key, entries[i].name);
        goods.items[key] += entries[i].amount;
    }
    ++goodsReceived;
    // New goods of the player traded with: its puppet shows them, unless a window is open or has swaps not yet offered.
    if (player == partner && !fillOff && !windowOpen && windowDelta.empty() && windowGold == 0)
        if (eCEntity* body = remote::PuppetOf(player)) {
            Entity puppet(body);
            Fill(puppet, goods);
            ReadPuppet(puppet, expected, expectedGold);
        }
}

// What a puppet offers now: its unequipped stacks, gold apart.
void ReadPuppet(Entity& puppet, Content& items, int32_t& gold)
{
    items.clear();
    gold = 0;
    for (int i = 0; i < puppet.Inventory.GetStackCount(); ++i) {
        if (puppet.Inventory.GetSlotFromStack(i) != gESlot_None) continue;
        Entity item = puppet.Inventory.GetTemplateItem(i);
        if (!item.GetInstance()) continue;
        if (!std::strcmp(item.GetName().GetText(), Gold)) { gold += puppet.Inventory.GetAmount(i); continue; }
        const std::string key = StackKey(puppet, i);
        names[key] = item.GetName().GetText();
        items[key] += puppet.Inventory.GetAmount(i);
    }
}

// Take `amount` of the item `key` (of its quality) from the hero, unequipped stacks first (an undo may meet one the player
// equipped since).
int Take(Entity& self, std::string const& key, int amount)
{
    int taken = 0;
    for (int pass = 0; pass < 2 && taken < amount; ++pass)
        for (int i = self.Inventory.GetStackCount() - 1; i >= 0 && taken < amount; --i) {
            if ((self.Inventory.GetSlotFromStack(i) == gESlot_None) != (pass == 0) || StackKey(self, i) != key) continue;
            const int here = self.Inventory.GetAmount(i);
            const int take = here < amount - taken ? here : amount - taken;
            if (take > 0 && (take == here ? self.Inventory.DeleteStack(i) : self.Inventory.DeleteItems(i, take))) taken += take;
        }
    return taken;
}

std::string Describe(Content const& items, int32_t gold);

int32_t HeroGold(Entity& self)
{
    int32_t gold = 0;
    for (int i = 0; i < self.Inventory.GetStackCount(); ++i)
        if (Entity item = self.Inventory.GetTemplateItem(i); item.GetInstance() && !std::strcmp(item.GetName().GetText(), Gold))
            gold += self.Inventory.GetAmount(i);
    return gold;
}

// The offer's gold within what both have: the buyer pays at most the gold it had before the window (what the screen moved,
// `moved`, is gone from the hero already), the seller at most the gold it last sent. A price goes down to nothing, not
// past it: a buyer who buys (or swaps) gets no gold on top, one who sells pays none. All limits hold `moved` itself.
int32_t ClampGold(int32_t gold, int32_t moved, int seller)
{
    int32_t low = moved, high = std::max(received[seller].gold, moved);
    if (eCDynamicEntity* hero = gCSession::GetInstance().GetHero()) {
        Entity self(hero);
        low = moved - HeroGold(self);
    }
    if (moved > 0) low = std::max(low, 0);
    else high = std::min(high, 0);
    return gold < low ? low : gold > high ? high : gold;
}

std::string GoldKey()
{
    const Template item(Gold);
    bCPropertyID const& id = item;
    return KeyOf(&id.GetCore(), 0);
}

// The buyer's side of an answered offer: accepted, it gets what it was held back (received); declined or not sent, what
// it gave comes back.
void Give(Content const& delta, int32_t gold, bool received)
{
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    if (!hero) return;
    Entity self(hero);
    for (auto const& [key, amount] : delta)
        if (received ? amount > 0 : amount < 0) self.Inventory.CreateItems(TemplateOf(key), QualityOf(key), received ? amount : -amount);
    if (received ? gold > 0 : gold < 0) self.Inventory.CreateItems(Template(Gold), 0, received ? gold : -gold);
    if (!received) ++undone;
}

void OnResult(uint32_t serialIn, bool accepted)
{
    for (auto it = pending.begin(); it != pending.end(); ++it) {
        if (it->serial != serialIn) continue;
        if (accepted) {
            ++offersAccepted;
            Logf("trade: offer %u to player %d accepted; the buyer gets %s", serialIn, it->seller, Describe(it->delta, it->gold).c_str());
        } else {
            ++offersDeclined;
            Logf("trade: offer %u to player %d declined; what the buyer gave comes back", serialIn, it->seller);
        }
        Give(it->delta, it->gold, accepted);
        pending.erase(it);
        return;
    }
}

// A swap: the puppet holds something else than it was filled with. It joins the window's swaps (the buyer got +).
void NoteSwap(eCEntity* body)
{
    Entity puppet(body);
    Content now_items;
    int32_t nowGold = 0;
    ReadPuppet(puppet, now_items, nowGold);
    if (now_items == expected && nowGold == expectedGold) return;
    for (auto const& [key, amount] : expected) windowDelta[key] += amount;
    for (auto const& [key, amount] : now_items) windowDelta[key] -= amount;
    for (auto it = windowDelta.begin(); it != windowDelta.end();) it = it->second == 0 ? windowDelta.erase(it) : ++it;
    windowGold += expectedGold - nowGold;
    expected = now_items;
    expectedGold = nowGold;
    ++swaps;
    Logf("trade: swap with player %d; this window so far: buyer gets %s", partner, Describe(windowDelta, windowGold).c_str());
}

std::wstring KeyLabel(char const* setting, int fallback)
{
    const int code = partner::KeyFromName(setting, fallback);
    if (code == VK_ADD) return L"Num+";
    if (code == VK_SUBTRACT) return L"Num-";
    char name[16];
    partner::KeyName(code, name, sizeof(name));
    return std::wstring(name, name + std::strlen(name));
}

std::wstring PlayerName(int id)
{
    wchar_t out[96] = {};
    MultiByteToWideChar(CP_UTF8, 0, net::PeerName(static_cast<uint8_t>(id)), -1, out, 95);
    return out;
}

void ShowPrice()
{
    const int32_t gold = windowGold - priceAdjust;
    wchar_t text[200];
    _snwprintf_s(text, _TRUNCATE, i18n::Text(gold > 0 ? i18n::TradePriceGet : i18n::TradePricePay), gold > 0 ? gold : -gold,
                 windowGold < 0 ? -windowGold : windowGold);
    hud::Message(hud::Info, text);
}

// One press of a price key (or QA's tradeadjust): the price `step` gold higher (negative: lower), within ClampGold.
// False: no window with goods to price.
bool ChangePrice(int step)
{
    if (!partner || !windowOpen) return false;
    if (eCEntity* body = remote::PuppetOf(partner)) NoteSwap(body);
    if (windowDelta.empty()) {
        hud::Message(hud::Info, i18n::Text(i18n::TradePriceNoGoods));
        return false;
    }
    const int32_t wanted = windowGold - (priceAdjust + (windowGold > 0 ? -step : step));
    const int32_t gold = ClampGold(wanted, windowGold, partner);
    priceAdjust = windowGold - gold;
    ++priceChanges;
    if (gold != wanted) {
        ++priceClamped;
        hud::Message(hud::Warning, i18n::Text(i18n::TradePriceLimit));
    }
    priceShowAt = GetTickCount() + 400;
    return true;
}

// In a trade window with a player: the hint once a trade, the price keys, the price once the presses settle.
void PriceWindow()
{
    Config const& config = GetConfig();
    if (!hintShown) {
        hintShown = true;
        wchar_t text[300];
        _snwprintf_s(text, _TRUNCATE, i18n::Text(i18n::TradePriceHint), PlayerName(partner).c_str(), KeyLabel(config.priceUpKey, VK_ADD).c_str(),
                     KeyLabel(config.priceDownKey, VK_SUBTRACT).c_str());
        hud::Message(hud::Info, text);
    }
    // Not with Ctrl, as the offer keys.
    const bool focused = partner::Focused() && !(GetAsyncKeyState(VK_CONTROL) & 0x8000);
    const bool up = focused && (GetAsyncKeyState(partner::KeyFromName(config.priceUpKey, VK_ADD)) & 0x8000) != 0;
    const bool down = focused && (GetAsyncKeyState(partner::KeyFromName(config.priceDownKey, VK_SUBTRACT)) & 0x8000) != 0;
    const int step = (GetAsyncKeyState(VK_SHIFT) & 0x8000) ? 100 : 10;
    if (up && !upWasDown) ChangePrice(step);
    else if (down && !downWasDown) ChangePrice(-step);
    upWasDown = up;
    downWasDown = down;
    if (priceShowAt && static_cast<int32_t>(GetTickCount() - priceShowAt) >= 0) {
        priceShowAt = 0;
        ShowPrice();
    }
}

// The trade window closed: its swaps go to the seller as one offer. What the buyer got is taken back from it until the
// answer; the puppet shows the seller's goods again.
void CloseWindow(int seller)
{
    if (eCEntity* body = remote::PuppetOf(seller)) NoteSwap(body);  // a swap right before closing
    const int32_t adjust = freePrices && !windowDelta.empty() ? priceAdjust : 0;  // a price needs goods
    priceAdjust = 0;
    priceShowAt = 0;
    if (windowDelta.empty() && windowGold == 0) return;
    Content delta;
    delta.swap(windowDelta);
    const int32_t moved = windowGold;  // what the screen moved at the items' values (+: the buyer got it)
    int32_t gold = adjust ? ClampGold(moved - adjust, moved, seller) : moved;
    windowGold = 0;
    if (eCDynamicEntity* hero = gCSession::GetInstance().GetHero()) {
        Entity self(hero);
        if (holdBack)
            for (auto& [key, amount] : delta)
                if (amount > 0) {
                    const int took = Take(self, key, amount);
                    if (took < amount) Logf("trade: only %d of %d %s could be held back", took, amount, NameOf(key));
                    amount = took;
                }
        // Until the answer the buyer has paid what it gives and holds back what it gets (gold it gets: nothing yet). The
        // screen changed its gold by `moved`; the rest of the way to that, the free price's difference included, is here.
        const int32_t target = holdBack ? std::min(gold, 0) : gold;
        const int32_t change = target - moved;
        if (change > 0) {
            self.Inventory.CreateItems(Template(Gold), 0, change);
        } else if (change < 0) {
            const int32_t missing = -change - Take(self, GoldKey(), -change);
            if (missing > 0) {
                Logf("trade: %d gold could not be taken from the buyer; the offer's gold changes by that", missing);
                gold += gold > 0 ? -missing : missing;
            }
        }
    }
    lastScreenGold = moved;
    lastOfferGold = gold;
    for (auto it = delta.begin(); it != delta.end();) it = it->second == 0 ? delta.erase(it) : ++it;
    if (eCEntity* body = remote::PuppetOf(seller)) {
        Entity puppet(body);
        Fill(puppet, received[seller]);
        ReadPuppet(puppet, expected, expectedGold);
    }
    if (delta.empty() && gold == 0) return;
    std::vector<offer::Delta> deltas;
    for (auto const& [key, amount] : delta) {
        offer::Delta d{{}, amount, QualityOf(key)};
        std::memcpy(d.item, key.data(), 16);
        deltas.push_back(d);
    }
    const uint32_t sent = deltas.size() <= static_cast<size_t>(net::MaxOfferEntries)
                              ? offer::Send(static_cast<uint8_t>(seller), deltas.data(), static_cast<int>(deltas.size()), gold) : 0;
    if (!sent) {
        ++notSent;
        Logf("trade: the window's swaps with player %d (%s) could not go out as an offer; what the buyer gave comes back", seller,
             Describe(delta, gold).c_str());
        Give(delta, gold, false);
        return;
    }
    ++offersSent;
    pending.push_back(Pending{sent, seller, delta, gold});
    Logf("trade: window with player %d closed; offer %u: buyer gets %s (held back until the answer; screen gold %d, free price %+d)", seller, sent,
         Describe(delta, gold).c_str(), moved, moved - gold);
}

std::string Describe(Content const& items, int32_t gold)
{
    std::string out;
    for (auto const& [key, amount] : items) {
        if (out.size() > 600) { out += ",..."; break; }
        const uint16_t quality = QualityOf(key);
        out += (out.empty() ? "" : ",") + std::string(NameOf(key)) + (quality ? "~q" + std::to_string(quality) : "") + ":" + std::to_string(amount);
    }
    return (out.empty() ? std::string("-") : out) + " gold=" + std::to_string(gold);
}
}

void Start()
{
    net::SetGoodsHandler(&OnGoods);
    offer::SetResultHandler(&OnResult);
}

void Tick()
{
    // A dialog with a puppet fills it at once: the game's trade option opens the screen without PlayerOpenTrade
    // (stageTradeGoods 23.10.: the screen showed an empty partner), so the goods must be there before it is chosen.
    if (gCSession::GetInstance().IsGameRunning()) {
        HookCost();
        gCInfoManager_PS* dialog = gCInfoManager_PS::GetInstance();
        const bool running = dialog && dialog->AccessIsRunning();
        eCEntity* npc = running ? reinterpret_cast<eCEntity*>(dialog->GetDialogPartnerNPC()) : nullptr;
        if (npc && remote::PuppetOwner(npc)) {
            // Every new dialog fills the puppet again: a second trade with the same player nearby showed the goods of the
            // first (stageTradeGoods twice in a session, 23.10.).
            if (!puppetDialog) OnOpen(npc);
            puppetDialog = true;
            dialogOverSince = 0;
        }
        // The puppet runs no AI, so its side of the dialog's end never comes: the hero stayed in ZS_Talk and could talk to
        // nobody afterwards (stageTrade after stageTradeGoods, 23.10.). Once the dialog is over, the hero goes back to its
        // routine, as the game's own end of a dialog does.
        if (puppetDialog && !running) {
            const DWORD at = GetTickCount();
            if (!dialogOverSince) dialogOverSince = at;
            if (at - dialogOverSince > 500) {
                puppetDialog = false;
                dialogOverSince = 0;
                if (eCDynamicEntity* self = gCSession::GetInstance().GetHero()) {
                    Entity hero(self);
                    if (!std::strcmp(hero.Routine.GetCurrentTask().GetText(), "ZS_Talk")) {
                        hero.Routine.FullStop();
                        hero.Routine.ContinueRoutine();
                        ++heroReleases;
                        Logf("trade: the dialog with a puppet is over; the hero leaves ZS_Talk (now %s)", hero.Routine.GetCurrentTask().GetText());
                    }
                }
            }
        }
        // The trade window: PlayerOpenTrade puts the hero into PS_HUD (C); when it leaves it, the window closed.
        if (partner) {
            if (eCDynamicEntity* self = gCSession::GetInstance().GetHero()) {
                Entity hero(self);
                const bool inHud = !std::strncmp(hero.Routine.GetCurrentTask().GetText(), "PS_HUD", 6) ||
                                   !std::strncmp(hero.Routine.GetCurrentState().GetText(), "PS_HUD", 6);
                if (inHud) {
                    windowOpen = true;
                    PriceWindow();
                } else if (windowOpen) {
                    windowOpen = false;
                    CloseWindow(partner);
                }
            }
        }
    }
    const DWORD now = GetTickCount();
    if (now - lastScan < 1000) return;
    lastScan = now;
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    if (!hero || !gCSession::GetInstance().IsGameRunning() || !net::Connected()) return;
    // Our goods when they changed, and every 20 s anyway (a player who joined since gets them too).
    Entity self(hero);
    Content items;
    int32_t gold = 0;
    Read(self, items, gold);
    if (items != sentItems || gold != sentGold || now - lastSent > 20000) SendOwn(items, gold);

    if (!partner) return;
    eCEntity* body = remote::PuppetOf(partner);
    gCInfoManager_PS* infos = gCInfoManager_PS::GetInstance();
    // The trade ends when the player walked away from the puppet (or it is gone), or 2 min after its dialog: opened through
    // PlayerOpenTrade there is no dialog at all, so a dialog's end alone emptied the puppet under an open screen (23.10.).
    const bCVector& h = hero->GetWorldPosition();
    const bool dialog = infos && infos->AccessIsRunning();
    if (dialog) idleSince = 0;
    else if (!idleSince) idleSince = now;
    if (!body || (body->GetWorldPosition() - h).GetMagnitude() > 800.0f || (idleSince && now - idleSince > 120000)) {
        CloseWindow(partner);
        windowOpen = false;
        Logf("trade: trade with player %d ended", partner);
        if (body) {
            Entity puppet(body);
            Fill(puppet, Wares{});
        }
        partner = 0;
        return;
    }
    NoteSwap(body);
}

bool OnOpen(eCEntity* body)
{
    const int id = remote::PuppetOwner(body);
    if (!id) return false;
    Entity puppet(body);
    Wares const& goods = received[id];
    if (!fillOff) Fill(puppet, goods);
    partner = id;
    hintShown = false;
    // What the puppet really holds now, not what was asked for: a create that failed must not look like a purchase (C).
    ReadPuppet(puppet, expected, expectedGold);
    idleSince = 0;
    Logf("trade: trade with player %d opened; its goods (%s, serial %u): %s", id, goods.known ? "known" : "not known yet", goods.serial,
         Describe(goods.items, goods.gold).c_str());
    return true;
}

void Report(LogFn log)
{
    char line[600];
    Format(line, "TRADESYNC goods_sent=%u serial=%u own_items=%zu own_gold=%d goods_received=%u partner=%d fills=%u swaps=%u offers=%u accepted=%u"
                    " declined=%u undone=%u not_sent=%u pending=%zu hero_releases=%u price_hooked=%d price_calls=%u price_equal=%u price_adjust=%d"
                    " price_changes=%u price_clamped=%u last_screen_gold=%d last_offer_gold=%d", goodsSent, serial,
              sentItems.size(), sentGold, goodsReceived, partner, fills, swaps, offersSent, offersAccepted, offersDeclined, undone, notSent,
              pending.size(), heroReleases, costHooked ? 1 : 0, costCalls, costEqual, priceAdjust, priceChanges, priceClamped, lastScreenGold,
              lastOfferGold);
    log(line);
}

// "<who>" or "<who>_<Item>[-q<quality>]", who: 0 (this machine), a player id, or puppet<id>; with an item only its amount
// (of that quality, with -q).
void GoodsReport(char const* argument, LogFn log)
{
    char line[900], who[32] = {}, onlyName[64] = {};
    char const* sep = std::strchr(argument, '_');
    strncpy_s(who, argument, sep ? std::min<size_t>(sep - argument, sizeof(who) - 1) : _TRUNCATE);
    char const* only = sep ? sep + 1 : nullptr;
    int onlyQuality = -1;
    if (only) {
        strncpy_s(onlyName, only, _TRUNCATE);
        if (char* q = std::strstr(onlyName, "-q")) {
            onlyQuality = std::atoi(q + 2);
            *q = '\0';
        }
        only = onlyName;
    }
    Content items;
    int32_t gold = 0;
    uint32_t serialOut = 0;
    bool known = true;
    int types[4] = {};
    if (!std::strncmp(who, "puppet", 6)) {  // what player <id>'s puppet offers here now
        eCEntity* body = remote::PuppetOf(std::atoi(who + 6));
        known = body != nullptr;
        if (body) {
            Entity puppet(body);
            ReadPuppet(puppet, items, gold);
            // Stack types of its unequipped stacks: the trade screen lists a partner's merchandise (2) only.
            if (auto* inventory = static_cast<gCInventory_PS*>(body->GetPropertySet(eEPropertySetType_Inventory)))
                for (int i = 0; i < inventory->GetStackCount(); ++i)
                    if (gCInventoryStack* stack = inventory->GetStack(i); stack && puppet.Inventory.GetSlotFromStack(i) == gESlot_None)
                        ++types[static_cast<int>(static_cast<gEStackType const&>(stack->AccessType())) & 3];
        }
    } else {
        const int id = std::atoi(who);
        if (id == 0) items = sentItems, gold = sentGold, serialOut = serial;
        else if (id > 0 && id < net::MaxPlayers && received[id].known) items = received[id].items, gold = received[id].gold, serialOut = received[id].serial;
        else known = false;
    }
    if (!known) {
        Format(line, "PLAYERGOODS %s unknown", who);
    } else if (only) {
        int amount = 0;
        for (auto const& [key, n] : items)
            if (!std::strcmp(NameOf(key), only) && (onlyQuality < 0 || RawQuality(key) == onlyQuality)) amount += n;
        Format(line, "PLAYERGOODS %s serial=%u item=%s:%d gold=%d stacks_normal=%d stacks_treasure=%d stacks_merchandise=%d", who, serialOut, only,
                  amount, gold, types[0], types[1], types[2]);
    } else {
        Format(line, "PLAYERGOODS %s serial=%u %s", who, serialOut, Describe(items, gold).c_str());
    }
    log(line);
}

void OnWorldChange()
{
    if (!pending.empty()) Logf("trade: %zu open offers forgotten (world change; the hero is the new world's)", pending.size());
    pending.clear();
    windowDelta.clear();
    windowGold = 0;
    windowOpen = false;
    priceAdjust = 0;
    priceShowAt = 0;
    partner = 0;
    puppetDialog = false;
    dialogOverSince = 0;
    expected.clear();
    expectedGold = 0;
    idleSince = 0;
    for (auto& gold : filledGold) gold = 0;
}

// While its player lay knocked out, a puppet carried that player's gold for a robber (ransack.cpp); then it gets the gold
// back that trade put there (merchandise, as Fill makes it).
void RestoreGold(eCEntity* body, int player)
{
    if (!body || player <= 0 || player >= net::MaxPlayers) return;
    Entity puppet(body);
    for (int i = puppet.Inventory.GetStackCount() - 1; i >= 0; --i) {
        Entity item = puppet.Inventory.GetTemplateItem(i);
        if (item.GetInstance() && !std::strcmp(item.GetName().GetText(), Gold) && puppet.Inventory.GetSlotFromStack(i) == gESlot_None)
            puppet.Inventory.DeleteStack(i);
    }
    eCTemplateEntityProxy gold;
    gold.SetEntity(bCString(Gold));
    CreateMerchandise(puppet, gold, 0, filledGold[player]);
}

void FillOff(LogFn log)
{
    fillOff = true;
    log("TRADEFILLOFF ok");
}

// QA: "<partner>_<Item>": what the trade screen would ask for one piece of the item, bought from the partner (its stack) and
// sold to it (the hero's stack), through the game's own price script. partner: puppet<id> or an NPC's name.
void Price(char const* argument, LogFn log)
{
    char line[300], who[64] = {};
    char const* sep = std::strchr(argument, '_');
    if (!sep) { log("REFUSED tradeprice: <partner>_<Item>"); return; }
    strncpy_s(who, argument, std::min<size_t>(sep - argument, sizeof(who) - 1));
    char const* name = sep + 1;
    eCDynamicEntity* heroBody = gCSession::GetInstance().GetHero();
    eCEntity* partnerBody = !std::strncmp(who, "puppet", 6) ? remote::PuppetOf(std::atoi(who + 6)) : Entity::GetEntity(bCString(who)).GetInstance();
    if (!heroBody || !partnerBody) { log("REFUSED tradeprice: no hero or partner"); return; }
    Entity hero(heroBody), other(partnerBody);
    auto stackOf = [name](Entity& e) {
        for (int i = 0; i < e.Inventory.GetStackCount(); ++i)
            if (Entity item = e.Inventory.GetTemplateItem(i); item.GetInstance() && !std::strcmp(item.GetName().GetText(), name)) return i;
        return -1;
    };
    const int theirs = stackOf(other), mine = stackOf(hero);
    const GEInt buy = theirs >= 0 ? GetScriptAdminExt().CallScript(bCString("OnPlayerGetGoldCost"), heroBody, partnerBody, theirs) : -1;
    const GEInt sell = mine >= 0 ? GetScriptAdminExt().CallScript(bCString("OnPlayerGetGoldCost"), partnerBody, heroBody, mine) : -1;
    const GEInt value = theirs >= 0 ? other.Inventory.GetTemplateItem(theirs).Item.GetGoldValue()
                                    : mine >= 0 ? hero.Inventory.GetTemplateItem(mine).Item.GetGoldValue() : -1;
    Format(line, "TRADEPRICE partner=%s item=%s buy=%d sell=%d value=%d hooked=%d calls=%u equal=%u no_entity=%u no_puppet=%u", who, name, buy,
              sell, value, costHooked ? 1 : 0, costCalls, costEqual, costNoEntity, costNoPuppet);
    log(line);
}

// QA: "<gold>" (n50 = -50) what that many presses' worth of the price keys does: the price up (down) by that much.
void AdjustCommand(char const* argument, LogFn log)
{
    char line[300];
    const int step = argument && (*argument == 'n' || *argument == 'N') ? -std::atoi(argument + 1) : std::atoi(argument ? argument : "0");
    const bool ok = ChangePrice(step);
    Format(line, "TRADEADJUST ok=%d step=%d adjust=%d screen_gold=%d offer_gold=%d window=%d partner=%d clamped=%u", ok ? 1 : 0, step,
              priceAdjust, windowGold, windowGold - priceAdjust, windowOpen ? 1 : 0, partner, priceClamped);
    log(line);
}

void AdjustOff(LogFn log)
{
    freePrices = false;
    log("TRADEADJUSTOFF ok");
}

void PricesOff(LogFn log)
{
    equalPrices = false;
    log("TRADEPRICESOFF ok");
}

void HoldOff(LogFn log)
{
    holdBack = false;
    log("TRADEHOLDOFF ok");
}

void QualityOff(LogFn log)
{
    keepQuality = false;
    log("TRADEQUALITYOFF ok");
}

// QA: "<Item>_<quality>" creates one of the item at that quality (gEItemQuality flags, e.g. 32 sharp, 64 blessed, 128 forged).
void GiveQuality(char const* argument, LogFn log)
{
    char line[200], name[64] = {};
    char const* sep = std::strrchr(argument, '_');
    if (!sep) { log("REFUSED givequality: <Item>_<quality>"); return; }
    strncpy_s(name, argument, std::min<size_t>(sep - argument, sizeof(name) - 1));
    const unsigned quality = static_cast<unsigned>(std::atoi(sep + 1));
    eCDynamicEntity* self = gCSession::GetInstance().GetHero();
    const Template item(name);
    if (!self || !item.IsValid() || quality >= 1024) { log("REFUSED givequality: no hero, no such item or quality above 1023"); return; }
    Entity hero(self);
    const GEInt stack = hero.Inventory.CreateItems(item, quality, 1);
    Format(line, "GIVEQUALITY name=%s quality=%u stack=%d got=%u", name, quality, stack, stack >= 0 ? hero.Inventory.GetQuality(stack) : 0u);
    log(line);
}

// QA: "<Item>": how many of the item the hero carries, per quality ("QUALITYCOUNT name=X total=3 q0=2 q32=1").
void QualityCount(char const* name, LogFn log)
{
    char line[400];
    eCDynamicEntity* self = gCSession::GetInstance().GetHero();
    if (!self) { log("REFUSED qualitycount: no hero"); return; }
    Entity hero(self);
    std::map<unsigned, int> byQuality;
    int total = 0;
    for (int i = 0; i < hero.Inventory.GetStackCount(); ++i)
        if (Entity item = hero.Inventory.GetTemplateItem(i); item.GetInstance() && !std::strcmp(item.GetName().GetText(), name)) {
            byQuality[hero.Inventory.GetQuality(i)] += hero.Inventory.GetAmount(i);
            total += hero.Inventory.GetAmount(i);
        }
    int at = Format(line, "QUALITYCOUNT name=%s total=%d", name, total);
    for (auto const& [quality, amount] : byQuality)
        if (at > 0 && at < static_cast<int>(sizeof(line)) - 24) at += Format(line + at, sizeof(line) - at, " q%u=%d", quality, amount);
    log(line);
}

// QA: the hero leaves the trade window's PS_HUD back into its routine, the state change closing the window brings (the bot's
// Escape does not reach the trade window, 23.10.). The window itself may stay drawn; the mod sees the window closed.
void CloseCommand(LogFn log)
{
    char line[200];
    eCDynamicEntity* self = gCSession::GetInstance().GetHero();
    if (!self) { log("REFUSED tradeclose: no hero"); return; }
    Entity hero(self);
    hero.Routine.FullStop();
    hero.Routine.ContinueRoutine();
    Format(line, "TRADECLOSE task=%s state=%s", hero.Routine.GetCurrentTask().GetText(), hero.Routine.GetCurrentState().GetText());
    log(line);
}
}
