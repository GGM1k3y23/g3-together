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
- [ ] Optional: statt von Hand mit `Einrichten.cmd` (Polnisch: `Instalacja.cmd`) aus dem Paket einrichten und notieren, was dabei schiefging. Es ist bisher
  nur an Testordnern ausprobiert.

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

`puppet_template` ist optional; Standardwert ist `PC_Hero`. Ebenfalls optional ist `look = <Charaktervorlage>` (zum Beispiel `look = Diego`): So sieht der andere Spieler dich. `offer_accept_key` und `offer_decline_key` (Standard Einfg und Entf) beantworten ein Handelsangebot. Fehlt die INI, startet der Code im Host-Modus. Deshalb vor dem Start auf dem Mitspieler-PC `role = client` kontrollieren.

Die Beispiel-INIs verwenden `language = de`, passend zu den unten zitierten Meldungen. Ohne die Zeile sind die
Meldungen englisch; `language = pl` gibt polnische, `language = auto` (schreibt das Einrichten) folgt der Textsprache
des Spiels. Jeder Spieler kann die Sprache unabhängig wählen; sie ändert nur die Mod-Meldungen.

- [ ] Beide verwenden dieselbe Mod-DLL und dieselben Installationsdateien. Der Host prüft Game.dll, Script.dll, Engine.dll, DLLs im `scripts`-Ordner, Regelarchive, Namen/Größen anderer Data-Archive sowie Optionen und den gesamten `[Game]`-Abschnitt aus `Ini\ge3.ini`. Lose Dateien in Data-Unterordnern und eigene Schwierigkeitsschieber deckt diese Prüfung laut README nicht ab.
- [ ] Schwierigkeit, alternatives Balancing, alternative KI, minimale Trefferdauer, XP-Faktor und Quick Loot abgleichen. Bei Join-Ablehnung den Grund in `g3together.log` ansehen.
- [ ] Beginnt der Host ein neues Spiel, beginnt auch der Mitspieler ein neues Spiel. Hat der Host geladen oder gespeichert, tritt der Mitspieler vom Hauptmenü bei und wartet auf den Kampagnentransfer.

## Ablauf für 90 Minuten

