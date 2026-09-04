"""User identity proofs for trusted application backends and local clients."""
from __future__ import annotations

import hashlib
import hmac
import time

from . import config


def signed_headers(user_id: str) -> dict[str, str]:
    """Return user headers, adding the HMAC proof when the gateway requires it."""
    user_id = user_id.strip()
    headers = {"X-Proteus-User-Id": user_id}
    if not config.PROTEUS_IDENTITY_SECRET:
        return headers
    timestamp = str(int(time.time()))
    signature = hmac.new(
        config.PROTEUS_IDENTITY_SECRET.encode(),
        f"{user_id}:{timestamp}".encode(),
        hashlib.sha256,
    ).hexdigest()
    headers["X-Proteus-Identity-Timestamp"] = timestamp
    headers["X-Proteus-Identity-Signature"] = signature
    return headers
