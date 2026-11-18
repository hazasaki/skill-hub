//! `skill_hub_host`: Rust host CLI for Skill Hub task-state, receipt verification,
//! and verbatim native hook delivery.

use std::collections::{BTreeMap, HashSet};
use std::env;
use std::fs::OpenOptions;
use std::io::{self, Read, Write};
use std::path::PathBuf;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use skill_hub_host::http_client::{post_json, HttpError};
use skill_hub_host::json::{parse, to_string_compact, JsonValue};
use skill_hub_host::receipt::{
    format_choice_pool, format_resource_block, load_session_state, save_session_state,
    verify_receipt_body, HEADER,
};
use skill_hub_host::sha256::sha256_hex;
use skill_hub_host::task_state::{
    clip_str, explicit_prior_context, extract_task_state, normalize_task_state, task_state_hash,
};

const DEFAULT_ROUTE_URL: &str = "http://127.0.0.1:8765/route_task";
const DEFAULT_DEADLINE_SECS: f64 = 20.0;
const SCHEMA_VERSION: &str = "skill_hub.nudge.v3";
const ENGINE: &str = "skill_hub_host_rust";

const SKIP_WORDS: [&str; 20] = [
    "ok", "oke", "okay", "yes", "no", "y", "n", "thanks", "thank you", "thx",
    "cam on", "dung", "sai", "stop", "dung lai", "continue", "tiep tuc", "tiep", "go on", "camon",
];

fn fold_ascii_vi(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for ch in s.chars() {
        let mapped = match ch {
            'á' | 'à' | 'ả' | 'ã' | 'ạ' | 'ă' | 'ắ' | 'ằ' | 'ẳ' | 'ẵ' | 'ặ' | 'â' | 'ấ'
            | 'ầ' | 'ẩ' | 'ẫ' | 'ậ' | 'Á' | 'À' | 'Ả' | 'Ã' | 'Ạ' | 'Ă' | 'Ắ' | 'Ằ'
            | 'Ẳ' | 'Ẵ' | 'Ặ' | 'Â' | 'Ấ' | 'Ầ' | 'Ẩ' | 'Ẫ' | 'Ậ' => 'a',
            'đ' | 'Đ' => 'd',
            'é' | 'è' | 'ẻ' | 'ẽ' | 'ẹ' | 'ê' | 'ế' | 'ề' | 'ể' | 'ễ' | 'ệ' | 'É' | 'È'
            | 'Ẻ' | 'Ẽ' | 'Ẹ' | 'Ê' | 'Ế' | 'Ề' | 'Ể' | 'Ễ' | 'Ệ' => 'e',
            'í' | 'ì' | 'ỉ' | 'ĩ' | 'ị' | 'Í' | 'Ì' | 'Ỉ' | 'Ĩ' | 'Ị' => 'i',
            'ó' | 'ò' | 'ỏ' | 'õ' | 'ọ' | 'ô' | 'ố' | 'ồ' | 'ổ' | 'ỗ' | 'ộ' | 'ơ' | 'ớ'
            | 'ờ' | 'ở' | 'ỡ' | 'ợ' | 'Ó' | 'Ò' | 'Ỏ' | 'Õ' | 'Ọ' | 'Ô' | 'Ố' | 'Ồ'
            | 'Ổ' | 'Ỗ' | 'Ộ' | 'Ơ' | 'Ớ' | 'Ờ' | 'Ở' | 'Ỡ' | 'Ợ' => 'o',
            'ú' | 'ù' | 'ủ' | 'ũ' | 'ụ' | 'ư' | 'ứ' | 'ừ' | 'ử' | 'ữ' | 'ự' | 'Ú' | 'Ù'
            | 'Ủ' | 'Ũ' | 'Ụ' | 'Ư' | 'Ứ' | 'Ừ' | 'Ử' | 'Ữ' | 'Ự' => 'u',
            'ý' | 'ỳ' | 'ỷ' | 'ỹ' | 'ỵ' | 'Ý' | 'Ỳ' | 'Ỷ' | 'Ỹ' | 'Ỵ' => 'y',
            c => c,
        };
        for lc in mapped.to_lowercase() {
            out.push(lc);
        }
    }
    out
}