| Zeit | Stufe | Schritte und Beobachtung |
|---|---|---|
| 0–8 min | Start und Beitritt | Host lädt oder beginnt die Testkampagne; genau ein Mitspieler tritt bei. Prüfen: beide sehen einander, Namen stimmen, Bewegung kommt an und es gibt keine Kompatibilitätsablehnung. Jeder wechselt einmal Rüstung oder Helm: Der andere sieht den Wechsel nach wenigen Sekunden. Läuft ein Spieler los, bewegen sich seine Beine beim anderen sofort, statt dass er gleitet. Karte öffnen: Der Partner ist mit Namen eingezeichnet; auf dem Kompass zeigt sein Zeichen in seine Richtung. |
| 8–18 min | Gemeinsamer Kampf | Einen kleinen Kampf nahe dem Host führen. Beide greifen dasselbe NPC an. Prüfen: Treffer und Tod erscheinen auf beiden Rechnern; Erfahrung erhält der tatsächliche Killer. Queststatus und Zähler notieren, ohne einen einzelnen Erfolg als Beleg für alle Kill-Quest-Fälle zu werten. |
| 18–28 min | Gefolgsmann anwerben und entlassen | Der Mitspieler wirbt ein geeignetes NPC an. Prüfen: es folgt dem Mitspieler, beide sehen dasselbe NPC und keine Seite simuliert es doppelt. Danach entlässt derselbe Mitspieler es und beide prüfen, dass es nicht weiter als sein Gefolgsmann läuft. |
| 28–38 min | Bewusstlosigkeit und Hilfe | In sicherer Umgebung einen Spieler kontrolliert niederschlagen lassen. Der Partner stellt sich zuerst ein paar Sekunden daneben: Es darf nichts passieren. Dann spricht er ihn an und wählt „Ich werde dir helfen.“. Prüfen: Aufhelfen und Meldung, ein Heiltrank des Helfers weniger. Ohne Heiltrank: Hinweis bei beiden, der Bewusstlose bleibt liegen. Optional zum Schluss (danach Spielstand laden): Liegen beide am Boden, sterben beide (Game Over). |
| 38–50 min | Dialog, Quest, Handel und Plündern | Einen Dialog mit einem NPC beginnen, eine bekannte Queststufe auslösen, eine eindeutige Handelsaktion durchführen und einen Gegenstand aus einer Truhe oder Leiche nehmen. Beide vergleichen Quest-/Dialogstatus, Gold und Gegenstandsbestand. Danach miteinander handeln, mit der Maus: Ein Spieler spricht die Figur des anderen an und wählt „Zeig mir deine Ware!“ (beim allerersten Handel den Tipp mit Ok schließen), nimmt einen Gegenstand, gibt einen und schließt das Fenster; der andere nimmt mit Einfg an. Gegenstände und Gold auf beiden Rechnern vergleichen. Noch einmal handeln und mit Entf ablehnen: Es darf nichts den Besitzer wechseln. Die Maussteuerung des Handelsfensters konnten die Bots nicht prüfen. |
| 50–70 min | Abstand, getrennte Regionen und Wiedersehen | Host und Mitspieler entfernen sich deutlich voneinander. Bei mehr als ungefähr 35 m prüfen, dass „Weit weg vom Host“ erscheint und der Host den entfernten Mitspieler erkennt. Während sie getrennt sind, kämpft jeder in seiner jeweiligen Gegend gegen ein eigenes NPC; notieren, ob beide dieselben Ergebnisse sehen und keine doppelte Simulation auffällt. Danach gehen sie wieder unter ungefähr 25 m zusammen und prüfen die Meldung „Wieder beim Host“ sowie den gemeinsamen Zustand. Einen Kill-Quest-Auftrag annehmen und ein Ziel in der Gegend des Mitspielers töten lassen: Der Zähler im Quest-Log des Hosts muss steigen. |
| 70–82 min | Speichern und Laden | Host speichert mit eindeutigem Namen. Prüfen, dass der Mitspieler seinen Helden für diesen Spielstand behält. Für das Laden einen separaten Teststand oder ein Backup verwenden. Nach dem Laden Welt und Spieler stabilisieren lassen und den allgemeinen Zustand vergleichen. Hatte der Mitspieler einen Gefolgsmann: Nach dem Laden und seinem Wiederbeitritt muss der Gefolgsmann ihm wieder folgen. |
| 82–90 min | Wiederbeitritt und Fehlerpaket | Falls noch Zeit ist, einen sauberen Leave und Wiederbeitritt im selben Prozess oder nach Neustart prüfen. Kampagne, Heldenaufzeichnung, NPC-Besitz und Begleiterzustand notieren. Bei Fehlern beide Logpakete nach der folgenden Anleitung erstellen und privat an den Host geben. |

Wenn nur 60 Minuten verfügbar sind: zuerst Beitritt, Kampf, Aufhelfen, Gefolgsmann anwerben/entlassen und Host-Speichern durchführen. Getrennte Regionen und Wiederbeitritt als eigene Folgerunde planen.

### Optional: 30 Minuten mehr für die Neuheiten von 0.3.0

| Abschnitt | Schritte und Beobachtungen |
|---|---|
| Host nicht erreichbar | Vor dem Abend startet der Mitspieler einmal mit falscher Adresse in der INI: Nach etwa 15 Sekunden steht in der obersten Zeile des Hauptmenüs, dass der Host nicht erreichbar ist, mit dieser Adresse und dem Port. |
| Mitgehörter Dialog | Einer spricht mit einem NPC, der andere steht höchstens 20 m entfernt und hat die Untertitel des Spiels an: Er hört die Zeilen und liest sie oben im Bild, in seiner eigenen Spielsprache. |
| Diebstahl vor Zeugen | Der Mitspieler nimmt einen Gegenstand, der einer Stadt gehört, während eine Wache oder ein Bürger aus dem Gebiet des Hosts zusieht: Dieser NPC geht auf den Mitspieler los, wie er es beim Host täte. |
| Ausgeraubt nach Niederschlag | Eine Wache schlägt den Mitspieler bewusstlos (nicht tot): Er steht nach einer Weile von selbst auf, die Wache nimmt die Hälfte seines Goldes, und er sieht „<Name> hat dir N Gold abgenommen.“. Der Host kann ihn währenddessen nicht ausplündern. |
| Ruf `together` | Nur wenn der Host `reputation = together` einstellt: Nach dem Diebstahl oben weist derselbe NPC auch den Host ab. |
### Optional: 20 Minuten mehr für die Neuheiten von 0.4.0

