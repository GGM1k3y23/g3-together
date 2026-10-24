// SPDX-License-Identifier: GPL-3.0-or-later
#include "dialogaudio.h"

#include "log.h"
#include "net.h"
#include "remote.h"

#include <Engine.h>
#include <Game.h>
#include <Script.h>
#include <Windows.h>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <memory>
#include <new>
#include <string>
#include <vector>

namespace g3t::dialogaudio {
namespace {
// Every spoken line of the game starts in gCScriptProcessingUnit::StartOutput (Game.dll+0x16fc50, after _AI_Say_Ex,
// StartSay and StartSayEx; C dialog-audio-c.md), once per line. Prologue of this build: sub esp,0Ch; push ebx;
// mov ebx,ecx (6 bytes, no relative operands); checked before patching.
constexpr uintptr_t StartOutputRva = 0x16fc50;
constexpr unsigned char StartOutputPrologue[] = {0x83, 0xEC, 0x0C, 0x53, 0x8B, 0xD9};
constexpr float StartRange = 2000.0f, FadeRange = 2500.0f;  // 20 m: a line starts; 25 m: faded out and stopped
// The game's own voice distances (Game.dll+0x307ffc/+0x307ff8), so the line sounds as it does to the speaker.
constexpr float MinDistance = 500.0f, MaxDistance = 1000000.0f;

using StartOutputFn = GEBool(__thiscall*)(gCScriptProcessingUnit*, bCString const&);
StartOutputFn original = nullptr;
bool hooked = false, hookFailed = false, disabled = false;

// The line this machine's dialog is speaking.
struct Speaking {
    uint32_t lineId = 0;
    uint8_t speaker[16] = {};  // looked up by GUID every tick, never kept as a pointer
    DWORD since = 0;
};
Speaking speaking;
uint32_t nextLineId = 1;

// Our own sound and channel. The SDK declares both 0xC bytes, the engine's constructors write 0x10 and 0x14: a plain new
// was too small, the constructors wrote past it and the heap broke; the game crashed when it freed memory later, mostly
// at exit (A's regression 22.10., C dialog-audio-impl-c.md section 3). So: storage of the engine's size, the engine's
// constructors placed into it, stop, then sound and channel destroyed in that order, then the storage freed. Never moved.
struct Audio {
    static_assert(sizeof(eCAudioSound) <= 0x10 && sizeof(eCAudioChannel) <= 0x14, "SDK audio types grew: check the sizes again");
    alignas(8) unsigned char soundStorage[0x10];
    alignas(8) unsigned char channelStorage[0x14];
    eCAudioSound* sound;
    eCAudioChannel* channel;
    Audio() : sound(::new (static_cast<void*>(soundStorage)) eCAudioSound()), channel(::new (static_cast<void*>(channelStorage)) eCAudioChannel()) {}
    ~Audio()
    {
        channel->Stop();
        sound->eCAudioSound::~eCAudioSound();
        channel->eCAudioChannel::~eCAudioChannel();
    }
    Audio(Audio const&) = delete;
    Audio& operator=(Audio const&) = delete;
};

// A line from another player, playing here.
struct Playing {
    uint8_t from = 0;
    uint32_t lineId = 0;
    uint8_t speakerKind = 0;
    uint8_t speaker[16] = {};
    std::unique_ptr<Audio> audio;
};
std::vector<Playing> playing;

unsigned sentStarts = 0, sentEnds = 0, thirdSpeakers = 0, received = 0, played = 0, droppedFar = 0, droppedOwnDialog = 0,
         droppedNoSpeaker = 0, droppedFailed = 0, stoppedFar = 0, finished = 0;
char lastSample[128] = "-";
// Subtitles (owner 24.10.): the line whose text the game's dialog subtitle shows now (GUI2::StartSay, as _AI_Say_Ex does;
// C dialog-listen-c.md). Never stopped while the listener has a dialog of its own: GUI2::StopSay would end that one's.
bool subtitles = true;  // QA negative control: subtitlesoff
uint8_t subtitleFrom = 0;
uint32_t subtitleLine = 0;
unsigned subtitlesShown = 0, subtitlesMissing = 0;
char lastSubtitle[96] = "-";

void GuidOf(eCEntity* entity, uint8_t out[16])
{
    const eCEntityProxy proxy(entity);
    std::memcpy(out, &proxy.GetEntityID().GetCore(), 16);
}

eCEntity* Resolve(uint8_t const guid[16])
{
    _GUID raw;
    std::memcpy(&raw, guid, 16);
    return Entity(bCPropertyID(bCGuid(raw))).GetInstance();
}

gCInfoManager_PS* Infos() { return gCSession::GetInstance().IsGameRunning() ? gCInfoManager_PS::GetInstance() : nullptr; }

bool OwnDialog()
{
    gCInfoManager_PS* infos = Infos();
    return infos && infos->AccessIsRunning();
}

// The text id inside a voice file name. FormatSampleName (Game+0x18bb90, C dialog-audio-c.md) makes
// INFO_<voice>_<speaker's name>_<text id without INFO_>_<audio language>.wav; the speaking machine knows voice and name.
void TextIdOf(eCEntity* speaker, char const* sample, char* out, size_t size)
{
    out[0] = 0;
    std::string s(sample);
    if (s.size() < 10 || _strnicmp(s.c_str(), "INFO_", 5) != 0 || _stricmp(s.c_str() + s.size() - 4, ".wav") != 0) return;
    s = s.substr(5, s.size() - 9);
    const size_t language = s.rfind('_');
    if (language == std::string::npos) return;
    s.resize(language);
    std::string prefix;
    if (auto* npc = static_cast<gCNPC_PS*>(speaker->GetPropertySet(eEPropertySetType_NPC)))
        prefix = std::string(npc->AccessVoice().GetText()) + "_" + speaker->GetName().GetText() + "_";
    if (prefix.size() > 2 && s.size() > prefix.size() && !_strnicmp(s.c_str(), prefix.c_str(), prefix.size())) s = s.substr(prefix.size());
    else if (const size_t cut = s.rfind('_'); cut != std::string::npos) s = s.substr(cut + 1);  // text ids seen have no _
    strncpy_s(out, size, s.c_str(), _TRUNCATE);
}

void HideSubtitle()
{
    if (subtitleLine && !OwnDialog()) gui2.StopSay();
    subtitleFrom = 0;
    subtitleLine = 0;
}

// The line's text in this game's language, as the game shows its own dialog's (when the player has subtitles on).
void ShowSubtitle(uint8_t from, net::DialogLineNote const& note, eCEntity* speaker)
{
    if (!subtitles || !note.text[0] || !eCApplication::GetInstance().GetEngineSetup().ShowSubtitle) return;
    const std::string id = std::string("INFO_") + note.text;
    const bCUnicodeString text = eCLocString(bCString(id.c_str()));
    char utf8[96] = {};
    WideCharToMultiByte(CP_UTF8, 0, reinterpret_cast<wchar_t const*>(text.GetText()), -1, utf8, sizeof(utf8) - 1, nullptr, nullptr);
    if (!text.GetLength() || !std::strcmp(utf8, id.c_str())) {
        ++subtitlesMissing;
        Logf("dialogaudio: line %u from player %u: no text %s here", note.lineId, from, id.c_str());
        return;
    }
    gui2.StartSay(Entity(speaker), text, GEFalse);
    subtitleFrom = from;
    subtitleLine = note.lineId;
    ++subtitlesShown;
    for (char* c = utf8; *c; ++c)
        if (*c == ' ') *c = '_';  // one word for the QA line
    strncpy_s(lastSubtitle, utf8, _TRUNCATE);
}

void SendEnd(uint8_t reason)
{
    if (!speaking.lineId) return;
    net::DialogLineNote note;
    note.kind = net::DialogLineEnd;
    note.reason = reason;
    note.lineId = speaking.lineId;
    net::SendDialogLine(note);
    ++sentEnds;
    speaking = {};
}

void OnLocalLine(gCScriptProcessingUnit* spu, bCString const& sample)
{
    if (disabled || !net::Connected() || !OwnDialog()) return;
    eCEntity* speaker = spu->GetSelfEntity();
    eCEntity* hero = gCSession::GetInstance().GetHero();
    eCEntity* partner = reinterpret_cast<eCEntity*>(Infos()->GetDialogPartnerNPC());
    if (!speaker || (speaker != hero && speaker != partner)) {
        // Version 1 sends the hero and the dialog partner only; a third speaker is counted, for a later version.
        ++thirdSpeakers;
        Logf("dialogaudio: line by %s (not hero or partner) not sent: %s", speaker ? speaker->GetName().GetText() : "-", sample.GetText());
        return;
    }
    SendEnd(2);  // the line before, if still marked, was replaced
    net::DialogLineNote note;
    note.kind = net::DialogLineStart;
    note.lineId = nextLineId++;
    if (speaker == hero) {
        note.speakerKind = net::DialogSpeakerPlayer;
    } else {
        note.speakerKind = net::DialogSpeakerNpc;
        GuidOf(speaker, note.speaker);
    }
    strncpy_s(note.sample, sample.GetText(), _TRUNCATE);
    TextIdOf(speaker, note.sample, note.text, sizeof(note.text));
    net::SendDialogLine(note);
    ++sentStarts;
    strncpy_s(lastSample, note.sample, _TRUNCATE);
    speaking = {};
    speaking.lineId = note.lineId;
    GuidOf(speaker, speaking.speaker);
    speaking.since = GetTickCount();
    Logf("dialogaudio: line %u by %s sent: %s", note.lineId, speaker->GetName().GetText(), note.sample);
}

GEBool __fastcall StartOutput(gCScriptProcessingUnit* spu, void*, bCString const& sample)
{
    const GEBool ok = original(spu, sample);
    if (ok) OnLocalLine(spu, sample);
    return ok;
}

void Hook()
{
    auto* game = reinterpret_cast<unsigned char*>(GetModuleHandleA("Game.dll"));
    if (!game) return;
    unsigned char* target = game + StartOutputRva;
    constexpr size_t stolen = sizeof(StartOutputPrologue);
    if (std::memcmp(target, StartOutputPrologue, stolen) != 0) {
        hookFailed = true;
        Log("dialogaudio: StartOutput not as expected; dialog lines are not shared");
        return;
    }
    auto* trampoline = static_cast<unsigned char*>(VirtualAlloc(nullptr, 32, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE));
    if (!trampoline) { hookFailed = true; return; }
    std::memcpy(trampoline, target, stolen);
    trampoline[stolen] = 0xE9;
    const int32_t back = static_cast<int32_t>((target + stolen) - (trampoline + stolen + 5));
    std::memcpy(trampoline + stolen + 1, &back, 4);
    DWORD old = 0;
    if (!VirtualProtect(target, stolen, PAGE_EXECUTE_READWRITE, &old)) { hookFailed = true; return; }
    target[0] = 0xE9;
    const int32_t to = static_cast<int32_t>(reinterpret_cast<unsigned char*>(&StartOutput) - (target + 5));
    std::memcpy(target + 1, &to, 4);
    target[5] = 0x90;
    VirtualProtect(target, stolen, old, &old);
    FlushInstructionCache(GetCurrentProcess(), target, stolen);
    original = reinterpret_cast<StartOutputFn>(trampoline);
    hooked = true;
    Log("dialogaudio: StartOutput hooked");
}

float Distance(eCEntity* a, eCEntity* b)
{
    const bCVector& p = a->GetWorldPosition();
    const bCVector& q = b->GetWorldPosition();
    const float dx = p.GetX() - q.GetX(), dy = p.GetY() - q.GetY(), dz = p.GetZ() - q.GetZ();
    return std::sqrt(dx * dx + dy * dy + dz * dz);
}

eCEntity* SpeakerOf(uint8_t from, uint8_t kind, uint8_t const guid[16])
{
    if (kind == net::DialogSpeakerPlayer) return remote::PuppetOf(from);
    if (kind == net::DialogSpeakerNpc) return Resolve(guid);
    return nullptr;
}

void Stop(Playing& line)
{
    if (line.from == subtitleFrom && line.lineId == subtitleLine) HideSubtitle();
    line.audio.reset();
}

void StopAll()
{
    for (auto& line : playing) Stop(line);
    playing.clear();
}

// A voice file name from the network: a bare resource name ending in .wav, nothing that names a path.
bool PlainSample(char const* sample)
{
    const size_t length = std::strlen(sample);
    if (length < 5 || _stricmp(sample + length - 4, ".wav") != 0 || std::strstr(sample, "..")) return false;
    for (char const* c = sample; *c; ++c)
        if (*c == '\\' || *c == '/' || *c == ':' || static_cast<unsigned char>(*c) < 32) return false;
    return true;
}

void OnLine(uint8_t from, net::DialogLineNote const& note)
{
    if (disabled) return;
    ++received;
    if (note.kind == net::DialogLineEnd) {
        for (auto it = playing.begin(); it != playing.end(); ++it)
            if (it->from == from && it->lineId == note.lineId) {
                Stop(*it);
                playing.erase(it);
                ++finished;
                break;
            }
        return;
    }
    if (note.kind != net::DialogLineStart) return;
    if (!PlainSample(note.sample)) {
        ++droppedFailed;
        Logf("dialogaudio: line %u from player %u refused: not a plain voice file name", note.lineId, from);
        return;
    }
    // One line per sender: a new one replaces what is left of the one before.
    for (auto it = playing.begin(); it != playing.end(); ++it)
        if (it->from == from) {
            Stop(*it);
            playing.erase(it);
            break;
        }
    eCEntity* hero = gCSession::GetInstance().GetHero();
    eCEntity* speaker = SpeakerOf(from, note.speakerKind, note.speaker);
    if (!hero || !speaker) {
        ++droppedNoSpeaker;
        Logf("dialogaudio: line %u from player %u: speaker not here", note.lineId, from);
        return;
    }
    if (OwnDialog()) {  // the player's own dialog comes first
        ++droppedOwnDialog;
        return;
    }
    if (Distance(hero, speaker) > StartRange) {
        ++droppedFar;
        return;
    }
    Playing line;
    line.from = from;
    line.lineId = note.lineId;
    line.speakerKind = note.speakerKind;
    std::memcpy(line.speaker, note.speaker, 16);
    line.audio = std::make_unique<Audio>();
    const GEBool ok = eCAudioModule::GetInstance().PlayStream3D(eEAudioChannelGroup_Voice, bCString(note.sample), speaker->GetWorldPosition(),
                                                                1.0f, MinDistance, MaxDistance, line.audio->sound, line.audio->channel);
    if (!ok || !line.audio->channel->IsPlaying()) {
        ++droppedFailed;
        Logf("dialogaudio: line %u from player %u not played (ok=%d): %s", note.lineId, from, ok ? 1 : 0, note.sample);
        Stop(line);
        return;
    }
    ++played;
    strncpy_s(lastSample, note.sample, _TRUNCATE);
    Logf("dialogaudio: line %u from player %u plays at %s: %s", note.lineId, from, speaker->GetName().GetText(), note.sample);
    ShowSubtitle(from, note, speaker);
    playing.push_back(std::move(line));
}

void TickSpeaking()
{
    if (!speaking.lineId) return;
    if (!OwnDialog()) {
        SendEnd(3);
        return;
    }
    // A short grace after the start; then the line is over when the speaker's output stops (finished, skipped, or a
    // full stop), whichever way it ended.
    eCEntity* speaker = Resolve(speaking.speaker);
    auto* routine = speaker ? static_cast<gCScriptRoutine_PS*>(speaker->GetPropertySet(eEPropertySetType_ScriptRoutine)) : nullptr;
    if (GetTickCount() - speaking.since > 300 && (!routine || !routine->GetSPU().IsOutputPlaying())) SendEnd(1);
}

void TickPlaying()
{
    if (playing.empty()) return;
    eCEntity* hero = gCSession::GetInstance().GetHero();
    if (!hero || !net::Connected() || OwnDialog()) {
        if (OwnDialog()) subtitleLine = 0;  // the player's own dialog shows its own text now: never stop that one
        StopAll();
        return;
    }
    for (auto it = playing.begin(); it != playing.end();) {
        // A sender that left: its line ends here too (a host stays connected when a client goes; C 22.10.).
        eCEntity* speaker = it->from < net::MaxPlayers && net::Remotes()[it->from].connected
                                ? SpeakerOf(it->from, it->speakerKind, it->speaker) : nullptr;
        const float distance = speaker ? Distance(hero, speaker) : FadeRange + 1.0f;
        if (!speaker || distance > FadeRange || !it->audio->channel->IsPlaying()) {
            if (speaker && distance > FadeRange) ++stoppedFar;
            else ++finished;
            Stop(*it);
            it = playing.erase(it);
            continue;
        }
        it->audio->channel->SetLocation(speaker->GetWorldPosition());
        it->audio->channel->SetVolume(distance <= StartRange ? 1.0f : (FadeRange - distance) / (FadeRange - StartRange));
        ++it;
    }
}
}

void Start() { net::SetDialogLineHandler(&OnLine); }

void OnWorldChange()
{
    // Before a load and before the game shuts down (the world goes first): no channel of ours outlives the audio system.
    StopAll();
    speaking = {};
}

void Tick()
{
    if (!gCSession::GetInstance().IsGameRunning()) {
        StopAll();
        speaking = {};
        return;
    }
    if (!hooked && !hookFailed) Hook();
    TickSpeaking();
    TickPlaying();
}

void Report(LogFn log)
{
    char line[600];
    sprintf_s(line,
              "DIALOGAUDIO hooked=%d sent_starts=%u sent_ends=%u third_speakers=%u received=%u played=%u playing=%zu finished=%u "
              "stopped_far=%u dropped_far=%u dropped_own_dialog=%u dropped_no_speaker=%u dropped_failed=%u subtitles=%u subtitles_missing=%u "
              "subtitle_on=%d last=%s last_subtitle=%s",
              hooked, sentStarts, sentEnds, thirdSpeakers, received, played, playing.size(), finished, stoppedFar, droppedFar,
              droppedOwnDialog, droppedNoSpeaker, droppedFailed, subtitlesShown, subtitlesMissing,
              subtitles && eCApplication::GetInstance().GetEngineSetup().ShowSubtitle ? 1 : 0, lastSample, lastSubtitle);
    log(line);
}

void Disable(LogFn log)
{
    disabled = true;
    StopAll();
    log("DIALOGAUDIOOFF ok=1");
}

void SubtitlesOff(LogFn log)
{
    subtitles = false;
    log("SUBTITLESOFF ok=1");
}

void MuteGame(char const* on, LogFn log)
{
    eCAudioChannelGroup* master = eCAudioModule::GetInstance().GetChannelGroup(eEAudioChannelGroup_Master);
    const bool mute = on && *on == '1';
    if (master) master->SetVolume(mute ? 0.0f : 1.0f);
    char line[80];
    sprintf_s(line, "MUTEGAME ok=%d muted=%d", master ? 1 : 0, mute ? 1 : 0);
    log(line);
}
}
