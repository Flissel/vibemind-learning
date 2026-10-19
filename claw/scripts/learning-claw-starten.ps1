<#
.SYNOPSIS
    Startet die Host-Dienste des Learning-Claws, falls sie nicht laufen.

.BESCHREIBUNG
    Zwei Dienste, beide nur auf 127.0.0.1:
      learning_claw_mcp   :8139  MCP-Sidecar (die 17 Werkzeuge; haelt den Schluessel)
      learning_claw_shim  :8141  Modell-Tuer; reicht den Sidecar ueber SHIM_EXTRA_MCP_CONFIG
                                 in die Claude-CLI (der Container erreicht den Sidecar nie)

    Idempotent: je Dienst wird der Port geprueft, gestartet wird nur, was schweigt.
    Ausgabe geht in Dateien (logs\learning\<name>.log), nicht in eine Pipe - Prozesse
    mit Pipe-Ausgabe sterben mit der startenden PowerShell.

    Der Learning-Repo ist unter <vibemind-os>\spaces\learning eingehaengt; der Sidecar
    startet mit dem vibemind-os-Wurzelverzeichnis als Arbeitsordner, damit
    `spaces.learning.claw.server` importierbar ist.

    Shim-Skript: Umgebung LEARNING_CLAW_SHIM_SCRIPT, sonst
    <vibemind-os>\spaces\marketing\claw\shim\marketing_shim.py.

    BEARER-TOKEN. Der Sidecar haelt den Schluessel mit Veroeffentlichungs-Rolle und der
    Container erreicht jeden Loopback-Port des Hosts. Darum verlangt der Sidecar
    `Authorization: Bearer <LEARNING_CLAW_MCP_TOKEN>` (mindestens 32 Zeichen). Ist die Variable
    nicht gesetzt, erzeugt dieses Skript je Start ein frisches Zufallstoken (kryptografischer RNG)
    und gibt es NUR an die Umgebung der beiden Kindprozesse; es steht in keinem Log und auf keiner
    Platte. Wer anbinden.sh oder die Probe nutzt, setzt LEARNING_CLAW_MCP_TOKEN selbst (gleicher Wert).

.PARAMETER Pruefen
    Nur nachsehen und berichten, nichts starten.

.PARAMETER Liste
    Die Dienstliste als JSON ausgeben (name, port, args, was) und nichts starten.

.PARAMETER Nur
    Nur diese Dienste (Namen, kommagetrennt) betrachten.
#>
[CmdletBinding()]
param([switch]$Pruefen, [switch]$Liste, [string[]]$Nur)

$ErrorActionPreference = 'Stop'

# claw\scripts -> claw -> spaces\learning -> spaces -> vibemind-os -> Vibemind_V1
$LearningRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)    # <vibemind-os>\spaces\learning
$OsRoot       = Split-Path -Parent (Split-Path -Parent $LearningRoot)    # vibemind-os
$Root         = Split-Path -Parent $OsRoot                              # Vibemind_V1
$Venv         = Join-Path $Root '.venv\Scripts\python.exe'
$LogDir       = Join-Path $Root 'logs\learning'

$ShimSkript = $env:LEARNING_CLAW_SHIM_SCRIPT
if (-not $ShimSkript) { $ShimSkript = Join-Path $OsRoot 'spaces\marketing\claw\shim\marketing_shim.py' }

# Token fuer Sidecar UND Shim: vorhandenes nehmen, sonst frisch erzeugen (nie in Log/Datei).
$TokenErzeugt = $false
$McpToken = $env:LEARNING_CLAW_MCP_TOKEN
if ($McpToken -and $McpToken.Length -lt 32) {
    throw 'LEARNING_CLAW_MCP_TOKEN ist gesetzt, aber kuerzer als 32 Zeichen - laengeres Token setzen oder die Variable entfernen.'
}
if (-not $McpToken) {
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $bytes = New-Object byte[] 32
    $rng.GetBytes($bytes)
    $rng.Dispose()
    $McpToken = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
    $TokenErzeugt = $true
}

$Dienste = @(
    @{
        Name = 'learning_claw_mcp'
        Port = 8139
        Args = @('-u', '-m', 'spaces.learning.claw.server')
        Cwd  = $OsRoot
        # Schluessel holt der Sidecar selbst aus der Learning-.env (server.py::_load_env_fallback).
        Env  = @{ LEARNING_CLAW_MCP_TOKEN = $McpToken }
        Was  = 'MCP-Sidecar (die Werkzeuge des Agenten, nur 127.0.0.1)'
    },
    @{
        Name = 'learning_claw_shim'
        Port = 8141
        Args = @($ShimSkript, '--host', '127.0.0.1', '--port', '8141')
        Cwd  = $Root
        Env  = @{
            # Der Shim reicht das Token ueber ${LEARNING_CLAW_MCP_TOKEN} in learning-mcp.json an die CLI.
            LEARNING_CLAW_MCP_TOKEN = $McpToken
            # Ohne diese Datei sieht die Claude-CLI die Learning-Werkzeuge nicht.
            SHIM_EXTRA_MCP_CONFIG = (Join-Path $LearningRoot 'claw\learning-mcp.json')
            # Kurserzeugung und Veroeffentlichen warten laenger als der CLI-Vorgabewert.
            MCP_TOOL_TIMEOUT      = '900000'
            # Budget-Waechter: ohne VIBEMIND_AGENT ist er aus.
            VIBEMIND_AGENT        = 'learning-claw'
            VIBEMIND_BUDGET_MODUL = (Join-Path $Root 'scripts\claude_budget.py')
            # openclaws [[...]]-Direktiven loesen sonst ein irrefuehrendes "out of extra usage" aus.
            SHIM_NEUTRALIZE_DOUBLE_BRACKETS = '1'
        }
        Was  = 'Modell-Tuer :8141 (eigene Shim-Instanz, Werkzeuge learning, Budget learning-claw)'
    }
)

