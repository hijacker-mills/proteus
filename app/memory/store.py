"""
Postgres + pgvector storage for proteus's tiered memory.

Two tables (separate from IntelliQ's NoteEmbedding):
  proteus.proteus_message  — durable conversation log (working memory; by recency)
  proteus.proteus_memory   — distilled long-term facts/preferences (by similarity)

SCHEMA: these live in the dedicated `proteus` Postgres schema, NOT in `public`.
This is load-bearing. IntelliQ's Prisma owns `public`, and any table it finds
there that isn't in schema.prisma is treated as drift — every generated
migration carried `DROP TABLE "proteus_*"`, and two of them were applied (2026-06-27,
2026-07-15), silently destroying all memory and cron state. Prisma only diffs the
schemas listed in its datasource (`public`), so an `proteus` schema is invisible to
it. Keep every statement here schema-qualified.

All vector ops pass the embedding as a text literal cast to ::vector (no driver
registration needed), matching the rest of the stack.
"""
from __future__ import annotations

import re
from typing import Any

from ..db import get_pool

_DDL = [
    "CREATE SCHEMA IF NOT EXISTS proteus",
    "CREATE EXTENSION IF NOT EXISTS vector",
    """
    CREATE TABLE IF NOT EXISTS proteus.proteus_message (
        id         BIGSERIAL PRIMARY KEY,
        user_key   TEXT        NOT NULL,
        channel    TEXT        NOT NULL DEFAULT '',
        role       TEXT        NOT NULL,
        content    TEXT        NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS proteus_message_user_ts ON proteus.proteus_message (user_key, created_at)",
    """
    CREATE TABLE IF NOT EXISTS proteus.proteus_memory (
        id           BIGSERIAL PRIMARY KEY,
        user_key     TEXT        NOT NULL,
        kind         TEXT        NOT NULL DEFAULT 'fact',
        text         TEXT        NOT NULL,
        fingerprint  TEXT        NOT NULL DEFAULT '',
        embedding    vector(1024),
        salience     REAL        NOT NULL DEFAULT 1.0,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_used_at TIMESTAMPTZ
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS proteus_memory_user_fp ON proteus.proteus_memory (user_key, fingerprint)",
    """
    CREATE INDEX IF NOT EXISTS proteus_memory_hnsw ON proteus.proteus_memory
        USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)
    """,
    """
    CREATE TABLE IF NOT EXISTS proteus.proteus_todo (
        user_key   TEXT PRIMARY KEY,
        items      JSONB       NOT NULL DEFAULT '[]'::jsonb,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
]


async def ensure_schema() -> None:
    async with get_pool().acquire() as conn:
        for stmt in _DDL:
            await conn.execute(stmt)


def _vec(values: list[float]) -> str:
    return "[" + ",".join(str(x) for x in values) + "]"


def fingerprint(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())[:300]


# ── working memory (conversation log) ────────────────────────────────────────

async def add_message(user_key: str, channel: str, role: str, content: str) -> None:
    async with get_pool().acquire() as conn:
        await conn.execute(
            'INSERT INTO proteus.proteus_message ("user_key","channel","role","content") VALUES ($1,$2,$3,$4)',
            user_key, channel, role, content,
        )


async def recent_messages(user_key: str, limit: int) -> list[dict[str, Any]]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            'SELECT role, content FROM proteus.proteus_message WHERE user_key=$1 ORDER BY created_at DESC, id DESC LIMIT $2',
            user_key, limit,
        )
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


async def user_turn_count(user_key: str) -> int:
    async with get_pool().acquire() as conn:
        return await conn.fetchval(
            "SELECT count(*) FROM proteus.proteus_message WHERE user_key=$1 AND role='user'", user_key
        )


async def clear_messages(user_key: str) -> int:
    async with get_pool().acquire() as conn:
        res = await conn.execute("DELETE FROM proteus.proteus_message WHERE user_key=$1", user_key)
    return int(res.split()[-1]) if res else 0


# ── long-term memory ─────────────────────────────────────────────────────────

async def recall(user_key: str, query_vec: list[float], k: int, min_score: float) -> list[dict[str, Any]]:
    vec = _vec(query_vec)
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, text, kind, 1 - (embedding <=> $1::vector) AS score
            FROM   proteus.proteus_memory
            WHERE  user_key = $2 AND embedding IS NOT NULL
            ORDER  BY embedding <=> $1::vector
            LIMIT  $3
            """,
            vec, user_key, k,
        )
        hits = [
            {"id": r["id"], "text": r["text"], "kind": r["kind"], "score": float(r["score"])}
            for r in rows if float(r["score"]) >= min_score
        ]
        if hits:
            await conn.execute(
                "UPDATE proteus.proteus_memory SET last_used_at = now() WHERE id = ANY($1::bigint[])",
                [h["id"] for h in hits],
            )
    return hits


async def nearest_score(user_key: str, query_vec: list[float]) -> float | None:
    vec = _vec(query_vec)
    async with get_pool().acquire() as conn:
        val = await conn.fetchval(
            """
            SELECT 1 - (embedding <=> $1::vector)
            FROM   proteus.proteus_memory
            WHERE  user_key = $2 AND embedding IS NOT NULL
            ORDER  BY embedding <=> $1::vector
            LIMIT  1
            """,
            vec, user_key,
        )
    return float(val) if val is not None else None


async def add_memory(user_key: str, kind: str, text: str, embedding: list[float]) -> None:
    async with get_pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO proteus.proteus_memory ("user_key","kind","text","fingerprint","embedding","last_used_at")
            VALUES ($1,$2,$3,$4,$5::vector, now())
            ON CONFLICT ("user_key","fingerprint") DO UPDATE SET
                "salience"     = proteus.proteus_memory."salience" + 0.5,
                "last_used_at" = now()
            """,
            user_key, kind, text, fingerprint(text), _vec(embedding),
        )


async def list_memories(user_key: str, limit: int = 200) -> list[dict[str, Any]]:
    """All of a user's memories (for the curator to reconcile against)."""
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            'SELECT id, kind, text, salience FROM proteus.proteus_memory WHERE user_key=$1 ORDER BY created_at ASC LIMIT $2',
            user_key, limit,
        )
    return [{"id": r["id"], "kind": r["kind"], "text": r["text"], "salience": float(r["salience"])} for r in rows]


