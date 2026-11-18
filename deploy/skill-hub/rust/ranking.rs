use super::*;

#[derive(Deserialize)]
struct Candidate {
    id: String,
    kind: String,
    name_hits: Names,
    hits: Names,
}

pub(super) fn metadata(data: &Value) -> Option<Value> {
    let rows: Vec<Candidate> = parse(&data["entries"])?;
    let limit: usize = parse(&data["limit"])?;
    let text = data["text"].as_str()?;
    let use_fts = data["fts"].is_object();
    let mut frequencies = BTreeMap::new();
    for row in &rows {
        for term in &row.hits {
            *frequencies.entry(term).or_insert(0usize) += 1;
        }
    }
    let weights: BTreeMap<_, _> = frequencies
        .into_iter()
        .map(|(term, count)| (term, (1.0 + rows.len() as f64 / (1 + count) as f64).ln()))
        .collect();
    let mut ranked: Vec<_> = rows
        .iter()
        .map(|row| {
            let mut sum = 0.0_f64;
            let mut correction = 0.0;
            for term in &row.hits {
                let weight = weights[term]
                    * if row.name_hits.contains(term) {
                        4.0
                    } else {
                        1.0
                    };
                let next = sum + weight;
                correction += if sum.abs() >= weight.abs() {
                    (sum - next) + weight
                } else {
                    (weight - next) + sum
                };
                sum = next;
            }
            let fts = if use_fts {
                data["fts"][&row.id]
                    .as_u64()
                    .unwrap_or(rows.len() as u64 + 1)
            } else {
                0
            };
            (row, fts, sum + correction)
        })
        .collect();
    // Hash only tied ranks. The common case leaves the full catalog free of per-row SHA.
    let mut ties = HashMap::new();
    ranked.sort_by(|a, b| {
        a.1.cmp(&b.1)
            .then_with(|| b.2.total_cmp(&a.2))
            .then_with(|| {
                let mut tie = |row: &Candidate| {
                    *ties.entry(row.id.clone()).or_insert_with(|| {
                        let mut hasher = Sha256::new();
                        hasher.update(text.as_bytes());
                        hasher.update(b"\0");
                        hasher.update(row.id.as_bytes());
                        hasher.finish()
                    })
                };
                tie(a.0).cmp(&tie(b.0))
            })
    });
    let (skills, prompts): (Vec<_>, Vec<_>) = ranked
        .into_iter()
        .map(|(row, _, _)| row)
        .filter(|row| matches!(row.kind.as_str(), "skill" | "prompt"))
        .partition(|row| row.kind == "skill");
    let cap = limit.min(rows.len());
    let reserved = if cap == 0 {
        0
    } else {
        prompts.len().min((cap / 5).max(1))
    };
    let skill_count = skills.len().min(cap - reserved);
    let prompt_count = prompts.len().min(cap - skill_count);
    let chosen: Vec<_> = skills
        .iter()
        .take(skill_count)
        .chain(prompts.iter().take(prompt_count))
        .map(|row| &row.id)
        .collect();
    Some(json!([chosen, skill_count, prompt_count]))
}

fn valid_identity(value: &Value) -> Option<&str> {
    value
        .as_str()
        .filter(|hash| hash.len() == 64 && hash.bytes().all(|b| b.is_ascii_hexdigit()))
}

fn same_pin(left: &Value, right: &Value) -> bool {
    valid_identity(left)
        .zip(valid_identity(right))
        .is_some_and(|(a, b)| a.eq_ignore_ascii_case(b))
}

fn contains(aliases: &Value, identifier: &Value) -> bool {
    aliases
        .as_array()
        .is_some_and(|items| items.contains(identifier))
}

fn audited(entry: &Value) -> bool {
    entry["equivalence_audited"] == true || entry["authorization_source"] == "host_policy_only"
}

fn equivalent(left: &Value, right: &Value) -> bool {
    let (a, b) = (&left["entry"], &right["entry"]);
    if a["type"] != b["type"] {
        return false;
    }
    let skill = a["type"] == "skill";
    let prompt = a["type"] == "prompt";
    let content = same_pin(&a["content_sha256"], &b["content_sha256"]);
    let bundle = same_pin(&a["bundle_sha256"], &b["bundle_sha256"]);
    if content && (prompt || (skill && bundle)) {
        return true;
    }
    let canonical = |row: &Value| {
        row["canonical_id"]
            .as_str()
            .filter(|id| !id.is_empty())
            .or_else(|| row["mirror_of"].as_str().filter(|id| !id.is_empty()))
            .map(str::to_owned)
    };
    let full = |row: &Value| {
        row["full_id"]
            .as_str()
            .filter(|id| !id.is_empty())
            .map_or_else(|| row["entry"]["id"].clone(), |id| json!(id))
    };
    let (a_can, b_can) = (canonical(a), canonical(b));
    let (a_full, b_full) = (full(left), full(right));
    let linked = a_can
        .as_ref()
        .is_some_and(|id| right["id"] == *id || b_full == *id)
        || b_can
            .as_ref()
            .is_some_and(|id| left["id"] == *id || a_full == *id)
        || (a_can.is_some() && a_can == b_can)
        || contains(&a["equivalent_ids"], &right["id"])
        || contains(&a["equivalent_ids"], &b_full)
        || contains(&b["equivalent_ids"], &left["id"])
        || contains(&b["equivalent_ids"], &a_full);
    linked
        && ((skill && (bundle || (content && audited(a) && audited(b))))
            || (prompt && (content || (audited(a) && audited(b)))))
}

pub(super) fn equivalence(data: &Value) -> Option<Value> {
    Some(json!(equivalent(&data["left"], &data["right"])))
}

pub(super) fn clusters(data: &Value) -> Option<Value> {
    let rows = data.as_array()?;
    let mut clusters: Vec<Vec<usize>> = Vec::new();
    for (index, row) in rows.iter().enumerate() {
        if let Some(cluster) = clusters
            .iter_mut()
            .find(|nodes| nodes.iter().any(|node| equivalent(row, &rows[*node])))
        {
            cluster.push(index);
        } else {
            clusters.push(vec![index]);
        }
    }
    let preference = |node: usize| {
        let row = &rows[node];
        (
            row["compatible"].as_bool().unwrap_or(false),
            row["score"].as_f64().unwrap_or(0.0),
            row["specificity"].as_u64().unwrap_or(0),
            row["canonical"].as_bool().unwrap_or(false),
        )
    };
    let results: Vec<_> = clusters
        .into_iter()
        .map(|nodes| {
            let primary = nodes
                .iter()
                .copied()
                .reduce(|best, node| {
                    if preference(node) > preference(best) {
                        node
                    } else {
                        best
                    }
                })
                .unwrap();
            (primary, nodes)
        })
        .collect();
    Some(json!(results))
}
