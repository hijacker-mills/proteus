"""Embeddings for memory (Ollama mxbai-embed-large, 1024-dim — same as the stack)."""
from __future__ import annotations

from .. import config
from ..httpclient import get_client


async def embed(text: str) -> list[float]:
    resp = await get_client().post(
        f"{config.OLLAMA_URL}/api/embed",
        json={"model": config.EMBEDDING_MODEL, "input": [text]},
    )
    resp.raise_for_status()
    return resp.json()["embeddings"][0]
