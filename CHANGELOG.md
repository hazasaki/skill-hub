# Release notes

## v0.1.0 — Local catalog and verified resource routing

The first tagged public release includes a Docker Compose quickstart, a browser dashboard and a Streamable HTTP MCP service.

- Browse, search and load the example catalog locally with six MCP tools and no API key. Full reads preserve frontmatter, Unicode and line endings.
- Enable the optional Baseten profile for structured task state, resource choices, dependency and alias conflict checks, and verified paging. Baseline scoring remains the default. The 24/48/96 scheduler is experimental.
- Inspect recommended and actually loaded resources separately. Detail links distinguish a skill and prompt that share a name.
- Explore the architecture diagram, repository map and live-demo screenshot in the README.

### Upgrading

Keep your content directory and local environment settings. Update your checkout and run `docker compose up --build -d`. See the backend guide for optional provider configuration.

### Verification and credits

Both Compose services passed health checks. The public MCP endpoint loaded the bundled skill and prompt, and the dashboard returned HTTP 200. Provider-backed routing was tested with a fixture. Live provider performance was not measured.

Codex contributed to implementation, testing, architecture documentation and release preparation.
