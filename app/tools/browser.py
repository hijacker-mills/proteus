"""
A real browser, as a packaged tool.

Playwright drives a headless Chromium in-process. The previous version shelled
out to a globally-installed Node CLI at a hard-coded path, which is fine on one
laptop and useless in a project other people install: `pip install
proteus-gateway[browser] && playwright install chromium` and it works.

ONE BROWSER, MANY PAGES. Launching Chromium costs ~300ms and ~80MB, so the
browser is started once and shared; each call gets its own page and closes it.
That keeps concurrent calls isolated (their own cookies, their own navigation)
without paying the launch cost per call.

It returns a SNAPSHOT (title, text, links, inputs, buttons) rather than a
screenshot, because a model reads structured text far better than pixels, and
because headless Chrome here has no GPU.

SECURITY. Every URL goes through the SSRF guard in `url_safety` first, so a
model cannot steer this at cloud metadata or your internal network. That guard
fails closed.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .. import config
from .url_safety import is_safe_url_async, refusal

logger = logging.getLogger("proteus.tools.browser")

_ACTIONS = ("navigate", "read", "click", "click_text", "fill", "type", "press", "back", "eval")

# A default-headless Chromium announces itself as HeadlessChrome, which a lot of
# sites answer with a challenge page or an empty body. A normal UA and locale is
# the difference between a usable snapshot and 87 bytes of nothing.
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

_browser: Any = None
_context: Any = None
_pw: Any = None
_lock = asyncio.Lock()


async def _get_context():
    """Launch once, reuse. Guarded so concurrent first-calls don't race."""
    global _browser, _context, _pw
    if _context is not None and _browser is not None and _browser.is_connected():
        return _context
    async with _lock:
        if _context is not None and _browser is not None and _browser.is_connected():
            return _context
        from playwright.async_api import async_playwright

        _pw = await async_playwright().start()
        _browser = await _pw.chromium.launch(
            headless=True,
            # --no-sandbox is required in most containers; --disable-dev-shm-usage
            # avoids Chrome dying on the small /dev/shm a container usually gets.
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        _context = await _browser.new_context(user_agent=_UA, locale="en-US")
        logger.info("headless chromium launched")
        return _context


async def close() -> None:
    global _browser, _context, _pw
    _context = None
    if _browser is not None:
        try:
            await _browser.close()
        except Exception:
            pass
        _browser = None
    if _pw is not None:
        try:
            await _pw.stop()
        except Exception:
            pass
        _pw = None


async def _snapshot(page, max_chars: int) -> dict[str, Any]:
    """What the model actually needs: readable text plus the things it can act on."""
    try:
        text = await page.inner_text("body")
    except Exception:
        text = ""
    js = """() => ({
        links: [...document.querySelectorAll('a[href]')].slice(0, 40)
            .map(a => ({text: (a.innerText||'').trim().slice(0,80), href: a.href}))
            .filter(l => l.text),
        buttons: [...document.querySelectorAll('button,[role=button],input[type=submit]')]
            .slice(0, 20).map(b => (b.innerText || b.value || '').trim().slice(0, 60)).filter(Boolean),
        inputs: [...document.querySelectorAll('input,textarea,select')].slice(0, 20)
            .map(i => ({name: i.name || i.id || '', type: i.type || i.tagName.toLowerCase(),
                        placeholder: i.placeholder || ''})).filter(i => i.name || i.placeholder),
    })"""
    try:
        parts = await page.evaluate(js)
    except Exception:
        parts = {"links": [], "buttons": [], "inputs": []}
    return {
        "url": page.url,
        "title": await page.title(),
        "text": " ".join((text or "").split())[:max_chars],
        **parts,
    }


async def browser(action: str, url: str = "", selector: str = "", text: str = "",
                  key: str = "", script: str = "", max_chars: int = 4000) -> dict[str, Any]:
    if not config.TOOLS_BROWSER:
        return {"error": "browser tool is disabled (set TOOLS_BROWSER=true)"}
    action = (action or "navigate").strip().lower()
    if action not in _ACTIONS:
        return {"error": f"unknown action {action!r}; expected one of {', '.join(_ACTIONS)}"}

    if url and not await is_safe_url_async(url):
        return refusal(url)

    try:
        ctx = await _get_context()
    except ImportError:
        return {"error": "playwright is not installed — pip install 'proteus-gateway[browser]' "
                         "and run: playwright install chromium"}
    except Exception as exc:
        return {"error": f"could not start the browser: {exc}"}

    page = None
    try:
        page = await ctx.new_page()
        page.set_default_timeout(config.TOOLS_BROWSER_TIMEOUT * 1000)
        if url:
            await page.goto(url, wait_until="domcontentloaded")
            # Most pages render their real content after DOMContentLoaded, so
            # snapshotting immediately returns a shell. Bounded, because plenty
            # of pages never reach networkidle at all.
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass

        if action in ("navigate", "read"):
            pass
        elif action == "click":
            await page.click(selector)
        elif action == "click_text":
            await page.get_by_text(text, exact=False).first.click()
        elif action in ("fill", "type"):
            await page.fill(selector, text)
        elif action == "press":
            await page.press(selector or "body", key or "Enter")
        elif action == "back":
            await page.go_back()
        elif action == "eval":
            return {"result": await page.evaluate(script)}

        if action not in ("navigate", "read"):
            # Let whatever the interaction triggered settle before snapshotting.
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
        return await _snapshot(page, max_chars)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass
