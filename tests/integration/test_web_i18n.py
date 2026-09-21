"""Runs the real translator script (static/js/i18n.js) in Node against the real catalogs
(tests/web/i18n_check.js). Like the other Node checks it is skipped where Node is not installed."""

import shutil
import subprocess
from pathlib import Path

import pytest

CHECK = Path(__file__).resolve().parents[1] / "web" / "i18n_check.js"
STATIC = Path(__file__).resolve().parents[2] / "src" / "rustdesk_api" / "web" / "static"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
def test_the_translator_translates_text_attributes_patterns_and_blocks():
    result = subprocess.run(["node", str(CHECK), str(STATIC)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
