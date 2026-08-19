"""
Prometheus metrics, hand-rolled.

No `prometheus_client` dependency: the exposition format is a few lines of text,
and a gateway that already treats every dependency as a liability should not
add one to print counters.

PER PROCESS, and that is not a caveat you can ignore. Each uvicorn worker keeps
its own numbers, so a scrape hits one worker at random and sees roughly 1/N of
the traffic. Two ways to live with that:

  * scrape each worker on its own port (the usual answer), or
  * `sum()` in PromQL and accept that gauges like in-flight are per-worker.

The `proteus_worker_pid` label exists so a scraper can tell workers apart rather
than silently averaging them together.
"""
from __future__ import annotations

import os
import time
from collections import defaultdict
from threading import Lock

# Buckets in seconds. Chosen for LLM latency rather than web latency: sub-100ms
# is impossible with a real model, and 30s+ is normal for a long answer, so the
# default web buckets would put everything in one bin.
_LATENCY_BUCKETS = (0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120)
_TTFT_BUCKETS = (0.1, 0.25, 0.5, 1, 2, 3, 5, 10, 20)

_lock = Lock()
_start = time.time()

_counters: dict[tuple[str, tuple], float] = defaultdict(float)
_gauges: dict[tuple[str, tuple], float] = {}
_hist_sum: dict[tuple[str, tuple], float] = defaultdict(float)
_hist_count: dict[tuple[str, tuple], int] = defaultdict(int)
_hist_buckets: dict[tuple[str, tuple], list[int]] = {}

_HISTOGRAMS = {
    "proteus_request_duration_seconds": _LATENCY_BUCKETS,
    "proteus_time_to_first_token_seconds": _TTFT_BUCKETS,
    "proteus_tool_duration_seconds": _LATENCY_BUCKETS,
}


def _key(labels: dict[str, str] | None) -> tuple:
    return tuple(sorted((labels or {}).items()))


def inc(name: str, value: float = 1.0, **labels: str) -> None:
    with _lock:
        _counters[(name, _key(labels))] += value


def gauge(name: str, value: float, **labels: str) -> None:
    with _lock:
        _gauges[(name, _key(labels))] = value


def observe(name: str, seconds: float, **labels: str) -> None:
    edges = _HISTOGRAMS.get(name, _LATENCY_BUCKETS)
    k = (name, _key(labels))
    with _lock:
        _hist_sum[k] += seconds
        _hist_count[k] += 1
        counts = _hist_buckets.setdefault(k, [0] * len(edges))
        for i, edge in enumerate(edges):
            if seconds <= edge:
                counts[i] += 1        # per-bin here; render() makes it cumulative
                break


def _fmt_labels(pairs: tuple, extra: str = "") -> str:
    parts = [f'{k}="{_escape(v)}"' for k, v in pairs]
    if extra:
        parts.append(extra)
    return "{" + ",".join(parts) + "}" if parts else ""


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def render() -> str:
    """The whole registry in Prometheus text exposition format."""
    with _lock:
        counters = dict(_counters)
        gauges = dict(_gauges)
        h_sum, h_count = dict(_hist_sum), dict(_hist_count)
        h_buckets = {k: list(v) for k, v in _hist_buckets.items()}

    lines: list[str] = []
    pid = str(os.getpid())

    lines.append("# HELP proteus_worker_info Identifies which worker answered this scrape.")
    lines.append("# TYPE proteus_worker_info gauge")
    lines.append(f'proteus_worker_info{{pid="{pid}"}} 1')
    lines.append("# HELP proteus_uptime_seconds Seconds since this worker started.")
    lines.append("# TYPE proteus_uptime_seconds gauge")
    lines.append(f"proteus_uptime_seconds {time.time() - _start:.0f}")

    seen: set[str] = set()
    for (name, labels), value in sorted(counters.items()):
        if name not in seen:
            lines.append(f"# TYPE {name} counter")
            seen.add(name)
        lines.append(f"{name}{_fmt_labels(labels)} {value:g}")

    for (name, labels), value in sorted(gauges.items()):
        if name not in seen:
            lines.append(f"# TYPE {name} gauge")
            seen.add(name)
        lines.append(f"{name}{_fmt_labels(labels)} {value:g}")

    for (name, labels), counts in sorted(h_buckets.items()):
        if name not in seen:
            lines.append(f"# TYPE {name} histogram")
            seen.add(name)
        edges = _HISTOGRAMS.get(name, _LATENCY_BUCKETS)
        # Prometheus histograms are cumulative: each bucket counts everything
        # at or below its edge, so running totals rather than per-bin counts.
        running = 0
        for edge, count in zip(edges, counts):
            running += count
            le = _fmt_labels(labels, 'le="%s"' % edge)
            lines.append(f"{name}_bucket{le} {running}")
        inf = _fmt_labels(labels, 'le="+Inf"')
        total = h_count[(name, labels)]
        lines.append(f"{name}_bucket{inf} {total}")
        lines.append(f"{name}_sum{_fmt_labels(labels)} {h_sum[(name, labels)]:g}")
        lines.append(f"{name}_count{_fmt_labels(labels)} {total}")

    return "\n".join(lines) + "\n"
