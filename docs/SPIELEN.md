# G3 Together spielen

[English](PLAYING.md) · **Deutsch**

G3 Together macht Gothic 3 zu einem Koop-Spiel für 2 Spieler (mehr sind vorgesehen, aber nicht getestet). Ein Spieler ist der **Host**: Seine Welt
ist die gemeinsame Kampagne, sein Spielstand ist der Spielstand aller. Die anderen treten bei.

Stand: Testversion. Was geht und was nicht, steht unten unter „Bekannte Grenzen“.

## Voraussetzungen (alle Spieler gleich)

- Gothic 3 von Steam mit dem **Community Patch 1.75.14** (dieselbe Version bei allen).
- **Parallel Universe Patch 1.1.1** auf beiden Rechnern. Steam, Community Patch 1.75.14 und PUP 1.1.1 bilden die getestete Grundlage; die Kompatibilität ohne PUP ist nicht belegt.
- Dieselbe Version von G3 Together bei allen (dieselbe `Script_G3Together.dll`) und dieselben Spieldateien.
- Dieselben Optionen bei allen: Schwierigkeit, alternatives Balancing, alternative KI, XP-Faktor, minimale
  Trefferdauer, Quick Loot und der ganze Abschnitt `[Game]` in `Ini\ge3.ini`. Die Mod prüft das beim Beitritt
  und lehnt einen Mitspieler mit Unterschieden ab. Den Grund findet der Mitspieler in `<Gothic 3>\g3together.log`
  (Zeile mit `REFUSED by host`). Lose Dateien in den Data-Unterordnern prüft sie nicht.
- Keine anderen Koop-Mods im Ordner `scripts` (zum Beispiel `Script_G3Coop.dll` von Myrtana Accord).
- Am besten eine **Kopie** der Gothic-3-Installation nur für Koop verwenden.

## Installieren

**Einrichten:** Sind Gothic 3 und beide Patches installiert, das ganze Paket irgendwohin entpacken und `Einrichten.cmd`
doppelklicken (`Setup.cmd` auf Englisch). Es erledigt das Kopieren und die INI: Es findet Gothic 3 (Steam, GOG oder der
Ordner, den du angibst), prüft Community Patch 1.75.14 und Parallel Universe Patch 1.1.1, nimmt eine andere Koop-Mod aus
`scripts`, kopiert die Mod nach `scripts` und schreibt `g3together.ini` nach deinen Antworten (Host oder Mitspieler, dein
Name, die Adresse des Hosts). Ersetzte Dateien liegen danach in `G3Together-backup` im Spielordner. Das Einrichten wurde
an Testordnern geprüft, noch nicht von Spielern; die Schritte unten machen dasselbe von Hand.

