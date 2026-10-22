# G3 Together: Kurzanleitung für den Mitspieler

## Vor dem Start

- Beide benötigen Gothic 3 aus Steam mit **Community Patch 1.75.14** sowie dieselbe G3-Together-DLL und dieselben Spieldateien.
- Schwierigkeit, alternatives Balancing, alternative KI, XP-Faktor, minimale Trefferdauer, Quick Loot und der Abschnitt `[Game]` in `Ini\ge3.ini` müssen mit dem Host übereinstimmen.
- Im Ordner `scripts` darf keine andere Koop-Mod liegen.

## Installieren

1. Kopiere `Script_G3Together.dll` nach `<Gothic-3-Ordner>\scripts\`.
2. Lege `g3together.ini` in `<Gothic-3-Ordner>\` und trage deine Daten ein:

```ini
role = client
address = <Adresse des Hosts>
port = 34567
name = <Dein Name>
```

Ersetze die beiden Platzhalter. **Über Hamachi** ist die Adresse des Hosts seine Hamachi-Adresse (`25.x.x.x`, steht bei ihm oben im Hamachi-Fenster); beide müssen im selben Hamachi-Netz sein, am besten mit grünem Status. Auf deinem Rechner muss unbedingt `role = client` stehen. Behalte deinen Namen bei:
Gefolgsleute findet der Host nach dem Laden eines Spielstands über diesen Namen wieder.

## Beitreten

Der Host startet Gothic 3 und beginnt eine neue Kampagne oder lädt seine Koop-Kampagne. Starte danach Gothic 3. Hat der Host eine Kampagne geladen oder gespeichert, bleib im Hauptmenü und warte, bis sie übertragen und geladen wurde. Bei einem neuen Spiel startest auch du ein neues Spiel. Lädt der Host später einen Spielstand, wirst du automatisch mitgeladen; du musst nichts tun. Lade selbst keinen eigenen Spielstand, solange du verbunden bist: Dann spielst du in einer anderen Welt, bis du wieder den Stand des Hosts hast.

Wird der Beitritt abgelehnt, öffne im Gothic-3-Ordner `g3together.log` und suche die Zeile `REFUSED by host`. Prüfe Versionen und Spieloptionen.

## Im Spiel

- Wirst du tödlich getroffen, bleib liegen: Ein verbundener Mitspieler in derselben Welt kann dir aufhelfen, wenn er 3 Sekunden höchstens 2,5 m neben dir steht. Das kostet ihn einen Heiltrank; ohne Trank bleibst du liegen, bis er einen hat. Liegen alle am Boden oder bist du allein, stirbst du wie im Einzelspiel (Game Over, Spielstand laden).
- Trag deshalb immer ein paar Heiltränke für deinen Partner bei dir.
- Schlägt dich eine Wache oder ein Duellgegner nur nieder, stehst du wie im Einzelspiel von selbst wieder auf (ausgeraubt wirst du dabei noch nicht).
- Kämpft dein Partner ein Duell oder in der Arena, halte dich heraus: Deine Treffer auf seinen Gegner zählen nicht.
- Ab etwa 35 m vom Host erscheint **„Weit weg vom Host“**; unter etwa 25 m **„Wieder beim Host“**.
- **Bild ab** (Page Down) bringt dich direkt hinter deinen Mitspieler, auch zu ihm, wenn er am Boden liegt. Danach 5 Sekunden
  Pause. Andere Taste: in der INI `partner_key = F9` (F1–F24, PAGEUP, HOME, END, INSERT, DELETE oder ein Buchstabe).
- Gespeichert wird beim Host.
- Was heute noch schiefgehen kann und was ihr dann tut: Abschnitt **„Bekannte Probleme“** in `SPIELEN.md`. Für den
  Spielabend gibt es die Kurzfassung `SPIELABEND.md`.

## Bei einem Fehler

Öffne PowerShell im entpackten Paketordner (dort, wo diese Datei liegt) und führe aus (braucht Python):

```powershell
python "Werkzeuge\collect_logs.py" "<Gothic-3-Ordner>"
```

Schicke die erzeugte Datei `g3together-logs-<Zeitstempel>.zip` an den Host. Sie liegt im Paketordner. Ohne Python: `g3together.log` aus dem Gothic-3-Ordner und die `.log`-Dateien aus
`Dokumente\gothic3` (mit Unterordner `paru`) von Hand schicken.

