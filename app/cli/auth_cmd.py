"""`proteus auth …` — which credentials are in play, and do they actually work."""
from __future__ import annotations

import asyncio
import os
import sys

import typer

from ._common import die, emit, err, load_env, out, table

app = typer.Typer(no_args_is_help=True, help="Inspect and test model credentials.")

# Which environment variable each provider prefix reads. LiteLLM picks these up
# itself, so the CLI has to know the mapping to report on them.
# A cheap model per provider, used only to prove a key works. Without this a
# `--provider X` login would validate against whatever MODEL happens to be,
# which silently accepts a bad key for X.
PROBE_MODELS = {
    "anthropic": "anthropic/claude-haiku-4-5-20251001",
    "openai": "openai/gpt-4o-mini",
    "openrouter": "openrouter/openai/gpt-4o-mini",
    "groq": "groq/llama-3.1-8b-instant",
    "mistral": "mistral/mistral-small-latest",
    "gemini": "gemini/gemini-1.5-flash",
    "deepseek": "deepseek/deepseek-chat",
    "xai": "xai/grok-3-mini",                 # cheapest current Grok
    "moonshot": "moonshot/kimi-latest-8k",    # Kimi, smallest context = cheapest probe
    "kimi-code": "kimi-code/kimi-for-coding",  # Moonshot's coding endpoint
}

# What people call a provider is not always what LiteLLM calls it. Accepting
# both means `proteus auth login -p grok` works without anyone having to learn
# that the prefix is `xai`.
ALIASES = {
    "kimicode": "kimi-code",
    "kimi_code": "kimi-code",
    "coding": "kimi-code",
    "grok": "xai",
    "x": "xai",
    "kimi": "moonshot",
    "moonshotai": "moonshot",
    "claude": "anthropic",
    "gpt": "openai",
    "google": "gemini",
}

PROVIDER_KEYS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "groq": "GROQ_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "xai": "XAI_API_KEY",
    "moonshot": "MOONSHOT_API_KEY",
    "kimi-code": "KIMI_CODE_API_KEY",
}


# Providers with more than one regional endpoint. LiteLLM reads <PROVIDER>_API_BASE
# from the environment, so this is only here to tell people it exists — a key
# from the wrong region authenticates nowhere and gives no hint why.
REGIONAL = {
    "moonshot": "MOONSHOT_API_BASE  (https://api.moonshot.ai/v1 default, "
                "https://api.moonshot.cn/v1 for the China platform)",
}


def canonical(name: str) -> str:
    """Resolve an alias to the prefix LiteLLM actually uses."""
    key = (name or "").strip().lower()
    return ALIASES.get(key, key)


def _mask(value: str) -> str:
    """Enough to recognise a key, never enough to use one."""
    if not value:
        return ""
    return f"{value[:7]}…{value[-4:]}" if len(value) > 14 else "set"


def _collect() -> dict:
    load_env()
    from app import config

    model = config.MODEL
    provider = canonical(model.split("/", 1)[0] if "/" in model else model)
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
        if provider == "kimi-code":
            from app import config as _c
            info["api_base"] = _c.KIMI_CODE_API_BASE

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
        if d.get("api_base"):
            out.print(f"  endpoint  : {d['api_base']}")
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


def _write_env(var: str, value: str) -> None:
    """Set one variable in .env, leaving every other line exactly as it was.

    Rewriting the file from parsed config would drop comments and reorder
    things, and this is a file people hand-edit.
    """
    from ._common import REPO

    path = REPO / ".env"
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.exists() else []
    replaced = False
    for i, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == var:
            lines[i] = f"{var}={value}\n"
            replaced = True
            break
    if not replaced:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append(f"{var}={value}\n")

    # Written 0600: it now holds a credential.
    import os as _os

    path.write_text("".join(lines), encoding="utf-8")
    try:
        _os.chmod(path, 0o600)
    except OSError:
        pass


