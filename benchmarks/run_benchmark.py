import argparse
import json
import math
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = Path(__file__).with_name("cases.jsonl")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def load_cases(path):
    with path.open() as stream:
        cases = [json.loads(line) for line in stream if line.strip()]
    if not cases:
        raise ValueError(f"No benchmark cases found in {path}")
    for case in cases:
        if not {"id", "prompt", "variants", "expected_keywords"} <= case.keys():
            raise ValueError(f"Invalid benchmark case: {case.get('id', '<missing id>')}")
    return cases


def keyword_score(text, groups):
    text = text.lower()
    return sum(any(keyword.lower() in text for keyword in group) for group in groups) / len(groups)


def percentile(values, percent):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(percent * len(ordered)) - 1)], 2)


def metric_value(metrics_text, metric, direction):
    prefix = f'{metric}{{direction="{direction}"}} '
    for line in metrics_text.splitlines():
        if line.startswith(prefix):
            return float(line[len(prefix):])
    return 0.0


def start_gateway(model, ollama_url, workdir):
    port = free_port()
    env = {
        **os.environ,
        "VECTOR_DB_DIR": str(workdir / "chroma"),
        "RUNBOOKS_PATH": str(ROOT / "runbooks.yaml"),
        "OLLAMA_URL": ollama_url,
        "DEFAULT_MODEL": model,
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Gateway exited during startup. Install requirements and check local dependencies.")
        try:
            if httpx.get(f"{url}/health", timeout=2).status_code == 200:
                return process, url
        except httpx.HTTPError:
            time.sleep(1)
    process.terminate()
    raise TimeoutError("Gateway did not become healthy within 240 seconds")


def call_ollama(client, url, model, prompt):
    start = time.perf_counter()
    response = client.post(
        f"{url.rstrip('/')}/api/generate",
        json={"model": model, "prompt": prompt, "stream": False},
        timeout=600,
    )
    response.raise_for_status()
    data = response.json()
    return {
        "response": data.get("response", ""),
        "latency_ms": round((time.perf_counter() - start) * 1000, 2),
        "prompt_eval_count": data.get("prompt_eval_count"),
        "eval_count": data.get("eval_count"),
    }


def call_gateway(client, url, prompt, groups):
    start = time.perf_counter()
    response = client.post(
        f"{url}/v1/complete",
        json={"prompt": prompt, "task_type": "triage"},
        timeout=600,
    )
    response.raise_for_status()
    data = response.json()
    model = data.get("model", "unknown")
    tier = "prefilter" if model.startswith("prefilter:") else model
    answer = data.get("response", "")
    return {
        "model": model,
        "tier": tier,
        "latency_ms": round((time.perf_counter() - start) * 1000, 2),
        "keyword_score": keyword_score(answer, groups),
        "response": answer,
    }


def build_report(args, cases, baseline, gateway, model, token_delta):
    gateway_rows = [row for case in gateway for row in case["requests"]]
    baseline_latencies = [row["latency_ms"] for row in baseline]
    gateway_latencies = [row["latency_ms"] for row in gateway_rows]
    baseline_scores = [row["keyword_score"] for row in baseline]
    gateway_scores = [row["keyword_score"] for row in gateway_rows]
    tiers = {}
    for row in gateway_rows:
        tiers[row["tier"]] = tiers.get(row["tier"], 0) + 1
    avoided = sum(count for tier, count in tiers.items() if tier in {"prefilter", "cache"})
    total = len(gateway_rows)
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "dataset": str(args.dataset),
        "case_count": len(cases),
        "gateway_requests": total,
        "gateway_tiers": tiers,
        "llm_avoided_requests": avoided,
        "llm_avoided_fraction": round(avoided / total, 4) if total else 0,
        "latency_ms": {
            "direct_model_p50": percentile(baseline_latencies, 0.50),
            "direct_model_p95": percentile(baseline_latencies, 0.95),
            "gateway_p50": percentile(gateway_latencies, 0.50),
            "gateway_p95": percentile(gateway_latencies, 0.95),
        },
        "keyword_check": {
            "direct_model_mean": round(sum(baseline_scores) / len(baseline_scores), 4),
            "gateway_mean": round(sum(gateway_scores) / len(gateway_scores), 4),
            "description": "Fraction of expected keyword groups present; a screening signal, not an expert quality judgment.",
        },
        "tokens": {
            "direct_model_prompt_eval_count": sum(row["prompt_eval_count"] or 0 for row in baseline),
            "direct_model_eval_count": sum(row["eval_count"] or 0 for row in baseline),
            "gateway_llm_tokens_in_metric_delta": token_delta["in"],
            "gateway_llm_tokens_out_metric_delta": token_delta["out"],
            "caveat": "Direct counts are Ollama tokenizer counts. Gateway token metrics currently estimate tokens by whitespace word count; do not treat these totals as directly comparable.",
        },
        "baseline": baseline,
        "gateway": gateway,
        "limitations": [
            "Small hand-authored dataset; results do not establish general performance.",
            "Keyword matching is not a substitute for human review of resolution correctness and safety.",
            "The baseline sends the raw error to Ollama; gateway agent prompts contain additional instructions and parsed context.",
            "The gateway uses a fresh temporary Chroma database for this run; semantically similar cases can still match entries created earlier in the same run.",
            "Local-model latency depends on hardware, model warmup, and Ollama settings.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description="Compare direct Ollama calls with the gateway pipeline.")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--model", default=os.getenv("DEFAULT_MODEL", "qwen2.5:3b"))
    parser.add_argument("--ollama-url", default=os.getenv("BENCHMARK_OLLAMA_URL", "http://localhost:11434"))
    parser.add_argument("--output", type=Path, help="Write JSON results here; defaults to stdout.")
    args = parser.parse_args()
    cases = load_cases(args.dataset)

    with httpx.Client() as client:
        tags = client.get(f"{args.ollama_url.rstrip('/')}/api/tags", timeout=10)
        tags.raise_for_status()
        installed = {item["name"] for item in tags.json().get("models", [])}
        if args.model not in installed and f"{args.model}:latest" not in installed:
            raise SystemExit(f"Model {args.model!r} is not installed in Ollama. Run: ollama pull {args.model}")

        with tempfile.TemporaryDirectory(prefix="llm-gateway-benchmark-") as temp:
            process = None
            try:
                # Keep model startup/warmup outside measured direct baseline requests.
                call_ollama(client, args.ollama_url, args.model, "Reply with the word ready.")
                baseline = []
                for case in cases:
                    result = call_ollama(client, args.ollama_url, args.model, case["prompt"])
                    result.update({"case_id": case["id"], "keyword_score": keyword_score(result["response"], case["expected_keywords"])})
                    baseline.append(result)

                process, gateway_url = start_gateway(args.model, args.ollama_url, Path(temp))
                before = client.get(f"{gateway_url}/metrics", timeout=10).text
                gateway = []
                for case in cases:
                    prompts = [case["prompt"], *case["variants"]]
                    requests = [call_gateway(client, gateway_url, prompt, case["expected_keywords"]) for prompt in prompts]
                    gateway.append({"case_id": case["id"], "requests": requests})
                after = client.get(f"{gateway_url}/metrics", timeout=10).text
                token_delta = {
                    direction: round(metric_value(after, "gateway_llm_tokens_total", direction) - metric_value(before, "gateway_llm_tokens_total", direction))
                    for direction in ("in", "out")
                }
            finally:
                if process is not None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()

    report = build_report(args, cases, baseline, gateway, args.model, token_delta)
    rendered = json.dumps(report, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n")
        print(f"Wrote benchmark results to {args.output}")
    else:
        print(rendered)
    print(
        f"\nGateway tiers: {report['gateway_tiers']} | "
        f"LLM avoided: {report['llm_avoided_fraction']:.1%} | "
        f"gateway p50/p95: {report['latency_ms']['gateway_p50']}/"
        f"{report['latency_ms']['gateway_p95']} ms"
    )


if __name__ == "__main__":
    main()
