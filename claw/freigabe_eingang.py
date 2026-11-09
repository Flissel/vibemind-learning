"""Client fuer den Freigabe-Eingang (`/api/freigaben/*`) und der Abholer der Kurs-Werkstatt.

Die Werkstatt meldet vorbereitete Kursfreigaben an den Eingang; der Betreiber entscheidet in der
Sales-Oberflaeche. Die Herkunft leitet der Server aus dem Schluessel ab. Jeder Fehler (Netz, HTTP,
JSON, Schluesseldatei) wird nur mit Fehlertyp (und HTTP-Status) geloggt und ergibt `None`/`False`;
es kommt nie eine Ausnahme nach aussen. Geloggt wird nie ein Schluessel, ein Rumpf oder ein Link.

Der Abholer laeuft im Claw-Prozess: nur dort kennt die Freigabeseite ihr Geheimnis. Das Geheimnis
und der `/freigabe/`-Link gelangen nie in Eintrag, Details, Ergebnis oder Log.

Umgebung: `FREIGABE_URL` (Basis der Sales-UI) und `FREIGABE_SCHLUESSEL_DATEI` (Pfad der Schluesseldatei).
Fehlt eins oder die Datei, ist alles aus.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import re
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "FreigabeEingang", "aus_umgebung", "abholer_einmal", "abholer_schleife", "starte_abholer",
]

LOG = logging.getLogger(__name__)
TIMEOUT_S = 10
TAKT_S = 60.0
_PFAD = "/api/freigaben"
ART_ZEIGEN = "kurs_freigabe_zeigen"
MAX_ALTER = timedelta(minutes=15)  # aeltere Auftraege laufen nicht mehr (Final-Review M3)
_KURS_ID = re.compile(r"[A-Za-z0-9_.\-]{1,128}")
_VERBOTEN = ("/freigabe/",)


@functools.lru_cache(maxsize=1)
def _tls_kontext() -> ssl.SSLContext:
    """Systemspeicher plus certifi, falls installiert. Windows-Python 3.11/OpenSSL 1.1.1 kennt ISRG Root X2
    nur als Zwischenzertifikat und baut die Let's-Encrypt-Kette sonst ueber die abgelaufene Querzertifizierung."""
    kontext = ssl.create_default_context()
    try:
        import certifi
    except ImportError:
        return kontext
    kontext.load_verify_locations(cafile=certifi.where())
    return kontext


def _oeffnen_mit_tls(req: urllib.request.Request, timeout: float | None = None) -> Any:
    return urllib.request.urlopen(req, timeout=timeout, context=_tls_kontext())


