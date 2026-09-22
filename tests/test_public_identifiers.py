"""Global retrieval exposes registry entries, never their nested parameters."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from stellaris_agent.index import Database, declaration
from stellaris_agent.server import Server
from stellaris_agent.validate import Validator

ROOT = Path(__file__).resolve().parents[1]


class PublicIdentifiersCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = Database()

    def test_base_does_not_impersonate_an_effect(self):
        for query in ('base', 'bsae', 'effects', 'supported_scopes', 'input_scopes'):
            for kind in (None, 'effect', 'effects.cwt', 'trigger', 'modifier', 'links'):
                with self.subTest(query=query, type=kind):
                    result = self.db.search(query, kind, 500)
                    for row in result['results'] + result.get('related', []):
                        node = self.db.by_id[row['id']].node
                        # Corpus registries use declarations or one container level.
                        self.assertTrue(node.parent is None or node.parent.parent is None)
                        if row['type'] in ('effects', 'triggers', 'aliases', 'scope_links'):
                            self.assertIsNone(node.parent)
        exact = [r for r in self.db.search('base')['results'] if r['name'] == 'base']
        self.assertTrue(exact)  # A legitimate modifier_rule alias must survive.
        self.assertTrue(all(r['type'] == 'modifier_rule' for r in exact))

    def test_all_declared_apis_and_registry_entries_remain_public(self):
        for symbol in self.db.symbols:
            node = symbol.node
            decl, _ = declaration(node.key)
            if node.parent is None and decl == 'alias':
                if '/' not in node.document.path or symbol.kind in ('effect', 'trigger', 'modifier'):
                    self.assertIn(symbol.id, self.db.search_ids)
        for name, kind in [('modify_species', 'effect'), ('has_background_job', 'trigger'),
                           ('<district.capped>_max_add', 'modifier'), ('owner', 'links'),
                           ('Country', 'scopes'), ('Colony', 'modifier_categories'),
                           ('weight_or_base', 'enums')]:
            with self.subTest(name=name):
                self.assertEqual(self.db.search(name, kind)['status'], 'CONFIRMED_CWT')
                self.assertEqual(self.db.get(name, kind)['status'], 'CONFIRMED_CWT')
        self.assertEqual(self.db.search('has_backgroud_job', 'trigger')['results'][0]['name'], 'has_background_job')
        self.assertEqual(self.db.search('country_resource_max_energy_add', 'modifier')['status'], 'TEMPLATE_MATCH')
        row = self.db.get('<district.capped>_max_add', 'modifier')['definitions'][0]
        self.assertIn('planet', row['scope_resolution']['supported_scopes'])

    def test_complete_ast_get_and_validator_keep_parameters(self):
        self.assertEqual(len(self.db.symbols), 23057)
        rows = self.db.get_definition('modify_species', 'effect')['definitions']
        self.assertTrue(any(f['key'] == 'base' for r in rows for f in r['fields']))
        self.assertTrue(any('base = auto' in r.get('definition', {}).get('text', '') for r in rows))
        fields = [s for s in self.db.by_name['base'] if s.canonical_type == 'effects']
        self.assertTrue(fields)
        for field in fields:
            self.assertNotIn(field.id, self.db.search_ids)
            self.assertIn(field.id, self.db.by_id)
            self.assertEqual(self.db.get(field.id)['definitions'][0]['raw'], field.node.raw)
        validator = Validator(self.db)
        context = {'type': 'effect', 'scope': 'country'}
        self.assertEqual(validator.validate('modify_species = { base = auto }', context)['status'], 'CONFIRMED_CWT')
        result = validator.validate('modify_species = { base = auto bogus_parameter = yes }', context)
        self.assertTrue(any(f['identifier'] == 'base' and f['status'] == 'CONFIRMED_CWT' and f['sources']
                            for f in result['findings']))
        self.assertTrue(any(f['identifier'] == 'bogus_parameter' and f['code'] == 'UNKNOWN_SCHEMA_FIELD'
                            for f in result['findings']))
        self.assertTrue(any(f['code'] == 'UNKNOWN_EFFECT' for f in validator.validate('base = auto', context)['findings']))

    def test_nested_exact_fuzzy_template_and_browse_paths_are_private(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'effects.cwt'
            path.write_text('alias[effect:real_effect] = {\n'
                            ' private_parameter = bool\n'
                            ' private_<resource>_parameter = int\n'
                            ' inner = { deeply_private = bool }\n'
                            '}\n'
                            'alias[effect:shared_name] = bool\n'
                            'alias[effect:another_effect] = { shared_name = int }\n', encoding='utf-8')
            db = Database(temp)
            for query in ('private_parameter', 'private_paramter', 'private_energy_parameter',
                          'private_energy_paramter', 'deeply_private', 'effects'):
                for kind in (None, 'effects.cwt'):
                    result = db.search(query, kind, 1)
                    self.assertTrue(all(db.by_id[r['id']].node.parent is None
                                        for r in result['results'] + result.get('related', [])))
            shared = db.search('shared_name')['results']
            self.assertEqual(len(shared), 1)
            self.assertEqual(shared[0]['path'], 'alias[effect:shared_name]')
            self.assertTrue(db.lookup('private_parameter'))
            self.assertTrue(db.lookup('private_energy_parameter'))

    def test_mcp_and_cli_use_the_same_public_search_boundary(self):
        server = Server(self.db)
        server.initialized = True

        def search(arguments):
            reply = server.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                     'params': {'name': 'stellaris_search', 'arguments': arguments}})
            return json.loads(reply['result']['content'][0]['text'])

        # The MCP response enriches the same public names; private schema fields stay out.
        rows = search({'query': 'base'})['results']
        row = next(r for r in rows if r['name'] == 'base' and r['type'] == 'modifier_rule')
        self.assertTrue(row['definitions'])
        self.assertFalse(any(r['name'] == 'base' and r['type'] == 'effects' for r in rows))
        process = subprocess.run([sys.executable, str(ROOT / 'stellaris_tool.py'), 'search', 'base'],
                                 capture_output=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        for row in result['results'] + result.get('related', []):
            self.assertNotIn('alias[effect:modify_species]/base', row['path'])
