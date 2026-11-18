//! Native routing accelerators for Skill Hub TEMPR retrieval and bundle hashing.
//! Pure Rust std (zero external crates), compiled with `rustc -O --crate-type cdylib`.
#![allow(clippy::missing_safety_doc)]

use std::collections::{HashMap, HashSet};
use std::convert::TryInto;
use std::fs::File;
use std::io::Read;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::slice;
use std::str;
use std::thread;

#[no_mangle]
pub unsafe extern "C" fn skill_hub_free_buffer(ptr: *mut u8, len: usize) {
    if !ptr.is_null() && len > 0 {
        drop(Vec::from_raw_parts(ptr, len, len));
    }
}

// ============================================================================
// 1. Semantic chunk aggregation + equivalence group dedup + top-K sort
// ============================================================================

#[no_mangle]
pub unsafe extern "C" fn skill_hub_semantic_score_topk(
    scores: *const f32,
    chunk_res_idx: *const u32,
    res_first_order: *const u32,
    res_group_idx: *const i32,
    res_eligible: *const u8,
    num_chunks: usize,
    num_res: usize,
    num_groups: usize,
    limit: usize,
    out_res_idx: *mut u32,
    out_chunk_idx: *mut u32,
    out_scores: *mut f64,
    out_count: *mut usize,
    out_matched_res: *mut usize,
    out_unique_groups: *mut usize,
) -> i32 {
    if out_count.is_null() || out_matched_res.is_null() || out_unique_groups.is_null() {
        return 1;
    }
    if num_chunks == 0 || num_res == 0 {
        *out_count = 0;
        *out_matched_res = 0;
        *out_unique_groups = 0;
        return 0;
    }
    if scores.is_null()
        || chunk_res_idx.is_null()
        || res_first_order.is_null()
        || res_group_idx.is_null()
        || res_eligible.is_null()
        || num_chunks > isize::MAX as usize / 4
        || num_res > isize::MAX as usize / 8
        || num_groups > isize::MAX as usize / 8
    {
        return 1;
    }
    if limit > 0 && (out_res_idx.is_null() || out_chunk_idx.is_null() || out_scores.is_null()) {
        return 1;
    }

    let scores = slice::from_raw_parts(scores, num_chunks);
    let chunk_res_idx = slice::from_raw_parts(chunk_res_idx, num_chunks);
    let res_first_order = slice::from_raw_parts(res_first_order, num_res);
    let res_group_idx = slice::from_raw_parts(res_group_idx, num_res);
    let res_eligible = slice::from_raw_parts(res_eligible, num_res);

    let res = catch_unwind(AssertUnwindSafe(|| -> i32 {
        let mut best_score = vec![f64::NEG_INFINITY; num_res];
        let mut best_chunk = vec![u32::MAX; num_res];

        for i in 0..num_chunks {
            let r = chunk_res_idx[i] as usize;
            if r >= num_res {
                return 1;
            }
            if res_eligible[r] == 0 {
                continue;
            }
            let s = scores[i] as f64;
            if !s.is_finite() {
                return 2;
            }
            if best_chunk[r] == u32::MAX || s > best_score[r] {
                best_score[r] = s;
                best_chunk[r] = i as u32;
            }
        }

        let mut matched_res = 0usize;
        let mut group_best_score = vec![f64::NEG_INFINITY; num_groups];
        let mut group_best_res = vec![u32::MAX; num_groups];
        let mut ranked: Vec<(f64, u32, u32)> = Vec::new();

        // Iterate in first-seen chunk order so exact float ties inside a group match Python dict order.
        for &r_u32 in res_first_order {
            let r = r_u32 as usize;
            if r >= num_res {
                return 1;
            }
            let c = best_chunk[r];
            if c == u32::MAX {
                continue;
            }
            matched_res += 1;
            let s = best_score[r];
            let g = res_group_idx[r];
            if g >= 0 {
                let gu = g as usize;
                if gu >= num_groups {
                    return 1;
                }
                if group_best_res[gu] == u32::MAX || s > group_best_score[gu] {
                    group_best_score[gu] = s;
                    group_best_res[gu] = r_u32;
                }
            } else {
                ranked.push((s, r_u32, c));
            }
        }

        for g in 0..num_groups {
            let r_u32 = group_best_res[g];
            if r_u32 != u32::MAX {
                let r = r_u32 as usize;
                ranked.push((group_best_score[g], r_u32, best_chunk[r]));
            }
        }

        let unique_groups = ranked.len();
        ranked.sort_unstable_by(|a, b| b.0.total_cmp(&a.0).then_with(|| a.1.cmp(&b.1)));

        let take = limit.min(unique_groups);
        if take > 0 {
            let out_r = slice::from_raw_parts_mut(out_res_idx, take);
            let out_c = slice::from_raw_parts_mut(out_chunk_idx, take);
            let out_s = slice::from_raw_parts_mut(out_scores, take);
            for (idx, &(s, r, c)) in ranked.iter().take(take).enumerate() {
                out_r[idx] = r;
                out_c[idx] = c;
                out_s[idx] = s;
            }
        }
        *out_count = take;
        *out_matched_res = matched_res;
        *out_unique_groups = unique_groups;
        0
    }));

    res.unwrap_or(3)
}

