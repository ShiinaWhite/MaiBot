"""仅一次性 Linux Docker host：双拓扑验收，保留白名单证据，不连接业务服务。"""

from pathlib import Path

import argparse
import json
import os
import subprocess
import time
import uuid

from pytests.startup_test.shutdown_observer import PREFIX

EVENTS = {
    "runner_started",
    "worker_spawned",
    "worker_ready",
    "runner_signal_received",
    "runner_signal_forwarding",
    "worker_signal_received",
    "startup.shutdown_started",
    "memory_stop",
    "memory_failure",
    "persist",
    "metadata_close",
    "writer_lock_release",
    "memory_stop_completed",
    "startup.shutdown_completed",
    "worker_exit_requested",
    "runner_worker_reaped",
    "runner_force_kill",
    "runner_exit",
}
FIELDS = {"event", "pid", "ppid", "ns", "child_pid", "code", "signum", "ignores_stop"}


def parse_events(raw: str) -> list[dict]:
    result = []
    for line in raw.splitlines():
        if not line.startswith(PREFIX):
            continue  # 原始日志不进入报告或 artifact。
        record = json.loads(line[len(PREFIX) :])
        assert record["event"] in EVENTS and set(record) <= FIELDS, "Unexpected event schema"
        assert all(isinstance(v, (int, bool)) for k, v in record.items() if k != "event"), "Non-numeric field"
        assert all(k in record for k in ("pid", "ppid", "ns")), "Missing event identity"
        result.append(record)
    return sorted(result, key=lambda e: e["ns"])


def one(events: list[dict], name: str) -> dict:
    matches = [e for e in events if e["event"] == name]
    assert len(matches) == 1, f"Expected exactly one {name}, found {len(matches)}"
    return matches[0]


def validate_init_config(host_config: dict, init: bool) -> None:
    # 缺省/null 没有明确的配置证据；最终仍须通过 validate() 的真实进程树断言。
    configured_init = host_config.get("Init")
    assert configured_init is None or configured_init is init, "Explicit Init conflicts with scenario"


def validate(
    events: list[dict],
    *,
    scenario: str,
    init: bool,
    state: dict,
    elapsed: float,
    topology: dict,
    alive_during_cleanup: bool,
) -> dict:
    expected_code = 0 if scenario == "normal" else 1
    assert state["Status"] == "exited" and not state["Running"] and not state["OOMKilled"]
    assert state["ExitCode"] == expected_code
    assert 0 < elapsed < 70, "Docker grace period exceeded"
    runner, worker = one(events, "runner_started"), one(events, "worker_ready")
    assert worker["ppid"] == runner["pid"] == one(events, "worker_spawned")["pid"]
    assert one(events, "worker_spawned")["child_pid"] == worker["pid"]
    assert topology["runner_ppid"] == runner["ppid"] and topology["worker_ppid"] == runner["pid"]
    if init:
        assert runner["pid"] != 1 and runner["ppid"] == 1
        assert topology["pid1_comm"] == "docker-init"
    else:
        assert runner["pid"] == 1
    received, forwarded = one(events, "runner_signal_received"), one(events, "runner_signal_forwarding")
    assert received["signum"] == forwarded["signum"] == 15
    assert received["pid"] == forwarded["pid"] == runner["pid"]
    assert forwarded["child_pid"] == worker["pid"]
    reaped, exited = one(events, "runner_worker_reaped"), one(events, "runner_exit")
    assert reaped["pid"] == exited["pid"] == runner["pid"] and reaped["child_pid"] == worker["pid"]
    assert exited["code"] == expected_code
    chain = ["worker_ready", "runner_signal_received", "runner_signal_forwarding"]
    if scenario == "timeout":
        killed = one(events, "runner_force_kill")
        assert worker["ignores_stop"] and killed["pid"] == runner["pid"] and killed["child_pid"] == worker["pid"]
        assert 60 <= (killed["ns"] - received["ns"]) / 1e9 < 69
        assert reaped["code"] == -9  # Runner 回收被自身 SIGKILL 的 Worker，而非 Docker 杀死 Runner。
        assert not any(
            e["event"]
            in {
                "worker_signal_received",
                "startup.shutdown_started",
                "memory_stop",
                "startup.shutdown_completed",
                "worker_exit_requested",
            }
            for e in events
        )
        chain += ["runner_force_kill"]
    else:
        assert alive_during_cleanup, "Runner/Worker survival was not observed during cleanup"
        assert not any(e["event"] == "runner_force_kill" for e in events)
        assert one(events, "worker_signal_received")["signum"] == 15
        chain += ["worker_signal_received", "startup.shutdown_started", "memory_stop"]
        if scenario == "normal":
            assert not any(e["event"] == "memory_failure" for e in events)
            chain += [
                "persist",
                "metadata_close",
                "writer_lock_release",
                "memory_stop_completed",
                "startup.shutdown_completed",
            ]
        else:
            chain += ["memory_failure"]
            assert not any(
                e["event"]
                in {
                    "persist",
                    "metadata_close",
                    "writer_lock_release",
                    "memory_stop_completed",
                    "startup.shutdown_completed",
                }
                for e in events
            )
        leaving = one(events, "worker_exit_requested")
        assert leaving["code"] == reaped["code"] == expected_code
        assert leaving["ns"] - one(events, "memory_stop")["ns"] >= 2_500_000_000
        chain += ["worker_exit_requested"]
        for name in chain[3:]:
            assert one(events, name)["pid"] == worker["pid"], f"Wrong Worker identity: {name}"
    chain += ["runner_worker_reaped", "runner_exit"]
    times = [one(events, name)["ns"] for name in chain]
    assert times == sorted(times), "Lifecycle event order violated"
    return {
        "scenario": scenario,
        "init": init,
        "exit_code": expected_code,
        "elapsed_seconds": elapsed,
        "signal_chain": "passed",
        "memory_evidence": "fixture callbacks, not real storage",
    }


