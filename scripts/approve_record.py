#!/usr/bin/env python3
"""approve_record.py — OUT-OF-BAND owner approval-token minter (sprint S3).

Mirrors ``merge_live.py``'s ``ZOT_MERGE_LIVE_ENABLED`` doctrine, one level richer: run this yourself,
in a SEPARATE terminal, to mint an HMAC approval token for one specific candidate record. The agent
may DISPLAY the command line below to you, but must never run it on your behalf and must never see
``ZOT_APPROVAL_HMAC_KEY`` — the secret is read only from YOUR shell's environment, is never a tool
parameter, is never logged, and this script never prints it.

One-time setup (in your own terminal — NEVER paste this into a chat with the agent):
    python -c "import secrets; print(secrets.token_hex(32))"
    export ZOT_APPROVAL_HMAC_KEY=<paste the printed value>        # bash/zsh
    $env:ZOT_APPROVAL_HMAC_KEY = "<paste the printed value>"       # PowerShell

Usage (from the engine repo root):
    uv run python scripts/approve_record.py --item-type journalArticle \
        --title "Basin of Mexico Settlement Patterns" --creator Sanders --year 1979 \
        --doi 10.1234/abc

Prints the canonical identity (to stderr, for you to double-check the record) and the token (to
stdout, alone — so it can be captured with `$(...)` / piped without echoing anything else). The token
is valid ONLY for this exact normalized identity (itemType, title, year, firstAuthor, DOI) — a
different record needs a freshly-minted token.
"""
import argparse
import os
import sys

# Defensive: works whether invoked via `uv run` (package already resolvable) or a bare `python`
# pointed at this file directly — either way, prefer the local dev tree over any installed copy so
# the minter always matches the code actually being tested/reviewed in this checkout.
_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from zotero_write_mcp.validation import (  # noqa: E402
    APPROVAL_HMAC_KEY_ENV,
    canonical_identity_string,
    compute_approval_token,
)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Mint an out-of-band HMAC approval token for one candidate record.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--item-type", required=True, help="Zotero itemType, e.g. journalArticle")
    ap.add_argument("--title", required=True)
    ap.add_argument("--creator", required=True, help="First author's last name")
    ap.add_argument("--year", required=True, help="Publication year (or full date string)")
    ap.add_argument("--doi", default="", help="DOI, if known (omit for a DOI-less record)")
    args = ap.parse_args()

    key = os.environ.get(APPROVAL_HMAC_KEY_ENV)
    if not key:
        sys.stderr.write(
            f"ERROR: {APPROVAL_HMAC_KEY_ENV} is not set in this shell.\n"
            f"Generate a random secret ONCE and export it in YOUR OWN terminal — never share it with "
            f"the agent, never commit it, never pass it as a tool argument:\n\n"
            f"    python -c \"import secrets; print(secrets.token_hex(32))\"\n"
            f"    export {APPROVAL_HMAC_KEY_ENV}=<paste the printed value>\n\n"
            f"The same value must also be visible to the zotero-write MCP server process (it is read "
            f"from the environment at hook-check time, never persisted) — re-launch Claude Code from "
            f"a terminal where this variable is exported.\n"
        )
        return 1

    record = {
        "itemType": args.item_type,
        "title": args.title,
        "creators": [{"creatorType": "author", "lastName": args.creator}],
        "date": args.year,
        "DOI": args.doi,
    }
    token = compute_approval_token(record, key)
    sys.stderr.write(f"Canonical identity: {canonical_identity_string(record)!r}\n")
    sys.stderr.write("Give this token to the agent (it authorizes ONLY the exact record above):\n")
    print(token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
