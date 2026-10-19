#!/usr/bin/env bash
# anbinden.sh — Abnahme des learning-claw-Gateways. Wiederholbar.
#
# Die Werkzeuge laufen NICHT im Gateway: sie haengen ueber SHIM_EXTRA_MCP_CONFIG in der
# Claude-CLI des Host-Shims (:8141). Die MCP-Probe von openclaw sieht sie darum nie.
# Statt dessen befragt werkzeug_probe.py den Sidecar (127.0.0.1:8139) selbst.
#
# Das Gateway-Token wird nur ueber Umgebung/Dateien weitergereicht, nie als Argument
# und nie in einer Ablaufverfolgung der Shell ausgegeben.
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

ROT=0

echo "1) Konfiguration pruefen, bevor sie eingespielt wird"
# Kein LearnHouse-/PenEcho-Port und kein MCP-Server in der Gateway-Konfiguration.
for port in 1338 9000 13000 3888; do
  if grep -q "$port" config/openclaw.json; then
    echo "   FEHL openclaw.json nennt Port $port (LearnHouse/PenEcho)"; ROT=$((ROT+1))
  fi
done
if grep -q '"mcp"' config/openclaw.json; then
  echo "   FEHL openclaw.json enthaelt einen mcp-Eintrag (Werkzeuge gehoeren in den Shim)"; ROT=$((ROT+1))
fi
if [ "$ROT" -ne 0 ]; then echo "ROT: $ROT" >&2; exit "$ROT"; fi

echo "2) Saat -> Laufzeit-Volume (Konfig; AGENTS.md und Fertigkeiten aus claw/, kein zweiter Stand im Git)"
docker compose cp config/openclaw.json learning-claw:/home/node/.openclaw/openclaw.json
docker compose exec -T learning-claw mkdir -p /home/node/.openclaw/workspace/skills
docker compose cp "$CLAW/AGENTS.md" learning-claw:/home/node/.openclaw/workspace/AGENTS.md
docker compose cp "$CLAW/skills/." learning-claw:/home/node/.openclaw/workspace/skills/

# Die Saat traegt KEIN Gateway-Token — das Kopieren loescht jedes vorhandene. Also hier eins
# setzen, wenn keins dasteht (idempotent, bleibt im Volume).
if [ -z "$(docker compose exec -T learning-claw openclaw config get gateway.auth.token 2>/dev/null | tr -d '\r\n "')" ]; then
  echo "   Gateway-Token fehlt -> neues erzeugen"
  NEUES_TOKEN="$(head -c 32 /dev/urandom | base64 | tr -d '=+/' | cut -c1-40)"
  docker compose exec -T -e NEU="$NEUES_TOKEN" learning-claw \
    node -e 'require("node:child_process").execFileSync("openclaw",["config","set","gateway.auth.token",process.env.NEU],{stdio:"ignore"})'
  unset NEUES_TOKEN
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
