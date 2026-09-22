"""Public MCP contracts independent of detailed implementation dictionaries."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request

from stellaris_modder_agent import __version__
from stellaris_modder_agent.index import Database
from stellaris_modder_agent.projection import project
from stellaris_modder_agent.server import Server
from stellaris_modder_agent.http_server import create_http_server
from stellaris_modder_agent.validate import Validator

ROOT = Path(__file__).resolve().parents[1]


class AgentProjectionCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = Database()

    def call(self, tool, **args):
        server = Server(self.db)
        server.initialized = True
        result = server.dispatch(self.request(tool, args))['result']
        self.assertNotIn('structuredContent', result)
        payload = json.loads(result['content'][0]['text'])
        self.assertNotIn('metadata', payload)
        return payload

    @staticmethod
    def request(tool, args):
        return {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': tool, 'arguments': args}}

    def test_search_only_actionable_candidates(self):
        result = self.call('stellaris_search', query='has_backgroud_job', type='trigger')
        self.assertEqual(result['status'], 'SUGGESTION')
        self.assertLessEqual(set(result), {'status', 'results', 'suggestions', 'more', 'tips'})
        self.assertNotIn('results', result)
        selected = result['suggestions'][0]
        self.assertEqual((selected['name'], selected['type']),
                         ('has_background_job', 'triggers'))
        self.assertGreaterEqual(len(selected['definitions']), 1)
        self.assertIn('alias[trigger:has_background_job]', selected['definitions'][0]['text'])

    def test_full_provenance_is_only_on_list(self):
        server = Server(self.db)
        server.initialized = True
        result = server.dispatch(self.request('stellaris_list', {}))['result']
        payload = json.loads(result['content'][0]['text'])
        metadata = payload['metadata']
        self.assertEqual(metadata['server'], {'name': 'stellaris-modder-agent-tool', 'version': __version__})
        self.assertEqual(metadata['stellaris']['version'], '4.5')
        self.assertIn('target version', metadata['stellaris']['note'])
        self.assertEqual(metadata['cwt']['commit'], self.db.upstream['commit_sha'])
        self.assertEqual(metadata['cwt']['file_count'], len(self.db.documents))

    def test_economic_modifier_generator_is_template_evidence(self):
        name = 'planet_jobs_energy_produces_mult'
        result = self.call('stellaris_search', query=name, type='modifier')
        self.assertEqual(result['status'], 'TEMPLATE_MATCH')
        # The concrete name the caller searched is reported, not the template's own name.
        selected = result['templates'][0]
        self.assertEqual((selected['name'], selected['type']), (name, 'modifiers'))
        self.assertIn('economic-category generator', selected['definitions'][0]['text'])
        validated = self.call('stellaris_validate', code=name + ' = 0.1', context='modifier')
        self.assertEqual(validated['status'], 'TEMPLATE_MATCH')

    def test_unknown_modifier_suggestions_use_retrieval_family_order(self):
        name = 'country_resource_energy_mult'
        result = self.call('stellaris_validate', code=name + ' = 0.1', context='modifier')
        self.assertEqual(result['status'], 'UNKNOWN')
        suggestions = result['findings'][0]['suggestions']
        self.assertEqual(suggestions[0], 'country_resource_max_add')
        self.assertNotEqual(suggestions[0], 'country_storm_influence_mult')

    def test_exact_candidates_deduplicated_without_debug_or_score(self):
        result = self.call('stellaris_search', query='has_background_job', type='trigger')
        self.assertEqual(result['status'], 'CONFIRMED_CWT')
        self.assertEqual((result['results'][0]['name'], result['results'][0]['type']),
                         ('has_background_job', 'triggers'))
        self.assertTrue(all(set(row) == {'name', 'type', 'definitions'}
                            for row in result['results']))
        default = self.call('stellaris_search', query='job')
        self.assertLessEqual(sum(len(default.get(key, ()))
                                 for key in ('results', 'templates', 'suggestions')), 5)
        self.assertGreater(self.call('stellaris_search', query='job', limit=1)['more'], 0)

    def test_legacy_get_complete_source_once_without_parallel_parsed_fields(self):
        result = self.call('stellaris_get', target='has_background_job')
        self.assertEqual(set(result), {'status', 'name', 'definitions'})
        rows = result['definitions']
        self.assertLessEqual({key for row in rows for key in row},
                             {'type', 'text', 'same_as', 'scopes', 'unresolved_categories', 'where'})
        texts = [row['text'] for row in rows if 'text' in row]
        self.assertEqual(len(texts), 1)
        text = texts[0]
        self.assertIn('alias[trigger:has_background_job] = <job>', text)
        self.assertIn('alias[trigger:has_background_job] = yes', text)
        self.assertIn('## scopes = { leader }', text)
        self.assertEqual(text, self.db.get('has_background_job')['definitions'][0]['definition']['text'])
        block = self.call('stellaris_get', target='num_assigned_jobs')['definitions'][0]
        self.assertEqual(block['text'], self.db.get('num_assigned_jobs')['definitions'][0]['definition']['text'])

    def test_modifier_scopes_and_template_evidence_retained(self):
        row = self.call('stellaris_get', target='<district.capped>_max_add')['definitions'][0]
        self.assertIn('Colony', row['text'])
        self.assertEqual(row['scopes'],
                         ['planet', 'ship', 'starbase', 'colony', 'sector', 'galacticobject', 'country'])
        result = self.call('stellaris_get', target='country_resource_max_energy_add', type='modifier')
        self.assertEqual(result['status'], 'TEMPLATE_MATCH')
        self.assertIn('<resource>', result['definitions'][0]['text'])

    def test_custom_sources_and_unresolved_category_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ('a', 'b'):
                (root / (name + '.cwt')).write_text('alias[trigger:dup] = yes', encoding='utf-8')
            (root / 'modifiers.cwt').write_text('modifiers = { fake_category_test = { MissingCategory } }', encoding='utf-8')
            db = Database(root)
            compact = project('stellaris_get', db.get('dup'))
            self.assertEqual(len(compact['definitions']), 2)
            row = project('stellaris_get', db.get('fake_category_test'))['definitions'][0]
            self.assertEqual(row['unresolved_categories'], ['MissingCategory'])

    def test_validation_retains_repairs_and_omits_confirmed_chatter(self):
        result = self.call('stellaris_validate', code='has_unlocked_council_positions >= 2',
                           context={'type': 'trigger', 'scope': 'country'})
        self.assertEqual(result['status'], 'CONFIRMED_CWT')
        self.assertEqual(result['findings'], [])
        self.assertEqual(result['counts'], {'CONFIRMED_CWT': 1})
        self.assertNotIn('legend', result)          # nothing to explain without findings
        unknown = self.call('stellaris_validate', code='has_magic_planet = yes', context='trigger')
        self.assertIn('UNKNOWN=no evidence here', unknown['legend'])
        cases = [('has_magic_planet = yes', 'trigger'),
                 ('has_unlocked_council_positions >= banana', 'trigger'),
                 ('has_unlocked_council_positions >= 2', {'type': 'trigger', 'scope': 'planet'}),
                 ('country_resource_max_energy_add = 2', 'modifier'),
                 ('base_buildtime = wrong\ninvented_field = yes', 'common/buildings'),
                 ('immediate = { invented_effect = yes }', 'events/events'),
                 ('x = {', None)]
        for code, context in cases:
            with self.subTest(code=code):
                full = Validator(self.db).validate(code, context)
                arguments = {'context': context} if context else {}
                compact = self.call('stellaris_validate', code=code, **arguments)
                self.assertEqual(compact['status'], full['status'])
                expected = [f for f in full['findings'] if f['status'] != 'CONFIRMED_CWT']
                self.assertEqual(len(compact['findings']), len(expected))
                for actual, original in zip(compact['findings'], expected):
                    for key in ('status', 'code', 'identifier', 'line', 'message', 'expected', 'scope',
                                'supported_scopes', 'candidates', 'field'):
                        if key in original:
                            self.assertEqual(actual[key], original[key])
                    self.assertNotIn('sources', actual)
                    if original.get('suggestions'):
                        self.assertEqual(actual['suggestions'],
                                         [suggestion['name'] for suggestion in original['suggestions']])
                expected_code_counts = {}
                for finding in full['findings']:
                    by_code = expected_code_counts.setdefault(finding['status'], {})
                    by_code[finding['code']] = by_code.get(finding['code'], 0) + 1
                ignored = self.call('stellaris_validate', code=code, detail=True, **arguments)
                self.assertEqual(ignored, compact)

    def test_cardinality_mismatch_remains_actionable_and_counted(self):
        code = 'building_test = {\n prerequisites = { "tech_a" }\n prerequisites = { "tech_b" }\n}'
        context = {'type': 'common/buildings', 'schema': 'building', 'mode': 'file'}
        result = self.call('stellaris_validate', code=code, context=context)
        cardinality = [row for row in result['findings'] if row['code'] == 'CARDINALITY_MISMATCH']
        self.assertEqual(len(cardinality), 1)
        self.assertEqual(cardinality[0]['field'], 'prerequisites')
        ignored = self.call('stellaris_validate', code=code, context=context, detail=True)
        self.assertEqual(ignored, result)

    def test_schema_name_is_accepted_as_a_bare_context_string(self):
        """A known schema is shorthand for ``context={"schema": ...}``."""
        result = self.call('stellaris_validate', code='x = yes', context='scripted_effect')
        accepted = self.call('stellaris_validate', code='x = yes', context={'schema': 'scripted_effect'})
        self.assertEqual(result, accepted)

    def test_early_errors_and_uncertainty_survive(self):
        result = self.call('stellaris_validate', code='is_ai = yes', context={'scope': 'imaginary'})
        self.assertEqual(result, {'status': 'UNRESOLVED', 'code': 'UNKNOWN_SCOPE_CONTEXT', 'scope': 'imaginary'})
        result = self.call('stellaris_search', query='job', type='imaginary')
        self.assertEqual(result['status'], 'UNKNOWN_TYPE')
        self.assertEqual(result['type'], 'imaginary')
        self.assertEqual(result['candidates'], [])
        # An unusable type filter must say what would work instead of only failing.
        self.assertIn('schema', result['hint'])
        for tool, args in [('stellaris_search', {'query': 'zzzz_nonexistent_identifier'}),
                           ('stellaris_get', {'target': 'zzzz_nonexistent_identifier'})]:
            result = self.call(tool, **args)
            self.assertEqual(result['status'], 'NOT_FOUND')
            self.assertIn('mod', result['tips'])

    def test_projection_is_detached_and_omits_internal_evidence(self):
        for tool, payload in [('stellaris_get', self.db.get('has_background_job')),
                              ('stellaris_search', self.db.search('has_backgroud_job')),
                              ('stellaris_validate', Validator(self.db).validate('has_magic_planet = yes', 'trigger'))]:
            before = deepcopy(payload)
            compact = project(tool, payload)
            self.assertEqual(payload, before)
            self.assertLess(len(json.dumps(compact)), len(json.dumps(payload)))
            self.assertNotIn('"source":', json.dumps(compact))
            self.assertNotIn('"sources":', json.dumps(compact))
            if tool != 'stellaris_validate':
                self.assertLess(len(json.dumps(compact)), len(json.dumps(payload)) * .6)
            compact.clear()
            self.assertEqual(payload, before)

    def test_stdio_and_http_deliver_compact_projection(self):
        requests = [self.request('stellaris_search', {'query': 'has_backgroud_job', 'type': 'trigger'}),
                    self.request('stellaris_get', {'target': 'has_background_job'}),
                    self.request('stellaris_validate', {'code': 'has_magic_planet = yes', 'context': 'trigger'})]
        init = {'jsonrpc': '2.0', 'id': 0, 'method': 'initialize'}
        process = subprocess.run([sys.executable, str(ROOT / 'stellaris_modder_tool.py'),
                                  '--no-game-data', 'serve'],
                                 input=('\n'.join(json.dumps(r) for r in [init] + requests) + '\n').encode(),
                                 capture_output=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        replies = [json.loads(line)['result'] for line in process.stdout.splitlines()][1:]
        server = create_http_server(self.db, port=0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            for request, stdio in zip(requests, replies):
                req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/mcp',
                                             data=json.dumps(request).encode(), headers={'Content-Type': 'application/json'})
                with urllib.request.urlopen(req, timeout=5) as response:
                    http = json.load(response)['result']
                self.assertEqual(http, stdio)
                self.assertNotIn('structuredContent', http)
                serialized = http['content'][0]['text']
                for key in ('source', 'sources', 'dataset', 'id', 'raw', 'annotations', 'token_diff'):
                    self.assertNotIn('"' + key + '":', serialized)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)
