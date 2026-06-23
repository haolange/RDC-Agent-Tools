# Capture Fixture Strategy

`rdx-tools` allows small public `.rdc` captures in `tests/fixtures/` for deterministic repository tests. Release packages must not include `.rdc` captures.

Use test layers this way:

- `unit` and `contract` tests use fake controllers, mock payloads, schema checks, and catalog validation.
- Fixture-backed repository tests may use the committed public captures listed in `tests/fixtures/README.md`.
- Full local smoke passes an explicit desktop capture to `bash scripts/smoke_cli.sh --rdc <path>` when a real RenderDoc runtime is available.
- Android remote smoke uses CLI transport and an explicit capture path for `rd.remote.connect`, `rd.remote.ping`, and `rd.capture.open_replay`.
- `gpu_live` checks depend on a real RenderDoc/GPU/remote environment and are not default clean-checkout gates.

Rules for captures:

- Only small public captures with clear source and license may live in `tests/fixtures/`.
- Do not copy local, private, customer, or large captures into this repository.
- Do not write developer-machine absolute paths into source, docs, tests, or release metadata.
- Record fixture file name, size, SHA256, source, and license in `tests/fixtures/README.md` and `THIRD_PARTY_NOTICES.md`.
- Record the Android device serial and release package SHA256 in `intermediate/logs/tool_smoke_findings.md` when they are part of release validation.
- If a capture exposes a product bug or environment blocker, keep the evidence log and open a focused follow-up task instead of weakening the release gate.

Release behavior:

- `bash scripts/smoke_cli.sh` without `--rdc` runs entry smoke only.
- `bash scripts/smoke_cli.sh --rdc <path>` runs the daemon-backed capture chain.
- `python scripts/release_gate.py --require-smoke-reports` checks only that `intermediate/logs/smoke_cli.log` contains `[smoke] PASS`.
- `scripts/package_release.py` and `scripts/verify_release_package.py` exclude `.rdc` captures and `tests/` from release packages.
