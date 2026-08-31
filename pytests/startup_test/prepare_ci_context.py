"""从固定 Git 对象构建白名单 context，绝不复制工作树的 config/data/secrets。"""

from pathlib import Path, PurePosixPath

import argparse
import hashlib
import io
import json
import subprocess
import tarfile

RUNTIME_COMMIT = "b4c098c9739b633bc8bd9d3c9e3ada6767b28957"
RUNTIME_FILES = ("bot.py", "src/common/process_runner.py", "src/common/shutdown.py")
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
    git("merge-base", "--is-ancestor", RUNTIME_COMMIT, tooling)
    changed = git("diff", "--name-only", RUNTIME_COMMIT, tooling).decode().splitlines()
    allowed = (*TEST_PREFIXES, "pytests/A_memorix_test/")
    assert all(p.startswith(allowed) or p == ".github/workflows/shutdown-linux.yml" for p in changed), "Not test-only"
    hashes = {}
    for path in RUNTIME_FILES:
        base = git("show", f"{RUNTIME_COMMIT}:{path}")
        assert base == git("show", f"{tooling}:{path}"), f"Runtime source changed: {path}"
        hashes[path] = hashlib.sha256(base).hexdigest()
    # 固定源码与测试工具分别从 Git 对象读取，不含 .git、忽略文件或 checkout 凭据。
    sources = [
        (RUNTIME_COMMIT, ["bot.py", "src", "locales", "pyproject.toml", "uv.lock"]),
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
        assert hashlib.sha256((target / path).read_bytes()).hexdigest() == expected, f"Archive changed runtime: {path}"
    manifest = {"runtime_commit": RUNTIME_COMMIT, "tooling_commit": tooling, "runtime_sha256": hashes}
    (target / "source-provenance.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    print(json.dumps(prepare(parser.parse_args().destination), indent=2))