// ============================================================================
// 2. Graph Projection Index + Hop-2 Traversal + Alias Collapse
// ============================================================================

const REL_PROVIDES: u8 = 0;
const REL_SPECIALIZES: u8 = 1;
const REL_USES_TOOL: u8 = 2;
const REL_REQUIRES_TOOL: u8 = 3;
const REL_DEPENDS_ON: u8 = 4;
const REL_ALIAS_OF: u8 = 5;

const MAX_CONCEPT_FANOUT: usize = 400;
const MAX_SEEDS: usize = 64;
const HOP_DECAY: f64 = 0.45;
const SECOND_HOP_DECAY: f64 = 0.2;
const MAX_EVIDENCE_PATH: usize = 5;

#[inline]
fn rel_weight(rel: u8) -> f64 {
    match rel {
        REL_PROVIDES => 1.0,
        REL_SPECIALIZES => 1.0,
        REL_USES_TOOL => 0.8,
        REL_REQUIRES_TOOL => 0.8,
        REL_DEPENDS_ON => 0.9,
        REL_ALIAS_OF => 0.6,
        _ => 0.5,
    }
}

#[inline]
fn is_second_hop_rel(rel: u8) -> bool {
    rel == REL_DEPENDS_ON || rel == REL_ALIAS_OF
}

#[inline]
fn round6(val: f64) -> f64 {
    (val.min(1.0) * 1_000_000.0).round_ties_even() / 1_000_000.0
}

fn expand_token_strings(tok: &str, out: &mut Vec<String>) {
    out.clear();
    out.push(tok.to_string());
    let char_len = tok.chars().count();
    if tok.ends_with('s') && char_len > 3 {
        out.push(tok[..tok.len() - 1].to_string());
    }
    if tok.ends_with("es") && char_len > 4 {
        out.push(tok[..tok.len() - 2].to_string());
    }
    if tok.ends_with("ing") && char_len > 5 {
        out.push(tok[..tok.len() - 3].to_string());
    }
    match tok {
        "test" => {
            out.push("tests".to_string());
            out.push("testing".to_string());
        }
        "testing" | "tests" => {
            out.push("test".to_string());
        }
        "issue" => {
            out.push("issues".to_string());
        }
        "issues" => {
            out.push("issue".to_string());
        }
        "token" => {
            out.push("tokens".to_string());
        }
        "tokens" => {
            out.push("token".to_string());
        }
        _ => {}
    }
}

#[derive(Clone, Copy)]
struct EdgeRec {
    other_idx: u32,
    other_is_concept: bool,
    rel: u8,
}

struct ConceptRec {
    fanout: u32,
    orig_tok_count: u32,
    exp_tids: Vec<u32>,
    in_edges: Vec<EdgeRec>,
}

struct NodeRec {
    active: bool,
    is_alias: bool,
    is_concept_prefix: bool,
    collapse_key: u32,
    n_tok_count: u32,
    n_exp_tids: Vec<u32>,
    seed_tok_count: u32,
    seed_exp_tids: Vec<u32>,
    out_edges: Vec<EdgeRec>,
    in_edges: Vec<EdgeRec>,
}

pub struct GraphIndex {
    token_map: HashMap<String, u32>,
    concepts: Vec<ConceptRec>,
    nodes: Vec<NodeRec>,
    concept_inv: Vec<Vec<u32>>,
    node_inv: Vec<Vec<u32>>,
}

struct BinReader<'a> {
    buf: &'a [u8],
    pos: usize,
}

impl<'a> BinReader<'a> {
    fn new(buf: &'a [u8]) -> Self {
        Self { buf, pos: 0 }
    }
    fn read_u8(&mut self) -> Option<u8> {
        if self.pos >= self.buf.len() {
            return None;
        }
        let v = self.buf[self.pos];
        self.pos += 1;
        Some(v)
    }
    fn read_u32(&mut self) -> Option<u32> {
        if self.pos + 4 > self.buf.len() {
            return None;
        }
        let v = u32::from_le_bytes(self.buf[self.pos..self.pos + 4].try_into().ok()?);
        self.pos += 4;
        Some(v)
    }
    fn read_str(&mut self) -> Option<&'a str> {
        let len = self.read_u32()? as usize;
        if self.pos + len > self.buf.len() {
            return None;
        }
        let s = str::from_utf8(&self.buf[self.pos..self.pos + len]).ok()?;
        self.pos += len;
        Some(s)
    }
}

