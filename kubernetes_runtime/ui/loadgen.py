"""Open-loop load generator: sends a target number of requests per second to the agent Service.

Open loop (fixed arrival rate) rather than N workers in a loop: a closed loop of CPU-bound requests
saturates whatever capacity exists, so the HPA would always scale to max. A fixed rate lets
the replica count settle where the load needs it.

Each request opens a new TCP connection. kube-proxy balances per connection, not per request, so
keep-alive would pin every worker to the pods that existed when the test started and new replicas
from a scale-out would sit idle. (An L7 load balancer such as ALB / GCLB balances per request.)
"""
import asyncio
import itertools
import time
from collections import Counter, deque

import httpx

MAX_INFLIGHT = 64
MESSAGES = [f"What is the fraud risk and status of {oid}?" for oid in ("A1001", "A1002", "A1003", "A1004")]


class LoadGen:
    def __init__(self, base_url: str, token_fn):
        self.base_url = base_url
        self.token_fn = token_fn
        self.task: asyncio.Task | None = None
        self.rps = 0
        self.ends_at = 0.0
        self.samples: deque = deque(maxlen=20000)   # (finished_at, latency_s, status, pod)
        self.total = Counter()

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def start(self, rps: int, duration_s: int) -> None:
        self.stop()
        self.rps, self.ends_at = rps, time.time() + duration_s
        self.task = asyncio.create_task(self._run(rps))

    def stop(self) -> None:
        if self.running:
            self.task.cancel()
        self.rps = 0

    async def _one(self, client: httpx.AsyncClient, message: str) -> None:
        started = time.perf_counter()
        try:
            r = await client.post("/v1/chat", json={"message": message, "backend": "scripted"},
                                  headers={"Authorization": f"Bearer {self.token_fn()}", "Connection": "close"})
            status, pod = r.status_code, r.headers.get("x-served-by", "?")
        except httpx.HTTPError as e:
            status, pod = type(e).__name__, "-"
        self.samples.append((time.time(), time.perf_counter() - started, status, pod))
        self.total[status] += 1

    async def _run(self, rps: int) -> None:
        limits = httpx.Limits(max_keepalive_connections=0, max_connections=MAX_INFLIGHT)
        msgs = itertools.cycle(MESSAGES)
        inflight: set[asyncio.Task] = set()
        async with httpx.AsyncClient(base_url=self.base_url, limits=limits, timeout=15) as client:
            next_at = time.perf_counter()
            while time.time() < self.ends_at:
                if len(inflight) < MAX_INFLIGHT:   # past this the target rate is not being met: shed
                    t = asyncio.create_task(self._one(client, next(msgs)))
                    inflight.add(t)
                    t.add_done_callback(inflight.discard)
                next_at += 1 / rps
                await asyncio.sleep(max(0, next_at - time.perf_counter()))
            if inflight:
                await asyncio.wait(inflight, timeout=15)
        self.rps = 0

    def window(self, seconds: float = 5.0) -> dict:
        """Throughput, errors, latency and per-pod share over the last few seconds."""
        cutoff = time.time() - seconds
        recent = [s for s in self.samples if s[0] >= cutoff]
        lat = sorted(s[1] for s in recent)
        ok = [s for s in recent if s[2] == 200]

        def pct(p):
            return round(lat[min(len(lat) - 1, int(p * len(lat)))] * 1000, 1) if lat else None

        return {
            "running": self.running, "target_rps": self.rps,
            "remaining_s": max(0, round(self.ends_at - time.time())) if self.running else 0,
            "rps": round(len(recent) / seconds, 1), "error_rate": round(1 - len(ok) / len(recent), 3) if recent else 0,
            "p50_ms": pct(0.5), "p95_ms": pct(0.95),
            "by_pod": dict(Counter(s[3] for s in ok)),
            "errors": dict(Counter(str(s[2]) for s in recent if s[2] != 200)),
            "total": {str(k): v for k, v in self.total.items()},
        }
