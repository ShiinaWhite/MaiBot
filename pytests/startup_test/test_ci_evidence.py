"""验证 CI 判定器自身；这些合成记录不是 POSIX/Docker 实测结果。"""

from copy import deepcopy

import json
import io
import os
import signal
import subprocess
import sys
import tarfile

import pytest

from pytests.startup_test.check_junit import check
from pytests.startup_test.docker_acceptance import parse_events, validate, validate_init_config
from pytests.startup_test.prepare_ci_context import RUNTIME_COMMIT, RUNTIME_FILES
from pytests.startup_test.shutdown_fixture import ROOT
from pytests.startup_test.shutdown_observer import PREFIX


def trace(scenario="normal", init=False):
    runner, worker = (7 if init else 1), 8
    events = []

    def add(name, pid=runner, seconds=0, **fields):
        events.append(
            {
                "event": name,
                "pid": pid,
                "ppid": runner if pid == worker else 1 if init else 0,
                "ns": int(seconds * 1e9),
                **fields,
            }
        )

    add("runner_started")
    add("worker_spawned", child_pid=worker)
    add("worker_ready", worker, ignores_stop=scenario == "timeout")
    add("runner_signal_received", seconds=1, signum=15)
    add("runner_signal_forwarding", seconds=1.1, child_pid=worker, signum=15)
    code = 0 if scenario == "normal" else 1
    if scenario == "timeout":
        add("runner_force_kill", seconds=61.2, child_pid=worker)
        add("runner_worker_reaped", seconds=61.3, child_pid=worker, code=-9)
        add("runner_exit", seconds=61.4, code=1)
    else:
        add("worker_signal_received", worker, seconds=1.2, signum=15)
        add("startup.shutdown_started", worker, seconds=1.3)
        add("memory_stop", worker, seconds=1.4)
        names = (
            ["persist", "metadata_close", "writer_lock_release", "memory_stop_completed", "startup.shutdown_completed"]
            if scenario == "normal"
            else ["memory_failure"]
        )
        for i, name in enumerate(names):
            add(name, worker, seconds=4.4 + i * 0.01)
        add("worker_exit_requested", worker, seconds=4.6, code=code)
        add("runner_worker_reaped", seconds=4.7, child_pid=worker, code=code)
        add("runner_exit", seconds=4.8, code=code)
    args = {
        "scenario": scenario,
        "init": init,
        "state": {"Status": "exited", "Running": False, "OOMKilled": False, "ExitCode": code},
        "elapsed": 60.5 if scenario == "timeout" else 4.0,
        "topology": {"runner_ppid": 1 if init else 0, "worker_ppid": runner, "pid1_comm": "docker-init"},
        "alive_during_cleanup": scenario != "timeout",
    }
    return events, args


@pytest.mark.parametrize("scenario", ["normal", "failure", "timeout"])
@pytest.mark.parametrize("init", [False, True])
def test_expected_trace_is_accepted(scenario, init):
    events, args = trace(scenario, init)
    assert validate(events, **args)["exit_code"] == args["state"]["ExitCode"]


@pytest.mark.parametrize("init", [False, True], ids=["pid1", "docker-init"])
@pytest.mark.parametrize(
    "host_config", [{}, {"Init": None}, {"Init": False}, {"Init": True}], ids=["missing", "null", "false", "true"]
)
def test_init_config_accepts_unspecified_or_matching_value(host_config, init):
    configured_init = host_config.get("Init")
    if configured_init is not None and configured_init is not init:
        with pytest.raises(AssertionError, match="Explicit Init conflicts"):
            validate_init_config(host_config, init)
    else:
        validate_init_config(host_config, init)
        events, args = trace(init=init)
        assert validate(events, **args)["init"] is init


@pytest.mark.parametrize("init", [False, True], ids=["pid1", "docker-init"])
@pytest.mark.parametrize("config_kind", ["missing", "null", "matching"])
@pytest.mark.parametrize("fault", ["opposite_topology", "runner_parent", "worker_parent"])
def test_init_config_never_replaces_real_process_tree(init, config_kind, fault):
    host_config = {} if config_kind == "missing" else {"Init": None if config_kind == "null" else init}
    validate_init_config(host_config, init)
    events, args = trace(init=not init if fault == "opposite_topology" else init)
    args["init"] = init
    if fault == "runner_parent":
        args["topology"]["runner_ppid"] = 99
    elif fault == "worker_parent":
        args["topology"]["worker_ppid"] = 99
    with pytest.raises(AssertionError):
        validate(events, **args)


