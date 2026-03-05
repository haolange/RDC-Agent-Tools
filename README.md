# rdx-tools

RenderDoc MCP + CLI utility set.

## Quickstart

1. Run menu launcher:

```bat
rdx.bat
```

2. Open a CLI window with pre-configured `rdx` alias:

```bat
rdx.bat
# 4 -> 3
# start CLI Window (new window)
```

Inside that new window, you can run:

```bat
rdx capture open --file "C:\path\to\capture.rdc" --frame-index 0
rdx capture status
rdx call rd.event.get_actions --args-json "{\"session_id\":\"<session_id>\"}" --json
```

3. Use the following flow to start daemon in-shell:

```bat
rdx.bat
# 5 -> 1
# start Daemon Shell (new window)
```

Inside the shell: `1` status, `2` stop, `3` cli command.

## Daemon lifecycle

Use daemon shell for daemon lifecycle. The shell keeps context and runs all commands through `cli-shell` runtime:

```bat
rdx.bat daemon-shell [context]
```

If you need a dedicated context, pass it to daemon-shell:

```bat
rdx.bat daemon-shell demo-a
```

Inside daemon shell, use daemon actions:

```bat
daemon start
daemon status
daemon stop
```

See `docs/quickstart.md` and `docs/troubleshooting.md` for menu flow and context notes.
