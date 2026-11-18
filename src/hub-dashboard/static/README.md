# Skill Hub dashboard interface

The browser interface uses HTML, CSS, JavaScript and SVG. See the
[container quickstart](../../../deploy/hub-dashboard/README.md) and
[Dashboard API](../README.md).

## Navigation and appearance

- Open a section from the sidebar. Use its toggle or Ctrl+B (Command+B on
  macOS) to collapse or expand it. On narrow screens the same control opens
  the navigation drawer. Press Escape to close the drawer and return focus
  to its toggle.
- Use the language control to switch between English and Vietnamese. English
  is the default. Resource names and source text stay in their original language.
- Choose Light, Dark or System. System follows your device's appearance setting.
  The browser remembers your language and theme for this Dashboard origin.
- The interface respects the device's reduced-motion setting.

## Explore capabilities

Open the capabilities view. Use **Search by name** and **Resource type** to
narrow the results. Select a resource and use **Focus on node** to explore up
to three links away. **Exit focus** restores your search and type filter.
The legend identifies node types. Relationship details show the endpoints, the
reported relationship and its source when available.

Use **Zoom out**, **Zoom in** and **Fit** in the map, or switch to **List**
to explore the same resources with the keyboard. The API returns up to 200
nodes and 500 relationships per view. A resource can appear without links when
its source has no recorded relationship for it. Edges such as aliases and
installed skills describe catalog data. They grant no access and provide no
runtime-use evidence.

## Other views

Overview reports counts and source availability. The skill catalog and prompt
library support browsing and detail inspection. Semantic search requires a
configured retrieval service. Router Studio runs an explicit
request against the configured Hub. Agent, integration, activity and quality
views depend on the sources configured for the installation.

Intake stages resources in Dashboard quarantine. Write operations use a
server-issued CSRF token. Resource content is untrusted and rendered as text.

## Local checks

From the repository root:

```sh
node --check src/hub-dashboard/static/app.js
node --check src/hub-dashboard/static/intake.js
node src/hub-dashboard/test_intake_ui.js
```
