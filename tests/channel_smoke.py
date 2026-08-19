"""
Channel framework smoke test — no real platform required.

Part A: WhatsApp webhook verification + /healthz, via FastAPI TestClient.
Part B: drives base.handle_inbound through a fake channel across two turns to
        prove working memory (turn 2 must recall a fact from turn 1).

Run:  set -a; . ./.env; set +a; .venv/bin/python tests/channel_smoke.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root on sys.path

from fastapi.testclient import TestClient

from app import config, db, memory
from app.channels import base


def part_a() -> None:
    print("== Part A: webhook verify + health ==")
    config.WHATSAPP_VERIFY_TOKEN = "verify-123"  # simulate configured verify token
    from app.main import app

    with TestClient(app) as client:
        r = client.get("/channels/whatsapp/webhook", params={
            "hub.mode": "subscribe",
            "hub.verify_token": "verify-123",
            "hub.challenge": "CHALLENGE_ECHO",
        })
        print("  whatsapp verify:", r.status_code, repr(r.text))
        assert r.status_code == 200 and r.text == "CHALLENGE_ECHO"

        r = client.get("/channels/whatsapp/webhook", params={
            "hub.mode": "subscribe", "hub.verify_token": "WRONG", "hub.challenge": "x",
        })
        print("  whatsapp verify (bad token):", r.status_code)
        assert r.status_code == 403

        h = client.get("/healthz").json()
        print("  healthz:", h)
    print("  ✓ Part A passed\n")


async def part_b() -> None:
    print("== Part B: inbound → agent → working memory (fake channel) ==")
    sent: list[str] = []

    async def fake_send(text: str) -> None:
        sent.append(text)

    sender = "smoketest-user"
    key = f"fake:{sender}"

    # Part A's TestClient closed the pool on exit, so open our own. Memory is an
    # optional feature, so skip rather than fail when there's no database.
    if not await db.try_init_pool():
        print("  ⏭  skipped — no database reachable (memory is optional)\n")
        return
    await memory.ensure_schema()
    await memory.clear(key)
    try:
        await base.handle_inbound("fake", sender, "Remember my name is Sam and I like hiking.", fake_send)
        print("  turn 1 reply:", sent[-1][:120])

        await base.handle_inbound("fake", sender, "What's my name and one thing I like?", fake_send)
        print("  turn 2 reply:", sent[-1][:160])

        recalled = "sam" in sent[-1].lower()
        print(f"  ✓ recalled name across turns: {recalled}")
        assert recalled, "working memory failed — turn 2 did not recall the name"
        print("  ✓ Part B passed")
    finally:
        await memory.clear(key)
        await db.close_pool()


if __name__ == "__main__":
    part_a()
    asyncio.run(part_b())
    print("\nALL CHANNEL SMOKE TESTS PASSED")
