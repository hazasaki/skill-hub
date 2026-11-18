# Skill Hub dashboard backend

Standard-library service. Run from the owning repository root:

```powershell
python src/hub-dashboard/server.py --port 8769
python -m unittest discover -s src/hub-dashboard -v
```

Bind is always loopback. `--project` selects the deployment source root (default
repository root); `--state` selects an isolated writable
state directory (default PROJECT/state/hub-dashboard). Sources are read-only.
The service serves the bundled `static/index.html` and assets.
For a standalone catalog built from local Markdown, see [container setup](../../deploy/hub-dashboard/README.md).
No package install, model call, embedding, source execution or live config cutover.
Do not expose through a public proxy: localhost plus Host/Origin checks is the
intended security boundary, not multi-user authentication.

## Router Studio

The Overview launcher fills the task and opens Router Studio. Use **Run routing**
to submit it to the configured scorer. Model routing may incur charges.
TEMPR and metadata are retrieval modes. The result shows the requested mode and
the reported retrieval method, scoring rounds, and per-kind thresholds.
Semantic search only queries retrieval and does not score or load resources.

The Hub menu shows recommendations. The checkboxes start unchecked, and
**Choose none (0)** sends no load request. To load a resource with dependencies,
select those dependencies explicitly or supply previously loaded identities in
the optional context. **Load selected** calls `POST /api/v1/load_resource_choice`
with the qualified ID, raw SHA-256, and the skill bundle SHA-256 when applicable.
Only a matching verified receipt updates the loaded count and **Use in
follow-up**. The follow-up control copies identity pins and does not submit a
request. A refused or stale response contributes no identities.

Reported partial results retain the stop reason and round evidence. The
requested model alias and the provider-reported resolved model are shown
separately. Missing provider, GPU, and engine observations display as unknown.
Timing and cost describe the response and are not billing records.

## API contract

All successful API responses use schema `skill-hub.dashboard.v1`, observed_at,
status, data, sources and limits. Unknown counts/values are null. Error responses
use HTTP 400/403/404/409 and `{error:...}` without echoing input or exceptions.
GET `/api/v1/{overview,resources,graph,routing,agents,integrations,library,telemetry,quality,config}`.
GET `/api/v1/semantic?q={query}&limit={1..50}` for fast dGPU/bounded semantic retrieval.
POST `/api/v1/route_task` for interactive `route_task` testing (requires CSRF token, Origin header, and JSON body `{"state": "...", "retrieval_mode": "tempr"|"metadata", "include_graph": bool, "task_id": "..."}`).

Resource detail `/api/v1/resources/{encoded-id}?kind=skill|prompt`.
Lists use items,total,offset,limit; q is a literal FTS phrase over indexed resource
body/name/description/tags, domain is exact canonical category, kind is skill,
prompt or all. Limit 1..100, offset 0..100000; search has a two-second SQL deadline.
Body output is capped at 262144 characters after masking complete source structures; text is untrusted and credential-like
strings are redacted. Render as text, never HTML. No URL/file-path resource reads.
Resource version is the catalog body hash; dependencies are unknown.

Graph returns bounded catalog nodes and only evidenced alias_of edges whose
endpoints are both in the page. No inferred skill grants or host binding edges.
Routing partitions route IDs by native/Docker source. Events use the observer's
allowlist sanitizer in observer_read.py, 1200 per
source and a merged cap of 1200; route grouping caps at 20 per source.
Recorded metadata only: no task text, gold labels, raw error/log payloads.
Token/cost aggregate fields remain null. Existing event cost, when recorded,
is producer metadata rather than an invoice. Freshness describes source file
age, not server health; old event timestamps remain visible.
MCP integration listing is a tool catalog snapshot, not a live connection test.
Configuration sync summaries are historical reconciliation receipts.

## Quarantine preview/apply/rollback

GET config returns data.csrf_token, capabilities and up to 100 quarantine items
with sha256 (hash of the current record), version (content hash), backup hashes.
The source hash for a new intake is SHA256 of empty bytes:
`e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`.

POST `/api/v1/operations/preview`, same Origin as Host, Content-Type application/json
and X-CSRF-Token from config. Example (synthetic content):

```json
{"operation":"resource.import","kind":"skill","name":"example","content":"# Example\nReviewed later","expected_sha256":"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"}
```

Response data gives exact changes, plan_id, 300-second expiry, ownership and backup.
After the user confirms that preview, POST `/api/v1/operations/apply`:

```json
{"plan_id":"SERVER_ISSUED_ID","confirmed":true}
```

Apply uses the stored plan, never replacement content from the browser. Plans
are single use, in-memory, discarded at restart, capped at 100 and protected by
a process lock. The source must still equal preview bytes. Resource identities
are restricted to simple names; bodies 1..262144 bytes. No URLs, archives, paths,
script execution or promotion. Invalid operations return 409.

