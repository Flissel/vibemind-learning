# Richtet die WSL2-Distro `vibemind-code` fuer den Code-Laeufer ein oder zieht sie nach. Braucht keine Adminrechte.
# Beruehrt weder `.wslconfig` noch andere Distros; es gibt kein `wsl --shutdown`.
#
#   pwsh -File einrichten.ps1            # anlegen (falls noetig), Pakete und lauf.py/aufseher.py einspielen
#   pwsh -File einrichten.ps1 -Neu       # Distro verwerfen und frisch aufbauen
param([switch]$Neu)

$ErrorActionPreference = 'Stop'
$Distro = 'vibemind-code'
$Ordner = Join-Path $env:LOCALAPPDATA 'VibeMind\code-laeufer'
$Basis = 'https://dl-cdn.alpinelinux.org/alpine/latest-stable/releases/x86_64'

function Wsl-Text { param([string[]]$Argumente) (& wsl.exe @Argumente 2>&1 | ForEach-Object { ("$_" -replace "`0", '').Trim() }) | Where-Object { $_ } }
function In-Distro { param([string]$Skript, [string]$Benutzer = 'root')
    ($Skript.Replace("`r`n", "`n") + "`n") | & wsl.exe -d $Distro -u $Benutzer --cd / -- sh -c "tr -d '\r' | sh"
    if ($LASTEXITCODE -ne 0) { throw "Schritt in der Distro fehlgeschlagen ($LASTEXITCODE)" }
}

$vorhanden = (Wsl-Text @('-l', '-q')) -contains $Distro
if ($Neu -and $vorhanden) { & wsl.exe --unregister $Distro | Out-Null; $vorhanden = $false }

if (-not $vorhanden) {
    New-Item -ItemType Directory -Force $Ordner | Out-Null
    $antwort = Invoke-WebRequest "$Basis/latest-releases.yaml" -UseBasicParsing -TimeoutSec 30
    $yaml = if ($antwort.Content -is [byte[]]) { [Text.Encoding]::UTF8.GetString($antwort.Content) } else { [string]$antwort.Content }
    $teil = $yaml.Substring($yaml.IndexOf('flavor: alpine-minirootfs'))
    $datei = [regex]::Match($teil, 'file:\s*(\S+)').Groups[1].Value
    $soll = [regex]::Match($teil, 'sha256:\s*([0-9a-f]{64})').Groups[1].Value
    if (-not $datei -or -not $soll) { throw 'Alpine-Verzeichnis nicht lesbar' }
    $ziel = Join-Path $Ordner $datei
    Invoke-WebRequest "$Basis/$datei" -OutFile $ziel -UseBasicParsing -TimeoutSec 180
    if ((Get-FileHash $ziel -Algorithm SHA256).Hash.ToLower() -ne $soll) { throw 'Pruefsumme des Grundsystems stimmt nicht' }
    & wsl.exe --import $Distro (Join-Path $Ordner 'distro') $ziel --version 2 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'wsl --import fehlgeschlagen' }
}

# Pakete, Benutzer, Abschottung der Distro selbst: keine Windows-Laufwerke, kein Start von Windows-Programmen.
In-Distro @'
set -e
apk update -q
apk add -q python3 nodejs bubblewrap
# Nur mit der Erlaubnis `pakete` sichtbar: ohne sie verdeckt lauf.py den Paketordner von Python.
apk add -q py3-numpy py3-matplotlib
# Eigene Benutzer-ID: alle WSL-Distros teilen einen Kernel, die Prozessgrenze (RLIMIT_NPROC) zaehlt je ID ueber
# alle Distros. Mit der ueblichen 1000 zaehlten die Prozesse der Ubuntu-Distro mit (bwrap: EAGAIN).
if [ "$(id -u laeufer 2>/dev/null)" != "61234" ]; then
  deluser laeufer 2>/dev/null || true
  adduser -D -u 61234 -h /home/laeufer -s /bin/sh laeufer
fi
cat > /etc/wsl.conf <<EOF
[automount]
enabled = false
mountFsTab = false
[interop]
enabled = false
appendWindowsPath = false
[user]
default = laeufer
[network]
generateHosts = false
EOF
mkdir -p /usr/local/lib/vibemind
'@

# lauf.py und aufseher.py einspielen (gehoeren root, fuer `laeufer` nur lesbar).
foreach ($name in 'lauf.py', 'aufseher.py') {
    $quelle = Join-Path $PSScriptRoot $name
    (Get-Content $quelle -Raw) | & wsl.exe -d $Distro -u root --cd / -- sh -c "tr -d '\r' > /usr/local/lib/vibemind/$name && chmod 644 /usr/local/lib/vibemind/$name && chown root:root /usr/local/lib/vibemind/$name"
    if ($LASTEXITCODE -ne 0) { throw "$name nicht eingespielt" }
}

& wsl.exe --terminate $Distro | Out-Null   # wsl.conf gilt erst nach dem Neustart dieser einen Distro

# Probe: Abschottung und ein echter Lauf.
$probe = '{"sprache":"python","quelltext":"import os,socket\ntry:\n    socket.create_connection((\"127.0.0.1\",445),1); print(\"NETZ\")\nexcept OSError: print(\"kein netz\")\nprint(os.path.exists(\"/mnt/c\"), sorted(os.listdir(\"/\")))","eingabe":"","zeit_s":5}'
$ergebnis = $probe | & wsl.exe -d $Distro -u laeufer --cd / -- python3 /usr/local/lib/vibemind/lauf.py | ConvertFrom-Json
if ($ergebnis.exit -ne 0 -or $ergebnis.stdout -notmatch '^kein netz\s+False ') { throw "Probe fehlgeschlagen: $($ergebnis | ConvertTo-Json -Compress)" }
$mounts = (& wsl.exe -d $Distro -u laeufer --cd / -- sh -c "grep -c drvfs /proc/mounts; ls /mnt/c 2>/dev/null | wc -l") -join ' '
if ($mounts -ne '0 0') { throw "Windows-Laufwerke sichtbar: $mounts" }
# Probe der Erlaubnisse: ohne `pakete` kein numpy, mit `pakete` und `diagramm` ein Bild.
function Lauf-Probe { param([hashtable]$Auftrag)
    ($Auftrag | ConvertTo-Json -Compress -Depth 5) | & wsl.exe -d $Distro -u laeufer --cd / -- python3 /usr/local/lib/vibemind/lauf.py | ConvertFrom-Json
}
$ohne = Lauf-Probe @{ sprache = 'python'; quelltext = 'import numpy'; eingabe = ''; zeit_s = 5 }
if ($ohne.exit -eq 0 -or $ohne.stderr -notmatch 'ModuleNotFoundError') { throw 'Pakete sind ohne Erlaubnis sichtbar' }
$bild = Lauf-Probe @{ sprache = 'python'; eingabe = ''; zeit_s = 5; erlaubnisse = @('pakete', 'diagramm')
    quelltext = "import matplotlib.pyplot as plt`nplt.plot([1, 2, 3])`nplt.savefig('ausgabe/probe.png', dpi=50)`nprint('ok')" }
if ($bild.exit -ne 0 -or @($bild.bilder).Count -ne 1) { throw "Diagramm-Probe fehlgeschlagen: $($bild.stderr)" }
"Code-Laeufer bereit: $Distro (" + ((& wsl.exe -d $Distro -u laeufer --cd / -- sh -c 'python3 --version; node --version') -join ', ') + ')'