@pytest.mark.parametrize("host_config", [{}, {"Init": None}, {"Init": True}], ids=["missing", "null", "true"])
def test_init_scenario_requires_actual_docker_init(host_config):
    validate_init_config(host_config, True)
    events, args = trace(init=True)
    args["topology"]["pid1_comm"] = "python"
    with pytest.raises(AssertionError):
        validate(events, **args)


@pytest.mark.parametrize(
    "missing",
    [
        "runner_signal_received",
        "worker_signal_received",
        "persist",
        "metadata_close",
        "memory_stop_completed",
        "startup.shutdown_completed",
        "runner_worker_reaped",
    ],
)
def test_exit_zero_is_not_sufficient(missing):
    events, args = trace()
    with pytest.raises(AssertionError):
        validate([e for e in events if e["event"] != missing], **args)


@pytest.mark.parametrize("fault", ["duplicate", "wrong_order", "no_survival", "oom", "wrong_topology", "late_exit"])
def test_invalid_trace_is_rejected(fault):
    events, args = trace(init=True)
    if fault == "duplicate":
        events.append(deepcopy(events[-1]))
    elif fault == "wrong_order":
        next(e for e in events if e["event"] == "persist")["ns"] = 9_000_000_000
    elif fault == "no_survival":
        args["alive_during_cleanup"] = False
    elif fault == "oom":
        args["state"]["OOMKilled"] = True
    elif fault == "wrong_topology":
        args["topology"]["pid1_comm"] = "python"
    else:
        args["elapsed"] = 71
    with pytest.raises(AssertionError):
        validate(events, **args)


def test_timeout_cannot_be_shortened_or_replaced_by_docker_kill():
    events, args = trace("timeout")
    next(e for e in events if e["event"] == "runner_force_kill")["ns"] = 5_000_000_000
    with pytest.raises(AssertionError):
        validate(events, **args)
    events, args = trace("timeout")
    args["state"]["ExitCode"] = 137
    with pytest.raises(AssertionError):
        validate(events, **args)


def test_parser_does_not_export_arbitrary_logs_or_fields():
    events, _ = trace()
    raw = "unrelated fixture output\n" + "\n".join(PREFIX + json.dumps(e) for e in events)
    assert parse_events(raw) == events
    events[0]["unexpected"] = "never export arbitrary strings"
    with pytest.raises(AssertionError):
        parse_events(PREFIX + json.dumps(events[0]))


@pytest.mark.parametrize("tag", ["skipped", "failure", "error"])
def test_junit_rejects_skipped_or_failed_tests(tmp_path, tag):
    report = tmp_path / "result.xml"
    report.write_text(f"<testsuites><testsuite><testcase><{tag}/></testcase></testsuite></testsuites>")
    with pytest.raises(AssertionError):
        check(str(report), 1)


def test_junit_requires_exact_count(tmp_path):
    report = tmp_path / "result.xml"
    report.write_text("<testsuites><testsuite><testcase/></testsuite></testsuites>")
    check(str(report), 1)
    with pytest.raises(AssertionError):
        check(str(report), 4)


def test_runtime_sources_are_unchanged():
    for name in RUNTIME_FILES:
        expected = subprocess.check_output(["git", "show", f"{RUNTIME_COMMIT}:{name}"], cwd=ROOT)
        actual = (ROOT / name).read_bytes().replace(b"\r\n", b"\n")
        assert actual == expected


def test_real_git_archive_preserves_runtime_bytes_on_this_host():
    from pytests.startup_test.prepare_ci_context import git

    with tarfile.open(fileobj=io.BytesIO(git("archive", RUNTIME_COMMIT, "--", *RUNTIME_FILES))) as archive:
        for name in RUNTIME_FILES:
            assert archive.extractfile(name).read() == git("show", f"{RUNTIME_COMMIT}:{name}")


def context_git(monkeypatch, changed=(), bad_runtime=None):
    from pytests.startup_test import prepare_ci_context as context

    tooling = "c" * 40
    calls = []
    source = {name: ("fixed " + name).encode() for name in RUNTIME_FILES}
    source.update({"locales/en.json": b"{}", "pyproject.toml": b"[project]", "uv.lock": b"version=1"})
    tests = {"pytests/startup_test/shutdown_fixture.py": b"# tooling"}
    tests.update({name: b"# storage test" for name in context.STORAGE_TESTS})

    def git(*args):
        calls.append(args)
        if args == ("rev-parse", "HEAD"):
            return tooling.encode()
        if args == ("merge-base", "--is-ancestor", RUNTIME_COMMIT, tooling):
            return b""
        if args == ("diff", "--name-only", RUNTIME_COMMIT, tooling):
            return "\n".join(changed).encode()
        if args[0] == "show":
            ref, path = args[1].split(":", 1)
            return b"altered runtime" if ref == tooling and path == bad_runtime else source[path]
        if args[0] == "archive":
            if args[1] == RUNTIME_COMMIT:
                assert args[2:] == ("--", "bot.py", "src", "locales", "pyproject.toml", "uv.lock")
                members = source
            else:
                assert args == ("archive", tooling, "--", *context.TEST_PREFIXES, *context.STORAGE_TESTS)
                members = tests
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode="w") as archive:
                for name, data in members.items():
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
            return stream.getvalue()
        raise AssertionError("Unexpected Git query")

    monkeypatch.setattr(context, "git", git)
    return context, calls, source | tests


