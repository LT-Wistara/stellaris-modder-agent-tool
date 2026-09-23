import io
import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from unittest.mock import patch

from stellaris_modder_agent.index import Database, DATA
from stellaris_modder_agent.http_server import create_http_server
from stellaris_modder_agent.parser import ParseError, parse, parse_file, metadata
from stellaris_modder_agent.server import Server, serve
from stellaris_modder_agent.templates import Template
from stellaris_modder_agent.validate import Validator

ROOT = Path(__file__).resolve().parents[1]


def source_definition(row, rows):
    if 'definition' in row:
        return row['definition']
    return next(r['definition'] for r in rows if r['id'] == row['definition_ref'])


class CorpusCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = Database()

    def test_all_cwt_parse_lossless(self):
        files = list((DATA / 'config').rglob('*.cwt'))
        self.assertEqual(len(files), 173)
        self.assertEqual(self.db.stats['PARSED'], len(files))
        self.assertEqual(self.db.stats['FAILED'], 0)
        for file in files:
            with self.subTest(file=file):
                doc = self.db.documents[file.relative_to(DATA / 'config').as_posix()]
                self.assertEqual(doc.roundtrip().encode('utf-8'), file.read_bytes())
                self.assertEqual(''.join(t.text for t in doc.tokens), doc.source)

    def test_each_node_indexed(self):
        nodes = [n for d in self.db.documents.values() for n in d.walk()]
        self.assertEqual(len(nodes), len(self.db.symbols))
        # Derived generator rules are indexed as symbols without a corpus node.
        self.assertEqual(len(nodes) + len(self.db.derived_templates), len(self.db.by_id))

    def test_manifest_complete(self):
        manifest = self.db.upstream
        self.assertEqual(sorted(self.db.documents), manifest['files'])
        self.assertEqual(manifest['file_count'], len(self.db.documents))
        self.assertEqual(manifest['commit_sha'], '9d417d7ef783fd5198b35ae55675923f1cf24101')

    def test_trigger_exact(self):
        result = self.db.search('has_unlocked_council_positions', 'triggers')
        self.assertEqual(result['status'], 'CONFIRMED_CWT')
        self.assertEqual(result['results'][0]['kind'], 'trigger')

    def test_contiguous_overloads(self):
        result = self.db.get_definition('has_background_job', 'trigger')
        self.assertEqual(len(result['definitions']), 2)
        for row in result['definitions']:
            text = source_definition(row, result['definitions'])['text']
            self.assertIn('alias[trigger:has_background_job] = <job>', text)
            self.assertIn('alias[trigger:has_background_job] = yes', text)
            self.assertIn('## scopes = { leader }', text)
            self.assertIn('### Checks', text)
            parse(text)

    def test_complete_block(self):
        row = self.db.get_definition('num_assigned_jobs', 'trigger')['definitions'][0]
        self.assertIn('automated_workforce = bool', row['definition']['text'])
        self.assertTrue(row['raw'].endswith('}'))
        parse(row['definition']['text'])

    def test_partial_job(self):
        result = self.db.search('job', 'trigger', 500)
        self.assertEqual(result['status'], 'SUGGESTION')
        self.assertIn('num_assigned_jobs', [r['name'] for r in result['results']])

    def test_precise_modifier_scopes(self):
        row = self.db.get_definition('<district.capped>_max_add', 'modifiers')['definitions'][0]
        self.assertIn('Colony', row['raw'])
        self.assertEqual(row['scope_resolution']['resolved'],
                         '<district.capped>_max_add = { planet ship starbase colony sector galacticobject country }')
        self.assertTrue(row['scope_resolution']['category_sources'])

    def test_template_exact(self):
        row = self.db.search('country_resource_max_energy_add', 'modifier')['results'][0]
        self.assertEqual(row['status'], 'TEMPLATE_MATCH')
        self.assertEqual(row['name'], 'country_resource_max_<resource>_add')
        self.assertEqual(row['substitutions'][0]['object_existence'], 'UNRESOLVED')

    def test_template_partial(self):
        result = self.db.search('country_resource_max_energy', 'modifier')
        self.assertEqual(result['status'], 'SUGGESTION')
        self.assertEqual(result['results'][0]['name'], 'country_resource_max_<resource>_add')

    def test_template_partial_typing(self):
        self.assertEqual(self.db.search('country_resource_max_energy_ad', 'modifier')['results'][0]['name'],
                         'country_resource_max_<resource>_add')

    def test_leading_placeholder_partial(self):
        self.assertEqual(self.db.search('continental_hab', 'modifier')['results'][0]['name'], '<planet_class>_habitability')

    def test_no_greedy_leader_false_positive(self):
        result = self.db.search('country_resource_energy', 'modifier')
        self.assertNotIn('country_<leader_class.capped>_cap_add', [r['name'] for r in result['results']])
        self.assertEqual(Template('country_<leader_class.capped>_cap_add').partial_score('country_resource_energy'), 0)

    def test_other_template_families(self):
        cases = [('planet_<building>_build_speed_mult', 'planet_building_lab_build_speed_mult'),
                 ('<planet_class>_habitability', 'pc_continental_habitability'),
                 ('<district.capped>_max_add', 'district_city_max_add'),
                 ('enum[component_tag]_weapon_damage_mult', 'kinetic_weapon_damage_mult')]
        for template, name in cases:
            with self.subTest(template=template):
                self.assertIsNotNone(Template(template).exact(name))
                result = self.db.search(name, 'modifier', 500)
                self.assertIn(template, [r['name'] for r in result['results']])

    def test_nonexistent_identifier(self):
        result = self.db.search('zzzz_completely_fake_api_98765', 'trigger')
        self.assertEqual(result['status'], 'NOT_FOUND')
        self.assertEqual(result['note'], 'No matching declaration in the loaded rules and data.')

    def test_naked_scripted_placeholder_not_evidence(self):
        self.assertIsNone(Template('<scripted_trigger>').exact('anything'))
        self.assertEqual(self.db.get_definition('imagined_hypercube_trigger')['status'], 'NOT_FOUND')

    def test_schemas_accessible(self):
        cases = [('building', 'common/buildings'), ('job', 'common/pop_jobs'), ('trait', 'common/traits'),
                 ('technology', 'common/technologies_consolidated'), ('district', 'common/districts'), ('event', 'events/events')]
        for name, kind in cases:
            with self.subTest(name=name):
                result = self.db.get(kind + '.cwt')
                self.assertEqual(result['status'], 'CONFIRMED_CWT')
                self.assertEqual(result['kind'], 'schema_file')
                self.assertEqual(result['content'], self.db.documents[kind + '.cwt'].source)

    def test_get_by_id(self):
        matches = self.db.search('has_background_job', 'trigger')['results']
        id = matches[-1]['id']
        rows = self.db.get_definition(id)['definitions']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], id)

    def test_type_synonyms(self):
        for kind in ('trigger', 'effect', 'modifier'):
            self.assertEqual(self.db.resolve_type(kind), self.db.resolve_type(kind+'s'))

    def test_canonical_and_basename(self):
        self.assertEqual(self.db.resolve_type('common/buildings'), self.db.resolve_type('buildings'))
        self.assertEqual(self.db.resolve_type('events/events.cwt')[0], ('file', 'events/events'))

    def test_unknown_type(self):
        self.assertEqual(self.db.search('job', 'not_a_type')['status'], 'UNKNOWN_TYPE')

    def test_limit_explicit(self):
        result = self.db.search('job', limit=1)
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['results']), 1)
        self.assertGreater(result['total'], 1)

    def test_invalid_search_arguments(self):
        for q, limit in [('', 20), ('x', 0), ('x', True), ('x', 501)]:
            with self.assertRaises(ValueError):
                self.db.search(q, limit=limit)

    def test_metadata_inventory_exposed(self):
        self.assertGreater(self.db.metadata_inventory['scopes'], 2000)
        self.assertNotIn('__unparsed_metadata__', self.db.metadata_inventory)

    def test_queries_do_not_reparse(self):
        with patch('stellaris_modder_agent.index.parse_file', side_effect=AssertionError('reparse')):
            self.db.search('has_background_job')
            self.db.get_definition('building', 'buildings')
            Validator(self.db).validate('is_ai = yes', {'type': 'trigger', 'scope': 'country'})


