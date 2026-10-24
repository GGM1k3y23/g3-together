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
    {L"%s is down. Talk to them and choose \"I will help you.\" (costs a healing potion).",
     L"%s liegt am Boden. Sprich ihn an und wähle „Ich werde dir helfen.“ (kostet einen Heiltrank)."},
    {L"%s is down. Talk to them and choose \"I will help you.\" to help them up.",
     L"%s liegt am Boden. Sprich ihn an und wähle „Ich werde dir helfen.“, um aufzuhelfen."},
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
    {L"Your own hero for this campaign could not be loaded (%s is damaged). You wait outside the host's game until the file is repaired.",
     L"Dein eigener Held für diese Kampagne ließ sich nicht laden (%s ist beschädigt). Du wartest außerhalb des Host-Spiels, bis die Datei repariert ist."},
    {L"Your own hero is back. You are in the host's game.", L"Dein eigener Held ist zurück. Du bist im Spiel des Hosts."},
    {L"Connection to the host lost. Reconnecting …", L"Verbindung zum Host verloren. Neuer Versuch läuft …"},
    {L"Connected to the host again.", L"Wieder mit dem Host verbunden."},
    {L"The host refused you: another G3 Together version (yours: protocol %u, host: %u). Both players need the same package.",
     L"Der Host hat dich abgewiesen: andere G3-Together-Version (deine: Protokoll %u, Host: %u). Beide brauchen dasselbe Paket."},
    {L"The host refused you: your game install differs (%s). Both players need the same game version and patches.",
     L"Der Host hat dich abgewiesen: deine Spielinstallation weicht ab (%s). Beide brauchen dieselbe Spielversion und dieselben Patches."},
    {L"The host refused you: %s", L"Der Host hat dich abgewiesen: %s"},
    {L"The host's save did not arrive intact and was not loaded.",
     L"Der Spielstand des Hosts kam nicht vollständig an und wurde nicht geladen."},
    {L"%s waits outside your game: their own hero for this campaign could not be loaded (the file is damaged).",
     L"%s wartet außerhalb deines Spiels: der eigene Held für diese Kampagne ließ sich nicht laden (Datei beschädigt)."},
    {L"%s offers a trade. You give: %s. You get: %s. Accept: %s, decline: %s (%d s).",
     L"%s bietet einen Handel an. Du gibst: %s. Du bekommst: %s. Annehmen: %s, ablehnen: %s (%d s)."},
    {L"%d gold", L"%d Gold"},
    {L"nothing", L"nichts"},
    {L"Trade with %s done.", L"Handel mit %s abgeschlossen."},
    {L"You declined the trade with %s.", L"Du hast den Handel mit %s abgelehnt."},
    {L"The trade offer of %s ran out.", L"Das Handelsangebot von %s ist abgelaufen."},
    {L"Trade with %s not possible: you no longer have %s.", L"Handel mit %s nicht möglich: du hast %s nicht mehr."},
    {L"%s accepted the trade.", L"%s hat den Handel angenommen."},
    {L"%s declined the trade; it is undone.", L"%s hat den Handel abgelehnt; er wird rückgängig gemacht."},
    {L"%s did not answer; the trade is undone.", L"%s hat nicht geantwortet; der Handel wird rückgängig gemacht."},
    {L"%s no longer has everything in the trade; it is undone.", L"%s hat nicht mehr alles aus dem Handel; er wird rückgängig gemacht."},
    {L"%s is deciding on another trade; this one is undone.", L"%s entscheidet gerade über einen anderen Handel; dieser wird rückgängig gemacht."},
    {L"The key %s is set for %s: one press does all of them. Choose different keys in g3together.ini.",
     L"Die Taste %s ist für %s eingestellt: ein Druck löst alles davon aus. Wähle in g3together.ini verschiedene Tasten."},
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
