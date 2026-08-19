"""
SSRF guard for any tool that fetches a URL the model chose.

Ported from the Hermes agent, whose blocklist is the product of getting this
wrong a few times. The threat is simple and the consequences are not: a model
can be talked into fetching a URL, your server has network positions the user
does not, and `http://169.254.169.254/` hands out cloud credentials.

It **fails closed**. DNS failure, an unparseable address, a surprise exception:
all block. A URL the HTTP client could not have fetched anyway loses nothing by
being refused, and a parsing edge case must never become the bypass.

Two tiers, deliberately:

  ALWAYS blocked   cloud metadata endpoints and the whole link-local range.
                   No toggle reaches these. They have no legitimate use from an
                   agent, on any deployment, ever.
  Blocked unless   ordinary private space: loopback, RFC1918, CGNAT, reserved,
  ALLOW_PRIVATE_   multicast. Turned off by operators whose tools genuinely
  URLS=true        need to reach an internal service.

Note CGNAT (100.64.0.0/10) is checked explicitly: `ipaddress.is_private` returns
False for it, so the obvious implementation lets through Tailscale, WireGuard
and a good deal of cloud internal networking.

IPv4-mapped IPv6 (`::ffff:a.b.c.d`) is unwrapped before checking, because a
resolver may return it for an IPv4-only host and Python treats it as a distinct
address that matches none of the IPv4 rules.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlparse

from .. import config

logger = logging.getLogger("proteus.url_safety")

# Never reachable, whatever the configuration says.
_BLOCKED_HOSTNAMES = frozenset({
    "metadata.google.internal",
    "metadata.goog",
})

_ALWAYS_BLOCKED_IPS = frozenset({
    ipaddress.ip_address("169.254.169.254"),    # AWS/GCP/Azure/DO/Oracle metadata
    ipaddress.ip_address("169.254.170.2"),      # AWS ECS task metadata (task IAM creds)
    ipaddress.ip_address("169.254.169.253"),    # Azure IMDS wire server
    ipaddress.ip_address("fd00:ec2::254"),      # AWS metadata over IPv6
    ipaddress.ip_address("100.100.100.200"),    # Alibaba Cloud metadata
    ipaddress.ip_address("::ffff:169.254.169.254"),
    ipaddress.ip_address("::ffff:169.254.170.2"),
    ipaddress.ip_address("::ffff:169.254.169.253"),
    ipaddress.ip_address("::ffff:100.100.100.200"),
})

_ALWAYS_BLOCKED_NETWORKS = (
    ipaddress.ip_network("169.254.0.0/16"),         # all link-local
    ipaddress.ip_network("::ffff:169.254.0.0/112"), # ...and its IPv4-mapped form
)

# RFC 6598 shared address space. `is_private` is False for this range, so it has
# to be named explicitly or CGNAT and VPN addresses sail straight through.
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Ordinary private space (the tier an operator may switch off)."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified or ip in _CGNAT_NETWORK
    )


def _is_always_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return ip in _ALWAYS_BLOCKED_IPS or any(ip in net for net in _ALWAYS_BLOCKED_NETWORKS)


def is_safe_url(url: str) -> bool:
    """True if this URL may be fetched. Blocking is the default on any doubt."""
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").strip().lower().rstrip(".")
        scheme = (parsed.scheme or "").strip().lower()

        if scheme not in ("http", "https"):
            logger.warning("blocked: unsupported scheme %r", scheme or "<empty>")
            return False
        if not hostname:
            return False
        if hostname in _BLOCKED_HOSTNAMES:
            logger.warning("blocked: internal hostname %s", hostname)
            return False

        try:
            addrs = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except socket.gaierror:
            logger.warning("blocked: DNS resolution failed for %s", hostname)
            return False

        for _family, _type, _proto, _canon, sockaddr in addrs:
            raw = sockaddr[0].split("%")[0]          # strip any IPv6 scope id
            try:
                ip = ipaddress.ip_address(raw)
            except ValueError:
                logger.warning("blocked: unparseable address %r for %s", sockaddr[0], hostname)
                return False

            if _is_always_blocked_ip(ip):
                logger.warning("blocked: cloud metadata address %s -> %s", hostname, raw)
                return False
            if not config.ALLOW_PRIVATE_URLS and _is_blocked_ip(ip):
                logger.warning("blocked: private/internal address %s -> %s", hostname, raw)
                return False
        return True

    except Exception as exc:
        logger.warning("blocked: url safety check errored for %s: %s", url, exc)
        return False


async def is_safe_url_async(url: str) -> bool:
    """`is_safe_url` off the event loop — getaddrinfo blocks."""
    return await asyncio.to_thread(is_safe_url, url)


def refusal(url: str) -> dict:
    """The message a tool returns when a URL is refused."""
    return {"error": f"refused to fetch {url!r}: it resolves to a private or "
                     f"metadata address, which is blocked to prevent SSRF. "
                     f"Set ALLOW_PRIVATE_URLS=true only if internal targets are intended."}
