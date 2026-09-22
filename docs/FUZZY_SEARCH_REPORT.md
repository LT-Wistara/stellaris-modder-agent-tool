# Fuzzy identifier retrieval: design, calibration and results (1.1.1)

The 1.4 catalog split changes public candidate eligibility, not scoring or token
calibration. The benchmark below documents the inherited retrieval engine; the full
regression suite still checks registry holdout recall and negative queries. Schema
fields are now retrieved through list/file-get instead of identifier search.

This document records how search finds the real CWT identifier behind an LLM's
approximate guess, how the score is calibrated against the actual corpus, and the
measured effect of the 1.3 rewrite against the 1.1 and 1.2 engines. Section 6 covers the
1.3.1 correctness fix that made token order actually binding.

Reproduce everything below with:

```powershell
python -m unittest discover -s tests -v
python scripts/evaluate_fuzzy.py --sweep
```

## 1. The problem

An LLM does not invent identifiers at random; it produces a name that is *close*
to a real one. Observed shapes:

| shape | example | real identifier |
|---|---|---|
| dropped token | `any_pop_job` | `any_owned_pop_job` |
| invented token | `has_background_pop_job` | `has_background_job` |
| misspelling | `has_backgroud_job` | `has_background_job` |
| adjacent transposition | `has_backgorund_job` | `has_background_job` |
| separator error | `any_owned_popjob`, `has_back_ground_job` | `any_owned_pop_job`, `has_background_job` |
| abbreviation | `num_assi_jobs`, `count_avai_debris` | `num_assigned_jobs`, `count_available_debris` |
| incomplete template instance | `country_resource_max_energy_ad` | `country_resource_max_<resource>_add` |
| wrong word, plausible shape | `country_resource_energy` | *nothing* - must stay `NOT_FOUND` |

The requirement is asymmetric. Missing a real candidate costs the Agent one more
search; suggesting a **wrong** interface with high relevance makes the Agent write
broken script and trust it. The last row is therefore a hard regression case.

## 2. Architecture

```
query
  │  parse: `_`-separated tokens, CWT placeholders atomic, cached per query
  ├── re-segmentation variants (`has_back_ground_job` -> has_background_job)
  ▼
shortlist  ── 5 independent strategies, unioned, never short-circuited
  │   exact token postings          `job`
  │   token typo postings (bigrams) `backgroud` -> `background`
  │   compound token postings       `pop`+`job` -> `pop_job`, `continental_hab`
  │   rare-token completion seeds   `set_flag` -> `set_*_flag`
  │   template literal runs         `<planet_class>_habitability`
  │   (+ plain substring for short queries, browse tier only)
  ▼
cheap bound   ordered-assignment upper bound on the final score
  ▼
alignment     weighted Damerau-style DP over tokens, full edit script
  ▼
fact sheet    MatchFacts - the only thing the scorer may look at
  ▼
one score     scoring.score_match, plus the bounded completion bonus
  ▼
rank          results (>=55) and related (browse context), by score then name
```

`retrieval.py` only ranks names that really exist in the index. **It never decides
that an interface exists.** `index.Database.lookup` (CWT exact declaration and full
dynamic-template structure) remains the only source of `CONFIRMED_CWT` and
`TEMPLATE_MATCH`, and it runs first, so a fuzzy row can never displace real evidence.

### Score

```
support  = (matched query tokens / query tokens) * (in-order matched mass / matched mass)
coverage = matched fixed literal mass / candidate fixed literal mass
align    = 1 - edit cost / (larger token mass * leniency floor)
base     = align * (0.55 + 0.45 * support)
score    = clamp(100 * (base + 0.20 * info_factor * competition_factor), 0, 98)
```

* Deleting a whole token is never free, so a candidate that ignores the query stays
  low.
* A query token the candidate *does* contain but did not use costs double, so an
  equal-cost alignment prefers the explanation a user expects
  (`has_background_pop_job` -> `has_background_job` + extra `pop`, not `+ extra has`).
* Order is binding, not a tie-break: the second support factor drops the mass of any
  matched token whose candidate position breaks query order (section 6).
* `info_factor` / `competition_factor` prefer completions that add rare, informative
  tokens over completions of a ubiquitous token (`set_flag` has ~30 equally plausible
  completions and therefore earns almost no bonus). The term is bounded by 20 points.

### Why `match_score` is not evidence

