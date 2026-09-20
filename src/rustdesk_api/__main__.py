"""Allows `python -m rustdesk_api` as a shortcut for `rustdesk-api serve`."""

from __future__ import annotations

import sys

from rustdesk_api.cli import main


def _run() -> None:
    # `python -m rustdesk_api` with no subcommand starts the server directly,
    # matching the quick-start documented in CLAUDE.md section 55. Any other
    # invocation (`python -m rustdesk_api migrate`, etc.) defers to the full
    # click CLI unchanged.
    if len(sys.argv) == 1:
        sys.argv.append("serve")
    main()


if __name__ == "__main__":
    _run()