| Abschnitt | Schritte und Beobachtungen |
|---|---|
| Prügelei | Der Mitspieler verprügelt einen Bürger aus dem Gebiet des Hosts, bis er fällt: Der Bürger ist bewusstlos, nicht tot, der Mitspieler bekommt die Erfahrung, der Mordzähler der Stadt bleibt gleich. Ein Schlag auf den am Boden Liegenden tötet ihn. |
| Mord | Nach so einer Tötung in einer Stadt, die nicht flieht, gilt sie als Mord (Wachen reagieren, die Tat taucht im Ruf-Gespräch auf), egal welcher Spieler getötet hat. |
| Taschendiebstahl | Einer bestiehlt einen Bürger (braucht das Talent): Der andere kann denselben Bürger nicht mehr bestehlen. |
| Sitzplatz | Einer setzt sich auf einen Hocker oder eine Bank, der andere versucht denselben Platz: „<Name> benutzt das gerade.“, und er setzt sich nicht. Steht der Erste auf, kann der andere sich setzen. |
| Tiergestalt | Ein Spieler in Wolfsgestalt (Zauber oder Trank) kämpft gegen einen Gegner: Treffer und Kill zählen für diesen Spieler, Treffer des Gegners verletzen den Wolf. |
| Seelenwanderung | Einer zaubert Seelenwanderung in der Nähe von Gegnern und lässt den Geist zu ihnen schweben. Bekannt: Der andere sieht den Geist sich bewegen, nicht den zurückgelassenen Körper. Notieren, ob die Gegner den Geist angreifen und was mit dem Körper geschieht. |
| Seltene Orte | Wer auf eine Innos- oder Beliar-Statue zum Beten stößt: Einer benutzt sie, der andere schaut zu, dann versucht es der andere. Notieren, was jeder sieht. Den Orkfelsen in Faring (Stärkeprobe; hebt ihn einer, gilt es für beide) und Piratenschätze (einer liegt 46 m von Tarok in Faring, mit Schaufel graben; die Beute gibt es einmal) haben die Bots geprüft. |

### Optional: die Neuheiten von 0.5.0 und Szenen, die Bots nicht aufbauen können

| Abschnitt | Schritte und Beobachtungen |
|---|---|
| Zeugen | Der Mitspieler verprügelt in einer Stadt aus dem Gebiet des Hosts einen Bürger, während andere zusehen: Zuschauer und Wachen greifen schon beim Kampf ein, nach einem Mord wird nur der Mitspieler verfolgt, nicht der Host. |
| Waffe ziehen | Der Mitspieler zieht neben einer Wache die Waffe oder betritt einen bewachten Ort: Die Wache warnt und greift dann diesen Spieler an. |
| Ein Gespräch je NPC | Einer spricht mit einem NPC, der andere versucht es gleichzeitig: „<NPC> spricht gerade mit <Spieler>.“ Wer im Umkreis von 20 m zuhört, sieht die Antwortmöglichkeiten des Sprechers und die gewählte Zeile. |
| Neuigkeiten | Nach dem Überfall zu Beginn hat Hamlar (Ardea) für jeden Helden etwas zu sagen: Er kommt auch zum Mitspieler und spricht ihn an. |
| Begleiten und Führen | Der Mitspieler nimmt eine Folgen- oder Hinbringen-Quest an (Mitch in Faring, der Sklave für Freman in Silden): Das NPC geht mit diesem Spieler, die Quest zählt für beide. |
| Bis zum Morgen warten | Einer wählt „bis zum Morgen warten“: Die Uhr springt für beide auf 9 Uhr. |
| Aufstand Kap Dun | Der Aufstand der Rebellen in Kap Dun (Phils Quests): Er beginnt für beide, der Kampf zählt für beide. Die Bots konnten ihn nicht starten. |
| Stadt befreien | Eine andere Stadt als Ardea (zum Beispiel Montera) mitten im Spiel befreien: Die Stadt ist bei beiden frei, die Quest bei beiden erledigt. |
| Vertreiben, Plündern | Eine Vertreiben-Quest (mit ihrem Dialog-Kampf) und eine Plündern-Quest: Beide sehen das Ergebnis, die Quest zählt für beide. |

