"""Read model for the capability graph. Catalog IDs and observed edges stay separate."""
from collections import Counter, defaultdict
from contextlib import closing
import json
import re
import unicodedata

from observer_read import connect_ro


RESOURCE_TYPES = ("skill", "prompt")
PROJECTED_TYPES = (*RESOURCE_TYPES, "agent", "mcp", "project")
TARGET_TYPES = {
    "cap": "capability", "tool": "tool", "dom": "concept",
    "dep": "concept", "ext": "concept",
}
RELATION_TARGETS = {
    "provides": {"cap"}, "specializes": {"cap", "dom"},
    "uses_tool": {"tool"}, "requires_tool": {"tool"},
    "depends_on": {"skill", "prompt", "dep", "ext"}, "alias_of": {"skill", "prompt"},
}
PROJECTION_SOURCES = {
    "authored:tags", "authored:category", "authored:categories", "authored:domain",
    "authored:subdomain", "authored:tools", "authored:allowed-tools",
    "authored:depends-on", "authored:requires", "catalog canonical_name", "index:aliases",
}
ID_PATTERN = re.compile(r"[^\x00-\x1f\x7f\\/]{1,512}")
CATEGORY_SLUG = re.compile(r"[^a-z0-9]+")
VERSION_PATTERN = re.compile(r"[0-9a-fA-F]{64}")
DATE_PATTERN = re.compile(r"[0-9TtZz:+.\-]{1,40}")
SOURCE_GAPS = {"missing_source", "unreadable_source", "source_index_mismatch"}


def catalog_rows(path):
    """Read graph metadata in one SQLite snapshot without reading bodies."""
    result = {}
    with closing(connect_ro(path)) as con:
        con.execute("BEGIN")
        for kind, table in (("skill", "skills"), ("prompt", "prompts")):
            columns = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
            version = "content_sha256" if "content_sha256" in columns else "body_sha256"
            provenance = "NULL"
            if {"category_state", "category_provenance"} <= columns:
                provenance = "CASE WHEN category_state='AUTHORED' THEN category_provenance END"
            for name, pin, canonical, active, authored in con.execute(
                f"SELECT name, {version}, canonical_name, active, {provenance} FROM {table}"
            ):
                if name:
                    result[f"{kind}:{name}"] = (kind, name, pin, canonical, active, authored)
    return result


def _matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _nested_categories(provenance):
    if not isinstance(provenance, str):
        return []
    try:
        values = json.loads(provenance)["sources"]["metadata.category"]
    except (ValueError, TypeError, KeyError):
        return []
    return values if isinstance(values, list) else []


def parse_projection(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("nodes"), dict) or not isinstance(raw.get("edges"), list):
        raise ValueError("invalid graph projection")
    return raw


