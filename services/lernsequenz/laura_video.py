"""Explainer video by Laura: slides as stills, Laura speaks, cuts, subtitles and renders.

Runs on the host (browser for the slide images, ffmpeg for the WebM):

    python -m spaces.learning.services.lernsequenz.laura_video <player-link> <out-dir> [--course <id>]

1. slide images from the presentation's public view (``video_bau.render_slides``),
2. uploaded to learning-api (``PUT /api/v1/lernsequenz-medien``) so Laura can fetch them,
3. Laura: one still per slide, one beat per narration piece (a still lasts at most 10 s,
   so longer narration becomes several beats on the same still), built-in voice, no
   crossfade (beat times stay exact), subtitles, loudness, 16:9,
4. MP4 from Laura, WebM beside it, both uploaded, and learning-api registers the video
   sequence with checkpoints at the slide ends (``POST /api/v1/lernsequenzen/<id>/video``);
   with ``--course`` it is embedded into the course's first lesson right away.

Environment: LAURA_API_URL, LAURA_TOKEN, LEARNING_API_URL (learning-api as Laura and this
host reach it), LEARNING_VIBEMIND_SERVICE_KEY, optional LAURA_STIMME ("standard").
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from uuid import uuid4

import httpx

from spaces.learning.services.lernsequenz.video_bau import _ffmpeg, _get_json, data_url, render_slides

FPS = 30
_STILL_MAX_S = 10.0
# German narration runs at roughly 14 characters per second; with the beat padding a
# piece of up to 110 characters stays inside one 10-second still.
_PIECE_CHARS = 110


def pieces(text: str, limit: int = _PIECE_CHARS) -> list[str]:
    """Narration in beats that fit one still: sentences, long ones split at commas/words."""
    out: list[str] = []
    for sentence in re.split(r"(?<=[.!?…])\s+", " ".join(text.split())):
        while len(sentence) > limit:
            cut = sentence.rfind(", ", 0, limit)
            cut = cut + 1 if cut > limit // 3 else sentence.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            out.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if sentence:
            if out and len(out[-1]) + len(sentence) + 1 <= limit and not out[-1].endswith((".", "!", "?", "…")):
                out[-1] = f"{out[-1]} {sentence}"
            else:
                out.append(sentence)
    return out


def slide_times(beat_slides: list[int], beats: list[dict], section_ids: list[str]) -> dict[str, dict[str, float]]:
    """Start/end per slide from Laura's beat placement (frames at FPS)."""
    times: dict[str, dict[str, float]] = {}
    for slide_index, beat in zip(beat_slides, beats, strict=True):
        section = section_ids[slide_index]
        start, end = beat["seq_in"] / FPS, beat["seq_out"] / FPS
        if section in times:
            times[section]["end"] = round(end, 2)
        else:
            times[section] = {"start": round(start, 2), "end": round(end, 2)}
    return times


class Laura:
    def __init__(self) -> None:
        self._http = httpx.Client(base_url=os.environ["LAURA_API_URL"].rstrip("/"), timeout=60,
                                  headers={"X-Laura-Token": os.environ.get("LAURA_TOKEN", "")})

    def call(self, method: str, path: str, **kw) -> dict:
        response = self._http.request(method, path, **kw)
        if response.status_code >= 400:
            raise RuntimeError(f"Laura {method} {path}: {response.status_code} {response.text[:300]}")
        return response.json()

    def job(self, job_id: str, timeout_s: float = 1800) -> dict:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            job = self.call("GET", f"/jobs/{job_id}")
            if job["status"] in ("succeeded", "completed", "done"):
                result = job.get("result_json")
                return json.loads(result) if isinstance(result, str) else (result or {})
            if job["status"] in ("failed", "error", "cancelled"):
                raise RuntimeError(f"Laura job {job_id}: {str(job.get('error_json'))[:300]}")
            time.sleep(3)
        raise TimeoutError(f"Laura job {job_id} did not finish")

    def asset(self, asset_id: str, timeout_s: float = 600) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.call("GET", f"/assets/{asset_id}").get("sha256"):
                return
            status = self.call("GET", f"/assets/{asset_id}/import-status")
            if status.get("phase") in ("error", "cancelled"):
                raise RuntimeError(f"Laura import {asset_id}: {status}")
            time.sleep(2)
        raise TimeoutError(f"Laura import {asset_id} did not finish")

    def download(self, export_id: str, target: Path, timeout_s: float = 3600) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            export = self.call("GET", f"/exports/{export_id}")
            if export["status"] == "error":
                raise RuntimeError(f"Laura export {export_id}: {export.get('error')}")
            if export["status"] == "ready":
                link = self._http.get(f"/exports/{export_id}/download-url")
                if link.status_code == 200:
                    with httpx.stream("GET", link.json()["url"], timeout=600) as stream, target.open("wb") as out:
                        for chunk in stream.iter_bytes():
                            out.write(chunk)
                    return
            time.sleep(5)
        raise TimeoutError(f"Laura export {export_id} was not ready")


