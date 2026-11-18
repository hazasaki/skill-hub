use super::*;

pub(super) fn coalesce(data: &Value) -> Option<Value> {
    let explicit: Names = parse(&data["explicit"])?;
    let mut representatives = Vec::new();
    let mut aliases: BTreeMap<&str, Vec<&str>> = BTreeMap::new();
    let mut seen: HashMap<String, &str> = HashMap::new();
    for entry in data["entries"].as_array()? {
        let id = entry["id"].as_str()?;
        let card = &entry["resource_card"];
        let pinned = valid_sha(&entry["content_sha256"])
            && (entry["type"] == "prompt" || valid_sha(&entry["bundle_sha256"]));
        let exact = data["exact"] == true;
        if pinned
            && !explicit.contains(id)
            && (!exact
                || (valid_sha(&card["source_raw_sha256"]) && valid_sha(&card["card_sha256"])))
        {
            let key = json!([
                entry["type"],
                entry["content_sha256"],
                entry["bundle_sha256"],
                entry["risk"],
                entry["description"],
                entry["tags"],
                entry["purpose_excerpt"],
                entry["state"],
                exact.then(|| [&card["source_raw_sha256"], &card["card_sha256"]])
            ])
            .to_string();
            if let Some(representative) = seen.get(&key) {
                aliases.entry(representative).or_default().push(id);
                continue;
            }
            seen.insert(key, id);
        }
        representatives.push(id);
    }
    Some(json!([representatives, aliases]))
}

fn single_vote(votes: &BTreeMap<String, usize>) -> Option<(&String, usize)> {
    votes
        .iter()
        .next()
        .filter(|(_, rank)| votes.len() == 1 && **rank <= 5)
        .map(|(engine, rank)| (engine, *rank))
}

#[derive(Serialize, Deserialize)]
struct StagedEntry {
    id: String,
    kind: String,
    goals: BTreeSet<usize>,
    authored: bool,
    explicit: bool,
}

pub(super) fn stage_init(data: &Value) -> Option<Value> {
    let explicit: Names = parse(&data["explicit"])?;
    let ranks: Ranks = parse(&data["ranks"])?;
    let goals: Vec<Names> = parse(&data["goals"])?;
    let entries: Vec<(String, String, Names, bool)> = parse(&data["entries"])?;
    let mut rare = BTreeMap::new();
    for (id, _, _, _) in &entries {
        if let Some((engine, rank)) = ranks.get(id).and_then(single_vote) {
            let best = rare.entry(engine).or_insert((rank, id));
            if rank < best.0 {
                *best = (rank, id);
            }
        }
    }
    let rare: HashSet<_> = rare.values().map(|(_, id)| *id).collect();
    let mut staged = Vec::with_capacity(entries.len());
    let mut document_frequency = HashMap::new();
    for (_, _, terms, _) in &entries {
        for term in terms {
            *document_frequency.entry(term).or_insert(0) += 1;
        }
    }
    let common_cutoff = (entries.len() / 5).max(3);
    for (id, kind, terms, authored) in &entries {
        staged.push(StagedEntry {
            id: id.clone(),
            kind: kind.clone(),
            explicit: explicit.contains(id),
            authored: *authored,
            goals: goals
                .iter()
                .enumerate()
                .filter_map(|(i, goal)| {
                    let supported = if *authored {
                        goal.is_subset(terms)
                    } else {
                        // A shared generic topic or one artifact name is insufficient to expand.
                        let shared: Vec<_> = goal.intersection(terms).collect();
                        shared.len() >= 2
                            && shared.len() >= goal.len().div_ceil(2).min(3)
                            && shared.iter().any(|term| document_frequency[*term] <= common_cutoff)
                    };
                    supported.then_some(i)
                })
                .collect(),
        });
    }
    staged.sort_by_key(|entry| (!entry.explicit, !rare.contains(&entry.id)));
    Some(json!([staged, goals.len()]))
}

