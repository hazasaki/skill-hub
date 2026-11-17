use std::{collections::HashSet, slice, str};

#[no_mangle]
pub unsafe extern "C" fn skill_hub_jaccard(
    pointers: *const *const u8, lengths: *const usize, count: usize, out: *mut f64,
) -> i32 {
    if count > 40 || pointers.is_null() || lengths.is_null() || out.is_null() { return 1; }
    let pointers = slice::from_raw_parts(pointers, count);
    let lengths = slice::from_raw_parts(lengths, count);
    let out = slice::from_raw_parts_mut(out, count * count);
    let mut words = Vec::with_capacity(count);
    for i in 0..count {
        if pointers[i].is_null() || lengths[i] > isize::MAX as usize { return 1; }
        let bytes = slice::from_raw_parts(pointers[i], lengths[i]);
        let text = match str::from_utf8(bytes) { Ok(v) => v, Err(_) => return 1 };
        words.push(text.split(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
            .filter(|s| !s.is_empty()).collect::<Vec<_>>());
    }
    let sets: Vec<HashSet<&[&str]>> = words.iter().map(|w| {
        if w.is_empty() { HashSet::new() }
        else if w.len() < 5 { std::iter::once(w.as_slice()).collect() }
        else { w.windows(5).collect() }
    }).collect();
    for i in 0..count {
        for j in (i + 1)..count {
            let left = &sets[i]; let right = &sets[j];
            let intersection = left.intersection(right).count();
            let union = left.len() + right.len() - intersection;
            out[i * count + j] = if union == 0 { 0.0 } else { intersection as f64 / union as f64 };
        }
    }
    0
}
