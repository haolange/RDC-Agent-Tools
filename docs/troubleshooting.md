# Troubleshooting

## I cannot find `daemon status`/`daemon stop` in the main menu

`daemon status` and `daemon stop` are now intentionally moved into **Daemon Shell**. Use:

```bat
rdx.bat
# menu: 4 -> 1
# in Daemon Shell: 1/2
```

## Parallel daemon contexts

State is isolated by context now:

- default context keeps `daemon_state.json`
- custom contexts use `daemon_state_<context>.json`

When calling daemon CLI explicitly, enter daemon shell with context and run the command inside:

```bat
rdx.bat --non-interactive daemon-shell <context>
```

## Daemon shell closes and daemon still alive

Daemon Shell tracks owner process and passes `--owner-pid` to daemon.
If the shell window is closed, daemon should receive owner-loss signal and stop automatically.

If needed, stop manually in shell with:

```bat
rdx.bat --non-interactive daemon-shell <context>
```

## MCP stdio URL confusion

For `stdio` transport, launcher displays `无 URL 地址` intentionally because no network endpoint exists.
`streamable-http` displays the `host:port` chosen when launching.
