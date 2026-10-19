# VibeMind Learning Space

The Learning Space integrates controlled forks of LearnHouse and PenEcho. The
VibeMind wrapper owns authority, MCP contracts, orchestration, truth readback,
and Learning-specific services. The embedded applications remain independent
projects with their own test suites and licenses.

## Pinned Sources

Pins and provenance are authoritative in `deployment/upstream-lock.yml`.

- LearnHouse: `Flissel/learnhouse`, derived from `learnhouse/learnhouse`.
- PenEcho: `Flissel/penecho`, derived from `penecho/penecho`.
- Both sources are licensed `AGPL-3.0-only` at the recorded pins.

Distribution and network use must preserve copyright notices, license text,
and corresponding source availability for the exact deployed modifications.
VibeMind does not claim a right to relicense either embedded application.

## Clone

This repository owns the LearnHouse and PenEcho gitlinks (`.gitmodules` at the
repository root). Inside a standalone clone:

```powershell
git submodule update --init learnhouse penecho
```

From the vibemind-os parent, where this repository is mounted at
`spaces/learning`:

```powershell
git submodule update --init --recursive spaces/learning
```

## Upstream Update Procedure

Perform updates in one child repository at a time. Start from a clean parent
worktree, create a feature branch in the fork, and retain the upstream remote:
inside each child repository the update operation is `git fetch upstream`.

```powershell
git -C spaces/learning/learnhouse remote add upstream https://github.com/learnhouse/learnhouse.git
git -C spaces/learning/learnhouse fetch upstream
git -C spaces/learning/penecho remote add upstream https://github.com/penecho/penecho.git
git -C spaces/learning/penecho fetch upstream
```

Run the relevant baseline and Learning-focused suites before committing a new
child SHA. Update `deployment/upstream-lock.yml` and the parent Gitlink in the
same parent task. Never advance a Gitlink without recording the exact source,
commit, license, test evidence, and known failures.

## Upstream Baseline Commands

LearnHouse API:

```powershell
Set-Location spaces/learning/learnhouse/apps/api
uv sync
uv run pytest -q
```

LearnHouse Web:

```powershell
bun install --cwd spaces/learning/learnhouse/apps/web --frozen-lockfile
bun --cwd spaces/learning/learnhouse/apps/web test
```

PenEcho:

```powershell
npm --prefix spaces/learning/penecho ci
npm --prefix spaces/learning/penecho run check
```

Record dependency/bootstrap failures as baseline limitations. A focused green
test never implies that an unavailable or unexecuted upstream suite is green.

### Baseline Snapshot: 2026-08-24

- PenEcho `d580110`: `npm run check` discovered 527 tests; 519 passed,
  7 failed, and 1 was skipped on Windows. The failures cover three POSIX file
  mode assertions, three macOS/POSIX path assertions, and one temporary-file
  cleanup `EPERM`. No Learning-specific PenEcho changes existed for this run.
- LearnHouse API `5a58c3d`: `uv sync --frozen` could not start because the
  pinned project requires Python `>=3.14.6,<3.14.7`, which was not installed.
- LearnHouse Web `5a58c3d`: Bun 1.3.11 rejected the pinned lockfile version 3;
  frozen installation stopped without modifying the lockfile.

These are upstream/environment baseline limitations, not green-suite claims.
They must be refreshed after the required isolated toolchains are provisioned;
they are not repaired as part of the parent pin commit.

## VibeMind Course Factory HTTP API

`learning-api` exposes a loopback HTTP adapter at `http://127.0.0.1:8090/api/v1`
for the same Course Factory operations the `spaces-learning` MCP tools provide.
Every route builds one Learning event and dispatches it through the durable
dispatcher, so receipts, revisions, review, publication, and readback are
identical across both transports. The registry records the adapter as
`http_api` on the existing `learning` Space; it is not a second authority.

| Method | Path | Role |
| --- | --- | --- |
| `POST` | `/materials/import` | `learning.author` |
| `GET` | `/materials/{source_id}` | any Learning role |
| `POST` | `/course-factory/jobs` | `learning.author` |
| `GET` | `/course-factory/jobs/{job_id}` | any Learning role |
| `POST` | `/course-factory/jobs/{job_id}/review` | `learning.reviewer` |
| `POST` | `/course-factory/jobs/{job_id}/publish` | `learning.publisher` |
| `GET` | `/courses/{course_id}` | any Learning role |

Request flow: import material (a path relative to `LEARNING_IMPORT_ROOT`, or a
public HTTPS URL) -> create a job from the returned `source_id` -> poll the job
until `review_ready` -> review -> publish with `confirmed: true` and an
`approval_ref`. Generation never publishes on its own. All AI stages run
through OpenFang in `learning-worker`; the API never holds a provider key.

