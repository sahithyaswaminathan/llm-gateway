"""End-to-end: fake pods -> K8sWatcher -> real gateway process over HTTP -> mock Ollama.

Only the Kubernetes API and the LLM are faked. The gateway runs as a real uvicorn
subprocess with real Chroma, real embeddings and the real runbooks.yaml.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest

from app.ingest.k8s_watcher import Deduper, GatewayClient, K8sWatcher
from tests.test_k8s_watcher import FakeV1, make_pod

ROOT = Path(__file__).resolve().parent.parent


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MockOllama:
    def __init__(self):
        self.prompts = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.prompts.append(body["prompt"])
                out = json.dumps({"response": "Check the referenced secret exists in the namespace."}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass

        self.port = free_port()
        self.server = HTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("e2e")
    runbooks = tmp / "runbooks.yaml"
    shutil.copy(ROOT / "runbooks.yaml", runbooks)
    ollama = MockOllama()
    port = free_port()
    env = {
        **os.environ,
        "VECTOR_DB_DIR": str(tmp / "chroma"),
        "RUNBOOKS_PATH": str(runbooks),
        "OLLAMA_URL": f"http://127.0.0.1:{ollama.port}",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    for _ in range(90):
        try:
            if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                break
        except Exception:
            time.sleep(1)
    else:
        proc.kill()
        pytest.fail("gateway did not start")
    yield url, ollama, runbooks
    proc.terminate()
    proc.wait(timeout=10)


def watcher(url):
    return K8sWatcher(core_v1=FakeV1(), gateway=GatewayClient(url, timeout=120),
                      deduper=Deduper(cooldown=999))


def test_known_problem_answered_by_runbook_without_llm(stack):
    url, ollama, _ = stack
    w = watcher(url)
    out = w.handle_pod(make_pod(waiting=("ImagePullBackOff", "Back-off pulling image")))
    assert out[0]["result"]["model"] == "prefilter:runbook"
    assert out[0]["result"]["cost_usd"] == 0.0
    assert ollama.prompts == []


def test_unknown_problem_hits_llm_once_then_variants_are_free(stack):
    url, ollama, runbooks = stack
    w = watcher(url)
    cfg = ("CreateContainerConfigError", 'secret "db-creds" not found')

    first = w.handle_pod(make_pod(name="billing-7d9c4f6b9-x2vkd", waiting=cfg))[0]["result"]
    assert first["model"] == "agent"
    assert len(ollama.prompts) == 1
    assert "CreateContainerConfigError" in ollama.prompts[0]

    second = w.handle_pod(make_pod(name="billing-7d9c4f6b9-p9zrt", waiting=cfg))[0]["result"]
    assert second["model"] in ("cache", "prefilter:runbook")
    assert len(ollama.prompts) == 1


def test_same_pod_suppressed_by_cooldown(stack):
    url, _, _ = stack
    w = watcher(url)
    pod = make_pod(name="dup-7d9c4f6b9-aaaaa", waiting=("ImagePullBackOff", "x"))
    assert len(w.handle_pod(pod)) == 1
    assert w.handle_pod(pod) == []


def test_learned_runbook_does_not_swallow_unrelated_errors(stack):
    url, ollama, _ = stack
    before = len(ollama.prompts)
    r = httpx.post(f"{url}/v1/complete", timeout=120, json={
        "prompt": "pod web-1: Unschedulable: 0/3 nodes are available: insufficient cpu",
        "task_type": "triage"}).json()
    assert r["model"] == "agent", f"unrelated error was answered by {r['model']}: {r['response']}"
    assert len(ollama.prompts) == before + 1


def test_agent_does_not_modify_curated_runbooks(stack):
    _, _, runbooks = stack
    assert runbooks.read_text() == (ROOT / "runbooks.yaml").read_text()


def test_metrics_endpoint_reports_tiers(stack):
    url, _, _ = stack
    body = httpx.get(f"{url}/metrics").text
    for tier in ("prefilter", "cache", "agent"):
        assert f'gateway_requests_total{{task_type="triage",tier="{tier}"}}' in body, tier
    assert "gateway_request_latency_seconds_bucket" in body
    assert "gateway_llm_tokens_total" in body