pub(super) fn stage_next(data: &Value) -> Option<Value> {
    const CAPS: [usize; 3] = [24, 48, 96];
    let mut remaining: Vec<StagedEntry> = parse(&data["remaining"])?;
    let admitted: Vec<StagedEntry> = parse(&data["admitted"])?;
    let step: usize = parse(&data["step"])?;
    let count: usize = parse(&data["goals"])?;
    let mut coverage = Value::Null;
    let mut reason = "INITIAL";
    if step > 0 {
        let mut covered = BTreeSet::new();
        for entry in &admitted {
            let score = data["scores"][&entry.id].as_f64().unwrap_or(-1.0);
            let bar = data["bars"][&entry.kind].as_f64()?;
            if score > bar || (entry.kind == "prompt" && score == bar) {
                covered.extend(entry.goals.iter().copied());
            }
        }
        coverage = json!({"supplied":count, "covered_indices":covered,
                          "semantics":"authored coverage or specific lexical overlap for qualifying scores"});
        let stop = if remaining.is_empty() {
            Some("POOL_EXHAUSTED")
        } else if step >= CAPS.len() {
            Some("STAGE_CAP_REACHED")
        } else if remaining.iter().any(|entry| entry.explicit) {
            reason = "EXPLICIT_PENDING";
            None
        } else if remaining
            .iter()
            .any(|entry| !entry.goals.is_subset(&covered))
        {
            reason = if remaining.iter().any(|entry| entry.authored && !entry.goals.is_subset(&covered)) {
                "UNRESOLVED_CURRENT_STEP_WITH_AUTHORED_EVIDENCE"
            } else {
                "UNRESOLVED_CURRENT_STEP_WITH_METADATA_EVIDENCE"
            };
            None
        } else {
            Some("NO_REMAINING_COVERAGE_EVIDENCE")
        };
        if let Some(stop) = stop {
            return Some(json!({"coverage": coverage, "stop":stop}));
        }
        remaining.sort_by_key(|entry| {
            (
                !entry.explicit,
                std::cmp::Reverse(entry.goals.difference(&covered).count()),
            )
        });
    }
    if remaining.is_empty() {
        return Some(json!({"coverage": coverage}));
    }
    let cap = *CAPS.get(step)?;
    let rest = remaining.split_off(cap.saturating_sub(admitted.len()).min(remaining.len()));
    Some(json!({"coverage":coverage, "reason":reason, "cap":cap, "next":remaining, "rest":rest}))
}

pub(super) fn cache_identity(data: &Value) -> Option<Value> {
    let canonical = data["canonical"].as_str()?;
    let allowed = match data["kind"].as_str()? {
        "scope" => data["revision"]
            .as_str()
            .is_some_and(|revision| !revision.is_empty()),
        "resource" => valid_sha(&data["scope"]) && data["pins"].as_array()?.iter().all(valid_sha),
        _ => false,
    };
    Some(if allowed {
        json!(digest(canonical.as_bytes()))
    } else {
        Value::Null
    })
}

pub(super) fn scores(data: &Value) -> Option<Value> {
    let expected: Names = parse(&data["ids"])?;
    let answers = data["answers"].as_object()?;
    if answers.len() != expected.len() {
        return Some(Value::Null);
    }
    let mut scores = BTreeMap::new();
    for (id, item) in answers {
        let Some(value) = item["noul"].as_f64() else {
            return Some(Value::Null);
        };
        if !expected.contains(id) || item["type"] != "noul" || !(0.0..=1.0).contains(&value) {
            return Some(Value::Null);
        }
        scores.insert(id, &item["noul"]);
    }
    Some(json!(scores))
}

fn interleaved<T>(rows: &[T], kind: impl Fn(&T) -> &str) -> Vec<&T> {
    let prompts: Vec<_> = rows.iter().filter(|row| kind(row) == "prompt").collect();
    let skills: Vec<_> = rows.iter().filter(|row| kind(row) == "skill").collect();
    (0..prompts.len().max(skills.len()))
        .flat_map(|i| {
            [prompts.get(i), skills.get(i)]
                .into_iter()
                .flatten()
                .copied()
        })
        .collect()
}

pub(super) fn interleave(data: &Value) -> Option<Value> {
    let entries: Vec<(String, String)> = parse(data)?;
    Some(json!(interleaved(&entries, |(_, kind)| kind)
        .into_iter()
        .map(|(id, _)| id)
        .collect::<Vec<_>>()))
}

pub(super) fn expansion_query(data: &Value) -> Option<Value> {
    let caps = data["required_capabilities"]
        .as_array()
        .into_iter()
        .flatten()
        .take(8);
    let types = data["artifact_types"]
        .as_array()
        .into_iter()
        .flatten()
        .take(4);
    let text = [&data["current_step"], &data["desired_output"]]
        .into_iter()
        .chain(caps)
        .chain(types)
        .filter_map(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(|s| s.chars().take(160).collect::<String>())
        .collect::<Vec<_>>()
        .join(" ");
    Some(json!(text))
}

pub(super) fn expand(data: &Value) -> Option<Value> {
    let mut ranks: BTreeMap<String, usize> = parse(&data["fts"])?;
    let extra: BTreeMap<String, usize> = parse(&data["extra"])?;
    let before = ranks.len();
    for (id, rank) in extra {
        ranks.entry(id).or_insert(rank.saturating_add(30));
    }
    Some(json!([ranks, ranks.len() - before]))
}