`match_score` answers "how close is this real name to what was typed". It cannot
answer "does this interface exist in your load order", because the corpus contains no
game objects. A 98-point suggestion stays `SUGGESTION`; only CWT evidence upgrades a
row. `get_definition` and `validate` never call the fuzzy ranker.

## 3. Calibration against the real corpus

`scripts/evaluate_fuzzy.py` perturbs real CWT identifiers into five mutation classes
plus ten unrelated negatives, splitting by source name into disjoint `calibration`
(threshold picking) and `holdout` (reporting) halves.

* corpus: 173 CWT files, 23,057 symbols, 9,944 unique names
* corpus-derived cases: 1,317 = 498 calibration + 507 holdout + 12 negative + 300 permutation
* seed 20260919, so the numbers are reproducible byte for byte

Live version numbers (`pyproject.toml`, `stellaris_modder_agent.__version__`, the MCP
handshake, this document and the README) are asserted to agree by `VersionCase` in
`tests/test_fuzzy_search.py`. `pyproject.toml` declares the version once and
`stellaris_modder_agent/__init__.py` reads it from there. `docs/FUZZY_BASELINE.json` keeps its
own historical `version` field on purpose: it is a frozen measurement of the 1.1 release.

Selected thresholds and what set them:

| constant | value | fixed by |
|---|---|---|
| `MIN_SCORE` | 55 | sweep below; the single negative row appears only under 55 |
| `MIN_QUERY_SUPPORT` | 0.50 | below this, `country_resource_max_energy` loses its completion and `continental_hab` its template |
| `MIN_CANDIDATE_COVERAGE` | 0.50 | fixed literals must carry half the dynamic template |
| `TEMPLATE_MASS_RATIO` | 0.55 | at 0.60 `country_resource_max_energy_ad` was lost, below 0.55 the `country_resource_energy` shape returned |
| `ABSORBED_MASS_FACTOR` / `MAX_ABSORBED_MASS_SHARE` | 0.70 / 0.55 | a wildcard eating several query tokens must stay smaller than the literal evidence |
| `LENIENCY_FLOOR` | 0.70 | stops a short query from discounting its own dropped mass |
| `SCORE_WEIGHTS['typo']` | 0.16 | keeps a one-character typo above 90 |
| `MAX_ALIGN_CANDIDATES` | 400 | latency bound; the assignment bound already removed the rest |

### Cutoff sweep (calibration half)

| MIN_SCORE | hit@1 | hit@5 | negatives returned |
|---|---|---|---|
| 45 | 462/503 | 500/503 | 0 |
| 50 | 462/503 | 500/503 | 0 |
| **55** | **462/503** | **500/503** | **0** |
| 60 | 461/503 | 498/503 | 0 |
| 65 | 456/503 | 489/503 | 0 |
| 70 | 419/503 | 456/503 | 0 |

55 is the selected cutoff: it is the lowest value with zero negative rows, and raising
it only loses recall.

### Holdout results

| mutation | hit@1 | hit@5 |
|---|---|---|
| separator | 86/86 | 86/86 |
| transposition | 86/86 | 86/86 |
| typo | 85/86 | 86/86 |
| truncation / abbreviation | 83/86 | 86/86 |
| extra token | 82/85 | 85/85 |
| missing token | 57/78 | 73/78 |
| **total** | **479/507 (94.5%)** | **502/507 (99.0%)** |
| negatives (12) | 0 returned | 0 returned |

Latency per query over the whole case set: median 30.6 ms, p95 102.7 ms, max 203.2 ms
(including the first, cold query). A typical Agent lookup is 10-35 ms.

The missing-token class is the honest residual. `has_flag -> has_design_flag` and
`set_flag -> set_global_flag` are genuinely ambiguous: dozens of real identifiers
contain the query's tokens as an ordered subset and nothing in the corpus says which
one the user meant. Those queries still return the plausible family at high score, so
the Agent sees `has_ship_flag`, `has_planet_flag`, ... and can pick with evidence.

## 4. Before and after

`MISS` means the top result was not the intended identifier.

