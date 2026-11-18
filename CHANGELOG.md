# Release notes

## Unreleased

- Package an animated React sidebar with keyboard and mobile drawer controls.
- Use black surfaces and charcoal panels in Dark and System-dark.

- The sidebar collapses on desktop and opens as a drawer on narrow screens.
- The Dashboard opens in English and supports Vietnamese. Light, Dark and System
  themes and language are saved in the browser.
- The capabilities view adds name search, type filters, neighborhood focus,
  relationship evidence, zoom and fit, and a keyboard-accessible list.

- Adaptive routing scores candidates in stages of 24, 48 and 96. Agents can choose from a menu of up to 12 skills and 8 prompts. Loading an entry checks its permissions, dependencies and content hash.
- The dashboard supports explicit resource selection and verified loading. The README illustrates the flow from catalog lookup to delivery.

- Optional model routing can use Liquid D1 or Baseten. The default quickstart
  stays local and credential-free.
- Model routing defaults to a dynamic candidate budget, with explicit baseline
  and staged modes.
- Bounded parallel reads prepare verified candidate cards. YAML dates and
  timestamps can now pass through model routing without changing source bytes.

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
