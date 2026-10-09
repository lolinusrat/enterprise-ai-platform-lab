"""Thin client for the OPA REST API."""
import os
import time

import httpx

OPA_URL = os.environ.get("OPA_URL", "http://localhost:8182")


class OPAUnavailable(RuntimeError):
    pass


class OPA:
    def __init__(self, url: str = OPA_URL) -> None:
        self.url = url
        self._http = httpx.Client(base_url=url, timeout=5)

    def healthy(self) -> bool:
        try:
            return self._http.get("/health").status_code == 200
        except httpx.HTTPError:
            return False

    def put_data(self, path: str, value) -> None:
        self._http.put(f"/v1/data/{path}", json=value).raise_for_status()

    def decide(self, path: str, input_: dict) -> tuple[dict, float]:
        """Evaluate a decision. Returns (result, latency_ms). Any failure is a deny: the caller fails closed."""
        start = time.perf_counter()
        try:
            r = self._http.post(f"/v1/data/{path}", json={"input": input_})
            r.raise_for_status()
            result = r.json().get("result")
        except httpx.HTTPError as e:
            raise OPAUnavailable(str(e)) from e
        ms = (time.perf_counter() - start) * 1000
        if not isinstance(result, dict):
            raise OPAUnavailable(f"no decision at {path}")
        return result, ms
