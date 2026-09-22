"""Single scoring model for identifier retrieval. Relevance only, never evidence.

Every candidate name is scored exactly once, whatever strategy found it:

    base   = alignment * (FUZZY_PENALTY + (1 - FUZZY_PENALTY) * query_support)
    score  = clamp(100 * (base + INFO_BONUS * info_factor * competition_factor), 0, 98)

* ``alignment = 1 - align_cost / token_mass`` — the weighted edit cost of turning
  the query into the candidate, over the larger token mass involved. Deleting a
  whole token is never free, so a candidate that swallows the query stays low.
* ``query_support`` — share of query token *mass* explained by real literal
  correspondence (exact / typo / prefix / separator edit). A name matched only
  through a CWT placeholder, or only through its first token, cannot be high.
* ``info_factor`` — share of the candidate's information content that the query
  does not already explain, i.e. how much the completion adds.
* ``competition_factor`` — how informative the query itself is: a query that only
  matches ubiquitous tokens (``set_flag``, ``has_type``) has many completions and
  earns almost no bonus, while a distinctive one earns close to the cap.

The bonus is bounded by ``INFO_BONUS`` (0.20) and multiplies two factors that are
each at most 1, so it is a tie-break between comparable alignments. It cannot
promote a weak alignment into the result tier by itself.

``evidence.TEMPLATE_MATCH``/``CONFIRMED_CWT`` are decided outside this module and
always score above any suggestion (`MAX_SUGGESTION_SCORE`).
"""
from __future__ import annotations

from dataclasses import dataclass

# Result tiers.
MIN_SCORE = 55.0                   # at or above: could be a ranked suggestion
MIN_QUERY_SUPPORT = 0.50           # ranked suggestions explain this share of the query
RELATED_QUERY_SUPPORT = 0.20       # below this a name only shares tokens: browse context
MIN_CANDIDATE_COVERAGE = 0.50      # dynamic template: fixed literals must carry the match
MAX_SUGGESTION_SCORE = 98.0        # exact name = 100, full template = 99
FULL_TEMPLATE_SCORE = 99.0

# Alignment weights (relative, per token mass unit).
SCORE_WEIGHTS = {
    'rarity_log': 8.0,             # bounded rarity: 1.0 .. 2.0
    'missing': 0.45,               # candidate-only token (query forgot it)
    'extra': 0.85,                 # query-only token (candidate does not have it)
    'typo': 0.16,                  # per character of token typo distance
    'prefix': 0.28,                # truncated final token
    'join': 0.22,                  # query token == two adjacent candidate tokens
    'split': 0.22,                 # two adjacent query tokens == one candidate token
    'template': 0.45,              # placeholder absorbs query tokens
}

FUZZY_PENALTY = 0.55               # part of the score that pure overlap can keep
COVERAGE_FLOOR = 0.75              # how much of the score template coverage can keep
TEMPLATE_ANCHOR = 0.40             # fixed-literal character evidence (`continental_hab`)
TEMPLATE_LITERAL_SHARE = 0.50      # share of the query's characters fixed literals must carry
TEMPLATE_MASS_RATIO = 0.55         # share of the query's token mass fixed literals must carry
MAX_ABSORBED_MASS_SHARE = 0.55     # a wildcard must not eat most of the query token mass
TEMPLATE_EXPLAINED_SHARE = 0.75    # share of tokens some part of the template must account for
TEMPLATE_SOLID_SUPPORT = 0.90      # above this query support a template needs no literal rule
LENIENCY_FLOOR = 0.70              # how far a short query may discount the alignment denominator
INFO_BONUS = 0.20                  # cap of the completion bonus
COMPETITION_SCALE = 3.0            # matched-information / candidate-information scale
MIN_MASS = 1e-9


def literal_anchor(literals, query):
    """Fixed-literal evidence in the raw query, as ``(anchored, prefix_length, covered)``.

    `covered` counts the query characters spelled out by fixed literals, so a broad
    wildcard that swallows most of a query is visible even when its short suffix
    aligns. `<planet_class>_habitability` against `continental_hab` covers only `hab`
    and is anchored by that 3-character run; `<district.capped>_max_add` against
    `country_resource_max_energy_add` covers `max` and anchors on `country`.
    """
    needle = query.replace('_', '').casefold()
    if not needle:
        return False, 0, 0
    best, covered = 0, 0
    for literal in literals:
        segment = literal.replace('_', '')
        if len(segment) < 2:
            continue
        if len(segment) >= 4 and segment in needle:
            covered += len(segment)
            best = max(best, len(segment))
            continue
        size = 0
        while size < len(segment) and size < len(needle) and segment[size] == needle[size]:
            size += 1
        if size >= 3 and size >= 0.6*len(needle):
            best = max(best, size)
        covered += size
    return best >= 3, best, covered


def score_components(cost, query_mass, candidate_mass, literal_qmass, literal_cmass, in_template,
                     anchor=0.0):
    """Return (base 0..1+, query_support 0..1, candidate_coverage 0..1).

    The alignment term is normalised by the larger token mass and then discounted by
    how much smaller the other side is, so dropping query mass can never be cheaper
    than matching it: a candidate that ignores most of the query cannot stay high.
    """
    mass = max(query_mass, candidate_mass, MIN_MASS)
    support = min(1.0, literal_qmass/max(query_mass, MIN_MASS))
    coverage = min(1.0, literal_cmass/max(candidate_mass, MIN_MASS))
    leniency = max(LENIENCY_FLOOR, min(query_mass, candidate_mass)/mass)
    alignment = max(0.0, 1.0 - cost/max(mass*leniency, MIN_MASS))
    base = alignment*(FUZZY_PENALTY + (1.0-FUZZY_PENALTY)*support)
    if in_template:
        # Fixed literals must carry the match; a broad wildcard cannot absorb a query.
        base = base*(COVERAGE_FLOOR + (1.0-COVERAGE_FLOOR)*coverage) + TEMPLATE_ANCHOR*anchor*coverage
    return base, support, coverage