fn skip_reason(prompt: &str) -> Option<&'static str> {
    let trimmed = prompt.trim();
    if trimmed.is_empty() {
        return Some("EMPTY_PROMPT");
    }
    if trimmed.starts_with('/') {
        return Some("SLASH_COMMAND");
    }
    let folded = fold_ascii_vi(trimmed);
    let stripped = folded.trim_matches(|c: char| c == ' ' || c == '.' || c == '!' || c == '?');
    if SKIP_WORDS.contains(&stripped) {
        return Some("ACKNOWLEDGEMENT");
    }
    None
}

fn read_stdin() -> Result<String, String> {
    let mut buf = String::new();
    io::stdin()
        .read_to_string(&mut buf)
        .map_err(|e| format!("stdin read failed: {e}"))?;
    Ok(buf)
}

fn cmd_normalize_task_state() -> i32 {
    let raw = match read_stdin() {
        Ok(s) => s,
        Err(e) => {
            println!("{{\"error\":\"INVALID_INPUT\",\"detail\":{:?}}}", e);
            return 1;
        }
    };
    let val = match parse(&raw) {
        Ok(v) => v,
        Err(e) => {
            println!("{{\"error\":\"INVALID_JSON\",\"detail\":\"{}\"}}", e);
            return 1;
        }
    };
    let state = val.get("state").and_then(|v| v.as_str()).unwrap_or("");
    let ts = val.get("task_state");
    match normalize_task_state(state, ts) {
        Ok(norm) => {
            let hash = task_state_hash(&norm);
            let mut out = BTreeMap::new();
            out.insert("task_state".to_string(), norm);
            out.insert("task_state_sha256".to_string(), JsonValue::String(hash));
            println!("{}", to_string_compact(&JsonValue::Object(out), false));
            0
        }
        Err(err) => {
            let mut out = BTreeMap::new();
            out.insert(
                "error".to_string(),
                JsonValue::String("INVALID_TASK_STATE".to_string()),
            );
            out.insert("detail".to_string(), JsonValue::String(err));
            println!("{}", to_string_compact(&JsonValue::Object(out), false));
            1
        }
    }
}

fn cmd_verify_receipt() -> i32 {
    let raw = match read_stdin() {
        Ok(s) => s,
        Err(e) => {
            println!("{{\"ok\":false,\"error\":\"{}\"}}", e);
            return 1;
        }
    };
    let val = match parse(&raw) {
        Ok(v) => v,
        Err(e) => {
            println!("{{\"ok\":false,\"error\":\"{}\"}}", e);
            return 1;
        }
    };
    match verify_receipt_body(&val) {
        Ok(body) => {
            let mut out = BTreeMap::new();
            out.insert("ok".to_string(), JsonValue::Bool(true));
            out.insert(
                "body_bytes".to_string(),
                JsonValue::from_u64(body.len() as u64),
            );
            out.insert(
                "body_raw_sha256".to_string(),
                JsonValue::String(sha256_hex(body.as_bytes())),
            );
            println!("{}", to_string_compact(&JsonValue::Object(out), false));
            0
        }
        Err(code) => {
            println!("{{\"ok\":false,\"error\":\"{code}\"}}");
            1
        }
    }
}

