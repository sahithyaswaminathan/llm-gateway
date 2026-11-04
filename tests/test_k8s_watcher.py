from kubernetes import client as k

from app.ingest.k8s_watcher import Deduper, K8sWatcher, build_prompt, extract_problems


def make_pod(name="api-7d9c4f6b9-x2vkd", ns="prod", waiting=None, terminated=None,
             last_terminated=None, restarts=0, phase="Running", conditions=None):
    state = k.V1ContainerState(
        waiting=k.V1ContainerStateWaiting(reason=waiting[0], message=waiting[1]) if waiting else None,
        terminated=k.V1ContainerStateTerminated(
            reason=terminated[0], exit_code=terminated[1]) if terminated else None,
    )
    last = k.V1ContainerState(
        terminated=k.V1ContainerStateTerminated(
            reason=last_terminated[0], exit_code=last_terminated[1])) if last_terminated else None
    cs = k.V1ContainerStatus(
        name="app", image="x", image_id="x", ready=False,
        restart_count=restarts, state=state, last_state=last,
    )
    return k.V1Pod(
        metadata=k.V1ObjectMeta(name=name, namespace=ns),
        status=k.V1PodStatus(phase=phase, container_statuses=[cs], conditions=conditions),
    )


class FakeGateway:
    def __init__(self):
        self.prompts = []

    def submit(self, prompt, task_type="triage"):
        self.prompts.append(prompt)
        return {"model": "fake", "response": "ok"}


class FakeV1:
    def read_namespaced_pod_log(self, pod, ns, **kw):
        return "java.lang.OutOfMemoryError: heap\nstack line"


def test_healthy_pod_has_no_problems():
    assert extract_problems(make_pod()) == []


def test_crashloop_detected():
    p = extract_problems(make_pod(waiting=("CrashLoopBackOff", "back-off 5m"), restarts=7))
    assert len(p) == 1 and p[0].reason == "CrashLoopBackOff" and p[0].restarts == 7


def test_oomkilled_terminated_detected():
    p = extract_problems(make_pod(terminated=("OOMKilled", 137)))
    assert p[0].reason == "OOMKilled" and "137" in p[0].message


def test_oom_last_state_needs_restart_threshold():
    assert extract_problems(make_pod(last_terminated=("OOMKilled", 137), restarts=1)) == []
    assert len(extract_problems(make_pod(last_terminated=("OOMKilled", 137), restarts=5))) == 1


def test_unschedulable_pending_pod():
    cond = [k.V1PodCondition(type="PodScheduled", status="False", reason="Unschedulable",
                             message="0/3 nodes available: insufficient memory")]
    p = extract_problems(make_pod(phase="Pending", conditions=cond))
    assert p[0].reason == "Unschedulable"


def test_deduper_cooldown():
    now = [0.0]
    d = Deduper(cooldown=10, clock=lambda: now[0])
    assert d.allow(("a",)) and not d.allow(("a",))
    now[0] = 11
    assert d.allow(("a",))


def test_watcher_sends_once_per_cooldown_and_includes_logs():
    gw = FakeGateway()
    w = K8sWatcher(core_v1=FakeV1(), gateway=gw, deduper=Deduper(cooldown=999))
    pod = make_pod(waiting=("CrashLoopBackOff", "back-off"), restarts=4)
    assert len(w.handle_pod(pod)) == 1
    assert len(w.handle_pod(pod)) == 0
    assert "pod api-7d9c4f6b9-x2vkd: CrashLoopBackOff" in gw.prompts[0]
    assert "OutOfMemoryError" in gw.prompts[0]


def test_prompt_normalizes_to_same_signature_across_pods():
    from app.gateway.normalizer import signature

    a = extract_problems(make_pod(name="api-7d9c4f6b9-x2vkd", waiting=("CrashLoopBackOff", "")))[0]
    b = extract_problems(make_pod(name="api-7d9c4f6b9-p9zrt", waiting=("CrashLoopBackOff", "")))[0]
    assert signature(build_prompt(a)) == signature(build_prompt(b))
