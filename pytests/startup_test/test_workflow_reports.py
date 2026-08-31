"""报告通道静态回归；不把 Windows 检查当作 Linux bind mount 实测。"""

from pathlib import Path

import re
import shlex
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ".github/workflows/shutdown-linux.yml"
BASE = "c62dcdbc0ffd75d46a823d0308f6b967fb4b75a3"
CASES = [
    ("posix", "Require all four real POSIX tests (zero skips)", 4),
    ("storage", "Real Host Kernel lifecycle and temporary storage roundtrip", 6),
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


def test_workflow_delta_is_only_report_transport():
    """只允许这一轮报告通道替换；触发、权限、六场景和所有门禁保持原样。"""
    before = subprocess.check_output(["git", "show", f"{BASE}:{WORKFLOW}"], cwd=ROOT).decode("utf-8")
    after = (ROOT / WORKFLOW).read_text(encoding="utf-8")
    assert after.count('--mount "type=bind,source=$report_dir,target=/reports"') == 2
    for kind, _, _ in CASES:
        after = after.replace(
            f'          report_dir="$(mktemp -d "$RUNNER_TEMP/shutdown-{kind}-reports.XXXXXX")"\n', ""
        )
        after = after.replace(f"--junitxml=/reports/{kind}.xml", f"--junitxml=/tmp/{kind}.xml")
        container = "shutdown-posix" if kind == "posix" else "shutdown-storage-test"
        after = after.replace(
            f'cp "$report_dir/{kind}.xml" evidence/{kind}.xml',
            f"docker cp {container}:/tmp/{kind}.xml evidence/{kind}.xml",
        )
    after = after.replace('            --mount "type=bind,source=$report_dir,target=/reports" \\\n', "")
    assert after == before
