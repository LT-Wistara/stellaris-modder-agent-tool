import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from stellaris_agent import __version__
from stellaris_agent.index import Database, identity
from stellaris_agent.retrieval import edit_distance, tokens
from stellaris_agent.scoring import MatchFacts, score_match
from stellaris_agent.server import TOOLS, Server
from stellaris_agent.validate import Validator
from stellaris_agent.projection import project
from scripts.evaluate_fuzzy import (make_cases, evaluate, evaluate_one_call_search,
                                    evaluate_reference_grounding)

ROOT = Path(__file__).resolve().parents[1]


class VersionCase(unittest.TestCase):
    """One release version, declared once and read everywhere."""

    def test_pyproject_is_the_single_source(self):
        declared = re.search(r'version = "([^"]+)"',
                             (ROOT / 'pyproject.toml').read_text('utf-8')).group(1)
        self.assertEqual(declared, __version__)

    def test_server_reports_the_shared_version(self):
        server = Server(Database())
        result = server.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                                  'params': {'protocolVersion': '2025-06-18'}})['result']
        self.assertEqual(result['serverInfo']['version'], __version__)

    def test_documentation_matches_the_release_version(self):
        self.assertRegex(__version__, r'^\d+\.\d+\.\d+$')
        report = (ROOT / 'docs/FUZZY_SEARCH_REPORT.md').read_text('utf-8')
        self.assertIn(f'results ({__version__})', report)
        readme = (ROOT / 'README.md').read_text('utf-8')
        self.assertIn(__version__, readme)

    def test_no_stale_release_version_is_left_behind(self):
        """Live files must not keep a released version label that is no longer current.

        `1.1` and `1.2` appearing as prose history ("against the 1.1 and 1.2 engines") is
        fine; a full version string of a past release is not.
        """
        # The release line was restarted at 1.1.0; the abandoned 1.6 labels must not
        # reappear as if they were current. Older labels (1.4/1.5) survive only as
        # explicit history in prose and are therefore not listed here.
        previous = ('1.6.0', '1.6.1')
        checked = [ROOT / 'README.md', ROOT / 'docs/FUZZY_SEARCH_REPORT.md',
                   ROOT / 'pyproject.toml', ROOT / 'stellaris_agent/server.py',
                   ROOT / 'stellaris_agent/__init__.py', ROOT / 'docs/TEST_REPORT.json']
        for path in checked:
            if not path.exists():
                continue
            with self.subTest(path=path.name):
                text = path.read_text('utf-8')
                stale = [version for version in previous if version in text]
                self.assertEqual(stale, [], f'stale release version {stale} in {path.name}')


class FuzzySearchCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = Database()

    def test_literal_object_reference_evaluation(self):
        report = evaluate_reference_grounding()
        self.assertEqual(report['cases'], 6)
        self.assertEqual(report['passed'], 6)
        self.assertEqual(report['failed'], 0)

    def test_one_call_search_evaluation(self):
        report = evaluate_one_call_search()
        self.assertEqual(report['cases'], 6)
        self.assertEqual(report['passed'], 6)
        self.assertEqual(report['failed'], 0)

    def top(self, query, type='trigger'):
        return self.db.search(query, type)['results'][0]

    def test_missing_internal_token(self):
        row = self.top('any_pop_job')
        self.assertEqual(row['name'], 'any_owned_pop_job')
        self.assertEqual(row['status'], 'SUGGESTION')
        self.assertGreater(row['match_score'], 70)
        self.assertEqual(row['token_diff']['matched_tokens'], ['any', 'pop', 'job'])
        self.assertEqual(row['token_diff']['missing_tokens'], ['owned'])

    def test_spelling_error(self):
        row = self.top('has_backgroud_job')
        self.assertEqual(row['name'], 'has_background_job')
        self.assertGreater(row['match_score'], 90)
        self.assertEqual(row['status'], 'SUGGESTION')
        self.assertIn({'query': 'backgroud', 'candidate': 'background', 'kind': 'typo', 'distance': 1},
                      row['token_diff']['substitutions'])

    def test_extra_token(self):
        row = self.top('has_background_pop_job')
        self.assertEqual(row['name'], 'has_background_job')
        self.assertEqual(row['token_diff']['extra_tokens'], ['pop'])

    def test_adjacent_letter_transposition(self):
        row = self.top('has_backgorund_job')
        self.assertEqual(row['name'], 'has_background_job')
        self.assertEqual(row['token_diff']['substitutions'][0]['distance'], 1)

    def test_short_token_typo_with_surrounding_anchors(self):
        self.assertEqual(self.top('any_owned_po_job')['name'], 'any_owned_pop_job')

    def test_missing_separator(self):
        row = self.top('any_owned_popjob')
        self.assertEqual(row['name'], 'any_owned_pop_job')
        self.assertEqual(row['token_diff']['substitutions'][0]['kind'], 'separator')

    def test_extra_separator(self):
        row = self.top('has_back_ground_job')
        self.assertEqual(row['name'], 'has_background_job')
        self.assertEqual(row['token_diff']['substitutions'][0]['kind'], 'separator')

    def test_missing_token_and_plural_edit(self):
        self.assertEqual(self.top('has_council_position')['name'], 'has_unlocked_council_positions')

    def test_fuzzy_without_type_filter(self):
        self.assertEqual(self.top('any_pop_job', None)['name'], 'any_owned_pop_job')
        self.assertEqual(self.top('has_backgroud_job', None)['name'], 'has_background_job')

    def test_leader_template_regression(self):
        result = self.db.search('country_resource_energy', 'modifier', 500)
        self.assertEqual(result['status'], 'NOT_FOUND')
        self.assertNotIn('country_<leader_class.capped>_cap_add', [r['name'] for r in result['results']])

    def test_fixed_template_typo(self):
        row = self.top('planet_research_lab_build_speeed_mult', 'modifier')
        self.assertEqual(row['name'], 'planet_<building>_build_speed_mult')
        self.assertEqual(row['status'], 'SUGGESTION')
        self.assertEqual(row['match_kind'], 'template_fuzzy')
        self.assertIn({'query': 'speeed', 'candidate': 'speed', 'kind': 'typo', 'distance': 1},
                      row['token_diff']['substitutions'])

    def test_broad_placeholder_does_not_outrank_fixed_evidence(self):
        rows = self.db.search('country_tech_research_speed', 'modifier', 100)['results']
        self.assertIn(rows[0]['name'], ('country_physics_tech_research_speed',
                                      'country_society_tech_research_speed', 'country_engineering_tech_research_speed'))
        for row in rows:
            if row['name'] == '<espionage_category>_speed_mult':
                self.assertLess(row['match_score'], 60)

    def test_all_legacy_template_completions(self):
        for query, target in [('country_resource_max_energy', 'country_resource_max_<resource>_add'),
                              ('country_resource_max_energy_ad', 'country_resource_max_<resource>_add'),
                              ('continental_hab', '<planet_class>_habitability')]:
            with self.subTest(query=query):
                row = self.top(query, 'modifier')
                self.assertEqual(row['name'], target)
                self.assertEqual(row['status'], 'SUGGESTION')

    def test_exact_evidence_still_first_with_suggestions_retained(self):
        rows = self.db.search('has_background_job', 'trigger', 500)['results']
        self.assertEqual(rows[0]['status'], 'CONFIRMED_CWT')
        self.assertEqual(rows[0]['match_score'], 100)
        self.assertEqual(rows[0]['match_kind'], 'exact')
        exact = [r for r in rows if r['status'] == 'CONFIRMED_CWT']
        self.assertEqual(len(exact), 2)  # overload locations remain independent

    def test_template_evidence_unchanged(self):
        row = self.top('country_resource_max_energy_add', 'modifier')
        self.assertEqual(row['status'], 'TEMPLATE_MATCH')
        self.assertEqual(row['match_kind'], 'template_exact')
        self.assertEqual(row['substitutions'][0]['object_existence'], 'UNRESOLVED')

    def test_strategies_merge_and_dedupe_by_location(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'triggers.cwt'
            path.write_text('alias[trigger:any_owned_pop_job] = bool\n\n'
                            'alias[trigger:trace_any_pop_job_history] = bool\n\n'
                            'alias[trigger:any_owned_pop_job] = yes', encoding='utf-8')
            db = Database(temp)
            rows = db.search('any_pop_job', 'trigger')['results']
            self.assertEqual(rows[0]['name'], 'any_owned_pop_job')
            self.assertIn('trace_any_pop_job_history', [r['name'] for r in rows])
            # One row per identifier, but the duplicate declarations stay visible and
            # stay retrievable: get_definition still returns every overload.
            self.assertEqual(len(rows), 2)
            self.assertEqual(len({r['name'] for r in rows}), 2)
            self.assertEqual(rows[0]['definition_count'], 2)
            self.assertEqual(len(rows[0]['definition_ids']), 2)
            self.assertEqual(len(db.get_definition('any_owned_pop_job', 'trigger')['definitions']), 2)

    def test_identifier_dedupe_keeps_overloads_retrievable(self):
        payload = self.db.search('has_background_job', 'trigger', 500)
        suggestions = [row['name'] for row in payload['results'] if row['status'] == 'SUGGESTION']
        # At most one suggestion row per identifier, so overloads cannot fill Top-N.
        self.assertEqual(len(suggestions), len(set(suggestions)))
        self.assertIn('definition_count', self.db.search('any_pop_job', 'trigger')['results'][0])
        # Exact evidence rows are the CWT declarations themselves, so both overloads of
        # the queried name remain, and get_definition still returns every definition.
        exact = [row for row in payload['results'] if row['status'] == 'CONFIRMED_CWT']
        self.assertEqual(len(exact), 2)
        names = [row['name'] for row in payload['results']]
        self.assertEqual(names.count('has_background_job'), 2)
        self.assertEqual(len(self.db.get_definition('has_background_job', 'trigger')['definitions']), 2)

    def test_no_fuzzy_promotion_in_definition_or_validation(self):
        self.assertEqual(self.db.get_definition('has_backgroud_job', 'trigger')['status'], 'NOT_FOUND')
        result = Validator(self.db).validate('has_backgroud_job = yes', 'trigger')
        self.assertEqual(result['status'], 'UNKNOWN')
        self.assertEqual(result['findings'][0]['code'], 'UNKNOWN_TRIGGER')

    def test_scores_sorted_bounded_and_legacy_keys_preserved(self):
        rows = self.db.search('pop_job', limit=100)['results']
        self.assertEqual([r['match_score'] for r in rows], sorted((r['match_score'] for r in rows), reverse=True))
        for row in rows:
            self.assertGreaterEqual(row['match_score'], 0)
            self.assertLessEqual(row['match_score'], 100)
            if row['status'] == 'SUGGESTION':
                self.assertEqual(row['score'], row['match_score'])
                self.assertEqual(row['reason'], row['match_kind'])

    def test_type_filter_applies_to_fuzzy_candidates(self):
        schema = self.db.search('backgroud_job', 'common/pop_jobs', 100)
        self.assertEqual(schema['status'], 'USE_SCHEMA_FILE')
        self.assertEqual(schema['path'], 'common/pop_jobs.cwt')
        rows = self.db.search('has_backgroud_job', 'trigger')['results']
        self.assertTrue(all(r['kind'] == 'trigger' for r in rows))

    def test_mcp_compact_fuzzy_response(self):
        server = Server(self.db)
        server.initialized = True
        result = server.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                  'params': {'name': 'stellaris_search', 'arguments': {'query': 'any_pop_job'}}})['result']
        payload = json.loads(result['content'][0]['text'])
        self.assertNotIn('metadata', payload)
        plain = json.loads(json.dumps(payload))
        for key in ('results', 'templates', 'suggestions'):
            for row in plain.get(key, []):
                self.assertTrue(row.pop('definitions'))
        self.assertEqual(plain, project('stellaris_search', self.db.search('any_pop_job', limit=5)))
        self.assertNotIn('structuredContent', result)
        self.assertNotIn('upstream', payload)

    def test_pretokenization_reused(self):
        with patch('stellaris_agent.index.parse_file', side_effect=AssertionError('reparse')):
            before = id(self.db.retrieval)
            first = self.db.search('has_backgroud_job', 'trigger')
            self.assertEqual(first, self.db.search('has_backgroud_job', 'trigger'))
            self.assertEqual(before, id(self.db.retrieval))

    def test_corpus_holdout_recall_and_negative_queries(self):
        cases = [c for c in make_cases(self.db) if c['split'] in ('holdout', 'negative')]
        report = evaluate(self.db, cases)
        holdout = report['groups']['holdout']
        self.assertGreaterEqual(holdout['queries'], 400)
        self.assertGreaterEqual(holdout['hit_at_1']/holdout['queries'], .92)
        self.assertGreaterEqual(holdout['hit_at_5']/holdout['queries'], .98)
        # `country_resource_energy` must stay NOT_FOUND: a broad wildcard template may
        # never be suggested for a query its literals do not spell out.
        self.assertEqual(report['groups']['negative']['returned'], 0)