@app.command("login")
def login(provider: str = typer.Option(None, "--provider", "-p",
                                       help="Provider to authenticate. Default: whatever MODEL uses."),
          key: str = typer.Option(None, "--key",
                                  help="Supply the key non-interactively (avoid: it lands in shell history).")) -> None:
    """Authenticate a model provider, whichever one you use.

    OAuth providers (currently `codex`, i.e. a ChatGPT subscription) run a
    device flow. Everything else is an API key: this prompts for it without
    echoing, makes one real call to check it actually works, and only then
    writes it to .env.
    """
    load_env()
    from app import config

    target = canonical(provider or (config.MODEL.split("/", 1)[0]
                                    if "/" in config.MODEL else config.MODEL))

    if target in ("codex", "openai-codex"):
        _login_oauth()
        return
    if target == "mock":
        die("mock/* is a synthetic backend and needs no credentials",
            "Point MODEL at a real provider first.")

    var = PROVIDER_KEYS.get(target)
    if var is None:
        var = f"{target.upper()}_API_KEY"
        err.print(f"[yellow]note:[/] {target!r} is not one I know; assuming it reads {var}.")

    value = key
    if not value:
        existing = os.environ.get(var, "").strip()
        if existing:
            err.print(f"[dim]{var} is already set ({_mask(existing)}).[/]")
        value = typer.prompt(f"{var}", hide_input=True).strip()
    if not value:
        die("no key given")

    # Validate against a model belonging to THE TARGET PROVIDER. Using
    # config.MODEL would test a different provider entirely whenever --provider
    # is passed, and cheerfully accept a key that does not work.
    current_provider = canonical(config.MODEL.split("/", 1)[0]
                                 if "/" in config.MODEL else config.MODEL)
    probe = config.MODEL if target == current_provider else PROBE_MODELS.get(target)

    os.environ[var] = value                      # test before persisting
    if probe:
        err.print(f"[dim]checking the key against {probe}…[/]")
        ok, detail = _try_completion(probe)
        if not ok:
            hint = f"Nothing was written to .env. Check the key is for {target}."
            if target in REGIONAL:
                hint += f"\n{target} has more than one regional endpoint; set {REGIONAL[target]}"
            die(f"that key did not work: {detail}", hint)
    else:
        err.print(f"[yellow]note:[/] no probe model known for {target!r}, so the key "
                  f"was saved unverified. Check it with:  proteus auth test")

    _write_env(var, value)
    out.print(f"[green]saved[/] {var} to .env  ({_mask(value)})")
    out.print("[dim]Restart the gateway to pick it up:  proteus serve[/]")


def _login_oauth() -> None:
    """Device flow for a ChatGPT subscription."""
    import subprocess

    from ._common import REPO

    module_ok = (REPO / "app" / "codex_login.py").exists()
    if not module_ok:
        die("the codex login module is missing", "This needs a source checkout.")
    err.print("[dim]starting the ChatGPT device-code flow…[/]")
    code = subprocess.call([sys.executable, "-u", "-m", "app.codex_login"], cwd=str(REPO))
    if code != 0:
        die("the device flow did not complete")
    out.print("[green]signed in[/]  proteus now has its own token chain")


def _try_completion(model: str) -> tuple[bool, str]:
    """One tiny real completion against `model`. Returns (worked, detail)."""
    import importlib

    from app import config, llm

    importlib.reload(config)                     # pick up the key just set
    async def go() -> str:
        text = []
        async for ev in llm.astream(model=model,
                                    messages=[{"role": "user", "content": "Reply with: OK"}],
                                    tools=None, max_tokens=8, temperature=0):
            if ev["type"] == "text":
                text.append(ev["text"])
        return "".join(text)

    try:
        return True, asyncio.run(go()).strip()
    except Exception as exc:
        message = str(exc)
        low = message.lower()
        if "429" in message or "usage limit" in low or "rate" in low:
            # A rate limit proves the credential was accepted.
            return True, "accepted (provider is rate-limiting right now)"
        return False, message[:180]


@app.command("keygen")
def keygen(length: int = typer.Option(48, "--length", "-l", min=24, max=128)) -> None:
    """Generate a strong value for API_KEY or ADMIN_API_KEY.

    Printed bare so it can be piped or copied. A gateway's API_KEY is the only
    thing between the internet and your provider spend, so it should not be a
    word someone chose.
    """
    import secrets

    print(secrets.token_urlsafe(length)[:length])
