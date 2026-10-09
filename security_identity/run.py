"""Start the agent, authorization server, tool gateway and UI on http://localhost:8090.

OPA must already be running:  docker compose up -d
Then:                          uv run python run.py
"""
import sys
import urllib.request

import uvicorn

PORT = 8090

if __name__ == "__main__":
    try:
        urllib.request.urlopen("http://localhost:8182/health", timeout=2)
    except OSError:
        sys.exit("OPA is not reachable on :8182. Start it first: docker compose up -d")
    print(f"UI and API  http://localhost:{PORT}\nOPA         http://localhost:8182\nCtrl-C to stop.")
    uvicorn.run("secagent.app:app", port=PORT, log_level="warning")
