"""
One-time Codex login for acag (independent OAuth token chain).

    python -m app.codex_login     (or: bash scripts/codex_login.sh)

Runs the OpenAI device-code flow, prints a URL + code to authorize with your
ChatGPT account, and stores acag's own tokens at CODEX_AUTH_FILE.
"""
from __future__ import annotations

from . import codex_auth, config


def main() -> None:
    print(f"acag Codex login → {config.CODEX_AUTH_FILE}")
    creds = codex_auth.device_code_login()
    codex_auth.save_creds(creds)
    access = creds["tokens"]["access_token"]
    acct = codex_auth.account_id(access, creds)
    print("\n✓ Logged in. Stored acag's own token chain.")
    print(f"  account_id: {acct or '(none)'}")
    print(f"  set MODEL=codex/gpt-5.5 (or another codex model) to use it.")


if __name__ == "__main__":
    main()
