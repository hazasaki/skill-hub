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

Standalone model routing is an explicit opt-in. From the repository root, use
the public Compose override with a file that contains only the selected provider's
key. Set `SKILL_HUB_MODEL_KEY_FILE` to its host path and
`SKILL_HUB_MODEL_PROVIDER` to `liquid` or `baseten` in your shell. Keep the key
file outside this checkout.

```sh
SKILL_HUB_MODEL_PROVIDER=liquid SKILL_HUB_MODEL_KEY_FILE=~/liquid.key \
  docker compose -f compose.yaml -f compose.model.yaml up --build -d
```

In PowerShell, set the two variables with `$env:SKILL_HUB_MODEL_PROVIDER = 'liquid'`
and `$env:SKILL_HUB_MODEL_KEY_FILE = '<path to key file>'` before the same Compose
command. Compose mounts the key read-only. Ensure the container user (UID 10001) can
read the mounted file. When running `serve_core.py` without
Compose, set `SKILL_HUB_ENABLE_MODEL_ROUTING=1`, `SKILL_HUB_MODEL_PROVIDER` and
`SKILL_HUB_KEY_PATH` explicitly. The quickstart without the override stays
credential-free.

`liquid` uses `d1:latest` through Liquid's TypeSafe System One endpoint and
records the resolved model returned by Liquid. `baseten` uses Elephant 9B and
remains the provider default when model routing is enabled.

The model profile exposes `route_task` and `get_route_result` alongside the six
local tools. It uses local metadata retrieval and sends the supplied task state
and candidate scoring metadata to the configured provider. Use it only with data you may send
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
- The default `dynamic` mode chooses a single scoring budget from request and
  retrieval evidence. Weak evidence, including standalone metadata retrieval
  without corroborating ranks, keeps the full prepared pool up to 250.
  `baseline` scores the full prepared pool. `staged` opts into cumulative
  scoring at 24, 48 and 96. Set `SKILL_HUB_SCORE_MODE` to override the default.
  The route receipt reports the budget or stages. Staged scoring requires an
  immutable model revision for per-resource score reuse.
- The router reads candidate cards concurrently, verifies their identity and
  stays within the existing route deadline. YAML dates in metadata become ISO
  strings for routing while the original delivered bytes stay intact.

Run the focused tests with:

```sh
python -m pip install -r deploy/skill-hub/requirements.txt
python -B -m unittest discover -s deploy/skill-hub -p test_public_core.py -v
```