@pytest.mark.parametrize("path", ["src/change.py", "config/example.toml", "data/example.db", "unrelated.txt"])
def test_context_rejects_non_test_changes_before_copy(tmp_path, monkeypatch, path):
    context, calls, _ = context_git(monkeypatch, changed=[path])
    with pytest.raises(AssertionError, match="Not test-only"):
        context.prepare(tmp_path / "context")
    assert not any(call[0] == "archive" for call in calls)
    assert not list((tmp_path / "context").rglob("*"))


@pytest.mark.parametrize("path", RUNTIME_FILES)
def test_context_rejects_changed_runtime_before_copy(tmp_path, monkeypatch, path):
    context, calls, _ = context_git(monkeypatch, bad_runtime=path)
    with pytest.raises(AssertionError, match="Runtime source changed"):
        context.prepare(tmp_path / "context")
    assert not any(call[0] == "archive" for call in calls)


def test_context_contains_only_git_allowlist(tmp_path, monkeypatch):
    context, _, files = context_git(monkeypatch, changed=[".github/workflows/shutdown-linux.yml"])
    destination = tmp_path / "context"
    result = context.prepare(destination)
    assert result["runtime_commit"] == RUNTIME_COMMIT and result["tooling_commit"] == "c" * 40
    actual = {p.relative_to(destination).as_posix() for p in destination.rglob("*") if p.is_file()}
    assert actual == set(files) | {"source-provenance.json"}
    assert all((destination / name).read_bytes() == data for name, data in files.items())
    assert not any((destination / name).exists() for name in ("config", "data", "secrets", ".git", ".env"))


def test_observer_delegates_signals_and_wait_without_counting_kill_as_stop(monkeypatch):
    from pytests.startup_test import shutdown_observer as observer
    from src.common import process_runner

    events, calls = [], []

    def initialize(process, *args, **kwargs):
        process.pid, process.returncode = 123, None

    def send(process, sig):
        calls.append(("signal", sig))

    def kill(process):
        # POSIX Popen.kill 的实际分派形态；不向 Windows 进程发送 POSIX 信号。
        process.send_signal(signal.SIGKILL)

    def wait(process, *args, **kwargs):
        calls.append(("wait", kwargs.get("timeout")))
        process.returncode = -9
        return -9

    monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(subprocess.Popen, "__init__", initialize)
    monkeypatch.setattr(subprocess.Popen, "send_signal", send)
    monkeypatch.setattr(subprocess.Popen, "kill", kill)
    monkeypatch.setattr(subprocess.Popen, "wait", wait)
    monkeypatch.setattr(subprocess.Popen, "poll", lambda p: p.returncode)
    monkeypatch.setattr(observer, "emit", lambda event, **fields: events.append(event))
    with observer.observe_runner():
        process = process_runner.subprocess.Popen(["fixture"])
        process.send_signal(signal.SIGTERM)
        process.kill()
        process.wait(timeout=5)
        process.poll()
    assert calls == [("signal", signal.SIGTERM), ("signal", 9), ("wait", 5)]
    assert events == ["worker_spawned", "runner_signal_forwarding", "runner_force_kill", "runner_worker_reaped"]


@pytest.mark.parametrize("fail", [False, True])
def test_observed_worker_still_executes_real_entrypoint(fail):
    args = [sys.executable, "-u", "-m", "pytests.startup_test.shutdown_fixture", "--worker", "--observe", "--self-stop"]
    if fail:
        args.append("--fail")
    result = subprocess.run(
        args,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=12,
        env=os.environ | {"PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == (1 if fail else 0), result.stderr
    events = parse_events(result.stdout)
    names = [e["event"] for e in events]
    assert names.count("memory_stop") == 1
    assert ("startup.shutdown_completed" in names) is (not fail)
    assert names[-1] == "worker_exit_requested"
