"""Ready-made terminal checks for setup courses, from Anthropic's official install guide
(https://code.claude.com/docs/en/setup and /troubleshoot-install).

The output messages are recognised in English and, on Windows, in German; the fixes are the
documented commands for exactly that shell.
"""

from __future__ import annotations

from spaces.learning.contracts.lernsequenz import CommandCheck, OutputRule, ShellCommand

_PATH_HINT = (
    "Der Installationsordner steht noch nicht im Suchpfad (PATH). Erst ein neues Terminalfenster "
    "öffnen und es erneut versuchen; ein schon offenes Fenster kennt das neue Programm nicht. Hilft "
    "das nicht, mit diesen Befehlen prüfen und den Ordner eintragen, danach das Terminal neu starten."
)


def claude_installiert(checkpoint_id: str, after_section: str, *, refs: tuple[str, ...] = ()) -> CommandCheck:
    """`claude --version`: version printed = installed; known errors get their fix."""
    return CommandCheck(
        checkpoint_id=checkpoint_id,
        after_section=after_section,
        title="Ist Claude Code installiert?",
        refs=refs,
        task="Prüfen Sie, ob Claude Code richtig installiert ist. Öffnen Sie dafür ein neues Terminalfenster.",
        commands=(
            ShellCommand(shell="powershell", command="claude --version"),
            ShellCommand(shell="cmd", command="claude --version"),
            ShellCommand(shell="unix", command="claude --version"),
        ),
        success=OutputRule(
            pattern=r"\d+\.\d+\.\d+\s*\(Claude Code\)",
            message="Claude Code ist installiert. Als Nächstes im Projektordner „claude“ eingeben und sich im Browser anmelden.",
        ),
        problems=(
            OutputRule(
                pattern=r"The term 'claude' is not recognized|Benennung .{0,3}claude.{0,3} wurde nicht",
                shells=("powershell",),
                message=_PATH_HINT,
                fix=(
                    ShellCommand(shell="powershell", command='Test-Path "$env:USERPROFILE\\.local\\bin\\claude.exe"'),
                    ShellCommand(shell="powershell", command="$env:PATH -split ';' | Select-String '\\.local\\\\bin'"),
                    ShellCommand(
                        shell="powershell",
                        command="$currentPath = [Environment]::GetEnvironmentVariable('PATH', 'User')\n"
                        "[Environment]::SetEnvironmentVariable('PATH', \"$currentPath;$env:USERPROFILE\\.local\\bin\", 'User')",
                    ),
                ),
            ),
            OutputRule(
                pattern=r"'claude' is not recognized as an internal or external command|claude.{0,3} ist entweder falsch geschrieben",
                shells=("cmd",),
                message=_PATH_HINT + " In CMD trägt man den Ordner „%USERPROFILE%\\.local\\bin“ in den "
                "Systemeinstellungen unter „Umgebungsvariablen“ in die Benutzervariable PATH ein.",
                fix=(
                    ShellCommand(shell="cmd", command='dir "%USERPROFILE%\\.local\\bin\\claude.exe"'),
                    ShellCommand(shell="cmd", command='echo %PATH% | findstr /i "local\\bin"'),
                ),
            ),
            OutputRule(
                pattern=r"command not found: claude|claude: command not found|claude: Befehl nicht gefunden",
                shells=("unix",),
                message=_PATH_HINT + " Für Zsh (Standard auf macOS) die ersten beiden Befehle, für Bash "
                "unter Linux die letzten beiden.",
                fix=(
                    ShellCommand(shell="unix", command="ls -la ~/.local/bin/claude"),
                    ShellCommand(shell="unix", command="echo 'export PATH=\"$HOME/.local/bin:$PATH\"' >> ~/.zshrc\nsource ~/.zshrc"),
                    ShellCommand(shell="unix", command="echo 'export PATH=\"$HOME/.local/bin:$PATH\"' >> ~/.bashrc\nsource ~/.bashrc"),
                ),
            ),
            OutputRule(
                pattern=r"not recognized|nicht erkannt|command not found|nicht gefunden",
                message="Das Terminal kennt den Befehl nicht. Prüfen Sie, ob oben die richtige Shell gewählt "
                "ist; dann zeigt die Prüfung die passende Lösung.",
            ),
        ),
        unknown_hint="Diese Ausgabe kenne ich nicht. Eine funktionierende Installation zeigt eine "
        "Versionsnummer wie „2.1.89 (Claude Code)“. Kopieren Sie bitte die ganze Ausgabe hierher, "
        "oder führen Sie „claude doctor“ aus: das zeigt Hinweise zur Behebung.",
    )