class Learning:
    def __init__(self) -> None:
        self.base = os.environ["LEARNING_API_URL"].rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {os.environ['LEARNING_VIBEMIND_SERVICE_KEY']}",
            "X-VibeMind-Actor-ID": "laura-video", "X-VibeMind-Space-ID": "learning",
            "X-VibeMind-Roles": "learning.author",
        }

    def _h(self) -> dict[str, str]:
        return {**self._headers, "X-VibeMind-Correlation-ID": str(uuid4())}

    def upload(self, path: Path, name: str) -> str:
        with path.open("rb") as stream:
            response = httpx.put(f"{self.base}/api/v1/lernsequenz-medien/{name}", content=stream,
                                 headers=self._h(), timeout=600)
        response.raise_for_status()
        return f"{self.base}/lernsequenz-medien/{name}"

    def post(self, path: str, body: dict) -> dict:
        response = httpx.post(f"{self.base}{path}", json=body, headers=self._h(), timeout=900)
        response.raise_for_status()
        return response.json()


def build(player_link: str, out: Path, *, course_id: str | None = None) -> dict:
    data = _get_json(data_url(player_link))
    slides = render_slides(data, out)
    learning, laura = Learning(), Laura()
    stem = "laura-" + re.sub(r"[^A-Za-z0-9-]", "", data["sequence_id"])[-40:]

    project = laura.call("POST", "/projects", json={"name": f"Lernvideo {data['title']}"[:120],
                                                    "sequence_rate_num": FPS, "sequence_rate_den": 1})
    pid = project["id"]
    stills: list[str] = []
    for index, _slide in enumerate(slides, start=1):
        url = learning.upload(out / f"folie-{index:02d}.png", f"{stem}-folie-{index:02d}.png")
        imported = laura.call("POST", f"/projects/{pid}/assets/import",
                              json={"source_url": url, "display_name": f"folie-{index:02d}.png"})
        laura.asset(imported["asset_id"])
        still = laura.job(laura.call("POST", f"/projects/{pid}/stills", json={
            "asset_id": imported["asset_id"], "dauer_s": _STILL_MAX_S, "bewegung": "ruhig", "format": "quer",
        })["job_id"])
        stills.append(still["asset_id"])

    beats, beat_slides = [], []
    for index, slide in enumerate(slides):
        for text in pieces(slide["narration"]):
            beats.append({"text": text, "asset_id": stills[index], "src_in_frame": 0, "pad_frames": 12})
            beat_slides.append(index)
    reel = laura.call("POST", f"/projects/{pid}/narrated-reel", json={
        "beats": beats, "crossfade_frames": 0, "final_fade_frames": 12, "backend": "sidecar",
        "language": "de", "voice_id": None if os.environ.get("LAURA_STIMME", "standard") == "standard"
        else os.environ["LAURA_STIMME"], "render": False, "ausgabe": "quer",
    })
    placed = laura.job(reel["job_id"], timeout_s=3600)
    if len(placed.get("beats", [])) != len(beats):
        raise RuntimeError("Laura placed a different number of beats")
    clipped = [w for w in placed.get("warnings", []) if "clip" in str(w).lower()]
    if clipped:
        raise RuntimeError(f"narration longer than its still: {clipped[:3]}")
    render = laura.call("POST", f"/timelines/{reel['timeline_id']}/render-reel", json={
        "format": "quer", "vertical": False, "captions": True, "caption_preset": "wide",
        "caption_source": "voiceover", "rahmung": "einpassen", "loudnorm": True, "kennzeichnung": "auto",
    })
    mp4 = out / "video.mp4"
    laura.download(render["export_id"], mp4)
    _ffmpeg("-i", str(mp4), "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "38", "-row-mt", "1",
            "-deadline", "good", "-c:a", "libopus", "-b:a", "64k", str(out / "video.webm"))
    learning.upload(mp4, f"{stem}.mp4")
    learning.upload(out / "video.webm", f"{stem}.webm")
    times = slide_times(beat_slides, placed["beats"], [s["section_id"] for s in slides])
    (out / "zeiten.json").write_text(json.dumps({"sequence_id": data["sequence_id"], "slides": times}, indent=2),
                                     encoding="utf-8")
    registered = learning.post(f"/api/v1/lernsequenzen/{data['sequence_id']}/video",
                               {"media_name": f"{stem}.mp4", "slide_times": times})
    if course_id:
        registered["einbettung"] = learning.post("/api/v1/lernsequenzen/einbetten", {"course_id": course_id})
    return {**registered, "beats": len(beats), "laura_project": pid}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="laura_video")
    parser.add_argument("player_link")
    parser.add_argument("out", type=Path)
    parser.add_argument("--course")
    args = parser.parse_args(argv)
    print(json.dumps(build(args.player_link, args.out, course_id=args.course), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
