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
  tötet ein anderes NPC es, bekommt niemand Erfahrung.
- Gefolgsleute folgen dem Spieler, der sie angeworben hat, auch weit weg vom Host. Entlässt er sie, gehören sie
  wieder dem Host.
- Quests und bereits geführte Dialoge sind gemeinsam; Quest-Belohnungen bekommen alle. Kill-Aufträge zählen auch Gegner, die
  der Mitspieler weit weg vom Host erledigt. Gibt einer eine Quest ab, bekommen **alle** die Erfahrung dafür, das **Gold** nur der,
  der abgibt.
- Gegenstände, Truhen, Leichen und Handel: Was einer nimmt, ist für alle weg.
- Uhrzeit: Es ist für alle dieselbe Tageszeit; wer schläft, stellt die Zeit für alle vor.
- Tote NPCs liegen bei allen am Boden, auch wenn sie weit weg vom Host gestorben sind und man erst später hinkommt.
- Niederschlag wie im Einzelspiel: Schlägt dich eine Wache, ein Duell- oder Arenagegner bewusstlos (ohne dass du sterben
  würdest), läuft es wie im Einzelspiel: Du stehst von selbst wieder auf, Aufhelfen gibt es dabei nicht. Dass der Sieger
  den Mitspieler dabei ausraubt, ist noch nicht umgesetzt (beim Host läuft es wie im Einzelspiel).
- Duell und Arena gehören den beiden Kämpfern: Kämpft dein Partner ein Duell oder in der Arena, zählen deine Treffer auf
  seinen Gegner nicht. Der Kampf endet wie im Einzelspiel mit einem Niederschlag, nie mit einem Mord.
- Aufhelfen kostet einen Heiltrank: Wer tödlich getroffen wird, liegt bewusstlos am Boden, bis ein Mitspieler
  (verbunden, in derselben Welt, nicht selbst am Boden) sich 3 Sekunden höchstens 2,5 m daneben stellt. Dabei
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
| Gleichzeitiges Benutzen eines Lagerfeuers, Sitzplatzes oder anderen festen Arbeitsplatzes kann einen Spieler festhalten. | Solche Plätze nacheinander benutzen. Bleibt jemand hängen, keine weiteren Aktionen darauf starten; Fehler notieren und gemeinsam neu laden. |
| Kämpfen oder Speichern in Tiergestalt ist noch nicht ausreichend geprüft. | Vor Kämpfen und vor dem Speichern zurückverwandeln. |
| Ein beherrschter oder gezähmter NPC (Wort der Kontrolle, Tierzähmung) folgt nicht zuverlässig oder verlässt die Gruppe. | Verlasst euch vorerst nicht darauf. Bei einem Fehler NPC und Uhrzeit notieren; erneutes Zaubern ist kein gesicherter Ersatz. |
| Betäubung und Hypnose, vom Mitspieler gezaubert, wirken nur über den Host. | Wirkt es nicht, notieren; der Host kann den Zauber selbst sprechen. |
| Eine beschworene Kreatur bleibt beim anderen Spieler stehen, wenn ihr euch weit entfernt. | Beschwörungen nur nutzen, wenn ihr zusammen seid. Passiert es doch, Ort und Kreatur notieren. |
| Gift und Krankheit sind beim Partner nicht vollständig zu erkennen; für einen Gifttod kann Erfahrung fehlen. | Abweichungen notieren. |
| Obwohl die Stadtzähler gemeinsam sind, kann eine Wache noch den falschen Spieler beschuldigen oder verfolgen. | Diebstähle und absichtliche Angriffe auf friedliche Bewohner vorerst vermeiden. Bei einem Vorfall festhalten, wer gehandelt hat und wen die Wache angesprochen oder angegriffen hat. |

## Bekannte Grenzen

- Ab etwa 35 m vom Host meldet das Spiel „Weit weg vom Host“: Die Gegend läuft dann beim Mitspieler. Unter etwa
  25 m kommt „Wieder beim Host“.
- **Zum Mitspieler springen:** Die Taste **Bild ab** (Page Down) setzt dich 1,5 m hinter deinen Mitspieler, in derselben
  Welt, auch wenn er bewusstlos ist (dann kannst du ihm aufhelfen). Danach 5 Sekunden Pause. So könnt ihr euch trennen und
  jederzeit wieder treffen; am besten bleibt ihr meist zusammen. Andere Taste: `partner_key = F9` in der INI
  (F1–F24, PAGEUP, HOME, END, INSERT, DELETE oder ein Buchstabe).
- Dialog-Auswirkungen: Was ein Dialog bei einem NPC ändert (Händler, Lehrer, Wache, Tagesablauf ...), Duelle und
  Angriffe aus Dialogen und das Anwerben von Gefolgsleuten gelten für alle. Gegenstände aus Dialogen bekommt, wer
  den Dialog führt.
- Gefolgsleute über Speichern und Laden: Der Host merkt sich, wem sie gehören. Nach dem Laden bekommt der Mitspieler sie
  beim Wiederbeitritt zurück, erkannt an seinem Namen in der INI; den Namen also nicht ändern. Nach einem
  Verbindungsabbruch ohne Laden gehören sie zunächst dem Host.
- Lädt der Host während des Spiels einen Spielstand, laden die Mitspieler ihn automatisch mit. Bis sie fertig sind,
  seht ihr euch nicht.
- Handel und Plündern sind mit Bots getestet, nicht mit der echten Handelsoberfläche.
- Schlafen: Wer schläft, stellt die Uhr für alle vor (die Heilung durch den Schlaf gilt nur für den Schläfer).
- Verwandlungen (z. B. in einen Wolf oder eine Blutfliege): Der andere Spieler sieht das Tier, wie es läuft und sich
  bewegt, und nach der Rückverwandlung wieder den Helden. Getestet mit Wolf und Blutfliege, ausgelöst wie der
  Verwandlungszauber des Spiels. Noch nicht getestet: Kämpfe in Tiergestalt (Treffer auf das Tier, Schläge des Tiers)
  und Speichern während einer Verwandlung; dabei kann es noch Fehler geben.
- Gegner greifen beide Spieler an. Wie ein NPC zum Mitspieler steht, richtet sich nach dem Helden des Rechners, der dieses
  NPC gerade berechnet: Hat der Host bei den Orks einen schlechten Ruf, gilt der beim Host auch für den Mitspieler.
- Ruf bei Städten und Lagern: Diebstähle und Morde zählt jede Stadt für euch beide gemeinsam, auch was der Mitspieler
  getan hat (Einstellung `reputation = shared` in der INI des Hosts). Noch nicht angepasst: welche Wache was sieht und wen
  sie verfolgt oder zur Rede stellt; die Einstellungen `together` und `personal` sind vorbereitet, wirken aber noch wie
  `shared` bzw. wie früher (jeder Rechner zählt für sich).
- Nicht getestet: Monster, die auf einen am Boden liegenden Helden weiter einschlagen; Ausrauben eines
  bewusstlosen Helden; Spielen über das echte Internet (getestet wurde auf einem PC mit zwei Spielkopien);
  mehr als zwei Spieler. Alles hier wurde mit Bots getestet. Ein erster kurzer Test mit Menschen (zwei Fenster auf einem
  PC) fand zwei Fehler: Gegner ignorierten den Mitspieler, und Kämpfe waren beim Mitspieler nicht zu sehen. Beide sind
  behoben und mit Bots geprüft, aber noch nicht erneut von Menschen.