class FreigabeEingang:
    def __init__(self, url: str, schluessel_datei: Path, *,
                 oeffnen: Callable[..., Any] = _oeffnen_mit_tls) -> None:
        self.url = url.rstrip("/")
        self.schluessel_datei = Path(schluessel_datei)
        self._oeffnen = oeffnen

    def __repr__(self) -> str:
        return "FreigabeEingang()"

    def _anfrage(self, methode: str, pfad: str, rumpf: dict[str, Any] | None = None) -> Any | None:
        """Eine Anfrage; die Antwort als JSON oder `None` bei jedem Fehler."""
        teil = pfad.split("/")[1] if "/" in pfad else pfad
        try:
            schluessel = self.schluessel_datei.read_text(encoding="utf-8").strip()  # pro Anfrage
            if not schluessel:
                raise ValueError("schluessel_leer")
            kopf = {"Authorization": f"Bearer {schluessel}"}
            daten = None
            if rumpf is not None:
                daten = json.dumps(rumpf, default=str).encode("utf-8")
                kopf["Content-Type"] = "application/json"
            req = urllib.request.Request(f"{self.url}{_PFAD}{pfad}", data=daten, headers=kopf, method=methode)
            with self._oeffnen(req, timeout=TIMEOUT_S) as antwort:
                return json.loads(antwort.read())
        except urllib.error.HTTPError as e:
            e.close()
            LOG.warning("Freigabe-Eingang %s %s: HTTP %d", methode, teil, e.code)
        except Exception as e:
            LOG.warning("Freigabe-Eingang %s %s: %s", methode, teil, type(e).__name__)
        return None

    def eintragen(self, art: str, quelle_ref: str, titel: str, link: str | None = None,
                  details: dict[str, Any] | None = None) -> str | None:
        """Meldet einen Eintrag und gibt die Kennung zurueck. Enthaelt ein Text einen `/freigabe/`-Link,
        wird nichts gesendet."""
        try:
            texte = [titel, link or "", json.dumps(details, ensure_ascii=False, default=str) if details else ""]
            if any(v in t for t in texte for v in _VERBOTEN):
                LOG.warning("Freigabe-Eingang eintrag: nicht gesendet (Link-Verdacht)")
                return None
            rumpf: dict[str, Any] = {"art": art, "quelle_ref": quelle_ref, "titel": titel}
            if link is not None:
                rumpf["link"] = link
            if details is not None:
                rumpf["details"] = details
        except Exception as e:
            LOG.warning("Freigabe-Eingang eintrag: nicht pruefbar (%s)", type(e).__name__)
            return None
        antwort = self._anfrage("POST", "/eintrag", rumpf)
        kennung = antwort.get("kennung") if isinstance(antwort, dict) else None
        return kennung if isinstance(kennung, str) else None

    def schliessen(self, quelle_ref: str, status: str = "erledigt") -> int | None:
        antwort = self._anfrage("POST", "/schliessen", {"quelle_ref": quelle_ref, "status": status})
        n = antwort.get("geschlossen") if isinstance(antwort, dict) else None
        return n if isinstance(n, int) and not isinstance(n, bool) else None

    def _liste(self, pfad: str, name: str) -> list[dict[str, Any]] | None:
        antwort = self._anfrage("GET", pfad)
        zeilen = antwort.get(name) if isinstance(antwort, dict) else None
        return [z for z in zeilen if isinstance(z, dict)] if isinstance(zeilen, list) else None

    def offene(self) -> list[dict[str, Any]] | None:
        return self._liste("/offen", "eintraege")

    def auftraege(self) -> list[dict[str, Any]] | None:
        return self._liste("/auftraege", "auftraege")

    def ergebnis(self, auftrag_id: str, ok: bool, fehler: str | None = None) -> bool:
        rumpf: dict[str, Any] = {"ok": ok}
        if fehler is not None:
            rumpf["fehler"] = fehler
        antwort = self._anfrage("POST", f"/auftraege/{urllib.parse.quote(auftrag_id, safe='')}", rumpf)
        return isinstance(antwort, dict) and antwort.get("ok") is True


def aus_umgebung() -> FreigabeEingang | None:
    """Der Client, wenn beide Umgebungsvariablen gesetzt sind und die Schluesseldatei existiert."""
    url = os.environ.get("FREIGABE_URL", "").strip()
    datei = os.environ.get("FREIGABE_SCHLUESSEL_DATEI", "").strip()
    if not url or not datei or not Path(datei).is_file():
        return None
    return FreigabeEingang(url, Path(datei))


def _kurs_id(wert: object) -> str | None:
    return wert if isinstance(wert, str) and _KURS_ID.fullmatch(wert) else None


def _anzeigen_standard(course_id: str) -> dict[str, Any]:
    # Lazy: werkzeuge importiert dieses Modul fuer den Meldehaken.
    from . import werkzeuge

    return werkzeuge.werkstatt_freigabe_anzeigen(course_id)


def _freigaben_standard(course_id: str) -> list[dict[str, Any]]:
    """Freigaben des Kurses, neueste zuerst, ueber denselben LearningClient-Pfad wie die Werkzeuge."""
    from . import werkzeuge

    antwort = werkzeuge._ws_get(f"/kurse/{werkzeuge._seg(course_id)}/freigaben")
    daten = antwort.get("daten") if antwort.get("ok") else None
    liste = daten.get("freigaben") if isinstance(daten, dict) else None
    if not isinstance(liste, list):
        raise LookupError("freigaben_nicht_lesbar")
    return [f for f in liste if isinstance(f, dict)]


