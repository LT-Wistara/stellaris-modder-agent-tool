"""Catalog/get boundaries are independent of the full semantic validation index."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from unittest.mock import patch

from stellaris_modder_agent.index import Database, DATA
from stellaris_modder_agent.http_server import create_http_server
from stellaris_modder_agent.server import Server, TOOLS
from stellaris_modder_agent.validate import Validator
from stellaris_modder_agent.projection import project

ROOT = Path(__file__).resolve().parents[1]


class SchemaCatalogCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = Database()

    def test_list_is_only_complete_sorted_paths(self):
        result = self.db.list_files()
        self.assertEqual(set(result), {'files'})
        self.assertEqual(result['files'], sorted(self.db.documents))
        self.assertEqual(len(result['files']), self.db.stats['PARSED'])
        self.assertEqual(len(result['files']), 173)
        for path in ('common/buildings.cwt', 'events/events.cwt', 'triggers.cwt'):
            self.assertIn(path, result['files'])

    def test_building_file_exact_content(self):
        self.assert_schema('common/buildings.cwt')

    def test_event_file_exact_content(self):
        self.assert_schema('events/events.cwt')

    def assert_schema(self, path):
        expected = (DATA / 'config' / path).read_bytes().decode('utf-8')
        result = self.db.get(path)
        self.assertEqual(result, {'status': 'CONFIRMED_CWT', 'kind': 'schema_file',
                                  'path': path, 'content': expected})

    def test_all_subdirectory_schemas_complete(self):
        for path in self.db.schema_files:
            with self.subTest(path=path):
                self.assert_file_policy(path)

    def test_all_root_files_follow_line_limit(self):
        roots = [path for path in self.db.documents if '/' not in path]
        self.assertEqual(len(roots), 20)
        for path in roots:
            with self.subTest(path=path):
                self.assert_file_policy(path)

    def assert_file_policy(self, path):
        source = self.db.documents[path].source
        result = self.db.get(path)
        if len(source.splitlines()) > 800:
            self.assertEqual(set(result), {'status', 'path', 'message'})
            self.assertEqual(result['status'], 'USE_SEARCH')
            self.assertEqual(result['path'], path)
            self.assertIn('too large', result['message'])
            self.assertIn('800', result['message'])
            self.assertIn('stellaris_search', result['message'])
            self.assertIn(path[:-4], result['message'])
        else:
            self.assertEqual(result, {'status': 'CONFIRMED_CWT', 'path': path,
                                     'kind': 'schema_file' if '/' in path else 'registry_file', 'content': source})

    def test_800_line_boundary_in_both_directories(self):
        for lines in (799, 800, 801):
            for newline in ('\n', '\r\n'):
                with self.subTest(lines=lines, newline=newline), tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    (root / 'common').mkdir()
                    text = ('# comment' + newline) * (lines-1) + 'object = { real_field = bool }' + newline
                    for path in ('sample.cwt', 'common/sample.cwt'):
                        (root / path).write_bytes(text.encode('utf-8'))
                    db = Database(root)
                    for path in db.list_files()['files']:
                        result = db.get(path)
                        self.assertEqual(db.file_lines[path], lines)
                        self.assertEqual(result['status'], 'USE_SEARCH' if lines > 800 else 'CONFIRMED_CWT')
                        if lines <= 800:
                            self.assertEqual(result['content'], text)
                        else:
                            self.assertNotIn('content', result)

    def test_large_schema_search_then_get_is_usable(self):
        path = 'common/defines/00_defines.cwt'
        self.assertEqual(self.db.get(path)['status'], 'USE_SEARCH')
        symbol = next(s for s in self.db.by_file[path[:-4]] if s.node.parent is not None and s.node.children is None and s.node.operator)
        result = self.db.search(symbol.name, path)
        row = next(r for r in result['results'] if r['id'] == symbol.id)
        for definition in (self.db.get(row['id']), self.db.get(symbol.name, path)):
            self.assertEqual(definition['status'], 'CONFIRMED_CWT')
            self.assertTrue(any(r['raw'] == symbol.node.raw for r in definition['definitions']))
        self.assertFalse(any(r['type'] == path[:-4] for r in self.db.search(symbol.name)['results']))

    def test_root_paths_cannot_be_bypassed_by_type_or_prefix(self):
        for path in ('triggers.cwt', './triggers.cwt', 'config/triggers.cwt',
                     'effects.cwt', 'config\\effects.cwt', 'modifiers.cwt'):
            with self.subTest(path=path):
                result = self.db.get(path, 'common/buildings')
                self.assertEqual(result['status'], 'USE_SEARCH')
                self.assertNotIn('content', result)
                self.assertEqual(self.db.get_definition(path)['status'], 'USE_SEARCH')

    def test_unknown_and_external_paths_are_not_read(self):
        for path in ('common/invented.cwt', 'common/technology.cwt', '../triggers.cwt',
                     '/triggers.cwt', 'C:/private/file.cwt', 'common/../triggers.cwt'):
            with self.subTest(path=path), patch.object(Path, 'read_bytes', side_effect=AssertionError('file I/O')):
                result = self.db.get(path)
                self.assertEqual(result['status'], 'NOT_FOUND')
                self.assertNotIn('content', result)
                self.assertIn('stellaris_list', result['message'])

    def test_search_never_exposes_subdirectory_fields(self):
        for query in ('potential', 'cost', 'allow', 'resources', 'weight', 'base_buildtime',
                      'building', 'common/buildings', 'economic_template', 'gui_button'):
            with self.subTest(query=query):
                result = self.db.search(query, limit=500)
                for row in result['results'] + result.get('related', []):
                    self.assertNotIn('/', row['source']['file'])

    def test_schema_type_filter_guides_to_whole_file(self):
        for type in ('common/buildings', 'common/buildings.cwt', 'buildings'):
            with self.subTest(type=type):
                for result in (self.db.search('potential', type), self.db.get('building', type)):
                    self.assertEqual(result['status'], 'USE_SCHEMA_FILE')
                    self.assertEqual(result['path'], 'common/buildings.cwt')
                    self.assertNotIn('definitions', result)

    def test_old_schema_id_cannot_expose_field_tree(self):
        symbol = next(s for s in self.db.by_file['common/buildings'] if s.name == 'building')
        self.assertEqual(self.db.get(symbol.id)['status'], 'USE_SCHEMA_FILE')

    def test_registry_containers_are_not_whole_file_shortcuts(self):
        for path in ('modifiers.cwt', 'enums.cwt', 'scopes.cwt', 'links.cwt'):
            node = self.db.documents[path].nodes[0]
            symbol = self.db.by_node[id(node)]
            rows = self.db.get(symbol.id)['definitions']
            self.assertEqual(rows, [])
            rows = self.db.search(node.key, path[:-4], 500)['results']
            self.assertNotIn(symbol.id, [row['id'] for row in rows])

    def test_root_api_search_get_and_modifier_scopes(self):
        cases = [('has_backgroud_job', 'trigger', 'has_background_job'),
                 ('any_pop_job', 'trigger', 'any_owned_pop_job'),
                 ('num_assi_jobs', 'trigger', 'num_assigned_jobs'),
                 ('add_resource', 'effect', 'add_resource'),
                 ('country_resource_max_energy_ad', 'modifier', 'country_resource_max_<resource>_add')]
        for query, type, expected in cases:
            with self.subTest(query=query):
                row = self.db.search(query, type)['results'][0]
                self.assertEqual(row['name'], expected)
                definition = self.db.get(row['id'])
                self.assertEqual(definition['status'], 'CONFIRMED_CWT')
                self.assertEqual(definition['definitions'][0]['id'], row['id'])
        row = self.db.get('<district.capped>_max_add', 'modifier')['definitions'][0]
        self.assertEqual(row['scope_resolution']['supported_scopes'],
                         ['planet', 'ship', 'starbase', 'colony', 'sector', 'galacticobject', 'country'])

    def test_schema_validation_keeps_known_and_unknown_fields(self):
        cases = [('base_buildtime = 100 invented_field = yes', 'common/buildings', 'base_buildtime'),
                 ('id = test.1 invented_field = yes', 'events/events', 'id'),
                 ('cost = 2 invented_field = yes', 'common/traits', 'cost')]
        for code, type, known in cases:
            with self.subTest(type=type):
                result = Validator(self.db).validate(code, {'type': type})
                self.assertTrue(any(f['identifier'] == 'invented_field' and f['code'] == 'UNKNOWN_SCHEMA_FIELD'
                                    for f in result['findings']))
                self.assertTrue(any(f['identifier'] == known and f['sources'] for f in result['findings']))

    def test_nested_event_effect_validation_retained(self):
        result = Validator(self.db).validate('immediate = { invented_effect = yes }',
                                            {'type': 'events/events', 'scope': 'country'})
        self.assertTrue(any(f['code'] == 'UNKNOWN_EFFECT' for f in result['findings']))

    def test_internal_semantic_index_remains_complete(self):
        for type in ('common/buildings', 'events/events', 'interface/gui_types'):
            self.assertTrue(self.db.by_file[type])
        self.assertTrue(self.db.lookup('base_buildtime', ('file', 'common/buildings')))
        self.assertTrue(self.db.aliases['gui'])
        self.assertEqual(len(self.db.symbols), 23057)

    def test_catalog_get_and_search_reuse_parsed_corpus(self):
        with patch('stellaris_modder_agent.index.parse_file', side_effect=AssertionError('reparse')):
            self.assert_schema('common/buildings.cwt')
            self.db.list_files()
            self.db.search('has_backgroud_job', 'trigger')
            Validator(self.db).validate('base_buildtime = 100', 'common/buildings')

    def test_global_api_exception_is_semantic_not_directory_name(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'common' / 'mixed.cwt'
            path.parent.mkdir()
            path.write_text('alias[trigger:real_global_api] = bool\n'
                            'alias[gui:private_schema_alias] = scalar\n'
                            'building = { private_field = bool }', encoding='utf-8')
            db = Database(temp)
            self.assertEqual(db.search('real_global_api', 'trigger')['status'], 'CONFIRMED_CWT')
            for query in ('private_schema_alias', 'private_field'):
                self.assertFalse(any(r['name'] == query for r in db.search(query)['results']))
            self.assertEqual(db.get('common/mixed.cwt')['content'], path.read_bytes().decode('utf-8'))

    def test_legacy_get_alias_uses_same_dispatcher(self):
        server = Server(self.db)
        server.initialized = True
        self.assertEqual({t['name'] for t in TOOLS},
                         {'stellaris_search', 'stellaris_list', 'stellaris_validate',
                          'stellaris_doctor'})
        for target in ('triggers.cwt', 'common/buildings.cwt', 'has_background_job'):
            request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                       'params': {'name': 'stellaris_get_definition', 'arguments': {'name': target}}}
            result = server.dispatch(request)['result']
            payload = json.loads(result['content'][0]['text'])
            self.assertNotIn('metadata', payload)
            self.assertEqual(payload, project('stellaris_get', self.db.get(target)))
            self.assertNotIn('structuredContent', result)

    def test_list_accepts_no_arguments(self):
        server = Server(self.db)
        server.initialized = True
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                   'params': {'name': 'stellaris_list', 'arguments': {'limit': 5}}}
        self.assertTrue(server.dispatch(request)['result']['isError'])

    def test_cli_catalog_schema_and_root_guard(self):
        for args, expected, exitcode in [(['list'], self.db.list_files(), 0),
                                        (['get', 'aliases.cwt'], self.db.get('aliases.cwt'), 0),
                                        (['get', 'interface/gui_types.cwt'], self.db.get('interface/gui_types.cwt'), 2),
                                        (['get', 'common/buildings.cwt'], self.db.get('common/buildings.cwt'), 0),
                                        (['get', 'events/events.cwt'], self.db.get('events/events.cwt'), 0),
                                        (['get', 'triggers.cwt'], self.db.get('triggers.cwt'), 2),
                                        (['get', 'effects.cwt'], self.db.get('effects.cwt'), 2)]:
            with self.subTest(args=args):
                result = subprocess.run([sys.executable, str(ROOT / 'stellaris_modder_tool.py')] + args,
                                        cwd=tempfile.gettempdir(), capture_output=True, timeout=30)
                self.assertEqual(result.returncode, exitcode, result.stderr)
                self.assertEqual(json.loads(result.stdout), expected)

    def test_mcp_stdio_catalog_schema_root_guard(self):
        calls = [('stellaris_list', {}, self.db.list_files()),
                 ('stellaris_search', {'query': 'aliases.cwt'}, self.db.get('aliases.cwt')),
                 ('stellaris_search', {'query': 'interface/gui_types.cwt'}, self.db.get('interface/gui_types.cwt')),
                 ('stellaris_search', {'query': 'common/buildings.cwt'}, self.db.get('common/buildings.cwt')),
                 ('stellaris_search', {'query': 'events/events.cwt'}, self.db.get('events/events.cwt')),
                 ('stellaris_search', {'query': 'triggers.cwt'}, self.db.get('triggers.cwt')),
                 ('stellaris_search', {'query': 'effects.cwt'}, self.db.get('effects.cwt'))]
        requests = [{'jsonrpc': '2.0', 'id': 0, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}}]
        requests += [{'jsonrpc': '2.0', 'id': i, 'method': 'tools/call',
                      'params': {'name': name, 'arguments': arguments}} for i, (name, arguments, _) in enumerate(calls, 1)]
        wire = ('\n'.join(json.dumps(r) for r in requests) + '\n').encode()
        process = subprocess.run([sys.executable, str(ROOT / 'stellaris_modder_tool.py'), 'serve'],
                                 input=wire, capture_output=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        replies = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(len(replies), len(calls) + 1)
        for response, (name, _, expected) in zip(replies[1:], calls):
            payload = json.loads(response['result']['content'][0]['text'])
            if name == 'stellaris_list':
                self.assertEqual(payload.pop('metadata')['server']['version'], '1.1.1')
            else:
                self.assertNotIn('metadata', payload)
            self.assertEqual(payload, expected)
            self.assertNotIn('structuredContent', response['result'])

    def test_mcp_http_catalog_schema_root_guard(self):
        server = create_http_server(self.db, port=0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            for name, args, expected in [('stellaris_list', {}, self.db.list_files()),
                                         ('stellaris_search', {'query': 'aliases.cwt'}, self.db.get('aliases.cwt')),
                                         ('stellaris_search', {'query': 'interface/gui_types.cwt'}, self.db.get('interface/gui_types.cwt')),
                                         ('stellaris_search', {'query': 'events/events.cwt'}, self.db.get('events/events.cwt')),
                                         ('stellaris_search', {'query': 'effects.cwt'}, self.db.get('effects.cwt'))]:
                request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}}
                req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/mcp',
                                             data=json.dumps(request).encode(), headers={'Content-Type': 'application/json'})
                with urllib.request.urlopen(req, timeout=5) as response:
                    result = json.load(response)['result']
                    payload = json.loads(result['content'][0]['text'])
                    if name == 'stellaris_list':
                        self.assertEqual(payload.pop('metadata')['server']['version'], '1.1.1')
                    else:
                        self.assertNotIn('metadata', payload)
                    self.assertEqual(payload, expected)
                    self.assertNotIn('structuredContent', result)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
