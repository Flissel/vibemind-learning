"""An AI-drawn scene for a task: one FLUX.2 picture from the ComfyUI already running on this machine.

ComfyUI is only used here, never started, stopped or asked to unload models. One picture at a time: if
its queue is not empty, nothing is submitted. The description of a task never leaves this machine.
"""

from __future__ import annotations

import io
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

URL_ENV = "LEARNING_COMFYUI_URL"
MODELL = "FLUX.2 klein"
FORMATE: dict[str, tuple[int, int]] = {"quer": (1024, 768), "hoch": (768, 1024), "quadrat": (896, 896)}
MAX_BESCHREIBUNG = 600
MAX_BYTES = 580_000
ZEITLIMIT_S = 420
GNADE_S = 90  # after a submit without an answer: how long the job may be missing everywhere
TAKT_S = 2
_ABLAUF = Path(__file__).with_name("szene_flux2.json")
_HIER = frozenset({"127.0.0.1", "localhost"})
_PNG = b"\x89PNG\r\n\x1a\n"
# What every picture must not contain. FLUX has no separate negative prompt, so it is part of the text.
_RAHMEN = ("Realistic photograph, natural light, neutral everyday setting. No text, no letters, no captions, "
           "no logos, no brand names, no watermark. No real person, no celebrity, no recognisable individual.")


class SzeneFehler(RuntimeError):
    """The picture could not be made; the message is meant for the owner."""


def _UHR() -> float:
    return time.monotonic()


def _SCHLAF(sekunden: float) -> None:
    time.sleep(sekunden)


def adresse() -> str:
    """Where ComfyUI listens: LEARNING_COMFYUI_URL or the usual local port. Only this machine is allowed."""
    roh = os.environ.get(URL_ENV, "").strip() or "http://127.0.0.1:8188"
    teile = urlsplit(roh)
    if teile.scheme != "http" or teile.hostname not in _HIER:
        raise SzeneFehler("Die Bild-KI wird nur auf diesem Rechner angesprochen")
    return roh.rstrip("/")


def rahmen(beschreibung: str) -> str:
    """The description on one line, followed by the fixed rules."""
    return f"{' '.join(beschreibung.split()).rstrip('.')}. {_RAHMEN}"


def ablauf(prompt: str, breite: int, hoehe: int, seed: int) -> dict[str, Any]:
    """The ComfyUI graph (`szene_flux2.json`): 4 = text, 6/7 = size, 8 = seed, 12 = save."""
    graph: dict[str, Any] = json.loads(_ABLAUF.read_text(encoding="utf-8"))
    graph["4"]["inputs"]["text"] = prompt
    graph["6"]["inputs"].update(width=breite, height=hoehe)
    graph["7"]["inputs"].update(width=breite, height=hoehe)
    graph["8"]["inputs"]["noise_seed"] = int(seed)
    return graph


def _schlange(http: httpx.Client) -> dict[str, Any]:
    antwort = http.get("/queue", timeout=10)
    antwort.raise_for_status()
    daten = antwort.json()
    return daten if isinstance(daten, dict) else {}


def besetzt(http: httpx.Client) -> bool:
    """Whether ComfyUI is drawing or has something waiting (someone else's picture)."""
    schlange = _schlange(http)
    return bool(schlange.get("queue_running") or schlange.get("queue_pending"))


def _abfragen(http: httpx.Client, pid: str) -> bytes | None:
    """One look at the history: the PNG when done, None while ComfyUI is still drawing."""
    eintrag = (http.get(f"/history/{pid}", timeout=30).json() or {}).get(pid)
    if not eintrag:
        return None
    status = eintrag.get("status") or {}
    if status.get("status_str") == "error":
        for meldung in status.get("messages") or []:
            if isinstance(meldung, list) and len(meldung) > 1 and meldung[0] == "execution_error":
                text = f"{meldung[1].get('exception_type', '')} {meldung[1].get('exception_message', '')}".lower()
                if "outofmemory" in text or "out of memory" in text:
                    raise SzeneFehler("Der Grafikspeicher reicht gerade nicht; bitte später noch einmal")
        raise SzeneFehler("Die Bild-KI ist beim Zeichnen fehlgeschlagen")
    for ausgabe in (eintrag.get("outputs") or {}).values():
        for bild in ausgabe.get("images") or []:
            antwort = http.get("/view", timeout=60, params={
                "filename": bild["filename"], "subfolder": bild.get("subfolder", ""), "type": bild.get("type", "output")})
            antwort.raise_for_status()
            return antwort.content
    return None


def erzeugen(http: httpx.Client, beschreibung: str, format: str, seed: int, zeitlimit_s: int = ZEITLIMIT_S) -> bytes:
    """Submits one picture and waits for it (PNG). The job id is ours, so a submit that got no answer is
    looked for instead of sent again: a second job would occupy the graphics card for minutes."""
    breite, hoehe = FORMATE[format]
    pid = str(uuid.uuid4())
    unbestaetigt = False
    try:
        antwort = http.post("/prompt", timeout=120, json={
            "prompt": ablauf(rahmen(beschreibung), breite, hoehe, seed), "prompt_id": pid})
    except httpx.TimeoutException:
        unbestaetigt = True
    else:
        if antwort.status_code >= 400:
            if "not in" in antwort.text and "flux" in antwort.text.lower():
                raise SzeneFehler("Das Bildmodell FLUX.2 fehlt auf diesem Rechner")
            raise SzeneFehler("Die Bild-KI lehnt den Auftrag ab")
    beginn = _UHR()
    while True:
        try:
            png = _abfragen(http, pid)
        except httpx.HTTPError:
            png = None  # ComfyUI is busy loading models; keep looking
        if png is not None:
            return png
        vergangen = _UHR() - beginn
        if unbestaetigt and vergangen >= GNADE_S:
            try:
                bekannt = pid in json.dumps(_schlange(http))
            except httpx.HTTPError:
                bekannt = True  # unclear: keep waiting until the limit
            if not bekannt:
                raise SzeneFehler("Die Bild-KI hat den Auftrag nicht angenommen")
            unbestaetigt = False
        if vergangen >= zeitlimit_s:
            raise SzeneFehler("Das Zeichnen hat zu lange gedauert; das Bild kann später noch fertig werden, "
                              "wird dann aber nicht übernommen")
        _SCHLAF(TAKT_S)


def als_jpeg(png: bytes) -> tuple[bytes, int, int]:
    """The picture as JPEG within the upload limit, with its width and height."""
    if not png.startswith(_PNG):
        raise SzeneFehler("Die Bild-KI hat kein Bild geliefert")
    try:
        from PIL import Image

        with Image.open(io.BytesIO(png)) as roh:
            bild = roh.convert("RGB")
    except Exception as fehler:  # noqa: BLE001 - whatever is wrong with the bytes, it is not a picture
        raise SzeneFehler("Die Bild-KI hat kein lesbares Bild geliefert") from fehler
    for guete in (86, 76, 66):
        puffer = io.BytesIO()
        bild.save(puffer, "JPEG", quality=guete, optimize=True)
        if puffer.tell() <= MAX_BYTES:
            return puffer.getvalue(), bild.width, bild.height
    raise SzeneFehler("Das Bild ist auch verkleinert zu groß")
