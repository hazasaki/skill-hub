//! Match ASCII catalog words against query terms for metadata ranking.
//! A non-ASCII row is delegated to Python's Unicode tokenizer.
use std::{collections::HashMap, slice};

/// Input consists of NUL-delimited terms and pairs of name/description fields.
/// The caller owns the buffers and reserves two output masks per row.
#[no_mangle]
pub unsafe extern "C" fn metadata_hits(
    queries: *const u8, query_size: usize, fields: *const u8, field_size: usize,
    out: *mut u64, rows: usize,
) -> i32 {
    if queries.is_null() || fields.is_null() || out.is_null()
        || query_size > isize::MAX as usize || field_size > isize::MAX as usize
        || rows > isize::MAX as usize / 16 {
        return 1;
    }
    let mut terms = HashMap::new();
    for (i, word) in slice::from_raw_parts(queries, query_size).split(|b| *b == 0).enumerate() {
        if i >= 63 || word.is_empty() || !word.is_ascii() { return 1; }
        terms.insert(word, 1u64 << i);
    }
    let mut parts = slice::from_raw_parts(fields, field_size).split(|b| *b == 0);
    for pair in slice::from_raw_parts_mut(out, rows * 2).chunks_exact_mut(2) {
        let (Some(name), Some(description)) = (parts.next(), parts.next()) else { return 1 };
        if !name.is_ascii() || !description.is_ascii() {
            pair[0] = u64::MAX;
            pair[1] = 0;
            continue;
        }
        let mask = |field: &[u8]| field.split(|b| !b.is_ascii_alphanumeric() && *b != b'_')
            .filter_map(|word| terms.get(word)).fold(0, |a, b| a | *b);
        pair[0] = mask(name);
        pair[1] = pair[0] | mask(description);
    }
    if parts.next().is_some() { 1 } else { 0 }
}
