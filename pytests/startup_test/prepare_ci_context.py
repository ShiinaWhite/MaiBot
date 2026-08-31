"""从固定 Git 对象构建白名单 context，绝不复制工作树的 config/data/secrets。"""

from pathlib import Path, PurePosixPath

import argparse
import hashlib
import io
import json
import subprocess
import tarfile

RUNTIME_COMMIT = "d5e93c90bb49b8bba10c403d8516ff294055cbe2"
CLEAN_PR_HEAD = "48e0db4ca879cc551ccfd396b0b869c2347e8838"
EVIDENCE_BASE = "3aecf0f8d082312a12df1757f38a1ed7df8a377a"
RUNTIME_FILES = ("bot.py", "src/common/process_runner.py", "src/common/shutdown.py", "docker-compose.yml")
PR_TEST_FILES = (
    "pytests/startup_test/shutdown_fixture.py",
    "pytests/startup_test/test_graceful_shutdown.py",
    "pytests/startup_test/memory_shutdown_fixture.py",
    "pytests/startup_test/README.md",
    "pytests/A_memorix_test/test_host_shutdown_failure_propagation.py",
)
IDENTITY = json.loads(Path(__file__).with_name("clean-pr-source.json").read_text(encoding="utf-8"))
TEST_PREFIXES = ("pytests/startup_test/",)
STORAGE_TESTS = (
    "pytests/A_memorix_test/test_host_shutdown_failure_propagation.py",
    "pytests/A_memorix_test/test_shutdown_real_storage.py",
)


def git(*args: str) -> bytes:
    # git archive 也可能应用宿主 core.autocrlf；只覆盖本次调用，不改仓库配置。
    return subprocess.check_output(["git", "-c", "core.autocrlf=false", "-c", "core.eol=lf", *args])


def prepare(target: Path) -> dict:
    target.mkdir(parents=True, exist_ok=False)
    tooling = git("rev-parse", "HEAD").decode().strip()
    git("merge-base", "--is-ancestor", EVIDENCE_BASE, tooling)
    changed = git("diff", "--name-only", EVIDENCE_BASE, tooling).decode().splitlines()
    allowed = (*TEST_PREFIXES, "pytests/A_memorix_test/")
    assert all(
        p.startswith(allowed) or p in (*RUNTIME_FILES, ".github/workflows/shutdown-linux.yml") for p in changed
    ), "Not validation-only"
    assert IDENTITY["clean_pr_head"] == CLEAN_PR_HEAD and IDENTITY["runtime_commit"] == RUNTIME_COMMIT
    hashes = IDENTITY["sha256"]
    assert set(hashes) == set(RUNTIME_FILES + PR_TEST_FILES), "Incomplete clean PR identity"
    # 哈希由本地 clean PR Git 对象生成；不要求把 clean PR 分支推到 fork。
    # 在任何文件进入 context 前拒绝 runtime 或 regression 的一个字节漂移。
    for path, expected in hashes.items():
        actual = git("show", f"{tooling}:{path}")
        assert hashlib.sha256(actual).hexdigest() == expected, f"Clean PR source changed: {path}"
    # 仅读取 validation Git 对象白名单，不含 .git、忽略文件或 checkout 凭据。
    sources = [
        (tooling, ["bot.py", "src", "locales", "pyproject.toml", "uv.lock", "docker-compose.yml"]),
        (tooling, [*TEST_PREFIXES, *STORAGE_TESTS]),
    ]
    for ref, paths in sources:
        with tarfile.open(fileobj=io.BytesIO(git("archive", ref, "--", *paths))) as archive:
            for member in archive:
                relative = PurePosixPath(member.name)
                assert not relative.is_absolute() and ".." not in relative.parts
                if member.isdir():
                    continue
                assert member.isfile(), "Only regular files may enter CI context"
                destination = target.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.extractfile(member).read())
    for path, expected in hashes.items():
        assert hashlib.sha256((target / path).read_bytes()).hexdigest() == expected, f"Archive changed source: {path}"
    manifest = {
        "clean_pr_head": CLEAN_PR_HEAD,
        "runtime_commit": RUNTIME_COMMIT,
        "tooling_commit": tooling,
        "runtime_sha256": {p: hashes[p] for p in RUNTIME_FILES},
        "regression_sha256": {p: hashes[p] for p in PR_TEST_FILES},
    }
    (target / "source-provenance.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    print(json.dumps(prepare(parser.parse_args().destination), indent=2))
