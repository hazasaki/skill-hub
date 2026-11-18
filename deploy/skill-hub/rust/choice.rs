use super::*;

#[derive(Deserialize)]
struct Verified {
    dependencies: Vec<String>,
    conflicts: Names,
    identity: Value,
    conflict_identity: Value,
    family: String,
    language_compatible: bool,
    capabilities: Names,
    body_bytes: usize,
    role: Option<String>,
    kind: String,
    score: Option<f64>,
}

fn closure<'a>(
    id: &'a str,
    verified: &'a BTreeMap<String, Verified>,
    active: &Names,
    ordered: &mut Vec<&'a str>,
    path: &mut HashSet<&'a str>,
    complete: &mut HashSet<&'a str>,
) -> Option<()> {
    let entry = verified.get(id)?;
    if complete.contains(id) {
        return Some(());
    }
    if path.len() >= 20 || !path.insert(id) {
        return None;
    }
    for dependency in &entry.dependencies {
        if active.contains(id) && !active.contains(dependency) {
            return None;
        }
        closure(dependency, verified, active, ordered, path, complete)?;
    }
    path.remove(id);
    complete.insert(id);
    if !active.contains(id) {
        ordered.push(id);
    }
    Some(())
}

fn declares(
    left: &Verified,
    right_id: &str,
    right: &Verified,
    verified: &BTreeMap<String, Verified>,
    identities: &Value,
) -> bool {
    left.conflicts.contains(right_id)
        || (!right.conflict_identity.is_null()
            && left.conflicts.iter().any(|id| {
                let identity = verified
                    .get(id)
                    .map_or(&identities[id], |entry| &entry.conflict_identity);
                *identity == right.conflict_identity
            }))
}

fn count_kinds<'a>(
    items: impl Iterator<Item = &'a str>,
    verified: &BTreeMap<String, Verified>,
) -> (usize, usize) {
    items.fold((0, 0), |(skills, prompts), id| {
        if verified[id].kind == "skill" {
            (skills + 1, prompts)
        } else {
            (skills, prompts + 1)
        }
    })
}

