"""The drawing surface: task, pens, eraser, undo, hint and the submit button.

Shared by the claw's local drawing page and learning-api's ``/zeichnen/<token>`` page.
The script posts the drawing to ``location.pathname`` and the hint request to
``location.pathname + '/hinweis'``; embedded in a player it tells the parent window
that the drawing was submitted (no data, only the event).
"""

from __future__ import annotations

import html

SUBMITTED_MESSAGE = "learning-zeichnung-abgegeben"


def _klartext(text: str) -> str:
    from spaces.learning.services.werkstatt.formeltext import klartext

    return klartext(text)


def flaeche_html(aufgabe: str, *, mathe: bool = False) -> str:
    if mathe:
        from spaces.learning.services.werkstatt.formeltext import klartext

        aufgabe = klartext(aufgabe)
    farben = "".join(
        f'<button type="button" class="farbe" data-farbe="{f}" style="background:{f}"></button>'
        for f in ("#111111", "#d22", "#16c", "#1a7f37", "#e8a400")
    )
    return (
        f'<p class="aufgabe" id="aufgabe">{html.escape(aufgabe) or "Zeichnen Sie Ihre Lösung."}</p>'
        '<button type="button" id="mehr" class="mehr" hidden>Mehr anzeigen</button>'
        f'<div class="leiste">{farben}'
        '<label>Stärke <input id="staerke" type="range" min="2" max="24" value="5"></label>'
        '<button type="button" id="radierer">Radierer</button>'
        '<button type="button" id="zurueck">Rückgängig</button>'
        '<button type="button" id="leeren">Leeren</button>'
        '<button type="button" id="hinweis">Hinweis</button></div>'
        '<p id="hinweistext" class="hinweistext"></p>'
        '<canvas id="flaeche" width="1000" height="620"></canvas>'
        '<p><button type="button" id="abgeben" class="abgeben">Abgeben</button> '
        '<span id="status"></span></p><div id="ergebnis"></div>'
    )


SKRIPT = r"""
const c = document.getElementById('flaeche'), g = c.getContext('2d');
const striche = []; let aktiv = null, farbe = '#111111', radierer = false;
// Keep the drawing coordinates isotropic: the box is as high as 60 % of the window at its
// CSS width, and the internal height follows (height:auto keeps the same ratio, no stretch).
function hoehe(breite, fenster){ return Math.min(3000, Math.max(620, Math.round(1000 * fenster * 0.6 / Math.max(breite, 1)))); }
c.height = hoehe(c.clientWidth || 1000, window.innerHeight || 0);
function weiss(){ g.fillStyle = '#fff'; g.fillRect(0, 0, c.width, c.height); }
function zeichne(s){ if (s.punkte.length < 1) return; g.strokeStyle = s.farbe; g.lineWidth = s.breite;
  g.lineCap = 'round'; g.lineJoin = 'round'; g.beginPath(); g.moveTo(...s.punkte[0]);
  for (const p of s.punkte) g.lineTo(...p); g.stroke(); }
function neu(){ weiss(); striche.forEach(zeichne); }
function punkt(e){ const r = c.getBoundingClientRect();
  return [Math.round((e.clientX - r.left) * c.width / r.width), Math.round((e.clientY - r.top) * c.height / r.height)]; }
c.addEventListener('pointerdown', e => { c.setPointerCapture(e.pointerId);
  aktiv = {farbe: radierer ? '#ffffff' : farbe, breite: +document.getElementById('staerke').value * (radierer ? 3 : 1), punkte: [punkt(e)]};
  striche.push(aktiv); });
c.addEventListener('pointermove', e => { if (!aktiv) return; aktiv.punkte.push(punkt(e)); zeichne(aktiv); });
['pointerup','pointercancel'].forEach(t => c.addEventListener(t, () => { aktiv = null; }));
document.querySelectorAll('.farbe').forEach(b => b.onclick = () => { farbe = b.dataset.farbe; radierer = false; });
document.getElementById('radierer').onclick = () => { radierer = !radierer; };
document.getElementById('zurueck').onclick = () => { striche.pop(); neu(); };
document.getElementById('leeren').onclick = () => { if (confirm('Alles löschen?')) { striche.length = 0; neu(); } };
const status = document.getElementById('status'), knopf = document.getElementById('abgeben');
knopf.onclick = async () => {
  const sichtbar = striche.filter(s => s.farbe !== '#ffffff');
  if (!sichtbar.length) { status.textContent = 'Die Zeichenfläche ist noch leer.'; return; }
  if (!confirm('Zeichnung jetzt abgeben? Danach ist sie nicht mehr änderbar.')) return;
  knopf.disabled = true; status.textContent = 'Wird abgegeben und bewertet, das dauert bis zu einigen Minuten ...';
  try {
    const antwort = await fetch(location.pathname, {method: 'POST', headers: {'content-type': 'application/json'},
      body: JSON.stringify({png: c.toDataURL('image/png'), striche: sichtbar.map(s => ({punkte: s.punkte, farbe: s.farbe, breite: s.breite}))})});
    const d = await antwort.json(); const box = document.getElementById('ergebnis');
    if (!d.ok) { status.textContent = 'Nicht abgegeben: ' + (d.fehler || 'unbekannter Fehler'); knopf.disabled = false; return; }
    status.textContent = 'Abgegeben.';
    const h = document.createElement('h2'); h.textContent = (d.daten.angenommen === false ? 'Vorläufiges Ergebnis: ' : 'Ergebnis: ') + Math.round((d.daten.punkte || 0) * 100) + ' %'; box.appendChild(h);
    if (d.daten.hinweis_bewertung) { const n = document.createElement('p'); n.className = 'hinweistext'; n.textContent = d.daten.hinweis_bewertung; box.appendChild(n); }
    for (const k of d.daten.kriterien || []) { const p = document.createElement('p');
      p.textContent = (k.kriterium || '') + ' (' + Math.round((k.punkte || 0) * 100) + ' %): ' + (k.rueckmeldung || ''); box.appendChild(p); }
    box.scrollIntoView({behavior: 'smooth', block: 'start'});
    if (window.parent !== window) window.parent.postMessage({type: '__SUBMITTED__'}, '*');
  } catch (err) { status.textContent = 'Abgabe fehlgeschlagen: ' + err; knopf.disabled = false; }
};
const aufgabeText = document.getElementById('aufgabe'), mehr = document.getElementById('mehr');
if (aufgabeText.scrollHeight > aufgabeText.clientHeight + 1) { mehr.hidden = false; }
mehr.onclick = () => { const offen = aufgabeText.classList.toggle('offen'); mehr.textContent = offen ? 'Weniger anzeigen' : 'Mehr anzeigen'; };
const hinweisKnopf = document.getElementById('hinweis'), hinweisText = document.getElementById('hinweistext');
hinweisKnopf.onclick = async () => {
  hinweisKnopf.disabled = true; hinweisText.textContent = 'Hinweis wird geholt ...';
  try {
    const d = await (await fetch(location.pathname + '/hinweis', {method: 'POST', headers: {'content-type': 'application/json'}, body: '{}'})).json();
    hinweisText.textContent = d.ok ? 'Hinweis: ' + d.hinweis : 'Kein Hinweis: ' + (d.fehler || 'nicht verfügbar');
  } catch (err) { hinweisText.textContent = 'Kein Hinweis: ' + err; }
  hinweisKnopf.disabled = false;
};
weiss();
""".replace("__SUBMITTED__", SUBMITTED_MESSAGE)