def graph_data(raw, catalog, agents, integrations, q, clean):
    projected = raw.get("nodes", {})
    concepts = raw.get("concepts", {})
    if not isinstance(concepts, dict):
        concepts = {}
    built_at = raw.get("built_at")
    built_at = built_at if _matches(DATE_PATTERN, built_at) else None
    nodes, edges = {}, {}
    observed = set()
    gaps = raw.get("gaps")
    unverified = {
        gap["id"] for gap in gaps
        if isinstance(gap, dict) and isinstance(gap.get("id"), str)
        and isinstance(gap.get("kind"), str) and gap["kind"] in SOURCE_GAPS
    } if isinstance(gaps, list) else set()

    def safe_id(ident):
        return _matches(ID_PATTERN, ident) and clean(ident) == ident

    def add_node(ident, kind, label, status, metadata):
        nodes[ident] = {
            "id": ident, "type": kind, "label": clean(str(label or ident)[:512]),
            "status": status, "metadata": metadata,
        }

    def add_edge(source, target, relation, evidence, edge_id):
        edges.setdefault((source, target, relation), {
            "id": edge_id, "source": source, "target": target,
            "type": relation, "evidence": evidence,
        })

    def add_typed_target(ident, namespace, suffix, evidence):
        if ident not in nodes:
            # An empty normalized suffix can still be a recorded producer destination.
            if not safe_id(ident) or (not suffix and ident not in concepts):
                return False
            concept = concepts.get(ident)
            label = concept.get("label") if isinstance(concept, dict) else None
            kind = TARGET_TYPES[namespace]
            add_node(ident, kind, label or suffix.replace("-", " ") or f"Unlabeled {kind}",
                     "projected", {"namespace": namespace, "provenance": []})
        provenance = nodes[ident]["metadata"]["provenance"]
        if evidence not in provenance:
            provenance.append(evidence)
        return True

    for ident, (kind, name, version, _, active, _) in (catalog or {}).items():
        if safe_id(ident):
            add_node(ident, kind, name, "cataloged", {
                "version": version if _matches(VERSION_PATTERN, version) else None,
                "observed_at": None, "active": None if active is None else bool(active),
            })

    for ident, entry in projected.items():
        if not isinstance(entry, dict):
            continue
        kind = entry.get("t")
        if kind not in PROJECTED_TYPES or ident.partition(":")[0] != kind:
            continue
        if ident not in nodes and (not safe_id(ident) or (kind in RESOURCE_TYPES and catalog is not None)):
            continue
        catalog_pin = catalog[ident][2] if catalog and ident in catalog else None
        projection_pin = entry.get("ch")
        if catalog_pin and projection_pin and catalog_pin != projection_pin:
            continue
        if ident not in nodes:
            add_node(ident, kind, entry.get("n"), "cataloged", {"version": None, "observed_at": None})
        if ident in unverified:
            continue
        node = nodes[ident]
        node["status"] = "projected"
        if _matches(VERSION_PATTERN, projection_pin):
            node["metadata"]["version"] = projection_pin
        observed_at = entry.get("obs")
        node["metadata"]["observed_at"] = observed_at if _matches(DATE_PATTERN, observed_at) else None
        observed.add(ident)

    for index, entry in enumerate(raw.get("edges", [])):
        if not isinstance(entry, dict):
            continue
        source, target, relation = entry.get("s"), entry.get("d"), entry.get("r")
        if not all(isinstance(value, str) for value in (source, target, relation)):
            continue
        if source not in observed or source == target or nodes[source]["type"] not in RESOURCE_TYPES:
            continue
        namespace, _, suffix = target.partition(":")
        if namespace not in RELATION_TARGETS.get(relation, ()):
            continue
        evidence_source = entry.get("src")
        if not isinstance(evidence_source, str) or evidence_source not in PROJECTION_SOURCES:
            continue
        if evidence_source in {"index:aliases", "catalog canonical_name"} and (
            relation != "alias_of" or nodes[source]["type"] != namespace
        ):
            continue
        evidence_class = "authored" if entry.get("class") == "authored" else None
        evidence = {"source": evidence_source, "class": evidence_class, "projection_built_at": built_at}
        if namespace in TARGET_TYPES:
            if evidence_class != "authored" or not evidence_source.startswith("authored:"):
                continue
            if not add_typed_target(target, namespace, suffix, evidence):
                continue
        elif target not in observed:
            continue
        add_edge(source, target, relation, evidence, str(index))

    nested_evidence = {"source": "authored:metadata.category", "class": "authored", "projection_built_at": None}
    for ident, (kind, name, pin, canonical, _, authored) in (catalog or {}).items():
        if ident not in nodes:
            continue
        if _matches(VERSION_PATTERN, pin):
            for label in _nested_categories(authored):
                if not isinstance(label, str) or clean(label) != label:
                    continue
                # Use the projection's capability ID normalization for the same vocabulary.
                suffix = CATEGORY_SLUG.sub("-", unicodedata.normalize("NFKC", label).lower()).strip("-")
                target = f"cap:{suffix}"
                if suffix and add_typed_target(target, "cap", suffix, nested_evidence):
                    add_edge(ident, target, "specializes", nested_evidence, f"{ident}->{target}:metadata.category")
        if canonical and canonical != name:
            target = f"{kind}:{canonical}"
            if target in nodes:
                add_edge(ident, target, "alias_of", {
                    "source": "catalog canonical_name", "class": None, "projection_built_at": None,
                }, f"{ident}->{target}")

    for agent in agents:
        ident = f"agent:{agent['id']}"
        if not safe_id(ident):
            continue
        add_node(ident, "agent", agent["name"], agent["status"], {
            "model": agent["model"], "runtime": agent["runtime"],
        })
        for skill in agent.get("skills") or []:
            target = f"skill:{skill}"
            if target in nodes:
                add_edge(ident, target, "installed_skill", "native agent .agents/skills directory",
                         f"{ident}->{target}")
    for integration in integrations:
        ident = f"mcp:{integration['id']}"
        if safe_id(ident):
            add_node(ident, "mcp", integration["name"], integration["status"],
                     {"enabled": integration.get("enabled")})

    return _graph_page(nodes, list(edges.values()), observed, q)


