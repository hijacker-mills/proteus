"""
OpenAI Codex OAuth — credential load / refresh / device-code login.

proteus keeps its OWN token chain in CODEX_AUTH_FILE (default <project>/.codex-auth.json).
This is deliberate: OAuth refresh tokens are single-use/rotating, so sharing a
chain with Hermes (`~/.hermes/auth.json`) or the codex CLI (`~/.codex/auth.json`)
would have them invalidate each other. proteus logs in independently.

Mechanics mirror the codex CLI / Hermes:
  - access tokens are JWTs; refresh when the `exp` claim is within 120s.
  - refresh: POST {issuer}/oauth/token  grant_type=refresh_token (client_id constant).
  - account id: JWT claim `https://api.openai.com/auth`.chatgpt_account_id → ChatGPT-Account-ID header.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from . import config

_REFRESH_SKEW = 120
_lock = asyncio.Lock()


# ── JWT helpers ──────────────────────────────────────────────────────────────

def _jwt_claims(token: str) -> dict[str, Any]:
    try:
        if not isinstance(token, str) or token.count(".") != 2:
            return {}
        seg = token.split(".")[1]
        seg += "=" * ((4 - len(seg) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(seg.encode()))
    except Exception:
        return {}


def account_id(access_token: str, creds: dict | None = None) -> str:
    claims = _jwt_claims(access_token)
    acct = claims.get("https://api.openai.com/auth", {})
    if isinstance(acct, dict) and acct.get("chatgpt_account_id"):
        return acct["chatgpt_account_id"]
    if creds:
        return (creds.get("tokens", {}) or {}).get("account_id", "") or ""
    return ""


def _expiring(access_token: str) -> bool:
    exp = _jwt_claims(access_token).get("exp")
    if not isinstance(exp, (int, float)):
        return True  # can't read expiry → treat as needs-refresh
    return float(exp) <= time.time() + _REFRESH_SKEW


# ── Persistence ──────────────────────────────────────────────────────────────

def _read_json(path: str) -> dict | None:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _read_own() -> dict | None:
    d = _read_json(config.CODEX_AUTH_FILE)
    return d if d and (d.get("tokens", {}) or {}).get("access_token") else None


def _read_hermes() -> dict | None:
    d = _read_json(config.HERMES_AUTH_FILE)
    if not d:
        return None
    tokens = (d.get("providers", {}).get("openai-codex", {}) or {}).get("tokens")
    return {"tokens": tokens} if tokens and tokens.get("access_token") else None


def _read_codex_cli() -> dict | None:
    d = _read_json(config.CODEX_CLI_AUTH_FILE)
    tokens = (d or {}).get("tokens")
    return {"tokens": tokens} if tokens and tokens.get("access_token") else None


def resolve() -> tuple[dict | None, str, bool]:
    """Return (creds, source_name, writable). External sources are read-only."""
    src = config.CODEX_AUTH_SOURCE
    chain = {
        "own": [("own", _read_own, True)],
        "hermes": [("hermes", _read_hermes, False)],
        "codex": [("codex", _read_codex_cli, False)],
    }.get(src, [("own", _read_own, True), ("hermes", _read_hermes, False), ("codex", _read_codex_cli, False)])
    for name, reader, writable in chain:
        creds = reader()
        if creds:
            return creds, name, writable
    return None, "none", False


def load_creds() -> dict | None:
    return _read_own()


def save_creds(creds: dict) -> None:
    creds["last_refresh"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    path = config.CODEX_AUTH_FILE
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".codex-auth.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(creds, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# ── Refresh ──────────────────────────────────────────────────────────────────

async def _refresh(creds: dict) -> dict:
    refresh_token = (creds.get("tokens", {}) or {}).get("refresh_token", "")
    if not refresh_token:
        raise RuntimeError("no refresh_token — run: bash scripts/codex_login.sh")
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            config.CODEX_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": config.CODEX_CLIENT_ID,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if r.status_code != 200:
        raise RuntimeError(f"codex token refresh failed ({r.status_code}): {r.text[:200]}")
    payload = r.json()
    creds.setdefault("tokens", {})
    creds["tokens"]["access_token"] = payload["access_token"]
    if payload.get("refresh_token"):  # rotation
        creds["tokens"]["refresh_token"] = payload["refresh_token"]
    save_creds(creds)
    return creds


async def get_auth() -> tuple[str, str]:
    """Return (access_token, account_id).

    - Own file: refresh in place when expiring.
    - External source (Hermes / codex CLI): READ-ONLY. We never refresh a shared
      chain (single-use refresh tokens would break the owner). If the external
      token is expiring we re-read once (the owner may have just refreshed it);
      if still expired we raise rather than hijack the chain.
    """
    creds, source, writable = resolve()
    if not creds:
        raise RuntimeError(
            "No Codex credentials found. Set CODEX_AUTH_SOURCE / log into Hermes or "
            "the codex CLI, or run: bash scripts/codex_login.sh"
        )
    access = creds["tokens"]["access_token"]

    if _expiring(access):
        if writable:
            async with _lock:
                creds = _read_own() or creds  # another worker may have refreshed
                access = creds["tokens"]["access_token"]
                if _expiring(access):
                    creds = await _refresh(creds)
                    access = creds["tokens"]["access_token"]
        else:
            creds, source, writable = resolve()  # owner may have refreshed the file
            access = (creds or {}).get("tokens", {}).get("access_token", "")
            if not access or _expiring(access):
                raise RuntimeError(
                    f"Codex token from '{source}' is expired and proteus won't refresh a "
                    f"shared chain (would break {source}). Use Hermes to refresh it, or "
                    "run scripts/codex_login.sh for proteus's own chain."
                )
    return access, account_id(access, creds)


def status() -> dict[str, Any]:
    """Non-throwing snapshot of the credential chain, for /healthz.

    Model auth is the one dependency that fails on a CLOCK rather than on load,
    so it has to be observable before it bites: a read-only chain (Hermes / the
    codex CLI) cannot be refreshed by proteus, and when its access token lapses
    every chat 500s while the DB check still says everything is fine.
    """
    creds, source, writable = resolve()
    if not creds:
        return {"source": "none", "ok": False,
                "detail": "no codex credentials — run: bash scripts/codex_login.sh"}

    access = (creds.get("tokens") or {}).get("access_token", "")
    out: dict[str, Any] = {"source": source, "own_chain": writable, "ok": True}
    exp = _jwt_claims(access).get("exp")
    if not isinstance(exp, (int, float)):
        out["ok"] = False
        out["detail"] = "cannot read token expiry"
        return out

    remaining = float(exp) - time.time()
    out["expires_at"] = datetime.fromtimestamp(float(exp), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out["expires_in_hours"] = round(remaining / 3600, 1)
    out["ok"] = remaining > 0
    if not out["ok"]:
        out["detail"] = f"token from '{source}' has EXPIRED"
    elif not writable and remaining < 48 * 3600:
        out["detail"] = (f"token from '{source}' expires in {out['expires_in_hours']}h and proteus "
                         "cannot refresh a borrowed chain — run: bash scripts/codex_login.sh")
    return out


# ── Device-code login (sync; used by app.codex_login) ────────────────────────

def device_code_login() -> dict:
    """Run the OpenAI device-code flow. Prints a URL + code; returns creds dict."""
    issuer = config.CODEX_ISSUER
    cid = config.CODEX_CLIENT_ID
    with httpx.Client(timeout=httpx.Timeout(20.0)) as client:
        resp = client.post(
            f"{issuer}/api/accounts/deviceauth/usercode",
            json={"client_id": cid},
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"device code request failed ({resp.status_code}): {resp.text[:200]}")
        data = resp.json()
        user_code = data.get("user_code", "")
        device_auth_id = data.get("device_auth_id", "")
        interval = max(3, int(data.get("interval", 5)))
        if not user_code or not device_auth_id:
            raise RuntimeError("device code response missing fields")

        print("\nTo authorize proteus with your ChatGPT account:\n")
        print(f"  1. Open:  {issuer}/codex/device")
        print(f"  2. Enter code:  {user_code}\n")
        print("Waiting for sign-in (Ctrl+C to cancel)…")

        deadline = time.monotonic() + 15 * 60
        code_resp = None
        while time.monotonic() < deadline:
            time.sleep(interval)
            poll = client.post(
                f"{issuer}/api/accounts/deviceauth/token",
                json={"device_auth_id": device_auth_id, "user_code": user_code},
                headers={"Content-Type": "application/json"},
            )
            if poll.status_code == 200:
                code_resp = poll.json()
                break
            if poll.status_code in (403, 404):
                continue
            raise RuntimeError(f"device auth poll failed ({poll.status_code})")
        if not code_resp:
            raise RuntimeError("login timed out after 15 minutes")

        authorization_code = code_resp.get("authorization_code", "")
        code_verifier = code_resp.get("code_verifier", "")
        if not authorization_code or not code_verifier:
            raise RuntimeError("device auth response missing authorization_code/code_verifier")

        tok = client.post(
            config.CODEX_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": authorization_code,
                "redirect_uri": f"{issuer}/deviceauth/callback",
                "client_id": cid,
                "code_verifier": code_verifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if tok.status_code != 200:
            raise RuntimeError(f"token exchange failed ({tok.status_code}): {tok.text[:200]}")
        tokens = tok.json()
        if not tokens.get("access_token"):
            raise RuntimeError("token exchange returned no access_token")

    return {
        "auth_mode": "chatgpt",
        "tokens": {
            "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token", ""),
        },
        "source": "device-code",
    }