class ParserCase(unittest.TestCase):
    def test_lossless_quotes_comments_placeholders(self):
        source = '\ufeff# heading\r\n\r\n## scopes = { country }\r\nalias[trigger:enum[x]] == {\n "quoted key" = "a # { \\\"b\\\""\n <resource> = int(0..inf]\n}\n'
        doc = parse(source, 'fixture.cwt')
        self.assertEqual(doc.roundtrip(), source)
        root = doc.nodes[0]
        self.assertEqual(root.key, 'alias[trigger:enum[x]]')
        self.assertEqual(root.operator, '==')
        self.assertEqual(root.line, 4)
        self.assertEqual(root.document.path, 'fixture.cwt')
        self.assertTrue(root.children[0].key_quoted)
        self.assertTrue(root.children[0].value_quoted)
        self.assertEqual(metadata(root)[0].key, 'scopes')

    def test_operators(self):
        for op in ('=', '==', '!=', '<>', '<', '>', '<=', '>=', '?='):
            self.assertEqual(parse('x'+op+'2').nodes[0].operator, op)

    def test_list_and_anonymous_block(self):
        doc = parse('x = { foo "bar" { 1 2 } y = {} }')
        self.assertEqual(len(doc.nodes[0].children), 4)
        self.assertEqual(doc.nodes[0].children[2].key, '')

    def test_malformed_constructs_not_silent(self):
        for code in ('x = {', 'x = "unterminated', 'x =', '}', 'alias[x = yes', 'x ! 2'):
            with self.subTest(code=code), self.assertRaises(ParseError):
                parse(code)

    def test_comment_braces_not_structural(self):
        self.assertEqual(len(parse('x = { # }\n a = "}"\n}').nodes), 1)