class IdentifierAlignmentCase(unittest.TestCase):
    def test_placeholder_tokens_stay_atomic(self):
        self.assertEqual(tokens('country_<leader_class.capped>_cap_add'),
                         ('country', '<leader_class.capped>', 'cap', 'add'))
        self.assertEqual(tokens('enum[component_tag]_weapon_damage_mult'),
                         ('enum[component_tag]', 'weapon', 'damage', 'mult'))

    def test_edit_distance_operations(self):
        for a, b, expected in [('job', 'job', 0), ('backgroud', 'background', 1),
                               ('backgorund', 'background', 1), ('ab', 'ba', 1),
                               ('jobs', 'job', 1), ('job', 'pop', 2)]:
            self.assertEqual(edit_distance(a, b), expected)


class ScoringModelCase(unittest.TestCase):
    """The scorer is a pure function of measured facts, so it can be tested directly."""

    def facts(self, **overrides):
        base = dict(cost=0.0, query_mass=4.0, candidate_mass=4.0, literal_qmass=4.0,
                    literal_cmass=4.0, query_tokens=3, matched_query_tokens=3,
                    matched_query_mass=4.0, in_order_query_mass=4.0, absorbed_mass=0.0,
                    absorbed_tokens=0, candidate_tokens=3, placeholders=0,
                    bound_placeholders=0, explained=3)
        base.update(overrides)
        return MatchFacts(**base)

    def test_exact_style_alignment_scores_at_the_top(self):
        base, support, coverage = score_match(self.facts())
        self.assertAlmostEqual(base, 1.0)
        self.assertEqual((support, coverage), (1.0, 1.0))

    def test_dropping_the_query_is_never_cheap(self):
        weak = score_match(self.facts(cost=3.0, literal_qmass=0.0, matched_query_tokens=0,
                                      matched_query_mass=0.0, in_order_query_mass=0.0,
                                      query_tokens=4, candidate_tokens=1,
                                      candidate_mass=1.0))[0]
        self.assertLess(weak, 0.2)

    def test_reordered_match_loses_support(self):
        """Same tokens matched, but reversed, must not read as the same identifier."""
        ordered = score_match(self.facts(in_order_query_mass=4.0))
        reversed_row = score_match(self.facts(in_order_query_mass=1.6))
        self.assertGreater(ordered[1], reversed_row[1])
        self.assertLess(reversed_row[0], ordered[0])

    def test_template_with_unbound_placeholder_is_rejected_when_it_could_rank(self):
        row = self.facts(template=True, placeholders=2, bound_placeholders=1, absorbed_tokens=0)
        self.assertIsNone(score_match(row))

    def test_broad_wildcard_absorption_is_rejected(self):
        # `country_resource_energy` against `country_<leader_class.capped>_cap_add`.
        row = self.facts(cost=2.0, query_tokens=3, candidate_tokens=5, template=True,
                         placeholders=1, bound_placeholders=1, explained=3,
                         literal_qmass=1.0, matched_query_tokens=1, matched_query_mass=1.0,
                         in_order_query_mass=1.0, absorbed_mass=0.0, absorbed_tokens=0, absorbed=0,
                         literal_cmass=1.0, candidate_mass=4.5, query_mass=4.5,
                         anchored=False, covered=0, query_chars=22)
        self.assertIsNone(score_match(row))

    def test_single_token_wildcard_binding_is_normal(self):
        row = self.facts(cost=1.0, query_tokens=4, candidate_tokens=5, template=True,
                         placeholders=1, bound_placeholders=1, explained=4,
                         literal_qmass=3.0, matched_query_tokens=3, matched_query_mass=3.0,
                         in_order_query_mass=3.0, absorbed_mass=1.0, absorbed_tokens=1, absorbed=1,
                         literal_cmass=3.0, candidate_mass=3.6, query_mass=4.0,
                         anchored=True, anchor_length=20, covered=20, query_chars=26)
        self.assertIsNotNone(score_match(row))


