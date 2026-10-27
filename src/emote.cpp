// SPDX-License-Identifier: GPL-3.0-or-later
#include "emote.h"

#include "body.h"
#include "config.h"
#include "hud.h"
#include "i18n.h"
#include "log.h"
#include "partner.h"
#include "remote.h"

#include <Game.h>
#include <Script.h>
#include <Windows.h>
#include <cmath>
#include <cstdlib>
#include <cstring>

namespace g3t::emote {
namespace {
// The game composes a motion name from an action and a phase for a body's stance and weapon (the combat moves and the
// scripts' _AI_Action go through it). Protected in the SDK; reached through a derived class, never instantiated.
struct SpuAccess : gCScriptProcessingUnit
{
    using Fn = void (gCScriptProcessingUnit::*)(bCString&, eCEntity*, gEAction, bCString, bCString&, GEBool);
    static Fn AniName() { return &SpuAccess::GetAniName; }
};

// Only motions NPCs play in the single-player game (GameEnum.h gEAction). Phases are tried in order; the first the game
// resolves to a name is played. A loop starts with its Begin when the game has one and runs until the hero moves.
struct Emote
{
    char const* key;           // QA and log name
    wchar_t const* names[3];   // en, de, pl
    gEAction action;
    char const* phases[2];
};
const Emote Emotes[] = {
    {"cheer", {L"Cheer", L"Jubeln", L"Wiwatowanie"}, gEAction_Cheer, {"Loop", "Full"}},
    {"applaud", {L"Applaud", L"Klatschen", L"Oklaski"}, gEAction_Applaud, {"Loop", "Full"}},
    {"dance", {L"Dance", L"Tanzen", L"Taniec"}, gEAction_Dance, {"Loop", "Full"}},
    {"sit", {L"Sit down", L"Hinsetzen", L"Siadanie"}, gEAction_SitGround, {"Loop", nullptr}},
    {"kneel", {L"Kneel", L"Knien", L"Klęczenie"}, gEAction_Kneel, {"Loop", nullptr}},
    {"arms", {L"Cross arms", L"Arme verschränken", L"Skrzyżowane ręce"}, gEAction_ArmsCrossed, {"Loop", "Full"}},
    {"think", {L"Think", L"Nachdenken", L"Zamyślenie"}, gEAction_Think, {"Loop", "Full"}},
    {"threaten", {L"Threaten", L"Drohen", L"Groźba"}, gEAction_Say, {"Threaten", nullptr}},
};
constexpr int Count = static_cast<int>(sizeof(Emotes) / sizeof(Emotes[0]));
constexpr uint32_t ChooseMs = 1000;  // without a further press the shown emote plays
constexpr float MoveCm = 30.0f;      // the hero walked off: the emote is over

enum Method { Auto, Spu, Ent };

struct Running
{
    int index = -1;
    char begin[96] = {}, loop[96] = {}, once[96] = {};
    bool inLoop = false;
    float start[3] = {};
    uint32_t nextLook = 0, checkAt = 0;
    char const* method = "-";
    int method0 = Auto;
};
Running running;
body::State heroBody;  // only for the play-ani instruction (its fault flag)
int selected = 0;
bool choosing = false, wasDown = false;
uint32_t lastPress = 0;
unsigned played = 0, refused = 0, loopRestarts = 0;
char lastResult[200] = "-";

gCScriptRoutine_PS* RoutineOf(eCEntity* entity)
{
    return entity ? static_cast<gCScriptRoutine_PS*>(entity->GetPropertySet(eEPropertySetType_ScriptRoutine)) : nullptr;
}

void CallAniName(gCScriptProcessingUnit* spu, eCEntity* entity, int action, char const* phase, GEBool flag, bCString* out, bCString* extra)
{
    (spu->*SpuAccess::AniName())(*out, entity, static_cast<gEAction>(action), bCString(phase), *extra, flag);
}

bool SafeAniName(gCScriptProcessingUnit* spu, eCEntity* entity, int action, char const* phase, GEBool flag, bCString* out, bCString* extra)
{
    __try {
        CallAniName(spu, entity, action, phase, flag, out, extra);
        return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return false;
    }
}

// The motion name the game gives this action and phase for the hero now; false when it has none (or the call faulted).
bool AniName(eCEntity* hero, int action, char const* phase, char* out, size_t size, char* extra = nullptr, size_t extraSize = 0,
             GEBool flag = GEFalse)
{
    *out = '\0';
    gCScriptRoutine_PS* routine = RoutineOf(hero);
    if (!routine || !phase) return false;
    bCString name, other;
    const bool ok = SafeAniName(&routine->GetSPU(), hero, action, phase, flag, &name, &other);
    if (!ok) {
        Logf("emote: GetAniName faulted (action %d, phase %s)", action, phase);
        return false;
    }
    strncpy_s(out, size, name.GetText() ? name.GetText() : "", _TRUNCATE);
    if (extra) strncpy_s(extra, extraSize, other.GetText() ? other.GetText() : "", _TRUNCATE);
    return *out != '\0';
}

wchar_t const* NameOf(int index)
{
    char const* language = i18n::Language();
    return Emotes[index].names[!std::strcmp(language, "de") ? 1 : !std::strcmp(language, "pl") ? 2 : 0];
}

bool Shows(eCEntity* hero, char const* name)
{
    char shows[128];
    return *name && body::CurrentMotion(hero, shows, sizeof(shows)) && !std::strncmp(shows, name, std::strlen(name));
}

char const* StartMotion(eCEntity* hero, char const* name, int method)
{
    if (method != Ent && body::PlayAniInstruction(hero, heroBody, name)) return "spu";
    if (method == Spu) return nullptr;
    Entity(hero).StartPlayAni(bCString(name), 0, GEFalse, 1.0f, GEFalse);
    return "entity";
}

bool Busy(eCEntity* hero, char const** reason)
{
    Entity self(hero);
    if (self.IsDead() || self.IsDown()) { *reason = "down"; return true; }
    if (self.IsSwimming()) { *reason = "swimming"; return true; }
    gCInfoManager_PS* infos = gCInfoManager_PS::GetInstance();
    if (infos && infos->AccessIsRunning()) { *reason = "dialog"; return true; }
    return false;
}

bool Play(int index, int method, char const** reason)
{
    eCEntity* hero = remote::ControlledBody();
    if (!hero || !gCSession::GetInstance().IsGameRunning()) { *reason = "no game"; return false; }
    if (Busy(hero, reason)) return false;
    Emote const& emote = Emotes[index];
    Running next;
    next.index = index;
    next.method0 = method;
    char const* phase = nullptr;
    for (char const* candidate : emote.phases)
        if (candidate && AniName(hero, emote.action, candidate, next.once, sizeof(next.once))) { phase = candidate; break; }
    if (!phase) { *reason = "no motion"; return false; }
    if (!std::strcmp(phase, "Loop")) {
        strcpy_s(next.loop, next.once);
        *next.once = '\0';
        AniName(hero, emote.action, "Begin", next.begin, sizeof(next.begin));
    }
    char const* first = *next.begin ? next.begin : *next.loop ? next.loop : next.once;
    next.inLoop = first == next.loop;
    next.method = StartMotion(hero, first, method);
    if (!next.method) { *reason = "not started"; return false; }
    const bCVector& p = hero->GetWorldPosition();
    next.start[0] = p.GetX(); next.start[1] = p.GetY(); next.start[2] = p.GetZ();
    const uint32_t now = GetTickCount();
    next.nextLook = now + 300;
    next.checkAt = now + 150;
    running = next;
    ++played;
    Format(lastResult, "key=%s phase=%s motion=%.100s method=%s", emote.key, phase, first, next.method);
    Logf("emote: %s", lastResult);
    *reason = "ok";
    return true;
}

void Follow(uint32_t now)
{
    if (running.index < 0) return;
    eCEntity* hero = remote::ControlledBody();
    char const* reason = "";
    if (!hero || !gCSession::GetInstance().IsGameRunning() || Busy(hero, &reason)) { running = Running{}; return; }
    char const* current = running.inLoop ? running.loop : *running.begin ? running.begin : running.once;
    if (running.checkAt && static_cast<int32_t>(now - running.checkAt) >= 0) {
        running.checkAt = 0;
        char shows[128] = "-";
        body::CurrentMotion(hero, shows, sizeof(shows));
        Logf("emote: %s after 150 ms, slot 0 shows %s (%s)", Emotes[running.index].key, shows, Shows(hero, current) ? "ours" : "not ours");
    }
    const bCVector& p = hero->GetWorldPosition();
    const float dx = p.GetX() - running.start[0], dz = p.GetZ() - running.start[2];
    if (std::sqrt(dx * dx + dz * dz) > MoveCm) { running = Running{}; return; }  // walked off: movement has slot 0
    if (static_cast<int32_t>(now - running.nextLook) < 0) return;
    running.nextLook = now + 300;
    const bool ours = Shows(hero, current), runs = body::MotionRunning(hero);
    if (ours && runs) return;
    char shows[128] = "";
    body::CurrentMotion(hero, shows, sizeof(shows));
    // The emote's own motion ended: the hero's stance idle (_Ambient_) took slot 0, or nothing runs. Anything else (a
    // weapon drawn, a hit) ends the emote.
    const bool ended = ours || !runs || std::strstr(shows, "_Ambient_");
    if (!ended || !*running.loop) { running = Running{}; return; }
    running.inLoop = true;
    if (StartMotion(hero, running.loop, running.method0)) ++loopRestarts;
}
}

void Tick()
{
    const uint32_t now = GetTickCount();
    Follow(now);
    if (!gCSession::GetInstance().IsGameRunning() || !partner::Focused()) { wasDown = false; choosing = false; return; }
    const bool down = (GetAsyncKeyState(partner::KeyFromName(GetConfig().emoteKey, VK_HOME)) & 0x8000) != 0;
    if (down && !wasDown) {
        if (choosing) selected = (selected + 1) % Count;  // the first press shows the emote played last
        choosing = true;
        lastPress = now;
        char key[16];
        partner::KeyName(partner::KeyFromName(GetConfig().emoteKey, VK_HOME), key, sizeof(key));
        wchar_t keyName[16] = {}, text[160];
        MultiByteToWideChar(CP_UTF8, 0, key, -1, keyName, 16);
        swprintf_s(text, i18n::Text(i18n::EmoteChoose), NameOf(selected), keyName);
        hud::Message(hud::Info, text);
    }
    wasDown = down;
    if (choosing && now - lastPress >= ChooseMs) {
        choosing = false;
        char const* reason = "";
        if (!Play(selected, Auto, &reason)) {
            ++refused;
            Logf("emote: %s refused (%s)", Emotes[selected].key, reason);
            hud::Message(hud::Warning, i18n::Text(i18n::EmoteNotNow));
        }
    }
}

void Command(char const* argument, LogFn log)
{
    // "cheer" or "cheer:spu" (the QA bot sends one word as the argument)
    char key[32] = {}, how[16] = {};
    if (argument) strncpy_s(key, argument, _TRUNCATE);
    if (char* colon = std::strchr(key, ':')) {
        strncpy_s(how, colon + 1, _TRUNCATE);
        *colon = '\0';
    }
    int index = -1;
    for (int i = 0; i < Count; ++i)
        if (!_stricmp(key, Emotes[i].key)) index = i;
    if (index < 0 && *key >= '0' && *key <= '9') index = std::atoi(key);
    char line[300];
    if (index < 0 || index >= Count) {
        Format(line, "EMOTE ok=0 reason=unknown key=%s", *key ? key : "-");
        log(line);
        return;
    }
    const int method = !_stricmp(how, "spu") ? Spu : !_stricmp(how, "entity") ? Ent : Auto;
    char const* reason = "";
    const bool ok = Play(index, method, &reason);
    if (ok) selected = index;
    Format(line, "EMOTE ok=%d reason=%s %s", ok ? 1 : 0, reason, ok ? lastResult : "");
    log(line);
}

void Names(LogFn log)
{
    eCEntity* hero = remote::ControlledBody();
    if (!hero) { log("EMOTENAMES none (no hero)"); return; }
    // Every action an emote could use, with the phases the game's motions have (gEPhase); Say takes the dialog gestures.
    static const struct { char const* name; gEAction action; } actions[] = {
        {"Cheer", gEAction_Cheer}, {"SuperCheer", gEAction_SuperCheer}, {"Applaud", gEAction_Applaud}, {"Dance", gEAction_Dance},
        {"SitGround", gEAction_SitGround}, {"Kneel", gEAction_Kneel}, {"ArmsCrossed", gEAction_ArmsCrossed},
        {"HandsOnHips", gEAction_HandsOnHips}, {"Think", gEAction_Think}, {"Warn", gEAction_Warn}, {"Pee", gEAction_Pee},
        {"SleepGround", gEAction_SleepGround}, {"WatchFight", gEAction_WatchFight}, {"Ambient", gEAction_Ambient},
        {"Listen", gEAction_Listen}, {"Say", gEAction_Say}};
    static char const* const phases[] = {"Begin", "Loop", "End", "Full", "Hit"};
    static char const* const gestures[] = {"Proud", "Approve", "Thoughtful", "Angry", "Threaten", "Nervous", "Point", "Preach", "Beg", "Sad"};
    unsigned found = 0;
    char line[400];
    for (auto const& entry : actions) {
        const bool say = entry.action == gEAction_Say;
        char const* const* list = say ? gestures : phases;
        const size_t count = say ? sizeof(gestures) / sizeof(*gestures) : sizeof(phases) / sizeof(*phases);
        for (size_t i = 0; i < count; ++i) {
            char name[160], extra[96], name1[160];
            const bool ok = AniName(hero, entry.action, list[i], name, sizeof(name), extra, sizeof(extra));
            AniName(hero, entry.action, list[i], name1, sizeof(name1), nullptr, 0, GETrue);
            if (!ok && !*name1) continue;
            ++found;
            Format(line, "EMOTENAME action=%s phase=%s name=%s extra=%s name1=%s", entry.name, list[i], *name ? name : "-",
                   *extra ? extra : "-", *name1 ? name1 : "-");
            log(line);
        }
    }
    Format(line, "EMOTENAMES end found=%u", found);
    log(line);
}

void State(LogFn log)
{
    eCEntity* hero = remote::ControlledBody();
    char shows[128] = "-";
    if (hero) body::CurrentMotion(hero, shows, sizeof(shows));
    char line[500];
    Format(line, "EMOTESTATE running=%s loop=%d shows=%s run0=%d played=%u refused=%u loops=%u last=%s",
           running.index >= 0 ? Emotes[running.index].key : "-", running.inLoop ? 1 : 0, *shows ? shows : "-",
           hero && body::MotionRunning(hero) ? 1 : 0, played, refused, loopRestarts, lastResult);
    log(line);
}
}