def seite_html(titel: str, inhalt: str, *, nonce: str, skript: bool = False) -> bytes:
    # The drawing page starts with the task itself; only message pages get a heading.
    kopf = "" if 'id="flaeche"' in inhalt else f"<h1>{html.escape(titel)}</h1>"
    script = f'<script nonce="{nonce}">{SKRIPT}</script>' if skript else ""
    return (
        "<!doctype html><html lang=\"de\"><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{html.escape(titel)}</title><style>"
        "body{font-family:sans-serif;max-width:64rem;margin:.75rem auto;padding:0 1rem}"
        ".aufgabe{font-size:1.05rem;margin:.3rem 0;display:-webkit-box;-webkit-box-orient:vertical;"
        "-webkit-line-clamp:6;line-clamp:6;overflow:hidden}.aufgabe.offen{display:block;-webkit-line-clamp:unset}"
        ".mehr{margin:0 0 .3rem}"
        ".leiste{display:flex;gap:.4rem;align-items:center;flex-wrap:nowrap;overflow-x:auto;margin:.3rem 0}"
        ".leiste button,.leiste label{white-space:nowrap;flex:none}"
        ".farbe{width:1.6rem;height:1.6rem;border:2px solid #888;border-radius:50%;cursor:pointer}"
        "canvas{border:1px solid #999;width:100%;height:auto;touch-action:none;background:#fff}"
        ".abgeben{font-size:1.1rem;padding:.5rem 1.5rem}.hinweistext{background:#f4f4ee;padding:.4rem .6rem}"
        ".hinweistext:empty{display:none}</style>"
        f"<body>{kopf}{inhalt}{script}</body></html>"
    ).encode("utf-8")


def kriterien_anzeige(bewertung: dict, ergebnis: dict, *, mathe: bool = False) -> list[dict]:
    """Per-criterion rows for the result: the rubric description, never the internal id.

    ``bewertung`` is the canvas evaluation, ``ergebnis`` the submit result; the
    descriptions come from the session turn's feedback (same criterion ids).
    """
    feedback = ((ergebnis.get("session") or {}).get("feedback") or {}).get("criteria") or []
    beschreibung = {
        str(k.get("criterion_id")): str(k.get("description") or "")
        for k in feedback if isinstance(k, dict)
    }
    umschreiben = _klartext if mathe else (lambda text: text)
    zeilen = []
    for nummer, c in enumerate(bewertung.get("criteria") or [], start=1):
        text = beschreibung.get(str(c.get("criterion_id"))) or f"Kriterium {nummer}"
        rueck = c.get("feedback")
        zeilen.append({
            "kriterium": umschreiben(text),
            "punkte": c.get("score"),
            "rueckmeldung": umschreiben(rueck) if isinstance(rueck, str) else rueck,
        })
    return zeilen
