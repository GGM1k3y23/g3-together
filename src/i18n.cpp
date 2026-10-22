// SPDX-License-Identifier: GPL-3.0-or-later
#include "i18n.h"

#include "config.h"

#include <cstring>

namespace g3t::i18n {
namespace {
struct Translation { wchar_t const* english; wchar_t const* german; };
constexpr Translation Messages[] = {
    {L"You need a healing potion to help %s up.",
     L"Du brauchst einen Heiltrank, um %s aufzuhelfen."},
    {L"You helped %s up (one healing potion used).",
     L"Du hast %s aufgeholfen (ein Heiltrank verbraucht)."},
    {L"You helped %s up.", L"Du hast %s aufgeholfen."},
    {L"Nobody can help you now.", L"Niemand kann dir mehr helfen."},
    {L"%s is down. Stand next to them with a healing potion to help them up.",
     L"%s liegt am Boden. Stell dich mit einem Heiltrank daneben, um aufzuhelfen."},
    {L"%s is down. Stand next to them to help them up.",
     L"%s liegt am Boden. Stell dich daneben, um aufzuhelfen."},
    {L"%s has died.", L"%s ist gestorben."},
    {L"You are knocked out. A partner with a healing potion can help you up.",
     L"Du bist niedergeschlagen. Ein Mitspieler mit Heiltrank kann dir aufhelfen."},
    {L"You are knocked out. Wait for your partner to help you up.",
     L"Du bist niedergeschlagen. Warte, bis dein Mitspieler dir aufhilft."},
    {L"%s helped you up.", L"%s hat dir aufgeholfen."},
    {L"%s has no healing potion to help you up.",
     L"%s hat keinen Heiltrank, um dir aufzuhelfen."},
    {L"Jumped to your partner.", L"Zum Mitspieler gesprungen."},
    {L"No partner in this world.", L"Kein Mitspieler in dieser Welt."},
    {L"You cannot jump while knocked out.", L"Nicht, solange du am Boden liegst."},
    {L"Wait a moment, then try again.", L"Kurz warten, dann noch einmal."},
    {L"No saved hero for this campaign: you are playing a copy of the host's hero.",
     L"Kein eigener Held f\u00fcr diese Kampagne: du spielst mit einer Kopie des Host-Helden."},
    {L"%s is in another world.", L"%s ist in einer anderen Welt."},
    {L"%s is far away (%d m).", L"%s ist weit entfernt (%d m)."},
    {L"Far from host: This area is now simulated on your PC.",
     L"Weit weg vom Host: Diese Gegend l\u00e4uft jetzt bei dir."},
    {L"%s is nearby again.", L"%s ist wieder in der N\u00e4he."},
    {L"Back near the host.", L"Wieder beim Host."},
    {L"The host is in the main menu. The game goes on once the host loads a game.",
     L"Der Host ist im Hauptmenü. Es geht weiter, sobald er ein Spiel lädt."},
    {L"The host plays a new game without a save. Start a new game too, or wait until the host saves.",
     L"Der Host spielt ein neues Spiel ohne Spielstand. Starte auch ein neues Spiel oder warte, bis der Host speichert."},
    {L"The host loaded a save: you will be loaded into the same save.",
     L"Der Host hat einen Spielstand geladen: du wirst in denselben Stand nachgeladen."},
    {L"You will be loaded into the host's save.", L"Du wirst in den Spielstand des Hosts geladen."},
    {L"A partner is in another world. Save, and they will be loaded into your save.",
     L"Ein Mitspieler ist in einer anderen Welt. Speichere, dann wird er in deinen Stand geladen."},
};
static_assert(sizeof(Messages) / sizeof(Messages[0]) == MessageCount, "Missing HUD translation");
}

wchar_t const* Text(Message message)
{
    if (message < 0 || message >= MessageCount) return L"";
    auto const& translation = Messages[message];
    return std::strcmp(GetConfig().language, "de") == 0 ? translation.german : translation.english;
}
}
