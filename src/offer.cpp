// SPDX-License-Identifier: GPL-3.0-or-later
#include "offer.h"

#include "config.h"
#include "hud.h"
#include "i18n.h"
#include "log.h"
#include "net.h"
#include "partner.h"
#include "qa.h"
#include "remote.h"

#include <Game.h>
#include <Script.h>
#include <Windows.h>
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

namespace g3t::offer {
namespace {
constexpr uint32_t DecideWithin = 30000;  // ms the seller has (owner 23.10.)
constexpr uint32_t AnswerWithin = 40000;  // ms the buyer waits for the answer
constexpr uint32_t RemindEvery = 10000;   // the seller's message fades after ~6 s: shown again with the time left
constexpr int MaxAmount = 100000, MaxGold = 10000000;

ResultFn resultHandler = nullptr;
bool checking = true;  // QA negative control: PartialOn

// Seller: the one open offer.
bool pending = false;
net::OfferNote incoming{};
uint32_t receivedAt = 0, remindedAt = 0;
bool acceptWasDown = false, declineWasDown = false;
// Buyer: the offers sent and not answered yet.
struct Sent { uint32_t serial; uint8_t seller; uint32_t at; };
std::vector<Sent> sent;
uint32_t nextSerial = 0;
unsigned offersSent = 0, offersReceived = 0, applied = 0, declined = 0, expired = 0, unavailable = 0, busy = 0, rolledBack = 0;
std::string lastSeller = "-", lastBuyer = "-";

Template Of(uint8_t const id[16])
{
    _GUID raw;
    std::memcpy(&raw, id, 16);
    return Template(bCPropertyID(bCGuid(raw)));
}

// A stack of this item; of this quality too unless AnyQuality (gold, QA counts).
constexpr unsigned AnyQuality = ~0u;
bool Same(Entity& self, int stack, Template const& item, unsigned quality = AnyQuality)
{
    return self.Inventory.GetTemplate(stack) == static_cast<bCPropertyID const&>(item) &&
           (quality == AnyQuality || self.Inventory.GetQuality(stack) == quality);
}

// What another player may get from this hero: not equipped or held, no quest item, no skill (B's goods, the same rule).
bool Tradable(Entity& self, int stack)
{
    if (static_cast<int>(self.Inventory.GetCategory(stack)) == gEItemCategory_Skill) return false;
    if (self.Inventory.GetSlotFromStack(stack) != gESlot_None) return false;
    Entity item = self.Inventory.GetTemplateItem(stack);
    return !(item.GetInstance() && item.Item.IsMissionItem());
}

int TradableAmount(Entity& self, Template const& item, unsigned quality = AnyQuality)
{
    int amount = 0;
    for (int i = 0; i < self.Inventory.GetStackCount(); ++i)
        if (Same(self, i, item, quality) && Tradable(self, i)) amount += self.Inventory.GetAmount(i);
    return amount;
}

Template Gold() { return Template("It_Gold"); }

// One step of applying an offer, to undo it if a later one fails.
struct Step { Template item; unsigned quality; int amount; bool created; };

// Takes `amount` of the item (of that quality) out of the tradable stacks, last stack first; returns how many went.
int Take(Entity& self, Template const& item, unsigned quality, int amount, std::vector<Step>& steps)
{
    int taken = 0;
    for (int i = self.Inventory.GetStackCount() - 1; i >= 0 && taken < amount; --i) {
        if (!Same(self, i, item, quality) || !Tradable(self, i)) continue;
        const int here = self.Inventory.GetAmount(i);
        const int take = std::min(amount - taken, here);
        if (take <= 0) continue;
        const unsigned stackQuality = self.Inventory.GetQuality(i);  // what an undo gives back (any quality for gold)
        if (take == here ? self.Inventory.DeleteStack(i) : self.Inventory.DeleteItems(i, take)) {
            taken += take;
            steps.push_back({item, stackQuality, take, false});
        }
    }
    return taken;
}

void Undo(Entity& self, std::vector<Step>& steps)
{
    std::vector<Step> none;
    for (auto it = steps.rbegin(); it != steps.rend(); ++it) {
        if (it->created) Take(self, it->item, it->quality, it->amount, none);
        else self.Inventory.CreateItems(it->item, it->quality, it->amount);
    }
    steps.clear();
    ++rolledBack;
}

std::wstring Wide(char const* text)
{
    wchar_t out[96] = {};
    MultiByteToWideChar(CP_UTF8, 0, text, -1, out, 95);
    return out;
}

std::wstring PlayerName(uint8_t id) { return Wide(net::PeerName(id)); }

// "2x Heiltrank, 50 Gold" for what goes one way (giving: what goes to the buyer, as the seller sees it).
std::wstring Summary(net::OfferNote const& note, bool toBuyer)
{
    std::wstring out;
    for (int i = 0; i < note.count; ++i) {
        const int amount = toBuyer ? note.entries[i].amount : -note.entries[i].amount;
        if (amount <= 0) continue;
        if (!out.empty()) out += L", ";
        out += std::to_wstring(amount) + L"x " + Wide(note.entries[i].name);
    }
    const int gold = toBuyer ? note.gold : -note.gold;
    if (gold > 0) {
        wchar_t text[48];
        _snwprintf_s(text, _TRUNCATE, i18n::Text(i18n::OfferGold), gold);
        if (!out.empty()) out += L", ";
        out += text;
    }
    if (out.size() > 160) out = out.substr(0, 157) + L"...";
    return out.empty() ? i18n::Text(i18n::OfferNothing) : out;
}

void ShowIncoming()
{
    const uint32_t now = GetTickCount();
    const int left = static_cast<int>((DecideWithin - std::min(DecideWithin, now - receivedAt) + 999) / 1000);
    wchar_t text[600];
    _snwprintf_s(text, _TRUNCATE, i18n::Text(i18n::OfferIncoming), PlayerName(incoming.buyer).c_str(), Summary(incoming, true).c_str(),
                 Summary(incoming, false).c_str(), Wide(GetConfig().offerAcceptKey).c_str(), Wide(GetConfig().offerDeclineKey).c_str(), left);
    hud::Message(hud::Info, text);
    remindedAt = now;
}

void Answer(uint8_t result)
{
    net::OfferResultNote note;
    note.buyer = incoming.buyer;
    note.result = result;
    note.serial = incoming.serial;
    net::SendOfferResult(note);
    pending = false;
}

void SellerMessage(i18n::Message message, wchar_t const* extra = nullptr)
{
    wchar_t text[400];
    _snwprintf_s(text, _TRUNCATE, i18n::Text(message), PlayerName(incoming.buyer).c_str(), extra ? extra : L"");
    hud::Message(message == i18n::OfferDoneSeller ? hud::Good : hud::Warning, text);
}

// Seller: is everything it gives still there? The name of what is missing, or nullptr.
char const* Missing(Entity& self)
{
    for (int i = 0; i < incoming.count; ++i) {
        net::OfferEntry const& entry = incoming.entries[i];
        const Template item = Of(entry.item);
        if (!item.IsValid() || entry.amount < -MaxAmount || entry.amount > MaxAmount) return entry.name;
        if (entry.quality >= 1024) return entry.name;
        if (entry.amount > 0 && TradableAmount(self, item, entry.quality) < entry.amount) return entry.name;
    }
    if (incoming.gold < -MaxGold || incoming.gold > MaxGold) return "gold";
    if (incoming.gold > 0 && TradableAmount(self, Gold()) < incoming.gold) return "gold";
    return nullptr;
}

void Apply(LogFn log)
{
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    char line[300];
    if (!hero) {
        ++unavailable;
        Answer(net::OfferUnavailable);
        log("offer: no hero here; answered not available");
        return;
    }
    Entity self(hero);
    // Checked first, then applied; a step that still fails undoes the ones before (owner 23.10.: whole or not at all).
    if (char const* missing = checking ? Missing(self) : nullptr) {
        ++unavailable;
        Format(line, "offer: %u from player %u not possible: %s is not there any more", incoming.serial, incoming.buyer, missing);
        log(line);
        const std::wstring name = std::strcmp(missing, "gold") ? Wide(missing) : std::wstring(L"Gold");
        SellerMessage(i18n::OfferUnavailableSeller, name.c_str());
        Answer(net::OfferUnavailable);
        return;
    }
    std::vector<Step> steps;
    bool ok = true;
    for (int i = 0; i < incoming.count && ok; ++i) {
        net::OfferEntry const& entry = incoming.entries[i];
        const Template item = Of(entry.item);
        if (!item.IsValid()) { ok = false; break; }
        if (entry.amount > 0) {
            ok = Take(self, item, entry.quality, entry.amount, steps) == entry.amount;
        } else if (entry.amount < 0) {
            ok = self.Inventory.CreateItems(item, entry.quality, -entry.amount) >= 0;
            if (ok) steps.push_back({item, entry.quality, -entry.amount, true});
        }
    }
    if (ok && incoming.gold > 0) ok = Take(self, Gold(), AnyQuality, incoming.gold, steps) == incoming.gold;
    if (ok && incoming.gold < 0) {
        ok = self.Inventory.CreateItems(Gold(), 0, -incoming.gold) >= 0;
        if (ok) steps.push_back({Gold(), 0, -incoming.gold, true});
    }
    if (!ok) {
        if (checking) Undo(self, steps);
        ++unavailable;
        Format(line, "offer: %u from player %u failed while applied; %s", incoming.serial, incoming.buyer, checking ? "undone" : "NOT undone (QA)");
        log(line);
        SellerMessage(i18n::OfferUnavailableSeller, Summary(incoming, true).c_str());
        Answer(net::OfferUnavailable);
        return;
    }
    ++applied;
    Format(line, "offer: %u from player %u accepted and applied (%u entries, gold %d to the buyer)", incoming.serial, incoming.buyer,
              incoming.count, incoming.gold);
    log(line);
    SellerMessage(i18n::OfferDoneSeller);
    Answer(net::OfferAccepted);
}

void Log2(char const* text) { Log(text); }

void OnOffer(net::OfferNote const& note)
{
    ++offersReceived;
    lastBuyer = net::PeerName(note.buyer);
    if (pending) {  // one at a time (owner 23.10.: one offer per trade window)
        ++busy;
        net::OfferResultNote result;
        result.buyer = note.buyer;
        result.result = net::OfferBusy;
        result.serial = note.serial;
        net::SendOfferResult(result);
        Logf("offer: %u from player %u answered busy (%u is open)", note.serial, note.buyer, incoming.serial);
        return;
    }
    incoming = note;
    pending = true;
    receivedAt = GetTickCount();
    acceptWasDown = declineWasDown = true;  // a key already held does not count; it has to be pressed for this offer
    Logf("offer: %u from player %u: %u entries, gold %d to the buyer", note.serial, note.buyer, note.count, note.gold);
    ShowIncoming();
}

void OnResult(net::OfferResultNote const& note)
{
    auto it = std::find_if(sent.begin(), sent.end(), [&note](Sent const& s) { return s.serial == note.serial && s.seller == note.seller; });
    if (it == sent.end()) { Logf("offer: answer %u to an offer %u not open here", note.result, note.serial); return; }
    sent.erase(it);
    lastSeller = net::PeerName(note.seller);
    const bool accepted = note.result == net::OfferAccepted;
    const i18n::Message message = accepted ? i18n::OfferAcceptedBuyer : note.result == net::OfferDeclined ? i18n::OfferDeclinedBuyer
                                  : note.result == net::OfferExpired ? i18n::OfferExpiredBuyer : note.result == net::OfferBusy ? i18n::OfferBusyBuyer
                                  : i18n::OfferUnavailableBuyer;
    wchar_t text[300];
    _snwprintf_s(text, _TRUNCATE, i18n::Text(message), PlayerName(note.seller).c_str());
    hud::Message(accepted ? hud::Good : hud::Warning, text);
    Logf("offer: %u answered by player %u: result %u (%s)", note.serial, note.seller, note.result, accepted ? "accepted" : "not accepted");
    if (resultHandler) resultHandler(note.serial, accepted);
}

// The buyer's name of an item: its focus name from its own inventory or the seller's puppet (the trade window had both).
std::string NameOf(uint8_t seller, uint8_t const id[16])
{
    const Template item = Of(id);
    eCEntity* holders[2] = {gCSession::GetInstance().GetHero(), remote::PuppetOf(seller)};
    for (eCEntity* holder : holders) {
        if (!holder) continue;
        Entity self(holder);
        for (int i = 0; i < self.Inventory.GetStackCount(); ++i) {
            if (!Same(self, i, item)) continue;
            Entity entity = self.Inventory.GetTemplateItem(i);
            if (!entity.GetInstance()) continue;
            char name[sizeof(net::OfferEntry::name)] = {};
            WideCharToMultiByte(CP_UTF8, 0, reinterpret_cast<wchar_t const*>(entity.GetFocusName().GetText()), -1, name, sizeof(name) - 1, nullptr, nullptr);
            if (*name) return name;
        }
    }
    return "?";
}

uint32_t SendNamed(uint8_t seller, Delta const* deltas, char const* const* names, int count, int32_t gold)
{
    if (count < 0 || count > net::MaxOfferEntries || seller == net::LocalId() || seller >= net::MaxPlayers || !net::Remotes()[seller].connected ||
        gold < -MaxGold || gold > MaxGold)
        return 0;
    net::OfferNote note;
    note.seller = seller;
    note.count = static_cast<uint8_t>(count);
    note.gold = gold;
    note.serial = ++nextSerial;
    for (int i = 0; i < count; ++i) {
        std::memcpy(note.entries[i].item, deltas[i].item, 16);
        note.entries[i].amount = deltas[i].amount;
        note.entries[i].quality = deltas[i].quality;
        std::string name = NameOf(seller, deltas[i].item);
        if (name == "?" && names && names[i]) name = names[i];  // QA: an item neither side has goes by its template name
        strncpy_s(note.entries[i].name, name.c_str(), _TRUNCATE);
    }
    net::SendOffer(note);
    sent.push_back({note.serial, seller, GetTickCount()});
    ++offersSent;
    Logf("offer: %u sent to player %u: %d entries, gold %d to the buyer", note.serial, seller, count, gold);
    return note.serial;
}
}

uint32_t Send(uint8_t seller, Delta const* deltas, int count, int32_t gold) { return SendNamed(seller, deltas, nullptr, count, gold); }

void SetResultHandler(ResultFn handler) { resultHandler = handler; }

namespace {
// The trade keys and the jump key are all INI choices, and two the same do both at one press (B 23.10.: partner_key =
// INSERT also accepts an offer). Warned once in the log at the start and once on the screen when the game runs.
struct Clash { std::string key, settings; };
std::vector<Clash> clashes;
bool keysChecked = false, clashShown = false;
uint32_t runningSince = 0;

void CheckKeys()
{
    Config const& config = GetConfig();
    struct Use { char const* setting; int code; };
    const Use uses[3] = {{"partner_key", partner::KeyFromName(config.partnerKey, VK_NEXT)},
                         {"offer_accept_key", partner::KeyFromName(config.offerAcceptKey, VK_INSERT)},
                         {"offer_decline_key", partner::KeyFromName(config.offerDeclineKey, VK_DELETE)}};
    bool listed[3] = {};
    for (int i = 0; i < 3; ++i) {
        if (listed[i]) continue;
        std::string settings = uses[i].setting;
        for (int j = i + 1; j < 3; ++j)
            if (uses[j].code == uses[i].code) {
                settings += std::string(", ") + uses[j].setting;
                listed[i] = listed[j] = true;
            }
        if (!listed[i]) continue;
        char name[16];
        partner::KeyName(uses[i].code, name, sizeof(name));
        clashes.push_back({name, settings});
        Logf("WARN keys: %s is set for %s; one press does all of them (g3together.ini)", name, settings.c_str());
    }
    keysChecked = true;
}

void ShowClashes(uint32_t now)
{
    if (clashShown || clashes.empty()) return;
    if (!gCSession::GetInstance().IsGameRunning()) { runningSince = 0; return; }
    if (!runningSince) runningSince = now ? now : 1;
    if (now - runningSince < 5000) return;  // after the game's own first messages
    for (Clash const& clash : clashes) {
        wchar_t text[300];
        _snwprintf_s(text, _TRUNCATE, i18n::Text(i18n::KeyClash), Wide(clash.key.c_str()).c_str(), Wide(clash.settings.c_str()).c_str());
        hud::Message(hud::Warning, text);
    }
    clashShown = true;
}
}

void Start()
{
    net::SetOfferHandlers(&OnOffer, &OnResult);
    if (GetConfig().qaKeyCheckOff && qa::Enabled()) Log("keys: not checked (QA negative control qa_keycheck = off)");
    else CheckKeys();
}

void KeyCheck(LogFn log)
{
    Config const& config = GetConfig();
    char names[3][16];
    partner::KeyName(partner::KeyFromName(config.partnerKey, VK_NEXT), names[0], sizeof(names[0]));
    partner::KeyName(partner::KeyFromName(config.offerAcceptKey, VK_INSERT), names[1], sizeof(names[1]));
    partner::KeyName(partner::KeyFromName(config.offerDeclineKey, VK_DELETE), names[2], sizeof(names[2]));
    std::string line = "KEYCHECK partner=" + std::string(names[0]) + " accept=" + names[1] + " decline=" + names[2] +
                       " checked=" + (keysChecked ? "1" : "0") + " clashes=" + std::to_string(clashes.size()) + " shown=" + (clashShown ? "1" : "0");
    for (Clash const& clash : clashes) line += " [" + clash.key + ": " + clash.settings + "]";
    log(line.c_str());
}

void Decide(bool accept, LogFn log)
{
    if (!pending) { log("OFFERDECIDE none"); return; }
    if (accept) {
        Apply(log);
    } else {
        ++declined;
        Logf("offer: %u from player %u declined", incoming.serial, incoming.buyer);
        SellerMessage(i18n::OfferDeclinedSeller);
        Answer(net::OfferDeclined);
    }
    log(accept ? "OFFERDECIDE accept" : "OFFERDECIDE decline");
}

void Tick()
{
    const uint32_t now = GetTickCount();
    ShowClashes(now);
    for (auto it = sent.begin(); it != sent.end();) {  // buyer: no answer in time counts as declined
        if (now - it->at < AnswerWithin) { ++it; continue; }
        const Sent gone = *it;
        it = sent.erase(it);
        wchar_t text[300];
        _snwprintf_s(text, _TRUNCATE, i18n::Text(i18n::OfferExpiredBuyer), PlayerName(gone.seller).c_str());
        hud::Message(hud::Warning, text);
        Logf("offer: %u to player %u: no answer in %u s; not accepted", gone.serial, gone.seller, AnswerWithin / 1000);
        if (resultHandler) resultHandler(gone.serial, false);
    }
    if (!pending) return;
    if (now - receivedAt >= DecideWithin) {
        ++expired;
        Logf("offer: %u from player %u ran out after %u s", incoming.serial, incoming.buyer, DecideWithin / 1000);
        SellerMessage(i18n::OfferExpiredSeller);
        Answer(net::OfferExpired);
        return;
    }
    if (now - remindedAt >= RemindEvery) ShowIncoming();
    // Not with Ctrl: the game's Ctrl+Insert is its free camera (Ini/controls.ini).
    const bool focused = gCSession::GetInstance().IsGameRunning() && partner::Focused() && !(GetAsyncKeyState(VK_CONTROL) & 0x8000);
    const bool acceptDown = focused && (GetAsyncKeyState(partner::KeyFromName(GetConfig().offerAcceptKey, VK_INSERT)) & 0x8000) != 0;
    const bool declineDown = focused && (GetAsyncKeyState(partner::KeyFromName(GetConfig().offerDeclineKey, VK_DELETE)) & 0x8000) != 0;
    if (acceptDown && !acceptWasDown) Decide(true, &Log2);
    else if (declineDown && !declineWasDown) Decide(false, &Log2);
    acceptWasDown = acceptDown;
    declineWasDown = declineDown;
}

void OnWorldChange()
{
    if (pending) {
        ++expired;
        Logf("offer: %u from player %u ends with the world", incoming.serial, incoming.buyer);
        Answer(net::OfferExpired);
    }
    for (Sent const& s : sent)
        if (resultHandler) resultHandler(s.serial, false);
    sent.clear();
}

void SendCommand(char const* argument, LogFn log)
{
    // <seller>_<gold>_<name>-<amount>[-<name>-<amount>...]: the bot passes only [A-Za-z0-9_-]. Template names hold
    // underscores, so only the first two separate; a negative amount (from the buyer) is written n3.
    std::string arg = argument ? argument : "";
    const size_t a = arg.find('_'), b = a == std::string::npos ? a : arg.find('_', a + 1);
    if (b == std::string::npos) { log("REFUSED offersend: <seller>_<gold>_<name>-<amount>[-<name>-<amount>]"); return; }
    const int seller = std::atoi(arg.substr(0, a).c_str()), gold = std::atoi(arg.substr(a + 1, b - a - 1).c_str());
    std::vector<std::string> parts;
    for (size_t at = b + 1; at <= arg.size();) {
        const size_t dash = arg.find('-', at), end = dash == std::string::npos ? arg.size() : dash;
        parts.push_back(arg.substr(at, end - at));
        at = end + 1;
    }
    if (parts.size() % 2) { log("REFUSED offersend: <name>-<amount> pairs"); return; }
    std::vector<Delta> deltas;
    std::vector<std::string> names;
    for (size_t i = 0; i + 1 < parts.size(); i += 2) {
        const std::string& name = parts[i];
        const Template item(name.c_str());
        if (!item.IsValid()) { log(("REFUSED offersend: no item " + name).c_str()); return; }
        Delta d{};
        bCPropertyID const& id = item;
        std::memcpy(d.item, &id.GetCore(), 16);
        std::string const& amount = parts[i + 1];
        d.amount = !amount.empty() && amount[0] == 'n' ? -std::atoi(amount.c_str() + 1) : std::atoi(amount.c_str());
        deltas.push_back(d);
        names.push_back(name);
    }
    std::vector<char const*> raw;
    for (auto const& n : names) raw.push_back(n.c_str());
    const uint32_t serial = SendNamed(static_cast<uint8_t>(seller), deltas.data(), raw.data(), static_cast<int>(deltas.size()), gold);
    char line[120];
    Format(line, "OFFERSENT serial=%u seller=%d entries=%zu gold=%d", serial, seller, deltas.size(), gold);
    log(line);
}

void Report(LogFn log)
{
    char line[400];
    Format(line, "OFFERSTATE pending=%d serial=%u buyer=%u open_sent=%zu sent=%u received=%u applied=%u declined=%u expired=%u unavailable=%u busy=%u rolled_back=%u checking=%d",
              pending ? 1 : 0, pending ? incoming.serial : 0u, pending ? incoming.buyer : 0u, sent.size(), offersSent, offersReceived, applied,
              declined, expired, unavailable, busy, rolledBack, checking ? 1 : 0);
    log(line);
}

void PartialOn(LogFn log)
{
    checking = false;
    log("OFFERPARTIAL ok=1 (no check before, no undo on a failure)");
}

void AddCommand(char const* argument, LogFn log)
{
    std::string arg = argument ? argument : "";
    const size_t cut = arg.rfind('_');
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    const Template item(cut == std::string::npos ? "" : arg.substr(0, cut).c_str());
    const int amount = cut == std::string::npos ? 0 : std::atoi(arg.substr(cut + 1).c_str());
    if (!hero || !item.IsValid() || amount < 1 || amount > MaxAmount) { log("REFUSED heroadd: <name>_<amount>"); return; }
    Entity self(hero);
    const int index = self.Inventory.CreateItems(item, 0, amount);
    char line[160];
    Format(line, "HEROADD name=%s amount=%d stack=%d", arg.substr(0, cut).c_str(), amount, index);
    log(line);
}

void CountCommand(char const* name, LogFn log)
{
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    const Template item(name ? name : "");
    if (!hero || !item.IsValid()) { log("REFUSED herocount: no hero or no such item"); return; }
    Entity self(hero);
    int total = 0;
    for (int i = 0; i < self.Inventory.GetStackCount(); ++i)
        if (Same(self, i, item)) total += self.Inventory.GetAmount(i);
    char line[160];
    Format(line, "HEROCOUNT name=%s total=%d tradable=%d", name, total, TradableAmount(self, item));
    log(line);
}
}
