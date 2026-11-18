use super::*;

const ENGINES: [&str; 4] = ["semantic", "keyword", "graph", "temporal"];

#[derive(Deserialize)]
struct Hit {
    id: String,
    rank: usize,
}

#[derive(Serialize)]
struct Row {
    group_id: String,
    id: String,
    score: f64,
    engines: Vec<String>,
    best_rank: BTreeMap<String, usize>,
}

pub(super) fn fuse(data: &Value) -> Option<Value> {
    let mut groups = HashMap::new();
    let mut verified = HashMap::new();
    let mut kinds = HashMap::new();
    for entry in data["entries"].as_array()? {
        let kind = entry["type"].as_str().unwrap_or("");
        let id = match entry["id"].as_str().filter(|id| !id.is_empty()) {
            Some(id) => {
                kinds.insert(id.to_owned(), kind);
                id.to_owned()
            }
            None => match entry["resource_id"].as_str() {
                Some(name) if !name.is_empty() && !kind.is_empty() => format!("{kind}:{name}"),
                _ => continue,
            },
        };
        let group = entry["equivalence_group_id"]
            .as_str()
            .filter(|s| !s.is_empty())
            .or_else(|| data["group_map"][&id].as_str().filter(|s| !s.is_empty()));
        let content = &entry["content_sha256"];
        let bundle = &entry["bundle_sha256"];
        let assigned = if let Some(group) = group {
            group.to_owned()
        } else if valid_sha(content) && (kind == "prompt" || valid_sha(bundle)) {
            let key = json!([kind, content, bundle, entry["risk"], entry["state"]]).to_string();
            verified.entry(key).or_insert_with(|| id.clone()).clone()
        } else {
            id.clone()
        };
        groups.insert(id, assigned);
    }

    let k = data["k"].as_f64()?;
    let mut scores: BTreeMap<&str, (f64, BTreeMap<&str, (usize, &str)>)> = BTreeMap::new();
    let mut collapse = BTreeMap::new();
    let mut weights = BTreeMap::new();
    let engine_hits: Vec<Vec<Hit>> = ENGINES
        .iter()
        .map(|name| parse(&data["engines"][name]["hits"]).unwrap_or_default())
        .collect();
    for (name, hits) in ENGINES.into_iter().zip(&engine_hits) {
        let mut best = HashMap::new();
        let (mut ineligible, mut duplicate) = (0, 0);
        for hit in hits {
            let Some(group) = groups.get(&hit.id) else {
                ineligible += 1;
                continue;
            };
            let rank = (hit.rank, hit.id.as_str());
            best.entry(group.as_str())
                .and_modify(|old| {
                    duplicate += 1;
                    if rank < *old {
                        *old = rank;
                    }
                })
                .or_insert(rank);
        }
        let mut stats = json!({"ineligible_dropped": ineligible,
                               "group_duplicates_dropped": duplicate, "groups_ranked": best.len()});
        let weight = data["weights"][name].as_f64().unwrap_or(1.0);
        weights.insert(name, weight);
        if !best.is_empty() && data["engines"][name]["status"] == "OK" {
            if weight == 0.0 {
                stats["zero_weight"] = json!(true);
            } else {
                let mut ordered: Vec<_> = best.into_iter().collect();
                ordered.sort_unstable_by_key(|(group, (rank, _))| (*rank, *group));
                for (index, (group, (_, id))) in ordered.into_iter().enumerate() {
                    let position = index + 1;
                    let (score, votes) = scores.entry(group).or_default();
                    *score += weight / (k + position as f64);
                    votes.insert(name, (position, id));
                }
            }
        }
        collapse.insert(name, stats);
    }
    let mut ranking: Vec<_> = scores
        .into_iter()
        .map(|(group, (score, votes))| Row {
            group_id: group.to_owned(),
            id: votes.values().min().unwrap().1.to_owned(),
            score: format!("{score:.9}").parse().unwrap_or(score),
            engines: votes.keys().map(|name| (*name).to_owned()).collect(),
            best_rank: votes
                .into_iter()
                .map(|(name, (rank, _))| (name.to_owned(), rank))
                .collect(),
        })
        .collect();
    ranking.sort_by(|a, b| b.score.total_cmp(&a.score).then_with(|| a.id.cmp(&b.id)));
    let (prompts, skills): (Vec<_>, Vec<_>) = ranking
        .iter()
        .partition(|row| kinds.get(&row.id) == Some(&"prompt"));
    let limit: usize = parse(&data["limit"])?;
    let skill_quota: isize = parse(&data["skill_quota"])?;
    let prompt_quota: isize = parse(&data["prompt_quota"])?;
    let total = limit.min(ranking.len());
    let skill_take = skills.len().min(total.min(skill_quota.max(0) as usize));
    let prompt_take = prompts
        .len()
        .min((total - skill_take).min(prompt_quota.max(0) as usize));
    let skill_take = skills.len().min(total - prompt_take);
    let prompt_take = prompts.len().min(total - skill_take);
    let mut selected = skills[..skill_take].to_vec();
    selected.extend_from_slice(&prompts[..prompt_take]);
    selected.sort_by(|a, b| b.score.total_cmp(&a.score).then_with(|| a.id.cmp(&b.id)));
    Some(
        json!({"ranking": ranking, "selected": selected, "coverage": {
        "eligible_ids": groups.len(), "fused_groups": ranking.len(), "selected": selected.len(),
        "skills_selected": skill_take, "prompts_selected": prompt_take,
        "quota": {"limit": limit, "skills": skill_quota, "prompts": prompt_quota},
        "group_collapse": collapse
    }, "weights": weights, "k": data["k"]}),
    )
}