def score_information(candidate, captured, missing, parts, matched_information, support, info):
    """Return {'factor': 0..1, 'competition': 0..1} for the completion bonus."""
    candidate_information = sum(info(token) for j, token in enumerate(candidate) if j not in captured)
    added = sum(info(token) for token in missing if token not in captured)
    total = candidate_information + sum(info(token) for token in parts)
    factor = min(1.0, added/max(total, MIN_MASS))
    competition = min(1.0, COMPETITION_SCALE*support*matched_information/max(candidate_information, MIN_MASS))
    return {'factor': factor, 'competition': competition}


@dataclass
class MatchFacts:
    """Everything the scoring model is allowed to look at for one aligned candidate."""
    cost: float
    query_mass: float
    candidate_mass: float
    literal_qmass: float             # query mass the alignment explained, capture included
    literal_cmass: float             # candidate mass explained by fixed literals
    query_tokens: int
    matched_query_tokens: int        # query tokens with real literal correspondence
    matched_query_mass: float        # their token mass
    in_order_query_mass: float       # mass of the matched tokens still in query order
    absorbed_mass: float             # query mass swallowed by CWT placeholders
    absorbed_tokens: int             # query tokens swallowed by CWT placeholders
    candidate_tokens: int
    placeholders: int
    bound_placeholders: int          # placeholders that actually bound a query token
    explained: int                   # query tokens either matched literally or bound
    absorbed: int = 0                # largest number of query tokens one placeholder took
    template: bool = False
    anchored: bool = False
    anchor_length: int = 0
    covered: int = 0                 # query characters spelled out by fixed literals
    query_chars: int = 0


def score_match(facts: MatchFacts):
    """Score one aligned candidate, or return None when it fails a template gate.

    Returns ``(base 0..1, query_support, candidate_coverage)``. ``base`` is the whole
    score before the bounded completion bonus.

    Support is the share of query *tokens* with real literal correspondence, scaled by
    how much of that matched mass still follows query order. A wildcard that merely
    absorbs a word cannot make it look matched, and a permutation cannot masquerade as
    the intended identifier: dropping a query token or inserting a candidate token is
    allowed, reversing the tokens that did correspond is not. Every gate below exists so
    that broad wildcard absorption cannot become a high-relevance suggestion; the
    regression case is `country_resource_energy` never ranking
    `country_<leader_class.capped>_cap_add`.
    """
    mass = max(facts.query_mass, facts.candidate_mass, MIN_MASS)
    token_ratio = min(1.0, facts.matched_query_tokens/max(facts.query_tokens, 1))
    order_share = min(1.0, facts.in_order_query_mass/max(facts.matched_query_mass, MIN_MASS))
    support = token_ratio*order_share
    coverage = min(1.0, facts.literal_cmass/max(facts.candidate_mass, MIN_MASS))
    leniency = max(LENIENCY_FLOOR, min(facts.query_mass, facts.candidate_mass)/mass)
    alignment = max(0.0, 1.0 - facts.cost/max(mass*leniency, MIN_MASS))
    base = alignment*(FUZZY_PENALTY + (1.0-FUZZY_PENALTY)*support)
    if not facts.template:
        return base, support, coverage
    # A dynamic template must be carried by its fixed literal tokens: every placeholder
    # has to bind, the literals must spell out a real share of the query, and a broad
    # wildcard may never swallow query tokens that a specific literal could have taken.
    if coverage < MIN_CANDIDATE_COVERAGE:
        return None
    # Measured against the raw query mass, not the mass left after placeholder capture:
    # absorbing query tokens must never make a template look better supported.
    literal_share = facts.literal_cmass/max(facts.query_mass, MIN_MASS)
    if literal_share < TEMPLATE_MASS_RATIO:
        return None
    # A name good enough to rank must bind every placeholder; a weaker one is only
    # browse context, and there an unbound placeholder is acceptable.
    if support >= MIN_QUERY_SUPPORT and facts.bound_placeholders != facts.placeholders:
        return None
    if facts.placeholders and facts.explained < TEMPLATE_EXPLAINED_SHARE*max(facts.query_tokens,
                                                                            facts.candidate_tokens):
        return None
    if support < TEMPLATE_SOLID_SUPPORT and not facts.anchored:
        return None
    if facts.covered < TEMPLATE_LITERAL_SHARE*max(facts.query_chars, 1):
        return None
    # A wildcard that swallows several query tokens instead of letting literals claim
    # them is weak evidence for a ranked suggestion; only the browse tier may keep it.
    # The literal-share gate above already keeps the wildcard smaller than the template's
    # own words, so this only has to bound what the wildcard may eat of the query.
    if facts.absorbed > 1 and facts.absorbed_mass/max(facts.query_mass, MIN_MASS) > MAX_ABSORBED_MASS_SHARE:
        return None
    if facts.anchored:
        # How much of the *query* the template's fixed literals spell out. Placeholder
        # names never enter the anchor, so one template family ranks consistently.
        anchor = min(1.0, facts.anchor_length/max(facts.query_chars, 1))*coverage
        base += TEMPLATE_ANCHOR*anchor
    return base, support, coverage