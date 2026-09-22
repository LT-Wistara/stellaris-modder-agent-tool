"""Identifier retrieval for LLM guesses against the CWT symbol corpus.

This module ranks *candidate names that really exist in the index*. It never
decides that an interface exists: `index.Database.lookup` (CWT exact and template
evidence) stays the only source of CONFIRMED_CWT / TEMPLATE_MATCH. Everything
produced here is a SUGGESTION no matter how high `match_score` is.

Pipeline
--------
1. **Parse** the query into `_`-separated tokens, keeping CWT placeholders atomic
   and recording a compound reading (`popjob` -> `pop` + `job`).
2. **Shortlist** candidates from several independent indexes: exact token
   postings, token-typo postings, compound split/join postings, terminal token
   prefix postings and rare-token postings. No strategy ends the search early;
   the unions are merged before scoring, so a weak substring hit can never block
   a stronger token edit found by another strategy.
3. **Align** each shortlisted name with a weighted dynamic program over tokens
   that yields a complete edit script (matched / missing / extra / substituted).
4. **Score** every name once with one formula (see `scoring.py`), then merge,
   deduplicate by name and sort.

Explanation
-----------
Each candidate carries `token_diff` with the non-empty subset of
`matched_tokens`, `missing_tokens` (candidate-only), `extra_tokens`
(query-only), `substitutions` (typo / prefix / separator plus edit distance) and
`placeholder_bindings`, plus `query_support` and `candidate_coverage`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import math
import re

from .scoring import (INFO_BONUS, MAX_SUGGESTION_SCORE, MIN_SCORE, RELATED_QUERY_SUPPORT,
                      MatchFacts, SCORE_WEIGHTS, literal_anchor, score_information, score_match)
from .templates import PLACEHOLDER, OBJECT, Template

MIN_CANDIDATE_CHARS = 3            # ignore 1-2 character name noise
MAX_TOKENS = 24                    # neither side of an identifier guess is longer

# Shortlisting bounds. These trade recall for latency; the corpus has ~10k
# unique names and ~3.4k unique tokens.
MAX_ALTERNATIVES = 60              # typo/prefix readings kept per query token
MAX_ALIGN_BUDGET = 1400            # names considered per query, best-supported first
MAX_ALIGN_CANDIDATES = 400         # names fully aligned per query
PREFILTER_SLACK = 2.0              # margin for the bounded completion bonus
ASSIGNMENT_TOKENS = 12             # above this the ordered-assignment bound is skipped
MAX_RARE_SEEDS = 150               # rarest query tokens used as completion seeds
RELATED_MIN_SCORE = 25.0           # below this a name is not even browse context
SUBSTRING_TOKENS = 2               # plain-text browsing is only offered for short queries
SUBSTRING_CHARS = 24
PREFIX_MIN_CHARS = 3
OOV_WEIGHT = 1.4                   # a token that exists nowhere in the corpus is a suspect
PLACEHOLDER_WEIGHT = 1.0           # CWT placeholders are not name tokens; keep them neutral
TYPO_MIN_CHARS = 3
TYPO_BOUND_LONG = 2                # token length >= 7 allows two edits
TYPO_MAX_RATIO = 0.34
JOIN_MIN_CHARS = 4
TEMPLATE_RUN_PREFIX = 3            # literal characters needed to shortlist a template
# Equal-cost alignments are broken by this key, lowest wins. The order prefers a real
# correspondence over a dropped token, and among dropped tokens the earliest one, so the
# report reads "has_background_job + extra pop" instead of "+ extra job".
_TIE_BREAK = {'exact': 0, 'typo': 1, 'prefix': 1, 'join': 2, 'capture': 3, 'missing': 4, 'extra': 5}


# Operations that establish real literal correspondence between a query token and a
# candidate token. A query token dropped as `extra` or a candidate token charged as
# `missing` is not correspondence, so it never counts toward support.
MATCH_OPERATIONS = ('exact', 'typo', 'prefix', 'join')


def _prefer(operation, prior):
    """Lowest key wins on an equal-cost alignment."""
    if prior is None:
        return True
    kind, index = operation[0], operation[1]
    return (_TIE_BREAK[kind], -index if kind == 'extra' else index) < \
           (_TIE_BREAK[prior[0]], -prior[1] if prior[0] == 'extra' else prior[1])

IDENTIFIER = re.compile(r'[\w.-]+\Z', re.UNICODE)


def tokens(name):
    """Split on `_` while keeping each CWT placeholder (`<x>`, `enum[x]`) atomic."""
    parts, pos = [], 0
    folded = name.casefold()
    for match in PLACEHOLDER.finditer(folded):
        parts.extend(t for t in folded[pos:match.start()].split('_') if t)
        parts.append(match.group())
        pos = match.end()
    parts.extend(t for t in folded[pos:].split('_') if t)
    return tuple(parts)


def is_placeholder(token):
    return PLACEHOLDER.fullmatch(token) is not None


def bigrams(token):
    padded = '^' + token + '$'
    return {padded[i:i+2] for i in range(len(padded)-1)}


def literal_runs(literals):
    """Character runs carried by fixed template literals, ignoring separators."""
    return tuple(run for literal in literals for run in literal.split('_') if len(run) >= 2)


def edit_distance(left, right, maximum=2):
    """Bounded optimal-string-alignment distance, including adjacent transposition."""
    if left == right:
        return 0
    if abs(len(left)-len(right)) > maximum:
        return maximum + 1
    previous = list(range(len(right)+1))
    older = None
    for i, a in enumerate(left, 1):
        current = [i]
        for j, b in enumerate(right, 1):
            cost = min(current[-1]+1, previous[j]+1, previous[j-1]+(a != b))
            if older is not None and j > 1 and a == right[j-2] and left[i-2] == b:
                cost = min(cost, older[j-2]+1)
            current.append(cost)
        older, previous = previous, current
    return previous[-1]


@lru_cache(maxsize=4096)
def query_tokens(query):
    """Query-side tokenization, cached across repeated Agent calls."""
    return tokens(' '.join(query.split()))


@dataclass
class NameEntry:
    name: str
    tokens: tuple
    template: Template
    symbols: list
    accepted: int = 0
    query_tokens: tuple = ()          # scratch: query tokens of the current alignment


@dataclass
class Match:
    """One scored candidate name. The score is relevance, never existence evidence."""
    name: str
    match_score: float
    match_kind: str
    token_diff: dict = field(default_factory=dict)
    query_support: float = 0.0
    candidate_coverage: float = 0.0
    symbols: list = field(default_factory=list)

    def as_dict(self):
        row = {'match_score': self.match_score, 'match_kind': self.match_kind,
               'query_support': self.query_support, 'candidate_coverage': self.candidate_coverage}
        if self.token_diff:
            row['token_diff'] = self.token_diff
        return row


class Retrieval:
    """Read-only index over unique symbol names; built once per Database."""

    def __init__(self, symbols):
        self.entries = {}
        for symbol in symbols:
            key = symbol.name.casefold()
            entry = self.entries.get(key)
            if entry is None:
                entry = self.entries[key] = NameEntry(symbol.name, tokens(key), symbol.template, [])
            entry.symbols.append(symbol)
        self.names = sorted(self.entries)
        self.total_names = len(self.names)
        self.dynamic = tuple(key for key in self.names if self.entries[key].template.dynamic)
        postings, frequencies = {}, {}
        join_index, split_index = {}, {}
        for key in self.names:
            parts = self.entries[key].tokens
            for token in set(parts):
                if is_placeholder(token) or not IDENTIFIER.fullmatch(token):
                    continue
                postings.setdefault(token, []).append(key)
                frequencies[token] = frequencies.get(token, 0) + 1
            for index in range(len(parts)-1):
                left, right = parts[index], parts[index+1]
                if is_placeholder(left) or is_placeholder(right):
                    continue
                if len(left) >= JOIN_MIN_CHARS and len(right) >= JOIN_MIN_CHARS:
                    join_index.setdefault(left+right, set()).add(key)
                if len(left) >= 2 and len(right) >= 2 and len(left)+len(right) >= 6:
                    split_index.setdefault(left+'_'+right, set()).add(key)
        self.postings = postings
        self.join_index = join_index
        self.split_index = split_index
        self.grams = {}
        for token in postings:
            for gram in bigrams(token):
                self.grams.setdefault(gram, set()).add(token)
        # Dynamic templates are reachable through their fixed literals, which are not
        # tokens of any real name (`continental_hab` -> `<planet_class>_habitability`).
        self.literal_runs = {}
        for key in self.dynamic:
            for run in literal_runs(self.entries[key].template.literals):
                self.literal_runs.setdefault(run[:TEMPLATE_RUN_PREFIX], set()).add(key)
        # Bounded rarity: rare jargon outranks filler words without unbounded IDF.
        self.weights = {token: 1.0 + min(1.0, math.log((self.total_names+1)/(count+1))/SCORE_WEIGHTS['rarity_log'])
                        for token, count in frequencies.items()}
        self.information = {token: max(0.0, math.log(self.total_names/max(count, 1)))
                            for token, count in frequencies.items()}
        # `avai` -> `available`: index every fragment of a token that names something.
        # Fragments of ubiquitous stems (`has`, `set`) are skipped so a short guess can
        # never drag in their hundreds of completions.
        self.rare = {token: len(keys) <= MAX_RARE_SEEDS for token, keys in postings.items()}
        self.prefix_index = {}
        for token in postings:
            if not self.rare[token] or len(token) < PREFIX_MIN_CHARS:
                continue
            for size in range(PREFIX_MIN_CHARS, len(token)):
                self.prefix_index.setdefault(token[:size], set()).add(token)

    # -- helpers -------------------------------------------------------------
    def weight(self, token):
        """Token mass. Unknown tokens (likely typos) must not look maximally important."""
        return self.weights.get(token, OOV_WEIGHT)

    def info(self, token):
        return self.information.get(token, 0.0)

    def completion_count(self, keys):
        """How many index names contain every token of `keys` (query ambiguity)."""
        sets = [set(self.postings.get(token, ())) for token in keys if not is_placeholder(token)]
        sets = [entry for entry in sets if entry]
        if not sets:
            return 1
        sets.sort(key=len)
        count = sets[0]
        for other in sets[1:]:
            count &= other
            if not count:
                return 0
        return len(count)

    @lru_cache(maxsize=4096)
    def alternatives(self, token):
        """Cheap token-level readings: exact, prefix, typo, compound join, compound split."""
        result = {}
        if not IDENTIFIER.fullmatch(token) or len(token) > 128:
            return result
        if token in self.postings:
            result[token] = (0.0, 'exact')
        if len(token) >= TYPO_MIN_CHARS:
            fused = set()
            for gram in bigrams(token):
                fused.update(self.grams.get(gram, ()))
            for candidate in fused:
                if candidate == token:
                    continue
                shorter = min(len(token), len(candidate))
                bound = TYPO_BOUND_LONG if shorter >= 7 else 1
                if shorter < TYPO_MIN_CHARS or abs(len(token)-len(candidate)) > bound:
                    continue
                distance = edit_distance(token, candidate, bound)
                if distance and distance/max(len(token), len(candidate)) <= TYPO_MAX_RATIO:
                    result[candidate] = (float(distance), 'typo')
        # Separator mistakes stay conservative: the halves must be individually specific.
        for candidate in self.join_index.get(token, ()):
            result[candidate] = (0.0, 'join')
        for candidate in self.split_index.get(token, ()):
            result[candidate] = (0.0, 'split')
        if self.rare.get(token):
            # Abbreviation: `avai` -> `available`. Only tokens the corpus confirms as a
            # completion of this fragment are reachable, so a short stem like `has`
            # cannot pull in its hundreds of `has_*` completions through this index.
            for candidate in self.prefix_index.get(token, ()):
                result.setdefault(candidate, (0.0, 'prefix'))
        if len(result) > MAX_ALTERNATIVES:
            ranked = sorted(result.items(), key=lambda kv: (kv[1][1] != 'exact', kv[1][0], kv[0]))
            return dict(ranked[:MAX_ALTERNATIVES])
        return result

    @lru_cache(maxsize=4096)
    def templates_for(self, query):
        """Dynamic templates whose fixed literals spell out part of the raw query."""
        needle = query.replace('_', '').casefold()
        if len(needle) < TEMPLATE_RUN_PREFIX:
            return ()
        names = set()
        for start in range(len(needle)-TEMPLATE_RUN_PREFIX+1):
            names.update(self.literal_runs.get(needle[start:start+TEMPLATE_RUN_PREFIX], ()))
        return tuple(sorted(names))

    def _shortlist(self, parts, accepts, query):
        """Union of every strategy's candidates, best-supported names first.

        A name is ranked by how many query tokens (or their readings) it actually
        contains, so the alignment budget below is spent on the names most likely to
        be the intended interface rather than on every name that merely shares one
        common token.
        """
        levels = {}
        support = {}
        for token in parts:
            for candidate, (distance, kind) in self.alternatives(token).items():
                keys = self.postings.get(candidate)
                if not keys:
                    continue
                priority = 0 if kind in ('exact', 'join', 'split') else (1 if kind == 'prefix' else 2)
                levels.setdefault(priority, set()).update(keys)
                for key in keys:
                    support[key] = support.get(key, 0) + 1
        # Dynamic templates are reachable through their fixed literals alone.
        levels.setdefault(0, set()).update(self.templates_for(query))
        # Missing-token queries (`set_flag` -> `set_global_flag`) must still reach
        # complete names, so seed from the rarest query tokens as well.
        rarest = {}
        for token in set(parts):
            if is_placeholder(token) or not IDENTIFIER.fullmatch(token):
                continue
            keys = self.postings.get(token)
            if keys:
                rarest.setdefault(len(keys), set()).update(keys[:MAX_RARE_SEEDS])
        for size in sorted(rarest):
            levels.setdefault(3, set()).update(rarest[size])
        # Browsing: names that merely contain the text. This is the weakest strategy and
        # only applies to short queries, so it cannot flood a precise lookup. Its rows
        # stay low-scored and end up in the browse tier, never as ranked suggestions.
        if len(parts) <= SUBSTRING_TOKENS and len(query) <= SUBSTRING_CHARS:
            needle = query.casefold()
            levels.setdefault(4, set()).update(
                key for key in self.names if needle in key and key not in levels.get(0, ()))
        return self._collect(levels, accepts, support, parts, self._readings(parts))

    def _collect(self, levels, accepts, support, parts, readings):
        query_mass = sum(self.weight(token) for token in parts)
        candidates, seen = [], set()
        for priority in sorted(levels):
            for key in sorted(levels[priority]):
                if key in seen:
                    continue
                seen.add(key)
                entry = self.entries[key]
                if len(entry.tokens) > MAX_TOKENS or len(entry.name) < MIN_CANDIDATE_CHARS:
                    continue
                entry.query_tokens = parts
                if not self._mass_reachable(query_mass, entry):
                    continue
                accepted = entry.symbols if accepts is None else [s for s in entry.symbols if accepts(s)]
                if not accepted:
                    continue
                entry.accepted = len(accepted)
                candidates.append((entry, accepted))
        # Best-supported names first: the cheap gate above runs on everything, the
        # ordered-assignment bound only on the names that could still win.
        candidates.sort(key=lambda item: -support.get(item[0].name.casefold(), 0))
        shortlisted = [(entry, accepted) for entry, accepted in candidates[:MAX_ALIGN_BUDGET]
                       if self._worth_aligning(parts, query_mass, entry, readings)]
        return shortlisted[:MAX_ALIGN_CANDIDATES]

    def _mass_reachable(self, query_mass, entry):
        """Reject names whose unmatched token mass alone exceeds the browse budget.

        Dropping a candidate-only token costs `MISSING_WEIGHT` times its mass, so the
        cheapest conceivable alignment still pays for every fixed candidate token the
        query does not have mass for. This gate is O(tokens), needs no pair computation,
        and is deliberately loose: the ordered-assignment bound below does the precise
        work. Placeholders bind query tokens and are therefore never charged.
        """
        fixed = sum(self.weight(token) for token in entry.tokens if not is_placeholder(token))
        missing = max(0.0, fixed - query_mass)
        return 100*(1.0 - SCORE_WEIGHTS['missing']*missing/max(fixed, query_mass, 1e-9)) >= RELATED_MIN_SCORE

    def _worth_aligning(self, parts, query_mass, entry, readings):
        """Cheap upper bound on a name's score; skip names that cannot be ranked at all.

        The bound is the cheapest ordered assignment of query tokens to candidate
        tokens, which is no greater than the dynamic program's optimum, so the resulting
        score is an upper bound and the filter is one-sided: it can only drop names the
        full scorer would also place below the browse tier. `PREFILTER_SLACK` keeps a
        margin for the bounded completion bonus.
        """
        candidate = entry.tokens
        costs = self._pair_options(entry, readings, parts)
        mass = max(query_mass, sum(self.weight(t) for t in candidate if not is_placeholder(t)), 1e-9)
        floor = RELATED_MIN_SCORE - PREFILTER_SLACK
        if self._greedy_cost(costs, parts, candidate) > mass*(1.0 - floor/100.0):
            return False
        total = self._assignment(costs, parts)
        return 100*max(0.0, 1.0 - total/mass) >= floor

    def _greedy_cost(self, costs, parts, candidate):
        """First-fit cost, never below the optimum: used only to reject obvious losers."""
        total, cursor = 0.0, 0
        for i, token in enumerate(parts):
            cheapest = None
            for j in range(cursor, len(candidate)):
                option = costs.get(j, {}).get(i)
                if option is not None and (cheapest is None or option[0] < cheapest[0]):
                    cheapest, at = option, j
            if cheapest is None:
                total += SCORE_WEIGHTS['extra']*self.weight(token)
            else:
                total += cheapest[0]
                cursor = at + 1
        return total

    def _assignment(self, costs, parts):
        """Cheapest ordered one-to-one assignment of query tokens to candidate tokens.

        Maximises the saving over waiving every query token, which is the same optimum
        as charging the substitution cost, without letting a substitution cost less than
        zero in the process. Only the reading registered for *this* query token counts.
        """
        count = len(parts)
        waived = [SCORE_WEIGHTS['extra']*self.weight(token) for token in parts]
        if count > ASSIGNMENT_TOKENS:
            return sum(waived)
        best = [0.0]*(1 << count)
        for table in costs.values():
            for i in range(count):
                option = table.get(i)
                if option is None:
                    continue
                for mask in range(1 << count):
                    value = best[mask]
                    bit = 1 << i
                    if mask & bit:
                        continue
                    saving = waived[i] - option[0]
                    if saving > 0 and value + saving > best[mask | bit]:
                        best[mask | bit] = value + saving
        return sum(waived) - best[(1 << count)-1]

    def _readings(self, parts):
        """Token-level readings **indexed by query token position**.

        Identity matters: `alternatives(token)` describes what one query token may
        stand for, so the result must stay attached to that position. Collapsing every
        query token's readings into one shared dictionary would let the alignment match
        a candidate token against a reading generated by a *different* query token,
        which is exactly what makes `job_any_pop` score like `any_pop_job`.
        """
        return tuple(self.alternatives(token) for token in parts)

    def _pair_options(self, entry, readings, parts):
        """Cost of matching each candidate token, per query token, with identity kept.

        Returns ``{candidate index: {query index: (cost, kind, distance)}}``. Only three
        readings survive: an exact token, a curated typo from the bigram index, or a
        truncated token that is a real prefix of a candidate token (`avai` ->
        `available`, `continental_hab` -> `<planet_class>_habitability`). If a query
        token cannot be read that way it must be dropped as a real cost, so a
        speculative reading can never become a cheap substitution.
        """
        tables = {}
        for i, (token, allowed) in enumerate(zip(parts, readings)):
            weight = self.weight
            for j, ctoken in enumerate(entry.tokens):
                if is_placeholder(ctoken):
                    continue
                best = None
                reading = allowed.get(ctoken)
                if reading is not None:
                    kind = reading[1]
                    if kind == 'exact':
                        best = (0.0, 'exact', 0.0)
                    elif kind == 'typo':
                        best = (SCORE_WEIGHTS['typo']*reading[0]*weight(ctoken), 'typo', reading[0])
                if best is None and self._prefix_reading(token, ctoken):
                    # Heavily truncated tokens are cheaper: the typed token genuinely
                    # carries less evidence, and the corpus confirms the reading.
                    best = (SCORE_WEIGHTS['prefix']*weight(ctoken)*(0.5 + 0.5*len(token)/len(ctoken)),
                            'prefix', 0.0)
                if best is not None:
                    tables.setdefault(j, {})[i] = best
        return tables

    def _split_sources(self, parts):
        """Merge points where two adjacent query tokens concatenate into a real token.

        `has_back_ground_job` -> `back`+`ground` is really the CWT token `background`,
        and `any_owned_popjob` -> `pop`+`job`. Re-segmenting the query before
        aligning keeps the dynamic program strictly ordered.
        """
        points = {}
        for start in range(len(parts)-1):
            formed = parts[start] + parts[start+1]
            if len(formed) >= 6 and IDENTIFIER.fullmatch(formed) and formed in self.postings:
                points[start] = formed
        return points

    @staticmethod
    def _prefix_reading(token, candidate):
        """`avai` -> `available`, `hab` -> `habitability`: a truncated real token.

        The typed token must be a proper prefix of the candidate and long enough to
        identify it, so `energy` never becomes the token `add`. Abbreviation is one of
        the most common LLM mistakes and is not a spelling error, so it is checked for
        every token position, not only the last one.
        """
        return (PREFIX_MIN_CHARS <= len(token) < len(candidate)
                and candidate.startswith(token))

    @staticmethod
    def _terminal_approximation(token, candidate):
        return len(token) >= PREFIX_MIN_CHARS and candidate.startswith(token)

    @staticmethod
    def _in_order_mass(operations, weights_q):
        """Query mass whose matched tokens still appear in query order in the candidate.

        Deleting a query token or inserting a candidate token is fine; matching the
        tokens that did correspond out of order is not. The longest increasing run of
        candidate positions is what still reads as the same identifier, so it keeps full
        support for `any_pop_job` -> `any_owned_pop_job` while a permutation loses the
        tokens it had to reverse.
        """
        positions = []
        for kind, first, second, third in operations:
            if kind in MATCH_OPERATIONS:
                positions.append((first, second if kind != 'join' else second + 1))
        if not positions:
            return 0.0

        best = [weights_q[first] for first, _ in positions]
        for a in range(len(positions)):
            for b in range(a):
                if positions[b][1] < positions[a][1]:
                    best[a] = max(best[a], best[b] + weights_q[positions[a][0]])
        return max(best)

    @staticmethod
    def _can_capture(captured):
        return len(captured) <= 128 and OBJECT.fullmatch(captured) is not None

    def _segments(self, parts, accepts=None):
        """Query tokenizations to try: the plain one, then one merge per real token.

        `has_back_ground_job` -> `back`+`ground` is really the CWT token `background`;
        `any_owned_popjob` -> `pop`+`job`. Re-segmenting the query keeps the alignment
        dynamic program strictly ordered (no token reordering tricks). Each variant is
        ``(labeled, merged)`` where ``merged`` maps a merged token back to the query
        tokens it was built from, so the explanation can still report a separator fix.
        """
        plain = tuple((token, 1.0) for token in parts)
        variants = [(plain, {})]
        for start in range(len(parts)-1):
            formed = parts[start] + parts[start+1]
            if len(formed) < 6 or not IDENTIFIER.fullmatch(formed):
                continue
            if not self._plausible_merge(formed, accepts):
                continue
            variants.append((plain[:start] + ((formed, 1.0),) + plain[start+2:],
                             {formed: (start, start+2)}))
        return variants

    def _plausible_merge(self, formed, accepts):
        """Skip a re-segmentation that no reachable candidate could possibly use."""
        keys = self.postings.get(formed, ())
        if accepts is not None:
            keys = [key for key in keys if any(accepts(s) for s in self.entries[key].symbols)]
        return bool(keys)

    def _align(self, labeled, entry, readings, query_chars, origin=None):
        """`labeled` is ((token, weight), ...); `readings` is indexed the same way.

        `origin` maps a re-segmented token back to the original query token span, so a
        successful merge is still explained as a separator correction.
        """
        parts = [token for token, weight in labeled]
        candidate = entry.tokens
        n, m = len(parts), len(candidate)
        if not n or not m:
            return None
        captured = {j for j, token in enumerate(candidate) if is_placeholder(token)}
        entry.query_tokens = parts
        pairs = self._pair_options(entry, readings, parts)
        origin = origin or {}
        weights_q = [0.0 if is_placeholder(t) else weight for t, weight in labeled]
        weights_c = [self.weight(t) for t in candidate]
        # The DP uses normalised weights so a re-segmented token stays comparable, but
        # order evidence has to be weighed by the real corpus mass of each query token.
        order_weights = [self.weight(token) for token in parts]
        # Candidate tokens already used by the cheapest correspondence, for the
        # "dropped a word the candidate has" penalty below.
        present = set()
        cursor = 0
        for token in parts:
            while cursor < m and candidate[cursor] != token:
                cursor += 1
            if cursor < m:
                present.add(token)
        costs = [[math.inf]*(m+1) for _ in range(n+1)]
        back = {}
        costs[0][0] = 0.0

        def put(i, j, ni, nj, cost, operation):
            value = costs[i][j] + cost
            prior = costs[ni][nj]
            prior_operation = back[(ni, nj)][2] if (ni, nj) in back else None
            if value < prior or (value == prior and _prefer(operation, prior_operation)):
                costs[ni][nj] = value
                back[ni, nj] = (i, j, operation)

        for i in range(n+1):
            row = costs[i]
            for j in range(m+1):
                if not math.isfinite(row[j]):
                    continue
                if i < n and j < m:
                    q, c = parts[i], candidate[j]
                    if j in captured:
                        # A placeholder may absorb query tokens but earns no credit. Its
                        # own rarity is irrelevant (it is not part of any real name), so a
                        # fixed unit keeps completions of one template family comparable.
                        for end in range(i+1, n+1):
                            capture = '_'.join(parts[i:end])
                            if not self._can_capture(capture):
                                break
                            put(i, j, end, j+1,
                                SCORE_WEIGHTS['template']*PLACEHOLDER_WEIGHT*(end-i), ('capture', i, end, j))
                    else:
                        match = pairs.get(j, {}).get(i)
                        if match is not None:
                            cost, kind, distance = match
                            put(i, j, i+1, j+1, cost, (kind, i, j, distance))
                        # Missing underscore in the query: one query token is really
                        # two adjacent candidate tokens (`any_owned_popjob`).
                        if j+1 < m and not is_placeholder(candidate[j+1]) and len(q) >= 4:
                            joined = c + candidate[j+1]
                            if joined == q:
                                put(i, j, i+1, j+2,
                                    SCORE_WEIGHTS['join']*(weights_c[j]+weights_c[j+1]), ('join', i, j, None))
                if i < n:
                    # Dropping a query token that the candidate *does* have, unused, is a
                    # weaker explanation than dropping a word the candidate lacks, so it
                    # costs double and equal-cost alignments prefer the latter.
                    penalty = SCORE_WEIGHTS['extra']*(2.0 if parts[i] in present else 1.0)
                    put(i, j, i+1, j, penalty*weights_q[i], ('extra', i, None, None))
                if j < m:
                    put(i, j, i, j+1, SCORE_WEIGHTS['missing']*weights_c[j], ('missing', j, None, None))
        if not math.isfinite(costs[n][m]):
            return None

        operations, i, j = [], n, m
        guard = 4*(n+m)+8
        while (i or j) and guard:
            guard -= 1
            i, j, operation = back[i, j]
            operations.append(operation)
        operations.reverse()

        matched, missing, extra, changes, captures = [], [], [], [], []
        matched_q, matched_c, captured_q = set(), set(), set()
        for kind, first, second, third in operations:
            if kind == 'exact':
                matched_q.add(first)
                matched_c.add(second)
                matched.append(candidate[second])
                span = origin.get(parts[first])
                if span:
                    # The query was re-segmented: report the missing/extra separator.
                    changes.append({'query': '_'.join(parts[span[0]:span[1]]),
                                    'candidate': candidate[second], 'kind': 'separator'})
            elif kind in ('typo', 'prefix'):
                matched_q.add(first)
                matched_c.add(second)
                change = {'query': parts[first], 'candidate': candidate[second], 'kind': kind}
                if kind == 'typo':
                    change['distance'] = int(third or 0)
                changes.append(change)
            elif kind == 'missing':
                missing.append(candidate[first])
            elif kind == 'extra':
                extra.append(parts[first])
            elif kind == 'capture':
                captured_q.update(range(first, second))
                captures.append({'placeholder': candidate[third], 'value': '_'.join(parts[first:second])})
            elif kind == 'join':
                matched_q.add(first)
                matched_c.update((second, second+1))
                changes.append({'query': parts[first], 'candidate': '_'.join(candidate[second:second+2]),
                                'kind': 'separator'})

        query_mass = sum(self.weight(token) for token in parts)
        candidate_mass = sum(weights_c[j] for j in range(m) if j not in captured)
        literal_qmass = sum(weights_q[k] for k in matched_q)
        literal_cmass = sum(weights_c[k] for k in matched_c if k not in captured)
        in_template = bool(captures)
        anchored, prefix_length, covered = (literal_anchor(entry.template.literals, entry.name)
                                            if in_template else (False, 0, 0))
        # The suffix literal may be reached through a truncated query token (`ad` -> `add`).
        if in_template and m > 1 and not is_placeholder(candidate[-1]) and (m-1) not in matched_c \
                and self._terminal_approximation(parts[-1], candidate[-1]):
            matched_c.add(m-1)
            covered += len(parts[-1])
            matched.append(candidate[m-1])
            changes.append({'query': parts[-1], 'candidate': candidate[-1], 'kind': 'prefix'})
            matched_q.add(n-1)
        # Masses are read after every correspondence is registered, so the matched mass,
        # the in-order mass and the support share always describe the same alignment.
        literal_cmass = sum(weights_c[k] for k in matched_c if k not in captured)
        literal_qmass = sum(order_weights[k] for k in matched_q)
        explained = len(matched_q) + len(captured_q)
        binds = [(first, second) for kind, first, second, third in operations if kind == 'capture']
        facts = MatchFacts(cost=costs[n][m], query_mass=query_mass, candidate_mass=candidate_mass,
                           literal_qmass=literal_qmass, literal_cmass=literal_cmass,
                           query_tokens=n, matched_query_tokens=len(matched_q), candidate_tokens=m,
                           matched_query_mass=literal_qmass,
                           in_order_query_mass=self._in_order_mass(operations, order_weights),
                           absorbed_mass=sum(order_weights[k] for k in captured_q),
                           absorbed_tokens=len(captured_q),
                           placeholders=len(captured), explained=explained,
                           absorbed=max((end-start for start, end in binds), default=0),
                           template=in_template, anchored=anchored, anchor_length=prefix_length,
                           covered=covered, query_chars=query_chars,
                           bound_placeholders=len({third for kind, first, second, third in operations
                                                   if kind == 'capture'}))
        scored = score_match(facts)
        if scored is None:
            return None
        base, support, coverage = scored
        matched_information = sum(self.info(parts[k]) for k in matched_q)
        info = score_information(candidate, captured, missing, parts, matched_information, support, self.info)
        bonus = INFO_BONUS*info['factor']*info['competition']*support
        score = min(MAX_SUGGESTION_SCORE, max(0.0, 100*(base + bonus)))
        # A query token that is missing from the candidate but is nearly identical to a
        # token the candidate does have is a better explanation than dropping a whole
        # word: report `pop` first for `has_background_pop_job` -> `has_background_job`.
        reachable = set()
        for reading in readings:
            reachable.update(reading)
        extra.sort(key=lambda token: 0 if token in reachable else 1)
        diff = {}
        for key, value in [('matched_tokens', matched), ('missing_tokens', missing), ('extra_tokens', extra),
                           ('substitutions', changes), ('placeholder_bindings', captures)]:
            if value:
                diff[key] = value
        return Match(entry.name, round(score, 2), 'template_fuzzy' if in_template else 'token_edit', diff,
                     round(support, 4), round(coverage, 4))

    # -- public API ----------------------------------------------------------
    def find(self, query, accepts=None):
        """Rank every reachable real name for `query`; one row per unique name."""
        parts = query_tokens(query)
        if not parts or len(parts) > MAX_TOKENS:
            return []
        query_chars = len(query.replace('_', ''))
        best = {}
        for labeled, origin in self._segments(parts, accepts):
            variant = [token for token, _ in labeled]
            # Every tokenization needs its own readings, indexed by position: a
            # re-segmented token is a real token of the candidate, not an alternative
            # spelling of the original, and no position may borrow another's readings.
            readings = self._readings(variant)
            for entry, accepted in self._shortlist(variant, accepts, query):
                prior = best.get(entry.name)
                if prior is not None and prior[0].match_score >= MAX_SUGGESTION_SCORE:
                    continue
                match = self._align(labeled, entry, readings, query_chars, origin)
                if match is None or match.query_support < RELATED_QUERY_SUPPORT:
                    continue
                if prior is None or match.match_score > prior[0].match_score:
                    best[entry.name] = (match, accepted)
        matches = []
        for match, accepted in best.values():
            match.symbols = accepted
            matches.append(match)
        matches.sort(key=lambda m: (-m.match_score, m.name))
        return matches

    def candidates(self, query, accepts):
        """Backwards-compatible generator: (symbols, match dict) for ranked names."""
        for match in self.find(query, accepts):
            yield match.symbols, match.as_dict()