fn intern_token(map: &mut HashMap<String, u32>, inv: &mut Vec<Vec<u32>>, tok: &str) -> u32 {
    if let Some(&id) = map.get(tok) {
        return id;
    }
    let id = map.len() as u32;
    map.insert(tok.to_string(), id);
    inv.push(Vec::new());
    id
}

#[no_mangle]
pub unsafe extern "C" fn skill_hub_graph_index_create(
    data: *const u8,
    len: usize,
) -> *mut GraphIndex {
    if data.is_null() || len == 0 || len > isize::MAX as usize {
        return std::ptr::null_mut();
    }
    let buf = slice::from_raw_parts(data, len);
    let res = catch_unwind(AssertUnwindSafe(|| -> Option<GraphIndex> {
        let mut r = BinReader::new(buf);
        let num_concepts = r.read_u32()? as usize;
        let num_nodes = r.read_u32()? as usize;
        let num_edges = r.read_u32()? as usize;

        let mut token_map: HashMap<String, u32> = HashMap::new();
        let mut concept_inv_tmp: Vec<Vec<u32>> = Vec::new();
        let mut node_inv_tmp: Vec<Vec<u32>> = Vec::new();
        let mut tmp_variants = Vec::with_capacity(8);

        let mut concepts = Vec::with_capacity(num_concepts);
        for c_idx in 0..num_concepts {
            let fanout = r.read_u32()?;
            let orig_tok_count = r.read_u32()?;
            let tok_num = r.read_u32()? as usize;
            let mut exp_set: Vec<u32> = Vec::new();
            for _ in 0..tok_num {
                let tok = r.read_str()?;
                expand_token_strings(tok, &mut tmp_variants);
                for var in &tmp_variants {
                    let tid = intern_token(&mut token_map, &mut concept_inv_tmp, var);
                    if node_inv_tmp.len() < token_map.len() {
                        node_inv_tmp.resize_with(token_map.len(), Vec::new);
                    }
                    if !exp_set.contains(&tid) {
                        exp_set.push(tid);
                        concept_inv_tmp[tid as usize].push(c_idx as u32);
                    }
                }
            }
            concepts.push(ConceptRec {
                fanout,
                orig_tok_count,
                exp_tids: exp_set,
                in_edges: Vec::new(),
            });
        }

        let mut nodes = Vec::with_capacity(num_nodes);
        for n_idx in 0..num_nodes {
            let flags = r.read_u8()?;
            let active = (flags & 1) != 0;
            let is_alias = (flags & 2) != 0;
            let is_concept_prefix = (flags & 4) != 0;
            let collapse_key = r.read_u32()?;

            let n_tok_count = r.read_u32()?;
            let n_tok_num = r.read_u32()? as usize;
            let mut n_exp_set: Vec<u32> = Vec::new();
            for _ in 0..n_tok_num {
                let tok = r.read_str()?;
                expand_token_strings(tok, &mut tmp_variants);
                for var in &tmp_variants {
                    let tid = intern_token(&mut token_map, &mut concept_inv_tmp, var);
                    if node_inv_tmp.len() < token_map.len() {
                        node_inv_tmp.resize_with(token_map.len(), Vec::new);
                    }
                    if !n_exp_set.contains(&tid) {
                        n_exp_set.push(tid);
                    }
                }
            }

            let same_as_n = r.read_u8()? != 0;
            let (seed_tok_count, seed_exp_tids) = if same_as_n {
                for &tid in &n_exp_set {
                    node_inv_tmp[tid as usize].push(n_idx as u32);
                }
                (n_tok_count, n_exp_set.clone())
            } else {
                let s_tok_count = r.read_u32()?;
                let s_tok_num = r.read_u32()? as usize;
                let mut s_exp_set: Vec<u32> = Vec::new();
                for _ in 0..s_tok_num {
                    let tok = r.read_str()?;
                    expand_token_strings(tok, &mut tmp_variants);
                    for var in &tmp_variants {
                        let tid = intern_token(&mut token_map, &mut concept_inv_tmp, var);
                        if node_inv_tmp.len() < token_map.len() {
                            node_inv_tmp.resize_with(token_map.len(), Vec::new);
                        }
                        if !s_exp_set.contains(&tid) {
                            s_exp_set.push(tid);
                            node_inv_tmp[tid as usize].push(n_idx as u32);
                        }
                    }
                }
                (s_tok_count, s_exp_set)
            };

            nodes.push(NodeRec {
                active,
                is_alias,
                is_concept_prefix,
                collapse_key,
                n_tok_count,
                n_exp_tids: n_exp_set,
                seed_tok_count,
                seed_exp_tids,
                out_edges: Vec::new(),
                in_edges: Vec::new(),
            });
        }

        for _ in 0..num_edges {
            let src_is_c = r.read_u8()? != 0;
            let src_idx = r.read_u32()?;
            let dst_is_c = r.read_u8()? != 0;
            let dst_idx = r.read_u32()?;
            let rel = r.read_u8()?;

            if !src_is_c && (src_idx as usize) < num_nodes {
                nodes[src_idx as usize].out_edges.push(EdgeRec {
                    other_idx: dst_idx,
                    other_is_concept: dst_is_c,
                    rel,
                });
            }
            if dst_is_c {
                if (dst_idx as usize) < num_concepts {
                    concepts[dst_idx as usize].in_edges.push(EdgeRec {
                        other_idx: src_idx,
                        other_is_concept: src_is_c,
                        rel,
                    });
                }
            } else if (dst_idx as usize) < num_nodes {
                nodes[dst_idx as usize].in_edges.push(EdgeRec {
                    other_idx: src_idx,
                    other_is_concept: src_is_c,
                    rel,
                });
            }
        }

        Some(GraphIndex {
            token_map,
            concepts,
            nodes,
            concept_inv: concept_inv_tmp,
            node_inv: node_inv_tmp,
        })
    }));

    match res {
        Ok(Some(idx)) => Box::into_raw(Box::new(idx)),
        _ => std::ptr::null_mut(),
    }
}

