"""Runs the shipped page scripts in Node against a stub DOM (tests/web/pages_smoke.js).

It cannot judge layout, but it catches a page script that reads an element its
template does not have, throws while rendering, or builds the wrong request for
the bulk, timeline, API-key, session, reset-link and registration flows.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

SMOKE = Path(__file__).resolve().parents[1] / "web" / "pages_smoke.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
def test_page_scripts_render_and_send_the_right_requests():
    result = subprocess.run(["node", str(SMOKE)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