async def update_memory(mem_id: int, user_key: str, text: str, embedding: list[float]) -> bool:
    """Overwrite an existing memory in place (the 'replace' op)."""
    async with get_pool().acquire() as conn:
        try:
            res = await conn.execute(
                """
                UPDATE proteus.proteus_memory
                SET "text"=$3, "fingerprint"=$4, "embedding"=$5::vector,
                    "salience" = "salience" + 0.5, "last_used_at" = now()
                WHERE id=$1 AND user_key=$2
                """,
                mem_id, user_key, text, fingerprint(text), _vec(embedding),
            )
        except Exception:
            return False  # e.g. fingerprint collides with another entry
    return res.split()[-1] != "0"


async def remove_memory(mem_id: int, user_key: str) -> bool:
    async with get_pool().acquire() as conn:
        res = await conn.execute("DELETE FROM proteus.proteus_memory WHERE id=$1 AND user_key=$2", mem_id, user_key)
    return res.split()[-1] != "0"


async def trim(user_key: str, cap: int) -> int:
    """Backstop: keep only the top `cap` memories by salience/recency."""
    async with get_pool().acquire() as conn:
        res = await conn.execute(
            """
            DELETE FROM proteus.proteus_memory WHERE id IN (
                SELECT id FROM proteus.proteus_memory WHERE user_key=$1
                ORDER BY salience DESC, last_used_at DESC NULLS LAST, created_at DESC
                OFFSET $2
            )
            """,
            user_key, cap,
        )
    return int(res.split()[-1]) if res else 0
