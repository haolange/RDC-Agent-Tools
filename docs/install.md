# Install

`rdx-tools` GA artifacts are self-contained Windows x64 packages. Users should not install Python, create a virtual environment, or run a package manager before using the CLI.

## Install or Upgrade

Extract `rdx-tools-<version>-windows-x64.zip`, then run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/rdx_install.ps1 -Action install -InstallDir "$env:LOCALAPPDATA\Programs\rdx-tools" -AddToPath
powershell -ExecutionPolicy Bypass -File scripts/rdx_install.ps1 -Action upgrade -InstallDir "$env:LOCALAPPDATA\Programs\rdx-tools" -AddToPath
```

Use `-DryRun` to inspect the file and PATH actions first.

## Doctor

```powershell
powershell -ExecutionPolicy Bypass -File scripts/rdx_install.ps1 -Action doctor -InstallDir "$env:LOCALAPPDATA\Programs\rdx-tools"
rdx --json doctor
```

## Uninstall

```powershell
powershell -ExecutionPolicy Bypass -File scripts/rdx_install.ps1 -Action uninstall -InstallDir "$env:LOCALAPPDATA\Programs\rdx-tools"
```

The install script only copies `rdx-tools`, updates the user PATH when requested, and refuses to remove a directory that does not contain `rdx.bat`.

