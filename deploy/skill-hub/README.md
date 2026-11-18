# Public keyword MCP service

Run `docker compose up --build -d` from the repository root. No API key, private
catalog, local GPU, or host service is required. See the root README for the full
quickstart and [examples](../../examples/README.md) for content customization.

- MCP Streamable HTTP: `http://127.0.0.1:8765/mcp`
- Health: `http://127.0.0.1:8765/health`
- Tools: `search_skills`, `get_skill`, `search_prompts`, `get_prompt`,
  `list_skills`, `skill_stats`.

The service builds a SQLite FTS index from mounted content at startup, serves
instruction text and content hashes, and safely materializes supporting files
under its temporary directory. It never executes a loaded resource. Health checks
catalog integrity and reports disabled model routing, semantic search, and host
broker capabilities explicitly. An empty catalog can be healthy; inspect counts.

Content is read-only; index, materialized bundles, and load metadata are ephemeral
inside `/tmp`. Restart after changing content. Set `SKILL_HUB_PORT` to change the
loopback host port. This local service has no authentication; keep its loopback
binding or add your own authenticated gateway before any remote deployment.

## Optional model routing

The default profile serves the original UTF-8 bytes of skills and prompts,
including frontmatter and line endings. Requests can still use `offset` and
`max_chars` explicitly. `identity.returned_raw_sha256` verifies the returned
bytes and `file_sha256` verifies the complete source file.

Standalone model routing is an explicit opt-in. Set
`SKILL_HUB_ENABLE_MODEL_ROUTING=1` and `SKILL_HUB_KEY_PATH` to the path of an
operator-provided Baseten key file. With Docker, mount that file read-only and
set the variable to its path **inside** the container. The default Compose
quickstart stays credential-free.

The model profile exposes `route_task` and `get_route_result` alongside the six
local tools. It uses local metadata retrieval and sends the supplied task state
and candidate scoring metadata to Baseten. Use it only with data you may send
to that provider. Provider calls can incur charges. No model requests are made
at startup. The service is intended for loopback use or an authenticated
gateway. Advanced GPU retrieval, host brokers and evaluation are outside this
standalone profile.

- Supply optional `task_state` to `route_task` for structured constraints,
  current step, loaded identities and explicitly requested skill IDs.
- Full delivery is the default. With explicit `response_mode="compact"`, follow
  the returned `get_route_result` page offsets and verify the full hash. Snapshots
  are in memory, expire and recheck source integrity and load permissions.
- The choice pool separates recommended choices from `loads.loaded`. The
  resolver checks declared dependencies and conflicts, including verified
  aliases. Use the resource ID and kind for follow-up access.
- The default scoring mode is `baseline`. With `SKILL_HUB_SCORE_MODE=staged`,
  the experimental scheduler can expand cumulative scoring to 24, 48 and 96
  candidates. The mutable provider alias does not enable cross-request score
  reuse.

Run the focused tests with:

```sh
python -m pip install -r deploy/skill-hub/requirements.txt
python -B -m unittest discover -s deploy/skill-hub -p test_public_core.py -v
```