class StrategyUnionCase(unittest.TestCase):
    """No strategy may stop the search: every hit has to be scored and ranked together."""

    @classmethod
    def setUpClass(cls):
        cls.db = Database()

    def test_repeated_query_is_served_from_cache(self):
        first = self.db.search('any_pop_job', 'trigger')
        with patch('stellaris_agent.index.parse_file', side_effect=AssertionError('reparse')):
            self.assertEqual(first, self.db.search('any_pop_job', 'trigger'))

    def test_prefilter_never_drops_a_scoring_candidate(self):
        """Every name the cheap bound rejects must be absent from both result tiers."""
        retrieval = self.db.retrieval
        for query in ('has_backgroud_job', 'country_resource_max_energy', 'num_assi_jobs',
                      'job', 'any_pop_job'):
            with self.subTest(query=query):
                payload = self.db.search(query, None, 500)
                returned = {row['name'] for row in payload['results']}
                returned |= {row['name'] for row in payload.get('related', ())}
                parts = tokens(query)
                query_mass = sum(retrieval.weight(token) for token in parts)
                readings = retrieval._readings(parts)
                for entry in retrieval.entries.values():
                    entry.query_tokens = parts
                    if not retrieval._worth_aligning(parts, query_mass, entry, readings):
                        self.assertNotIn(entry.name, returned)

    def test_results_and_related_never_share_a_candidate(self):
        """The two tiers are disjoint by identity, not by dict equality.

        `deduplicate` returns enriched copies of the rows it keeps, so a membership test
        comparing whole dicts reported every summarised suggestion as absent and listed it
        a second time under `related`.
        """
        queries = [('has_backgroud_job', 'trigger'), ('has_background_job', 'trigger'),
                   ('any_pop_job', 'trigger'), ('job', 'trigger'), ('has_type', 'trigger'),
                   ('set_flag', 'effect'), ('country_resource_energy', 'modifier'),
                   ('country_resource_max_energy', 'modifier'),
                   ('has_background_pop_job', 'trigger'),
                   ('country_tech_research_speed', 'modifier')]
        for query, type in queries:
            with self.subTest(query=query):
                payload = self.db.search(query, type, 200)
                results = {identity(row) for row in payload['results']}
                related = {identity(row) for row in payload.get('related', ())}
                self.assertEqual(results & related, set())

    def test_suggestion_is_not_repeated_as_related(self):
        payload = self.db.search('has_backgroud_job', 'trigger', 200)
        self.assertEqual(payload['results'][0]['name'], 'has_background_job')
        suggestions = {identity(row) for row in payload['results']}
        related = [row for row in payload.get('related', ()) if identity(row) in suggestions]
        self.assertEqual(related, [])
        # Browse rows are all strictly below the suggestion cutoff.
        for row in payload.get('related', ()):
            self.assertLess(row['match_score'], 55)

    def test_dedupe_identity_ignores_annotation_fields(self):
        """A summarised row and a plain row of one candidate are the same candidate."""
        payload = self.db.search('has_backgroud_job', 'trigger', 200)
        row = payload['results'][0]
        self.assertEqual(row['status'], 'SUGGESTION')
        self.assertIn('definition_count', row)
        self.assertEqual(identity(row), identity({**row, 'definition_count': 9}))
        self.assertEqual(identity(row), (row['name'], row['type']))

    def test_dedupe_is_per_identifier_and_type(self):
        retrieval = self.db
        rows = [{'name': 'dup', 'type': 'a', 'status': 'SUGGESTION', 'id': '1'},
                {'name': 'dup', 'type': 'a', 'status': 'SUGGESTION', 'id': '2'},
                {'name': 'dup', 'type': 'b', 'status': 'SUGGESTION', 'id': '3'}]
        collapsed = retrieval.deduplicate(rows)
        self.assertEqual([row['id'] for row in collapsed], ['1', '3'])
        self.assertEqual(collapsed[0]['definition_count'], 2)
        self.assertEqual(collapsed[0]['definition_ids'], ['1', '2'])

    def test_related_tier_is_separate_from_suggestions(self):
        payload = self.db.search('country_resource_energy', 'modifier')
        self.assertEqual(payload['status'], 'NOT_FOUND')
        self.assertEqual(payload['total'], 0)
        for row in payload.get('related', []):
            self.assertLess(row['match_score'], 55)
        self.assertGreater(payload.get('related_total', 0), 0)

    def test_query_support_reported_on_every_candidate(self):
        for row in self.db.search('has_backgroud_job', 'trigger')['results']:
            self.assertIn('query_support', row)
            self.assertIn('candidate_coverage', row)
            self.assertGreaterEqual(row['query_support'], 0.0)
            self.assertLessEqual(row['query_support'], 1.0)

    def test_truncated_token_matches_mid_identifier(self):
        row = self.top('count_avai_debris')
        self.assertEqual(row['name'], 'count_available_debris')
        self.assertEqual(row['status'], 'SUGGESTION')

    def test_wildcard_absorption_never_outranks_a_literal_name(self):
        rows = self.db.search('any_pop_job', None, 20)['results']
        self.assertEqual(rows[0]['name'], 'any_owned_pop_job')
        for row in rows[1:]:
            self.assertLess(row['match_score'], rows[0]['match_score'])

    def top(self, query, type='trigger'):
        return self.db.search(query, type)['results'][0]


