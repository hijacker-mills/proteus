"""
Memory tools — read/write/search the student's pgvector "brain" in Neon.

Direct async Postgres access via asyncpg pool. Embeddings via Ollama
(mxbai-embed-large, 1024-dim — the same model the whole IntelliQ stack uses).

Every handler is scoped to `user_id`, which the gateway supplies from the
authenticated request — never from model input.
"""
from __future__ import annotations

import uuid
from typing import Any

from .. import config
from ..db import get_pool
from ..httpclient import get_client


async def _embed(text: str) -> list[float]:
    resp = await get_client().post(
        f"{config.OLLAMA_URL}/api/embed",
        json={"model": config.EMBEDDING_MODEL, "input": [text]},
    )
    resp.raise_for_status()
    return resp.json()["embeddings"][0]


def _vec(values: list[float]) -> str:
    return "[" + ",".join(str(x) for x in values) + "]"


async def memory_write(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    text = args.get("text", "")
    memory_type = args.get("memory_type", "user_memory")
    if not text:
        return {"error": "text is required"}
    embedding = await _embed(text)
    chunk_id = f"mem:{uuid.uuid4()}"
    async with get_pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO "NoteEmbedding"
                ("id", "userId", "chunkId", "text", "memoryType", "source",
                 "embedding", "createdAt", "updatedAt")
            VALUES
                (gen_random_uuid()::text, $1, $2, $3, $4, 'qubi',
                 $5::vector, NOW(), NOW())
            ON CONFLICT ("chunkId") DO UPDATE SET
                "text"      = EXCLUDED."text",
                "embedding" = EXCLUDED."embedding",
                "updatedAt" = NOW()
            """,
            user_id, chunk_id, text, memory_type, _vec(embedding),
        )
    return {"ok": True, "chunk_id": chunk_id}


async def memory_read(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    limit = int(args.get("limit", 10))
    memory_type = args.get("memory_type", "user_memory")
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT "chunkId", "text", "createdAt"
            FROM   "NoteEmbedding"
            WHERE  "userId" = $1 AND "memoryType" = $2
            ORDER  BY "createdAt" DESC
            LIMIT  $3
            """,
            user_id, memory_type, limit,
        )
    memories = []
    for r in rows:
        created = r["createdAt"]
        memories.append({
            "chunkId": r["chunkId"],
            "text": r["text"],
            "createdAt": created.isoformat() if hasattr(created, "isoformat") else created,
        })
    return {"memories": memories}


async def memory_search(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    query = args.get("query", "")
    top_k = int(args.get("top_k", 5))
    memory_type = args.get("memory_type")
    if not query:
        return {"error": "query is required"}
    vec = _vec(await _embed(query))
    async with get_pool().acquire() as conn:
        if memory_type:
            rows = await conn.fetch(
                """
                SELECT "chunkId", "text", "memoryType",
                       1 - (embedding <=> $1::vector) AS score
                FROM   "NoteEmbedding"
                WHERE  "userId" = $2 AND "memoryType" = $3
                ORDER  BY embedding <=> $1::vector
                LIMIT  $4
                """,
                vec, user_id, memory_type, top_k,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT "chunkId", "text", "memoryType",
                       1 - (embedding <=> $1::vector) AS score
                FROM   "NoteEmbedding"
                WHERE  "userId" = $2
                  AND  "memoryType" IN ('user_memory', 'mastery_signal')
                ORDER  BY embedding <=> $1::vector
                LIMIT  $3
                """,
                vec, user_id, top_k,
            )
    results = [
        {
            "chunkId": r["chunkId"],
            "text": r["text"],
            "memoryType": r["memoryType"],
            "score": float(r["score"]) if r["score"] is not None else 0.0,
        }
        for r in rows
    ]
    return {"results": results}
