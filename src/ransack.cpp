// SPDX-License-Identifier: GPL-3.0-or-later
#include "ransack.h"

#include "downed.h"
#include "hud.h"
#include "i18n.h"
#include "log.h"
#include "net.h"
#include "npcs.h"
#include "remote.h"
#include "trade.h"

#include <Game.h>
#include <Script.h>
#include <Windows.h>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <set>
#include <vector>

namespace g3t::ransack {
namespace {
// Native robbery (C re/findings/defeat-arena-c.md, hero-death.md, p3-load-autoload-ransack-contract-c-20261023.md):
// ZS_Attack_End asks WantToRansack (two humans, victim not Ransacked yet, not a mage or paladin robbing) and sets the task
// ZS_RansackBody: the robber walks to the body, checks that it is near and still in AIMode Down, resets the body's task
// time (so it does not stand up meanwhile), finds its first It_Gold stack and takes half of it (one piece of one) with
// PSInventory::TransferAmountTo. A puppet is a body of that kind, in AIMode Abandoned and without the player's gold: for
// the AI nobody lay there to rob.
char const* const Gold = "It_Gold";
constexpr uintptr_t RobCall = 0x1fb73;  // ZS_RansackBody: call dword ptr [Script_Game.dll+0xe4500], returns to 0x1fb79
constexpr uint32_t HoldEvery = 500;     // ms between two holds to one player
constexpr uint32_t PaidWait = 60000;    // ms: a take nobody paid for in this time is given back by the robber
bool enabled = true;  // QA negative control: ransackoff
bool hooked = false, hookFailed = false;
uint32_t lastTick = 0;

// A remote player, as its puppet here.
struct Victim {
    bool down = false;    // lies knocked out by the game's own rules (state flag bit 4), and so does its puppet here
    bool robbed = false;  // a robber took from its puppet during this knock-out: the puppet's gold stays as it is
    int mirrored = -1;    // gold put on the puppet during this knock-out (-1: none yet)
    uint32_t lastHold = 0;
};
Victim victims[net::MaxPlayers];

// Takes sent, waiting for the player's payment.
struct Pending {
    uint8_t player = 0;
    uint32_t seq = 0;
    int32_t amount = 0;
    uint8_t robber[16] = {};
    uint32_t at = 0;
};
std::vector<Pending> pending;
uint32_t nextSeq = 1;
std::set<uint32_t> applied[net::MaxPlayers];  // robbed side: robberies already paid, per robbing machine

unsigned holdsSent = 0, holdsTaken = 0, takesSent = 0, takesPaid = 0, takesReceived = 0, corrections = 0, refunds = 0, downs = 0;
int32_t goldTaken = 0, goldPaid = 0, goldPaidHere = 0;

void GuidOf(eCEntity* entity, uint8_t out[16])
{
    std::memset(out, 0, 16);
    if (!entity) return;
    const eCEntityProxy proxy(entity);
    std::memcpy(out, &proxy.GetEntityID().GetCore(), 16);
}

Entity ByGuid(uint8_t const guid[16])
{
    _GUID raw;
    std::memcpy(&raw, guid, 16);
    return Entity(bCPropertyID(bCGuid(raw)));
}

int GoldOn(Entity& body)
{
    int sum = 0;
    for (GEInt i = 0; i < body.Inventory.GetStackCount(); ++i) {
        Entity item = body.Inventory.GetTemplateItem(i);
        if (item.GetInstance() && !std::strcmp(item.GetName().GetText(), Gold)) sum += body.Inventory.GetAmount(i);
    }
    return sum;
}

// Takes up to `amount` gold out of a body's stacks; returns what it took.
int TakeGold(Entity& body, int amount)
{
    int taken = 0;
    for (GEInt i = body.Inventory.GetStackCount() - 1; i >= 0 && taken < amount; --i) {
        Entity item = body.Inventory.GetTemplateItem(i);
        if (!item.GetInstance() || std::strcmp(item.GetName().GetText(), Gold) != 0) continue;
        const int here = body.Inventory.GetAmount(i);
        const int take = here < amount - taken ? here : amount - taken;
        if (take > 0 && (take == here ? body.Inventory.DeleteStack(i) : body.Inventory.DeleteItems(i, take))) taken += take;
    }
    return taken;
}

// One unequipped gold stack of exactly `amount` (the robbery reads the first It_Gold stack only).
void SetGold(Entity& body, int amount)
{
    for (GEInt i = body.Inventory.GetStackCount() - 1; i >= 0; --i) {
        Entity item = body.Inventory.GetTemplateItem(i);
        if (item.GetInstance() && !std::strcmp(item.GetName().GetText(), Gold) && body.Inventory.GetSlotFromStack(i) == gESlot_None)
            body.Inventory.DeleteStack(i);
    }
    if (amount > 0) body.Inventory.CreateItems(Template(Gold), 0, amount);
}

// The game's own member (the script wrapper looks properties up by name; see infocmd.cpp).
void ClearRansacked(eCEntity* body)
{
    if (auto* ps = static_cast<gCNPC_PS*>(body->GetPropertySet(eEPropertySetType_NPC))) ps->AccessRansacked() = GEFalse;
}

void Wide(char const* text, wchar_t* out, int size)
{
    out[0] = 0;
    if (!MultiByteToWideChar(CP_UTF8, 0, text, -1, out, size)) out[0] = 0;
    out[size - 1] = 0;
}

// The robbery's transfer, from ZS_RansackBody's own call site. From a puppet: it happens here as the game does it (the
// robber gets the gold); the player whose puppet it is pays it.
GEBool __fastcall RobTransfer(PSInventory* self, void*, GEInt stack, GEInt amount, Entity& to)
{
    eCEntity* owner = nullptr;
    if (auto* set = reinterpret_cast<EntityPropertySet*>(self)->m_pEngineEntityPropertySet) owner = set->GetEntity();
    const int id = remote::PuppetOwner(owner);
    const GEBool moved = self->TransferAmountTo(stack, amount, to);
    if (!id) return moved;
    victims[id].robbed = true;
    if (!moved || amount <= 0) {
        Logf("ransack: %s could not take %d gold from player %d's puppet", to.GetName().GetText(), amount, id);
        return moved;
    }
    goldTaken += amount;
    if (!enabled) {
        Logf("ransack: %s took %d gold from player %d's puppet; not passed on (ransackoff)", to.GetName().GetText(), amount, id);
        return moved;
    }
    Pending take;
    take.player = static_cast<uint8_t>(id);
    take.seq = nextSeq++;
    take.amount = amount;
    GuidOf(to.GetInstance(), take.robber);
    take.at = net::Now();
    pending.push_back(take);
    net::RansackNote note;
    note.op = net::RansackTake;
    note.seq = take.seq;
    note.amount = amount;
    std::memcpy(note.robber, take.robber, 16);
    net::SendRansack(take.player, note);
    ++takesSent;
    Logf("ransack: %s took %d gold from player %d's puppet (robbery %u, robber now %d gold); the player pays it", to.GetName().GetText(),
         amount, id, take.seq, to.Inventory.GetItemAmount(Template(Gold)));
    return moved;
}

void Hook()
{
    auto* base = reinterpret_cast<uint8_t*>(GetModuleHandleA("Script_Game.dll"));
    if (!base) return;
    uint8_t* site = base + RobCall;
    // Expect "call dword ptr [Script_Game.dll+0xe4500]" (ff 15 <address>), as at the quick loot site; else another build.
    uint32_t slot = 0;
    std::memcpy(&slot, site + 2, 4);
    if (site[0] != 0xFF || site[1] != 0x15 || slot != reinterpret_cast<uintptr_t>(base) + 0xe4500) {
        hookFailed = true;
        Log("ransack: robbery call site not as expected; a partner is not robbed");
        return;
    }
    DWORD old = 0;
    if (!VirtualProtect(site, 6, PAGE_EXECUTE_READWRITE, &old)) { hookFailed = true; return; }
    site[0] = 0xE8;  // call rel32 RobTransfer, then a nop: same length, same stack
    const int32_t rel = static_cast<int32_t>(reinterpret_cast<intptr_t>(&RobTransfer) - reinterpret_cast<intptr_t>(site + 5));
    std::memcpy(site + 1, &rel, 4);
    site[5] = 0x90;
    VirtualProtect(site, 6, old, &old);
    FlushInstructionCache(GetCurrentProcess(), site, 6);
    hooked = true;
    Log("ransack: robbery hook installed");
}

// Robbed side: pay a take from the real hero (as much as it still has), and say so.
void Pay(uint8_t from, net::RansackNote const& note)
{
    ++takesReceived;
    net::RansackNote reply;
    reply.op = net::RansackPaid;
    reply.seq = note.seq;
    std::memcpy(reply.robber, note.robber, 16);
    if (!applied[from].insert(note.seq).second) {  // the same robbery again: paid already, nothing more
        Logf("ransack: robbery %u of player %d came again; paid already", note.seq, from);
        return;
    }
    eCDynamicEntity* instance = gCSession::GetInstance().GetHero();
    int paid = 0, before = 0;
    if (instance && note.amount > 0) {
        Entity hero(instance);
        before = hero.Inventory.GetItemAmount(Template(Gold));
        paid = TakeGold(hero, note.amount);
    }
    reply.amount = paid;
    net::SendRansack(from, reply);
    goldPaidHere += paid;
    eCEntity* robber = npcs::NpcByGuid(note.robber);
    Logf("ransack: robbed of %d gold by %s (robbery %u of player %d; asked %d, had %d)", paid, robber ? Entity(robber).GetName().GetText() : "?",
         note.seq, from, note.amount, before);
    if (paid <= 0) return;
    wchar_t text[200];
    if (robber) {
        wchar_t name[64];
        bCUnicodeString focus = Entity(robber).GetFocusName();
        wcsncpy_s(name, reinterpret_cast<wchar_t const*>(focus.GetText()), _TRUNCATE);
        if (!name[0]) Wide(Entity(robber).GetName().GetText(), name, 64);
        swprintf_s(text, i18n::Text(i18n::RobbedBy), name, paid);
    } else {
        swprintf_s(text, i18n::Text(i18n::Robbed), paid);
    }
    hud::Message(hud::Bad, text);
}

// Robbing side: the player paid. Whatever it could not pay, the robber gives back.
void Paid(uint8_t from, net::RansackNote const& note)
{
    for (auto it = pending.begin(); it != pending.end(); ++it) {
        if (it->player != from || it->seq != note.seq) continue;
        ++takesPaid;
        goldPaid += note.amount;
        if (note.amount < it->amount) {
            Entity robber = ByGuid(it->robber);
            const int back = robber.GetInstance() ? TakeGold(robber, it->amount - note.amount) : 0;
            ++corrections;
            Logf("ransack: player %d paid %d of %d gold (robbery %u); the robber gives %d back", from, note.amount, it->amount, note.seq, back);
        } else {
            Logf("ransack: player %d paid %d gold (robbery %u)", from, note.amount, note.seq);
        }
        pending.erase(it);
        return;
    }
    Logf("ransack: payment for an unknown robbery %u from player %d", note.seq, from);
}

void OnRansack(uint8_t from, net::RansackNote const& note)
{
    if (from <= 0 || from >= net::MaxPlayers) return;
    if (note.op == net::RansackHold) {
        // As the robbery does to the body it robs: its knock-out timer starts again, the hero does not stand up meanwhile.
        if (!downed::NativeDown()) return;
        Entity(gCSession::GetInstance().GetHero()).Routine.SetTaskTime(0.0f);
        if (!holdsTaken++) Logf("ransack: a robber of player %d is at work; this hero stays down meanwhile", from);
    } else if (note.op == net::RansackTake) {
        Pay(from, note);
    } else if (note.op == net::RansackPaid) {
        Paid(from, note);
    }
}
}

void Start() { net::SetRansackHandler(&OnRansack); }

void OnRobbing(gCScriptProcessingUnit* spu)
{
    eCEntity* self = spu ? spu->GetSelfEntity() : nullptr;
    eCEntity* target = spu ? spu->GetTargetEntity() : nullptr;
    const int id = remote::PuppetOwner(target);
    if (!id || !self) return;
    Victim& victim = victims[id];
    const uint32_t now = net::Now();
    if (victim.lastHold && now - victim.lastHold < HoldEvery) return;
    if (!victim.lastHold) Logf("ransack: %s robs player %d's puppet", Entity(self).GetName().GetText(), id);
    victim.lastHold = now ? now : 1;
    net::RansackNote note;
    note.op = net::RansackHold;
    GuidOf(self, note.robber);
    net::SendRansack(static_cast<uint8_t>(id), note);
    ++holdsSent;
}

void Tick()
{
    gCSession& session = gCSession::GetInstance();
    if (!session.IsGameRunning() || !session.GetHero()) return;
    if (!hooked && !hookFailed) Hook();
    const uint32_t now = net::Now();
    if (now - lastTick < 100) return;
    lastTick = now;
    net::Remote const* remotes = net::Remotes();
    for (int id = 1; id < net::MaxPlayers; ++id) {
        if (id == net::LocalId()) continue;
        Victim& victim = victims[id];
        eCEntity* body = remote::PuppetOf(id);
        if (!body) { victim = Victim{}; continue; }  // a new puppet starts as the game made it
        net::Remote const& r = remotes[id];
        const bool fresh = r.connected && r.latestTicks && now - r.latestTicks < 5000;
        const bool down = fresh && (r.latest.flags & 16) && !(r.latest.flags & 4);
        Entity puppet(body);
        if (down) {
            if (!victim.down) {
                victim.down = true;
                victim.robbed = false;
                victim.lastHold = 0;
                ClearRansacked(body);  // a new knock-out may be robbed again (no Defeat runs for a puppet here)
                ++downs;
                Logf("ransack: player %d is knocked out (%d gold); its puppet lies down for the AI", id, r.latest.gold);
            }
            if (puppet.Routine.AIMode != gEAIMode_Down) puppet.Routine.AIMode = gEAIMode_Down;
            if (!victim.robbed && (victim.mirrored != r.latest.gold || GoldOn(puppet) != r.latest.gold)) {
                SetGold(puppet, r.latest.gold);
                victim.mirrored = r.latest.gold;
            }
        } else if (victim.down) {
            victim.down = false;
            victim.robbed = false;
            victim.mirrored = -1;
            puppet.Routine.AIMode = gEAIMode_Abandoned;  // as body::Pacify keeps a puppet
            ClearRansacked(body);
            trade::RestoreGold(body, id);
            Logf("ransack: player %d stands again", id);
        }
    }
    // A take that nobody paid (the player left, or no answer): the robber gives it back; nobody lost that gold.
    for (auto it = pending.begin(); it != pending.end();) {
        net::Remote const& r = remotes[it->player];
        if (r.connected && now - it->at < PaidWait) { ++it; continue; }
        Entity robber = ByGuid(it->robber);
        const int back = robber.GetInstance() ? TakeGold(robber, it->amount) : 0;
        ++refunds;
        Logf("ransack: robbery %u of player %d was not paid (%s); the robber gives %d gold back", it->seq, it->player,
             r.connected ? "no answer" : "player gone", back);
        it = pending.erase(it);
    }
}

void OnWorldChange()
{
    for (auto& victim : victims) victim = Victim{};
    for (auto& seen : applied) seen.clear();
    if (!pending.empty()) Logf("ransack: %zu unpaid robberies forgotten (world change)", pending.size());
    pending.clear();
}

void Report(LogFn log)
{
    char line[500];
    int aiMode = -1, gold = -1, ransacked = -1;
    if (eCEntity* body = remote::PuppetOf(net::IsHost() ? 2 : 1)) {
        Entity puppet(body);
        aiMode = static_cast<int>(puppet.Routine.AIMode);
        gold = GoldOn(puppet);
        ransacked = puppet.NPC.Ransacked ? 1 : 0;
    }
    eCDynamicEntity* hero = gCSession::GetInstance().GetHero();
    sprintf_s(line, "RANSACK enabled=%d hooked=%d hook_failed=%d downs=%u holds_sent=%u holds_taken=%u takes_sent=%u takes_received=%u takes_paid=%u"
                    " gold_taken=%d gold_paid=%d gold_paid_here=%d corrections=%u refunds=%u pending=%zu puppet_aimode=%d puppet_gold=%d"
                    " puppet_ransacked=%d native_down=%d hero_gold=%d",
              enabled ? 1 : 0, hooked ? 1 : 0, hookFailed ? 1 : 0, downs, holdsSent, holdsTaken, takesSent, takesReceived, takesPaid, goldTaken, goldPaid,
              goldPaidHere, corrections, refunds, pending.size(), aiMode, gold, ransacked, downed::NativeDown() ? 1 : 0,
              hero ? Entity(hero).Inventory.GetItemAmount(Template(Gold)) : -1);
    log(line);
}

void Off(LogFn log)
{
    enabled = false;
    log("RANSACKOFF ok=1");
}

void NpcGold(char const* name, LogFn log)
{
    char line[200];
    Entity npc = Entity::GetEntity(bCString(name));
    sprintf_s(line, "NPCGOLD name=%s found=%d gold=%d task=%s", name, npc.GetInstance() ? 1 : 0, npc.GetInstance() ? GoldOn(npc) : -1,
              npc.GetInstance() ? npc.Routine.GetCurrentTask().GetText() : "-");
    log(line);
}
}