Every mutation needs an `Idempotency-Key`; replaying it returns the original
result, reusing it for a different request is `409`. Errors use
`{code, message, retryable, correlation_id}` and never echo request input.

```powershell
$headers = @{
  Authorization = "Bearer $env:LEARNING_VIBEMIND_SERVICE_KEY"
  "X-VibeMind-Actor-ID" = "local-owner"
  "X-VibeMind-Space-ID" = "learning"
  "X-VibeMind-Roles" = "learning.author"
  "X-VibeMind-Correlation-ID" = [guid]::NewGuid().ToString()
  "Idempotency-Key" = [guid]::NewGuid().ToString()
}
$body = @{
  course_id = "<course-uuid>"
  expected_course_revision = 1
  title = "Industrial AI Safety"
  audience = "Maintenance engineers"
  level = "advanced"
  locale = "de-DE"
  learning_objectives = @("Assess AI-assisted maintenance risks")
  source_ids = @("<source-uuid-from-material-import>")
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8090/api/v1/course-factory/jobs `
  -Headers $headers -ContentType application/json -Body $body
```

## Learning-Claw

Konversations-Agent fuer Kurs-Erzeugung und Ueben (Spec 2026-10-19). Code-Stand: nichts davon ist
gestartet oder abgenommen.

| Teil | Ort | Port |
| --- | --- | --- |
| MCP-Sidecar (17 Werkzeuge, nur loopback) | `claw/server.py` | `127.0.0.1:8139` |
| Modell-Tuer (Shim, Werkzeuge ueber `SHIM_EXTRA_MCP_CONFIG`) | `marketing_shim.py` | `127.0.0.1:8141` |
| openclaw-Gateway (Container `learning-claw`, ohne Kanaele, ohne MCP-Eintrag) | `claw/gateway/` | `18897` (loopback im Container, nicht veroeffentlicht) |

Der Gateway kennt den Sidecar nicht: die Werkzeuge laufen in der Claude-CLI des Shims. `AGENTS.md` und
`skills/` liegen nur unter `claw/`; `anbinden.sh` kopiert sie ins Container-Volume.

```powershell
# Host-Dienste (idempotent; -Pruefen, -Liste, -Nur learning_claw_mcp)
powershell -File spaces\learning\claw\scripts\learning-claw-starten.ps1
# Gateway (Container) hochfahren, dann abnehmen
docker compose -f spaces/learning/claw/gateway/docker-compose.yml up -d
bash spaces/learning/claw/gateway/anbinden.sh
```

Wer `anbinden.sh` oder die Probe nutzen will, setzt `LEARNING_CLAW_MCP_TOKEN` (mindestens 32 Zeichen) schon vor dem Start
des Startskripts in der Umgebung und nutzt denselben Wert danach; ein vom Skript selbst erzeugtes Token wird bewusst
nirgends gespeichert und ist danach unbekannt. Mit `-Nur` fuer nur einen Dienst verlangt das Skript ein vorab gesetztes Token.

Abnahme (`anbinden.sh`): die 17 erwarteten Werkzeugnamen (`werkzeug_probe.py` befragt den Sidecar), kein Name
mit `send`/`approve`/`save`/`submit`, kein LearnHouse-/PenEcho-Port und kein MCP-Eintrag in `openclaw.json`,
die drei Fertigkeiten sichtbar. Das Shim-Skript laesst sich mit `LEARNING_CLAW_SHIM_SCRIPT` ueberschreiben.

Veroeffentlichungsregel: der Agent entwirft nur. `veroeffentlichung_anzeigen` zeigt Titel, unbelegte Aussagen,
`revision` (Auftrag) und `kurs_revision` (LearnHouse-Kurs) und merkt sich die Anzeige samt MCP-Sitzung; ein Token
gibt es nicht. `veroeffentlichen(job_id, revision, kurs_revision)` gilt nur, wenn genau diese Anzeige aus einer
frueheren, anderen MCP-Sitzung stammt (der Shim startet je Nachricht ein frisches `claude -p`), hoechstens
10 Minuten alt ist und Auftrag wie Kurs noch auf diesen Revisionen stehen; einmal verwendbar. Ohne
Sitzungskennung wird abgelehnt. Es gibt kein Werkzeug, das im Namen der lernenden Person speichert oder abgibt.

## Current Evidence Boundary

The pins and registry documentation are structural evidence only. They do not
prove that the Learning runtime, OpenFang agent, providers, application
readback, or MCP Golden Path is live.
