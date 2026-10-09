"""Scripted run of every scenario under live load, printing a timeline and failed requests per phase.

    uv run --group ui python demo.py | tee demo_output.txt

Needs the cluster from scripts/up.sh. Uses the same functions as the demo UI (ui/server.py).
"""
import asyncio
import time

import httpx

from ui import server as ui

T0 = time.time()


def say(msg: str = "") -> None:
    print(f"[{time.time() - T0:6.1f}s] {msg}" if msg else "", flush=True)


def snap() -> dict:
    s = ui.rt.snapshot
    pods = s.get("pods", [])
    return {"ready": sum(p["phase"] == "Ready" for p in pods), "pods": len(pods), "desired": s["hpa"]["desired"],
            "cpu": s["hpa"]["cpu_pct"], "restarts": sum(p["restarts"] for p in pods),
            "nodes": {n: sum(p["node"] == n and p["phase"] == "Ready" for p in pods) for n in ui.WORKERS}}


def line() -> str:
    s, w = snap(), ui.rt.load.window()
    spread = " ".join(f"{n.replace('agent-runtime-', '')}={c}" for n, c in s["nodes"].items())
    return (f"ready {s['ready']}/{s['pods']} desired {s['desired']} cpu {s['cpu']}% | {spread} | "
            f"{w['rps']}/s p95 {w['p95_ms']}ms err {w['error_rate']:.1%}")


class Phase:
    """Counts requests and failures sent while the phase runs."""

    def __init__(self, title: str, counted: bool = True):
        self.title, self.counted = title, counted

    async def __aenter__(self):
        say()
        say(f"== {self.title}")
        self.before = dict(ui.rt.load.total)
        return self

    async def __aexit__(self, *exc):
        if not self.counted:
            return
        after = ui.rt.load.total
        sent = sum(after.values()) - sum(self.before.values())
        failed = {str(k): v - self.before.get(k, 0) for k, v in after.items() if k != 200 and v - self.before.get(k, 0)}
        say(f"   requests {sent}, failed {sum(failed.values())} {failed or ''}")
        RESULTS.append((self.title, sent, sum(failed.values())))


RESULTS: list[tuple[str, int, int]] = []


async def wait_for(cond, timeout: float, every: float = 2.0, show: float = 10.0) -> bool:
    deadline, last = time.time() + timeout, 0.0
    while time.time() < deadline:
        if cond():
            return True
        if time.time() - last >= show:
            say("   " + line())
            last = time.time()
        await asyncio.sleep(every)
    return False


