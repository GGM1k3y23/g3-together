// SPDX-License-Identifier: GPL-3.0-or-later
#include "clock.h"

#include "log.h"
#include "net.h"

#include <Game.h>
#include <Script.h>
#include <Windows.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace g3t::clock {
namespace {
constexpr uint32_t SendInterval = 5000;  // ms
// The clock runs at factor 12 (12 game seconds per real second), so 5 s between updates are 60 game seconds;
// two game minutes of difference are a real difference, not the delay of one update.
constexpr int Tolerance = 120;
bool following = true;
uint32_t lastSend = 0;
unsigned corrections = 0, received = 0;
int lastDifference = 0;
uint32_t lastLocal = 0;        // client: the local clock at the previous frame
unsigned requests = 0, granted = 0;
constexpr uint32_t SleepJump = 1800;  // game seconds: a jump this large in one frame is a sleep (or a wait)
net::ClockState Now();
uint32_t requestedAt = 0;              // client: net::Now() of the last request to move time on
bool waitJumps = true;                 // QA negative control: waitoff
bool waitJumped = false;               // the hero's current wait has moved the clock on already
unsigned waits = 0;

// Client: a sleep on this machine jumps its clock; ask the host to move time on for everyone instead of having the
// next update set this clock back. Also checked when an update arrives: one that came in the same frame as the jump,
// before Tick, set the clock back and hid the sleep (stage G2, once).
bool DetectSleep()
{
    const uint32_t local = Entity::GetWorldEntity().Clock.GetTimeStampInSeconds();
    const bool slept = lastLocal && local > lastLocal + SleepJump && following && net::Connected();
    if (slept) {
        net::SendClock(Now());
        ++requests;
        requestedAt = net::Now();
        Logf("clock: slept here (%u game seconds); asking the host to move time on", local - lastLocal);
    }
    lastLocal = local;
    return slept;
}

void Apply(net::ClockState const& to)
{
    Entity world = Entity::GetWorldEntity();
    // Through the clock's own setters (property notifications rebuild its internal clock; static analysis).
    world.Clock.SetYear(to.year);
    world.Clock.SetDay(to.day);
    world.Clock.SetHour(to.hour);
    world.Clock.SetMinute(to.minute);
    lastLocal = world.Clock.GetTimeStampInSeconds();  // our own change, not a sleep
}

net::ClockState Now()
{
    Entity world = Entity::GetWorldEntity();
    net::ClockState state;
    state.timestamp = world.Clock.GetTimeStampInSeconds();
    state.year = static_cast<uint16_t>(world.Clock.GetYear());
    state.day = static_cast<uint16_t>(world.Clock.GetDay());
    state.hour = static_cast<uint8_t>(world.Clock.GetHour());
    state.minute = static_cast<uint8_t>(world.Clock.GetMinute());
    return state;
}

bool Ready() { return gCSession::GetInstance().IsGameRunning() && Entity::GetWorldEntity().GetInstance(); }

// "Until morning" (spell Spl_DuskToDawn, state PS_DuskToDawn): the game runs this machine's clock fast and slows its
// whole simulation (timer.fTimeScale=0.3) until the hour passes 8 (re/findings/p1-p2-p3-static-b.md, P2). In co-op the
// host's clock set a waiting client back, and a waiting host slowed every NPC it simulates. Instead the clock jumps
// to 09:00 at once, as a sleep does: the client asks the host to move time on for everyone, the host tells everyone.
// A start hour below 9 lets the state's loop end on its next frame (factor and time scale back, refills, routine).
void Wait()
{
    Entity hero = Entity::GetPlayer();
    const bool waiting = hero.GetInstance() && !std::strcmp(hero.Routine.GetCurrentState().GetText(), "PS_DuskToDawn_Loop");
    if (!waiting) { waitJumped = false; return; }
    if (waitJumped || !waitJumps || !following || !net::Connected()) return;
    waitJumped = true;
    Entity world = Entity::GetWorldEntity();
    const uint32_t before = world.Clock.GetTimeStampInSeconds();
    const int hour = world.Clock.GetHour();
    hero.PlayerMemory.DuskToDawnStartHour = 0;
    if (hour >= 9) world.Clock.SetDay(world.Clock.GetDay() + 1);
    world.Clock.SetHour(9);
    world.Clock.SetMinute(0);
    lastLocal = world.Clock.GetTimeStampInSeconds();  // our own change, not a sleep
    ++waits;
    Logf("clock: waiting until morning; time moves on %u game seconds to day %d 09:00", lastLocal - before, world.Clock.GetDay());
    if (net::IsHost()) {
        lastSend = 0;  // tell everyone at once
        return;
    }
    net::SendClock(Now());
    ++requests;
    requestedAt = net::Now();
}

void OnClock(net::ClockState const& host)
{
    ++received;
    if (!following || !Ready()) return;
    if (net::IsHost()) {
        // A client slept (or waited): time moves on for everyone, never back.
        const uint32_t own = Entity::GetWorldEntity().Clock.GetTimeStampInSeconds();
        if (host.timestamp <= own) return;
        // A sleep moves on a day at most; anything else is no sleep of this game (Fable's review 22.10. 1.9).
        if (host.hour >= 24 || host.minute >= 60 || host.timestamp - own > 48u * 3600) {
            Logf("clock: a time request out of range (day %u %02u:%02u, %u s ahead); ignored", host.day, host.hour, host.minute, host.timestamp - own);
            return;
        }
        Apply(host);
        ++granted;
        lastSend = 0;  // tell everyone at once
        Logf("clock: a player slept; time moves on to day %u %02u:%02u for everyone", host.day, host.hour, host.minute);
        return;
    }
    if (DetectSleep()) return;
    Entity world = Entity::GetWorldEntity();
    const int local = static_cast<int>(world.Clock.GetTimeStampInSeconds());
    lastDifference = static_cast<int>(host.timestamp) - local;
    if (std::abs(lastDifference) <= Tolerance) return;
    // Ahead of the host right after asking it to move on: its answer is on the way, do not set the clock back.
    if (lastDifference < 0 && requestedAt && net::Now() - requestedAt < 5000) return;
    Apply(host);
    ++corrections;
    Logf("clock: set to the host's day %u %02u:%02u (was %d s off)", host.day, host.hour, host.minute, lastDifference);
}
}