fn record_status(
    status: &str,
    reason: Option<&str>,
    stage: &str,
    session_hash: Option<&str>,
    turn_hash: Option<&str>,
    delivered_ids: &[String],
    bytes: usize,
    payload_chars: usize,
    payload_bytes: usize,
    elapsed_ms: f64,
) {
    let sink = env::var("SKILL_HUB_NUDGE_STATUS_FILE").ok().filter(|s| !s.is_empty());
    let Some(sink_path) = sink else { return };
    let mut rec = BTreeMap::new();
    rec.insert("schema_version".to_string(), JsonValue::String(SCHEMA_VERSION.to_string()));
    rec.insert("engine".to_string(), JsonValue::String(ENGINE.to_string()));
    rec.insert("status".to_string(), JsonValue::String(status.to_string()));
    rec.insert(
        "reason".to_string(),
        reason.map(|r| JsonValue::String(r.to_string())).unwrap_or(JsonValue::Null),
    );
    rec.insert("stage".to_string(), JsonValue::String(stage.to_string()));
    rec.insert(
        "host_context_observed".to_string(),
        JsonValue::String("UNKNOWN".to_string()),
    );
    rec.insert(
        "session".to_string(),
        session_hash.map(|s| JsonValue::String(s.to_string())).unwrap_or(JsonValue::Null),
    );
    rec.insert(
        "turn".to_string(),
        turn_hash.map(|t| JsonValue::String(t.to_string())).unwrap_or(JsonValue::Null),
    );
    rec.insert(
        "delivered".to_string(),
        JsonValue::Array(delivered_ids.iter().map(|s| JsonValue::String(s.clone())).collect()),
    );
    rec.insert("bytes".to_string(), JsonValue::from_u64(bytes as u64));
    rec.insert("payload_chars".to_string(), JsonValue::from_u64(payload_chars as u64));
    rec.insert("payload_bytes".to_string(), JsonValue::from_u64(payload_bytes as u64));
    rec.insert("elapsed_ms".to_string(), JsonValue::from_f64((elapsed_ms * 100.0).round() / 100.0));
    let secs = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
    rec.insert("unix_ts".to_string(), JsonValue::from_u64(secs));

    if let Some(parent) = PathBuf::from(&sink_path).parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    if let Ok(mut fh) = OpenOptions::new().create(true).append(true).open(&sink_path) {
        let _ = writeln!(fh, "{}", to_string_compact(&JsonValue::Object(rec), true));
    }
}

