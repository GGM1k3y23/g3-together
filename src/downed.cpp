// SPDX-License-Identifier: GPL-3.0-or-later
#include "downed.h"

#include "body.h"
#include "hud.h"
#include "log.h"
#include "net.h"
#include "remote.h"

#include <Game.h>
#include <Script.h>
#include <Windows.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace g3t::downed {
namespace {
bool enabled = true;      // death becomes a knock-out
bool holdEnabled = true;  // a knocked-out hero waits for a partner
bool potionNeeded = true; // QA negative control: potionoff (helping up is free again)
// Only a death the mod turned into a knock-out (RedirectDeath) waits for help (PLAN-2026-10-22 2.2, re/findings/
// defeat-arena-c.md). A knock-out of the game's own (guards, a duel, the arena: Defeat) takes the game's course:
// the winner may rob the hero, the hero stands up by itself. Before, every knock-out was held (spieltest 21.10.
// 21:35: guards knocked a player out, a partner paid a potion, nobody robbed him).
bool lethalOnly = true;   // QA negative control: lethalonlyoff (every knock-out waits for help again)
unsigned redirects = 0, revives = 0, releases = 0, deaths = 0, potionsPaid = 0, refusedNoPotion = 0;
char const* const KnockOutTask = "ZS_LieKnockDown";
char const* const DeathTask = "ZS_Dead";
char const* const PotionPrefix = "It_Potion_Health";  // healing potions (the game's own templates)
constexpr float ReviveRange = 250.0f;  // cm: a partner standing next to the hero
constexpr uint32_t ReviveTime = 3000;  // ms the partner has to stay there
constexpr uint32_t StaleState = 5000;  // ms: a remote state older than this does not count
constexpr uint32_t AloneGrace = 5000;  // ms nobody can help before the knocked-out hero dies (a partner loading a save)
constexpr uint32_t PaidCooldown = 5000; // ms: the revived player's state still says "down" for a moment

bool held = false;              // the local hero lies knocked out and waits for help
bool lethal = false;            // the knock-out it lies in replaced its death (RedirectDeath)
bool lethalSeenDown = false;    // ... and the hero has lain down since (until then a frame without IsDown is no standing up)
uint32_t lethalAt = 0;          // when RedirectDeath set it
bool nativeNoted = false;       // logged once per knock-out of the game's own
bool dying = false;             // nobody could help: the hero dies like in the single-player game
uint32_t diedAt = 0;            // when the death task was (last) set
uint32_t aloneSince = 0;        // when the knocked-out hero last had nobody able to help (0: someone can)
int revivedBy = 0;              // a partner paid for our revive (set by the network, handled in Tick)
bool told[net::MaxPlayers] = {};  // this player already saw "X is down" for player i
bool toldDead[net::MaxPlayers] = {};
uint32_t helpingSince[net::MaxPlayers] = {};  // local hero next to downed player i since then (0: not there)
bool hinted[net::MaxPlayers] = {};            // "you need a potion" shown for this stay next to player i
uint32_t paidAt[net::MaxPlayers] = {};        // when we paid a potion for player i

// A remote player who can help: in the same world, in game, recent, neither dead nor waiting for help itself. One
// in a knock-out of the game's own stands up by itself shortly.
bool Able(net::Remote const& r, uint32_t now)
{
    return r.connected && r.latestTicks && now - r.latestTicks < StaleState && (r.latest.flags & 1) && !(r.latest.flags & 12) &&
           r.latest.world == remote::CurrentWorld();
}

bool Present(net::Remote const& r, uint32_t now)
{
    return r.connected && r.latestTicks && now - r.latestTicks < StaleState && (r.latest.flags & 1) &&
           r.latest.world == remote::CurrentWorld();
}

void Wide(char const* text, wchar_t* out, int size)
{
    out[0] = 0;
    if (!MultiByteToWideChar(CP_UTF8, 0, text, -1, out, size)) out[0] = 0;
    out[size - 1] = 0;
}

void StandUp(Entity& hero)
{
    const int max = hero.PlayerMemory.GetHitPointsMax();
    hero.Routine.FullStop();
    hero.PlayerMemory.SetHitPoints(max / 4 > 1 ? max / 4 : 1);
    hero.Routine.ContinueRoutine();  // like the knock-out loop itself: Rtn_Player stands the hero up
}

// The weakest healing potion the hero carries (cheapest by gold value), or -1.
GEInt SmallestPotion(Entity& hero, char* name, size_t size)
{
    GEInt best = -1, bestValue = 0;
    for (GEInt i = 0; i < hero.Inventory.GetStackCount(); ++i) {
        Entity item = hero.Inventory.GetTemplateItem(i);
        if (!item.GetInstance() || hero.Inventory.GetAmount(i) < 1) continue;
        if (std::strncmp(item.GetName().GetText(), PotionPrefix, std::strlen(PotionPrefix)) != 0) continue;
        const GEInt value = item.Item.GetGoldValue();
        if (best < 0 || value < bestValue) { best = i; bestValue = value; strncpy_s(name, size, item.GetName().GetText(), _TRUNCATE); }
    }
    return best;
}

void SendToPlayer(uint8_t kind, int player)
{
    net::WorldState s{};
    s.kind = kind;
    s.guard = static_cast<uint8_t>(player);      // to this player
    s.npcClass = net::LocalId();                 // from this one (the host passes it on)
    net::SendWorldState(s, 0);
}

// The local hero stands next to a downed partner: after ReviveTime it pays a healing potion and helps it up.
void Help(Entity& hero, net::Remote const* remotes, uint32_t now)
{
    const bCVector& at = hero.GetInstance()->GetWorldPosition();
    const float here[3] = {at.GetX(), at.GetY(), at.GetZ()};
    wchar_t name[32], text[200];
    for (int i = 1; i < net::MaxPlayers; ++i) {
        if (i == net::LocalId()) continue;
        net::Remote const& r = remotes[i];
        const bool down = Present(r, now) && (r.latest.flags & 8) && !(r.latest.flags & 4);  // waiting for help, not any knock-out
        if (!down || body::Distance(r.latest.position, here) > ReviveRange || now - paidAt[i] < PaidCooldown) {
            helpingSince[i] = 0;
            hinted[i] = false;
            continue;
        }
        if (!helpingSince[i]) { helpingSince[i] = now; continue; }
        if (now - helpingSince[i] < ReviveTime) continue;
        Wide(r.latest.name, name, 32);
        char potion[64] = "";
        const GEInt stack = potionNeeded ? SmallestPotion(hero, potion, sizeof(potion)) : -1;
        if (potionNeeded && stack < 0) {
            if (!hinted[i]) {
                hinted[i] = true;
                ++refusedNoPotion;
                swprintf_s(text, L"Du brauchst einen Heiltrank, um %s aufzuhelfen.", name);
                hud::Message(hud::Warning, text);
                SendToPlayer(9, i);
                Logf("downed: next to player %d without a healing potion", i);
            }
            continue;
        }
        if (stack >= 0) {
            hero.Inventory.DeleteItems(stack, 1);
            ++potionsPaid;
        }
        paidAt[i] = now;
        helpingSince[i] = 0;
        SendToPlayer(8, i);
        swprintf_s(text, potionNeeded ? L"Du hast %s aufgeholfen (ein Heiltrank verbraucht)." : L"Du hast %s aufgeholfen.", name);
        hud::Message(hud::Good, text);
        Logf("downed: helped player %d up, paid %s", i, *potion ? potion : "nothing");
    }
}

// Nobody can help: the hero dies the way the game lets it die (the death task, whose script shows the game over).
void Die(Entity& hero)
{
    dying = true;
    diedAt = net::Now();
    held = false;
    ++deaths;
    Logf("downed: nobody can help; the hero dies (hp=%d)", hero.PlayerMemory.GetHitPoints());
    hud::Message(hud::Bad, L"Niemand kann dir mehr helfen.");
    hero.PlayerMemory.SetHitPoints(0);
    hero.Routine.FullStop();
    hero.Routine.SetTask(bCString(DeathTask));
}
}

