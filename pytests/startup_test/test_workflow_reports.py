"""报告通道静态回归；不把 Windows 检查当作 Linux bind mount 实测。"""

from pathlib import Path

import re
import shlex
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ".github/workflows/shutdown-linux.yml"
BASE = "3aecf0f8d082312a12df1757f38a1ed7df8a377a"
CASES = [
    ("posix", "Require all four real POSIX tests (zero skips)", 4),
    ("storage", "Real Host Kernel lifecycle and temporary storage roundtrip", 6),
    ("p1", "Require all fifteen P1 handoff regressions (zero skips)", 15),
    ("regression", "Require remaining graceful shutdown regressions (zero skips)", 27),
]


def script_for(text, name):
    # 这里只提取已知 run block；完整 YAML 另用标准 YAML parser 做静态验证。
    match = re.search(r"^      - name: " + re.escape(name) + r"\n        run: \|\n((?:          .*\n)+)", text, re.M)
    assert match, f"Missing report step: {name}"
    return "\n".join(line[10:] for line in match[1].splitlines())


@pytest.mark.parametrize("kind,name,count", CASES)
def test_report_mount_and_collection_preserve_isolation(kind, name, count):
    text = (ROOT / WORKFLOW).read_text(encoding="utf-8")
    script = script_for(text, name)
    assert f'report_dir="$(mktemp -d "$RUNNER_TEMP/shutdown-{kind}-reports.XXXXXX")"' in script
    commands = [shlex.split(line) for line in script.replace("\\\n", " ").splitlines()]
    run = next(cmd for cmd in commands if cmd[:2] == ["docker", "run"])
    assert run[run.index("--network") + 1] == "none"
    assert "--read-only" in run and run[run.index("--tmpfs") + 1] == "/tmp"
    assert run.count("--mount") == 1
    assert run[run.index("--mount") + 1] == "type=bind,source=$report_dir,target=/reports"
    assert not {"-v", "--volume", "--privileged"}.intersection(run)
    assert f"--junitxml=/reports/{kind}.xml" in run
    assert "docker cp" not in script and "--junitxml=/tmp/" not in script
    copy = ["cp", f"$report_dir/{kind}.xml", f"evidence/{kind}.xml"]
    check = ["python3", "-m", "pytests.startup_test.check_junit", f"evidence/{kind}.xml", str(count)]
    assert commands.index(run) < commands.index(copy) < commands.index(check)
    assert "|| test_exit=$?" in script
    assert commands[-1] == ["test", "$test_exit", "-eq", "0"]


def test_workflow_delta_only_adds_exact_clean_pr_regressions():
    """只增加两组 clean PR 回归；已验证的触发、隔离、六场景与报告门禁逐字不变。"""
    before = subprocess.check_output(["git", "show", f"{BASE}:{WORKFLOW}"], cwd=ROOT).decode("utf-8")
    after = (ROOT / WORKFLOW).read_text(encoding="utf-8")
    assert after.count('--mount "type=bind,source=$report_dir,target=/reports"') == 4
    for _, name, _ in CASES[2:]:
        script = script_for(after, name)
        if name == CASES[2][1]:
            assert "-k 'task_handoff or publication_boundaries'" in script
        else:
            assert (
                "-k 'not task_handoff and not publication_boundaries and not test_posix_real_runner_worker_shutdown'"
                in script
            )
        after = re.sub(
            r"^      - name: " + re.escape(name) + r"\n        run: \|\n(?:          .*\n)+", "", after, flags=re.M
        )
    assert after == before