1. Gothic 3 von Steam installieren, danach den **Community Patch 1.75.14**.
2. Den [Parallel Universe Patch **1.1.1**](https://www.worldofgothic.de/dl/download_678.htm) in dieselbe Spielinstallation installieren. Beide Spieler verwenden denselben Patchstand.
3. Aus demselben G3-Together-Paket `Script_G3Together.dll` in den Ordner `<Gothic 3>\scripts\` kopieren.
4. Die passende Datei `g3together.ini.host-example` oder `g3together.ini.client-example` aus dem Paket als `g3together.ini` in den Ordner `<Gothic 3>\` kopieren und anpassen:

   Host:

   ```ini
   role = host
   port = 34567
   name = DeinName
   ```

   Mitspieler:

   ```ini
   role = client
   address = <IP-Adresse des Hosts>
   port = 34567
   name = DeinName
   ```

   Wichtig: Bei jedem Mitspieler muss `role = client` stehen. Fehlt die Datei oder steht dort etwas anderes,
   startet das Spiel als Host.

### Sprache der Mod-Meldungen

Standard ist Englisch (`language = en`). Für deutsche Meldungen `language = de` in die eigene
`g3together.ini` eintragen und das Spiel neu starten. Die Spieler können die Sprache unabhängig
voneinander wählen. Die Sprache von Gothic 3 selbst wird dadurch nicht geändert. Die deutschen
Meldungen in dieser Anleitung beziehen sich auf `language = de`.

## Verbinden über das Internet

Die Mod verbindet sich direkt per UDP (Port 34567). Ohne Router-Einstellungen geht das am einfachsten über ein
virtuelles privates Netz, das alle Spieler installieren, zum Beispiel **Tailscale** oder **ZeroTier**.
Als `address` trägt man dann die Tailscale- bzw. ZeroTier-Adresse des Hosts ein.
Alternativ leitet der Host UDP-Port 34567 in seinem Router auf seinen PC weiter.

### Über Hamachi

1. Beide Spieler installieren Hamachi und treten **demselben** Hamachi-Netz bei.
2. Den angezeigten Verbindungsstatus zum Partner prüfen. Eine direkte Verbindung ist vorzuziehen; eine Verbindung
   über einen Relay-Server erhöht die Verzögerung und kann ruckeln. Verbindungsfehler vor dem Spielstart beheben.
3. Der Host sieht seine Hamachi-Adresse oben im Hamachi-Fenster (`25.x.x.x`). Der Mitspieler trägt sie in seiner INI ein:
   `address = 25.x.x.x`, `port = 34567`.
4. **Firewall beim Host:** Windows behandelt das Hamachi-Netz oft als „Öffentliches Netzwerk“. Erscheint beim ersten Start von
   Gothic 3 die Firewall-Frage, das von Hamachi verwendete Netzwerkprofil erlauben, gegebenenfalls auch **„Öffentliche Netzwerke“**.
5. Probe: Der Mitspieler tippt in der Eingabeaufforderung `ping 25.x.x.x` (Adresse des Hosts). Damit lassen sich Verbindung
   und Verzögerung prüfen. Eine geringe Verzögerung ist besser; eine Ping-Antwort allein bestätigt noch keine erlaubte UDP-Spielverbindung.
6. Erreicht das Spiel des Mitspielers den Host nicht, sagt das nach etwa 15 Sekunden die oberste Zeile seines Hauptmenüs, mit der
   eingetragenen Adresse und dem Port (im Spiel: alle 30 Sekunden eine Meldung).

Die Mod nimmt auf dem Host Verbindungen über jeden Netzwerkadapter an, also auch über Hamachi. Die in diesen Anleitungen
beschriebenen Tests liefen auf einem PC mit zwei Spielkopien; Spielen über das echte Internet bleibt ungeprüft.

## Spielen

1. Der Host startet Gothic 3 und beginnt ein neues Spiel oder lädt seine Koop-Kampagne.
2. Die Mitspieler starten Gothic 3.
   - Hat der Host eine Kampagne geladen oder gespeichert, bekommt ein Mitspieler sie beim Beitritt
     automatisch und lädt sie selbst. Dafür im Hauptmenü warten.
   - Hat der Host ein neues Spiel begonnen, startet der Mitspieler ebenfalls ein neues Spiel.
   - Lädt der Host später einen Spielstand (auch denselben noch einmal), lädt jeder Mitspieler ihn automatisch
     mit, ein paar Sekunden nach dem Host; ein Hinweis erscheint auf dem Bildschirm. Ist der Host noch im
     Hauptmenü, wartet der Mitspieler, bis der Host ein Spiel lädt.
   - Gemeinsam gespielt wird nur in derselben Welt: Spielt ein Mitspieler einen eigenen Spielstand oder ein
     anderes Spiel als der Host, sehen sich die beiden nicht, bis er im Stand des Hosts ist. Hat der Host einen
     Spielstand, bekommt der Mitspieler ihn dann automatisch; sonst speichert der Host einmal.
3. Gespeichert wird beim **Host**. Jeder Mitspieler behält dabei automatisch seinen eigenen Helden
   (Stufe, Erfahrung, Werte, Inventar) für diesen Spielstand und bekommt ihn beim nächsten Beitritt zurück.
   Beim allerersten Beitritt aus dem Hauptmenü spielt man mit einer Kopie des Host-Helden; wer seinen eigenen
   neuen Helden behalten will, beginnt vorher ein neues Spiel und tritt dann bei.

## Was gemeinsam ist

- Spieler sehen sich und ihre Bewegungen, Kampf-Animationen, Stürze.
- Alle sehen dieselben NPCs, Kämpfe und Tode. Jede Gegend läuft bei einem Spieler: in der Nähe des Hosts beim
  Host, weit weg von ihm bei dem Mitspieler, der zuerst dort war. Kommt der andere dazu, sieht er dieselbe Gegend.
- Schaden: Einen Treffer auf ein NPC entscheidet der Rechner, bei dem dieses NPC läuft. Erfahrung bekommt, wer
  tötet. Tötet ein Begleiter oder eine Beschwörung eines Spielers ein NPC, bekommt dieser Spieler die Erfahrung;
  tötet ein anderes NPC es, bekommt niemand Erfahrung. Einen Menschen oder Ork schlägt dein letzter Treffer wie im
  Einzelspiel nieder (Erfahrung, kein Mord); erst ein Schlag auf den am Boden Liegenden tötet ihn.
- Gefolgsleute folgen dem Spieler, der sie angeworben hat, auch weit weg vom Host. Entlässt er sie, gehören sie
  wieder dem Host.
- Wer dich führt oder wen du begleitest (Folgen- und Hinbringen-Quests), geht mit dem Spieler, der die Quest angenommen
  hat, auch mit dem Mitspieler; lädt der Host einen Spielstand, führt wieder dieser Spieler. Endet das Hinbringen weit weg
  vom Host, steht das NPC danach bei beiden am selben Ort.
- Quests und bereits geführte Dialoge sind gemeinsam; Quest-Belohnungen bekommen alle. Kill-Aufträge zählen auch Gegner, die
  der Mitspieler weit weg vom Host erledigt. Gibt einer eine Quest ab, bekommen **alle** die Erfahrung dafür, das **Gold** nur der,
  der abgibt.
- Ein Hocker, eine Bank, ein Lagerfeuer oder ein anderer Platz hat einen Benutzer: Solange dein Mitspieler ihn benutzt,
  kommst du nicht dran („<Name> benutzt das gerade.“), und NPCs nehmen ihn auch nicht.
- Gegenstände, Truhen, Leichen und Handel: Was einer nimmt, ist für alle weg. Wem einer die Taschen geleert hat, den kann
  auch der andere nicht mehr bestehlen.
- Uhrzeit: Es ist für alle dieselbe Tageszeit; wer schläft, stellt die Zeit für alle vor, ebenso wer bis zum Morgen
  wartet: Er springt sofort auf 9 Uhr.
- Tiere, die nach einigen Tagen wiederkommen (Aasfresser, Hirsche, Wölfe und ähnliche), kommen bei beiden wieder oder bei
  keinem.
- Tote NPCs liegen bei allen am Boden, auch wenn sie weit weg vom Host gestorben sind und man erst später hinkommt.
- Niederschlag wie im Einzelspiel: Schlägt dich eine Wache, ein Duell- oder Arenagegner bewusstlos (ohne dass du sterben
  würdest), läuft es wie im Einzelspiel: Du stehst von selbst wieder auf, Aufhelfen gibt es dabei nicht, und der Sieger
  kann dir die Hälfte deines Goldes abnehmen. Tut das ein NPC aus dem Spiel des anderen Rechners, siehst du „<Name> hat
  dir N Gold abgenommen.“ Dein Mitspieler kann dich nicht ausplündern.
- Zeugen: Sieht ein NPC einen Spieler stehlen, reagiert er wie im Einzelspiel, auch wenn der andere Rechner ihn berechnet:
  Er geht auf den Dieb los. Kämpfe und Morde beider Spieler sehen NPCs genauso: Zuschauer und Wachen greifen schon beim
  Prügeln ein, verfolgt wird nur der Mörder. Wachen warnen einen Spieler, der neben ihnen die Waffe zieht oder einen
  bewachten Ort betritt, und greifen ihn dann an. Einen Mord durch den Begleiter eines Spielers bemerken sie wie im
  Einzelspiel.
- Mithören: Wer höchstens etwa 20 m entfernt steht, hört den Dialog des Mitspielers. Sind die Untertitel des Spiels an,
  erscheint auch der Text, in der eigenen Spielsprache, solange man nicht selbst in einem Dialog ist. Man sieht auch die
  Antworten, die der Mitspieler wählen kann, und dann die gewählte; wer spricht, wählt.
- Ein NPC spricht mit einem Spieler zur Zeit: Spricht dein Mitspieler mit ihm, ist es für dich beschäftigt („<NPC>
  spricht gerade mit <Spieler>.“). Es gestikuliert und schaut den Sprecher an, auf beiden PCs.
- Ein NPC mit wichtigen Neuigkeiten für einen Spieler (eine Wache, ein Questgeber) kommt wie im Einzelspiel zu ihm und
  spricht ihn an, auch zum Mitspieler.
- Duell und Arena gehören den beiden Kämpfern: Kämpft dein Partner ein Duell oder in der Arena, zählen deine Treffer auf
  seinen Gegner nicht. Der Kampf endet wie im Einzelspiel mit einem Niederschlag, nie mit einem Mord.
- Aufhelfen kostet einen Heiltrank: Wer tödlich getroffen wird, liegt bewusstlos am Boden, bis ein Mitspieler
  (verbunden, in derselben Welt, nicht selbst am Boden) ihn anspricht und „Ich werde dir helfen.“ wählt. Dabei
  verbraucht er einen Heiltrank aus seinem Inventar (den günstigsten). Hat er keinen, bekommen beide einen Hinweis,
  und der Bewusstlose bleibt liegen, bis der Partner mit einem Trank zurückkommt.
- Game Over: Kann niemand mehr helfen (alle liegen am Boden, oder man ist allein, weil der Partner nicht verbunden,
  in einer anderen Welt oder länger als 5 Sekunden nicht im Spiel ist), stirbt man wie im Einzelspiel. Der Host lädt
  dann einen Spielstand; der Mitspieler wird automatisch mitgeladen.
- Tote NPCs bleiben tot, auch wenn sie weit weg vom Host gestorben sind.
- Türen und Schlösser: Öffnet einer eine Tür oder knackt ein Schloss, ist sie für alle offen, auch für den, der
  später beitritt.
- NPCs wehren sich gegen beide: Schlägt der Mitspieler einen NPC, reagiert dieser wie im Einzelspiel (greift an,
  flieht oder ruft um Hilfe).
- Pfeile, Bolzen und Feuerbälle der Spieler sieht der andere fliegen; ein Treffer zählt genau einmal. Heilzauber
  wirken auch auf den Mitspieler.
- Zauber auf NPCs (Schlaf, Furcht, Bezaubern, Gift, Krankheit) wirken bei beiden. Beschworene Kreaturen sieht der
  andere Spieler und sie kämpfen mit.

## Fehler melden

- Fehler mit Uhrzeit aufschreiben (wer Host war, was ihr gemacht und gesehen habt).
- Danach bei beiden `tools\collect_logs.py` aus dem entpackten Paketordner ausführen und die ZIP-Datei weitergeben; die Befehle stehen in der [Spieltest-Checkliste](TESTPLAN.md).
- Für Fehler an NPCs und Gegnern, die ihr wiederholen könnt (z. B. ein toter Ork steht beim Mitspieler aufrecht):
  in die INI `trace = on` schreiben und neu starten. Dann schreibt das Spiel `g3trace.log` in den Spielordner: welche
  Animation und welche Aufgabe die NPCs bekommen, die die Mod steuert, und welcher Spielcode das auslöst. Die Datei ist
  auf etwa 50 000 Zeilen begrenzt und wird bei jedem Start neu angelegt; `collect_logs.py` nimmt sie mit. Danach die Zeile
  wieder entfernen.

## Bekannte Probleme (Stand 21.10.2026)

| Was passieren kann | Was ihr dann tut |
|---|---|
| Das Spiel kann während eines Kampfes oder kurz nach dem Tod eines Gegners abstürzen. Der bekannte Absturz im Ork-Überfall ist abgesichert, andere sind nicht ausgeschlossen. | Vor schwierigen Kämpfen speichert der Host. Bei einem Absturz Uhrzeit und Gegner notieren, beide Logs vor dem nächsten Start sichern. Danach lädt der Host den letzten guten Spielstand; der Mitspieler wird automatisch mitgeladen (ist sein eigenes Spiel abgestürzt, startet er es neu und wartet im Hauptmenü) und tritt wieder bei. |
| Die Figur des Partners läuft unter Treffern kurz hinterher oder zeigt eine Bewegung ungenau. | Kurz zusammenkommen und abwarten. Wenn nötig bringt euch **Bild ab** wieder zusammen. Bleiben Gegner, Lebenspunkte oder die Position deutlich verschieden, den Kampf unterbrechen und den Fehler notieren. |
| Nach dem Beitritt können Gegner oder Begleiter bei euch unterschiedlich handeln. | Erst kurz in der Nähe des Hosts prüfen, ob ihr dieselben Figuren und Bewegungen seht. Bei anhaltenden Unterschieden gemeinsam vom letzten guten Spielstand neu beginnen; Logs vorher sichern. |
| „<Name> benutzt das gerade.“, wenn du dich setzen oder ein Lagerfeuer, einen Amboss oder einen anderen Platz benutzen willst | Dein Mitspieler benutzt ihn gerade; warte, bis er aufsteht. Mit Bots geprüft sind nur Hocker; bleibt doch jemand an so einem Platz hängen, den Fehler notieren und gemeinsam neu laden. |
| Speichern in Tiergestalt ist noch nicht geprüft. | Vor dem Speichern zurückverwandeln. |
| Ein beherrschter oder gezähmter NPC (Wort der Kontrolle, Tierzähmung) folgt nicht zuverlässig oder verlässt die Gruppe. | Verlasst euch vorerst nicht darauf. Bei einem Fehler NPC und Uhrzeit notieren; erneutes Zaubern ist kein gesicherter Ersatz. |
| Betäubung und Hypnose, vom Mitspieler gezaubert, wirken nur über den Host. | Wirkt es nicht, notieren; der Host kann den Zauber selbst sprechen. |
| Eine beschworene Kreatur bleibt beim anderen Spieler stehen, wenn ihr euch weit entfernt. | Beschwörungen nur nutzen, wenn ihr zusammen seid. Passiert es doch, Ort und Kreatur notieren. |
| Gift und Krankheit sind beim Partner nicht vollständig zu erkennen; für einen Gifttod kann Erfahrung fehlen. | Abweichungen notieren. |
| Spricht dich eine Wache auf Verbrechen an, zählt sie den ganzen Ruf der Stadt (bei `shared` und `together` also auch die Taten deines Mitspielers). | Diebstähle und absichtliche Angriffe auf friedliche Bewohner vermeiden, wenn euch das stört. Bei einem Vorfall festhalten, wer gehandelt hat und wen die Wache angesprochen oder angegriffen hat. |

## Bekannte Grenzen

- Ab etwa 35 m vom Host meldet das Spiel „Weit weg vom Host“: Die Gegend läuft dann beim Mitspieler. Unter etwa
  25 m kommt „Wieder beim Host“.
- **Zum Mitspieler springen:** Die Taste **Bild ab** (Page Down) setzt dich 1,5 m hinter deinen Mitspieler, in derselben
  Welt, auch wenn er bewusstlos ist (dann kannst du ihm aufhelfen). Danach 5 Sekunden Pause. So könnt ihr euch trennen und
  jederzeit wieder treffen; am besten bleibt ihr meist zusammen. Andere Taste: `partner_key = F9` in der INI
  (F1–F24, PAGEUP, HOME, END, INSERT, DELETE oder ein Buchstabe; Einfg und Entf sind die Handelstasten, siehe unten).
- **Karte und Kompass:** Dein Mitspieler ist auf deiner Karte mit seinem Namen eingezeichnet und auf dem Kompass in seiner
  Richtung, von deinem Blick aus gesehen.
- **Dein Aussehen:** Dein Mitspieler sieht die Rüstung und den Helm, die du trägst. Willst du für ihn wie eine andere Figur
  aussehen, trag in deiner eigenen INI `look = Diego` ein (eine Figurvorlage des Spiels); ohne den Eintrag entscheidet sein
  `puppet_template` (Standard: der Held).
- **Handeln mit deinem Mitspieler:** Sprich seine Figur an und wähle „Zeig mir deine Ware!“. Es öffnet sich der normale
  Handelsbildschirm mit allem, was dein Mitspieler handeln kann (nicht, was er trägt, keine Questgegenstände), und seinem
  Gold; die Preise sind in beide Richtungen gleich, eine geschärfte, gesegnete oder geschmiedete Waffe bleibt es. Beim
  allerersten Handel kommt zuerst ein Tipp: auf „Ok“ klicken.
  Tausche so oft du willst und schließe dann das Fenster: Dein Mitspieler bekommt ein Angebot und nimmt es innerhalb von
  30 Sekunden mit Einfg an oder lehnt mit Entf ab. Bis dahin hast du weder, was du bekommst, noch, was du hergegeben hast;
  danach ist alles beim Richtigen. Andere Tasten: `offer_accept_key = F7` und `offer_decline_key = F8` in der INI
  (dieselben Namen wie bei `partner_key`). Sind zwei dieser drei Tasten gleich, löst ein Druck beides aus: Die Mod sagt
  das beim Start auf dem Bildschirm und in `g3together.log`.
- Dialog-Auswirkungen: Was ein Dialog bei einem NPC ändert (Händler, Lehrer, Wache, Tagesablauf ...), Duelle und
  Angriffe aus Dialogen und das Anwerben von Gefolgsleuten gelten für alle. Gegenstände aus Dialogen bekommt, wer
  den Dialog führt.
- Gefolgsleute über Speichern und Laden: Der Host merkt sich, wem sie gehören. Nach dem Laden bekommt der Mitspieler sie
  beim Wiederbeitritt zurück, erkannt an seinem Namen in der INI; den Namen also nicht ändern. Nach einem
  Verbindungsabbruch ohne Laden gehören sie zunächst dem Host.
- Lädt der Host während des Spiels einen Spielstand, laden die Mitspieler ihn automatisch mit. Bis sie fertig sind,
  seht ihr euch nicht.
- Handel und Plündern sind mit Bots über die Handelsfunktionen des Spiels getestet; die Maussteuerung des
  Handelsfensters (Ziehen, Knöpfe) hat noch kein Mensch ausprobiert.
- Schlafen: Wer schläft, stellt die Uhr für alle vor (die Heilung durch den Schlaf gilt nur für den Schläfer).
- Verwandlungen (z. B. in einen Wolf oder eine Blutfliege): Der andere Spieler sieht das Tier, wie es läuft und sich
  bewegt, und nach der Rückverwandlung wieder den Helden. Getestet mit Wolf und Blutfliege, ausgelöst wie der
  Verwandlungszauber des Spiels. In Tiergestalt kämpfst du als du selbst: Deine Treffer zählen als deine (Niederschlag,
  Tötung, Erfahrung, Mord), und Treffer von Gegnern verletzen das Tier, nicht deinen versteckten Menschenkörper (mit Bots
  geprüft: Wolf). Noch nicht getestet: Speichern während einer Verwandlung.
- Hagelsturm des Mitspielers: Sein Wetter bleibt auf deinem Rechner jetzt beim Mitspieler (vorher verdunkelte es den
  Himmel und blitzte um deinen eigenen Helden; von Bots über Position und Screenshot geprüft). Schaden macht nur der Hagel
  des Zaubernden, wie im Einzelspiel.
- Gegner greifen beide Spieler an. Wie ein NPC zum Mitspieler steht, richtet sich nach dem Helden des Rechners, der dieses
  NPC gerade berechnet: Hat der Host bei den Orks einen schlechten Ruf, gilt der beim Host auch für den Mitspieler.
- Befreit (oder erobert) einer eine Stadt oder ein Lager, gilt das für beide: Den Zustand der Stadt führt der Host.
- Ruf bei Städten und Lagern, einzustellen mit `reputation` in der INI des Hosts:
  - `shared` (Standard): ein Ruf pro Stadt für beide; Diebstähle beider Spieler zählen, und eine Wache, die dich auf
    Verbrechen anspricht, zählt die Taten aller. Ein NPC, der eine Tat gesehen hat, hält sie dem vor, der sie begangen hat.
  - `together`: wie `shared`, und ein NPC, der einen von euch bei einer Tat gesehen hat, hält sie euch beiden vor (er
    weist beide ab).
  - `personal`: jeder hat seinen eigenen Ruf; was dein Mitspieler tut, ändert deinen nie.
  Ein Mord zählt wie im Einzelspiel für die Stadt des Opfers, auch wenn der Rechner des anderen Spielers das Opfer
  berechnet (`personal`: im eigenen Ruf des Mörders). Einen Menschen oder Ork schlägst du zuerst nieder; erst ein Schlag
  auf den am Boden Liegenden tötet ihn.
- Seelenwanderung: Der andere Spieler sieht deinen Geist sich bewegen, nicht den zurückgelassenen Körper.
- Eis- und Feuerzauber, die Brennen oder Frost löschen (und solche Geschosse zerfallen lassen), wirken bei NPCs des
  anderen Spielers nur auf deinem eigenen PC. Das fällt selten auf.
- Ein NPC mit Neuigkeiten kommt nicht mehr zu dir, sobald du dich mehr als etwa 25 m von ihm entfernst; sprich es dann
  selbst an.
- Noch nicht geprüft, weil die Bots diese Szenen nicht aufbauen können: der Aufstand in Kap Dun, eine Stadt mitten im
  Spiel befreien (die Bots prüften Ardea zu Beginn), Vertreiben-Quests (ihr Dialog-Kampf) und Plündern. Sie stehen in der
  Spieltest-Checkliste.
- Nicht getestet: Monster, die auf einen am Boden liegenden Helden weiter einschlagen; Spielen über das echte Internet (getestet wurde auf einem PC mit zwei Spielkopien);
  mehr als zwei Spieler. Alles hier wurde mit Bots getestet. Ein erster kurzer Test mit Menschen (zwei Fenster auf einem
  PC) fand zwei Fehler: Gegner ignorierten den Mitspieler, und Kämpfe waren beim Mitspieler nicht zu sehen. Beide sind
  behoben und mit Bots geprüft, aber noch nicht erneut von Menschen.
