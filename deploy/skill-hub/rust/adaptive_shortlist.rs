//! Pure decisions over a cumulative snapshot for one unchanged task and rubric.
use std::collections::{HashMap, HashSet};

const FLOOR: f64 = 0.65;
const POOL_LIMIT: usize = 300;
const SKILL_TARGET: usize = 12;
const PROMPT_TARGET: usize = 8;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Kind {
    Skill,
    Prompt,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Candidate<'a> {
    pub id: &'a str,
    pub kind: Kind,
}

/// One attempted candidate. Missing scores occupy a slot without voting.
#[derive(Clone, Copy, Debug)]
pub struct Attempt<'a> {
    pub id: &'a str,
    pub score: Option<f64>,
}

#[derive(Clone, Copy, Debug)]
pub struct Coefficients {
    /// Nonnegative multiplier of median absolute deviation.
    pub k: f64,
    /// Positive prior sample size.
    pub h: f64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Round {
    First,
    Second,
    Last,
}

impl Round {
    pub fn cap(self) -> usize {
        match self {
            Self::First => 24,
            Self::Second => 48,
            Self::Last => 96,
        }
    }

    fn next(self) -> Option<Self> {
        match self {
            Self::First => Some(Self::Second),
            Self::Second => Some(Self::Last),
            Self::Last => None,
        }
    }
}

#[derive(Debug)]
pub struct KindStats<'a> {
    pub n: usize,
    pub median: Option<f64>,
    pub mad: Option<f64>,
    pub weight: f64,
    pub threshold: f64,
    /// All score-eligible candidates, highest score first.
    pub eligible: Vec<Attempt<'a>>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Reason {
    MissingEligibleKinds,
    MenuComplete,
    Deadline,
    PoolExhausted,
    RoundLimit,
    ExplicitNoResourceEvidence,
}

#[derive(Debug)]
pub struct Decision<'a> {
    pub skills: KindStats<'a>,
    pub prompts: KindStats<'a>,
    pub reason: Reason,
    pub next: Option<Round>,
    pub to_score: Vec<Candidate<'a>>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum InputError {
    InvalidCoefficients,
    InvalidScore,
    InvalidCanonicalId,
    ConflictingKind,
    UnknownAttempt,
    PoolLimit,
    RoundBudget,
}

fn median(values: &mut [f64]) -> f64 {
    values.sort_unstable_by(f64::total_cmp);
    let middle = values.len() / 2;
    if values.len() % 2 == 0 {
        (values[middle - 1] + values[middle]) / 2.0
    } else {
        values[middle]
    }
}

