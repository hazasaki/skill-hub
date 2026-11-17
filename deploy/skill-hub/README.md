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

The older advanced routing modules are source references, not configured features
of this quickstart. Provider-backed routing, GPU retrieval, host integration, and
evaluation require separate setup and are not claimed by the public health check.

Run the focused tests with:

```sh
python -m pip install -r deploy/skill-hub/requirements.txt
python -B -m unittest discover -s deploy/skill-hub -p test_public_core.py -v
```
