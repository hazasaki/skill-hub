use std::io::{Read, Write};
use std::net::TcpListener;
use std::process::{Command, Stdio};
use std::thread;

use skill_hub_host::json::{parse, to_string_compact, JsonValue};
use skill_hub_host::receipt::{body_digest, format_resource_block, verify_receipt_body, REQUIRED_CHECKS};
use skill_hub_host::sha256::sha256_hex;
use skill_hub_host::task_state::{normalize_task_state, task_state_hash};

fn make_verified_receipt(id: &str, rtype: &str, body: &str) -> String {
    let lf_sha = body_digest(body);
    let raw_sha = sha256_hex(body.as_bytes());
    let checks: Vec<String> = REQUIRED_CHECKS.iter().map(|c| format!("\"{c}\"")).collect();
    let pinned = if rtype == "prompt" {
        format!(",\"pinned_content_sha256\":\"{lf_sha}\"")
    } else {
        String::new()
    };
    let body_json = to_string_compact(&JsonValue::String(body.to_string()), false);
    format!(
        "{{\"id\":\"{id}\",\"type\":\"{rtype}\",\"body\":{body_json},\"load_verified\":true,\"checks\":[{}],\"body_sha256\":\"{lf_sha}\",\"body_raw_sha256\":\"{raw_sha}\",\"served_content_sha256\":\"{lf_sha}\"{pinned}}}",
        checks.join(",")
    )
}

#[test]
fn test_over_16kib_and_tamper_rejection() {
    let big_skill = format!("{}\nEND_SKILL_RUST_TEST", "A".repeat(18000));
    let receipt_json = make_verified_receipt("rust-big", "skill", &big_skill);
    let val = parse(&receipt_json).unwrap();
    let verified = verify_receipt_body(&val).unwrap();
    assert_eq!(verified, big_skill);
    let block = format_resource_block(&val, verified);
    assert!(block.contains(&sha256_hex(big_skill.as_bytes())));
    assert!(block.contains("END_SKILL_RUST_TEST"));

    // Tampered body must be rejected
    let tampered = receipt_json.replace("END_SKILL_RUST_TEST", "TAMPERED_BODY_XXXX");
    let tampered_val = parse(&tampered).unwrap();
    assert_eq!(verify_receipt_body(&tampered_val), Err("BODY_HASH_MISMATCH"));
}

#[test]
fn test_cli_nudge_over_http_and_normalize_task_state() {
    let bin = env!("CARGO_BIN_EXE_skill_hub_host");

    // 1. Test normalize-task-state CLI parity
    let mut norm_child = Command::new(bin)
        .arg("normalize-task-state")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    norm_child
        .stdin
        .take()
        .unwrap()
        .write_all(br#"{"state":"test","task_state":{"available_tools":["read_file"],"explicit_skill_ids":["alias-1"]}}"#)
        .unwrap();
    let norm_out = norm_child.wait_with_output().unwrap();
    assert!(norm_out.status.success());
    let norm_json = parse(&String::from_utf8_lossy(&norm_out.stdout)).unwrap();
    let py_equiv = normalize_task_state(
        "test",
        Some(&parse(r#"{"available_tools":["read_file"],"explicit_skill_ids":["alias-1"]}"#).unwrap()),
    )
    .unwrap();
    assert_eq!(
        norm_json.get("task_state_sha256").and_then(|v| v.as_str()),
        Some(task_state_hash(&py_equiv).as_str())
    );

    // 2. Test nudge CLI over real localhost HTTP with >16KiB skill + prompt + choice_pool
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let big_skill = format!("{}🧪\r\nEND_RUST_SKILL", "X".repeat(17000));
    let prompt_body = "Prompt body\nEND_RUST_PROMPT";
    let r1 = make_verified_receipt("big-skill", "skill", &big_skill);
    let r2 = make_verified_receipt("small-prompt", "prompt", prompt_body);
    let resp_body = format!(
        "{{\"outcome\":\"SELECTED\",\"loads\":{{\"loaded\":[{r1},{r2}]}},\"choice_pool\":{{\"skills\":[{{\"id\":\"skill:big-skill\",\"resource_id\":\"big-skill\",\"score\":0.95,\"recommended\":true}}],\"prompts\":[{{\"id\":\"prompt:small-prompt\",\"resource_id\":\"small-prompt\",\"score\":0.88,\"recommended\":true}}],\"excluded\":[]}}}}"
    );

    let srv = thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let mut req_buf = [0u8; 4096];
        let _ = stream.read(&mut req_buf);
        let http = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            resp_body.len(),
            resp_body
        );
        stream.write_all(http.as_bytes()).unwrap();
    });

    let mut nudge_child = Command::new(bin)
        .arg("nudge")
        .env("SKILL_HUB_ROUTE_URL", format!("http://127.0.0.1:{port}/route_task"))
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    nudge_child
        .stdin
        .take()
        .unwrap()
        .write_all(br#"{"hook_event_name":"UserPromptSubmit","prompt":"design api","explicit_skill_ids":["alias-1"]}"#)
        .unwrap();
    let nudge_out = nudge_child.wait_with_output().unwrap();
    srv.join().unwrap();

    assert!(nudge_out.status.success());
    let out_val = parse(&String::from_utf8_lossy(&nudge_out.stdout)).unwrap();
    let ctx = out_val
        .get("hookSpecificOutput")
        .and_then(|v| v.get("additionalContext"))
        .and_then(|v| v.as_str())
        .unwrap();
    assert!(ctx.contains(&big_skill));
    assert!(ctx.contains(prompt_body));
    assert!(ctx.contains(&sha256_hex(big_skill.as_bytes())));
    assert!(ctx.contains("SKILL HUB CHOICE POOL"));
}