async def main() -> None:
    await ui.rt.read_token()
    sampler = asyncio.create_task(ui.sampler())
    await asyncio.sleep(3)
    say(f"cluster: {line()}")
    async with httpx.AsyncClient() as c:
        info = (await c.get(f"{ui.AGENT_URL}/v1/info")).json()
    say(f"agent {info['version']} backends {info['backends']}")

    async with Phase("1. Chat through the Service", counted=False):
        for backend in info["backends"]:
            r = await ui.chat(ui.ChatIn(message="What is the status and fraud risk of A1003?", backend=backend))
            b = r["body"]
            say(f"   {backend}: {r['status']} {r['latency_ms']}ms on {r['served_by']} tools={b['tools']}")
            say(f"   answer: {b['answer'][:150]}")

    async with Phase("2. Autoscale: 120 req/s from 2 replicas"):
        ui.rt.load.start(120, 1500)
        ok = await wait_for(lambda: snap()["ready"] >= 5 and snap()["desired"] == snap()["ready"], 150)
        await asyncio.sleep(20)
        say(f"   {'scaled out' if ok else 'did not settle'}: {line()}")

    ui.rt.load.start(60, 1500)
    say("load lowered to 60 req/s for the remaining phases")
    await asyncio.sleep(10)

    async with Phase("3. Readiness fault on one pod"):
        pod = next(p["name"] for p in ui.rt.snapshot["pods"] if p["phase"] == "Ready")
        await ui.fault(pod, ui.FaultIn(mode="unready"))
        say(f"   {pod}: readiness failing")
        await wait_for(lambda: any(p["name"] == pod and p["phase"] == "NotReady" for p in ui.rt.snapshot["pods"]), 30)
        await asyncio.sleep(8)
        served = ui.rt.load.window(5)["by_pod"].get(pod, 0)
        say(f"   NotReady, restarts unchanged, requests it served in the last 5s: {served}")
        await ui.fault(pod, ui.FaultIn(mode="none"))
        await wait_for(lambda: any(p["name"] == pod and p["phase"] == "Ready" for p in ui.rt.snapshot["pods"]), 30)
        say(f"   fault cleared, back in the Service: {line()}")

    async with Phase("4. Liveness fault on one pod"):
        pod = next(p for p in ui.rt.snapshot["pods"] if p["phase"] == "Ready")
        await ui.fault(pod["name"], ui.FaultIn(mode="unhealthy"))
        say(f"   {pod['name']}: liveness failing (restarts {pod['restarts']})")
        restarted = await wait_for(lambda: any(p["name"] == pod["name"] and p["restarts"] > pod["restarts"]
                                               for p in ui.rt.snapshot["pods"]), 60)
        await wait_for(lambda: snap()["ready"] == snap()["pods"], 60)
        say(f"   {'container restarted by the kubelet' if restarted else 'no restart seen'}: {line()}")

    async with Phase("5. Delete a pod"):
        pod = ui.rt.snapshot["pods"][0]["name"]
        before = {p["name"] for p in ui.rt.snapshot["pods"]}
        await ui.delete_pod(pod)
        say(f"   deleted {pod}")
        await wait_for(lambda: {p["name"] for p in ui.rt.snapshot["pods"] if p["phase"] == "Ready"} - before, 60)
        await wait_for(lambda: snap()["ready"] == snap()["pods"], 60)
        say(f"   replacement Ready: {line()}")

    async with Phase("6. Rolling update under load"):
        old = {p["name"] for p in ui.rt.snapshot["pods"]}
        await ui.rollout()
        await asyncio.sleep(4)
        done = await wait_for(lambda: not (old & {p["name"] for p in ui.rt.snapshot["pods"]})
                              and snap()["ready"] == snap()["pods"], 240)
        say(f"   {'every pod replaced' if done else 'rollout incomplete'}: {line()}")

    async with Phase("7. Rotate the API token"):
        r = await ui.rotate()
        say(f"   token {r['old_fp']} -> {r['new_fp']}; old kept as api-token-previous")
        await wait_for(lambda: ui.rt.rotation["state"] != "propagating", 240)
        rot = ui.rt.rotation
        say(f"   {rot['state']}: {rot['pods_on_new']}/{rot['pods_total']} pods on new token after "
            f"{rot.get('seconds', '?')}s, client switched, restarts {snap()['restarts']} (unchanged)")

    async with Phase("8. Drain a worker node"):
        node = ui.WORKERS[0]
        say(f"   before: {line()}")
        await ui._drain(node)
        await wait_for(lambda: snap()["ready"] == snap()["pods"] and snap()["nodes"][node] == 0, 120)
        say(f"   drained: {line()}")
        await ui.uncordon(node)
        say(f"   {node} uncordoned (running pods are not moved back; new ones may land there)")

    ui.rt.load.stop()
    await asyncio.sleep(2)

    say()
    say("== Summary")
    for title, sent, failed in RESULTS:
        say(f"   {title:<42} requests {sent:>6}  failed {failed}")
    say()
    say("Actions and scaling / eviction events (oldest first). Startup-probe failures during the 4s")
    say("warm-up and readiness failures on pods that are shutting down are expected and omitted.")
    for a in reversed(ui.rt.activity):
        if a["source"] == "ui" or any(k in a["text"] for k in ("SuccessfulRescale", "Evict", "BackOff", "Failed")):
            say(f"   {time.strftime('%H:%M:%S', time.localtime(a['t']))} {a['source']:<10} {a['text'][:150]}")
    sampler.cancel()


if __name__ == "__main__":
    asyncio.run(main())
