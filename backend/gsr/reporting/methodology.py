"""Bilingual methodology narrative + method-comparison table for the PDF report.

Ported from GSRv2 ``gsr/pipeline/methodology.py`` without the segmentation,
correlation and trend sections (those analyses were dropped in 2.0). Blocks are
simple tuples the report renderer walks:
    ("p", text) | ("formula", text) | ("note", label, text)
    | ("list", [items]) | ("params", [(term, definition), ...])
"""

from __future__ import annotations

from typing import Any

MethodologyBlock = tuple

METHODOLOGY: dict[str, dict[str, Any]] = {
    "pl": {
        "title": "Metodologia analizy",
        "intro": (
            "Dokument można czytać bez przygotowania statystycznego. Najpierw wyjaśnia prostym językiem, "
            "na jakie pytanie odpowiada każda analiza i jak czytać jej wykres, a następnie podaje szczegóły "
            "techniczne, parametry i ograniczenia. Opis prowadzi od surowych sygnałów GSR przez czyszczenie "
            "do synchronizacji oraz ko-pobudzenia."
        ),
        "sections": [
            {
                "title": "0. Jak czytać raport bez znajomości statystyki",
                "blocks": [
                    ("p", "Każdy wynik czytaj w kolejności: jakość danych → wielkość efektu → niepewność → "
                          "czas i kontekst badania. Sama pojedyncza liczba prawie nigdy nie wystarcza."),
                    ("list", [
                        "Jakość danych: sprawdź wspólny zakres czasu, odrzucone artefakty oraz sygnały płaskie lub urwane.",
                        "Wielkość efektu: odczytaj synchronizację lub indeks ko-pobudzenia.",
                        "Niepewność: sprawdź p-wartość testu surogatowego i rozkład zerowy.",
                        "Kontekst: znajdź wynik na osi czasu i porównaj go z przebiegiem zadania lub zajęć.",
                    ]),
                    ("params", [
                        ("p-wartość", "jak często wynik co najmniej tak silny mógłby pojawić się w modelu przypadku; "
                                      "małe p nie oznacza dużego, ważnego ani przyczynowego efektu."),
                        ("Test surogatowy", "tworzy wersje danych zachowujące dynamikę pojedynczych osób, ale "
                                            "niszczące wspólne wyrównanie w czasie — praktyczny model przypadku."),
                        ("Okno / krok", "okno jest analizowanym naraz fragmentem; krok określa przesunięcie między "
                                        "kolejnymi oknami."),
                    ]),
                    ("note", "Przykład", "p = 0,02 oznacza, że w zastosowanym modelu przypadku wynik co najmniej "
                                         "tak silny pojawiał się w około 2 na 100 prób. Nie oznacza „98% pewności”, "
                                         "że hipoteza jest prawdziwa."),
                    ("note", "Najważniejsze ograniczenie", "GSR mierzy pobudzenie fizjologiczne, ale nie rozpoznaje "
                                                           "konkretnej emocji ani jej znaku. Analiza pokazuje "
                                                           "współwystępowanie i dynamikę, nie dowodzi przyczyny ani "
                                                           "przenoszenia emocji między osobami."),
                ],
            },
            {
                "title": "1. Przegląd i cel badania",
                "blocks": [
                    ("p", "Aplikacja odpowiada na pytanie: w których momentach zajęć fizjologiczne pobudzenie "
                          "uczestników rosło lub spadało wspólnie (spontanicznie)? Sygnałem jest aktywność "
                          "elektrodermalna (EDA/GSR) w mikrosimensach (µS), rejestrowana czujnikami eSense z "
                          "częstotliwością ok. 5 Hz. Wyższa przewodność odpowiada silniejszej aktywacji "
                          "współczulnej (pobudzeniu), niezależnie od walencji."),
                    ("p", "Potok: import i synchronizacja → czyszczenie → skalowanie i decymacja → synchronizacja → "
                          "zarażanie emocjonalne (ko-pobudzenie). Analizy grupowe działają na wspólnej siatce "
                          "czasu z importu."),
                    ("note", "Ważne", "GSR jest sygnałem wolnozmiennym i silnie autokorelowanym: kolejne próbki nie "
                                      "są niezależne. Dlatego istotność liczona jest metodami zachowującymi "
                                      "autokorelację (test surogatowy), a nie klasycznymi "
                                      "wzorami zakładającymi niezależność próbek."),
                    ("note", "Po ludzku", "Aplikacja nie odgaduje, co ktoś czuł. Sprawdza, czy poziom pobudzenia "
                                           "zmieniał się u uczestników w podobnym czasie i czy ta zgodność była "
                                           "większa niż można oczekiwać przypadkowo."),
                ],
            },
            {
                "title": "2. Import i synchronizacja czasu",
                "blocks": [
                    ("p", "Pliki CSV są parsowane (wykrywanie kodowania i separatora, obsługa przecinka "
                          "dziesiętnego). Nagrania są nakładane na wspólny zegar absolutny i analizowane jest "
                          "wyłącznie okno wspólne."),
                    ("formula", "okno wspólne = [ max(start_i) , min(koniec_i) ] po wszystkich uczestnikach i"),
                    ("note", "Warunek analizy grupowej", "Jeżeli nagrania nie mają wspólnego czasu absolutnego, analiza "
                                                            "jest zatrzymywana. Aplikacja nie wyrównuje takich nagrań "
                                                            "automatycznie względem początku każdego pliku."),
                    ("note", "Kontrola duplikatów", "Identyczne ciągi wartości GSR są oznaczane ostrzeżeniem. Pozostają "
                                                        "jednak w analizie, aby decyzja o ich użyciu należała do badacza."),
                    ("note", "Jak czytać wykres", "Sprawdź, czy wszystkie linie obejmują ten sam okres i żadna nie "
                                                   "urywa się przedwcześnie. Różne poziomy RAW między osobami są "
                                                   "normalne i nie oznaczają jeszcze różnej reaktywności."),
                    ("note", "Ograniczenia", "Jeśli znaczniki czasu są błędne lub ich brak, wspólne okno może być "
                                             "niepoprawne. Analiza wymaga wiarygodnych znaczników czasu."),
                ],
            },
            {
                "title": "3. Czyszczenie artefaktów",
                "blocks": [
                    ("p", "Usuwane są tylko rzeczywiste artefakty czujnika, a nie prawdziwe reakcje skórno-galwaniczne "
                          "(SCR). Usunięte punkty są interpolowane liniowo. Filtry działają niezależnie:"),
                    ("params", [
                        ("Zakres fizjologiczny", "wartości poza [min, max] traktowane jako rozłączenie/nasycenie czujnika."),
                        ("Filtr artefaktów z-score", "punkty odległe od średniej o > K·SD jako artefakty (próg "
                                                     "konserwatywny); to filtr, nie normalizacja sygnału."),
                        ("Skok próbka-do-próbki", "nagłe skoki amplitudy (ruch/dotknięcie elektrody)."),
                        ("Reguła IQR", "odrzuca wartości poza [Q1 − 1.5·IQR, Q3 + 1.5·IQR]."),
                        ("Butterworth", "dolnoprzepustowy (domyślnie 0.05 Hz), dwukierunkowo (filtfilt, bez przesunięcia fazy)."),
                    ]),
                    ("note", "Ustawienia domyślne", "Filtr medianowy ma okno 5, próg Z-score wynosi 5 SD, próg skoku "
                                                      "wynosi średnia zmiana + 5 SD, a filtr IQR jest domyślnie "
                                                      "wyłączony. Wszystkie filtry nadal można zmienić ręcznie."),
                    ("note", "Jakość sygnału", "Na podstawie udziału odrzuconych próbek aplikacja pokazuje ocenę: dobra "
                                                 "< 10%, uwaga od 10% do poniżej 30%, słaba ≥ 30%. Ocena jest wyłącznie "
                                                 "informacyjna i nie wyklucza sygnału ani uczestnika z analizy."),
                    ("note", "Po ludzku", "Czyszczenie przypomina usuwanie trzasków z nagrania audio: powinno "
                                           "usunąć oczywiste błędy czujnika, ale zachować ogólny kształt prawdziwej reakcji."),
                    ("note", "Ograniczenia", "Filtr 0.05 Hz przepuszcza składowe o okresie ≥ 20 s; szybsze zmiany są tłumione."),
                ],
            },
            {
                "title": "4. Normalizacja i decymacja",
                "blocks": [
                    ("p", "Analiza działa na z-score: każdy sygnał jest standaryzowany w obrębie analizowanego okna "
                          "(stany Wysoki i Niski, sygnały wywiadów VSR) albo porównywany korelacją, której żadne "
                          "liniowe przeskalowanie nie zmienia (synchronizacja, ko-ruch). Sygnały decymowane są na "
                          "wspólną siatkę (domyślnie 1 Hz)."),
                    ("formula", "z = (x − średnia(x)) / SD(x)"),
                    ("p", "Przed decymacją sygnały przechodzą wewnętrzne skalowanie min-max do 0–1. Jest to liniowe "
                          "przekształcenie każdego sygnału osobno, więc nie zmienia żadnej korelacji, żadnego z-score "
                          "ani żadnego wyniku. Skala 0–1 jest dostępna tylko do podglądu: na wykresie sygnałów z "
                          "zaznaczonymi epizodami i w pliku analysis_input_merged.csv można wybrać z-score "
                          "(domyślnie) albo min-max (0–1)."),
                    ("note", "Po ludzku", "z = 0 to przeciętny poziom danej osoby w analizowanym zakresie, z = 1 to "
                                           "jedno odchylenie standardowe powyżej. Porównujemy kształt zmian, a nie "
                                           "bezwzględny poziom między osobami."),
                    ("note", "Nie oznacza", "z = 1 u jednej osoby nie musi oznaczać większego pobudzenia w µS niż "
                                            "z = 0,5 u innej."),
                ],
            },
            {
                "title": "5. Analiza synchronizacji",
                "blocks": [
                    ("p", "Oknowa korelacja krzyżowa par (z opóźnieniem do maks. lagu), koherencja, opcjonalnie DTW. "
                          "Podgrupy utrzymujące synchronizację powyżej progu tworzą epizody; sygnały są klastrowane."),
                    ("p", "Istotność: test surogatowy zachowujący autokorelację (przesunięcie kołowe / randomizacja fazy) "
                          "niszczy tylko synchronizację między osobami. Synchronizacja liczona ze znakiem — antyfaza nie jest "
                          "mylona z synchronizacją."),
                    ("p", "Synchronizacja rozbieżna: podgrupy, w których r każdej pary w oknie jest równe lub "
                          "niższe od minus progu rozbieżności (domyślnie −0,30, za prośbą zespołu Hassana), z tymi "
                          "samymi regułami epizodu. Sygnały zmieniają się wtedy systematycznie w przeciwnych "
                          "kierunkach: to związek o tej samej sile co synchronizacja, z przeciwnym znakiem. Kryteria "
                          "schematu VSR („Synchronous = above 0.8”, „Asynchronous = below 0.1”) czytane są jako siła "
                          "korelacji, czyli jej wartość bezwzględna."),
                    ("p", "Rozprzężenie (brak synchronii): odcinek między epizodami synchronizacji i rozbieżności pary, "
                          "co najmniej tak długi jak minimalny epizod, którego korelacja liczona dla całego odcinka "
                          "(średnia r jego okien) mieści się w ± progu rozprzężenia (domyślnie 0,1). Pojedyncze okna "
                          "30 s po filtrze 0,05 Hz wahają się między −1 a +1, więc warunek „każde okno |r| < 0,1” "
                          "prawie nigdy nie jest spełniony; jedna korelacja dla odcinka odpowiada opisowi zespołu "
                          "Hassana („the correlation between GSR data within an episode”)."),
                    ("note", "Po ludzku", "Synchronizacja pyta, czy osoby zmieniały się podobnie w tym samym czasie "
                                           "albo z niewielkim opóźnieniem. Epizod pokazuje kiedy i w jakiej podgrupie."),
                    ("note", "Opóźnienie", "Lag 5 s oznacza najlepsze dopasowanie po przesunięciu jednego sygnału o "
                                          "5 sekund. Nie dowodzi, że jedna osoba spowodowała reakcję drugiej."),
                    ("note", "p surogatowe", "Małe p oznacza, że podobnie silna zgodność rzadko pojawiała się po "
                                             "celowym rozminięciu sygnałów w czasie. Nadal potrzebny jest kontekst."),
                ],
            },
            {
                "title": "6. Zarażanie emocjonalne (ko-pobudzenie)",
                "blocks": [
                    ("p", "Każdy sygnał jest standaryzowany (z-score) w obrębie okna analizy i kodowany na stany: "
                          "Wysoki (z > próg), Niski (z < próg), Neutralny. Wykrywamy epizody High/Low (wspólny stan skrajny) "
                          "oraz ko-ruch (średnia parowa korelacja Spearmana w oknie ≥ próg)."),
                    ("formula", "Indeks ko-pobudzenia = średnia po czasie z max( liczba osób High , liczba osób Low ); zakres 0–N"),
                    ("p", "Pobudzenie przeciwstawne (za prośbą zespołu Hassana, dopełnienie wspólnego stanu Wysokiego i "
                          "Niskiego): ktoś jest w stanie Wysokim, gdy ktoś inny jest w stanie Niskim, z tymi samymi "
                          "progami z i regułami epizodu co High/Low. To brak synchronii w kroku 5 wywiadów VSR."),
                    ("p", "Kontrybutorzy: dla High/Low uczestnik utrzymuje stan przez ≥ ułamek epizodu; dla ko-ruchu musi "
                          "być dodatnio skorelowany z resztą grupy (leave-one-out), co wyklucza osoby płaskie/w przeciwfazie."),
                    ("p", "Istotność: indeks porównany z rozkładem zerowym z przesunięcia kołowego każdego sygnału "
                          "(zachowuje autokorelację, niszczy tylko wspólne wyrównanie w czasie)."),
                    ("note", "Jak czytać mapę stanów", "Wiersz odpowiada uczestnikowi, kolumny czasowi. Czerwony to "
                                                       "przewaga High, niebieski Low, jasny kolor stan bliski "
                                                       "neutralnemu. Pionowe pasy wskazują wspólny stan kilku osób."),
                    ("note", "Przykład indeksu", "Przy trzech osobach indeks 0,97 oznacza średnio około 0,97 osoby w "
                                                 "liczniejszym stanie skrajnym. Nie oznacza 97% synchronizacji ani "
                                                 "97% prawdopodobieństwa zarażania."),
                    ("note", "Termin „zarażanie emocjonalne”", "To operacyjna nazwa współwystępującego pobudzenia. "
                                                               "Wynik nie dowodzi transmisji emocji, jej kierunku ani "
                                                               "wspólnej walencji."),
                    ("note", "Ograniczenia", "W oknie 30 s sygnał po filtrze 0.05 Hz jest bardzo gładki, więc korelacja "
                                             "parowa bywa zawyżona przez wspólny wolny trend. Indeks ko-pobudzenia ma "
                                             "niezerowy poziom oczekiwany — liczy się porównanie z rozkładem zerowym."),
                ],
            },
            {
                "title": "7. Jak formułować wnioski",
                "blocks": [
                    ("list", [
                        "Zacznij od jakości danych i wspólnego zakresu czasu.",
                        "Opisz wielkość oraz kierunek efektu, a dopiero potem jego p-wartość i niepewność.",
                        "Zlokalizuj wynik na osi czasu i połącz go z udokumentowanym przebiegiem badania.",
                        "Sprawdź kontrybutorów — wynik grupowy może być napędzany przez podgrupę.",
                        "Porównaj synchronizację i ko-pobudzenie; mierzą różne zjawiska.",
                        "Nie zamieniaj współwystępowania w twierdzenie o przyczynie lub wspólnej emocji.",
                    ]),
                    ("note", "Bezpieczny szablon", "„W okresie [czas] [osoby] wykazały [kierunek i wielkość "
                                                   "efektu]. Wynik [był/nie był] większy od oczekiwanego z przypadku "
                                                   "po [test/korekta]. Współwystąpił z [zdarzenie], ale GSR samo nie "
                                                   "pozwala ustalić emocji ani przyczyny.”"),
                ],
            },
        ],
    },
    "en": {
        "title": "Analysis methodology",
        "intro": (
            "This document is readable without statistical training. It first explains in plain language which "
            "question each analysis answers and how to read its chart, then gives technical details, parameters, "
            "and limitations. It follows raw GSR through cleaning to synchrony and co-arousal."
        ),
        "sections": [
            {
                "title": "0. Reading the report without statistical training",
                "blocks": [
                    ("p", "Read each result in this order: data quality → effect size → uncertainty → timing and "
                          "study context. A single number is almost never sufficient."),
                    ("list", [
                        "Data quality: inspect the shared time range, rejected artifacts, and flat or truncated signals.",
                        "Effect size: read the synchrony or the co-arousal index.",
                        "Uncertainty: inspect the surrogate-test p and the null distribution.",
                        "Context: locate the result in time and compare it with the task or class activity.",
                    ]),
                    ("params", [
                        ("p-value", "how often a result at least this strong could occur under the chosen chance model; "
                                    "small p does not imply a large, important, or causal effect."),
                        ("Surrogate test", "preserves each person's dynamics but destroys shared timing to model chance."),
                        ("Window / step", "a window is the portion analyzed at once, and step the shift between windows."),
                    ]),
                    ("note", "Example", "p = 0.02 means a result at least this strong appeared in roughly 2 of 100 "
                                      "trials under the chosen chance model. It is not a 98% probability that the "
                                      "hypothesis is true."),
                    ("note", "Most important limitation", "GSR measures physiological arousal, not a specific emotion "
                                                           "or valence. The analysis shows co-occurrence and dynamics, "
                                                           "not causation or emotional transmission."),
                ],
            },
            {
                "title": "1. Overview & research goal",
                "blocks": [
                    ("p", "The app answers: at which moments during a class did participants' physiological arousal rise "
                          "or fall together (spontaneously)? The signal is electrodermal activity (EDA/GSR) in "
                          "microsiemens (µS), recorded by eSense at ~5 Hz. Higher conductance reflects stronger "
                          "sympathetic activation (arousal), regardless of valence."),
                    ("p", "Pipeline: import & sync → cleaning → scaling & decimation → synchrony → emotional contagion "
                          "(co-arousal). Group analyses run on a common time grid."),
                    ("note", "Important", "GSR is a slow, heavily autocorrelated signal: consecutive samples are not "
                                          "independent. Significance is therefore computed with autocorrelation-preserving "
                                          "methods (surrogate test), not classical formulas assuming "
                                          "independent samples."),
                    ("note", "In plain language", "The app does not guess what a person felt. It tests whether "
                                                   "participants' arousal changed similarly in time and whether that "
                                                   "agreement exceeded chance."),
                ],
            },
            {
                "title": "2. Import & time synchronization",
                "blocks": [
                    ("p", "CSV files are parsed (encoding/separator detection, decimal-comma handling). Recordings are "
                          "aligned on a shared absolute clock and only the common window is analyzed."),
                    ("formula", "common window = [ max(start_i) , min(end_i) ] over all participants i"),
                    ("note", "Group-analysis requirement", "If recordings have no shared absolute-time window, the "
                                                               "analysis stops. The app does not automatically align "
                                                               "such recordings relative to the start of each file."),
                    ("note", "Duplicate check", "Identical GSR value sequences are marked with a warning but remain in "
                                                    "the analysis, leaving the decision about their use to the researcher."),
                    ("note", "Reading the chart", "Verify that every line covers the same period and none ends "
                                                    "unexpectedly. Different RAW levels between people are normal and "
                                                    "do not yet imply different reactivity."),
                    ("note", "Limitations", "If timestamps are wrong or missing the common window can be incorrect. The "
                                            "analysis requires reliable timestamps."),
                ],
            },
            {
                "title": "3. Artifact cleaning",
                "blocks": [
                    ("p", "Only genuine sensor artifacts are removed, not real skin-conductance responses (SCRs). Removed "
                          "points are linearly interpolated. Filters are independent:"),
                    ("params", [
                        ("Physiological range", "values outside [min, max] treated as sensor disconnect/rail."),
                        ("Z-score artifact filter", "points > K·SD from the mean flagged as artifacts "
                                                    "(conservative threshold); a filter, not a normalization."),
                        ("Sample-to-sample jump", "sudden amplitude jumps (motion/electrode tap)."),
                        ("IQR rule", "rejects values outside [Q1 − 1.5·IQR, Q3 + 1.5·IQR]."),
                        ("Butterworth", "low-pass (default 0.05 Hz), forward-backward (filtfilt, zero phase shift)."),
                    ]),
                    ("note", "Default settings", "The median-filter window is 5, the Z-score threshold is 5 SD, the jump "
                                                    "threshold is mean change + 5 SD, and the IQR filter is disabled by "
                                                    "default. All filters remain manually configurable."),
                    ("note", "Signal quality", "Based on the rejected-sample share, the app reports: good < 10%, warning "
                                                 "from 10% to below 30%, or poor ≥ 30%. This is informational only and "
                                                 "does not exclude a signal or participant from the analysis."),
                    ("note", "In plain language", "Cleaning is like removing clicks from audio: it should remove "
                                                   "obvious sensor failures while retaining the overall shape of a "
                                                   "genuine response."),
                    ("note", "Limitations", "A 0.05 Hz filter passes components with period ≥ 20 s; faster changes are attenuated."),
                ],
            },
            {
                "title": "4. Normalization & decimation",
                "blocks": [
                    ("p", "The analysis works on z-scores: each signal is standardized within the analysed window "
                          "(the High and Low states, the VSR interview signals) or compared by a correlation, which "
                          "no linear rescaling changes (synchrony, co-movement). Signals are decimated onto a common "
                          "grid (default 1 Hz)."),
                    ("formula", "z = (x − mean(x)) / SD(x)"),
                    ("p", "Before decimation the signals go through an internal min-max scaling to 0–1. It is a "
                          "linear transform of each signal on its own, so it changes no correlation, no z-score and "
                          "no result. The 0–1 scale is available for viewing only: the signals chart with marked "
                          "episodes and analysis_input_merged.csv show z-scores (the default) or min-max (0–1)."),
                    ("note", "In plain language", "z = 0 is a person's average level in the analysed range, z = 1 one "
                                                   "standard deviation above it. We compare change shape, not absolute "
                                                   "level."),
                    ("note", "Does not mean", "z = 1 for one person need not be greater arousal in µS than z = 0.5 for "
                                              "another."),
                ],
            },
            {
                "title": "5. Synchrony analysis",
                "blocks": [
                    ("p", "Windowed cross-correlation of pairs (with lag up to a max), coherence, optionally DTW. Subgroups "
                          "staying synchronized above threshold form episodes; signals are clustered."),
                    ("p", "Significance: an autocorrelation-preserving surrogate test (circular shift / phase randomization) "
                          "destroys only between-person synchrony. Synchrony is signed — anti-phase is not mistaken for synchrony."),
                    ("p", "Divergent synchrony: subgroups in which every pair's windowed r is at or below minus the "
                          "divergent threshold (default −0.30, as Hassan's team asked), with the same episode rules. "
                          "The signals then move systematically in opposite directions: a relation as strong as "
                          "synchrony, with the opposite sign. The VSR flow chart's criteria (“Synchronous = above "
                          "0.8”, “Asynchronous = below 0.1”) are read as the strength of the correlation, its "
                          "absolute value."),
                    ("p", "Decoupling (no synchrony): a stretch between a pair's synchronization and divergent "
                          "episodes, at least as long as the minimum episode, whose correlation over the whole stretch "
                          "(the mean r of its windows) lies within ± the decoupling threshold (default 0.1). Single "
                          "30 s windows after the 0.05 Hz filter swing between −1 and +1, so “every window |r| < 0.1” "
                          "is almost never met; one correlation for the stretch follows Hassan's team's description "
                          "(“the correlation between GSR data within an episode”)."),
                    ("note", "In plain language", "Synchrony asks whether people changed similarly at the same time or "
                                                   "with a small delay. An episode shows when and which subgroup."),
                    ("note", "Lag", "A 5 s lag means the best fit appeared after shifting one signal by five seconds. "
                                     "It does not prove one person caused the other's reaction."),
                    ("note", "Surrogate p", "Small p means similarly strong agreement rarely appeared after deliberately "
                                             "misaligning signals in time. Study context is still needed."),
                ],
            },
            {
                "title": "6. Emotional contagion (co-arousal)",
                "blocks": [
                    ("p", "Each signal is z-scored within the analysis window and coded into states: High (z > threshold), "
                          "Low (z < threshold), Neutral. We detect High/Low episodes (shared extreme state) and co-movement "
                          "(mean pairwise Spearman in the window ≥ threshold)."),
                    ("formula", "Co-arousal index = time-average of max( people High , people Low ); range 0–N"),
                    ("p", "Opposite arousal (as Hassan's team asked, the complement of a shared High and Low state): "
                          "someone is High while someone else is Low, with the same z thresholds and episode rules as "
                          "High/Low. It is no synchrony in step 5 of the VSR interviews."),
                    ("p", "Contributors: for High/Low a participant holds the state for ≥ a fraction of the episode; for "
                          "co-movement they must be positively correlated with the rest of the group (leave-one-out), "
                          "excluding flat/anti-phase participants."),
                    ("p", "Significance: the index is compared to a null from circularly shifting each signal (preserves "
                          "autocorrelation, destroys only shared timing)."),
                    ("note", "Reading the state map", "Rows are participants and columns time. Red is predominantly "
                                                       "High, blue Low, and pale color near-neutral. Vertical bands "
                                                       "show a shared state across people."),
                    ("note", "Index example", "With three people, index 0.97 means about 0.97 people were in the more "
                                                 "populated extreme state at an average moment. It is not 97% "
                                                 "synchrony or a 97% probability of contagion."),
                    ("note", "The term 'emotional contagion'", "This is an operational label for co-occurring arousal. "
                                                               "The result does not prove emotional transmission, its "
                                                               "direction, or shared valence."),
                    ("note", "Limitations", "Within a 30 s window a 0.05 Hz-filtered signal is very smooth, so within-window "
                                            "correlation can be inflated by the shared slow trend. The co-arousal index has a "
                                            "non-zero expected level — comparison to the null is what matters."),
                ],
            },
            {
                "title": "7. Writing conclusions",
                "blocks": [
                    ("list", [
                        "Start with data quality and the shared time range.",
                        "Describe effect direction and size before p and uncertainty.",
                        "Locate the result in time and align it with documented study events.",
                        "Inspect contributors; a group result may be driven by a subgroup.",
                        "Compare synchrony and co-arousal because they measure different things.",
                        "Do not turn co-occurrence into a causal or shared-emotion claim.",
                    ]),
                    ("note", "Safe template", "“In the period [time], [participants] showed [direction and effect "
                                               "size]. The result [did/did not] exceed chance after [test/correction]. "
                                               "It coincided with [event], but GSR alone cannot identify the emotion "
                                               "or establish a cause.”"),
                ],
            },
        ],
    },
}