def _angelegt_am(wert: object) -> datetime | None:
    """Anlagezeit des Auftrags (ISO 8601 mit Zeitzone); `None`, wenn sie fehlt oder nicht lesbar ist."""
    if not isinstance(wert, str):
        return None
    try:
        zeit = datetime.fromisoformat(wert.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return zeit if zeit.tzinfo is not None else None  # ohne Zeitzone nicht vergleichbar


def _jetzt_utc() -> datetime:
    return datetime.now(timezone.utc)


def _auftrag_bearbeiten(client: Any, auftrag: dict[str, Any], anzeigen: Callable[[str], dict[str, Any]],
                        jetzt: Callable[[], datetime] = _jetzt_utc) -> None:
    auftrag_id = auftrag.get("id")
    if not isinstance(auftrag_id, str) or not auftrag_id:
        return
    if auftrag.get("art") != ART_ZEIGEN:
        client.ergebnis(auftrag_id, False, "unbekannte_art")
        return
    angelegt = _angelegt_am(auftrag.get("angelegt_am"))
    if angelegt is not None and jetzt() - angelegt > MAX_ALTER:
        # Ein alter Klick (Claw lange aus) oeffnet keinen Browser und schickt keinen Link mehr.
        client.ergebnis(auftrag_id, False, "veraltet")
        return
    nutzlast = auftrag.get("nutzlast")
    course_id = _kurs_id(nutzlast.get("course_id") if isinstance(nutzlast, dict) else None)
    if course_id is None:
        client.ergebnis(auftrag_id, False, "ungueltig")
        return
    try:
        antwort = anzeigen(course_id)
    except Exception as e:
        LOG.warning("Freigabe-Abholer: Anzeigen fehlgeschlagen (%s)", type(e).__name__)
        client.ergebnis(auftrag_id, False, type(e).__name__)
        return
    if not isinstance(antwort, dict) or antwort.get("ok") is not True:
        client.ergebnis(auftrag_id, False, "anzeigen_fehlgeschlagen")
        return
    daten = antwort.get("daten")
    if not isinstance(daten, dict) or not (daten.get("link_geoeffnet") or daten.get("handy_link_gesendet")):
        # Sonst meldete der Server dem Betreiber "Link gesendet", obwohl nichts ankam.
        client.ergebnis(auftrag_id, False, "kein_link")
        return
    client.ergebnis(auftrag_id, True)


def abholer_einmal(
    client: Any,
    anzeigen: Callable[[str], dict[str, Any]] | None = None,
    freigaben_lesen: Callable[[str], list[dict[str, Any]]] | None = None,
    jetzt: Callable[[], datetime] | None = None,
) -> int:
    """Ein Durchlauf: Auftraege abarbeiten, erledigte Kursfreigaben schliessen. Gibt die Zahl der
    bearbeiteten Auftraege plus geschlossenen Eintraege zurueck. Ein Auftrag, der laut `angelegt_am`
    aelter als `MAX_ALTER` ist, laeuft nicht und wird als `veraltet` gemeldet."""
    anzeigen = anzeigen or _anzeigen_standard
    freigaben_lesen = freigaben_lesen or _freigaben_standard
    n = 0
    for auftrag in client.auftraege() or []:
        _auftrag_bearbeiten(client, auftrag, anzeigen, jetzt or _jetzt_utc)
        n += 1
    for eintrag in client.offene() or []:
        ref = eintrag.get("quelle_ref")
        if not isinstance(ref, str) or not ref.startswith("kurs:"):
            continue
        course_id = _kurs_id(ref[len("kurs:"):])
        if course_id is None:
            continue
        try:
            freigaben = freigaben_lesen(course_id)
        except Exception as e:
            LOG.warning("Freigabe-Abholer: Freigaben nicht lesbar (%s)", type(e).__name__)
            continue
        if freigaben and freigaben[0].get("zustand") == "freigegeben":
            if client.schliessen(f"kurs:{course_id}", "erledigt") is not None:
                n += 1
    return n


def abholer_schleife(client: Any, stop: threading.Event, takt_s: float = TAKT_S, **kwargs: Any) -> None:
    """Laeuft bis `stop` gesetzt wird; ein Fehler in einem Durchlauf wird nur nach Typ geloggt."""
    # Erst warten, dann arbeiten: so zeigt kein Link auf den Port, bevor uvicorn ihn bindet.
    while not stop.wait(takt_s):
        try:
            abholer_einmal(client, **kwargs)
        except Exception as e:
            LOG.warning("Freigabe-Abholer: Durchlauf fehlgeschlagen (%s)", type(e).__name__)


def starte_abholer(
    client: FreigabeEingang | None = None, stop: threading.Event | None = None
) -> threading.Thread | None:
    """Daemon-Faden im Claw-Prozess; `None`, wenn der Client nicht eingerichtet ist."""
    client = client or aus_umgebung()
    if client is None:
        LOG.info("Freigabe-Abholer: aus (Freigabe-Eingang nicht eingerichtet)")
        return None
    faden = threading.Thread(
        target=abholer_schleife, args=(client, stop or threading.Event()), name="freigabe-abholer", daemon=True
    )
    faden.start()
    LOG.info("Freigabe-Abholer: gestartet")
    return faden
