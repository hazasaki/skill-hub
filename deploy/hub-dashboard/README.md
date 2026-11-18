# Portable dashboard

The bundled Dashboard browses skills and prompts, searches full text,
inspects resource bodies, and stages imports in its own quarantine. It builds its
catalog from the same local Markdown directory as the MCP service on every start.
No private database, snapshot bundle, API key, or native host configuration is
required. Credentials found in resource text are masked before API output.

From the repository root:

```sh
docker compose -f compose.dashboard.yaml config --quiet
docker compose -f compose.dashboard.yaml up -d --build
```

Open [the dashboard](http://127.0.0.1:8769). The root Compose file starts it alongside
Skill Hub. To use a different content tree, set `SKILL_HUB_CONTENT_DIR` to a folder
containing `skills/NAME/SKILL.md` and optional `prompts/`. Set `HUB_DASHBOARD_PORT`
when 8769 is occupied; Host/Origin checks use that same port. Recreate the service
after changing either setting. Restart to rebuild the catalog after editing files.

The Dashboard opens in English and follows the system appearance. Use the
language and theme controls to change these preferences. In the capabilities
view, search for the bundled skill by name, then choose it to see the recorded
relationships. The example catalog can show resources without edges.
[Browser controls and graph guide](../../src/hub-dashboard/static/README.md)

Content is mounted read-only. Catalog construction uses temporary container
storage; settings and quarantine persist in the `dashboard-state` named volume.
Importing a batch here stages it only. To make resources available to MCP, add
reviewed Markdown to your content directory and restart both services. The legacy
CLI publication command shown in the intake panel requires an explicitly built
catalog and a separately configured publication overlay.

The root stack sets `SKILL_HUB_CORE_HOST=skill-hub` for health and routing requests.
The standalone dashboard defaults to loopback inside its container; an absent MCP
service therefore returns unavailable promptly. This target is operator configuration,
never a browser-supplied address. When running just
the dashboard, routing/semantic requests and health probes are unavailable.
The Dashboard builds a local capability graph from the mounted catalog's authored
frontmatter. Its temporary projection and index stay in container storage. Native
agent/plugin configuration, telemetry and historical quality receipts are not
bundled. Absent sources are shown as unavailable.
[Graph API fields](../../src/hub-dashboard/README.md#api-contract)

Static quality findings are heuristics rather than security approval. The container
healthcheck verifies HTTP availability only.

This is a local single-user console. Keep the loopback binding; it has no public
multi-user authentication. To stop it without changing persistent state:

```sh
docker compose -f compose.dashboard.yaml stop hub-dashboard
```

For an existing private installation, `prepare_sources.py`, `attach_reviews.py`,
and `validate_inputs` remain available for explicitly prepared sanitized snapshots.
The public container no longer mounts or automatically reads those bundles.

Focused checks (with the repository Python dependencies installed):

```sh
python -B -m unittest discover -s src/hub-dashboard -q
python -B -m unittest discover -s deploy/hub-dashboard -q
node src/hub-dashboard/test_intake_ui.js
```

### Build the sidebar for local development

The Dashboard container builds the React and TypeScript sidebar automatically.
When starting the Python Dashboard directly from a source checkout, build the
bundled sidebar first. Run these commands from the repository root:

```sh
cd src/hub-dashboard/frontend
npm ci --ignore-scripts --no-audit --no-fund
npm run build
```

Use Node.js 22 or newer. The build emits local JavaScript and scoped Tailwind
CSS under `src/hub-dashboard/static`. The component lives in
`frontend/components/ui/animated-sidebar.tsx`. It uses the project's theme
tokens. The build also emits the license notices for bundled libraries.

### Routing compatibility

The bundled keyless catalog supports browsing, search and resource details.
For model scoring with the standalone Hub, enable the model profile and use
Router Studio's **Metadata ranking** mode. Routing sends the task to the
configured scorer only when you submit it and may incur charges. TEMPR and
semantic search require a connected Hub that supports those retrieval modes.
The [Router Studio guide](../../src/hub-dashboard/README.md#router-studio)
explains menu choices, verified loads, and follow-up.

Dashboard catalog details remain bounded previews. The routing adapter requests
full results and rejects deferred transport or JSON over 64 MiB. The existing
credential redaction still applies to displayed text.