// A hero's death runs through the tasks ZS_Dead or ZS_RagDollDead (the hit judgement and the kill cheat set
// them); their script Kill then shows "Game over" and the player can only load a save (stage A exploration:
// the hero lay dead with the session still running). The knock-out task ends with the hero standing up again
// (from 0 hit points to 20 to 30 within 10 s in the same exploration), so it replaces the death, unless nobody
// can help any more (Die).
bool RedirectDeath(eCEntity* self, char const* state)
{
    if (!enabled || dying || !self || self != gCSession::GetInstance().GetHero()) return false;
    if (std::strcmp(state, "ZS_Dead") != 0 && std::strcmp(state, "ZS_RagDollDead") != 0) return false;
    Entity hero(self);
    hero.Routine.FullStop();
    hero.Routine.SetTask(bCString(KnockOutTask));
    lethal = true;
    lethalSeenDown = false;
    lethalAt = net::Now();
    ++redirects;
    Logf("downed: hero would die (%s), knocked out instead (hp=%d)", state, hero.PlayerMemory.GetHitPoints());
    return true;
}

// The knocked-out hero gets up when its knock-out loop (ZS_Unconscious_Loop) finds the task time above 5 s
// and calls ContinueRoutine (static analysis). The task time is held at 0 and the hit points at most 1 (the loop
// heals +10 per second) until a partner who stood next to the hero for 3 s pays a healing potion for it (owner,
// 19.10.); then it stands up with a quarter of its hit points. Without a potion the partner is told so and the
// hero stays down. When nobody can help for 5 s (alone, everybody down or dead, partner gone), the hero dies like
// in the single-player game.
void Tick()
{
    gCSession& session = gCSession::GetInstance();
    eCDynamicEntity* instance = session.GetHero();
    if (!instance || !session.IsGameRunning()) { held = false; lethal = false; dying = false; aloneSince = 0; revivedBy = 0; return; }
    Entity hero(instance);
    const uint32_t now = net::Now();
    net::Remote const* remotes = net::Remotes();
    const uint8_t self = net::LocalId();
    wchar_t name[32], text[200];

    // Tell this player when someone else goes down or dies.
    for (int i = 1; i < net::MaxPlayers; ++i) {
        if (i == self) continue;
        const bool present = Present(remotes[i], now);
        const bool dead = present && (remotes[i].latest.flags & 4);
        const bool down = present && (remotes[i].latest.flags & 8) && !dead;  // waiting for help
        Wide(remotes[i].latest.name, name, 32);
        if (down && !told[i]) {
            swprintf_s(text, potionNeeded ? L"%s liegt am Boden. Stell dich mit einem Heiltrank daneben, um aufzuhelfen."
                                          : L"%s liegt am Boden. Stell dich daneben, um aufzuhelfen.", name);
            hud::Message(hud::Warning, text);
        }
        if (dead && !toldDead[i]) {
            swprintf_s(text, L"%s ist gestorben.", name);
            hud::Message(hud::Bad, text);
        }
        told[i] = down;
        toldDead[i] = dead;
    }

    if (hero.IsDead()) { held = false; lethal = false; return; }
    const bool down = hero.IsDown();
    if (down && lethal) lethalSeenDown = true;
    if (!down) {
        // The knock-out task RedirectDeath sets makes the hero lie a frame or more later: not standing up yet (22.10.:
        // reset at once, every intercepted death went the game's own way and nobody could help, stages A and A2).
        if (lethal && !lethalSeenDown && now - lethalAt < 3000) return;
        held = false;
        lethal = false;
        lethalSeenDown = false;
        nativeNoted = false;
        dying = false;
        aloneSince = 0;
        revivedBy = 0;
        Help(hero, remotes, now);
        return;
    }
    if (dying) {
        // A hit on the dying hero (an orc striking it again) set a knock-out over the death task, and the hero lay
        // there for good (stage A2, second run). Until it is dead, put the death back.
        const bCString task = hero.Routine.GetCurrentTask();
        if (std::strcmp(task.GetText(), DeathTask) != 0 && now - diedAt >= 500) {
            diedAt = now;
            Logf("downed: the death task was replaced by %s; dying again", task.GetText());
            hero.PlayerMemory.SetHitPoints(0);
            hero.Routine.FullStop();
            hero.Routine.SetTask(bCString(DeathTask));
        }
        return;
    }
    if (!held) {
        if (!holdEnabled) return;  // QA negative control: the game's own timer stands the hero up
        if (lethalOnly && !lethal) {
            if (!nativeNoted) {
                nativeNoted = true;
                Logf("downed: hero knocked out by the game's own rules (not a death; hp=%d); it stands up by itself",
                     hero.PlayerMemory.GetHitPoints());
            }
            return;
        }
        held = true;
        aloneSince = 0;
        revivedBy = 0;
        Logf("downed: hero knocked out, waiting for a partner (hp=%d)", hero.PlayerMemory.GetHitPoints());
        hud::Message(hud::Bad, potionNeeded ? L"Du bist niedergeschlagen. Ein Mitspieler mit Heiltrank kann dir aufhelfen."
                                            : L"Du bist niedergeschlagen. Warte, bis dein Mitspieler dir aufhilft.");
    }
    hero.Routine.SetTaskTime(0.0f);
    if (hero.PlayerMemory.GetHitPoints() > 1) hero.PlayerMemory.SetHitPoints(1);

    if (revivedBy) {
        const int by = revivedBy;
        revivedBy = 0;
        held = false;
        aloneSince = 0;
        ++revives;
        StandUp(hero);
        Wide(remotes[by].latest.name, name, 32);
        swprintf_s(text, L"%s hat dir aufgeholfen.", name);
        hud::Message(hud::Good, text);
        Logf("downed: revived by player %d (hp=%d)", by, hero.PlayerMemory.GetHitPoints());
        return;
    }

    bool helpPossible = false;
    for (int i = 1; i < net::MaxPlayers; ++i)
        if (i != self && Able(remotes[i], now)) helpPossible = true;
    if (helpPossible) { aloneSince = 0; return; }
    if (!aloneSince) { aloneSince = now ? now : 1; return; }
    if (now - aloneSince >= AloneGrace) Die(hero);
}