fn cmd_nudge() -> i32 {
    let raw = match read_stdin() {
        Ok(s) if !s.trim().is_empty() => s,
        _ => return 0,
    };
    let payload = match parse(&raw) {
        Ok(v) => v,
        Err(_) => return 0,
    };
    let event = payload
        .get("hook_event_name")
        .or_else(|| payload.get("hookEventName"))
        .and_then(|v| v.as_str());
    if !matches!(event, None | Some("UserPromptSubmit")) {
        return 0;
    }

    let prompt = ["prompt", "user_prompt", "userPrompt"]
        .iter()
        .find_map(|k| payload.get(k).and_then(|v| v.as_str()));
    let Some(prompt) = prompt else { return 0 };

    let session_raw = payload
        .get("session_id")
        .or_else(|| payload.get("conversationId"))
        .or_else(|| payload.get("sessionId"))
        .and_then(|v| v.as_str());
    let session_hash = session_raw.map(|s| sha256_hex(s.as_bytes())[..16].to_string());
    let turn_raw = payload
        .get("turn_id")
        .or_else(|| payload.get("turnId"))
        .or_else(|| payload.get("executionNum"))
        .and_then(|v| match v {
            JsonValue::String(s) => Some(s.clone()),
            JsonValue::Number(n) => Some(n.raw.clone()),
            _ => None,
        });
    let turn_hash = turn_raw.as_ref().map(|t| sha256_hex(t.as_bytes())[..16].to_string());

    if let Some(reason) = skip_reason(prompt) {
        record_status("SKIPPED", Some(reason), "ROUTED", session_hash.as_deref(), turn_hash.as_deref(), &[], 0, 0, 0, 0.0);
        return 0;
    }

    let state_dir = env::var("SKILL_HUB_NUDGE_STATE_DIR")
        .ok()
        .filter(|s| !s.is_empty())
        .or_else(|| {
            env::var("SKILL_HUB_NUDGE_STATUS_FILE")
                .ok()
                .filter(|s| !s.is_empty())
                .and_then(|p| PathBuf::from(p).parent().map(|d| d.join("sessions").to_string_lossy().into_owned()))
        });
    let session_state = load_session_state(session_hash.as_deref(), state_dir.as_deref());

    let mut task_state = extract_task_state(&payload);
    if !session_state.loaded.is_empty() {
        match &mut task_state {
            None => {
                let mut m = BTreeMap::new();
                m.insert(
                    "loaded_skill_ids_and_versions".to_string(),
                    JsonValue::Array(session_state.loaded.clone()),
                );
                task_state = Some(JsonValue::Object(m));
            }
            Some(JsonValue::Object(m)) if !m.contains_key("loaded_skill_ids_and_versions") => {
                m.insert(
                    "loaded_skill_ids_and_versions".to_string(),
                    JsonValue::Array(session_state.loaded.clone()),
                );
            }
            _ => {}
        }
    }

    if let Some(ref ts) = task_state {
        if let Err(err) = normalize_task_state(prompt, Some(ts)) {
            record_status("ERROR", Some(&format!("INVALID_TASK_STATE: {err}")), "ROUTED", session_hash.as_deref(), turn_hash.as_deref(), &[], 0, 0, 0, 0.0);
            return 0;
        }
    }

    let route_url = env::var("SKILL_HUB_ROUTE_URL")
        .ok()
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| DEFAULT_ROUTE_URL.to_string());
    let retrieval_mode = env::var("SKILL_HUB_RETRIEVAL_MODE")
        .ok()
        .map(|s| s.trim().to_ascii_lowercase())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "tempr".to_string());
    let deadline_secs = env::var("SKILL_HUB_NUDGE_DEADLINE_SECONDS")
        .ok()
        .and_then(|s| s.parse::<f64>().ok())
        .filter(|v| *v > 0.0)
        .unwrap_or(DEFAULT_DEADLINE_SECS);

    let query_text = format!("{prompt}{}", explicit_prior_context(&payload, prompt));
    let mut req = BTreeMap::new();
    req.insert("state".to_string(), JsonValue::String(query_text));
    req.insert("retrieval_mode".to_string(), JsonValue::String(retrieval_mode));
    req.insert("response_mode".to_string(), JsonValue::String("full".to_string()));
    if let Some(ts) = task_state {
        req.insert("task_state".to_string(), ts);
    }

    let started = Instant::now();
    let answer = match post_json(&route_url, &to_string_compact(&JsonValue::Object(req), false), Duration::from_secs_f64(deadline_secs)) {
        Ok((_code, val)) => val,
        Err(HttpError::Timeout) => {
            record_status("TIMEOUT", Some("CONTAINER_TIMEOUT"), "ROUTED", session_hash.as_deref(), turn_hash.as_deref(), &[], 0, 0, 0, started.elapsed().as_secs_f64() * 1000.0);
            return 0;
        }
        Err(HttpError::Unavailable(_)) => {
            record_status("ERROR", Some("CONTAINER_UNAVAILABLE"), "ROUTED", session_hash.as_deref(), turn_hash.as_deref(), &[], 0, 0, 0, started.elapsed().as_secs_f64() * 1000.0);
            return 0;
        }
        Err(HttpError::InvalidJson) => {
            record_status("ERROR", Some("CONTAINER_INVALID_JSON"), "ROUTED", session_hash.as_deref(), turn_hash.as_deref(), &[], 0, 0, 0, started.elapsed().as_secs_f64() * 1000.0);
            return 0;
        }
        Err(HttpError::InvalidShape) => {
            record_status("ERROR", Some("CONTAINER_INVALID_SHAPE"), "ROUTED", session_hash.as_deref(), turn_hash.as_deref(), &[], 0, 0, 0, started.elapsed().as_secs_f64() * 1000.0);
            return 0;
        }
        Err(HttpError::HttpStatus(_)) => {
            record_status("ERROR", Some("CONTAINER_HTTP_ERROR"), "ROUTED", session_hash.as_deref(), turn_hash.as_deref(), &[], 0, 0, 0, started.elapsed().as_secs_f64() * 1000.0);
            return 0;
        }
    };

    if let Some(err) = answer.get("error").and_then(|v| v.as_str()) {
        record_status("ERROR", Some(&clip_str(err, 120)), "ROUTED", session_hash.as_deref(), turn_hash.as_deref(), &[], 0, 0, 0, started.elapsed().as_secs_f64() * 1000.0);
        return 0;
    }

    let loaded_arr = answer
        .get("loads")
        .and_then(|v| v.get("loaded"))
        .and_then(|v| v.as_array())
        .unwrap_or(&[]);

    let mut blocks = Vec::new();
    let mut delivered_items = Vec::new();
    let mut delivered_ids = Vec::new();
    let mut delivered_qids = HashSet::new();
    let mut body_bytes_total = 0usize;

    for receipt in loaded_arr {
        if let Ok(body) = verify_receipt_body(receipt) {
            let rtype = receipt.get("type").and_then(|v| v.as_str()).unwrap_or("skill");
            let rid = receipt.get("id").and_then(|v| v.as_str()).unwrap_or("");
            let sha = receipt
                .get("body_raw_sha256")
                .or_else(|| receipt.get("served_content_sha256"))
                .or_else(|| receipt.get("body_sha256"))
                .and_then(|v| v.as_str())
                .unwrap_or("");
            if !session_state.seen.is_empty()
                && session_state.seen.contains(&(rtype.to_string(), rid.to_string(), sha.to_string()))
            {
                continue;
            }
            blocks.push(format_resource_block(receipt, body));
            body_bytes_total += body.len();
            delivered_ids.push(rid.to_string());
            delivered_qids.insert(rid.to_string());
            delivered_qids.insert(format!("{rtype}:{rid}"));
            delivered_items.push(receipt.clone());
        }
    }

    let pool_text = format_choice_pool(answer.get("choice_pool"), &delivered_qids);
    let mut context_text = if !blocks.is_empty() {
        let mut s = format!("{HEADER}\n\n{}", blocks.join("\n\n"));
        if !pool_text.is_empty() {
            s.push_str("\n\n");
            s.push_str(&pool_text);
        }
        s
    } else if !pool_text.is_empty()
        && answer
            .get("choice_pool")
            .map(|p| {
                p.get("skills").and_then(|v| v.as_array()).is_some_and(|a| !a.is_empty())
                    || p.get("prompts").and_then(|v| v.as_array()).is_some_and(|a| !a.is_empty())
            })
            .unwrap_or(false)
    {
        pool_text
    } else {
        let outcome = answer.get("outcome").and_then(|v| v.as_str()).unwrap_or("NO_SELECTION");
        record_status("VALID_NO_SELECTION", Some(outcome), "SELECTED", session_hash.as_deref(), turn_hash.as_deref(), &[], 0, 0, 0, started.elapsed().as_secs_f64() * 1000.0);
        return 0;
    };

    let mut specific = BTreeMap::new();
    specific.insert(
        "hookEventName".to_string(),
        JsonValue::String("UserPromptSubmit".to_string()),
    );
    specific.insert(
        "additionalContext".to_string(),
        JsonValue::String(std::mem::take(&mut context_text)),
    );
    let mut out = BTreeMap::new();
    out.insert("hookSpecificOutput".to_string(), JsonValue::Object(specific));
    let out_val = JsonValue::Object(out);
    let out_json = to_string_compact(&out_val, false);
    if let Some(JsonValue::String(ref ctx)) = out_val.get("hookSpecificOutput").and_then(|v| v.get("additionalContext")) {
        let reason = if delivered_items.is_empty() { "CHOICE_POOL_DELIVERED" } else { "BODY_DELIVERED" };
        record_status("EMITTED", Some(reason), "EMITTED", session_hash.as_deref(), turn_hash.as_deref(), &delivered_ids, body_bytes_total, ctx.chars().count(), ctx.len(), started.elapsed().as_secs_f64() * 1000.0);
    }
    if !delivered_items.is_empty() {
        save_session_state(&session_state, &delivered_items);
    }
    print!("{out_json}");
    let _ = io::stdout().flush();
    0
}

fn main() {
    let args: Vec<String> = env::args().collect();
    let sub = args.get(1).map(|s| s.as_str()).unwrap_or("nudge");
    let code = match sub {
        "nudge" => cmd_nudge(),
        "normalize-task-state" => cmd_normalize_task_state(),
        "verify-receipt" => cmd_verify_receipt(),
        _ => {
            eprintln!("usage: skill_hub_host [nudge|normalize-task-state|verify-receipt]");
            2
        }
    };
    std::process::exit(code);
}
