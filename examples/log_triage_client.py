#!/usr/bin/env python3
"""
Example: generic log-triage client for the cost-aware LLM gateway.

Same layering as examples/k8s_triage_client.py, but for data-platform
log sources — Docker, Spark, and Flink:

  1. The client tails logs from each source (mocked here; swap in
     `docker logs -f`, Spark's log4j output, or Flink's JobManager REST API).
  2. It applies cheap rule-based classification FIRST:
       healthy   -> nothing to do, no LLM call
       known     -> answer from a runbook, no LLM call
       suspicious-> worth an LLM call for root-cause analysis
  3. Only suspicious events go to the gateway, which makes the LLM call
     as cheap as possible (pre-filter, semantic cache, cheapest-capable
     model) and logs tokens / latency / cost.

The agent decides *whether* an LLM call is warranted.
The gateway decides *how* to make it cheap.

Run with the gateway up:
    docker compose up --build
    python examples/log_triage_client.py
"""
import os
import re
import time
from dataclasses import dataclass

import httpx

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8000")

# Keep prompts token-cheap: the model rarely needs more than this of a
# stack trace to diagnose the failure class.
MAX_LOG_CHARS = 600

# ---------------------------------------------------------------------------
# 1. Log sources (mock). Replace with real tailers in production.
# ---------------------------------------------------------------------------

MOCK_LOGS = [
    # -- docker -----------------------------------------------------------
    ("docker", "daemon", "INFO", "container web-1 started (image: nginx:1.25)"),
    ("docker", "daemon", "ERROR",
     "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
     "Is the docker daemon running?"),
    ("docker", "daemon", "ERROR",
     "driver failed programming external connectivity: Bind for 0.0.0.0:8080 "
     "failed: port is already allocated"),
    ("docker", "daemon", "ERROR",
     "failed to copy files: no space left on device"),
    ("docker", "daemon", "ERROR",
     "error from daemon in stream: Error grabbing logs: "
     "rpc error: code = Unknown desc = unexpected EOF"),
    # -- spark ------------------------------------------------------------
    ("spark", "driver", "INFO", "stage 12 finished: 200/200 tasks, 41s"),
    ("spark", "executor-3", "ERROR",
     "java.lang.OutOfMemoryError: Java heap space "
     "at org.apache.spark.util.collection.ExternalAppendOnlyMap.insertAll"),
    ("spark", "driver", "ERROR",
     "org.apache.spark.sql.AnalysisException: Table or view not found: "
     "prod.orders_daily"),
    ("spark", "executor-7", "ERROR",
     "org.apache.spark.shuffle.FetchFailedException: Failed to connect to "
     "/10.0.4.21:7337"),
    ("spark", "driver", "WARN",
     "Heartbeat from unknown executor 14 (may have been restarted)"),
    # -- flink ------------------------------------------------------------
    ("flink", "jobmanager", "INFO",
     "Checkpoint 881 completed in 2345 ms"),
    ("flink", "jobmanager", "ERROR",
     "Checkpoint 882 expired before completing (timeout 10 min). "
     "Increase the checkpoint timeout or check backpressure."),
    ("flink", "jobmanager", "ERROR",
     "Could not acquire the minimum required resources (4 slots). "
     "Only 2 slots available on 1 TaskManager."),
    ("flink", "taskmanager-2", "ERROR",
     "JobManager lost leadership; suspending job and waiting for new leader"),
]


def tail_logs():
    """Yield (source, component, level, message) tuples like tailed logs."""
    for source, component, level, message in MOCK_LOGS:
        yield {"source": source, "component": component, "level": level,
               "message": message, "ts": time.strftime("%H:%M:%S")}
        time.sleep(0.15)


# ---------------------------------------------------------------------------
# 2. Rule-based classification (no LLM involved)
# ---------------------------------------------------------------------------

KNOWN_ISSUES = [
    # -- docker --
    (re.compile(r"cannot connect to the docker daemon", re.I),
     "Docker daemon is not running (or DOCKER_HOST is wrong). Start Docker "
     "Desktop / the docker service and retry."),
    (re.compile(r"port is already allocated", re.I),
     "Port conflict: another process owns the host port. Stop it or remap "
     "with -p <free-port>:<container-port>."),
    (re.compile(r"no space left on device", re.I),
     "Docker disk space exhausted. Run `docker system prune` and check "
     "dangling volumes with `docker volume ls -f dangling=true`."),
    # -- spark --
    (re.compile(r"outofmemoryerror: java heap space", re.I),
     "Executor/driver OOM. Raise spark.executor.memory (or driver memory), "
     "add memory overhead, or increase parallelism so each task holds less."),
    (re.compile(r"table or view not found", re.I),
     "AnalysisException: the table/view name or database is wrong, or the "
     "catalog was not refreshed. Verify with `SHOW TABLES` / refresh."),
    (re.compile(r"fetchfailedexception", re.I),
     "Shuffle fetch failed: an executor was likely lost (OOM or preempted). "
     "Check executor logs, then raise partitions / executor memory."),
    # -- flink --
    (re.compile(r"checkpoint \d+ expired before completing", re.I),
     "Checkpoint timed out: backpressure or a slow state backend. Increase "
     "the checkpoint timeout and inspect backpressure on the job graph."),
    (re.compile(r"could not acquire the minimum required resources", re.I),
     "Not enough task slots. Add TaskManagers or lower the job parallelism "
     "to fit available slots."),
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
    if event["level"] in {"ERROR", "WARN"} or re.search(
            r"error|exception|failed|killed|traceback|oom", msg, re.I):
        return Verdict("suspicious",
                       f"{event['source']}/{event['component']}: "
                       f"{msg[:MAX_LOG_CHARS]}")
    return Verdict("healthy", "no action needed")


# ---------------------------------------------------------------------------
# 3. Gateway call (only for suspicious events)
# ---------------------------------------------------------------------------

def ask_gateway(event: dict) -> dict:
    log_line = event["message"][:MAX_LOG_CHARS]
    prompt = (
        f"You are a data-platform SRE. Given this {event['source']} log line "
        "from component "
        f"{event['component']}, give a one-paragraph root-cause hypothesis "
        "and the single most useful next debugging command.\n\n"
        f"Log: [{event['level']}] {log_line}"
    )
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

    print(f"{'time':<10} {'source/component':<26} {'verdict':<12} outcome")
    print("-" * 90)
    for event in tail_logs():
        verdict = classify_event(event)
        stats[verdict.decision] += 1
        where = f"{event['source']}/{event['component']}"

        if verdict.decision == "healthy":
            outcome = "skipped (healthy)"
        elif verdict.decision == "known":
            outcome = f"runbook, no LLM call: {verdict.detail[:60]}..."
        else:
            try:
                result = ask_gateway(event)
            except httpx.HTTPError as exc:
                outcome = f"gateway unreachable: {exc}"
                print(f"{event['ts']:<10} {where:<26} "
                      f"{verdict.decision:<12} {outcome}")
                continue
            total_cost += result.get("cost_usd", 0.0)
            outcome = (f"LLM via gateway ({result.get('model')}, "
                       f"${result.get('cost_usd', 0):.4f}): "
                       f"{result['response'][:70]}...")

        print(f"{event['ts']:<10} {where:<26} "
              f"{verdict.decision:<12} {outcome}")

    print("-" * 90)
    n = sum(stats.values())
    avoided = stats["healthy"] + stats["known"]
    print(f"events: {n} | LLM calls avoided by rules: {avoided}/{n} | "
          f"gateway spend: ${total_cost:.4f}")


if __name__ == "__main__":
    main()