// A partner paid a potion for us (WorldState kind 8 to this player).
void Revived(int byPlayer)
{
    if (byPlayer > 0 && byPlayer < net::MaxPlayers) revivedBy = byPlayer;
}

// A partner stood next to us without a healing potion (kind 9).
void HelperHasNoPotion(int byPlayer)
{
    if (byPlayer <= 0 || byPlayer >= net::MaxPlayers || !held) return;
    wchar_t name[32], text[160];
    Wide(net::Remotes()[byPlayer].latest.name, name, 32);
    swprintf_s(text, L"%s hat keinen Heiltrank, um dir aufzuhelfen.", name);
    hud::Message(hud::Info, text);
}

bool Held() { return held; }

void Off(LogFn log)
{
    enabled = false;
    log("DOWNEDOFF ok=1");
}

void HoldOff(LogFn log)
{
    holdEnabled = false;
    log("HOLDOFF ok=1");
}

void PotionOff(LogFn log)
{
    potionNeeded = false;
    log("POTIONOFF helping up is free");
}

void LethalOnlyOff(LogFn log)
{
    lethalOnly = false;
    log("LETHALONLYOFF ok=1");
}

// QA: items for the local hero ("Template" or "Template-Amount"; QA arguments allow only [A-Za-z0-9_-]).
void GivePotion(char const* argument, LogFn log)
{
    char line[200], templ[64] = {};
    strncpy_s(templ, argument, _TRUNCATE);
    int amount = 1;
    if (char* dash = std::strrchr(templ, '-')) {
        *dash = '\0';
        amount = std::atoi(dash + 1);
    }
    eCDynamicEntity* instance = gCSession::GetInstance().GetHero();
    const Template item(templ);
    if (!instance || !item.IsValid()) { sprintf_s(line, "GIVEPOTION name=%s valid=%d", templ, item.IsValid() ? 1 : 0); log(line); return; }
    Entity hero(instance);
    if (amount > 0) {
        hero.Inventory.CreateItems(item, 0, amount);
    } else {  // "-0": take all of them away
        for (GEInt i = hero.Inventory.GetStackCount() - 1; i >= 0; --i) {
            Entity carried = hero.Inventory.GetTemplateItem(i);
            if (carried.GetInstance() && !std::strcmp(carried.GetName().GetText(), templ)) hero.Inventory.DeleteItems(i, hero.Inventory.GetAmount(i));
        }
    }
    sprintf_s(line, "GIVEPOTION name=%s valid=1 amount=%d now=%d", templ, amount, hero.Inventory.GetItemAmount(item));
    log(line);
}