#[no_mangle]
pub unsafe extern "C" fn skill_hub_graph_index_free(handle: *mut GraphIndex) {
    if !handle.is_null() {
        drop(Box::from_raw(handle));
    }
}

#[derive(Clone)]
struct CandEntry {
    node_idx: u32,
    raw_score: f64,
    relation: u8,
    via_concept: i32,
    // Each path step: node_idx (u32) or rel (u8 encoded with high bit or parallel arrays)
    // Max 5 elements: [n0, r1, n1, r2, n2] -> path_nodes (up to 3), path_rels (up to 2)
    path_len: u8,
    path_nodes: [u32; 3],
    path_rels: [u8; 2],
}

impl CandEntry {
    fn from_single(node_idx: u32, raw_score: f64, relation: u8, via_concept: i32) -> Self {
        Self {
            node_idx,
            raw_score,
            relation,
            via_concept,
            path_len: 1,
            path_nodes: [node_idx, 0, 0],
            path_rels: [0, 0],
        }
    }

    fn from_hop1b(r0: u32, rel: u8, r1: u32, raw_score: f64, hit_rel: u8, via_concept: i32) -> Self {
        Self {
            node_idx: r1,
            raw_score,
            relation: hit_rel,
            via_concept,
            path_len: 3,
            path_nodes: [r0, r1, 0],
            path_rels: [rel, 0],
        }
    }

    fn extended(&self, other: u32, edge_rel: u8, raw_score: f64) -> Self {
        // Matches Python: path = (hit['evidence']['path'] + [edge['r'], other])[:5]
        // Note: path lengths in hop 1 are 1 ([n0]) or 3 ([n0, r1, n1]).
        // After extending once: 3 ([n0, r1, n1]) or 5 ([n0, r1, n1, r2, n2]).
        // After extending twice from length 3: sliced to 5, so unchanged!
        let mut nodes = self.path_nodes;
        let mut rels = self.path_rels;
        let new_len = if self.path_len == 1 {
            rels[0] = edge_rel;
            nodes[1] = other;
            3
        } else if self.path_len == 3 {
            rels[1] = edge_rel;
            nodes[2] = other;
            5
        } else {
            5
        };
        Self {
            node_idx: other,
            raw_score,
            relation: edge_rel,
            via_concept: -1,
            path_len: new_len,
            path_nodes: nodes,
            path_rels: rels,
        }
    }
}