| query | 1.1 behaviour | 1.3 behaviour |
|---|---|---|
| `any_pop_job` | MISS (`NOT_FOUND`) | `any_owned_pop_job` 87.8 `token_edit`, missing `owned` |
| `has_backgroud_job` | MISS (`NOT_FOUND`) | `has_background_job` 93.8, substitution `backgroud`->`background` |
| `has_backgorund_job` | MISS | `has_background_job` 93.8 |
| `any_owned_popjob` | MISS | `any_owned_pop_job` 84.8, separator fix |
| `has_back_ground_job` | MISS | `has_background_job` 98.0, separator fix |
| `num_assi_jobs` | MISS | `num_assigned_jobs` 91.8 (abbreviation, new in 1.3) |
| `has_type` | MISS | `has_federation_type` 82.4, missing `purge`/`job`/... |
| `set_flag` | MISS | `set_ship_flag` 82.8 (30-name family, honestly tied) |
| `country_resource_energy` | `NOT_FOUND` | `NOT_FOUND` (regression held) |
| `country_resource_max_energy` | `country_resource_max_<resource>_add` | same, 96.8 |
| `country_resource_max_energy_ad` | `country_resource_max_<resource>_add` | same, 83.0 |
| `has_background_pop_job` | MISS | `has_background_job` 73.1, extra `pop` |
| `country_tech_research_speed` | `country_*_tech_research_speed` 77-92 | same, 89.7 |

Aggregate, same harness, same corpus:

| engine | hit@1 | hit@5 | negatives returned |
|---|---|---|---|
| 1.1 (substring + anchored template prefix) | 0 % | 0 % | 0 |
| 1.2 (ordered token edit) | see 1.2 notes | see 1.2 notes | 0 |
| **1.3 (this report)** | **94.4 %** | **99.0 %** | **0** |

`docs/FUZZY_BASELINE.json` holds the measured 1.1 numbers, including the mutation
breakdown where 1.1 returned nothing at all for 501 of 513 corpus-derived misspellings.

## 5. What the Agent sees

Each candidate carries the score, the kind, and the explanation:

```json
{
  "name": "any_owned_pop_job",
  "status": "SUGGESTION",
  "match_score": 87.85,
  "match_kind": "token_edit",
  "query_support": 1.0,
  "candidate_coverage": 0.7676,
  "token_diff": {
    "matched_tokens": ["any", "pop", "job"],
    "missing_tokens": ["owned"]
  }
}
```

`missing_tokens` = the candidate has them and the query does not; `extra_tokens` = the
query has them and the candidate does not; `substitutions` gives query/candidate pairs
with `kind` (`typo`, `prefix`, `separator`) and edit distance;
`placeholder_bindings` explains template alignment only and does not prove that the
bound object exists.

Weak rows that only share tokens with the query (`country_resource_max_add` for
`country_resource_energy`) are listed separately under `related`, below `MIN_SCORE`,
and never inside `results`. That split is what lets the tool stay useful for browsing
without ever presenting a wrong interface as a suggestion.

## 6. Token order is enforced, not preferred (1.3.1)

The 1.3 engine claimed ordered alignment but did not enforce it. `_pair_options`
answered "can candidate token *j* be matched?" with a cost table keyed only by the
candidate position, built from the union of every query token's readings. The dynamic
program therefore had no way to tell which query token a cost belonged to, and because
every query token's own exact form was in that union, a candidate token listed at cost
zero could be consumed by *any* remaining query token. Measured directly:

```
any_pop_job    ('any','pop','job')   candidate[0]=any -> cost 0.00 exact   <- which query token?
job_any_pop    ('job','any','pop')   candidate[0]=any -> cost 0.00 exact   <- identical table
```

Both queries produced byte-identical cost tables, so `job_any_pop` scored the same as
`any_pop_job` against `any_owned_pop_job`.

### Fix

1. **Readings are indexed by query token position.** `_readings(parts)` returns one
   reading map per position, and `_pair_options` returns
   `{candidate index: {query index: (cost, kind, distance)}}`. A candidate token can only
   be matched by a reading that *that* query token produced. The cost table for
   `job_any_pop` is now different from the one for `any_pop_job`.
2. **Support is weighted by how much of the matched mass stays in query order.** The
   longest increasing run of candidate positions, weighted by real corpus token mass,
   over the matched mass. Deleting a query token or inserting a candidate token is still
   free (the unmatched mass is simply absent), but reversing the tokens that *did*
   correspond loses the mass it had to reverse.

The prefilter's ordered-assignment bound (`_assignment`, `_greedy_cost`) had the same
identity-free shape and was fixed with it, so the cheap bound stays a valid upper bound
on the new scorer.

### Effect

