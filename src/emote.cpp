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
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace g3t::emote {
namespace {
// Motion names exactly as the game's _compiledAnimation.pak has them for the hero's skeleton (human NPCs share it), without
// .xmot. The game's own name composer (gCScriptProcessingUnit::GetAniName) gave only a stem without direction and
// variant (..._Cheer_Begin_N__00_), which played nothing (stageEmote 27.10. 16:38). Only standing motions: sitting or
// kneeling change the hero's stance, and getting up again is a stance change of its own.
#define HERO_STAND "Hero_Stand_None_None_P0_"
struct Emote
{
    char const* key;          // QA and log name
    wchar_t const* names[3];  // en, de, pl
    char const* begin;        // played first
    char const* loop;         // then repeated until the hero walks off; nullptr: the emote ends with its begin
};
const Emote Emotes[] = {
    {"cheer", {L"Cheer", L"Jubeln", L"Wiwatowanie"}, HERO_STAND "SuperCheer_Begin_N_Fwd_00_%_01_P0_0", nullptr},
    {"applaud", {L"Applaud", L"Klatschen", L"Oklaski"}, HERO_STAND "Applaud_Begin_O_Fwd_00_%_00_P0_0",
     HERO_STAND "Applaud_Loop_O_Fwd_00_%_00_P0_0"},
    {"dance", {L"Dance", L"Tanzen", L"Taniec"}, HERO_STAND "Dance_Begin_N_Fwd_00_%_00_P0_0", HERO_STAND "Dance_Loop_N_Fwd_00_%_01_P0_0"},
    {"arms", {L"Cross arms", L"Arme verschränken", L"Skrzyżowane ręce"}, HERO_STAND "ArmsCrossed_Begin_N_Fwd_00_%_00_P0_0",
     HERO_STAND "ArmsCrossed_Loop_N_Fwd_00_%_01_P0_0"},
    {"hips", {L"Hands on hips", L"Hände in die Hüften", L"Ręce na biodrach"}, HERO_STAND "HandsOnHips_Begin_N_Fwd_00_%_00_P0_0",
     HERO_STAND "HandsOnHips_Loop_N_Fwd_00_%_01_P0_0"},
    {"think", {L"Think", L"Nachdenken", L"Zamyślenie"}, HERO_STAND "Think_Begin_N_Fwd_00_%_00_P0_0", HERO_STAND "Think_Loop_N_Fwd_00_%_00_P0_0"},
    {"approve", {L"Nod", L"Zustimmen", L"Zgoda"}, HERO_STAND "Say_Approve_N_Fwd_00_%_00_P0_0", nullptr},
    {"point", {L"Point", L"Zeigen", L"Wskazanie"}, HERO_STAND "Say_Point_N_Fwd_00_%_00_P0_0", nullptr},
    {"threaten", {L"Threaten", L"Drohen", L"Groźba"}, HERO_STAND "Say_Threaten_N_Fwd_00_%_00_P0_0", nullptr},
    {"pee", {L"Pee", L"Pinkeln", L"Sikanie"}, HERO_STAND "Pee_Begin_N_Fwd_00_%_00_P0_0", HERO_STAND "Pee_Loop_N_Fwd_00_%_00_P0_0"},
};
constexpr int Count = static_cast<int>(sizeof(Emotes) / sizeof(Emotes[0]));
constexpr uint32_t ChooseMs = 1000;  // without a further press the shown emote plays
constexpr float MoveCm = 30.0f;      // the hero walked off: the emote is over

// How a motion starts on the hero: as a play-ani instruction on its script processing unit (the way the game's scripts
// play gestures), with Entity::StartPlayAni, or as an overlay (the _O_ motions are upper-body ones). QA chooses; Auto
// tries the instruction first.
enum Method { Auto, Spu, Ent, Overlay };

struct Running
{
    int index = -1;        // -1 with a QA raw motion
    char begin[96] = {}, loop[96] = {};
    bool inLoop = false;
    float start[3] = {};
    uint32_t nextLook = 0, checkAt = 0;
    int method = Auto;
    bool active = false;
};
Running running;
body::State heroBody;  // only for the play-ani instruction (its fault flag)
int selected = 0;
bool choosing = false, wasDown = false;
uint32_t lastPress = 0;
unsigned played = 0, refused = 0, loopRestarts = 0;
char lastResult[220] = "-";

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
    if (method == Overlay) {
        Entity(hero).StartPlayOverlayAni(bCString(name), 0, GEFalse, 1.0f);
        return "overlay";
    }
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

bool Start(int index, char const* begin, char const* loop, int method, char const** reason)
{
    eCEntity* hero = remote::ControlledBody();
    if (!hero || !gCSession::GetInstance().IsGameRunning()) { *reason = "no game"; return false; }
    if (Busy(hero, reason)) return false;
    Running next;
    next.index = index;
    next.method = method;
    strncpy_s(next.begin, begin, _TRUNCATE);
    if (loop) strncpy_s(next.loop, loop, _TRUNCATE);
    char const* how = StartMotion(hero, next.begin, method);
    if (!how) { *reason = "not started"; return false; }
    const bCVector& p = hero->GetWorldPosition();
    next.start[0] = p.GetX(); next.start[1] = p.GetY(); next.start[2] = p.GetZ();
    const uint32_t now = GetTickCount();
    next.nextLook = now + 300;
    next.checkAt = now + 150;
    next.active = true;
    running = next;
    ++played;
    Format(lastResult, "key=%s motion=%.100s method=%s", index >= 0 ? Emotes[index].key : "raw", next.begin, how);
    Logf("emote: %s", lastResult);
    *reason = "ok";
    return true;
}

void Follow(uint32_t now)
{
    if (!running.active) return;
    eCEntity* hero = remote::ControlledBody();
    char const* reason = "";
    if (!hero || !gCSession::GetInstance().IsGameRunning() || Busy(hero, &reason)) { running = Running{}; return; }
    char const* current = running.inLoop ? running.loop : running.begin;
    if (running.checkAt && static_cast<int32_t>(now - running.checkAt) >= 0) {
        running.checkAt = 0;
        char shows[128] = "-";
        body::CurrentMotion(hero, shows, sizeof(shows));
        Logf("emote: %s after 150 ms, slot 0 shows %s (%s)", running.index >= 0 ? Emotes[running.index].key : "raw", shows,
             Shows(hero, current) ? "ours" : "not ours");
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
    if (StartMotion(hero, running.loop, running.method)) ++loopRestarts;
}

int MethodOf(char const* how)
{
    return !_stricmp(how, "spu") ? Spu : !_stricmp(how, "entity") ? Ent : !_stricmp(how, "overlay") ? Overlay : Auto;
}

// "<word>[:spu|:entity|:overlay]" from the QA bot's one-word argument
void Split(char const* argument, char* word, size_t size, char* how, size_t howSize)
{
    strncpy_s(word, size, argument ? argument : "", _TRUNCATE);
    *how = '\0';
    if (char* colon = std::strrchr(word, ':')) {
        strncpy_s(how, howSize, colon + 1, _TRUNCATE);
        *colon = '\0';
    }
}
}

void Tick()
{
    const uint32_t now = GetTickCount();
    Follow(now);
    if (!gCSession::GetInstance().IsGameRunning() || !partner::Focused()) { wasDown = false; choosing = false; return; }
    const int code = partner::KeyFromName(GetConfig().emoteKey, VK_HOME);
    const bool down = (GetAsyncKeyState(code) & 0x8000) != 0;
    if (down && !wasDown) {
        if (choosing) selected = (selected + 1) % Count;  // the first press shows the emote played last
        choosing = true;
        lastPress = now;
        char key[16];
        partner::KeyName(code, key, sizeof(key));
        wchar_t keyName[16] = {}, text[160];
        MultiByteToWideChar(CP_UTF8, 0, key, -1, keyName, 16);
        swprintf_s(text, i18n::Text(i18n::EmoteChoose), NameOf(selected), keyName);
        hud::Message(hud::Info, text);
    }
    wasDown = down;
    if (choosing && now - lastPress >= ChooseMs) {
        choosing = false;
        char const* reason = "";
        if (!Start(selected, Emotes[selected].begin, Emotes[selected].loop, Auto, &reason)) {
            ++refused;
            Logf("emote: %s refused (%s)", Emotes[selected].key, reason);
            hud::Message(hud::Warning, i18n::Text(i18n::EmoteNotNow));
        }
    }
}

void Command(char const* argument, LogFn log)
{
    char key[64], how[16];
    Split(argument, key, sizeof(key), how, sizeof(how));
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
    char const* reason = "";
    const bool ok = Start(index, Emotes[index].begin, Emotes[index].loop, MethodOf(how), &reason);
    if (ok) selected = index;
    Format(line, "EMOTE ok=%d reason=%s %s", ok ? 1 : 0, reason, ok ? lastResult : "");
    log(line);
}

void Raw(char const* argument, LogFn log)
{
    // The argument is at most 63 characters: a name without "Hero_" is taken as a standing motion of the hero's skeleton.
    char word[64], how[16], name[96];
    Split(argument, word, sizeof(word), how, sizeof(how));
    Format(name, "%s%s", std::strncmp(word, "Hero_", 5) ? HERO_STAND : "", word);
    char const* reason = "";
    const bool ok = *word && Start(-1, name, nullptr, MethodOf(how), &reason);
    char line[300];
    Format(line, "EMOTE ok=%d reason=%s %s", ok ? 1 : 0, *word ? reason : "no name", ok ? lastResult : "");
    log(line);
}

void State(LogFn log)
{
    eCEntity* hero = remote::ControlledBody();
    char shows[128] = "-";
    if (hero) body::CurrentMotion(hero, shows, sizeof(shows));
    char line[500];
    Format(line, "EMOTESTATE running=%s loop=%d shows=%s run0=%d played=%u refused=%u loops=%u last=%s",
           !running.active ? "-" : running.index >= 0 ? Emotes[running.index].key : "raw", running.inLoop ? 1 : 0,
           *shows ? shows : "-", hero && body::MotionRunning(hero) ? 1 : 0, played, refused, loopRestarts, lastResult);
    log(line);
}
}