`resource.update` uses current quarantine sha256 and new content. Existing bytes
are backed up under quarantine/KIND/NAME/versions/RECORD_HASH.json before atomic
current.json replacement. `resource.rollback` preview uses current sha256 plus
`version` set to the backup RECORD_HASH; it restores that content and saves the
prior current record. Rollback is also quarantined. No deletes are exposed.
For a new import, rollback means leaving it isolated, not activating or deleting it.
A running instance owns its state directory: do not run concurrent processes on
one state directory. Keep backups private; they contain submitted resource text.
Restart leaves versioned state intact; GET config recovers hashes and backups.

## Available functionality and limits

The backend supports catalog browsing, source adapters, static quality analysis,
and isolated quarantine staging. Optional source adapters report unavailable when
their inputs are absent. Routing and semantic requests require a separately
configured Skill Hub service. The dashboard has no writer for live host settings,
models, accounts, permissions, native synchronization, or resource activation.
Unknown routing confidence, candidate usage, token usage and cost stay unknown.

## Catalog revision identity

Catalog detail includes `item.revision` with operation `resource.revise`, a
server-derived quarantine name, expected_sha256 of that quarantine record (empty
hash if absent), and source `{kind,id,version}`. Send those exact fields with kind
and edited content to preview. Both catalog version and quarantine current bytes
are checked at preview and apply. This creates/updates only a quarantine revision;
resource.update remains exclusively for an existing quarantine identity/hash.
GET resource detail with `?source=quarantine&kind=skill|prompt` returns the isolated
record, body and current sha256 for subsequent edits. Catalog IDs are not paths.

## Optional source adapters

`agents.data.items`: id,name,role,model,runtime,status,skills,mcp,permissions,
updated_at,source,loaded. OpenBot profile columns are read through query-only
SQLite; only allowlisted JSON fields leave the adapter. Hub TOML agents are
catalogued separately. Skill bindings are observed installed SKILL.md directories;
MCP availability is global configured enablement, not per-agent loaded tools.
Native paths can be configured by integration code via Sources constructor; never
by HTTP input. Container deployments need read-only native source mounts or a
trusted native adapter. Missing native mount is reported unavailable.

Overview counts agents across those two registries (not deduplicated across hosts),
MCP configured projection rows, plugins from Claude installed_plugins.json only.
Plugin count does not claim OpenBot or all-runtime plugin coverage.
Integrations retain tools and add enabled,transport,updated_at,health. Explicit
GET integrations?probe=skill-hub performs a fixed 1-second loopback /health GET;
no redirects, proxy settings, arbitrary URLs/ports or stdio executions. Tool
catalogs remain source snapshots and may add entries outside native MCP registry.

Library `kind=document` lists bounded .md shared instruction/library/workflow
sources, excluding policy/evaluation/gold/secret names and symlinks. Detail uses
resources/{encoded document id}?kind=document; documents are read-only with byte
hash version and modified time. Prompt default behavior remains unchanged.

Graph accepts type=skill|prompt|agent|mcp|project|all, node_id and depth=0..3,
limit<=200. It reads typed graph-projection edges with provenance, plus observed
agent installed-skill edges. Unknown external concept/tool nodes are excluded.
Initial view prioritizes connected dependencies; no invented project nodes.

Quality data.security_scan={items:[{id,kind,version,checks,bucket,disposition,status,evidence,remediation}],total,coverage,summary,scope} scans
all normalized skill and prompt bodies across the full catalog. Frontmatter was removed for
catalog ranking and is not assessed here. Empty bodies are separated into DATA_QUALITY
(`unscannable_empty_body`, never PASS/safe); security rule hits default to
`needs_source_review` and any `confirmed_example`/`confirmed_finding` disposition via
`quality.disposition` is pinned to `(kind,id,version,content_sha256,rule_signature,detector_fingerprint)` and
automatically invalidated on content or detector rule change; no hit is not security
acceptance. data.benchmark is allowlisted historical AUDIT aggregate fields and
numeric timing_ms, historical=true; raw cases/gold labels are never returned.

Credential masking and security triage are separate. `public_text` retains its
existing credential patterns and additionally masks password-bearing URL userinfo
and clear XML credential text/attributes, including CDATA. Password-free URLs and
public XML near misses stay unchanged; multiline masks preserve line numbers.
Dashboard staging and native publisher admission share identical v2 mask modules.
Only presentation/admission changes; original files and authorized native loading
semantics remain unchanged. Explicit placeholders, environment substitutions and
parsed Python value references may receive automatic `confirmed_example` for
their detected spans. Fenced attack examples require an example label, attack
context and defensive/mitigation context; an uncovered execution span or uncertain
credential keeps the resource in review. Literal provider-looking values, default
passwords, ambiguous expressions and original redaction markers stay unresolved.
`items[].automatic_triage` explains the syntax/context reason codes. These labels
do not prove whole-resource safety or credential validity. Manual dispositions
remain pinned and can override automatic classification.