### Optional: die Neuheiten von 0.5.1

| Abschnitt | Schritte und Beobachtungen |
|---|---|
| Blocken | Der Mitspieler blockt ein paar Schläge eines Gegners mit Waffe oder Schild: Der Block kostet Ausdauer (ohne Schild-Talent mehr), wie im Einzelspiel. |
| Parade eines NPC | Der Mitspieler schlägt auf ein NPC aus dem Gebiet des Hosts, das pariert (etwa eine Wache im Duell): Parierte Schläge richten weniger Schaden an und kosten das NPC Ausdauer, wie bei Schlägen des Hosts. |
| Zuschauer | Der Mitspieler schlägt in einer Stadt einen Bürger nieder, während andere zusehen: Die Zuschauer kommen und reagieren wie beim Host. |
| Groll | Der Mitspieler fängt mit einem NPC Streit an und geht weg: Kommt er zurück, ist das NPC nur ihm noch böse, nicht dem Host. |
| Kapitel | Einer gibt die Artefakte an einem Götterschrein ab: Das Kapitel wechselt für beide (andere NPCs sprechen mit beiden wie im neuen Kapitel). |
| Das Ende | Einer startet den Epilog: Der andere sieht dieselben Bilder zur selben Zeit, bis zum Spielende. Notieren, was danach mit der Verbindung passiert. |
| Schwimmen | Der Mitspieler schwimmt im Meer vorwärts: Der Host sieht ihn schwimmen, nicht gleiten. |

### Optional: die Neuheiten von 0.6.0

| Abschnitt | Schritte und Beobachtungen |
|---|---|
| Polnisch | Ein Spieler mit polnischen Untertiteln (Optionsmenü) sieht die Meldungen der Mod auf Polnisch, etwa wenn er sich vom Host entfernt. |
| Das Ende, beschäftigt | Einer startet den Epilog, während der andere im Dialog ist: Der andere sieht eine Meldung, sein Epilog beginnt nach dem Dialog. |
| Story-Umzüge | Nach der Befreiung von Ardea gehen beide weit weg: Diego, Gorn und Milten stehen bei beiden am selben neuen Ort. Folgt Diego dem Mitspieler, bleibt er bei ihm. |
| Befreite Stadt | Der Mitspieler befreit bei gemeinsamem Ruf eine Stadt: Die Rebellen sprechen den Host darauf an wie den Mitspieler. |
| Schlafendes Tier | Der Mitspieler geht zu einem schlafenden Tier: Es wacht auf wie beim Host. |

### Optional: über Steam (experimentell)

Beide stellen `transport = steam` ein (oder wählen im Setup Steam) und sind Steam-Freunde; siehe [SPIELEN.md](SPIELEN.md#über-steam-experimentell).

| Abschnitt | Schritte und Beobachtungen |
|---|---|
| Host finden | Der Host startet zuerst, dann der Mitspieler ohne Adresse: Er tritt nach wenigen Sekunden bei (Dauer notieren). Steam zeigt beide als „spielt Spacewar“. |
| Verschiedene Netze | Von zwei verschiedenen Wohnungen aus spielen (kein LAN, kein VPN): Beitritt, Übertragung des Spielstands und eine Stunde Spiel klappen. Ruckler im Vergleich zu Hamachi notieren. |
| Spiel beitreten | Das Spiel des Mitspielers läuft schon; dann in der Steam-Freundesliste beim Host „Spiel beitreten“: Er tritt bei. |
| Verlassen | Der Mitspieler beendet sein Spiel: Der Host sieht ihn sofort weg. Er startet neu und tritt wieder bei. |
| Ohne Steam | Der Mitspieler beendet Steam und startet das Spiel: Eine Meldung sagt, dass Steam nicht nutzbar ist; mit Adresse in der INI verbindet er sich über die Adresse. |

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
- Lädt der Host während des Spiels einen Spielstand, laden die Mitspieler ihn automatisch mit (Stufe S1); ein Mitspieler
  in einer anderen Welt spielt nicht mit, bis er den Stand des Hosts hat (Stufen S2, S3).
- Nicht geprüft: Monster, die auf bewusstlose Helden einschlagen; der echte Bett-Dialog; die
  Maussteuerung des Handelsfensters (Ziehen, Knöpfe; die Bots handeln über die Handelsfunktionen des Spiels).
