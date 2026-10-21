# Checkliste: erster Koop-Abend mit Menschen

[English](PLAYTEST.md) · **Deutsch**

## Worum es geht

Ein Ablauf für den ersten Abend zu zweit (ein Host, ein Mitspieler): Beitritt, gemeinsamer Kampf, Gefolgsmann, Bewusstlosigkeit, Dialog, Quest, Handel und Plündern, getrennte Gegenden, Speichern und Laden, Wiederbeitritt. Dazu, was bei einem Fehler zu notieren ist und wie man die Logs mit `tools\collect_logs.py` einsammelt.

## Vor dem Start

### Host, Mitspieler und Installationen

- [ ] Genau zwei Personen festlegen: ein Host und ein Mitspieler. Der Host startet oder lädt die gemeinsame Kampagne und speichert.
- [ ] Beide verwenden die getestete Grundlage: Gothic 3 von Steam, danach Community Patch **1.75.14**, danach [Parallel Universe Patch **1.1.1**](https://www.worldofgothic.de/dl/download_678.htm), danach dieselbe G3-Together-Build/Protokollversion. Die Kompatibilität ohne PUP ist nicht belegt. Keine andere Koop-Mod im `scripts`-Ordner; wenn möglich eine eigene Installationskopie für den Koop-Abend verwenden. Die [Spielanleitung](SPIELEN.md) beschreibt die Installation.
- [ ] Vorher den Gothic-3-Spielstandordner beider Personen sichern. Während des Abends einen klar benannten Test-Spielstand verwenden.
- [ ] Verbindung wählen: gleiches LAN oder privates VPN. Host-IP/VPN-Adresse und Firewall-Regel nur dem Mitspieler geben. Der Host braucht eingehendes UDP auf Port 34567. Die Verbindung über echtes Internet gilt noch nicht als von Menschen getestet.
- [ ] Beide notieren ihre Rolle und lokale Uhrzeit/Zeitzone für spätere Fehlerberichte.

### INI und Kompatibilität

Datei: `<Gothic-3-Installationsordner>\g3together.ini`.

Host:

```ini
role = host
port = 34567
name = Host
language = de
```

Mitspieler:

```ini
role = client
address = <LAN- oder VPN-Adresse des Hosts>
port = 34567
name = Spieler2
language = de
```

`puppet_template` ist optional; Standardwert ist `PC_Hero`. Fehlt die INI, startet der Code im Host-Modus. Deshalb vor dem Start auf dem Mitspieler-PC `role = client` kontrollieren.

Die Beispiel-INIs verwenden `language = de`, passend zu den unten zitierten Meldungen. Standard ist
`language = en`. Jeder Spieler kann die Sprache unabhängig wählen; sie ändert nur die Mod-Meldungen.

- [ ] Beide verwenden dieselbe Mod-DLL und dieselben Installationsdateien. Der Host prüft Game.dll, Script.dll, Engine.dll, DLLs im `scripts`-Ordner, Regelarchive, Namen/Größen anderer Data-Archive sowie Optionen und den gesamten `[Game]`-Abschnitt aus `Ini\ge3.ini`. Lose Dateien in Data-Unterordnern und eigene Schwierigkeitsschieber deckt diese Prüfung laut README nicht ab.
- [ ] Schwierigkeit, alternatives Balancing, alternative KI, minimale Trefferdauer, XP-Faktor und Quick Loot abgleichen. Bei Join-Ablehnung den Grund in `g3together.log` ansehen.
- [ ] Beginnt der Host ein neues Spiel, beginnt auch der Mitspieler ein neues Spiel. Hat der Host geladen oder gespeichert, tritt der Mitspieler vom Hauptmenü bei und wartet auf den Kampagnentransfer.

## Ablauf für 90 Minuten

| Zeit | Stufe | Schritte und Beobachtung |
|---|---|---|
| 0–8 min | Start und Beitritt | Host lädt oder beginnt die Testkampagne; genau ein Mitspieler tritt bei. Prüfen: beide sehen einander, Namen stimmen, Bewegung kommt an und es gibt keine Kompatibilitätsablehnung. |
| 8–18 min | Gemeinsamer Kampf | Einen kleinen Kampf nahe dem Host führen. Beide greifen dasselbe NPC an. Prüfen: Treffer und Tod erscheinen auf beiden Rechnern; Erfahrung erhält der tatsächliche Killer. Queststatus und Zähler notieren, ohne einen einzelnen Erfolg als Beleg für alle Kill-Quest-Fälle zu werten. |
| 18–28 min | Gefolgsmann anwerben und entlassen | Der Mitspieler wirbt ein geeignetes NPC an. Prüfen: es folgt dem Mitspieler, beide sehen dasselbe NPC und keine Seite simuliert es doppelt. Danach entlässt derselbe Mitspieler es und beide prüfen, dass es nicht weiter als sein Gefolgsmann läuft. |
| 28–38 min | Bewusstlosigkeit und Hilfe | In sicherer Umgebung einen Spieler kontrolliert niederschlagen lassen. Der Partner stellt sich innerhalb von ungefähr 2,5 m daneben und bleibt mindestens 3 Sekunden. Prüfen: Aufhelfen und Meldung, ein Heiltrank des Helfers weniger. Ohne Heiltrank: Hinweis bei beiden, der Bewusstlose bleibt liegen. Optional zum Schluss (danach Spielstand laden): Liegen beide am Boden, sterben beide (Game Over). |
| 38–50 min | Dialog, Quest, Handel und Plündern | Einen Dialog mit einem NPC beginnen, eine bekannte Queststufe auslösen, eine eindeutige Handelsaktion durchführen und einen Gegenstand aus einer Truhe oder Leiche nehmen. Beide vergleichen Quest-/Dialogstatus, Gold und Gegenstandsbestand. Die echte Handelsoberfläche ist nicht durch die Botprüfung abgedeckt. |
| 50–70 min | Abstand, getrennte Regionen und Wiedersehen | Host und Mitspieler entfernen sich deutlich voneinander. Bei mehr als ungefähr 35 m prüfen, dass „Weit weg vom Host“ erscheint und der Host den entfernten Mitspieler erkennt. Während sie getrennt sind, kämpft jeder in seiner jeweiligen Gegend gegen ein eigenes NPC; notieren, ob beide dieselben Ergebnisse sehen und keine doppelte Simulation auffällt. Danach gehen sie wieder unter ungefähr 25 m zusammen und prüfen die Meldung „Wieder beim Host“ sowie den gemeinsamen Zustand. Einen Kill-Quest-Auftrag annehmen und ein Ziel in der Gegend des Mitspielers töten lassen: Der Zähler im Quest-Log des Hosts muss steigen. |
| 70–82 min | Speichern und Laden | Host speichert mit eindeutigem Namen. Prüfen, dass der Mitspieler seinen Helden für diesen Spielstand behält. Für das Laden einen separaten Teststand oder ein Backup verwenden. Nach dem Laden Welt und Spieler stabilisieren lassen und den allgemeinen Zustand vergleichen. Hatte der Mitspieler einen Gefolgsmann: Nach dem Laden und seinem Wiederbeitritt muss der Gefolgsmann ihm wieder folgen. |
| 82–90 min | Wiederbeitritt und Fehlerpaket | Falls noch Zeit ist, einen sauberen Leave und Wiederbeitritt im selben Prozess oder nach Neustart prüfen. Kampagne, Heldenaufzeichnung, NPC-Besitz und Begleiterzustand notieren. Bei Fehlern beide Logpakete nach der folgenden Anleitung erstellen und privat an den Host geben. |

Wenn nur 60 Minuten verfügbar sind: zuerst Beitritt, Kampf, Aufhelfen, Gefolgsmann anwerben/entlassen und Host-Speichern durchführen. Getrennte Regionen und Wiederbeitritt als eigene Folgerunde planen.

## Bei einem Fehler

Jeder Spieler legt für jeden Fehler einen eigenen Eintrag an:

- lokale Uhrzeit mit Zeitzone und Rolle (Host oder Mitspieler);
- Spielwelt/Ort und ungefährer Abstand zwischen Host und Mitspieler;
- letzter Schritt der Checkliste und die genauen Aktionen direkt davor;
- erwartet vs. gesehen, auf welchen Rechnern es auftrat und ob es sich wiederholen ließ;
- ob gerade Dialog, Kampf, Niederschlag, Save/Load oder Join/Leave lief;
- Screenshot oder kurzes Video, wenn es den Zustand zeigt.

Danach kann jeder sein Logpaket mit dem mitgelieferten Sammler erstellen:

1. PowerShell im entpackten Paketordner öffnen (braucht Python).
2. Das Skript mit dem Gothic-3-Installationsordner aufrufen:

   ```powershell
   python "tools\collect_logs.py" "<Gothic-3-Installationsordner>"
   ```

   Liegen die Windows-Dokumente an einem anderen Ort, diesen als zweites Argument angeben:

   ```powershell
   python "tools\collect_logs.py" "<Gothic-3-Installationsordner>" "<Dokumente-Ordner>"
   ```

3. Das Skript meldet den Namen der erzeugten ZIP. Sie entsteht im Paketordner, üblicherweise als `g3together-logs-<Zeitstempel>.zip`.

Gesammelt werden `g3together.log`, `g3together.ini` und gegebenenfalls `g3trace.log` aus dem Spielordner sowie, falls vorhanden, Engine-Logs aus `%USERPROFILE%\Documents\gothic3\`:

- `paru\g3debug.log` (Parallel-Universe-Protokoll, sofern vorhanden);
- `ge3log*.log`;
- `Lastlog_GE3.log`.

Der Dokumente-Ordner ist standardmäßig `%USERPROFILE%\Documents`; ein abweichender Pfad kann als zweites Argument übergeben werden. `collect_logs.py` entfernt Windows-Benutzerpfade, IP-Adressen und die `address`-Zeile aus der INI. Die ZIP trotzdem vor dem Teilen kurz prüfen. Logs und Screenshots zuerst privat an den Host geben, nicht öffentlich posten. Keine INI-Datei, Spielstände, Kampagnen-Sidecars oder ganzen Documents-Ordner weitergeben.

## Bekannte Grenzen

- Alles ist mit Bots getestet. Ein erster kurzer Test mit Menschen (zwei Fenster auf einem PC) fand zwei Fehler, beide sind
  behoben und mit Bots geprüft. Über das echte Internet ist noch nicht getestet.
- Nur zu zweit getestet.
- Gefolgsleute nach dem Laden findet der Host über den Spielernamen; wer seinen Namen ändert, verliert sie.
- Lädt der Host während des Spiels einen anderen Spielstand, müssen sich die Mitspieler trennen und neu beitreten.
- Nicht geprüft: Monster, die auf bewusstlose Helden einschlagen; Ausrauben Bewusstloser; der echte Bett-Dialog; die echte
  Handelsoberfläche (die Bots handeln über Skriptbefehle).