void Start() { net::SetClockHandler(&OnClock); }

void Tick()
{
    if (!Ready()) return;
    Wait();
    if (!net::IsHost()) {
        DetectSleep();
        return;
    }
    const uint32_t now = net::Now();
    if (now - lastSend < SendInterval) return;
    lastSend = now;
    net::SendClock(Now());
}

void Report(LogFn log)
{
    char line[200];
    if (!Ready()) { log("CLOCK none"); return; }
    Entity world = Entity::GetWorldEntity();
    Format(line, "CLOCK timestamp=%u day=%d hour=%d minute=%d corrections=%u received=%u difference=%d following=%d requests=%u granted=%u waits=%u",
              world.Clock.GetTimeStampInSeconds(), world.Clock.GetDay(), world.Clock.GetHour(), world.Clock.GetMinute(), corrections,
              received, lastDifference, following ? 1 : 0, requests, granted, waits);
    log(line);
}

void Set(char const* hhmm, LogFn log)
{
    int hour = 0, minute = 0;
    if (!Ready() || sscanf_s(hhmm, "%d_%d", &hour, &minute) != 2 || hour < 0 || hour > 23 || minute < 0 || minute > 59) {
        log("REFUSED clockset: HH_MM");
        return;
    }
    Entity world = Entity::GetWorldEntity();
    world.Clock.SetHour(hour);
    world.Clock.SetMinute(minute);
    Report(log);
}

void AddDays(char const* days, LogFn log)
{
    const int n = std::atoi(days);
    if (!Ready() || n < 1 || n > 30) {
        log("REFUSED clockdays: 1..30");
        return;
    }
    Entity world = Entity::GetWorldEntity();
    world.Clock.SetDay(world.Clock.GetDay() + n);
    Report(log);
}

void Off(LogFn log)
{
    following = false;
    log("CLOCKOFF ok=1");
}

void WaitOff(LogFn log)
{
    waitJumps = false;
    log("WAITOFF ok=1");
}
}
