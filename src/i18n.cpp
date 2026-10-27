// SPDX-License-Identifier: GPL-3.0-or-later
#include "i18n.h"

#include "config.h"
#include "log.h"

#include <cstring>

namespace g3t::i18n {
namespace {
// Polish: the other player's name always follows "Gracz"/"gracz" (so the verb agrees with that word, not with an unknown
// gender), and the reader's own past actions are worded without a gendered past tense.
struct Translation { wchar_t const* english; wchar_t const* german; wchar_t const* polish; };
constexpr Translation Messages[] = {
    {L"You need a healing potion to help %s up.",
     L"Du brauchst einen Heiltrank, um %s aufzuhelfen.",
     L"Potrzebujesz mikstury leczniczej, aby pomóc graczowi %s wstać."},
    {L"You helped %s up (one healing potion used).",
     L"Du hast %s aufgeholfen (ein Heiltrank verbraucht).",
     L"Pomagasz graczowi %s wstać (zużyto jedną miksturę leczniczą)."},
    {L"You helped %s up.", L"Du hast %s aufgeholfen.", L"Pomagasz graczowi %s wstać."},
    {L"Nobody can help you now.", L"Niemand kann dir mehr helfen.", L"Nikt już nie może ci pomóc."},
    {L"%s is down. Talk to them and choose \"I will help you.\" (costs a healing potion).",
     L"%s liegt am Boden. Sprich ihn an und wähle „Ich werde dir helfen.“ (kostet einen Heiltrank).",
     L"Gracz %s leży na ziemi. Porozmawiaj z nim i wybierz „Pomogę ci.” (kosztuje miksturę leczniczą)."},
    {L"%s is down. Talk to them and choose \"I will help you.\" to help them up.",
     L"%s liegt am Boden. Sprich ihn an und wähle „Ich werde dir helfen.“, um aufzuhelfen.",
     L"Gracz %s leży na ziemi. Porozmawiaj z nim i wybierz „Pomogę ci.”, aby postawić go na nogi."},
    {L"%s has died.", L"%s ist gestorben.", L"Gracz %s zginął."},
    {L"You are knocked out. A partner with a healing potion can help you up.",
     L"Du bist niedergeschlagen. Ein Mitspieler mit Heiltrank kann dir aufhelfen.",
     L"Powalono cię. Drugi gracz z miksturą leczniczą może pomóc ci wstać."},
    {L"You are knocked out. Wait for your partner to help you up.",
     L"Du bist niedergeschlagen. Warte, bis dein Mitspieler dir aufhilft.",
     L"Powalono cię. Poczekaj, aż drugi gracz pomoże ci wstać."},
    {L"%s helped you up.", L"%s hat dir aufgeholfen.", L"Gracz %s pomógł ci wstać."},
    {L"%s has no healing potion to help you up.",
     L"%s hat keinen Heiltrank, um dir aufzuhelfen.",
     L"Gracz %s nie ma mikstury leczniczej, aby pomóc ci wstać."},
    {L"Jumped to your partner.", L"Zum Mitspieler gesprungen.", L"Przeniesiono do drugiego gracza."},
    {L"No partner in this world.", L"Kein Mitspieler in dieser Welt.", L"W tym świecie nie ma drugiego gracza."},
    {L"You cannot jump while knocked out.", L"Nicht, solange du am Boden liegst.",
     L"Nie możesz się przenieść, dopóki leżysz na ziemi."},
    {L"Wait a moment, then try again.", L"Kurz warten, dann noch einmal.", L"Odczekaj chwilę i spróbuj ponownie."},
    {L"No saved hero for this campaign: you are playing a copy of the host's hero.",
     L"Kein eigener Held für diese Kampagne: du spielst mit einer Kopie des Host-Helden.",
     L"Brak zapisanego bohatera dla tej kampanii: grasz kopią bohatera hosta."},
    {L"%s is in another world.", L"%s ist in einer anderen Welt.", L"Gracz %s jest w innym świecie."},
    {L"%s is far away (%d m).", L"%s ist weit entfernt (%d m).", L"Gracz %s jest daleko (%d m)."},
    {L"Far from host: This area is now simulated on your PC.",
     L"Weit weg vom Host: Diese Gegend läuft jetzt bei dir.",
     L"Daleko od hosta: ta okolica jest teraz symulowana na twoim komputerze."},
    {L"%s is nearby again.", L"%s ist wieder in der Nähe.", L"Gracz %s znów jest w pobliżu."},
    {L"Back near the host.", L"Wieder beim Host.", L"Znów w pobliżu hosta."},
    {L"The host is in the main menu. The game goes on once the host loads a game.",
     L"Der Host ist im Hauptmenü. Es geht weiter, sobald er ein Spiel lädt.",
     L"Host jest w menu głównym. Gra będzie kontynuowana, gdy host wczyta grę."},
    {L"The host plays a new game without a save. Start a new game too, or wait until the host saves.",
     L"Der Host spielt ein neues Spiel ohne Spielstand. Starte auch ein neues Spiel oder warte, bis der Host speichert.",
     L"Host gra w nową grę bez zapisu. Rozpocznij również nową grę albo poczekaj, aż host zapisze grę."},
    {L"The host loaded a save: you will be loaded into the same save.",
     L"Der Host hat einen Spielstand geladen: du wirst in denselben Stand nachgeladen.",
     L"Host wczytał zapis: ten sam zapis zostanie wczytany u ciebie."},
    {L"You will be loaded into the host's save.", L"Du wirst in den Spielstand des Hosts geladen.",
     L"Zapis hosta zostanie wczytany u ciebie."},
    {L"A partner is in another world. Save, and they will be loaded into your save.",
     L"Ein Mitspieler ist in einer anderen Welt. Speichere, dann wird er in deinen Stand geladen.",
     L"Drugi gracz jest w innym świecie. Zapisz grę, a zostanie wczytany do twojego zapisu."},
    {L"Your own hero for this campaign could not be loaded (%s is damaged). You wait outside the host's game until the file is repaired.",
     L"Dein eigener Held für diese Kampagne ließ sich nicht laden (%s ist beschädigt). Du wartest außerhalb des Host-Spiels, bis die Datei repariert ist.",
     L"Nie udało się wczytać twojego bohatera dla tej kampanii (plik %s jest uszkodzony). Czekasz poza grą hosta, dopóki plik nie zostanie naprawiony."},
    {L"Your own hero is back. You are in the host's game.", L"Dein eigener Held ist zurück. Du bist im Spiel des Hosts.",
     L"Twój bohater wrócił. Jesteś w grze hosta."},
    {L"Connection to the host lost. Reconnecting …", L"Verbindung zum Host verloren. Neuer Versuch läuft …",
     L"Utracono połączenie z hostem. Trwa ponowna próba …"},
    {L"Connected to the host again.", L"Wieder mit dem Host verbunden.", L"Ponownie połączono z hostem."},
    {L"The host refused you: another G3 Together version (yours: protocol %u, host: %u). Both players need the same package.",
     L"Der Host hat dich abgewiesen: andere G3-Together-Version (deine: Protokoll %u, Host: %u). Beide brauchen dasselbe Paket.",
     L"Host cię odrzucił: inna wersja G3 Together (twoja: protokół %u, host: %u). Obaj gracze potrzebują tego samego pakietu."},
    {L"The host refused you: your game install differs (%s). Both players need the same game version and patches.",
     L"Der Host hat dich abgewiesen: deine Spielinstallation weicht ab (%s). Beide brauchen dieselbe Spielversion und dieselben Patches.",
     L"Host cię odrzucił: twoja instalacja gry się różni (%s). Obaj gracze potrzebują tej samej wersji gry i tych samych łatek."},
    {L"The host refused you: %s", L"Der Host hat dich abgewiesen: %s", L"Host cię odrzucił: %s"},
    {L"The host's save did not arrive intact and was not loaded.",
     L"Der Spielstand des Hosts kam nicht vollständig an und wurde nicht geladen.",
     L"Zapis hosta nie dotarł w całości i nie został wczytany."},
    {L"%s waits outside your game: their own hero for this campaign could not be loaded (the file is damaged).",
     L"%s wartet außerhalb deines Spiels: der eigene Held für diese Kampagne ließ sich nicht laden (Datei beschädigt).",
     L"Gracz %s czeka poza twoją grą: nie udało się wczytać jego bohatera dla tej kampanii (plik jest uszkodzony)."},
    {L"%s offers a trade. You give: %s. You get: %s. Accept: %s, decline: %s (%d s).",
     L"%s bietet einen Handel an. Du gibst: %s. Du bekommst: %s. Annehmen: %s, ablehnen: %s (%d s).",
     L"Gracz %s proponuje wymianę. Dajesz: %s. Dostajesz: %s. Przyjmij: %s, odrzuć: %s (%d s)."},
    {L"%d gold", L"%d Gold", L"%d szt. złota"},
    {L"nothing", L"nichts", L"nic"},
    {L"Trade with %s done.", L"Handel mit %s abgeschlossen.", L"Wymiana z graczem %s zakończona."},
    {L"You declined the trade with %s.", L"Du hast den Handel mit %s abgelehnt.", L"Odrzucono wymianę z graczem %s."},
    {L"The trade offer of %s ran out.", L"Das Handelsangebot von %s ist abgelaufen.", L"Oferta wymiany od gracza %s wygasła."},
    {L"Trade with %s not possible: you no longer have %s.", L"Handel mit %s nicht möglich: du hast %s nicht mehr.",
     L"Wymiana z graczem %s niemożliwa, brakuje ci już: %s."},
    {L"%s accepted the trade.", L"%s hat den Handel angenommen.", L"Gracz %s przyjął wymianę."},
    {L"%s declined the trade; it is undone.", L"%s hat den Handel abgelehnt; er wird rückgängig gemacht.",
     L"Gracz %s odrzucił wymianę; zostaje ona cofnięta."},
    {L"%s did not answer; the trade is undone.", L"%s hat nicht geantwortet; der Handel wird rückgängig gemacht.",
     L"Gracz %s nie odpowiedział; wymiana zostaje cofnięta."},
    {L"%s no longer has everything in the trade; it is undone.", L"%s hat nicht mehr alles aus dem Handel; er wird rückgängig gemacht.",
     L"Gracz %s nie ma już wszystkiego z wymiany; zostaje ona cofnięta."},
    {L"%s is deciding on another trade; this one is undone.", L"%s entscheidet gerade über einen anderen Handel; dieser wird rückgängig gemacht.",
     L"Gracz %s decyduje właśnie o innej wymianie; ta zostaje cofnięta."},
    {L"The key %s is set for %s: one press does all of them. Choose different keys in g3together.ini.",
     L"Die Taste %s ist für %s eingestellt: ein Druck löst alles davon aus. Wähle in g3together.ini verschiedene Tasten.",
     L"Klawisz %s jest ustawiony dla: %s. Jedno naciśnięcie wywołuje je wszystkie. Wybierz różne klawisze w g3together.ini."},
    {L"G3 Together: host %s:%u not reachable for %u s. Is its game running? Check address and port in g3together.ini and the host's firewall (UDP %u).",
     L"G3 Together: Host %s:%u seit %u s nicht erreichbar. Läuft sein Spiel? Prüfe Adresse und Port in g3together.ini und die Firewall beim Host (UDP %u).",
     L"G3 Together: host %s:%u nieosiągalny od %u s. Czy jego gra jest uruchomiona? Sprawdź adres i port w g3together.ini oraz zaporę sieciową hosta (UDP %u)."},
    {L"Connected to the host.", L"Mit dem Host verbunden.", L"Połączono z hostem."},
    {L"%s robbed you of %d gold.", L"%s hat dir %d Gold abgenommen.", L"%s – zabrano ci %d szt. złota."},
    {L"You were robbed of %d gold.", L"Dir wurden %d Gold abgenommen.", L"Zabrano ci %d szt. złota."},
    {L"%s is using this.", L"%s benutzt das gerade.", L"%s właśnie tego używa."},
    {L"%s is talking to %s.", L"%s spricht gerade mit %s.", L"%s rozmawia właśnie z graczem %s."},
    {L"The other player has started the epilogue. It starts here once your dialog, fight or menu is over.",
     L"Der Mitspieler hat den Epilog begonnen. Er beginnt hier, sobald dein Dialog, Kampf oder Menü vorbei ist.",
     L"Drugi gracz rozpoczął epilog. Rozpocznie się on tutaj po zakończeniu twojej rozmowy, walki lub menu."},
    {L"G3 Together: looking for the host among your Steam friends for %u s. The host must be your Steam friend and run its game with transport = steam.",
     L"G3 Together: Suche den Host seit %u s unter deinen Steam-Freunden. Er muss dein Steam-Freund sein und sein Spiel mit transport = steam laufen.",
     L"G3 Together: od %u s szukam hosta wśród znajomych na Steam. Host musi być twoim znajomym na Steam i mieć uruchomioną grę z transport = steam."},
    {L"G3 Together: connecting to %s over Steam for %u s. Is its game still loading?",
     L"G3 Together: Verbindung zu %s über Steam seit %u s. Lädt sein Spiel noch?",
     L"G3 Together: łączenie z graczem %s przez Steam od %u s. Czy jego gra nadal się wczytuje?"},
    {L"G3 Together: Steam cannot be used (%s). Connecting to the address in g3together.ini instead.",
     L"G3 Together: Steam ist nicht nutzbar (%s). Verbinde stattdessen mit der Adresse aus g3together.ini.",
     L"G3 Together: nie można użyć Steam (%s). Zamiast tego łączenie z adresem z g3together.ini."},
    {L"G3 Together: friends cannot join over Steam (%s). Joining by address still works.",
     L"G3 Together: Freunde können nicht über Steam beitreten (%s). Beitreten über die Adresse geht weiterhin.",
     L"G3 Together: znajomi nie mogą dołączyć przez Steam (%s). Dołączanie przez adres nadal działa."},
};
static_assert(sizeof(Messages) / sizeof(Messages[0]) == MessageCount, "Missing HUD translation");

char resolved[3] = "";
}

char const* LanguageFor(char const* game)
{
    if (!game) return "en";
    if (!_stricmp(game, "German")) return "de";
    if (!_stricmp(game, "Polish")) return "pl";
    return "en";
}

char const* Language()
{
    char const* configured = GetConfig().language;
    if (std::strcmp(configured, "auto")) return configured;
    // Asked at every message: the player can change the game's text language (subtitle language) in the options.
    char const* game = GameTextLanguage();
    if (!game || !*game) return "en";  // the game has not set its language yet
    char const* language = LanguageFor(game);
    if (std::strcmp(language, resolved)) {
        strcpy_s(resolved, language);
        Logf("i18n: language auto, game text language %s -> %s", game, resolved);
    }
    return language;
}

wchar_t const* Text(Message message)
{
    if (message < 0 || message >= MessageCount) return L"";
    auto const& translation = Messages[message];
    char const* language = Language();
    if (!std::strcmp(language, "de")) return translation.german;
    if (!std::strcmp(language, "pl")) return translation.polish;
    return translation.english;
}
}