Optional `security_scan.original_sources` reads only a trusted metadata receipt
from `Sources.security_review_receipt`. The file must be pinned as
`security-review.json` in the validated input manifest, use schema
`skill-hub.security-review.v1`, match the catalog hash and current detector
fingerprint, and contain only the strict per-record metadata allowlist. Missing,
modified, stale or malformed receipts report unavailable. It never reads original
source paths in the container. Filters: `original_disposition`, `original_q`,
`original_offset` and `original_limit` (up to 500). Counts and original-document
line numbers are separate from normalized-catalog body coverage. Original reviews
of quarantined files do not change empty catalog bodies, activation or validation.

The review receipt records the inspection timestamp and both normalized content
and raw file hashes. The scanner cache watches committed WAL changes as well as
the main file, and fingerprints masking, hints, triage and evidence semantics.
An immutable source snapshot remains a snapshot after a service restart; a fresh
review requires new source evidence and a newly pinned receipt.

Regenerate original-source evidence locally with the maintained tool. It reads
current scanner findings plus empty catalog records, verifies original file hashes
against the selected finalized snapshot, and emits no bodies, values or snippets:

```powershell
python -B src/hub-dashboard/review_original_sources.py --hub ./skill-hub --catalog <versioned-snapshot>/skill-index.db --output <new-review-directory>/security-review.json
```

Optional `--audit-output` writes the same metadata plus local source paths for an
operator. Output paths must be outside the source hub/catalog. A changed or
journaled catalog prevents publication. The deployment owner pins the generated
receipt hash in a new bundle manifest; receipt generation does not deploy it.

## Dashboard-owned settings and source export

GET config `data.owned` contains `settings` with exact keys resource_limit (1..100),
graph_limit (1..200), routing_source (all/native/docker), plus `settings_state` and
`source-snapshot_state` each `{sha256,backups,present}`. Defaults control this
service's resource/graph page sizes and routing/telemetry source filter. These
settings never change the System1 decision model or routing thresholds.

Preview config.apply request: `{operation:'config.apply',expected_sha256:<owned.settings_state.sha256>,settings:{resource_limit:50,graph_limit:100,routing_source:'all'}}`.
Preview config.rollback: `{operation:'config.rollback',expected_sha256:<current settings record hash>,version:<backup record hash>}`.
Preview config.snapshot: `{operation:'config.snapshot',expected_sha256:<owned['source-snapshot_state'].sha256>}`.
Apply always uses `{plan_id,confirmed:true}` with the same CSRF/Origin checks.
Settings and sanitized registry export write only state/managed/{settings,source-snapshot}/current.json
and content-addressed versions. Snapshot is a read-only source reconciliation
export, not a sync trigger or host configuration activation. Native disabled
sync remains disabled. No arbitrary setting keys, paths, commands or URLs.

## Container/native source bridge

Optional trusted CLI overrides (never available through HTTP):
`--native-db PATH --native-agents PATH --plugins PATH --native-snapshot PATH`.
Prefer exporting a sanitized snapshot on Windows over mounting any raw native DB:

```powershell
python src/hub-dashboard/server.py --export-native-snapshot ./state/dashboard-native-snapshot.json
```

The destination must not already exist; the command refuses overwrite, emits only
allowlisted profile/MCP/plugin fields, exports up to 200 agents, exits without
starting HTTP, and never exports env/headers/args/provider accounts. Mount just
that JSON read-only into the container and pass `--native-snapshot /sources/native.json`.
The schema is skill-hub.dashboard.native.v1; original observation time is exposed
as source.recorded_at. A snapshot is configured state, not loaded/runtime proof.
Refresh via a new versioned file; do not silently treat old snapshots as live.
The native snapshot remains private (roles/models/local binding metadata).

Other minimum readonly sources relative to --project:
- skill-hub/state/skill-index.db (with consistent SQLite WAL handling if applicable)
- skill-hub/state/telemetry/hub-telemetry.db
- state/docker-skill-hub/telemetry/hub-telemetry.db
- skill-hub/state/{mcp-tool-catalog,sync-status,hook-sync-status,autosync}.json
- skill-hub/state/graph-projection.json
- skill-hub/state/AUDIT.json
- optional shared workflow/ and skill-hub/{library,instructions}/ docs

Only --state is writable. Do not mount provider directories, native credentials,
raw tasks/evaluation cases or the whole OpenBot directory. Missing optional inputs
stay explicitly unavailable. A full raw AUDIT is only read at the fixed path and
API exports selected aggregates; integration may provide an aggregate-only copy.
Health probes include observed_at and are fresh only for that request. Fixed
127.0.0.1:8765 is relative to the service's network namespace; in a separate
container it does not prove Windows host MCP connectivity. No URL override or
redirect following is permitted. Settings affect dashboard only, not router model,
thresholds, native sync, native permissions or account selection.