#[no_mangle]
pub unsafe extern "C" fn skill_hub_graph_index_query(
    handle: *const GraphIndex,
    q_tokens_buf: *const u8,
    q_tokens_len: usize,
    eligible_mask: *const u8,
    eligible_len: usize,
    limit: usize,
    out_ptr: *mut *mut u8,
    out_len: *mut usize,
) -> i32 {
    if handle.is_null()
        || q_tokens_buf.is_null()
        || eligible_mask.is_null()
        || out_ptr.is_null()
        || out_len.is_null()
        || q_tokens_len > isize::MAX as usize
        || eligible_len > isize::MAX as usize
    {
        return 1;
    }

    let g = &*handle;
    if eligible_len != g.nodes.len() {
        return 1;
    }
    let q_buf = slice::from_raw_parts(q_tokens_buf, q_tokens_len);
    let eligible = slice::from_raw_parts(eligible_mask, eligible_len);

    let res = catch_unwind(AssertUnwindSafe(|| -> Option<Vec<u8>> {
        let mut qr = BinReader::new(q_buf);
        let q_count = qr.read_u32()? as usize;
        let mut q_exp_tids: HashSet<u32> = HashSet::new();
        let mut tmp_vars = Vec::with_capacity(8);
        for _ in 0..q_count {
            let tok = qr.read_str()?;
            expand_token_strings(tok, &mut tmp_vars);
            for v in &tmp_vars {
                if let Some(&tid) = g.token_map.get(v.as_str()) {
                    q_exp_tids.insert(tid);
                }
            }
        }

        let mut matched_concepts: Vec<(u32, f64, u32)> = Vec::new();
        let mut matched_concept_set = vec![false; g.concepts.len()];

        if q_count > 0 && !q_exp_tids.is_empty() {
            let mut visited_concepts: Vec<u32> = Vec::new();
            let mut seen_c = vec![false; g.concepts.len()];
            for &tid in &q_exp_tids {
                for &cid in &g.concept_inv[tid as usize] {
                    if !seen_c[cid as usize] {
                        seen_c[cid as usize] = true;
                        visited_concepts.push(cid);
                    }
                }
            }
            for cid in visited_concepts {
                let c = &g.concepts[cid as usize];
                if c.orig_tok_count == 0 {
                    continue;
                }
                let mut overlap = 0usize;
                for &tid in &c.exp_tids {
                    if q_exp_tids.contains(&tid) {
                        overlap += 1;
                    }
                }
                if overlap == 0 {
                    continue;
                }
                let by_concept = overlap as f64 / c.orig_tok_count as f64;
                let by_query = overlap as f64 / q_count as f64;
                if by_concept < 0.5 && by_query < 0.5 {
                    continue;
                }
                let c_score = by_concept.max(by_query);
                matched_concepts.push((cid, c_score, c.fanout));
                matched_concept_set[cid as usize] = true;
            }
            // concepts are sorted lexicographically by concept_id -> cid ascending matches id ascending
            matched_concepts.sort_unstable_by(|a, b| b.1.total_cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
        }

        let mut seeds: Vec<(u32, f64)> = Vec::new();
        if !q_exp_tids.is_empty() {
            let mut visited_nodes: Vec<u32> = Vec::new();
            let mut seen_n = vec![false; g.nodes.len()];
            for &tid in &q_exp_tids {
                for &nid in &g.node_inv[tid as usize] {
                    if !seen_n[nid as usize] {
                        seen_n[nid as usize] = true;
                        visited_nodes.push(nid);
                    }
                }
            }
            for nid in visited_nodes {
                if eligible[nid as usize] == 0 {
                    continue;
                }
                let node = &g.nodes[nid as usize];
                if !node.active || node.seed_tok_count == 0 {
                    continue;
                }
                let mut overlap = 0usize;
                for &tid in &node.seed_exp_tids {
                    if q_exp_tids.contains(&tid) {
                        overlap += 1;
                    }
                }
                if overlap > 0 {
                    seeds.push((nid, overlap as f64 / node.seed_tok_count as f64));
                }
            }
            seeds.sort_unstable_by(|a, b| b.1.total_cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
            if seeds.len() > MAX_SEEDS {
                seeds.truncate(MAX_SEEDS);
            }
        }

        let mut candidates: Vec<CandEntry> = Vec::new();
        let mut cand_pos = vec![u32::MAX; g.nodes.len()];
        let mut truncated_concepts = 0u32;

        let mut offer_single = |rid: u32, score: f64, rel: u8, via: i32| {
            let ru = rid as usize;
            if ru >= g.nodes.len() || eligible[ru] == 0 {
                return;
            }
            let node = &g.nodes[ru];
            if !node.active || node.is_concept_prefix || score <= 0.0 {
                return;
            }
            let pos = cand_pos[ru];
            if pos == u32::MAX {
                cand_pos[ru] = candidates.len() as u32;
                candidates.push(CandEntry::from_single(rid, round6(score), rel, via));
            } else if score > candidates[pos as usize].raw_score {
                candidates[pos as usize] = CandEntry::from_single(rid, round6(score), rel, via);
            }
        };

        // Hop 1a
        for &(cid, c_score, fanout) in &matched_concepts {
            if fanout as usize > MAX_CONCEPT_FANOUT {
                truncated_concepts += 1;
            }
            let spec = 1.0 / (fanout.max(1) as f64).sqrt();
            let mut members = 0usize;
            for edge in &g.concepts[cid as usize].in_edges {
                if members >= MAX_CONCEPT_FANOUT {
                    break;
                }
                members += 1;
                if edge.other_is_concept {
                    continue;
                }
                let src_idx = edge.other_idx;
                let src_node = &g.nodes[src_idx as usize];
                let name_bonus = if src_node.n_tok_count > 0 && !q_exp_tids.is_empty() {
                    let mut ov = 0usize;
                    for &tid in &src_node.n_exp_tids {
                        if q_exp_tids.contains(&tid) {
                            ov += 1;
                        }
                    }
                    ov as f64 / src_node.n_tok_count as f64
                } else {
                    0.0
                };
                let w = rel_weight(edge.rel);
                let score = (HOP_DECAY + 0.55 * c_score * spec) * w + 0.30 * name_bonus;
                offer_single(src_idx, score, edge.rel, cid as i32);
            }
        }

        // Hop 1b
        for &(rid, seed_score) in &seeds {
            for edge in &g.nodes[rid as usize].out_edges {
                if !edge.other_is_concept {
                    continue;
                }
                let cid = edge.other_idx as usize;
                if cid >= g.concepts.len() || !matched_concept_set[cid] {
                    continue;
                }
                let w = rel_weight(edge.rel);
                let spec = 1.0 / (g.concepts[cid].fanout.max(1) as f64).sqrt();
                for sib in &g.concepts[cid].in_edges {
                    if sib.other_is_concept || sib.other_idx == rid {
                        continue;
                    }
                    let sib_idx = sib.other_idx;
                    let sib_u = sib_idx as usize;
                    if sib_u >= g.nodes.len() || eligible[sib_u] == 0 {
                        continue;
                    }
                    let sib_node = &g.nodes[sib_u];
                    if !sib_node.active || sib_node.is_concept_prefix {
                        continue;
                    }
                    let score = seed_score * HOP_DECAY * w * spec;
                    if score <= 0.0 {
                        continue;
                    }
                    let pos = cand_pos[sib_u];
                    if pos == u32::MAX {
                        cand_pos[sib_u] = candidates.len() as u32;
                        candidates.push(CandEntry::from_hop1b(
                            rid, edge.rel, sib_idx, round6(score), sib.rel, cid as i32,
                        ));
                    } else if score > candidates[pos as usize].raw_score {
                        candidates[pos as usize] = CandEntry::from_hop1b(
                            rid, edge.rel, sib_idx, round6(score), sib.rel, cid as i32,
                        );
                    }
                }
            }
        }

        // Hop 2 BFS
        let mut frontier: Vec<u32> = (0..candidates.len() as u32).collect();
        for depth in 0..2 {
            let mut next_frontier: Vec<u32> = Vec::new();
            let decay = SECOND_HOP_DECAY.powi(depth + 1);
            for c_pos in frontier {
                let hit = candidates[c_pos as usize].clone();
                let rid = hit.node_idx;
                let two_dirs = depth == 0 && is_second_hop_rel(hit.relation);
                let dirs = if two_dirs { 2 } else { 1 };
                for d_idx in 0..dirs {
                    let edge_slice = if d_idx == 0 {
                        &g.nodes[rid as usize].out_edges
                    } else {
                        &g.nodes[rid as usize].in_edges
                    };
                    for edge in edge_slice {
                        if edge.other_is_concept || !is_second_hop_rel(edge.rel) {
                            continue;
                        }
                        let other = edge.other_idx;
                        let ou = other as usize;
                        if ou >= g.nodes.len() || cand_pos[ou] != u32::MAX || other == rid {
                            continue;
                        }
                        if eligible[ou] == 0 {
                            continue;
                        }
                        let o_node = &g.nodes[ou];
                        if !o_node.active || o_node.is_concept_prefix {
                            continue;
                        }
                        let score = hit.raw_score * decay;
                        if score <= 0.0 {
                            continue;
                        }
                        let rounded = round6(score);
                        let new_pos = candidates.len() as u32;
                        cand_pos[ou] = new_pos;
                        candidates.push(hit.extended(other, edge.rel, rounded));
                        next_frontier.push(new_pos);
                    }
                }
            }
            frontier = next_frontier;
        }

        // Collapse aliases (preserving first-seen insertion order of collapse_key)
        let mut best_by_key: HashMap<u32, usize> = HashMap::with_capacity(candidates.len());
        let mut collapsed: Vec<CandEntry> = Vec::with_capacity(candidates.len());
        for cand in &candidates {
            let node = &g.nodes[cand.node_idx as usize];
            let key = node.collapse_key;
            if let Some(&slot) = best_by_key.get(&key) {
                let cur = &collapsed[slot];
                let cur_node = &g.nodes[cur.node_idx as usize];
                let prefer_non_alias = if node.is_alias { 0u8 } else { 1u8 };
                let cur_non_alias = if cur_node.is_alias { 0u8 } else { 1u8 };
                if (prefer_non_alias, cand.raw_score) > (cur_non_alias, cur.raw_score) {
                    collapsed[slot] = cand.clone();
                }
            } else {
                best_by_key.insert(key, collapsed.len());
                collapsed.push(cand.clone());
            }
        }

        collapsed.sort_unstable_by(|a, b| {
            b.raw_score.total_cmp(&a.raw_score).then_with(|| a.node_idx.cmp(&b.node_idx))
        });
        if collapsed.len() > limit {
            collapsed.truncate(limit);
        }

        // Serialize compact binary response:
        // matched_concepts:u32, seeds:u32, candidates:u32, truncated_concepts:u32, hit_count:u32
        // per hit: node_idx:u32, raw_score:f64, rel:u8, via:i32, path_len:u8, nodes[3]:u32, rels[2]:u8
        let mut out = Vec::with_capacity(20 + collapsed.len() * 32);
        out.extend_from_slice(&(matched_concepts.len() as u32).to_le_bytes());
        out.extend_from_slice(&(seeds.len() as u32).to_le_bytes());
        out.extend_from_slice(&(candidates.len() as u32).to_le_bytes());
        out.extend_from_slice(&truncated_concepts.to_le_bytes());
        out.extend_from_slice(&(collapsed.len() as u32).to_le_bytes());
        for h in &collapsed {
            out.extend_from_slice(&h.node_idx.to_le_bytes());
            out.extend_from_slice(&h.raw_score.to_le_bytes());
            out.push(h.relation);
            out.extend_from_slice(&h.via_concept.to_le_bytes());
            out.push(h.path_len.min(MAX_EVIDENCE_PATH as u8));
            for &pn in &h.path_nodes {
                out.extend_from_slice(&pn.to_le_bytes());
            }
            for &pr in &h.path_rels {
                out.push(pr);
            }
        }
        Some(out)
    }));

    match res {
        Ok(Some(vec)) => {
            let len = vec.len();
            let boxed = vec.into_boxed_slice();
            let raw = Box::into_raw(boxed) as *mut u8;
            *out_ptr = raw;
            *out_len = len;
            0
        }
        Ok(None) => 1,
        Err(_) => 2,
    }
}

// ============================================================================
// 3. Parallel SHA-256 over file paths for bundle_digest acceleration
// ============================================================================

const K: [u32; 64] = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
];

struct Sha256 {
    state: [u32; 8],
    len: u64,
    buf: [u8; 64],
    pos: usize,
}

impl Sha256 {
    fn new() -> Self {
        Self {
            state: [
                0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
                0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
            ],
            len: 0,
            buf: [0u8; 64],
            pos: 0,
        }
    }

    fn compress(state: &mut [u32; 8], block: &[u8; 64]) {
        let mut w = [0u32; 64];
        for i in 0..16 {
            w[i] = u32::from_be_bytes([
                block[i * 4],
                block[i * 4 + 1],
                block[i * 4 + 2],
                block[i * 4 + 3],
            ]);
        }
        for i in 16..64 {
            let s0 = w[i - 15].rotate_right(7) ^ w[i - 15].rotate_right(18) ^ (w[i - 15] >> 3);
            let s1 = w[i - 2].rotate_right(17) ^ w[i - 2].rotate_right(19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16].wrapping_add(s0).wrapping_add(w[i - 7]).wrapping_add(s1);
        }
        let [mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut h] = *state;
        for i in 0..64 {
            let s1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
            let ch = (e & f) ^ ((!e) & g);
            let temp1 = h.wrapping_add(s1).wrapping_add(ch).wrapping_add(K[i]).wrapping_add(w[i]);
            let s0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
            let maj = (a & b) ^ (a & c) ^ (b & c);
            let temp2 = s0.wrapping_add(maj);
            h = g;
            g = f;
            f = e;
            e = d.wrapping_add(temp1);
            d = c;
            c = b;
            b = a;
            a = temp1.wrapping_add(temp2);
        }
        state[0] = state[0].wrapping_add(a);
        state[1] = state[1].wrapping_add(b);
        state[2] = state[2].wrapping_add(c);
        state[3] = state[3].wrapping_add(d);
        state[4] = state[4].wrapping_add(e);
        state[5] = state[5].wrapping_add(f);
        state[6] = state[6].wrapping_add(g);
        state[7] = state[7].wrapping_add(h);
    }

    fn update(&mut self, data: &[u8]) {
        self.len += data.len() as u64;
        let mut idx = 0;
        if self.pos > 0 {
            let take = (64 - self.pos).min(data.len());
            self.buf[self.pos..self.pos + take].copy_from_slice(&data[..take]);
            self.pos += take;
            idx += take;
            if self.pos == 64 {
                let block = self.buf;
                Self::compress(&mut self.state, &block);
                self.pos = 0;
            }
        }
        while idx + 64 <= data.len() {
            let block: &[u8; 64] = data[idx..idx + 64].try_into().unwrap();
            Self::compress(&mut self.state, block);
            idx += 64;
        }
        if idx < data.len() {
            let rem = data.len() - idx;
            self.buf[..rem].copy_from_slice(&data[idx..]);
            self.pos = rem;
        }
    }

    fn finish(mut self) -> [u8; 32] {
        let bit_len = self.len.wrapping_mul(8);
        self.update(&[0x80]);
        if self.pos > 56 {
            while self.pos < 64 {
                self.buf[self.pos] = 0;
                self.pos += 1;
            }
            let block = self.buf;
            Self::compress(&mut self.state, &block);
            self.pos = 0;
        }
        while self.pos < 56 {
            self.buf[self.pos] = 0;
            self.pos += 1;
        }
        self.buf[56..64].copy_from_slice(&bit_len.to_be_bytes());
        let block = self.buf;
        Self::compress(&mut self.state, &block);
        let mut out = [0u8; 32];
        for (i, word) in self.state.iter().enumerate() {
            out[i * 4..i * 4 + 4].copy_from_slice(&word.to_be_bytes());
        }
        out
    }
}

fn hash_one_file(path: &str) -> Result<[u8; 32], ()> {
    let mut file = File::open(path).map_err(|_| ())?;
    let mut hasher = Sha256::new();
    let mut buf = [0u8; 65536];
    loop {
        let n = file.read(&mut buf).map_err(|_| ())?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(hasher.finish())
}

#[no_mangle]
pub unsafe extern "C" fn skill_hub_sha256_files(
    paths_buf: *const u8,
    paths_len: usize,
    count: usize,
    out_digests: *mut u8,
) -> i32 {
    if count == 0 {
        return 0;
    }
    if paths_buf.is_null()
        || out_digests.is_null()
        || paths_len == 0
        || paths_len > isize::MAX as usize
        || count > isize::MAX as usize / 32
    {
        return 1;
    }
    let raw = slice::from_raw_parts(paths_buf, paths_len);
    let out = slice::from_raw_parts_mut(out_digests, count * 32);

    let res = catch_unwind(AssertUnwindSafe(|| -> i32 {
        let mut paths: Vec<&str> = Vec::with_capacity(count);
        for part in raw.split(|&b| b == 0) {
            if part.is_empty() {
                return 1;
            }
            let Ok(s) = str::from_utf8(part) else { return 1 };
            paths.push(s);
        }
        if paths.len() != count {
            return 1;
        }
        if count < 4 {
            for (i, p) in paths.iter().enumerate() {
                let Ok(dig) = hash_one_file(p) else { return 2 };
                out[i * 32..(i + 1) * 32].copy_from_slice(&dig);
            }
            return 0;
        }
        let workers = 4.min(count);
        let chunk_size = (count + workers - 1) / workers;
        let mut failed = false;
        thread::scope(|s| {
            let mut handles = Vec::with_capacity(workers);
            for (w_idx, (p_chunk, out_chunk)) in paths
                .chunks(chunk_size)
                .zip(out.chunks_mut(chunk_size * 32))
                .enumerate()
            {
                let _ = w_idx;
                handles.push(s.spawn(move || -> bool {
                    for (i, p) in p_chunk.iter().enumerate() {
                        let Ok(dig) = hash_one_file(p) else { return false };
                        out_chunk[i * 32..(i + 1) * 32].copy_from_slice(&dig);
                    }
                    true
                }));
            }
            for h in handles {
                if !h.join().unwrap_or(false) {
                    failed = true;
                }
            }
        });
        if failed { 2 } else { 0 }
    }));
    res.unwrap_or(3)
}

mod decisions;

pub mod adaptive_shortlist;
