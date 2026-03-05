# Quickstart

1. Start the interactive launcher

```bat
rdx.bat
```

Main menu now is:

1. env
2. help
3. Start MCP
4. Daemon management
0. Exit

## Env

`1` runs existing environment check and dependency bootstrap flow.

`2` shows quick usage/help text.

## Start MCP

`3` now asks transport first and always starts MCP in a new window:

- `1` stdio
  - Example:
  ```bat
  start "RDX MCP (stdio)"
  ```
  - The new window displays: `无 URL 地址`
- `2` streamable-http
  - You can type host and port
  - The new window displays: `<host>:<port>`

## CLI shell

CLI shell is still available as a dedicated entry:

```bat
rdx.bat cli-shell
```

In that window, `rdx` is an alias of:

```bat
python cli/run_cli.py <args...>
```

So you can run commands directly:

```bat
rdx capture open --file "C:\path\to\capture.rdc" --frame-index 0
rdx capture status
rdx call rd.event.get_actions --args-json "{\"session_id\":\"<session_id>\"}" --json
rdx daemon start
rdx daemon status
rdx daemon stop
```

## Daemon management

`4` opens:

- `1` Start Daemon
- `2` Show command lists
- `3` Show examples
- `0` Back

### Start Daemon

`4 -> 1` opens a **new `Daemon Shell` window** and starts daemon with an auto-generated context.

For example:

```bat
rdx.bat
# menu: 4 -> 1
```

Inside Daemon Shell:

- `1` daemon status
- `2` daemon stop
- `3` cli command
- `0` Back (also attempts `daemon stop` on exit)

### Context examples

```bat
rdx.bat --non-interactive daemon-shell demo-a
rdx.bat --non-interactive daemon-shell demo-b
```

Inside daemon shell, use:

```bat
daemon start
daemon status
daemon stop
daemon connect
```

If multiple contexts are needed, pass different context names when entering `daemon-shell`.

## Non-interactive

```bat
rdx.bat --non-interactive mcp --ensure-env
```
