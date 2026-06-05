# Android Remote CLI Smoke Prompt

Use this prompt for Android remote-only smoke through CLI transport. Start from [../README.md](../README.md), then keep [session-model.md](session-model.md), [agent-model.md](agent-model.md), [troubleshooting.md](troubleshooting.md), [doc-governance.md](doc-governance.md), and [../scripts/README.md](../scripts/README.md) aligned.

Core sequence:

```bat
rdx.bat --json doctor
rdx.bat call rd.remote.connect --format json
rdx.bat call rd.remote.ping --format json
rdx.bat call rd.capture.open_replay --format json
rdx.bat call rd.session.get_context --format json
```

Run `preview_geometry_smoke.py` when the Android remote smoke changes preview behavior.