# Final clean-PR validation (evidence branch only)

- Clean PR HEAD: `48e0db4ca879cc551ccfd396b0b869c2347e8838`.
- Runtime commit: `d5e93c90bb49b8bba10c403d8516ff294055cbe2`.
- Harness baseline: `3aecf0f8d082312a12df1757f38a1ed7df8a377a` (old successful run `33417611302`).
- Validation tooling identity is the triggering `github.sha`, recorded in `source.json`.

`clean-pr-source.json` contains SHA-256 values generated directly from the local clean PR Git
objects. `prepare_ci_context.py` rejects any runtime/Compose/regression byte mismatch before
extracting a Git-only allowlist, then rechecks the extracted bytes. The clean PR commits need
not be pushed or available as Git objects on the runner. This manifest is verified against
those actual Git objects locally before the validation commit is pushed.

`shutdown_fixture.py` and focused tests are byte-identical to the clean PR. The old observed
Docker fixture is retained as `docker_shutdown_fixture.py`, with only its self-module name
changed. It loads the same verified `bot.py` and uses the original observer and acceptance
assertions. Fixture persist/close events are not real-storage evidence.

JUnit gates: 4 POSIX + 15 P1 task-handoff + 27 other shutdown regressions; storage remains 6.
All must have zero skipped/failure/error cases. Reports use separate runner-temporary bind
mounts; business data stays in `/tmp` tmpfs. Test containers use `--network none`, read-only
root filesystems, no Docker socket and no production data/secrets.

Docker A/PID1 and B/`--init` each retain normal, injected failure and actual 60-second timeout
scenarios using `docker stop --timeout 70`. Real storage tests retain Host/Kernel shutdown,
actual persist/metadata close, vector/graph/SQLite reopen/readback and writer-lock reacquire.

This validation-only commit and tooling do not belong in the clean upstream PR.
