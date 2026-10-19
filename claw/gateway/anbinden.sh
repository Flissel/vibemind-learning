#!/usr/bin/env bash
# anbinden.sh — Abnahme des learning-claw-Gateways. Wiederholbar.
#
# Die Werkzeuge laufen NICHT im Gateway: sie haengen ueber SHIM_EXTRA_MCP_CONFIG in der
# Claude-CLI des Host-Shims (:8141). Die MCP-Probe von openclaw sieht sie darum nie.
# Statt dessen befragt werkzeug_probe.py den Sidecar (127.0.0.1:8139) selbst.
#
# Das Gateway-Token steht nie in einer Befehlszeile (Argument) und nie in einer Ablaufverfolgung
# der Shell: es geht ueber stdin in eine Temp-Datei (Modus 600), die per `docker compose cp`
# ins Volume kommt und danach geloescht wird.
#
# Voraussetzung: LEARNING_CLAW_MCP_TOKEN steht in der Umgebung (dasselbe Token wie beim Sidecar);
# die Probe sendet es als Bearer.
set -euo pipefail

HIER="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAW="$(cd "$HIER/.." && pwd)"
cd "$HIER"

ERWARTET="antwort_geben bewertung_zeigen entwurf_ablehnen fortschritt hinweis kurs_erzeugen \
kurs_lesen kurs_status material_importieren material_status naechste_aufgabe sitzung_starten \
tutor_fragen veroeffentlichen veroeffentlichung_anzeigen zeichenaufgabe_oeffnen zeichenhinweis"

if [ "$(docker inspect -f '{{.State.Status}}' learning-claw 2>/dev/null || echo fehlt)" != "running" ]; then
  echo "ABBRUCH: learning-claw laeuft nicht (docker compose up -d in $HIER)." >&2
  exit 1
fi

if [ -z "${LEARNING_CLAW_MCP_TOKEN:-}" ]; then
  echo "ABBRUCH: LEARNING_CLAW_MCP_TOKEN ist nicht gesetzt (gleiches Token wie beim Sidecar)." >&2
  exit 1
fi

ROT=0

echo "1) Konfiguration pruefen, bevor sie eingespielt wird"
# Kein LearnHouse-/PenEcho-Port und kein MCP-Server in der Gateway-Konfiguration.
if grep -qE ":(1338|9000|13000|3888)([^0-9]|$)" config/openclaw.json; then
  echo "   FEHL openclaw.json nennt einen LearnHouse-/PenEcho-Port (1338|9000|13000|3888)"; ROT=$((ROT+1))
fi
if grep -q '"mcp"' config/openclaw.json; then
  echo "   FEHL openclaw.json enthaelt einen mcp-Eintrag (Werkzeuge gehoeren in den Shim)"; ROT=$((ROT+1))
fi
if [ "$ROT" -ne 0 ]; then echo "ROT: $ROT" >&2; exit "$ROT"; fi

echo "2) Saat -> Laufzeit-Volume (Konfig; AGENTS.md und Fertigkeiten aus claw/, kein zweiter Stand im Git)"
# Das Kopieren der Saat loescht das Gateway-Token. Vorhandenes zuerst lesen (nur in einer Variable),
# sonst ein neues erzeugen.
TOKEN="$(docker compose exec -T learning-claw openclaw config get gateway.auth.token 2>/dev/null | tr -d '\r\n "' || true)"
NEU=0
if [ -z "$TOKEN" ]; then
  echo "   Gateway-Token fehlt -> neues erzeugen"
  TOKEN="$(head -c 32 /dev/urandom | base64 | tr -d '=+/' | cut -c1-40)"
  NEU=1
fi
umask 077
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
# Token ueber stdin, nicht als Argument: die fertige Konfiguration entsteht nur in der Temp-Datei.
printf '%s' "$TOKEN" | "${PYTHON:-python}" -c '
import json, sys
cfg = json.load(open(sys.argv[1], encoding="utf-8"))
cfg["gateway"]["auth"]["token"] = sys.stdin.read()
json.dump(cfg, open(sys.argv[2], "w", encoding="utf-8"), indent=2)
' config/openclaw.json "$TMP"
unset TOKEN
docker compose cp "$TMP" learning-claw:/home/node/.openclaw/openclaw.json
rm -f "$TMP"
docker compose exec -T learning-claw mkdir -p /home/node/.openclaw/workspace/skills
docker compose cp "$CLAW/AGENTS.md" learning-claw:/home/node/.openclaw/workspace/AGENTS.md
docker compose cp "$CLAW/skills/." learning-claw:/home/node/.openclaw/workspace/skills/
if [ "$NEU" -eq 1 ]; then
  docker compose restart learning-claw >/dev/null
  sleep 5
fi

echo "3) Werkzeuge des Sidecars (127.0.0.1:8139)"
PYTHON="${PYTHON:-python}"
if ! GEFUEHRT="$("$PYTHON" "$HIER/werkzeug_probe.py")"; then
  echo "   FEHL Sidecar nicht befragbar (laeuft learning_claw_mcp?)"; exit 1
fi
for w in $ERWARTET; do
  if printf '%s\n' "$GEFUEHRT" | grep -qx "$w"; then echo "   ok   $w"; else echo "   FEHL $w"; ROT=$((ROT+1)); fi
done
# Alles, was nicht erwartet ist, ist zu viel.
while IFS= read -r name; do
  [ -z "$name" ] && continue
  case " $ERWARTET " in *" $name "*) ;; *) echo "   FEHL unerwartetes Werkzeug: $name"; ROT=$((ROT+1));; esac
done <<< "$GEFUEHRT"
# Negativ: nichts Sendendes, Genehmigendes, Speicherndes, Abgebendes.
if printf '%s\n' "$GEFUEHRT" | grep -qi 'send\|approve\|save\|submit'; then
  echo "   FEHL verbotenes Werkzeug sichtbar: $(printf '%s\n' "$GEFUEHRT" | grep -i 'send\|approve\|save\|submit' | tr '\n' ' ')"
  ROT=$((ROT+1))
fi
# Das Gateway selbst fuehrt keinen MCP-Server.
if docker compose exec -T learning-claw openclaw mcp list 2>&1 | grep -qi learning; then
  echo "   FEHL Gateway kennt einen learning-MCP-Server (gehoert in den Shim)"; ROT=$((ROT+1))
fi

echo "4) Fertigkeiten"
FERTIG="$(docker compose exec -T learning-claw openclaw skills 2>&1 || true)"
for f in kurs-aus-material ueben zeichenaufgabe; do
  if printf '%s' "$FERTIG" | grep -q "$f"; then echo "   ok   $f"; else echo "   FEHL $f"; ROT=$((ROT+1)); fi
done

if [ "$ROT" -ne 0 ]; then
  echo "ROT: $ROT" >&2
  exit "$ROT"
fi
echo "Gateway abgenommen: 17 Werkzeuge, 3 Fertigkeiten, nichts Sendendes, kein MCP im Gateway, kein LearnHouse-/PenEcho-Port."
