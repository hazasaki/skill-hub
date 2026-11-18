use super::{catch_unwind, slice, AssertUnwindSafe, Sha256};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

#[path = "choice.rs"]
mod choice;
#[path = "fusion.rs"]
mod fusion;
#[path = "preparation.rs"]
mod preparation;
#[path = "ranking.rs"]
mod ranking;

type Names = BTreeSet<String>;
type Ranks = BTreeMap<String, BTreeMap<String, usize>>;

fn parse<T: serde::de::DeserializeOwned>(value: &Value) -> Option<T> {
    serde_json::from_value(value.clone()).ok()
}

fn valid_sha(value: &Value) -> bool {
    value.as_str().is_some_and(|s| {
        s.len() == 64
            && s.bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    })
}

fn digest(bytes: &[u8]) -> String {
    let mut sha = Sha256::new();
    sha.update(bytes);
    sha.finish()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn dispatch(op: u32, bytes: &[u8]) -> Option<Value> {
    if op == 0 {
        return Some(json!(digest(bytes)));
    }
    let data: Value = serde_json::from_slice(bytes).ok()?;
    match op {
        1 => fusion::fuse(&data),
        3 => preparation::coalesce(&data),
        4 => preparation::stage_init(&data),
        5 => preparation::stage_next(&data),
        6 => choice::select(&data),
        7 => preparation::cache_identity(&data),
        8 => preparation::scores(&data),
        9 => preparation::expansion_query(&data),
        10 => preparation::expand(&data),
        11 => preparation::interleave(&data),
        12 => ranking::metadata(&data),
        13 => ranking::clusters(&data),
        14 => ranking::equivalence(&data),
        15 => adaptive_round(&data),
        16 => Some(json!(data.as_array()?.iter().filter_map(Value::as_str).any(|constraint| {
            matches!(constraint.trim().to_lowercase().as_str(),
                "do not use skills or prompts" | "không dùng skill hoặc prompt")
        }))),
        _ => None,
    }
}

fn adaptive_round(data: &Value) -> Option<Value> {
    use super::adaptive_shortlist::{
        after_round, after_verified_round, Attempt, Candidate, Coefficients, Kind, KindStats, Round,
    };
    let round = match data["cap"].as_u64()? {
        24 => Round::First,
        48 => Round::Second,
        96 => Round::Last,
        _ => return None,
    };
    let pool: Vec<_> = data["pool"].as_array()?.iter().map(|row| {
        Some(Candidate {
            id: row["id"].as_str()?,
            kind: match row["kind"].as_str()? {
                "skill" => Kind::Skill,
                "prompt" => Kind::Prompt,
                _ => return None,
            },
        })
    }).collect::<Option<_>>()?;
    let attempts: Vec<_> = data["attempts"].as_array()?.iter().map(|row| {
        let score = row.get("score")?;
        Some(Attempt { id: row["id"].as_str()?, score: if score.is_null() {
            None
        } else {
            Some(score.as_f64()?)
        } })
    }).collect::<Option<_>>()?;
    let coefficients = Coefficients { k: data["k"].as_f64()?, h: data["h"].as_f64()? };
    let deadline = data["deadline"].as_bool()?;
    let no_resource = data["no_resource_evidence"].as_bool()?;
    let result = if let Some(counts) = data.get("verified_counts") {
        after_verified_round(round, &pool, &attempts, coefficients, deadline, no_resource,
                             Some(parse(counts)?))
    } else {
        after_round(round, &pool, &attempts, coefficients, deadline, no_resource)
    }.ok()?;
    let stats = |item: KindStats<'_>| json!({
        "n": item.n, "median": item.median, "mad": item.mad, "weight": item.weight,
        "threshold": item.threshold,
        "eligible_ids": item.eligible.iter().map(|row| row.id).collect::<Vec<_>>(),
    });
    Some(json!({
        "skills": stats(result.skills), "prompts": stats(result.prompts),
        "reason": format!("{:?}", result.reason), "next_cap": result.next.map(Round::cap),
        "next_ids": result.to_score.iter().map(|row| row.id).collect::<Vec<_>>(),
    }))
}

#[no_mangle]
pub unsafe extern "C" fn skill_hub_decide_v1(
    op: u32,
    input: *const u8,
    size: usize,
    out: *mut *mut u8,
    out_size: *mut usize,
) -> i32 {
    if input.is_null() || out.is_null() || out_size.is_null() || size > 32 * 1024 * 1024 {
        return 1;
    }
    *out = std::ptr::null_mut();
    *out_size = 0;
    match catch_unwind(AssertUnwindSafe(|| {
        let result = dispatch(op, slice::from_raw_parts(input, size))?;
        Some(serde_json::to_vec(&result).ok()?.into_boxed_slice())
    })) {
        Ok(Some(buffer)) => {
            *out_size = buffer.len();
            *out = Box::into_raw(buffer) as *mut u8;
            0
        }
        _ => 2,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn native_digest_matches_sha256_conformance_vector() {
        assert_eq!(
            digest(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn native_scores_preserve_values_adjacent_to_the_skill_threshold() {
        let inputs = br#"{"ids":["skill:a","skill:b"],"answers":{"skill:a":{"type":"noul","noul":0.7999999999999999},"skill:b":{"type":"noul","noul":0.8000000000000002}}}"#;
        let output = dispatch(8, inputs).unwrap();
        assert!(output["skill:a"].as_f64().unwrap() < 0.8);
        assert!(output["skill:b"].as_f64().unwrap() > 0.8);
        assert_eq!(
            output["skill:b"].as_f64().unwrap().to_bits(),
            0.8000000000000002_f64.to_bits()
        );
    }

    #[test]
    fn native_operations_reject_malformed_json() {
        assert!(dispatch(1, b"[broken").is_none());
        assert!(dispatch(100, b"null").is_none());
    }
}