def docker(*args: str, timeout: float = 15) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, encoding="utf-8")
    if result.returncode:
        raise RuntimeError(f"Docker {args[0]} failed (exit {result.returncode}); raw output withheld")
    return result.stdout + result.stderr if args[0] == "logs" else result.stdout


def snapshot(container: str, runner_pid: int, worker_pid: int) -> dict:
    # 仅容器内 /proc 的无秘密进程字段；没有发送信号/attach。
    script = """import json, pathlib, sys
def p(pid):
    rows = dict(line.split(':',1) for line in pathlib.Path('/proc/'+pid+'/status').read_text().splitlines())
    return int(rows['PPid']), rows['State'].strip().split()[0]
r, w = p(sys.argv[1]), p(sys.argv[2])
print(json.dumps({'pid1_comm':pathlib.Path('/proc/1/comm').read_text().strip(),
                  'runner_ppid':r[0], 'worker_ppid':w[0], 'alive':r[1] not in 'ZX' and w[1] not in 'ZX'}))
"""
    return json.loads(docker("exec", container, "python", "-c", script, str(runner_pid), str(worker_pid)))


def run_case(image: str, scenario: str, init: bool, output: Path) -> dict:
    name = f"shutdown-{scenario}-{'init' if init else 'pid1'}-{uuid.uuid4().hex[:10]}"
    flags = ["--fail"] if scenario == "failure" else ["--ignore-stop"] if scenario == "timeout" else []
    docker(
        "run",
        "-d",
        "--name",
        name,
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/tmp",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--stop-timeout",
        "70",
        *(["--init"] if init else []),
        image,
        "--observe",
        *flags,
    )
    stop = None
    result = {"scenario": scenario, "init": init, "result": "FAIL"}
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            events = parse_events(docker("logs", name))
            if any(e["event"] == "worker_ready" for e in events):
                break
            time.sleep(0.1)
        else:
            raise AssertionError("Worker readiness deadline exceeded")
        runner, worker = one(events, "runner_started"), one(events, "worker_ready")
        topology = snapshot(name, runner["pid"], worker["pid"])
        assert topology["alive"]
        info = json.loads(docker("inspect", name))[0]
        validate_init_config(info["HostConfig"], init)
        assert info["HostConfig"]["NetworkMode"] == "none"
        assert all(m["Type"] == "tmpfs" and m["Destination"] == "/tmp" for m in info["Mounts"])
        assert not info["HostConfig"]["PortBindings"] and not info["HostConfig"].get("Binds")
        assert info["Config"].get("StopSignal", "SIGTERM") in ("", "SIGTERM", "15")
        started = time.monotonic()
        stop = subprocess.Popen(
            ["docker", "stop", "--timeout", "70", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        alive = False
        while stop.poll() is None and time.monotonic() - started < 75:
            if scenario != "timeout" and not alive:
                observed = parse_events(docker("logs", name))
                if any(e["event"] == "memory_stop" for e in observed):
                    alive = snapshot(name, runner["pid"], worker["pid"])["alive"]
            time.sleep(0.1)
        assert stop.wait(timeout=2) == 0, "docker stop command failed"
        elapsed = time.monotonic() - started
        events = parse_events(docker("logs", name))
        state = json.loads(docker("inspect", name))[0]["State"]
        result.update(
            validate(
                events,
                scenario=scenario,
                init=init,
                state=state,
                elapsed=elapsed,
                topology=topology,
                alive_during_cleanup=alive,
            )
        )
        result["result"] = "PASS"
        return result
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        raise
    finally:
        # 只保存固定字段的合成证据，不保存 docker inspect 的 Env 或原始业务日志。
        try:
            evidence = parse_events(docker("logs", name))
        except Exception as evidence_error:
            evidence = []
            result["evidence_error_type"] = type(evidence_error).__name__
            if result["result"] == "PASS":
                raise  # 不允许成功运行却静默丢失证据。
        (output / f"{scenario}-{'init' if init else 'pid1'}.json").write_text(
            json.dumps({"summary": result, "events": evidence}, indent=2) + "\n", encoding="utf-8"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if os.name != "posix" or not Path("/proc").exists():
        raise SystemExit("This acceptance driver requires a real Linux Docker host")
    args.output.mkdir(parents=True, exist_ok=True)
    for scenario in ("normal", "failure", "timeout"):
        for init in (False, True):
            result = run_case(args.image, scenario, init, args.output)
            print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
