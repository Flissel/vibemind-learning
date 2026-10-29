"""KaTeX for the local claw pages: formulas in ``$...$`` and ``$$...$$`` are typeset.

Contents stay HTML-escaped in the body; the browser only typesets the text nodes. The
pages' Content-Security-Policy allows exactly the pinned KaTeX path on ``cdn.jsdelivr.net``
for script, style and font, plus the one inline start script by its hash (no ``'unsafe-inline'`` for scripts).
"""

from __future__ import annotations

import base64
import hashlib

# The CSP names the pinned KaTeX path, not all of jsDelivr (fonts load from dist/fonts/).
CDN = "https://cdn.jsdelivr.net/npm/katex@0.16.11/"
_BASIS = f"{CDN}dist/"
_CSS_SRI = "sha384-nB0miv6/jRmo5UMMR1wu3Gz6NLsoTkbqJghGIsx//Rlm+ZU03BU6SQNC66uf4l5+"
_JS_SRI = "sha384-7zkQWkzuo3B5mTepMUcHkMB5jZaolc2xDwL6VFqjFALcbeS9Ggm/Yr2r3Dy4lfFg"
_AUTO_SRI = "sha384-43gviWU0YVjaDtb/GhzOouOXtZMP/7XUzwPTstBeZFe/+rCMvRwr4yROQP43s0Xk"

_START = (
    "document.addEventListener('DOMContentLoaded',function(){"
    "if(window.renderMathInElement){renderMathInElement(document.body,{delimiters:["
    "{left:'$$',right:'$$',display:true},{left:'$',right:'$',display:false}],"
    "throwOnError:false});}});"
)
START_HASH = "'sha256-" + base64.b64encode(hashlib.sha256(_START.encode()).digest()).decode() + "'"

KOPF = (
    f'<link rel="stylesheet" href="{_BASIS}katex.min.css" integrity="{_CSS_SRI}" crossorigin="anonymous">'
    f'<script defer src="{_BASIS}katex.min.js" integrity="{_JS_SRI}" crossorigin="anonymous"></script>'
    f'<script defer src="{_BASIS}contrib/auto-render.min.js" integrity="{_AUTO_SRI}" '
    'crossorigin="anonymous"></script>'
    f"<script>{_START}</script>"
)


def csp(*, bilder: bool = False) -> bytes:
    """The Content-Security-Policy of the claw pages, KaTeX source included."""
    teile = [
        "default-src 'none'",
        f"script-src {CDN} {START_HASH}",
        f"style-src 'unsafe-inline' {CDN}",
        f"font-src {CDN}",
    ]
    if bilder:
        teile.append("img-src data:")
    teile += ["form-action 'self'", "frame-ancestors 'none'"]
    return "; ".join(teile).encode()
