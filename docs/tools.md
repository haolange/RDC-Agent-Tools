# Tools

`spec/tool_catalog.json` defines 194 `rd.*` tools. The public transport is CLI; raw tool calls use `rdx call <rd.*>`.

```bat
rdx tools list --json
rdx tools search texture --json
rdx context status --json
rdx context update --key notes --value "triaged" --json
rdx call rd.session.get_context --format json
rdx call rd.session.update_context --args-file .\args.json --format json
```

`rdx context status|update|list|clear` is the canonical agent-facing context surface. Raw `rd.session.get_context` and `rd.session.update_context` remain low-level catalog tools for precise tool transport and tests.

Session tools are described in [session-model.md](session-model.md). Agent usage rules are described in [agent-model.md](agent-model.md). Use VFS for runtime exploration before selecting precise tools:

```bat
rdx vfs ls --path / --format tsv
rdx vfs cat --path /context --format json
rdx vfs tree --path /draws --depth 2 --max-nodes 2000 --format json
```

Broad `/draws` tree nodes are summaries. When a node reports `detail_deferred=true`, fetch that event with `event show`, targeted `vfs cat`, or `rd.event.get_action_details`. Do not broad-expand `/resources`, `/textures`, or `/buffers`; use list and targeted canonical tools.

JSON is the stable protocol. TSV is only a tabular projection for list/navigation output; nested context, pipeline, shader, and preview data stay JSON.

`rd.session.open_preview` opens the preview window through the daemon-backed CLI runtime. `preview.display` is returned from context/session state for geometry inspection.

The generated reader-facing reference is [tool-reference.md](tool-reference.md). The high-frequency facade commands are `rdx event`, `rdx pipeline`, `rdx shader`, `rdx export`, `rdx pixel`, and `rdx resource`; they all dispatch to canonical `rd.*` tools and keep JSON as the default output.

For simple one-line payloads, `--args-json` remains supported. For multiline shader source, Windows paths, embedded quotes, or any complex JSON, write the object to a UTF-8 file and use `--args-file`; CLI errors for broken `--args-json` point back to this path.
