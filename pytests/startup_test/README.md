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

## Optional isolated Linux/Docker check

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
of real FAISS/SQLite persistence. The separate existing A_Memorix tests exercise
the real kernel lifecycle with fake stores. A full application Docker acceptance
test is still required before a production deployment.

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
