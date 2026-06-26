"""
Channel framework smoke test — no real platform required.

Part A: WhatsApp webhook verification + /healthz, via FastAPI TestClient.
Part B: drives base.handle_inbound through a fake channel across two turns to
        prove session memory (turn 2 must recall a fact from turn 1).

Run:  set -a; . ./.env; set +a; .venv/bin/python tests/channel_smoke.py
"""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from app import config
from app.channels import base, session


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
    print("== Part B: inbound → agent → session memory (fake channel) ==")
    sent: list[str] = []

    async def fake_send(text: str) -> None:
        sent.append(text)

    sender = "loadtest-user"
    await session.clear(f"fake:{sender}")

    await base.handle_inbound("fake", sender, "Remember my name is Sam and I like hiking.", fake_send)
    print("  turn 1 reply:", sent[-1][:120])

    await base.handle_inbound("fake", sender, "What's my name and one thing I like?", fake_send)
    print("  turn 2 reply:", sent[-1][:160])

    recalled = "sam" in sent[-1].lower()
    print(f"  ✓ recalled name across turns: {recalled}")
    assert recalled, "session memory failed — turn 2 did not recall the name"
    print("  ✓ Part B passed")


if __name__ == "__main__":
    part_a()
    asyncio.run(part_b())
    print("\nALL CHANNEL SMOKE TESTS PASSED")