def _graph_page(nodes, edges, observed, q):
    adjacency = defaultdict(list)
    for index, edge in enumerate(edges):
        adjacency[edge["source"]].append((edge["target"], index))
        adjacency[edge["target"]].append((edge["source"], index))
    for ident, node in nodes.items():
        node["degree"] = len(adjacency[ident])
        node["isolated"] = False if node["degree"] else (True if ident in observed else None)

    def order(ident):
        return not adjacency[ident], nodes[ident]["label"].casefold(), ident

    def counts(ids, edge_count):
        return {
            "nodes": len(ids), "edges": edge_count,
            "isolated_nodes": sum(nodes[ident]["isolated"] is True for ident in ids),
            "unknown_nodes": sum(nodes[ident]["isolated"] is None for ident in ids),
            "by_type": dict(Counter(nodes[ident]["type"] for ident in ids)),
        }

    root = q.get("node_id") or None
    limit = max(1, min(200, int(q.get("limit", 100))))
    offset = max(0, int(q.get("offset", 0)))
    neighbor_limit = max(0, min(100, int(q.get("neighbor_limit", 40))))
    context = []
    parents = {}
    if root:
        matches = [root] if root in nodes else []
        visited = set(matches)
        frontier = matches
        for _ in range(max(0, min(3, int(q.get("depth", 1))))):
            for ident in frontier:
                for neighbor, _ in adjacency[ident]:
                    if neighbor not in visited:
                        parents.setdefault(neighbor, ident)
            frontier = sorted(set(parents) - visited, key=order)
            if not frontier:
                break
            matches.extend(frontier)
            visited.update(frontier)
        if matches and offset:
            context.append(root)
    else:
        kind = q.get("type", "all")
        query = q.get("q", "")[:200].casefold()
        matches = sorted((
            ident for ident, node in nodes.items()
            if (kind == "all" or node["type"] == kind)
            and (query in node["label"].casefold() or query in ident.casefold())
        ), key=order)

    page = matches[offset:offset + limit]
    context_truncated = False
    if root:
        page_ids = set(page) | set(context)
        missing_parents = {}
        for ident in page:
            while ident in parents:
                ident = parents[ident]
                if ident not in page_ids:
                    missing_parents[ident] = None
        context.extend(list(missing_parents)[:neighbor_limit])
        context_truncated = len(missing_parents) > neighbor_limit
    else:
        page_ids = set(page)
        incident = Counter(
            neighbor for ident in page for neighbor, _ in adjacency[ident]
            if neighbor not in page_ids and nodes[ident]["type"] != nodes[neighbor]["type"]
        )
        neighbors = sorted(incident, key=lambda ident: (-incident[ident], order(ident)))
        context = neighbors[:neighbor_limit]
        context_truncated = len(neighbors) > neighbor_limit

    visible_ids = (context + page) if root else (page + context)
    visible_set = set(visible_ids)
    match_edge_count = len({index for ident in matches for _, index in adjacency[ident]})
    visible_edge_ids = sorted({
        index for ident in visible_ids for neighbor, index in adjacency[ident]
        if neighbor in visible_set
    })
    visible_edges = [edges[index] for index in visible_edge_ids[:500]]
    degree = Counter(ident for edge in visible_edges for ident in (edge["source"], edge["target"]))
    context_ids = set(context)
    next_offset = offset + limit if offset + limit < len(matches) else None
    return {
        "nodes": [{
            **nodes[ident], "role": "context" if ident in context_ids else "match",
            "visible_degree": degree[ident],
        } for ident in visible_ids],
        "edges": visible_edges,
        "truncated": next_offset is not None or context_truncated or len(visible_edge_ids) > 500,
        "total": len(matches), "node_id": root if root in nodes else None, "limit": limit, "offset": offset,
        "next_offset": next_offset,
        "counts": {
            "full": counts(nodes, len(edges)),
            "matched": counts(matches, match_edge_count),
            "visible": {
                **counts(visible_ids, len(visible_edges)), "context_nodes": len(context),
                "no_visible_edges": len(visible_ids) - len(degree),
            },
        },
    }
