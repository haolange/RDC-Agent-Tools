# Release Baseline

Before release, run:

```bat
python spec/validate_catalog.py
python -m pytest tests -q
python scripts/check_markdown_health.py
python scripts/package_release.py
python scripts/release_gate.py --require-smoke-reports --require-release-package
```

Run smoke directly through bash before the release gate when smoke evidence is required:

```bash
bash scripts/smoke_cli.sh --context release-smoke
bash scripts/smoke_cli.sh --rdc "C:/path/sample.rdc" --context release-smoke
```

Agent platforms should see each CLI command and result in the bash output. The smoke entry writes `intermediate/logs/smoke_cli.log`; `release_gate.py --require-smoke-reports` checks that log for `[smoke] PASS` only and no longer consumes Python smoke report JSON files. `preview_geometry_smoke.py` may produce JSON or markdown evidence for preview geometry, but that evidence is separate from the release gate smoke report. GA release requires a first-party `tests/fixtures/*.rdc` or an explicit release smoke sample before tagging.