METHODS_COMPARISON: dict[str, dict[str, Any]] = {
    "pl": {
        "title": "Porównanie metod",
        "intro": "Dwie analizy grupowe mierzą różne aspekty wspólnej dynamiki. Nie porównuj ich liczb bezpośrednio — każda odpowiada na inne pytanie.",
        "headers": ["Metoda", "Co mierzy", "Rozdzielczość", "Wysoka wartość =", "Istotność", "Ograniczenie"],
        "rows": [
            ["Synchronizacja", "Sprzężenie czasowe par (z opóźnieniem)", "Okna przesuwne", "Silne sprzężenie w czasie",
             "Test surogatowy (przesunięcie kołowe/faza)", "Wrażliwa na długość okna"],
            ["Ko-pobudzenie", "Wspólne stany skrajne + ko-ruch", "Okna + kodowanie stanów", "Grupa razem w skrajnym pobudzeniu",
             "Test surogatowy indeksu", "Ko-ruch zawyżony przez gładkość"],
        ],
    },
    "en": {
        "title": "Method comparison",
        "intro": "The two group analyses measure different aspects of shared dynamics. Do not compare their numbers directly — each answers a different question.",
        "headers": ["Method", "What it measures", "Resolution", "High value =", "Significance", "Limitation"],
        "rows": [
            ["Synchrony", "Temporal coupling of pairs (with lag)", "Sliding windows", "Strong temporal coupling",
             "Surrogate test (circular shift/phase)", "Sensitive to window length"],
            ["Contagion", "Shared extreme states + co-movement", "Windows + state coding", "Group jointly in extreme arousal",
             "Surrogate test of the index", "Co-movement inflated by smoothness"],
        ],
    },
}
