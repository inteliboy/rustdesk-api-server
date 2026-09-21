"""QR codes for the WebUI, drawn on the server so the page needs no script for it.

Always dark modules on a white background with a quiet zone: scanners expect that,
also when the page itself is in the dark theme.
"""

from __future__ import annotations

import segno


def svg_data_uri(text: str) -> str:
    """`text` as an SVG image in a `data:` URI, ready for `<img src>`."""
    code = segno.make(text, error="m")
    return code.svg_data_uri(scale=5, border=3, dark="#000000", light="#ffffff")
