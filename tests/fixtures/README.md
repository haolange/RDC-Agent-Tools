# `tests/fixtures/`

This directory contains small public `.rdc` captures used only by repository tests.

Current fixture set:

| File | Size | SHA256 | Source |
| --- | ---: | --- | --- |
| `hello_triangle.rdc` | 75478 | `00797a27e6316a0cf4369327f9db30a21635fa757673b3f9712af07989145ba8` | `rdx-third/rdc-cli/tests/fixtures/hello_triangle.rdc` |
| `vkcube.rdc` | 75478 | `00797a27e6316a0cf4369327f9db30a21635fa757673b3f9712af07989145ba8` | `rdx-third/rdc-cli/tests/fixtures/vkcube.rdc` |
| `vkcube_validation.rdc` | 65913 | `c50cd1e7c29241c64fd33faf07cb35e802f9dc85692a8512aa36db01c956b385` | `rdx-third/rdc-cli/tests/fixtures/vkcube_validation.rdc` |

Policy:

- Only small public captures with clear source and license may live here.
- Do not copy developer-local, private, customer, or large `.rdc` files into this repository.
- Do not write developer-machine absolute capture paths into source, docs, tests, or release metadata.
- These fixtures are test-only and must not be included in release packages.
- Attribution for copied MIT assets is tracked in `THIRD_PARTY_NOTICES.md`.

Full local or Android remote smoke may still pass an explicit external capture path to CLI smoke commands when a real RenderDoc runtime/device is available:

```bash
bash scripts/smoke_cli.sh --rdc "C:/path/sample.rdc" --context cli-smoke
```
