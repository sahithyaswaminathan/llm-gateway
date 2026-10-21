#!/usr/bin/env python3
"""
Example: K8s triage agent client for the cost-aware LLM gateway.

Shows the intended layering between project #1 (this gateway) and
project #3 (the K8s triage agent):

  1. The agent tails pod logs (mocked here; swap in the official
     `kubernetes` client against a real cluster).
  2. It applies cheap rule-based health checks FIRST:
       healthy   -> nothing to do, no LLM call
       known     -> answer from a runbook, no LLM call
       suspicious-> worth an LLM call for root-cause analysis
  3. Only suspicious events go to the gateway, which then makes the
     LLM call as cheap as possible (cache, cheapest-capable model)
     and logs tokens / latency / cost.

The agent decides *whether* an LLM call is warranted.
The gateway decides *how* to make it cheap.

Run with the gateway up:
    docker compose up --build
    python examples/k8s_triage_client.py
"""
import os
import re
import time
from dataclasses import dataclass

import httpx

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8000")

# ---------------------------------------------------------------------------
# 1. Log source (mock). Replace with kubernetes client watch in production.
# ---------------------------------------------------------------------------

MOCK_LOGS = [
    ("api-7d9c4f6b9-x2vkd", "INFO", "GET /health 200 3ms"),
    ("api-7d9c4f6b9-x2vkd", "INFO", "request completed in 42ms"),
    ("worker-5f8b2c1a9-q7plm", "INFO", "consumed 120 messages from orders-topic"),
    ("api-7d9c4f6b9-p9zrt", "WARN", "Back-off restarting failed container"),
    ("api-7d9c4f6b9-p9zrt", "ERROR", "CrashLoopBackOff: container exited with code 137"),
    ("worker-5f8b2c1a9-q7plm", "ERROR", "OOMKilled: container exceeded memory limit 512Mi"),
    ("api-7d9c4f6b9-x2vkd", "INFO", "GET /health 200 2ms"),
    ("db-migrate-1a2b3c", "ERROR", "ImagePullBackOff: image not found in registry"),
    ("api-7d9c4f6b9-p9zrt", "ERROR", "CrashLoopBackOff: container exited with code 137"),
    ("worker-5f8b2c1a9-q7plm", "INFO", "consumed 98 messages from orders-topic"),
]


def tail_pod_logs():
    """Yield (pod, level, message) tuples like a `kubectl logs -f` stream."""
    for pod, level, message in MOCK_LOGS:
        yield {"pod": pod, "level": level, "message": message,
               "ts": time.strftime("%H:%M:%S")}
        time.sleep(0.15)


# ---------------------------------------------------------------------------
# 2. Rule-based health classification (no LLM involved)
# ---------------------------------------------------------------------------

KNOWN_ISSUES = [
    (re.compile(r"imagepullbackoff", re.I),
     "ImagePullBackOff: the container image is missing or the registry "
     "credentials are wrong. Check the image tag and imagePullSecrets."),
    (re.compile(r"oomkilled", re.I),
     "OOMKilled: the container exceeded its memory limit. Raise the memory "
     "limit or investigate the memory growth in the application."),
]

DEGRADED_PATTERNS = [
    re.compile(r"crashloopbackoff", re.I),
    re.compile(r"back-off restarting", re.I),
    re.compile(r"error", re.I),
]


@dataclass
class Verdict:
    decision: str          # "healthy" | "known" | "suspicious"
    detail: str


def classify_event(event: dict) -> Verdict:
    """Cheap rules first. Only genuinely ambiguous failures go to the LLM."""
    msg = event["message"]
    for pattern, runbook in KNOWN_ISSUES:
        if pattern.search(msg):
            return Verdict("known", runbook)
    if any(p.search(msg) for p in DEGRADED_PATTERNS):
        return Verdict("suspicious",
                       f"pod {event['pod']}: {msg}")
    return Verdict("healthy", "no action needed")


# ---------------------------------------------------------------------------
# 3. Gateway call (only for suspicious events)
# ---------------------------------------------------------------------------

def ask_gateway(prompt: str) -> dict:
    with httpx.Client(timeout=60) as client:
        resp = client.post(
            f"{GATEWAY_URL}/v1/complete",
            json={"prompt": prompt, "task_type": "triage",
                  "max_cost_usd": 0.02},
        )
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# 4. Triage loop
# ---------------------------------------------------------------------------

def main() -> None:
    stats = {"healthy": 0, "known": 0, "suspicious": 0}
    total_cost = 0.0

    print(f"{'time':<10} {'pod':<28} {'verdict':<12} outcome")
    print("-" * 90)
    for event in tail_pod_logs():
        verdict = classify_event(event)
        stats[verdict.decision] += 1

        if verdict.decision == "healthy":
            outcome = "skipped (healthy)"
        elif verdict.decision == "known":
            outcome = f"runbook, no LLM call: {verdict.detail[:60]}..."
        else:
            prompt = (
                "You are a Kubernetes SRE. Given this pod log line, "
                "give a one-paragraph root-cause hypothesis and the single "
                "most useful next kubectl command.\n\n"
                f"Log: [{event['level']}] {event['message']}"
            )
            try:
                result = ask_gateway(prompt)
            except httpx.HTTPError as exc:
                outcome = f"gateway unreachable: {exc}"
                print(f"{event['ts']:<10} {event['pod']:<28} "
                      f"{verdict.decision:<12} {outcome}")
                continue
            total_cost += result.get("cost_usd", 0.0)
            outcome = (f"LLM via gateway ({result.get('model')}, "
                       f"${result.get('cost_usd', 0):.4f}): "
                       f"{result['response'][:70]}...")

        print(f"{event['ts']:<10} {event['pod']:<28} "
              f"{verdict.decision:<12} {outcome}")

    print("-" * 90)
    n = sum(stats.values())
    avoided = stats["healthy"] + stats["known"]
    print(f"events: {n} | LLM calls avoided by rules: {avoided}/{n} | "
          f"gateway spend: ${total_cost:.4f}")


if __name__ == "__main__":
    main()