fn stats(mut scored: Vec<Attempt<'_>>, prior: f64, coefficients: Coefficients) -> KindStats<'_> {
    let n = scored.len();
    if n == 0 {
        return KindStats {
            n, median: None, mad: None, weight: 0.0, threshold: prior, eligible: scored,
        };
    }
    let mut scores: Vec<_> = scored.iter().map(|row| row.score.unwrap()).collect();
    let m = median(&mut scores);
    for score in &mut scores {
        *score = (*score - m).abs();
    }
    let mad = median(&mut scores);
    let weight = n as f64 / (n as f64 + coefficients.h);
    // This form preserves exact equality when the prior and observed target agree.
    let threshold = (prior + weight * (m + coefficients.k * mad - prior)).clamp(FLOOR, 1.0);
    scored.retain(|row| row.score.unwrap() >= threshold);
    scored.sort_unstable_by(|a, b| {
        b.score.unwrap().total_cmp(&a.score.unwrap()).then_with(|| a.id.cmp(b.id))
    });
    KindStats { n, median: Some(m), mad: Some(mad), weight, threshold, eligible: scored }
}

/// Pool is in retrieval order. Attempts are chronological, so the first observation for an
/// ID wins even when it has no score. Only the caller may assert reliable no-resource evidence.
/// When asserted, the decision exposes no eligible choices. Statistics remain inspectable.
pub fn after_round<'a>(
    round: Round,
    pool: &[Candidate<'a>],
    attempts: &[Attempt<'a>],
    coefficients: Coefficients,
    deadline_reached: bool,
    explicit_no_resource_evidence: bool,
) -> Result<Decision<'a>, InputError> {
    after_verified_round(round, pool, attempts, coefficients, deadline_reached,
                         explicit_no_resource_evidence, None)
}

/// Reuse score statistics while using counts from the verified, deliverable menu.
pub fn after_verified_round<'a>(
    round: Round,
    pool: &[Candidate<'a>],
    attempts: &[Attempt<'a>],
    coefficients: Coefficients,
    deadline_reached: bool,
    explicit_no_resource_evidence: bool,
    verified_counts: Option<[usize; 2]>,
) -> Result<Decision<'a>, InputError> {
    if !(coefficients.k.is_finite() && coefficients.k >= 0.0
        && coefficients.h.is_finite() && coefficients.h > 0.0)
    {
        return Err(InputError::InvalidCoefficients);
    }

    let mut kinds = HashMap::new();
    let mut ranked = Vec::new();
    for &candidate in pool {
        if candidate.id.trim().is_empty() {
            return Err(InputError::InvalidCanonicalId);
        }
        match kinds.insert(candidate.id, candidate.kind) {
            Some(kind) if kind != candidate.kind => return Err(InputError::ConflictingKind),
            Some(_) => {}
            None => ranked.push(candidate),
        }
        if ranked.len() > POOL_LIMIT {
            return Err(InputError::PoolLimit);
        }
    }

    let mut attempted = HashSet::new();
    let mut skills = Vec::new();
    let mut prompts = Vec::new();
    for &attempt in attempts {
        let kind = kinds.get(attempt.id).ok_or(InputError::UnknownAttempt)?;
        if attempt.score.is_some_and(|score| !(0.0..=1.0).contains(&score)) {
            return Err(InputError::InvalidScore);
        }
        if attempted.insert(attempt.id) && attempt.score.is_some() {
            match kind {
                Kind::Skill => skills.push(attempt),
                Kind::Prompt => prompts.push(attempt),
            }
        }
    }
    if attempted.len() > round.cap() {
        return Err(InputError::RoundBudget);
    }

    let mut skills = stats(skills, 0.80, coefficients);
    let mut prompts = stats(prompts, 0.75, coefficients);
    let [skill_count, prompt_count] =
        verified_counts.unwrap_or([skills.eligible.len(), prompts.eligible.len()]);
    if skill_count > skills.eligible.len() || prompt_count > prompts.eligible.len() {
        return Err(InputError::RoundBudget);
    }
    let skill_gap = SKILL_TARGET.saturating_sub(skill_count);
    let prompt_gap = PROMPT_TARGET.saturating_sub(prompt_count);
    ranked.retain(|row| !attempted.contains(row.id));
    let reason = if explicit_no_resource_evidence {
        skills.eligible.clear();
        prompts.eligible.clear();
        Reason::ExplicitNoResourceEvidence
    } else if skill_gap == 0 && prompt_gap == 0 {
        Reason::MenuComplete
    } else if deadline_reached {
        Reason::Deadline
    } else if ranked.is_empty() {
        Reason::PoolExhausted
    } else if round == Round::Last {
        Reason::RoundLimit
    } else {
        Reason::MissingEligibleKinds
    };

    let mut to_score = Vec::new();
    let next = if reason == Reason::MissingEligibleKinds { round.next() } else { None };
    if let Some(next) = next {
        let mut reserve = [skill_gap, prompt_gap];
        let mut priority = [Vec::new(), Vec::new(), Vec::new()];
        for row in ranked {
            let i = if row.kind == Kind::Skill { 0 } else { 1 };
            let bucket = if reserve[i] > 0 {
                reserve[i] -= 1;
                0
            } else if [skill_gap, prompt_gap][i] > 0 {
                1
            } else {
                2
            };
            priority[bucket].push(row);
        }
        to_score.extend(priority.into_iter().flatten().take(next.cap() - attempted.len()));
    }
    Ok(Decision { skills, prompts, reason, next, to_score })
}

#[cfg(test)]
mod tests {
    use super::*;

    const TEST: Coefficients = Coefficients { k: 1.0, h: 4.0 };

    fn fixture(
        skills: usize, prompts: usize, round: Round, deadline: bool,
    ) -> (Reason, Option<Round>, usize, usize, Vec<Kind>) {
        let ids: Vec<_> = (0..150).map(|id| id.to_string()).collect();
        let pool: Vec<_> = ids.iter().enumerate().map(|(i, id)| Candidate {
            id,
            kind: if i % 2 == 0 { Kind::Skill } else { Kind::Prompt },
        }).collect();
        let attempts: Vec<_> = pool.iter().take(round.cap()).enumerate().map(|(i, row)| {
            let count = if row.kind == Kind::Skill { skills } else { prompts };
            Attempt { id: row.id, score: Some(if i / 2 < count { 0.99 } else { 0.2 }) }
        }).collect();
        let out = after_round(round, &pool, &attempts, Coefficients { k: 0.0, ..TEST },
                              deadline, false).unwrap();
        assert!(out.to_score.iter().all(|candidate| !attempts.iter().any(|a| a.id == candidate.id)));
        (out.reason, out.next, out.skills.eligible.len(), out.prompts.eligible.len(),
         out.to_score.iter().map(|row| row.kind).collect())
    }

    #[test]
    fn first_round_five_and_three_expands_to_total_48() {
        let (reason, next, skills, prompts, batch) = fixture(5, 3, Round::First, false);
        assert_eq!((reason, next, skills, prompts), (Reason::MissingEligibleKinds, Some(Round::Second), 5, 3));
        assert_eq!(batch.len(), 24);
        assert!(batch[..12].contains(&Kind::Skill) && batch[..12].contains(&Kind::Prompt));
    }

    #[test]
    fn second_round_prioritizes_only_missing_type_to_total_96() {
        let (reason, next, skills, prompts, batch) = fixture(12, 6, Round::Second, false);
        assert_eq!((reason, next, skills, prompts), (Reason::MissingEligibleKinds, Some(Round::Last), 12, 6));
        assert_eq!(batch, vec![Kind::Prompt; 48]);
    }

    #[test]
    fn complete_second_round_stops() {
        assert_eq!(fixture(12, 8, Round::Second, false),
                   (Reason::MenuComplete, None, 12, 8, vec![]));
    }

    #[test]
    fn limit_and_deadline_preserve_actual_partial_counts() {
        assert_eq!(fixture(12, 6, Round::Last, false), (Reason::RoundLimit, None, 12, 6, vec![]));
        assert_eq!(fixture(5, 3, Round::First, true), (Reason::Deadline, None, 5, 3, vec![]));
    }

    #[test]
    fn median_mad_and_weight_are_separate_and_canonical_utf8_ids_vote_once() {
        let pool = [
            Candidate { id: "biên-bản", kind: Kind::Skill },
            Candidate { id: "công-cụ", kind: Kind::Skill },
            Candidate { id: "biên-bản", kind: Kind::Skill },
            Candidate { id: "lời-nhắc", kind: Kind::Prompt },
        ];
        let out = after_round(Round::First, &pool, &[
            Attempt { id: "biên-bản", score: Some(1.0) },
            Attempt { id: "công-cụ", score: Some(0.5) },
            Attempt { id: "biên-bản", score: Some(1.0) },
            Attempt { id: "lời-nhắc", score: Some(0.75) },
        ], TEST, false, false).unwrap();
        assert_eq!(out.reason, Reason::PoolExhausted);
        assert_eq!((out.skills.n, out.skills.median, out.skills.mad), (2, Some(0.75), Some(0.25)));
        assert_eq!((out.prompts.n, out.prompts.median, out.prompts.mad), (1, Some(0.75), Some(0.0)));
        assert_eq!(out.skills.weight, 1.0 / 3.0);
        assert_eq!(out.prompts.weight, 0.2);
        assert!((out.skills.threshold - (0.8 * 2.0 / 3.0 + 1.0 / 3.0)).abs() < 1e-12);
        assert_eq!(out.skills.eligible.iter().map(|row| row.id).collect::<Vec<_>>(), ["biên-bản"]);
        assert_eq!(out.prompts.eligible.len(), 1);
    }

    #[test]
    fn floor_equality_zero_and_missing_scores() {
        let pool: Vec<_> = ["floor", "zero", "low", "error", "orphan"].into_iter()
            .map(|id| Candidate { id, kind: Kind::Skill }).collect();
        let out = after_round(Round::First, &pool, &[
            Attempt { id: "floor", score: Some(0.65) },
            Attempt { id: "zero", score: Some(0.0) },
            Attempt { id: "low", score: Some(0.05) },
            Attempt { id: "error", score: None },
        ], TEST, false, false).unwrap();
        assert_eq!(out.skills.threshold, 0.65);
        assert_eq!(out.skills.n, 3);
        assert_eq!(out.skills.eligible[0].id, "floor");
        assert_eq!((out.prompts.n, out.prompts.median, out.prompts.mad, out.prompts.weight), (0, None, None, 0.0));
        assert!(out.prompts.eligible.is_empty());
        assert_eq!(out.to_score, [Candidate { id: "orphan", kind: Kind::Skill }]);
    }

    #[test]
    fn low_scores_do_not_imply_no_resource() {
        assert_eq!(fixture(0, 0, Round::First, false).0, Reason::MissingEligibleKinds);
        let pool = [Candidate { id: "present", kind: Kind::Skill }];
        let out = after_round(Round::First, &pool, &[], TEST, false, true).unwrap();
        assert_eq!(out.reason, Reason::ExplicitNoResourceEvidence);
        assert!(out.to_score.is_empty() && out.skills.eligible.is_empty() && out.prompts.eligible.is_empty());
        let none = after_round(Round::First, &[], &[], TEST, false, false).unwrap();
        assert_eq!(none.reason, Reason::PoolExhausted);
        assert_eq!((none.skills.n, none.prompts.n), (0, 0));
    }

    #[test]
    fn invalid_numeric_bounds_and_conflicting_kind_are_rejected() {
        let candidate = Candidate { id: "x", kind: Kind::Skill };
        for score in [f64::NAN, f64::INFINITY, -0.01, 1.01] {
            let tried = [Attempt { id: "x", score: Some(score) }];
            assert_eq!(after_round(Round::First, &[candidate], &tried, TEST, false, false).unwrap_err(),
                       InputError::InvalidScore);
        }
        for coefficients in [
            Coefficients { k: -1.0, h: 1.0 },
            Coefficients { k: f64::NAN, h: 1.0 },
            Coefficients { k: 0.0, h: 0.0 },
            Coefficients { k: 0.0, h: f64::INFINITY },
        ] {
            assert_eq!(after_round(Round::First, &[], &[], coefficients, false, false).unwrap_err(),
                       InputError::InvalidCoefficients);
        }
        let pool = [candidate, Candidate { kind: Kind::Prompt, ..candidate }];
        assert_eq!(after_round(Round::First, &pool, &[], TEST, false, false).unwrap_err(),
                   InputError::ConflictingKind);
    }

    #[test]
    fn cap_for_pool_and_attempts_uses_unique_ids() {
        let ids: Vec<_> = (0..301).map(|id| id.to_string()).collect();
        let mut pool: Vec<_> = ids.iter().map(|id| Candidate { id, kind: Kind::Skill }).collect();
        assert_eq!(after_round(Round::First, &pool, &[], TEST, false, false).unwrap_err(), InputError::PoolLimit);
        pool.pop();
        pool.push(pool[0]);
        let attempts: Vec<_> = pool.iter().take(25).map(|row| Attempt { id: row.id, score: None }).collect();
        assert_eq!(after_round(Round::First, &pool, &attempts, TEST, false, false).unwrap_err(), InputError::RoundBudget);
        assert_eq!(after_round(Round::First, &pool, &attempts[..24], TEST, false, false).unwrap().to_score.len(), 24);
    }
}
