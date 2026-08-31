# Docker / SIGTERM shutdown regression tests

These fixtures load the real lifecycle functions and Worker entry block from
`bot.py` using AST, bypassing its top-level configuration, migration, logging and
update-check side effects. Business services are fakes. They do **not** open a
production database, read model/QQ credentials or send messages. Uvicorn's real
`Server.serve()` / signal context is used with a socket-free `_serve()` fixture.

## Tests

From the repository root, with the project's development dependencies available:

```sh
python -m pytest pytests/startup_test/test_graceful_shutdown.py -q
python -m pytest pytests/A_memorix_test/test_runtime_lifecycle_boundaries.py -q
python -m pytest pytests/A_memorix_test/test_host_shutdown_failure_propagation.py -q
```

The Worker-entry subprocess tests run on Windows and Linux. On Windows they use
Python `signal.raise_signal`, **not** `os.kill(..., SIGTERM)` (which terminates a
Windows process without exercising Python's handler). Runner forwarding is unit
tested with fake processes. The four real Runner-to-Worker signal tests require
POSIX and are explicitly skipped on Windows (normal/early startup, successful/
failed cleanup). A Windows CTRL_BREAK delivery test
requires a console and is not claimed by these unit tests.

## GitHub Actions 隔离验收（执行前须另行批准 push / Actions）

被测运行代码固定为 `b4c098c9739b633bc8bd9d3c9e3ada6767b28957`；CI/test
工具版本为触发 workflow 的完整 `github.sha`。`prepare_ci_context.py` 分别从这两个
Git 对象创建白名单构建 context，并检查运行文件没有变化。不会复制工作区的
config、data、secret、`.git`、凭据或本地未提交 Prompt。

`.github/workflows/shutdown-linux.yml` 只响应 `fix/graceful-docker-shutdown`
分支的 push，使用 Ubuntu 24.04、`contents: read` 和 25 分钟 job 总时限。
没有 schedule、workflow_dispatch、PR、release/deploy 或镜像推送；不用 repository
secrets。构建阶段需要公开镜像/依赖下载，所有测试容器均 `--network none`、只读根文件
系统及临时 `/tmp`，不挂 Docker socket、不发布端口。Docker CLI 只在隔离 runner 上运行。
Artifacts 只保留环境版本、源码身份、固定字段合成事件和测试结果，7 天过期。

验收分三层，不能相互冒充：

1. **真实 Linux/Docker 信号链**：AST 提取的实际 Worker 入口、真实 Runner 和 Uvicorn
   signal context；业务服务为 fixture。`shutdown_observer.py` 仅包装并委托真实的
   signal/Popen/wait，不替换 Runner 或缩短 60 秒超时。
2. **真实 Host → Kernel 关闭控制流**：既有 `test_host_shutdown_failure_propagation.py`
   调用真实 Host.stop / Kernel.shutdown / lifecycle，存储替身用于失败传播断言。
3. **真实临时存储集成**：`test_shutdown_real_storage.py` 在临时目录写入合成段落、
   三维向量和双节点图，调用真实 Host/Kernel shutdown、`_persist()`、MetadataStore.close，
   重新打开并验证 SQLite integrity、段落/关系、向量值和图节点/边，验证旧连接已关闭、
   writer lock 可再次获取。分别注入 persist 失败与 close 后报错，确认异常不被吞掉。
   不调用 Host.start / Kernel.initialize；无模型、QQ、实际业务数据。

`storage_isolation.py` 在测试收集前阻断配置、消息、模型客户端等无关宿主导入，
保留真实 A_Memorix 实现。运行它而非直接加载测试，避免宿主顶层导入初始化数据库。
Python 网络调用默认失败；只允许 Windows asyncio 的标准库自唤醒 socketpair。

### 必须全部通过的场景

| 场景 | A：Runner PID1 | B：`--init`（生产等价进程拓扑） |
| --- | --- | --- |
| 正常停止 | exit 0 + 完整有序事件 | 同左，额外确认 PID1=docker-init、Runner PPid=1 |
| A_Memorix fixture stop 抛错 | Worker/Runner exit 1、无 completed | 同左 |
| Worker 忽略 SIGTERM | 等待真实 60 秒后强杀、Runner exit 1 | 同左 |

六个场景全部使用真实 `docker stop --timeout 70`。正常/失败关闭留出 3 秒 fixture
清理窗口，通过容器 `/proc` 抽查 Runner 与 Worker 都仍存活；不是仅由日志顺序推断等待。
断言 SIGTERM 接收与转发、Worker shutdown 开始、关闭回调一次且有序、Worker 退出请求、
Runner 实际 wait/poll 回收 Worker、Runner 随后退出及容器最终退出。超时必须有 Runner
强杀证据和 Worker `-9`，不能把 Docker 的 exit 137 当成功。所有场景要求非 OOM、
总停止耗时小于 70 秒。B 的容器最终 exited 也证明 PID1 docker-init 已退出。

另要求四个原 POSIX 子进程测试全部通过，JUnit 必须恰好 4 项、0 skip/failure/error；
两组 Host/存储测试合计恰好 6 项、0 skip/failure/error。
`test_ci_evidence.py` 验证判定器拒绝缺失、重复、乱序、未等待、错误拓扑、超时和 OOM
证据；其合成轨迹仅验证测试工具，不属于 Linux 实测。

本地（已具备项目依赖）可运行，不安装额外环境：

```sh
PYTHONDONTWRITEBYTECODE=1 python -m pytest pytests/startup_test/test_ci_evidence.py pytests/startup_test/test_graceful_shutdown.py -q -p no:cacheprovider
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytests.startup_test.storage_isolation -p pytest_asyncio.plugin -q -p no:cacheprovider
```

Windows 无 Docker/WSL 时，四个 POSIX 测试应明确 skipped，六个真实 Docker 场景为
**未执行**。即使本地存储测试通过，也不得报告生产等价 Linux 验收通过。

这套 workflow 可随未来 upstream PR 保留，但固定运行 SHA 和分支触发是本轮隔离验收
门禁；泛化为上游常规 CI 时需单独审查，不能悄悄移动被测基线。

## Optional manual isolated Linux/Docker check

Use a local disposable Docker host, never the production server. The fixture
image copies only lifecycle code and these tests, no runtime/config/secrets.
Building downloads test dependencies; no image is published.

```sh
docker build -f pytests/startup_test/Dockerfile -t maibot-shutdown-fixture .
docker run --name maibot-shutdown-fixture --init --network none -d maibot-shutdown-fixture
docker logs maibot-shutdown-fixture
# Wait for worker_ready before stopping:
docker stop --timeout 70 maibot-shutdown-fixture
docker logs maibot-shutdown-fixture
docker inspect --format '{{.State.ExitCode}}' maibot-shutdown-fixture

# Run the real POSIX process tests inside the isolated image:
docker run --network none --entrypoint python maibot-shutdown-fixture \
  -m pytest pytests/startup_test/test_graceful_shutdown.py -q
```

Expected order: `worker_ready`, `memory_stop`, `persist`, `metadata_close`,
`writer_lock_release`, `startup.shutdown_completed`, `runner_exit=0`.
The memory events in this Docker fixture are fake lifecycle markers, not proof
of real FAISS/SQLite persistence. The separate temporary-storage tests above
provide real storage evidence. Neither is a full production-data application
acceptance test; that remains outside this isolated CI stage.

## Semantics / maintenance notes

- Runner forwards the first stop request once and waits at most 60 seconds;
  repeated signals do not interrupt Worker cleanup or extend that deadline.
  Timeout escalates to kill and exits nonzero. Docker's deadline must exceed the
  Runner budget (the example Compose uses 70 seconds).
- Worker cleanup steps share a 50-second cooperative budget, leaving headroom
  before Runner's deadline. A coroutine ignoring cancellation or a blocking
  synchronous persist cannot be forcibly bounded inside the event loop; Runner
  enforces the process-level limit and reports incomplete shutdown instead.
- An early Worker signal handler records a stop received during business imports;
  once its loop is ready, it enters cleanup without starting initialization.
- Windows uses a dedicated process group and CTRL_BREAK, not `terminate()` for
  the graceful phase. Headless hosts unable to deliver console events must not
  interpret the timeout path as graceful success.
- A successful restart request still exits Worker with 42 and relaunches it.
  A simultaneous external stop suppresses relaunch. Failed shutdown exits 1,
  even when a restart was requested.
- The Worker owns process signals while embedded Uvicorn servers run. A narrow,
  reversible replacement of `Server.capture_signals` covers both directly
  constructed and `maim_message`-constructed servers without modifying vendor
  code. Uvicorn upgrades must rerun the signal-ownership tests; do not extend
  this into a general monkey-patching mechanism.
- `graceful_shutdown` returns false if a monitored step fails; the success log
  is emitted only when all those steps complete. The outer Worker caches the
  shutdown task so failure/finally/repeated-signal paths cannot run it twice.
  A_Memorix storage exceptions must propagate through its host `stop()`.

This changes shutdown failure reporting intentionally: nonzero means application
cleanup was not confirmed. It does not change storage schemas, model settings,
the restart-42 protocol or QQ/network configuration. APIs that internally swallow
their own failures cannot be made fully observable solely by this entrypoint fix.
