"""Zugangslink erzeugen und per Telegram schicken. Der Link wird nie ausgegeben."""

from __future__ import annotations

import sys


def main() -> int:
    from .einstellung import einstellung_lesen
    from .zugang import token_erzeugen

    # Lazy: claw.werkzeuge zieht das mcp-Paket; die Huelle braucht es sonst nicht.
    from ..claw.werkzeuge import _telegram_senden

    e = einstellung_lesen()
    link = f"{e.basis}/lernen/zugang/{token_erzeugen(e.dienst_schluessel)}"
    ok = _telegram_senden(f"Ihr Zugang zum Lernen:\n{link}")
    print("gesendet" if ok else "nicht gesendet")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
