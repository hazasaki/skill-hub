use std::{slice, str};

const MAX_BYTES: usize = 64 * 1024 * 1024;

fn prefix_len(input: &[u8]) -> Option<usize> {
    match input.first() {
        Some(b'r') if input.starts_with(b"references/") => Some(11),
        Some(b'r') if input.starts_with(b"resources/") => Some(10),
        Some(b's') if input.starts_with(b"scripts/") => Some(8),
        _ => None,
    }
}

fn ascii_paths(input: &[u8], emit: &mut impl FnMut(usize, usize), unicode_fallback: bool) -> bool {
    let mut i = 0;
    while i < input.len() {
        if let Some(prefix) = prefix_len(&input[i..]) {
            // Python owns the Unicode word table; defer to it for non-ASCII left context.
            if unicode_fallback && i > 0 && !input[i - 1].is_ascii() { return false; }
            if i > 0 && (input[i - 1].is_ascii_alphanumeric() || b"_.-".contains(&input[i - 1])) {
                i += 1;
                continue;
            }
            let mut end = i + prefix;
            while end < input.len() && (input[end].is_ascii_alphanumeric() || b"_.-/".contains(&input[end])) {
                end += 1;
            }
            if end > i + prefix {
                emit(i, end);
                i = end;
                continue;
            }
        }
        i += 1;
    }
    true
}

// Mirrors the second REFERENCE_PATTERNS expression, including Unicode, spaces and newlines.
fn markdown_paths(input: &[u8], emit: &mut impl FnMut(usize, usize)) {
    let mut pos = 0;
    while let Some(open) = input[pos..].iter().position(|b| *b == b'[') {
        pos += open + 1;
        let Some(close) = input[pos..].iter().position(|b| *b == b']') else { break };
        pos += close + 1;
        if input.get(pos) != Some(&b'(') { continue; }
        let start = pos + 1;
        let Some(prefix) = prefix_len(&input[start..]) else { continue };
        let content = start + prefix;
        let Some(rest) = input[content..].iter().position(|b| *b == b')') else { break };
        if rest > 0 {
            emit(start, content + rest);
            pos = content + rest + 1;
        }
    }
}

// URL markers are ASCII. The URI tail uses Python's Unicode whitespace semantics.
fn remote_spans(text: &str) -> Vec<(usize, usize)> {
    let bytes = text.as_bytes();
    let mut spans = Vec::new();
    let mut brackets = Vec::new();
    let mut pos = 0;
    while pos < bytes.len() {
        let start = pos;
        let mut scheme = start;
        match bytes[pos] {
            b':' if bytes[pos..].starts_with(b"://") => {
                pos += 3;
                while scheme > 0 && (bytes[scheme - 1].is_ascii_alphanumeric() ||
                                     b"+.-".contains(&bytes[scheme - 1])) {
                    scheme -= 1;
                }
                if !bytes[scheme..start].iter().any(u8::is_ascii_alphabetic) { continue; }
            }
            b'w' | b'W' if bytes.get(pos..pos + 4).is_some_and(|x| x.eq_ignore_ascii_case(b"www.")) => {
                pos += 4;
            }
            _ => { pos += 1; continue; }
        }
        let quoted = scheme > 0 && bytes[scheme - 1] == b'\'';
        brackets.clear();
        while pos < bytes.len() {
            let b = bytes[pos];
            match b {
                b'(' | b'[' => brackets.push(b),
                b')' if brackets.pop() != Some(b'(') => break,
                b']' if brackets.pop() != Some(b'[') => break,
                b'\'' if quoted => break,
                b'<' | b'>' | b'"' | 96 | b' ' | 9..=13 | 28..=31 => break,
                128..=255 => {
                    let c = text[pos..].chars().next().unwrap();
                    if c.is_whitespace() { break; }
                    pos += c.len_utf8();
                    continue;
                }
                _ => {}
            }
            pos += 1;
        }
        spans.push((start, pos));
    }
    spans
}

unsafe fn scan(data: *const u8, len: usize, output: *mut usize, capacity: usize, markdown: bool, unicode_fallback: bool) -> usize {
    if data.is_null() || output.is_null() || len > MAX_BYTES || capacity > isize::MAX as usize / 2 / size_of::<usize>() {
        return usize::MAX;
    }
    let input = slice::from_raw_parts(data, len);
    let spans = if markdown {
        let Ok(text) = str::from_utf8(input) else { return usize::MAX };
        remote_spans(text)
    } else {
        Vec::new()
    };
    let mut count = 0;
    let mut emit = |start, end| {
        let i = spans.partition_point(|(_, end)| *end <= start);
        if spans.get(i).is_some_and(|(begin, _)| *begin <= start) { return; }
        if count < capacity {
            *output.add(count * 2) = start;
            *output.add(count * 2 + 1) = end;
        }
        count += 1;
    };
    if !ascii_paths(input, &mut emit, unicode_fallback) { return usize::MAX; }
    if markdown { markdown_paths(input, &mut emit); }
    count
}

#[no_mangle]
pub unsafe extern "C" fn skill_hub_reference_offsets(
    data: *const u8, len: usize, output: *mut usize, capacity: usize,
) -> usize {
    scan(data, len, output, capacity, false, false)
}

#[no_mangle]
pub unsafe extern "C" fn skill_hub_reference_offsets_v2(
    data: *const u8, len: usize, output: *mut usize, capacity: usize,
) -> usize {
    scan(data, len, output, capacity, true, false)
}

// Require the boundary-aware scanner at the Python loading seam.
#[no_mangle]
pub unsafe extern "C" fn skill_hub_reference_offsets_v3(
    data: *const u8, len: usize, output: *mut usize, capacity: usize,
) -> usize {
    scan(data, len, output, capacity, true, true)
}
