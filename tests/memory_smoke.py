"""
Tiered memory smoke test (Postgres + pgvector).

Proves the full loop:
  1. durable conversation log persists
  2. distiller extracts long-term facts (via the LLM) + embeds them
  3. after CLEARING working memory, semantic recall still surfaces those facts
     — i.e. the bot "remembers you next week".

Run: set -a; . ./.env; set +a; .venv/bin/python tests/memory_smoke.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root on sys.path

from app import db, memory
from app.channels import base
from app.db import get_pool
from app.memory import curator, store

KEY = "fake:memtest"


async def main() -> None:
    # This suite is entirely about the database, so a missing one is a hard stop
    # here — unlike the gateway itself, which runs fine without it.
    await db.init_pool()
    await memory.ensure_schema()
    print("✓ schema ensured")

    # clean slate
    await store.clear_messages(KEY)
    async with get_pool().acquire() as c:
        await c.execute("DELETE FROM proteus.proteus_memory WHERE user_key=$1", KEY)

    sent: list[str] = []

    async def send(t: str) -> None:
        sent.append(t)

    print("\n-- conversation (durable log) --")
    await base.handle_inbound("fake", "memtest",
        "Hi! My name is Fletcher and I'm building an agent gateway called proteus. I prefer concise answers.", send)
    print("turn1:", sent[-1][:100])
    await base.handle_inbound("fake", "memtest",
        "Also note I'm allergic to peanuts.", send)
    print("turn2:", sent[-1][:100])

    recent = await store.recent_messages(KEY, 20)
    print(f"✓ {len(recent)} messages persisted to proteus.proteus_message")

    print("\n-- curate long-term facts (LLM) --")
    n = await curator.curate(KEY)
    async with get_pool().acquire() as c:
        rows = await c.fetch("SELECT text FROM proteus.proteus_memory WHERE user_key=$1 ORDER BY id", KEY)
    print(f"✓ {n} memories stored:")
    for r in rows:
        print("   -", r["text"])

    print("\n-- clear working memory, then recall from long-term --")
    await store.clear_messages(KEY)
    messages, extra = await memory.prepare(KEY, "what do you know about me and my project?")
    print(f"working memory after clear: {len(messages) - 1} prior msgs")
    print("recall block:\n" + (extra or "(empty)"))

    blob = extra.lower()
    assert any(w in blob for w in ("fletcher", "proteus", "concise", "peanut")), "recall failed"
    print("\n✓ ALL MEMORY TESTS PASSED — long-term recall works after working memory cleared")

    await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
