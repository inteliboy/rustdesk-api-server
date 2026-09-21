"""The WebUI's language catalogs (frontend/i18n -> static/i18n)."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "src" / "rustdesk_api" / "web"


def _builder():
    spec = importlib.util.spec_from_file_location("build_i18n", ROOT / "scripts" / "build_i18n.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_i18n"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def builder():
    return _builder()


@pytest.fixture(scope="module")
def entries(builder):
    return builder.load()


def test_the_catalog_is_well_formed(entries):
    # load() rejects a line with the wrong number of fields, an empty translation, a placeholder the
    # English does not have, and a key defined twice with different translations.
    assert len(entries) > 500


def test_the_committed_language_files_are_up_to_date(builder, entries):
    for lang in builder.LANGUAGES:
        committed = (builder.OUTPUT / f"{lang}.js").read_text(encoding="utf-8")
        # A Windows checkout may have converted the line endings.
        assert committed.replace("\r\n", "\n") == builder.render(entries, lang), (
            f"static/i18n/{lang}.js is out of date: run python scripts/build_i18n.py"
        )


def test_every_translation_differs_in_kind_from_a_stub(entries, builder):
    """No language is a copy of the English text where the English is a sentence (a forgotten
    translation). Short words that are the same in a language (Windows, Linux, IP) are fine."""
    for key, translations in entries.items():
        if key.startswith("@") or len(key) < 25 or "{" in key and len(key) < 30:
            continue
        for lang, text in translations.items():
            assert text != key, f"{lang} translation of {key[:40]!r} is the English text"


def test_html_blocks_match_the_templates(entries):
    """Every data-i18n-html id in a template has a translation, and every translated block is used."""
    used: set[str] = set()
    pattern = re.compile(r'data-i18n-html="([^"]+)"')
    for path in list((WEB / "templates").glob("*.html")) + list((WEB / "static" / "js").rglob("*.js")):
        if path.name != "i18n.js":  # its comment shows the attribute as an example
            used.update(pattern.findall(path.read_text(encoding="utf-8")))
    defined = {key[1:] for key in entries if key.startswith("@")}
    assert used == defined


def test_html_block_translations_keep_the_markup_the_english_has(entries):
    """A translation may not add tags or attributes of its own (it is inserted as HTML)."""
    allowed = {"code", "em", "strong", "span", "a"}
    for key, translations in entries.items():
        if not key.startswith("@"):
            continue
        for lang, text in translations.items():
            tags = set(re.findall(r"</?([a-z]+)", text))
            assert tags <= allowed, f"{lang} {key}: unexpected tags {tags - allowed}"
            assert "<script" not in text.lower() and "javascript:" not in text.lower()
            for attribute in re.findall(r"\s([a-z-]+)=", text):
                assert attribute in {"class", "href"}, f"{lang} {key}: unexpected attribute {attribute}"
            # Opening and closing tags balance.
            for tag in tags:
                assert len(re.findall(rf"<{tag}\b", text)) == len(re.findall(rf"</{tag}>", text)), (
                    lang,
                    key,
                    tag,
                )


def test_t_calls_in_the_scripts_name_catalog_entries(entries):
    """A t("...") in a script must name an entry, or it silently stays English."""
    known = {key.lstrip("!") for key in entries}
    call = re.compile(r"""\bt\(\s*(["'])((?:(?!\1).)*)\1""")
    missing = []
    for path in sorted((WEB / "static" / "js").rglob("*.js")):
        if path.name == "i18n.js":
            continue
        for _quote, text in call.findall(path.read_text(encoding="utf-8")):
            if re.sub(r"\s+", " ", text).strip() not in known:
                missing.append(f"{path.name}: {text[:60]}")
    assert not missing, missing