class FixtureCase(unittest.TestCase):
    def database(self, files):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for file, content in files.items():
            path = Path(temp.name) / file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
        return Database(temp.name)

    def test_unknown_category_unresolved(self):
        db = self.database({'modifiers.cwt': 'modifiers = { real_modifier = { Mystery } }'})
        row = db.get_definition('real_modifier')['definitions'][0]
        self.assertEqual(row['scope_resolution']['status'], 'UNRESOLVED')
        self.assertEqual(row['scope_resolution']['code'], 'UNRESOLVED_MODIFIER_CATEGORY')
        self.assertIsNone(row['scope_resolution']['resolved'])

    def test_categories_merge_dedupe(self):
        db = self.database({'modifiers.cwt': 'modifiers = { x = { A B } }',
                            'modifier_categories.cwt': 'modifier_categories = { A = { supported_scopes = { planet country } } B = { supported_scopes = { country ship } } }'})
        scopes = db.get_definition('x', 'modifier')['definitions'][0]['scope_resolution']['supported_scopes']
        self.assertEqual(scopes, ['planet', 'country', 'ship'])

    def test_duplicate_symbols_retained(self):
        db = self.database({'a.cwt': 'alias[trigger:dup] = bool\nalias[trigger:dup] = yes',
                            'nested/b.cwt': 'alias[trigger:dup] = no'})
        self.assertEqual(len(db.get_definition('dup', 'trigger')['definitions']), 3)

    def test_ambiguous_basename(self):
        db = self.database({'a/shared.cwt': 'foo = yes', 'b/shared.cwt': 'bar = yes'})
        result = db.search('foo', 'shared')
        self.assertEqual(result['status'], 'AMBIGUOUS_TYPE')
        self.assertEqual(result['candidates'], ['a/shared', 'b/shared'])

    def test_blank_inside_block_and_adjacent_overload(self):
        db = self.database({'x.cwt': '### first\nalias[trigger:x] = {\n a = bool\n\n b = int\n}\n### second\nalias[trigger:x] = bool\n\n# other\nalias[trigger:y] = yes'})
        rows = db.get_definition('x')['definitions']
        for row in rows:
            text = source_definition(row, rows)['text']
            self.assertIn('b = int', text)
            self.assertIn('### second', text)
            self.assertNotIn('trigger:y', text)
            parse(text)

    def test_injection_expansion(self):
        db = self.database({'x.cwt': 'base = { foo = bool }\n\n## inject = x.cwt@base/*\nobject = { bar = int }'})
        result = Validator(db).validate('foo = yes bar = 2', {'schema': 'object'})
        self.assertEqual(result['status'], 'CONFIRMED_CWT')

    def test_explicit_cardinality_empty_block(self):
        db = self.database({'x.cwt': 'alias[effect:test] = {\n## cardinality = 1..1\n needed = int\n}'})
        result = Validator(db).validate('test = {}', 'effect')
        self.assertTrue(any(f['code'] == 'CARDINALITY_MISMATCH' for f in result['findings']))

    def test_numeric_range(self):
        db = self.database({'x.cwt': 'alias[trigger:test] = float(1..5]'})
        for value, expected in [('1', 'UNKNOWN'), ('2', 'CONFIRMED_CWT'), ('5', 'CONFIRMED_CWT'), ('6', 'UNKNOWN')]:
            self.assertEqual(Validator(db).validate('test = '+value, 'trigger')['status'], expected)


class ValidateCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = Database()

    def check(self, code, context):
        return Validator(self.db).validate(code, context)

    def test_fake_trigger(self):
        result = self.check('has_quantum_banana = yes', 'trigger')
        self.assertEqual(result['status'], 'UNKNOWN')
        self.assertEqual(result['findings'][0]['code'], 'UNKNOWN_TRIGGER')

    def test_real_trigger_provenance(self):
        result = self.check('has_unlocked_council_positions >= 2', {'type': 'trigger', 'scope': 'country'})
        self.assertEqual(result['status'], 'CONFIRMED_CWT')
        self.assertEqual(result['findings'][0]['sources'][0]['file'], 'triggers.cwt')
        self.assertGreater(result['findings'][0]['sources'][0]['line'], 0)
        self.assertEqual(result['findings'][0]['sources'][0]['commit'], self.db.upstream['commit_sha'])
        self.assertEqual(result['game_correctness'], 'NOT_EVALUATED')

    def test_fake_effect(self):
        result = self.check('summon_fake_interface = yes', 'effect')
        self.assertEqual(result['findings'][0]['code'], 'UNKNOWN_EFFECT')

    def test_fake_modifier(self):
        result = self.check('imaginary_buff_mult = 5', 'modifier')
        self.assertEqual(result['findings'][0]['code'], 'UNKNOWN_MODIFIER')

    def test_unknown_schema_field(self):
        result = self.check('base_buildtime = 100 imagined_field = yes', 'common/buildings')
        self.assertTrue(any(f['code'] == 'UNKNOWN_SCHEMA_FIELD' for f in result['findings']))
        self.assertTrue(any(f['identifier'] == 'base_buildtime' and f['status'] == 'CONFIRMED_CWT' for f in result['findings']))

    def test_nested_trigger(self):
        result = self.check('if = { limit = { has_quantum_banana = yes } }', {'type': 'effect', 'scope': 'country'})
        self.assertTrue(any(f['code'] == 'UNKNOWN_TRIGGER' and f['identifier'] == 'has_quantum_banana' for f in result['findings']))

    def test_nested_schema_alias(self):
        result = self.check('potential = { imagined_check = yes }', 'common/buildings')
        self.assertTrue(any(f['code'] == 'UNKNOWN_TRIGGER' for f in result['findings']))

    def test_scope_mismatch(self):
        result = self.check('has_unlocked_council_positions >= 2', {'type': 'trigger', 'scope': 'planet'})
        self.assertTrue(any(f['code'] == 'SCOPE_MISMATCH' and f['status'] == 'UNKNOWN' for f in result['findings']))

    def test_scope_missing(self):
        self.assertTrue(any(f['code'] == 'SCOPE_CONTEXT_REQUIRED' for f in self.check('is_ai = yes', 'trigger')['findings']))

    def test_scope_link(self):
        result = self.check('owner = { is_ai = yes }', {'type': 'trigger', 'scope': 'planet'})
        self.assertEqual(result['status'], 'CONFIRMED_CWT')
        self.assertTrue(any(f['code'] == 'CWT_SCOPE_LINK' for f in result['findings']))

    def test_scope_push(self):
        result = self.check('every_owned_planet = { limit = { is_ai = yes } }', {'type': 'effect', 'scope': 'country'})
        self.assertTrue(any(f['code'] == 'SCOPE_MISMATCH' and f['identifier'] == 'is_ai' for f in result['findings']))

    def test_template_modifier(self):
        result = self.check('country_resource_max_energy_add = 100', {'type': 'modifier', 'scope': 'country'})
        self.assertEqual(result['status'], 'TEMPLATE_MATCH')

    def test_modifier_shape(self):
        result = self.check('country_resource_max_energy_add = {}', 'modifier')
        self.assertTrue(any(f['code'] == 'SHAPE_MISMATCH' for f in result['findings']))

    def test_modifier_scope(self):
        result = self.check('district_city_max_add = 2', {'type': 'modifier', 'scope': 'species'})
        self.assertTrue(any(f['code'] == 'SCOPE_MISMATCH' for f in result['findings']))

    def test_ambiguous_context(self):
        result = self.check('if = {}', None)
        self.assertEqual(result['status'], 'UNRESOLVED')
        self.assertTrue(any(f['code'] == 'AMBIGUOUS_CONTEXT' for f in result['findings']))

    def test_fake_without_context(self):
        self.assertEqual(self.check('bananas_of_destiny = yes', None)['status'], 'UNKNOWN')

    def test_bad_bool(self):
        result = self.check('is_ai = banana', {'type': 'trigger', 'scope': 'country'})
        self.assertTrue(any(f['code'] == 'VALUE_MISMATCH' for f in result['findings']))

    def test_type_word_not_a_number(self):
        result = self.check('has_unlocked_council_positions = value_field', {'type': 'trigger', 'scope': 'country'})
        self.assertNotEqual(result['status'], 'CONFIRMED_CWT')

    def test_shape_mismatch(self):
        result = self.check('num_assigned_jobs = yes', {'type': 'trigger', 'scope': 'country'})
        self.assertTrue(any(f['code'] == 'SHAPE_MISMATCH' for f in result['findings']))

    def test_overload_literal_yes(self):
        self.assertEqual(self.check('has_background_job = yes', {'type': 'trigger', 'scope': 'leader'})['status'], 'CONFIRMED_CWT')

    def test_reference_existence_unresolved(self):
        result = self.check('has_background_job = invented_job', {'type': 'trigger', 'scope': 'leader'})
        self.assertEqual(result['status'], 'UNRESOLVED')

    def test_operator_mismatch(self):
        result = self.check('is_ai > yes', {'type': 'trigger', 'scope': 'country'})
        self.assertTrue(any(f['code'] == 'OPERATOR_MISMATCH' for f in result['findings']))

    def test_script_parse_error(self):
        self.assertEqual(self.check('a = {', 'effect')['findings'][0]['code'], 'SCRIPT_PARSE_ERROR')

    def test_unknown_parent_children_unresolved(self):
        result = self.check('fake = { nested_fake = yes }', 'effect')
        self.assertTrue(any(f['identifier'] == 'nested_fake' and f['status'] == 'UNRESOLVED' for f in result['findings']))

    def test_enum_value(self):
        valid = self.check('category = research', 'common/buildings')
        invalid = self.check('category = zzzz_fake_category', 'common/buildings')
        self.assertEqual(valid['status'], 'CONFIRMED_CWT')
        self.assertEqual(invalid['status'], 'UNKNOWN')

    def test_unknown_scope_context(self):
        result = self.check('is_ai = yes', {'type': 'trigger', 'scope': 'imaginary_scope'})
        self.assertEqual(result['code'], 'UNKNOWN_SCOPE_CONTEXT')

    def test_schema_file_mode(self):
        result = self.check('building_demo = { base_buildtime = 100 fake_field = yes }',
                            {'type': 'common/buildings', 'mode': 'file'})
        self.assertTrue(any(f['identifier'] == 'fake_field' and f['code'] == 'UNKNOWN_SCHEMA_FIELD' for f in result['findings']))

    def test_empty_not_proof(self):
        self.assertEqual(self.check('# only comment', 'effect')['status'], 'UNRESOLVED')

    def test_context_unknown_option(self):
        with self.assertRaises(ValueError):
            self.check('is_ai = yes', {'typo': 'trigger'})

    def test_cwt_metasyntax_not_concrete_api(self):
        for code in ('scope_field = {}', '<scripted_trigger> = yes'):
            self.assertEqual(self.check(code, 'trigger')['status'], 'UNRESOLVED')

    def test_actual_effect_resource_fields(self):
        result = self.check('add_resource = { energy = 10 }', {'type': 'effect', 'scope': 'country'})
        self.assertEqual(result['status'], 'TEMPLATE_MATCH')

    def test_modifier_in_schema(self):
        result = self.check('planet_modifier = { imaginary_buff = 5 }', 'common/buildings')
        self.assertTrue(any(f['code'] == 'UNKNOWN_MODIFIER' for f in result['findings']))


class ProtocolCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = Database()

    def test_initialize_and_tools(self):
        server = Server(self.db)
        init = server.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}})
        self.assertEqual(init['result']['protocolVersion'], '2025-06-18')
        listed = server.dispatch({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
        self.assertEqual(len(listed['result']['tools']), 4)
        self.assertIsNone(server.dispatch({'jsonrpc': '2.0', 'method': 'notifications/initialized'}))

    def test_protocol_errors(self):
        server = Server(self.db)
        self.assertEqual(server.dispatch([])['error']['code'], -32600)
        self.assertEqual(server.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})['error']['code'], -32000)
        server.initialized = True
        self.assertEqual(server.dispatch({'jsonrpc': '2.0', 'id': 2, 'method': 'bad_method'})['error']['code'], -32601)
        response = server.dispatch({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'stellaris_search', 'arguments': {}}})
        self.assertTrue(response['result']['isError'])

    def test_stdio_all_tools_actual_process(self):
        messages = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'smoke', 'version': '1'}}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'stellaris_search', 'arguments': {'query': 'country_resource_max_energy_add'}}},
            {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'stellaris_get_definition', 'arguments': {'name': 'has_background_job', 'type': 'trigger'}}},
            {'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call', 'params': {'name': 'stellaris_validate', 'arguments': {'code': 'has_quantum_banana = yes', 'context': 'trigger'}}},
            {'jsonrpc': '2.0', 'id': 6, 'method': 'ping'}]
        text = '\n'.join(json.dumps(m) for m in messages)+'\n'
        process = subprocess.run([sys.executable, str(ROOT / 'stellaris_modder_tool.py'), 'serve'], input=text,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
                                 cwd=tempfile.gettempdir(), timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn('Stellaris MCP ready', process.stderr)
        replies = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(len(replies), 6)
        payloads = [json.loads(reply['result']['content'][0]['text']) for reply in replies[2:5]]
        self.assertEqual(payloads[0]['status'], 'TEMPLATE_MATCH')
        self.assertEqual((payloads[0]['templates'][0]['name'], payloads[0]['templates'][0]['type']),
                         ('country_resource_max_energy_add', 'modifiers'))
        self.assertTrue(payloads[0]['templates'][0]['definitions'])
        self.assertEqual(len([row for row in payloads[1]['definitions'] if 'text' in row]), 1)
        self.assertEqual(payloads[2]['status'], 'UNKNOWN')
        for reply in replies[2:5]:
            self.assertNotIn('structuredContent', reply['result'])

    def test_parse_error_keeps_server_alive(self):
        source = io.BytesIO(b'bad json\n{"jsonrpc":"2.0","id":2,"method":"ping"}\n')
        target = io.BytesIO()
        serve(self.db, source, target)
        rows = [json.loads(line) for line in target.getvalue().splitlines()]
        self.assertEqual(rows[0]['error']['code'], -32700)
        self.assertEqual(rows[1]['id'], 2)

    def test_cli_smoke(self):
        for args, status, exitcode in [(['search', 'has_unlocked_council_positions', '-t', 'trigger'], 'CONFIRMED_CWT', 0),
                                      (['search', 'zzzz_invented_api'], 'NOT_FOUND', 2),
                                      (['validate', '--code', 'has_quantum_banana = yes', '--context', 'trigger'], 'UNKNOWN', 2)]:
            process = subprocess.run([sys.executable, str(ROOT / 'stellaris_modder_tool.py')] + args,
                                     capture_output=True, text=True, encoding='utf-8', cwd=tempfile.gettempdir(), timeout=30)
            self.assertEqual(process.returncode, exitcode, process.stderr)
            self.assertEqual(json.loads(process.stdout)['status'], status)

    def test_http_transport_all_tools(self):
        """A local HTTP server needs no credentials: plain requests must work."""
        server = create_http_server(self.db, port=0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        url = f'http://127.0.0.1:{server.server_port}/mcp'
        def request(method, params=None, notification=False):
            message = {'jsonrpc': '2.0', 'method': method, 'params': params or {}}
            if not notification:
                message['id'] = 1
            req = urllib.request.Request(url, data=json.dumps(message).encode(), headers={
                'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
                'MCP-Protocol-Version': '2025-06-18'})
            return urllib.request.urlopen(req, timeout=5)
        try:
            with request('initialize', {'protocolVersion': '2025-06-18'}) as response:
                self.assertEqual(json.load(response)['result']['protocolVersion'], '2025-06-18')
            with request('notifications/initialized', notification=True) as response:
                self.assertEqual(response.status, 202)
            with request('tools/list') as response:
                self.assertEqual(len(json.load(response)['result']['tools']), 4)
            for name, arguments, status in [
                    ('stellaris_search', {'query': 'has_background_job'}, 'CONFIRMED_CWT'),
                    ('stellaris_search', {'query': 'common/buildings.cwt'}, 'CONFIRMED_CWT'),
                    ('stellaris_validate', {'code': 'fake_trigger = yes', 'context': 'trigger'}, 'UNKNOWN')]:
                with request('tools/call', {'name': name, 'arguments': arguments}) as response:
                    result = json.load(response)['result']
                    self.assertEqual(json.loads(result['content'][0]['text'])['status'], status)
                    self.assertNotIn('structuredContent', result)
            # No optional standalone SSE stream: a plain GET is still 405.
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(
                    urllib.request.Request(url, headers={'MCP-Protocol-Version': '2025-06-18'}),
                    timeout=5)
            self.assertEqual(error.exception.code, 405)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)

    def test_remote_bind_requires_explicit_opt_in(self):
        """There is no authentication, so publishing to the network is refused."""
        with self.assertRaises(ValueError):
            create_http_server(self.db, host='0.0.0.0', port=0)
        server = create_http_server(self.db, host='0.0.0.0', port=0, allow_remote=True)
        server.server_close()

    def test_foreign_origin_is_rejected(self):
        """DNS-rebinding guard: a page on another site must not reach the server."""
        server = create_http_server(self.db, port=0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        url = f'http://127.0.0.1:{server.server_port}/mcp'
        try:
            req = urllib.request.Request(
                url, data=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}).encode(),
                headers={'Content-Type': 'application/json', 'Origin': 'https://evil.example'})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(error.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
