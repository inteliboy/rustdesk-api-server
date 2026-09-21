"""The menu (top or left, with icons): its saved settings, its icons and the links it builds
(tests/web/menu_check.js). Like the other Node checks it is skipped where Node is not installed."""

import shutil
import subprocess
from pathlib import Path

import pytest

CHECK = Path(__file__).resolve().parents[1] / "web" / "menu_check.js"
JS_DIR = Path(__file__).resolve().parents[2] / "src" / "rustdesk_api" / "web" / "static" / "js"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
def test_the_menu_settings_icons_and_links():
    result = subprocess.run(["node", str(CHECK), str(JS_DIR)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