pub(super) fn select(data: &Value) -> Option<Value> {
    let verified: BTreeMap<String, Verified> = parse(&data["verified"])?;
    let active: Names = parse(&data["active"])?;
    let explicit: Names = parse(&data["explicit"])?;
    let kinds: Names = parse(&data["kinds"])?;
    let required: Names = parse(&data["required"])?;
    let step = data["step"].as_str()?;
    let (skill_limit, prompt_limit) = (
        data["limits"]["skill"].as_u64()?,
        data["limits"]["prompt"].as_u64()?,
    );
    let byte_limit = data["byte_limit"].as_u64();
    let mut ranked = Vec::new();
    let mut relevant = HashSet::new();
    let mut verified_active = HashSet::new();
    for id in &active {
        closure(id, &verified, &active, &mut Vec::new(), &mut HashSet::new(), &mut verified_active)?;
    }
    for (id, info) in &verified {
        let Some(score) = info.score else { continue };
        let mut group = Vec::new();
        closure(id, &verified, &active, &mut group, &mut HashSet::new(), &mut verified_active.clone())?;
        let cost: usize = group.iter().map(|node| verified[*node].body_bytes).sum();
        if !step.is_empty() && info.capabilities.contains(step) {
            relevant.insert(id.as_str());
        }
        ranked.push((id.as_str(), score, group, cost));
    }
    ranked.sort_by(|(a, a_score, _, a_cost), (b, b_score, _, b_cost)| {
        let preference = |id: &str| {
            let info = &verified[id];
            (
                !explicit.contains(id),
                !kinds.is_empty() && !kinds.contains(&info.kind),
                std::cmp::Reverse(required.intersection(&info.capabilities).count()),
                !relevant.contains(id),
                !info.language_compatible,
            )
        };
        preference(a)
            .cmp(&preference(b))
            .then_with(|| b_score.total_cmp(a_score))
            .then_with(|| a_cost.cmp(b_cost))
            .then_with(|| a.cmp(b))
    });
    let mut admitted = Vec::new();
    let mut chosen = Vec::new();
    let mut reasons = BTreeMap::new();
    let mut issues = BTreeMap::new();
    let mut covered = Names::new();
    let mut roles = BTreeSet::new();
    let mut served = 0;
    for node in &active {
        covered.extend(verified[node].capabilities.iter().cloned());
        roles.extend(verified[node].role.as_deref());
    }
    let conflict = |a: &str, b: &str| {
        let identities = &data["conflict_identities"];
        declares(&verified[a], b, &verified[b], &verified, identities)
            || declares(&verified[b], a, &verified[a], &verified, identities)
    };
    for (id, _, group, _) in ranked {
        if chosen.contains(&id) {
            continue;
        }
        let members: Vec<_> = group
            .iter()
            .copied()
            .chain(active.iter().map(String::as_str))
            .chain(admitted.iter().copied().filter(|node| data["compatible_menu"] == true
                && !group.contains(node)))
            .collect();
        let conflicting = group.iter().enumerate().find_map(|(i, left)| {
            members[i + 1..]
                .iter()
                .find(|right| conflict(left, right))
                .copied()
        });
        if let Some(other) = conflicting {
            issues.insert(id, ("DECLARED_CONFLICT", Some(other)));
            continue;
        }
        let mut additions: Vec<_> = group
            .iter()
            .copied()
            .filter(|node| !admitted.contains(node))
            .collect();
        if active.contains(id) && !admitted.contains(&id) {
            additions.push(id);
        }
        let (skill_count, prompt_count) =
            count_kinds(admitted.iter().chain(&additions).copied(), &verified);
        if skill_count > 12 || prompt_count > 8 {
            issues.insert(id, ("CHOICE_POOL_LIMIT", None));
            continue;
        }
        admitted.extend(additions);
        let info = &verified[id];
        let pending: Vec<_> = group
            .into_iter()
            .filter(|node| !chosen.contains(node))
            .collect();
        let cost: usize = pending.iter().map(|node| verified[*node].body_bytes).sum();
        let (skill_count, prompt_count) =
            count_kinds(chosen.iter().chain(&pending).copied(), &verified);
        let over_count = skill_count as u64 > skill_limit || prompt_count as u64 > prompt_limit;
        let already = || {
            active
                .iter()
                .map(String::as_str)
                .chain(chosen.iter().copied())
                .map(|node| &verified[node])
        };
        let is_explicit = explicit.contains(id);
        let reason = if active.contains(id) {
            "ALREADY_LOADED_EXACT"
        } else if pending
            .iter()
            .any(|node| chosen.iter().any(|other| conflict(node, other)))
        {
            "DECLARED_CONFLICT"
        } else if !is_explicit && !kinds.is_empty() && !kinds.contains(&info.kind) {
            "OTHER_RESOURCE_TYPE"
        } else if !is_explicit
            && !info.language_compatible
            && already().any(|node| {
                node.kind == info.kind
                    && node.family == info.family
                    && node.language_compatible
                    && info.capabilities.is_subset(&node.capabilities)
            })
        {
            "LANGUAGE_VARIANT"
        } else if !is_explicit
            && (info.kind == "prompt" || info.identity[2].as_str().is_some_and(|s| !s.is_empty()))
            && already().any(|node| node.identity == info.identity)
        {
            "EXACT_BODY_OVERLAP"
        } else if !is_explicit
            && !info.capabilities.is_empty()
            && info.capabilities.is_subset(&covered)
        {
            "CAPABILITY_OVERLAP"
        } else if !is_explicit
            && info
                .role
                .as_deref()
                .is_some_and(|role| roles.contains(role))
        {
            "PROMPT_ROLE_COVERED"
        } else if !is_explicit
            && !relevant.is_empty()
            && !info.capabilities.is_empty()
            && !relevant.contains(id)
        {
            "OTHER_STEP"
        } else if over_count {
            "LOAD_LIMIT"
        } else if byte_limit.is_some_and(|limit| {
            (served != 0 || pending.len() > 1) && (served + cost) as u64 > limit
        }) {
            "BYTE_BUDGET"
        } else {
            let reason = if is_explicit {
                "EXPLICIT_REQUEST"
            } else if required
                .difference(&covered)
                .any(|cap| info.capabilities.contains(cap))
            {
                "FILLS_REQUIRED_CAPABILITY"
            } else if !info.capabilities.is_empty() {
                "ADDS_CAPABILITY"
            } else if info.role.is_some() {
                "ADDS_PROMPT_ROLE"
            } else {
                "SCORE_ONLY"
            };
            for node in pending {
                chosen.push(node);
                reasons.insert(
                    node,
                    if node == id {
                        reason
                    } else {
                        "REQUIRED_DEPENDENCY"
                    },
                );
                covered.extend(verified[node].capabilities.iter().cloned());
                roles.extend(verified[node].role.as_deref());
            }
            served += cost;
            reason
        };
        reasons.insert(id, reason);
    }
    Some(json!({"admitted":admitted,"chosen":chosen,"reasons":reasons,"issues":issues}))
}