$Alle = $Dienste
if ($Liste) {
    # -InputObject statt Pipe: bleibt auch in Windows PowerShell 5.1 ein JSON-Array.
    ConvertTo-Json -Depth 3 -Compress -InputObject @($Dienste | ForEach-Object {
        [pscustomobject]@{ name = $_.Name; port = $_.Port; args = @($_.Args); was = $_.Was }
    })
    return
}
if ($Nur) {
    $namen = @($Nur | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    $unbekannt = @($namen | Where-Object { $_ -notin $Dienste.Name })
    if ($unbekannt) { throw "Unbekannte Dienste: $($unbekannt -join ', ')" }
    $Dienste = @($Dienste | Where-Object { $_.Name -in $namen })
}

if (-not (Test-Path $Venv)) { throw "Das gemeinsame venv fehlt: $Venv" }
if (-not (Test-Path $ShimSkript)) { throw "Shim-Skript fehlt: $ShimSkript (LEARNING_CLAW_SHIM_SCRIPT?)" }
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

function Test-Port([int]$Port) {
    $null -ne (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
               Select-Object -First 1)
}

# Ein frisch erzeugtes Token kennt kein bereits laufender Dienst: Sidecar und Shim muessen zusammen starten.
if ($TokenErzeugt -and -not $Pruefen) {
    $laeuft = @($Alle | Where-Object { Test-Port $_.Port })
    if ($laeuft.Count -gt 0 -and $laeuft.Count -lt $Alle.Count) {
        throw 'LEARNING_CLAW_MCP_TOKEN ist nicht gesetzt und ein Dienst laeuft schon: ohne bekanntes Token passen Sidecar und Shim nicht zusammen. Variable setzen oder beide beenden.'
    }
}

$gestartet = 0
$liefen = 0
foreach ($d in $Dienste) {
    if (Test-Port $d.Port) {
        Write-Output ("  laeuft   :{0,-5} {1}" -f $d.Port, $d.Was)
        $liefen++
        continue
    }
    if ($Pruefen) {
        Write-Output ("  FEHLT    :{0,-5} {1}" -f $d.Port, $d.Was)
        continue
    }

    # Alten Stand wegrollen, damit die neue Ausgabe fuer sich steht.
    foreach ($suf in '.log', '.err.log') {
        $f = Join-Path $LogDir "$($d.Name)$suf"
        if (Test-Path $f) {
            Move-Item $f (Join-Path $LogDir "$($d.Name)$($suf -replace '\.log$', '.prev.log')") -Force -ErrorAction SilentlyContinue
        }
    }

    $env:PYTHONUNBUFFERED = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    # Nur der gestartete Dienst soll diese Schluessel erben: alten Stand merken und
    # direkt nach Start-Process zurueckrollen.
    $vorher = @{}
    foreach ($k in $d.Env.Keys) {
        $vorher[$k] = [Environment]::GetEnvironmentVariable($k, 'Process')
        Set-Item -Path "env:$k" -Value $d.Env[$k]
    }

    try {
        $p = Start-Process -FilePath $Venv -ArgumentList $d.Args -WorkingDirectory $d.Cwd `
            -RedirectStandardOutput (Join-Path $LogDir "$($d.Name).log") `
            -RedirectStandardError  (Join-Path $LogDir "$($d.Name).err.log") `
            -WindowStyle Hidden -PassThru
    } finally {
        foreach ($k in $d.Env.Keys) {
            if ($null -eq $vorher[$k]) { Remove-Item -Path "env:$k" -ErrorAction SilentlyContinue }
            else { Set-Item -Path "env:$k" -Value $vorher[$k] }
        }
    }
    Write-Output ("  gestartet :{0,-5} PID {1,-6} {2}" -f $d.Port, $p.Id, $d.Was)
    $gestartet++
}

if ($gestartet -and -not $Pruefen) {
    # Ein Prozess, der sofort stirbt, ist kein gestarteter Dienst. Der Log sagt dann warum.
    Start-Sleep -Seconds 8
    foreach ($d in $Dienste) {
        if (-not (Test-Port $d.Port)) {
            Write-Warning ("  :{0} antwortet nicht - siehe {1}" -f $d.Port, (Join-Path $LogDir "$($d.Name).err.log"))
        }
    }
}
Write-Output ("  -> {0} liefen, {1} gestartet" -f $liefen, $gestartet)
