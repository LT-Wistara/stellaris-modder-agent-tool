"""Output-only regressions: compact transport without losing CWT evidence."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from stellaris_agent.index import Database
from stellaris_agent.server import Server
from stellaris_agent.validate import Validator
from stellaris_agent.projection import project

ROOT = Path(__file__).resolve().parents[1]


class CompactOutputCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = Database()

    def assert_no_manifest(self, value):
        if isinstance(value, dict):
            self.assertNotIn('upstream', value)
            self.assertNotIn('files', value)
            for child in value.values():
                self.assert_no_manifest(child)
        elif isinstance(value, list):
            for child in value:
                self.assert_no_manifest(child)

    def call(self, name, arguments):
        server = Server(self.db)
        server.initialized = True
        return server.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                'params': {'name': name, 'arguments': arguments}})['result']

    def test_get_has_no_manifest(self):
        result = self.db.get_definition('has_background_job')
        self.assert_no_manifest(result)
        self.assertEqual(result['dataset'], {'commit': self.db.upstream['commit_sha']})

    def test_validate_unknown_has_no_manifest(self):
        result = Validator(self.db).validate('has_magic_planet = yes', 'trigger')
        self.assertEqual(result['status'], 'UNKNOWN')
        self.assertEqual(result['findings'][0]['code'], 'UNKNOWN_TRIGGER')
        self.assert_no_manifest(result)
        self.assertEqual(result['dataset'], {'commit': self.db.upstream['commit_sha']})

    def test_search_suggestion_not_found_and_errors_have_no_manifest(self):
        results = [self.db.search('has_background_job'), self.db.search('job', 'trigger'),
                   self.db.search('country_resource_max_energy_ad', 'modifier'),
                   self.db.get_definition('zzzz_nonexistent_identifier'),
                   self.db.get_definition('job', 'unknown_type'),
                   Validator(self.db).validate('bad = {', 'trigger'),
                   self.call('stellaris_search', {})]
        for result in results:
            with self.subTest(status=result.get('status')):
                self.assert_no_manifest(result)

    def test_definition_provenance_and_evidence_unchanged(self):
        for name in ('has_background_job', '<district.capped>_max_add', 'num_assigned_jobs',
                     'country_resource_max_energy_add'):
            with self.subTest(name=name):
                result = self.db.get_definition(name)
                by_id = {row['id']: row for row in result['definitions']}
                matches = self.db.lookup(name, registry_only=True)
                self.assertEqual(len(result['definitions']), len(matches))
                for row, match in zip(result['definitions'], matches):
                    self.assertTrue(row['source']['file'])
                    self.assertGreater(row['source']['line'], 0)
                    self.assertGreaterEqual(row['source']['end_line'], row['source']['line'])
                    self.assertEqual(row['source']['commit'], self.db.upstream['commit_sha'])
                    expanded = dict(row)
                    if 'definition_ref' in expanded:
                        expanded['definition'] = by_id[expanded.pop('definition_ref')]['definition']
                    # Compare every valuable field, not just a count or status.
                    self.assertEqual(expanded, self.db.result(*match, full=True))

    def test_contiguous_source_text_occurs_once(self):
        rows = self.db.get_definition('has_background_job')['definitions']
        inline = [row for row in rows if 'definition' in row]
        refs = [row for row in rows if 'definition_ref' in row]
        self.assertEqual(len(inline), 1)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]['definition_ref'], inline[0]['id'])
        text = inline[0]['definition']['text']
        self.assertIn('alias[trigger:has_background_job] = <job>', text)
        self.assertIn('alias[trigger:has_background_job] = yes', text)
        self.assertIn('## scopes = { leader }', text)
        self.assertEqual(sum('definition' in row for row in rows), 1)

    def test_get_by_id_is_self_contained_after_grouped_query(self):
        rows = self.db.get_definition('has_background_job')['definitions']
        for row in rows:
            selected = self.db.get_definition(row['id'])['definitions']
            self.assertEqual(len(selected), 1)
            self.assertIn('definition', selected[0])
            self.assertNotIn('definition_ref', selected[0])
            self.assertEqual(selected[0]['id'], row['id'])

    def test_identical_source_at_distinct_locations_not_merged(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'a.cwt').write_text('alias[trigger:dup] = yes\n\nalias[trigger:dup] = yes', encoding='utf-8')
            (root / 'b.cwt').write_text('alias[trigger:dup] = yes', encoding='utf-8')
            rows = Database(root).get_definition('dup')['definitions']
            self.assertEqual(len(rows), 3)
            self.assertTrue(all('definition' in row for row in rows))

    def test_validate_real_provenance_retained(self):
        result = Validator(self.db).validate('has_unlocked_council_positions >= 2',
                                              {'type': 'trigger', 'scope': 'country'})
        self.assertEqual(result['status'], 'CONFIRMED_CWT')
        source = result['findings'][0]['sources'][0]
        self.assertEqual(source['file'], 'triggers.cwt')
        self.assertGreater(source['line'], 0)
        self.assertEqual(source['commit'], self.db.upstream['commit_sha'])

    def test_stats_and_corpus_retain_full_dataset_details(self):
        for command in ('stats', 'corpus'):
            with self.subTest(command=command):
                process = subprocess.run([sys.executable, str(ROOT / 'stellaris_tool.py'), command],
                                         capture_output=True, timeout=30)
                self.assertEqual(process.returncode, 0, process.stderr)
                result = json.loads(process.stdout)
                self.assertEqual(result['upstream'], self.db.upstream)
                self.assertEqual(result['upstream']['file_count'], 173)
                self.assertEqual(len(result['upstream']['files']), 173)
                self.assertEqual(result['PARSED'], 173)
                self.assertEqual(result['FAILED'], 0)

    def test_cli_detailed_and_mcp_text_projected_result_models(self):
        cases = [(['get', 'has_background_job'], 'stellaris_get_definition', {'name': 'has_background_job'}),
                 (['search', 'country_resource_max_energy_ad', '-t', 'modifier'], 'stellaris_search',
                  {'query': 'country_resource_max_energy_ad', 'type': 'modifier'}),
                 (['validate', '--code', 'has_magic_planet = yes', '--context', 'trigger'], 'stellaris_validate',
                  {'code': 'has_magic_planet = yes', 'context': 'trigger'})]
        for args, name, arguments in cases:
            with self.subTest(tool=name):
                process = subprocess.run([sys.executable, str(ROOT / 'stellaris_tool.py'),
                                          '--no-game-data'] + args,
                                         capture_output=True, timeout=30)
                self.assertIn(process.returncode, (0, 2), process.stderr)
                cli = json.loads(process.stdout)
                mcp = self.call(name, arguments)
                payload = json.loads(mcp['content'][0]['text'])
                self.assertNotIn('metadata', payload)
                if name == 'stellaris_search':
                    plain = json.loads(json.dumps(payload))
                    for key in ('results', 'templates', 'suggestions'):
                        for row in plain.get(key, []):
                            row.pop('definitions')
                    expected = project(name, self.db.search(arguments['query'],
                                                             arguments.get('type'), 5))
                    self.assertEqual(expected, plain)
                    self.assertTrue(all(row.get('definitions') for key in
                                        ('results', 'templates', 'suggestions')
                                        for row in payload.get(key, [])))
                else:
                    self.assertEqual(project(name, cli), payload)
                self.assertNotEqual(cli, payload)
                self.assert_no_manifest(cli)
                self.assertFalse(mcp['isError'])
                self.assertNotIn('structuredContent', mcp)

    def test_mcp_text_content_for_overloads(self):
        result = self.call('stellaris_get_definition', {'name': 'has_background_job'})
        payload = json.loads(result['content'][0]['text'])
        # Overloads sharing one source block repeat a pointer, never the text again.
        self.assertEqual(len([row for row in payload['definitions'] if 'text' in row]), 1)
        for row in payload['definitions']:
            self.assertLessEqual(set(row), {'type', 'text', 'same_as', 'scopes',
                                            'unresolved_categories', 'where'})
        self.assertNotIn('structuredContent', result)

    def test_mcp_text_content_keeps_long_query(self):
        result = self.call('stellaris_search', {'query': 'x' * 512})
        payload = json.loads(result['content'][0]['text'])
        self.assertIn(payload['status'], ('NOT_FOUND', 'SUGGESTION'))
        self.assertLessEqual(set(payload),
                             {'status', 'results', 'templates', 'suggestions', 'more', 'tips'})
        self.assertNotIn('structuredContent', result)


if __name__ == '__main__':
    unittest.main()
