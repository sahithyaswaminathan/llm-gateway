"""Kubernetes ingestion: watch pod health, tail logs of unhealthy pods, and
forward each problem to the gateway's /v1/complete endpoint.

Layers (only the last one needs a live cluster):
- extract_problems(pod): pure function, pod object -> list[PodProblem]
- Deduper: suppress repeats of the same problem within a cooldown
- build_prompt(problem): text sent to the gateway
- K8sWatcher: wires the kubernetes client, the above, and the gateway call
"""

import logging
import os
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

import httpx

log = logging.getLogger("k8s_watcher")

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8000")
WATCH_NAMESPACE = os.environ.get("WATCH_NAMESPACE", "")
COOLDOWN_SECONDS = float(os.environ.get("INGEST_COOLDOWN_SECONDS", "300"))
LOG_TAIL_LINES = int(os.environ.get("INGEST_LOG_TAIL_LINES", "20"))
MAX_LOG_CHARS = int(os.environ.get("INGEST_MAX_LOG_CHARS", "1500"))
RESTART_THRESHOLD = int(os.environ.get("INGEST_RESTART_THRESHOLD", "3"))

BAD_WAITING_REASONS = {
    "CrashLoopBackOff",
    "ImagePullBackOff",
    "ErrImagePull",
    "CreateContainerConfigError",
    "CreateContainerError",
    "InvalidImageName",
}
BAD_TERMINATED_REASONS = {"OOMKilled", "Error", "ContainerCannotRun", "DeadlineExceeded"}


@dataclass(frozen=True)
class PodProblem:
    namespace: str
    pod: str
    container: str
    reason: str
    message: str
    restarts: int = 0

    @property
    def key(self) -> tuple:
        return (self.namespace, self.pod, self.container, self.reason)


def extract_problems(pod) -> list[PodProblem]:
    """Inspect a V1Pod (or any object with the same attributes) for unhealthy containers."""
    meta, status = pod.metadata, pod.status
    problems: list[PodProblem] = []

    for cs in (status.container_statuses or []) + (status.init_container_statuses or []):
        state, last = cs.state, cs.last_state
        restarts = cs.restart_count or 0

        if state and state.waiting and state.waiting.reason in BAD_WAITING_REASONS:
            problems.append(PodProblem(
                meta.namespace, meta.name, cs.name,
                state.waiting.reason, state.waiting.message or "", restarts,
            ))
        elif state and state.terminated and state.terminated.reason in BAD_TERMINATED_REASONS:
            t = state.terminated
            problems.append(PodProblem(
                meta.namespace, meta.name, cs.name, t.reason,
                f"exit code {t.exit_code}. {t.message or ''}".strip(), restarts,
            ))
        elif last and last.terminated and last.terminated.reason == "OOMKilled" \
                and restarts >= RESTART_THRESHOLD:
            problems.append(PodProblem(
                meta.namespace, meta.name, cs.name, "OOMKilled",
                f"exit code {last.terminated.exit_code}", restarts,
            ))

    if status.phase == "Pending" and not problems:
        for cond in status.conditions or []:
            if cond.type == "PodScheduled" and cond.status == "False":
                problems.append(PodProblem(
                    meta.namespace, meta.name, "", cond.reason or "Unschedulable",
                    cond.message or "",
                ))
    return problems


class Deduper:
    """Allow a problem key through at most once per cooldown window."""

    def __init__(self, cooldown: float = COOLDOWN_SECONDS, clock: Callable[[], float] = time.monotonic):
        self.cooldown = cooldown
        self.clock = clock
        self._seen: dict[tuple, float] = {}

    def allow(self, key: tuple) -> bool:
        now = self.clock()
        last = self._seen.get(key)
        if last is not None and now - last < self.cooldown:
            return False
        self._seen[key] = now
        return True


def build_prompt(problem: PodProblem, logs: str = "") -> str:
    lines = [f"pod {problem.pod}: {problem.reason}"]
    if problem.message:
        lines[0] += f": {problem.message}"
    lines.append(f"namespace: {problem.namespace}")
    if problem.container:
        lines.append(f"container: {problem.container}")
    if logs:
        lines.append("recent logs:\n" + logs[-MAX_LOG_CHARS:])
    return "\n".join(lines)


class GatewayClient:
    def __init__(self, url: str = GATEWAY_URL, timeout: float = 600):
        self.url = url.rstrip("/")
        self.client = httpx.Client(timeout=timeout)

    def submit(self, prompt: str, task_type: str = "triage") -> dict:
        resp = self.client.post(
            f"{self.url}/v1/complete", json={"prompt": prompt, "task_type": task_type}
        )
        resp.raise_for_status()
        return resp.json()


class K8sWatcher:
    def __init__(
        self,
        core_v1=None,
        gateway: Optional[GatewayClient] = None,
        deduper: Optional[Deduper] = None,
        namespace: str = WATCH_NAMESPACE,
    ):
        self.v1 = core_v1 or self._load_core_v1()
        self.gateway = gateway or GatewayClient()
        self.deduper = deduper or Deduper()
        self.namespace = namespace

    @staticmethod
    def _load_core_v1():
        from kubernetes import client, config

        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        return client.CoreV1Api()

    def fetch_logs(self, problem: PodProblem) -> str:
        if not problem.container:
            return ""
        try:
            return self.v1.read_namespaced_pod_log(
                problem.pod, problem.namespace, container=problem.container,
                tail_lines=LOG_TAIL_LINES, previous=problem.restarts > 0,
            ) or ""
        except Exception as exc:
            log.debug("log fetch failed for %s/%s: %s", problem.namespace, problem.pod, exc)
            return ""

    def handle_pod(self, pod) -> list[dict]:
        results = []
        for problem in extract_problems(pod):
            if not self.deduper.allow(problem.key):
                continue
            prompt = build_prompt(problem, self.fetch_logs(problem))
            try:
                result = self.gateway.submit(prompt)
            except Exception as exc:
                log.error("gateway call failed for %s: %s", problem.key, exc)
                continue
            log.info("%s/%s %s -> %s", problem.namespace, problem.pod, problem.reason,
                     result.get("model"))
            results.append({"problem": problem, "result": result})
        return results

    def _stream(self, w) -> Iterable[dict]:
        if self.namespace:
            return w.stream(self.v1.list_namespaced_pod, self.namespace, timeout_seconds=300)
        return w.stream(self.v1.list_pod_for_all_namespaces, timeout_seconds=300)

    def run_forever(self) -> None:
        from kubernetes import watch

        while True:
            try:
                for event in self._stream(watch.Watch()):
                    if event["type"] in ("ADDED", "MODIFIED"):
                        self.handle_pod(event["object"])
            except Exception as exc:
                log.error("watch stream error, retrying in 5s: %s", exc)
                time.sleep(5)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    K8sWatcher().run_forever()
