# Zugang zur Lern-Hülle: Link, Cookie und ihr Schlüssel

## Wie der Zugang funktioniert

- `python -m spaces.learning.huelle.link` erzeugt einen Zugangslink (`/lernen/zugang/<Token>`) und schickt ihn
  per Telegram. Der Link wird nie ausgegeben.
- Das Token ist ein HMAC über sein Ablaufdatum und gilt 30 Tage (`huelle/zugang.py`).
- Wer den Link öffnet, bekommt das Cookie `huelle_zugang` (ebenfalls 30 Tage) und arbeitet unter der Kennung aus
  `LEARNING_HUELLE_ACTOR`.
- Einzelne Tokens lassen sich nicht widerrufen. Ungültig werden sie nur gemeinsam, durch einen neuen Schlüssel.

## Eigener Schlüssel: `LEARNING_HUELLE_ZUGANG_SCHLUESSEL`

Link und Cookie werden mit `LEARNING_HUELLE_ZUGANG_SCHLUESSEL` signiert, wenn die Zeile gesetzt ist. Ohne sie
gilt wie früher der Dienstschlüssel `LEARNHOUSE_LEARNING_SERVICE_KEY`.

Der eigene Schlüssel trennt zwei Dinge:

| Schlüssel | Wer kennt ihn | Wofür |
|---|---|---|
| `LEARNHOUSE_LEARNING_SERVICE_KEY` | Hülle, LearnHouse, Learning-API | Übergabe aus LearnHouse, Personen-Cookie, Gruppen, Katalog, Dienstkopf |
| `LEARNING_HUELLE_ZUGANG_SCHLUESSEL` | nur die Hülle | Zugangslink und Zugangscookie |

Ein Wechsel des Dienstschlüssels müsste an drei Stellen gleichzeitig passieren (PC und VM). Der Zugangsschlüssel
steht nur in `huelle.env` auf dem PC.

Regeln beim Lesen (`huelle/einstellung.py`): mindestens 32 Zeichen, ein anderer Wert als der Dienstschlüssel,
Leerraum am Rand zählt nicht.

## Alle Zugangslinks ungültig machen

1. `huelle.env` sichern.
2. `LEARNING_HUELLE_ZUGANG_SCHLUESSEL` auf einen neuen Zufallswert setzen (genau eine Zeile).
3. Hülle neu starten.
4. Mit `python -m spaces.learning.huelle.link` einen neuen Link schicken.

Danach sind alle früheren Links und Zugangscookies ungültig. Personen, die über LearnHouse kommen, merken nichts:
Übergabe und Personen-Cookie hängen weiter am Dienstschlüssel.

## Hinweis für Proben

Wer den Zugangslink in einem Skript aufruft, gibt weder die Adresse noch den Pfad der Antwort aus, auch nicht den
der Zielseite nach einer Weiterleitung. Das Token steht im Pfad.