class TokenOrderCase(unittest.TestCase):
    """Ordered alignment: gaps and typos are fine, permutations are not.

    A query token may only be matched by a reading that token itself produced. When the
    readings of every query token were fused into one shared set, a candidate token could
    be matched against a reading generated by a different query token, so `job_any_pop`
    aligned with `any_owned_pop_job` exactly like `any_pop_job` did.
    """

    @classmethod
    def setUpClass(cls):
        cls.db = Database()

    def best(self, query, type=None, limit=100):
        """Highest-scoring row that names the given target, or None."""
        return next((row for row in self.db.search(query, type, limit)['results']), None)

    def score(self, query, type=None):
        row = self.best(query, type)
        return row['match_score'] if row else None

    def test_a_correct_order_still_wins(self):
        row = self.best('any_pop_job', 'trigger')
        self.assertEqual(row['name'], 'any_owned_pop_job')
        self.assertEqual(row['status'], 'SUGGESTION')
        self.assertGreater(row['match_score'], 80)
        self.assertEqual(row['token_diff']['matched_tokens'], ['any', 'pop', 'job'])
        self.assertEqual(row['token_diff']['missing_tokens'], ['owned'])
        self.assertEqual(row['query_support'], 1.0)

    def test_b_permuted_query_scores_far_below_the_ordered_one(self):
        ordered = self.db.search('any_pop_job', 'trigger', 100)['results']
        target = next(row for row in ordered if row['name'] == 'any_owned_pop_job')
        permuted = [row for row in self.db.search('job_any_pop', 'trigger', 100)['results']
                    if row['name'] == 'any_owned_pop_job']
        # Whatever the permuted query returns for that name must be far weaker.
        for row in permuted:
            self.assertLess(row['match_score'], target['match_score'] - 20)
            self.assertLess(row['match_score'], 70)
        # And it must not be the top result for the permuted query.
        top = self.db.search('job_any_pop', 'trigger', 100)['results']
        if top:
            self.assertNotEqual(top[0]['name'], 'any_owned_pop_job')

    def test_c_reordered_two_token_query_is_not_near_perfect(self):
        ordered = self.best('has_backgroud_job', 'trigger')
        self.assertEqual(ordered['name'], 'has_background_job')
        self.assertGreater(ordered['match_score'], 90)
        for query in ('background_has_job', 'job_background_has'):
            with self.subTest(query=query):
                payload = self.db.search(query, 'trigger', 100)
                row = next((r for r in payload['results'] if r['name'] == 'has_background_job'), None)
                if row is not None:
                    self.assertLess(row['match_score'], 70)
                    self.assertLess(row['match_score'], ordered['match_score'] - 20)

    def test_d_fully_reversed_query_is_not_a_suggestion(self):
        payload = self.db.search('job_background_has', 'trigger', 100)
        self.assertNotIn('has_background_job', [row['name'] for row in payload['results']])

    def test_e_permuted_four_token_query_is_not_near_perfect(self):
        ordered = self.best('any_pop_job', 'trigger')
        payload = self.db.search('owned_any_job_pop', 'trigger', 100)
        row = next((r for r in payload['results'] if r['name'] == 'any_owned_pop_job'), None)
        if row is not None:
            self.assertLess(row['match_score'], ordered['match_score'] - 20)
        for candidate in payload['results']:
            self.assertLess(candidate['match_score'], 90)

    def test_f_single_token_typo_is_unaffected(self):
        row = self.best('has_backgroud_job', 'trigger')
        self.assertEqual(row['name'], 'has_background_job')
        self.assertGreater(row['match_score'], 90)
        self.assertEqual(row['query_support'], 1.0)
        self.assertIn({'query': 'backgroud', 'candidate': 'background', 'kind': 'typo', 'distance': 1},
                      row['token_diff']['substitutions'])

    def test_g_wildcard_template_regression_holds(self):
        payload = self.db.search('country_resource_energy', 'modifier', 500)
        self.assertEqual(payload['status'], 'NOT_FOUND')
        names = [row['name'] for row in payload['results']]
        self.assertNotIn('country_<leader_class.capped>_cap_add', names)
        for row in payload.get('related', []):
            self.assertLess(row['match_score'], 55)

    def test_gaps_and_insertions_still_supported(self):
        for query, target in [('any_pop_job', 'any_owned_pop_job'),
                              ('has_background_pop_job', 'has_background_job'),
                              ('has_council_position', 'has_unlocked_council_positions'),
                              ('planet_research_lab_build_speeed_mult', 'planet_<building>_build_speed_mult')]:
            with self.subTest(query=query):
                payload = self.db.search(query, 'modifier' if 'planet' in query else 'trigger', 100)
                names = [row['name'] for row in payload['results']]
                self.assertIn(target, names)

    def test_identity_is_preserved_in_the_alignment_table(self):
        """The cost table must name which query token each candidate token answers."""
        retrieval = self.db.retrieval
        entry = retrieval.entries['any_owned_pop_job']
        ordered = retrieval._pair_options(entry, retrieval._readings(tokens('any_pop_job')),
                                          tokens('any_pop_job'))
        permuted = retrieval._pair_options(entry, retrieval._readings(tokens('job_any_pop')),
                                           tokens('job_any_pop'))
        self.assertNotEqual(ordered, permuted)
        self.assertEqual(sorted(ordered[0]), [0])   # candidate `any` answers query token 0
        self.assertEqual(sorted(permuted[0]), [1])  # ... and query token 1 when reordered


if __name__ == '__main__':
    unittest.main()
