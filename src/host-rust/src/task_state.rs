//! Single request-local task-state contract (`skill_hub.task_state.v1`).
//! Preserves caller values verbatim; never infers or harvests external files.

use std::collections::BTreeMap;

use crate::json::{to_string_compact, JsonValue};
use crate::sha256::{is_hex64, sha256_hex};

pub const VERSION: &str = "skill_hub.task_state.v1";
pub const TEXT_FIELDS: [&str; 2] = ["current_step", "desired_output"];
pub const LIST_FIELDS: [&str; 4] = [
    "artifact_types",
    "available_tools",
    "constraints",
    "explicit_skill_ids",
];
pub const LOADED_FIELD: &str = "loaded_skill_ids_and_versions";
pub const ALL_TASK_STATE_KEYS: [&str; 7] = [
    "current_step",
    "desired_output",
    "artifact_types",
    "available_tools",
    "constraints",
    "loaded_skill_ids_and_versions",
    "explicit_skill_ids",
];

pub fn normalize_task_state(
    state: &str,
    task_state: Option<&JsonValue>,
) -> Result<JsonValue, String> {
    if state.trim().is_empty() {
        return Err("state: must be a nonempty string".to_string());
    }
    let empty = BTreeMap::new();
    let supplied = match task_state {
        None | Some(JsonValue::Null) => &empty,
        Some(JsonValue::Object(map)) => map,
        Some(_) => return Err("task_state: must be an object or null".to_string()),
    };

    for key in supplied.keys() {
        let k = key.as_str();
        if k != "schema_version" && !ALL_TASK_STATE_KEYS.contains(&k) {
            return Err(format!("{k}: unknown field"));
        }
    }

    if let Some(sv) = supplied.get("schema_version") {
        if sv.as_str() != Some(VERSION) {
            return Err("schema_version: unsupported version".to_string());
        }
    }

    let mut result = BTreeMap::new();
    result.insert(
        "schema_version".to_string(),
        JsonValue::String(VERSION.to_string()),
    );
    result.insert("state".to_string(), JsonValue::String(state.to_string()));

    for key in ALL_TASK_STATE_KEYS {
        let val = supplied.get(key).unwrap_or(&JsonValue::Null);
        if !val.is_null() {
            if TEXT_FIELDS.contains(&key) {
                if val.as_str().is_none() {
                    return Err(format!("{key}: must be text or null"));
                }
            } else {
                let arr = val
                    .as_array()
                    .ok_or_else(|| format!("{key}: must be an array or null"))?;
                if key != LOADED_FIELD {
                    if arr.iter().any(|item| item.as_str().is_none()) {
                        return Err(format!("{key}: items must be strings"));
                    }
                } else {
                    for (i, item) in arr.iter().enumerate() {
                        let field = format!("{key}[{i}]");
                        let obj = item
                            .as_object()
                            .ok_or_else(|| format!("{field}: invalid identity fields"))?;
                        for k in obj.keys() {
                            if !matches!(k.as_str(), "id" | "version" | "content_sha256") {
                                return Err(format!("{field}: invalid identity fields"));
                            }
                        }
                        match obj.get("id").and_then(|v| v.as_str()) {
                            Some(id) if !id.trim().is_empty() => {}
                            _ => return Err(format!("{field}.id: must be nonempty text")),
                        }
                        for attr in ["version", "content_sha256"] {
                            if let Some(v) = obj.get(attr) {
                                if !v.is_null() && v.as_str().is_none() {
                                    return Err(format!("{field}.{attr}: must be text or null"));
                                }
                            }
                        }
                        if let Some(sha) = obj.get("content_sha256").and_then(|v| v.as_str()) {
                            if !is_hex64(sha) {
                                return Err(format!(
                                    "{field}.content_sha256: must be a SHA256 hex digest"
                                ));
                            }
                        }
                    }
                }
            }
        }
        result.insert(key.to_string(), val.clone());
    }

    Ok(JsonValue::Object(result))
}

pub fn task_state_hash(normalized: &JsonValue) -> String {
    let raw = to_string_compact(normalized, false);
    sha256_hex(raw.as_bytes())
}

pub fn extract_task_state(payload: &JsonValue) -> Option<JsonValue> {
    let obj = payload.as_object()?;
    if let Some(ts) = obj.get("task_state") {
        return Some(ts.clone());
    }
    let mut direct = BTreeMap::new();
    for key in ALL_TASK_STATE_KEYS {
        if let Some(val) = obj.get(key) {
            direct.insert(key.to_string(), val.clone());
        }
    }
    if direct.is_empty() {
        None
    } else {
        Some(JsonValue::Object(direct))
    }
}

/// Extract bounded prior user context ONLY from explicit harness-supplied
/// `prior_context` or `conversation_history` in payload (never from disk files).
pub fn explicit_prior_context(payload: &JsonValue, prompt: &str) -> String {
    let Some(obj) = payload.as_object() else {
        return String::new();
    };
    if let Some(pc) = obj.get("prior_context").and_then(|v| v.as_str()) {
        let cleaned: String = pc.split_whitespace().collect::<Vec<_>>().join(" ");
        if !cleaned.is_empty() && cleaned != prompt.trim() {
            let clipped = clip_str(&cleaned, 360);
            return format!("\n\nPrior user context (harness-supplied):\n- {clipped}");
        }
    }
    let Some(hist) = obj.get("conversation_history").and_then(|v| v.as_array()) else {
        return String::new();
    };
    let mut snippets = Vec::new();
    let start = hist.len().saturating_sub(6);
    for msg in &hist[start..] {
        let Some(m) = msg.as_object() else { continue };
        if m.get("role").and_then(|v| v.as_str()) != Some("user") {
            continue;
        }
        let Some(content) = m.get("content").and_then(|v| v.as_str()) else {
            continue;
        };
        let trimmed = content.trim();
        if trimmed.is_empty() || trimmed == prompt.trim() {
            continue;
        }
        let collapsed: String = trimmed.split_whitespace().collect::<Vec<_>>().join(" ");
        snippets.push(clip_str(&collapsed, 180));
    }
    if snippets.is_empty() {
        return String::new();
    }
    let tail_start = snippets.len().saturating_sub(2);
    let mut out = String::from("\n\nPrior user context (harness-supplied):");
    for s in &snippets[tail_start..] {
        out.push_str("\n- ");
        out.push_str(s);
    }
    out
}

pub fn clip_str(s: &str, max_chars: usize) -> String {
    let mut count = 0;
    for (idx, _) in s.char_indices() {
        if count == max_chars {
            return format!("{}…", &s[..idx]);
        }
        count += 1;
    }
    s.to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::json::parse;

    #[test]
    fn test_normalize_and_hash_parity() {
        let norm = normalize_task_state("work", None).unwrap();
        assert_eq!(norm.get("schema_version").and_then(|v| v.as_str()), Some(VERSION));
        assert!(norm.get("constraints").unwrap().is_null());
        let h1 = task_state_hash(&norm);

        let empty_c = parse(r#"{"constraints": []}"#).unwrap();
        let norm2 = normalize_task_state("work", Some(&empty_c)).unwrap();
        let h2 = task_state_hash(&norm2);
        assert_ne!(h1, h2);
    }

    #[test]
    fn test_invalid_task_state_rejected() {
        let bad = parse(r#"{"unknown": true}"#).unwrap();
        assert!(normalize_task_state("task", Some(&bad)).is_err());
        let bad_sha = parse(r#"{"loaded_skill_ids_and_versions":[{"id":"x","content_sha256":"bad"}]}"#).unwrap();
        assert!(normalize_task_state("task", Some(&bad_sha)).is_err());
    }
}
