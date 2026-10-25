"""Explainer video from a presentation: slide images + spoken narration, no filming.

Runs on the host (voice service, browser and ffmpeg live there):

    python -m spaces.learning.services.lernsequenz.video_bau <player-link> <out-dir>

Reads the presentation's public view (slides and narration, never solutions), renders each
slide at 1920×1080, speaks each sentence with the local voice service (built-in voice, no
cloned voice), and writes ``video.mp4`` plus ``zeiten.json`` (start/end per slide). The
learning-api CLI ``video`` turns that into a video sequence whose checkpoints sit at the
slide ends.
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

VOICE_URL = os.environ.get("TON_DIENST_URL", "http://127.0.0.1:8138")
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
_PAUSE_S = 0.7

SLIDE_HTML = """<!doctype html><html lang="de"><meta charset="utf-8"><style>
body{{margin:0;width:1920px;height:1080px;background:#F4F5F2;font-family:Inter,"Segoe UI",system-ui,sans-serif;color:#16191D}}
.card{{position:absolute;inset:60px;background:#fff;border-radius:36px;padding:110px 130px;display:flex;flex-direction:column;justify-content:center;gap:48px;box-shadow:0 20px 60px rgba(22,25,29,.08)}}
.eyebrow{{font-size:28px;letter-spacing:.08em;font-weight:700;color:#0E6B5C;text-transform:uppercase}}
h1{{margin:0;font-size:76px;line-height:1.12;font-weight:800}}
.statement{{font-size:54px;line-height:1.3;font-weight:700;max-width:1500px}}
.points{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:28px}}
.point{{background:#F4F5F2;border-radius:28px;padding:34px 40px;font-size:40px;line-height:1.3;display:flex;gap:26px}}
.point b{{color:#0E6B5C}}
.foot{{position:absolute;left:130px;right:130px;bottom:90px;display:flex;justify-content:space-between;font-size:26px;color:#5C6168}}
</style><div class="card"><div class="eyebrow">{course}</div><h1>{title}</h1>{body}</div>
<div class="foot"><span>{source}</span><span>{number}</span></div></html>"""


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read())


def data_url(player_link: str) -> str:
    parts = urlsplit(player_link)
    return urlunsplit(parts._replace(path=parts.path.rstrip("/") + "/daten"))


def sentences(text: str) -> list[str]:
    """Short pieces for the voice service; long sentences are split at commas."""
    pieces: list[str] = []
    for sentence in re.split(r"(?<=[.!?…])\s+", " ".join(text.split())):
        while len(sentence) > 280:
            cut = sentence.rfind(", ", 0, 280)
            cut = cut + 1 if cut > 80 else 280
            pieces.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if sentence:
            pieces.append(sentence)
    return pieces


def slide_page(slide: dict, *, course: str, number: str, source: str) -> str:
    body = ""
    if slide.get("statement"):
        body += f'<div class="statement">{html.escape(slide["statement"])}</div>'
    if slide.get("points"):
        body += '<div class="points">' + "".join(
            f'<div class="point"><b>{i}</b><span>{html.escape(p)}</span></div>'
            for i, p in enumerate(slide["points"][:6], start=1)
        ) + "</div>"
    return SLIDE_HTML.format(course=html.escape(course), title=html.escape(slide["title"]), body=body,
                             source=html.escape(source), number=html.escape(number))


def speak(text: str, target: Path) -> None:
    body = json.dumps({"text": text, "sprache": "de", "stimme": "eingebaut"}).encode("utf-8")
    request = urllib.request.Request(f"{VOICE_URL}/sprechen", data=body,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=600) as response:
        target.write_bytes(response.read())


def duration(path: Path) -> float:
    probe = subprocess.run(
        [FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True, text=True, check=False
    ).stderr
    match = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", probe)
    if not match:
        raise RuntimeError(f"no duration for {path.name}")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _ffmpeg(*args: str) -> None:
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def build(player_link: str, out: Path) -> dict:
    from playwright.sync_api import sync_playwright

    data = _get_json(data_url(player_link))
    slides = [s for s in data["sections"] if s["kind"] == "slide"]
    if not slides:
        raise ValueError("the sequence has no slides")
    titles = {s["ref"]: s["title"] for s in data.get("sources", [])}
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        for index, slide in enumerate(slides, start=1):
            source = next((titles[r] for r in slide.get("refs", []) if r in titles), "")
            page.set_content(slide_page(slide, course=data["title"], number=f"{index} / {len(slides)}",
                                        source=f"Quelle: {source}" if source else ""))
            page.screenshot(path=str(out / f"folie-{index:02d}.png"))
        browser.close()

    times: dict[str, dict[str, float]] = {}
    segments: list[Path] = []
    clock = 0.0
    for index, slide in enumerate(slides, start=1):
        parts = []
        for number, piece in enumerate(sentences(slide["narration"]), start=1):
            part = out / f"ton-{index:02d}-{number:02d}.wav"
            if not part.exists():
                speak(piece, part)
            parts.append(part)
        listing = out / f"ton-{index:02d}.txt"
        listing.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
        voice = out / f"ton-{index:02d}.wav"
        _ffmpeg("-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(voice))
        length = duration(voice) + _PAUSE_S
        segment = out / f"teil-{index:02d}.mp4"
        _ffmpeg("-loop", "1", "-framerate", "25", "-i", str(out / f"folie-{index:02d}.png"), "-i", str(voice),
                "-af", f"apad=pad_dur={_PAUSE_S}", "-t", f"{length:.3f}",
                "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p", "-r", "25",
                "-c:a", "aac", "-ar", "44100", "-ac", "1", str(segment))
        real = duration(segment)
        times[slide["section_id"]] = {"start": round(clock, 2), "end": round(clock + real, 2)}
        clock += real
        segments.append(segment)
    listing = out / "teile.txt"
    listing.write_text("".join(f"file '{s.name}'\n" for s in segments), encoding="utf-8")
    _ffmpeg("-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", "-movflags", "+faststart",
            str(out / "video.mp4"))
    # WebM beside the MP4: Chromium builds without H.264 play VP9, Safari/iPad play MP4.
    _ffmpeg("-i", str(out / "video.mp4"), "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "38", "-row-mt", "1",
            "-deadline", "good", "-c:a", "libopus", "-b:a", "64k", str(out / "video.webm"))
    result = {"sequence_id": data["sequence_id"], "duration_s": round(clock, 2), "slides": times}
    (out / "zeiten.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    summary = build(sys.argv[1], Path(sys.argv[2]))
    print(json.dumps({"duration_s": summary["duration_s"], "slides": len(summary["slides"])}))
