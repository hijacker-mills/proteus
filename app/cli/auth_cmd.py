"""`proteus auth …` — which credentials are in play, and do they actually work."""
from __future__ import annotations

import asyncio
import os

import typer

from ._common import die, emit, err, load_env, out, table

app = typer.Typer(no_args_is_help=True, help="Inspect and test model credentials.")

# Which environment variable each provider prefix reads. LiteLLM picks these up
# itself, so the CLI has to know the mapping to report on them.
PROVIDER_KEYS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "groq": "GROQ_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "xai": "XAI_API_KEY",
}


def _mask(value: str) -> str:
    """Enough to recognise a key, never enough to use one."""
    if not value:
        return ""
    return f"{value[:7]}…{value[-4:]}" if len(value) > 14 else "set"


def _collect() -> dict:
    load_env()
    from app import config

    model = config.MODEL
    provider = model.split("/", 1)[0] if "/" in model else model
    info: dict = {"model": model, "provider": provider, "ready": False}

    if provider in ("codex", "openai-codex"):
        from app import codex_auth

        status = codex_auth.status()
        info.update({
            "kind": "oauth",
            "source": status.get("source"),
            "own_chain": status.get("own_chain"),
            "ready": bool(status.get("ok")),
            "expires_at": status.get("expires_at"),
            "expires_in_hours": status.get("expires_in_hours"),
            "detail": status.get("detail"),
        })
    elif provider == "mock":
        info.update({"kind": "none", "source": "synthetic backend", "ready": True,
                     "detail": "mock/* needs no credentials and must never be served."})
    else:
        var = PROVIDER_KEYS.get(provider, f"{provider.upper()}_API_KEY")
        value = os.environ.get(var, "").strip()
        info.update({"kind": "api-key", "env_var": var, "key": _mask(value),
                     "ready": bool(value),
                     "detail": "" if value else f"{var} is not set, so every completion will fail."})

    # Other provider keys present, so a switch is one env var away.
    info["other_keys"] = sorted(v for v in PROVIDER_KEYS.values()
                                if os.environ.get(v, "").strip() and v != info.get("env_var"))
    return info


@app.command("info")
def info(as_json: bool = typer.Option(False, "--json")) -> None:
    """Show which credentials the configured model will use. Non-zero if unusable."""
    data = _collect()

    def render(d):
        mark = "green" if d["ready"] else "red"
        out.print(f"[bold]{d['model']}[/]  [{mark}]{'ready' if d['ready'] else 'NOT USABLE'}[/]")
        out.print(f"  provider  : {d['provider']}")
        out.print(f"  auth      : {d.get('kind')}")
        if d.get("kind") == "api-key":
            out.print(f"  env var   : {d['env_var']} = {d.get('key') or '[red]unset[/]'}")
        if d.get("kind") == "oauth":
            out.print(f"  source    : {d.get('source')}"
                      f"{'' if d.get('own_chain') else '  [yellow](borrowed — cannot be refreshed here)[/]'}")
            if d.get("expires_at"):
                hours = d.get("expires_in_hours") or 0
                colour = "red" if hours <= 0 else ("yellow" if hours < 48 else "green")
                out.print(f"  expires   : [{colour}]{d['expires_at']}"
                          f"  ({hours:.1f}h)[/]" if hours else f"  expires   : [{colour}]{d['expires_at']}[/]")
        if d.get("other_keys"):
            out.print(f"  also set  : {', '.join(d['other_keys'])}")
        if d.get("detail"):
            out.print(f"\n  [yellow]{d['detail']}[/]")
        if not d["ready"]:
            hint = ("proteus auth login" if d.get("kind") == "oauth"
                    else f"Set {d.get('env_var', 'the provider key')} in .env")
            out.print(f"  [dim]fix: {hint}[/]")

    emit(data, as_json, render)
    if not data["ready"]:
        raise typer.Exit(1)


@app.command("test")
def test(prompt: str = typer.Option("Reply with exactly: OK", "--prompt"),
         as_json: bool = typer.Option(False, "--json")) -> None:
    """Make one real completion to prove the credentials work.

    `info` reads configuration; this spends a few tokens actually calling the
    provider, which is the only way to tell a present key from a valid one.
    """
    load_env()
    from app import config, llm

    async def go() -> dict:
        text = []
        async for ev in llm.astream(model=config.MODEL,
                                    messages=[{"role": "user", "content": prompt}],
                                    tools=None, max_tokens=32, temperature=0):
            if ev["type"] == "text":
                text.append(ev["text"])
        return {"model": config.MODEL, "reply": "".join(text).strip()}

    if not as_json:
        err.print(f"[dim]calling {config.MODEL}…[/]")
    try:
        result = asyncio.run(go())
    except Exception as exc:
        message = str(exc)
        hint = ""
        low = message.lower()
        if "429" in message or "rate" in low or "usage limit" in low:
            hint = "The credentials are valid but the quota is exhausted."
        elif "401" in message or "403" in message or "auth" in low:
            hint = "Credentials rejected. Check proteus auth info."
        die(f"{type(exc).__name__}: {message[:200]}", hint)

    def render(d):
        out.print(f"[green]ok[/]  {d['model']}")
        out.print(f"  reply: {d['reply'][:120]!r}")

    emit(result, as_json, render)


@app.command("login")
def login() -> None:
    """ChatGPT (Codex) OAuth device login, for MODEL=codex/*."""
    from ._common import REPO

    script = REPO / "scripts" / "codex_login.sh"
    if not script.exists():
        die(f"{script} not found", "This command needs a source checkout.")
    import subprocess

    raise typer.Exit(subprocess.call(["bash", str(script)]))


@app.command("keygen")
def keygen(length: int = typer.Option(48, "--length", "-l", min=24, max=128)) -> None:
    """Generate a strong value for API_KEY or ADMIN_API_KEY.

    Printed bare so it can be piped or copied. A gateway's API_KEY is the only
    thing between the internet and your provider spend, so it should not be a
    word someone chose.
    """
    import secrets

    print(secrets.token_urlsafe(length)[:length])