// QA: the healing potions the local hero carries.
void Potions(LogFn log)
{
    char line[300];
    eCDynamicEntity* instance = gCSession::GetInstance().GetHero();
    if (!instance) { log("POTIONS end count=0"); return; }
    Entity hero(instance);
    int total = 0;
    for (GEInt i = 0; i < hero.Inventory.GetStackCount(); ++i) {
        Entity item = hero.Inventory.GetTemplateItem(i);
        if (!item.GetInstance() || std::strncmp(item.GetName().GetText(), "It_Potion", 9) != 0) continue;
        const GEInt amount = hero.Inventory.GetAmount(i);
        if (!std::strncmp(item.GetName().GetText(), PotionPrefix, std::strlen(PotionPrefix))) total += amount;
        sprintf_s(line, "POTION name=%s amount=%d gold=%d", item.GetName().GetText(), amount, item.Item.GetGoldValue());
        log(line);
    }
    sprintf_s(line, "POTIONS end health=%d", total);
    log(line);
}

void Report(LogFn log)
{
    char line[240];
    sprintf_s(line, "DOWNED lethal=%d lethal_only=%d enabled=%d hold=%d held=%d redirects=%u revives=%u releases=%u deaths=%u potions_paid=%u no_potion=%u potion_needed=%d",
              lethal ? 1 : 0, lethalOnly ? 1 : 0, enabled ? 1 : 0, holdEnabled ? 1 : 0, held ? 1 : 0, redirects, revives, releases, deaths, potionsPaid, refusedNoPotion,
              potionNeeded ? 1 : 0);
    log(line);
}
}