| query | target | before | after |
|---|---|---|---|
| `any_pop_job` | `any_owned_pop_job` | 87.85, rank 1 | **87.85, rank 1** |
| `job_any_pop` | `any_owned_pop_job` | ~87, rank 1 | **31.53, browse tier only** |
| `owned_any_job_pop` | `any_owned_pop_job` | ~87, rank 1 | **41.22, browse tier only** |
| `has_backgroud_job` | `has_background_job` | 93.81, rank 1 | **93.81, rank 1** |
| `background_has_job` | `has_background_job` | ~98, rank 1 | **60.17, rank 1** |
| `job_background_has` | `has_background_job` | ~98 | **absent from both tiers** |
| `country_resource_energy` | - | `NOT_FOUND` | `NOT_FOUND` |

### Measured by the benchmark

`scripts/evaluate_fuzzy.py` gained a `permutation` class: real identifiers with their
tokens reversed (`reverse`) or rotated (`rotate`), skipped when the shuffle happens to
spell another real identifier. These have no correct answer, so the class measures the
false-suggestion rate directly.

| class | suggested |
|---|---|
| `permutation/reverse` (150) | 2 |
| `permutation/rotate` (150) | 110 |

Reversals are effectively eliminated. A single-token rotation is the honest residual:
`border_any_pre_ftl_within` for `any_pre_ftl_within_border` still matches
`any_pre_ftl_within_border`, because four of its five tokens are in order and the
displaced `border` is genuinely present in the query. That string is not a real
identifier, but it does name four of that identifier's tokens in order, so a suggestion
is defensible rather than a scoring failure. Some of the 110 are also independent
correct hits on real names that happen to share the rotated tokens.

Non-permutation classes are unchanged by this work, because a dropped token, an invented
token, a typo and a separator error all preserve the order of the tokens that remain:

| metric | 1.3 | 1.3.1 |
|---|---|---|
| calibration hit@1 | 482/503 | 482/503 |
| holdout hit@1 | 485/513 | 485/513 |
| holdout hit@5 | 508/513 | 508/513 |
| negatives returned | 0 | 0 |
| latency median / p95 | 20.9 ms / 53.7 ms | 20.9 ms / 53.7 ms |

### Fuzzy candidates are deduplicated by identifier

An identifier with several overloads used to occupy several Top-N positions. `results`
and `related` now carry one row per identifier, chosen by the same ranking, with
`definition_count` and (when there is more than one location) `definition_ids` naming the
collapsed locations. `CONFIRMED_CWT` and `TEMPLATE_MATCH` rows are the CWT declarations
themselves and are left untouched, and `get_definition` still returns every overload.

### The two tiers are disjoint by identity

Tier membership was decided with `row not in suggestions`. `deduplicate` returns
enriched *copies* of the rows it keeps (it adds `definition_count` / `definition_ids`),
so comparing a plain row with dictionary equality never matched and every summarised
suggestion was listed a second time under `related`:

```
results: has_background_job, has_job_type, has_job_category
related: has_background_job, has_job_type, has_job_category, ...   # same ids
```

Membership and deduplication now use `index.identity(row)` — the stable
`(identifier, canonical type)` key — and never compare whole result dicts. A candidate
that reached `results` cannot appear in `related`, while the two tiers stay independent
for a name that legitimately exists under two canonical types. Exact and template
evidence rows are excluded from the browse tier as well, since their declarations can
share a name with a suggestion.

Verified over all 1,317 benchmark queries: `results ∩ related` is empty for every one,
and `tests/test_fuzzy_search.py` asserts disjointness, that a summarised row and a plain
row of one candidate share an identity, and that collapsing is per `(identifier, type)`.

## 7. Deliberate limits

* No embeddings, no network calls, no generated symbol names. Standard library only,
  so the ZIP stays installable with no pip step.
* A candidate is only ever a name that exists in the pinned CWT commit.
* `match_score` is a ranking value, not a calibrated probability.
* The browse tier is bounded (`RELATED_LIMIT`, `RELATED_MIN_SCORE`) so a very short
  query cannot return a thousand rows.
* Placement in `results` vs `related` is decided by the same scoring model, not by a
  separate heuristic.

## 8. Literal object-reference grounding

The same offline evaluation now includes six validation cases backed by synthetic game
data: real, misspelled and invented building and technology identifiers. Building reuses
the normal dynamic index; technology exercises the per-type lazy scan. All six cases pass:
real objects return `OBJECT_REFERENCE`, invented objects return
`UNKNOWN_OBJECT_REFERENCE`, and both misspellings return the real object as a candidate.
