"""The drawing surface: task, pens, eraser, undo, hint and the submit button.

Shared by the claw's local drawing page and learning-api's ``/zeichnen/<token>`` page.
The script posts the drawing to ``location.pathname`` and the hint request to
``location.pathname + '/hinweis'``; embedded in a player it tells the parent window
that the drawing was submitted (no data, only the event).
"""

from __future__ import annotations

import html

SUBMITTED_MESSAGE = "learning-zeichnung-abgegeben"


def flaeche_html(aufgabe: str, *, mathe: bool = False) -> str:
    if mathe:
        from spaces.learning.services.werkstatt.formeltext import klartext

        aufgabe = klartext(aufgabe)
    farben = "".join(
        f'<button type="button" class="farbe" data-farbe="{f}" style="background:{f}"></button>'
        for f in ("#111111", "#d22", "#16c", "#1a7f37", "#e8a400")
    )
    return (
        f'<p class="aufgabe">{html.escape(aufgabe) or "Zeichne deine Loesung."}</p>'
        f'<div class="leiste">{farben}'
        '<label>Staerke <input id="staerke" type="range" min="2" max="24" value="5"></label>'
        '<button type="button" id="radierer">Radierer</button>'
        '<button type="button" id="zurueck">Rueckgaengig</button>'
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
document.getElementById('leeren').onclick = () => { if (confirm('Alles loeschen?')) { striche.length = 0; neu(); } };
const status = document.getElementById('status'), knopf = document.getElementById('abgeben');
knopf.onclick = async () => {
  const sichtbar = striche.filter(s => s.farbe !== '#ffffff');
  if (!sichtbar.length) { status.textContent = 'Die Zeichenflaeche ist noch leer.'; return; }
  if (!confirm('Zeichnung jetzt abgeben? Danach ist sie nicht mehr aenderbar.')) return;
  knopf.disabled = true; status.textContent = 'Wird abgegeben und bewertet, das dauert bis zu einige Minuten ...';
  try {
    const antwort = await fetch(location.pathname, {method: 'POST', headers: {'content-type': 'application/json'},
      body: JSON.stringify({png: c.toDataURL('image/png'), striche: sichtbar.map(s => ({punkte: s.punkte, farbe: s.farbe, breite: s.breite}))})});
    const d = await antwort.json(); const box = document.getElementById('ergebnis');
    if (!d.ok) { status.textContent = 'Nicht abgegeben: ' + (d.fehler || 'unbekannter Fehler'); knopf.disabled = false; return; }
    status.textContent = 'Abgegeben.';
    const h = document.createElement('h2'); h.textContent = (d.daten.angenommen === false ? 'Vorlaeufiges Ergebnis: ' : 'Ergebnis: ') + Math.round((d.daten.punkte || 0) * 100) + ' %'; box.appendChild(h);
    if (d.daten.hinweis_bewertung) { const n = document.createElement('p'); n.className = 'hinweistext'; n.textContent = d.daten.hinweis_bewertung; box.appendChild(n); }
    for (const k of d.daten.kriterien || []) { const p = document.createElement('p');
      p.textContent = (k.kriterium || '') + ' (' + Math.round((k.punkte || 0) * 100) + ' %): ' + (k.rueckmeldung || ''); box.appendChild(p); }
    box.scrollIntoView({behavior: 'smooth', block: 'start'});
    if (window.parent !== window) window.parent.postMessage({type: '__SUBMITTED__'}, '*');
  } catch (err) { status.textContent = 'Abgabe fehlgeschlagen: ' + err; knopf.disabled = false; }
};
const hinweisKnopf = document.getElementById('hinweis'), hinweisText = document.getElementById('hinweistext');
hinweisKnopf.onclick = async () => {
  hinweisKnopf.disabled = true; hinweisText.textContent = 'Hinweis wird geholt ...';
  try {
    const d = await (await fetch(location.pathname + '/hinweis', {method: 'POST', headers: {'content-type': 'application/json'}, body: '{}'})).json();
    hinweisText.textContent = d.ok ? 'Hinweis: ' + d.hinweis : 'Kein Hinweis: ' + (d.fehler || 'nicht verfuegbar');
  } catch (err) { hinweisText.textContent = 'Kein Hinweis: ' + err; }
  hinweisKnopf.disabled = false;
};
weiss();
""".replace("__SUBMITTED__", SUBMITTED_MESSAGE)


def seite_html(titel: str, inhalt: str, *, nonce: str, skript: bool = False) -> bytes:
    script = f'<script nonce="{nonce}">{SKRIPT}</script>' if skript else ""
    return (
        "<!doctype html><html lang=\"de\"><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{html.escape(titel)}</title><style>"
        "body{font-family:sans-serif;max-width:64rem;margin:1.5rem auto;padding:0 1rem}"
        ".aufgabe{font-size:1.15rem}.leiste{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap;margin:.5rem 0}"
        ".farbe{width:2rem;height:2rem;border:2px solid #888;border-radius:50%;cursor:pointer}"
        "canvas{border:1px solid #999;width:100%;height:auto;touch-action:none;background:#fff}"
        ".abgeben{font-size:1.1rem;padding:.5rem 1.5rem}.hinweistext{background:#f4f4ee;padding:.4rem .6rem}"
        ".hinweistext:empty{display:none}</style>"
        f"<body><h1>{html.escape(titel)}</h1>{inhalt}{script}</body></html>"
    ).encode("utf-8")
