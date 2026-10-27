# G3 Together: dołączanie do hosta

[English](JOINING.md) · [Deutsch](MITSPIELER.md) · **Polski**

## Przed startem

- Oba komputery powinny mieć przetestowaną podstawę: Gothic 3 ze Steama, **Community Patch 1.75.14**
  i **Parallel Universe Patch 1.1.1**. Używajcie tej samej biblioteki DLL G3 Together i tych samych plików gry.
  Działanie bez PUP nie zostało potwierdzone.
- Poziom trudności, alternatywny balans, alternatywna SI, mnożnik doświadczenia, minimalny czas trafienia, Quick Loot
  i sekcja `[Game]` w `Ini\ge3.ini` muszą być takie same jak u hosta.
- W folderze `scripts` nie może być innego moda kooperacji.

## Instalacja

**Instalator:** gdy Gothic 3 i obie łatki są zainstalowane, rozpakuj cały pakiet w dowolnym miejscu i kliknij dwukrotnie
`Instalacja.cmd` (po angielsku `Setup.cmd`, po niemiecku `Einrichten.cmd`). Instalator zrobi resztę: znajdzie Gothic 3
(Steam, GOG albo podany folder), sprawdzi Community Patch 1.75.14 i Parallel Universe Patch 1.1.1, przeniesie inny mod
kooperacji ze `scripts`, skopiuje mod do `scripts` i zapisze `g3together.ini` na podstawie twoich odpowiedzi (host czy
dołączający gracz, twoja nazwa, adres hosta). Zastąpione pliki trafiają do `G3Together-backup` w folderze gry. Instalator
sprawdzono na folderach testowych, jeszcze nie u graczy; poniższe kroki robią to samo ręcznie.

1. Zainstaluj Gothic 3 ze Steama, potem **Community Patch 1.75.14**, potem
   [Parallel Universe Patch **1.1.1**](https://www.worldofgothic.de/dl/download_678.htm)
   do tej samej instalacji gry.
2. Skopiuj `Script_G3Together.dll` z tego samego pakietu co host do `<folder Gothic 3>\scripts\`.
3. Skopiuj `g3together.ini.client-example` z pakietu do `<folder Gothic 3>\`, zmień nazwę na `g3together.ini` i wpisz
   swoje dane:

   ```ini
   role = client
   address = <adres hosta>
   port = 34567
   name = <twoja nazwa>
   language = auto
   ```

Zastąp oba pola w nawiasach. **Przy Hamachi** wpisz adres Hamachi hosta (`25.x.x.x`, widoczny na górze jego okna
Hamachi). Obaj gracze muszą być w tej samej sieci Hamachi; najlepiej z połączeniem bezpośrednim. Twój komputer musi mieć
`role = client`. Nie zmieniaj swojej nazwy gracza: host po niej przywraca twoich towarzyszy po wczytaniu zapisu.

`language = auto` pokazuje komunikaty moda w języku tekstów twojego Gothic 3 (polska gra: po polsku). Na stałe możesz
ustawić `language = pl`, `de` albo `en`; bez tego wiersza komunikaty są po angielsku. Każdy gracz wybiera niezależnie;
ustawienie nie zmienia języka samego Gothic 3.

## Dołączanie

Host uruchamia Gothic 3 i zaczyna nową kampanię albo wczytuje kampanię kooperacji. Ty uruchamiasz grę potem. Jeśli host
wczytał albo zapisał kampanię, zostań w menu głównym i poczekaj, aż kampania zostanie przesłana i wczytana. Jeśli host
zaczyna zupełnie nową grę, ty też zacznij nową grę.

Jeśli dołączenie zostanie odrzucone, otwórz `g3together.log` w folderze Gothic 3 i znajdź wiersz z `REFUSED by host`.
Porównaj wersje i opcje gry z hostem. Powód zostaje też w górnym wierszu menu głównego.

Jeśli twoja gra w ogóle nie może połączyć się z hostem, po około 15 sekundach mówi o tym górny wiersz menu głównego,
z adresem i portem z twojego INI (w grze: komunikat co 30 sekund). Sprawdź, czy gra hosta działa, adres i port oraz zaporę
u hosta. Mod próbuje dalej; gdy połączy się z hostem, wiersz wraca do normy.

**Przez Steam** (`transport = steam`, eksperymentalnie, zob. [GRANIE.md](GRANIE.md#przez-steam-eksperymentalnie)): adres
nie jest potrzebny. Twoja gra szuka hosta wśród twoich znajomych na Steam; dopóki go nie znajdzie, po około 15 sekundach
mówi o tym górny wiersz menu głównego. Host musi być na twojej liście znajomych na Steam i mieć uruchomiony Steam i grę.

## Podczas gry

- Gdy leżysz powalony, poczekaj na drugiego gracza. Połączony gracz w tym samym świecie może pomóc ci wstać: rozmawia
  z tobą i wybiera „Pomogę ci.”. Kosztuje go to miksturę leczniczą. Bez mikstury leżysz, dopóki jej nie zdobędzie. Jeśli
  wszyscy leżą albo jesteś sam, giniesz jak w grze dla jednego gracza: koniec gry, potem wczytaj zapis.
- Noś mikstury lecznicze dla drugiego gracza.
- Dalej niż około 35 m od hosta widzisz **„Daleko od hosta”**; bliżej niż około 25 m: **„Znów w pobliżu hosta.”**
- **Page Down** stawia cię tuż za drugim graczem, także gdy leży powalony. Można go użyć raz na pięć sekund. Aby wybrać
  inny klawisz, ustaw w INI `partner_key = F9` (F1–F24, PAGEUP, HOME, END, INSERT, DELETE albo litera; Insert i Delete to
  klawisze handlu, chyba że je zmienisz).
- Drugi gracz jest zaznaczony na twojej mapie i kompasie.
- Drugi gracz widzi twoją zbroję i hełm. `look = Diego` w twoim INI pokazuje cię jako inną postać.
- Handel z drugim graczem: porozmawiaj z jego postacią, „Pokaż mi swoje towary.”; on potwierdza klawiszem Insert (Delete
  odrzuca). Inne klawisze: `offer_accept_key` i `offer_decline_key` w INI; mod ostrzega przy starcie, jeśli dwa twoje
  klawisze są takie same.
- Kampanię zapisuje host.
- Problemy i obejścia: **Znane problemy** w [pełnym poradniku](GRANIE.md).

## Gdy coś pójdzie nie tak

Otwórz PowerShell w folderze rozpakowanego pakietu, tam gdzie leży ten plik, i uruchom poniższe polecenie. Potrzebny jest
Python:

```powershell
python "tools\collect_logs.py" "<folder Gothic 3>"
```

Wyślij hostowi powstały plik `g3together-logs-<czas>.zip`. Powstaje w folderze pakietu. Bez Pythona zbierz ręcznie
`g3together.log` z folderu Gothic 3 oraz pliki `.log` z `Dokumenty\gothic3`, łącznie z podfolderem `paru`.
