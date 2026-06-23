# Tools

`spec/tool_catalog.json` defines 194 `rd.*` tools. The public transport is CLI; raw tool calls use `rdx call <rd.*>` or `python cli/run_cli.py call <rd.*>`.

```bat
rdx tools list --json
rdx tools search texture --json
rdx context status --json
rdx context update --key notes --value "triaged" --json
rdx call rd.session.get_context --format json
rdx call rd.session.update_context --args-json "{\"key\":\"notes\",\"value\":\"triaged\"}" --format json
```

`rdx context status|update|list|clear` is the canonical agent-facing context surface. Raw `rd.session.get_context` and `rd.session.update_context` remain low-level catalog tools for precise tool transport and tests.

Session tools are described in [session-model.md](session-model.md). Agent usage rules are described in [agent-model.md](agent-model.md). Use VFS for runtime exploration before selecting precise tools:

```bat
rdx vfs ls --path / --format tsv
rdx vfs tree --path / --depth 2 --format json
rdx vfs cat --path /context --format json
```

JSON is the stable protocol. TSV is only a tabular projection for list/navigation output; nested context, pipeline, shader, and preview data stay JSON.

`rd.session.open_preview` opens the preview window through the daemon-backed CLI runtime. `preview.display` is returned from context/session state for geometry inspection.
