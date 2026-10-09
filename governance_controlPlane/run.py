"""Start the control plane (8080), gateways (8081) and BU agent runtime (8082).

OPA must already be running:  docker compose up -d
Then:                          uv run python run.py
Stop with Ctrl-C. The ledger and registry start fresh on every run.
"""
import subprocess
import sys
import time
import urllib.request

SERVICES = [("control plane", "cp.controlplane:app", 8080), ("gateways", "cp.gateway:app", 8081), ("agent runtime", "cp.agent_runtime:app", 8082)]


def up(url: str, tries: int = 60) -> bool:
    for _ in range(tries):
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except OSError:
            time.sleep(0.5)
    return False


def main() -> None:
    if not up("http://localhost:8181/health", tries=4):
        sys.exit("OPA is not reachable on :8181. Start it first: docker compose up -d")
    procs = []
    try:
        for label, target, port in SERVICES:
            procs.append(subprocess.Popen([sys.executable, "-m", "uvicorn", target, "--port", str(port), "--log-level", "warning"]))
            if label == "control plane" and not up(f"http://localhost:{port}/api/health"):
                sys.exit("Control plane did not start; see the error above.")
        if not all(up(f"http://localhost:{p}/docs") for *_, p in SERVICES):
            sys.exit("A service did not start; see the error above.")
        print("Control plane + UI  http://localhost:8080\nGateways            http://localhost:8081/docs\n"
              "Agent runtime       http://localhost:8082/docs\nPolicy engine (OPA) http://localhost:8181\n\n"
              "Run the scenarios:  uv run python demo.py\nCtrl-C to stop.")
        while all(p.poll() is None for p in procs):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            p.terminate()


if __name__ == "__main__":
    main()
