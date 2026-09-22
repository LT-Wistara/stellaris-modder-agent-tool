"""Dynamic (game/mod data) behaviour, exercised without a game installation.

Every fixture is written into a temporary directory: the tests never read the real
Stellaris installation, so they pass on a bare checkout.  The bundled CWT corpus is
still the schema source, which is what makes these fixtures meaningful -- the corpus
declares the shapes, the fixtures provide the objects.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from stellaris_agent import dynamic as dynamic_module  # noqa: E402
from stellaris_agent import environment as environment_module  # noqa: E402
from stellaris_agent.index import Database  # noqa: E402
from stellaris_agent.parser import parse  # noqa: E402
from stellaris_agent.projection import project  # noqa: E402
from stellaris_agent.validate import Validator  # noqa: E402

GAME_FILES = {
    'stellaris.exe': '',
    'common/defines/00_defines.txt': 'NDefines = { }\n',
    'common/economic_categories/00_mini.txt': '''
planet_jobs = {
    modifier_category = planet_jobs
    generate_mult_modifiers = { produces }
    triggered_upkeep_modifier = {
        key = planet_jobs
        modifier_types = { add }
    }
}

country_base = {
    modifier_category = economic_unit
    generate_add_modifiers = { produces }
}
''',
    'common/strategic_resources/00_mini.txt': '''
energy = { category = basic }
minerals = { category = basic }
alloys = { category = advanced }
''',
    'common/pop_jobs/00_mini.txt': '''
researcher = {
    category = worker
    resources = { category = planet_jobs produces = { physics_research = 4 } }
}
farmer = {
    category = worker
}
''',
    'common/buildings/00_mini.txt': '''
building_fixture_lab = {
    base_buildtime = 120
}
''',
    'common/technology/00_mini.txt': '''
tech_fixture_power = {
    area = physics
    tier = 1
}
''',
    'common/ship_sizes/00_mini.txt': '''
fixture_ship = {
    section_slots = {
        "bow" = { locator = "part1" }
        "mid" = { locator = "part2" }
        "stern" = { locator = "part3" }
    }
}
''',
    'common/astral_actions/00_text_probe.txt': 'text_search_probe = early\n',
    'common/scripted_effects/99_text_probe.txt': '''
text_search_probe = one
text_search_probe = two
text_search_probe = three
''',
    'events/99_text_probe.txt': '''
text_search_probe = event_one
text_search_probe = event_two
''',
}
MOD_FILES = {
    'descriptor.mod': 'name="Fixture Mod"\nsupported_version="v4.2.4"\n',
    'common/scripted_triggers/00_fixture.txt': 'fixture_trigger = {\n\talways = yes\n}\n',
    'common/scripted_effects/00_fixture.txt': 'fixture_effect = {\n\tadd_resource = { energy = 10 }\n}\n',
}


def write_tree(root: Path, files):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')


class DynamicCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temp = tempfile.TemporaryDirectory()
        base = Path(cls._temp.name)
        cls.game_root = base / 'game'
        cls.mod_root = base / 'mod'
        write_tree(cls.game_root, GAME_FILES)
        write_tree(cls.mod_root, MOD_FILES)
        cls.environment = environment_module.Environment(
            game_root=cls.game_root, mod_root=cls.mod_root,
            mod_name='Fixture Mod', mod_supported_version='v4.2.4', dynamic_ready=True)

    @classmethod
    def tearDownClass(cls):
        cls._temp.cleanup()

    def database(self, game_data=True):
        return Database(game_data=game_data, environment=self.environment if game_data else None)

    # region environment

    def test_explicit_roots_are_accepted_and_validated(self):
        self.assertEqual(environment_module.detect_game_root(self.game_root), self.game_root)
        self.assertEqual(environment_module.detect_mod_root(self.mod_root), self.mod_root)

    def test_marker_validation_rejects_a_directory_without_game_data(self):
        with tempfile.TemporaryDirectory() as empty:
            ok, detail = environment_module.is_game_root(Path(empty))
            self.assertFalse(ok)
            self.assertIn('common/', detail)

    def test_descriptor_is_parsed(self):
        descriptor = environment_module.read_descriptor(self.mod_root)
        self.assertEqual(descriptor['name'], 'Fixture Mod')
        self.assertEqual(descriptor['supported_version'], 'v4.2.4')

    # endregion

    # region generated modifiers

    def test_economic_category_generator_matches_the_documented_rule(self):
        db = self.database()
        modifiers = db.dynamic.modifiers()
        # `<economic_category>_<resource>_<category>_<type>`
        self.assertIn('planet_jobs_energy_produces_mult', modifiers)
        entry = modifiers['planet_jobs_energy_produces_mult']
        self.assertEqual(entry['economic_category'], 'planet_jobs')
        self.assertEqual(entry['resource'], 'energy')
        self.assertEqual(entry['category'], 'produces')
        self.assertEqual(entry['modifier_type'], 'mult')
        self.assertFalse(entry['triggered'])

    def test_triggered_declaration_uses_its_own_key(self):
        db = self.database()
        entry = db.dynamic.modifiers().get('planet_jobs_minerals_upkeep_add')
        self.assertIsNotNone(entry, 'triggered_upkeep_modifier key/type pair must generate a name')
        self.assertTrue(entry['triggered'])
        self.assertEqual(entry['category'], 'upkeep')

    def test_resource_less_names_only_exist_for_mult(self):
        db = self.database()
        modifiers = db.dynamic.modifiers()
        self.assertIn('planet_jobs_produces_mult', modifiers)
        self.assertNotIn('planet_jobs_produces_add', modifiers)

    def test_unknown_combination_is_not_generated(self):
        db = self.database()
        # `country_base` enables `add`/`produces` only: no mult, no upkeep.
        modifiers = db.dynamic.modifiers()
        self.assertIn('country_base_energy_produces_add', modifiers)
        self.assertNotIn('country_base_energy_produces_mult', modifiers)
        self.assertNotIn('planet_jobs_energy_upkeep_mult', modifiers)

    def test_modifier_scopes_come_from_the_modifier_category(self):
        db = self.database()
        entry = db.dynamic.modifiers()['planet_jobs_energy_produces_mult']
        # The corpus declares the Planet/Colony categories; either spelling resolves.
        self.assertTrue(entry['supported_scopes'])

    # endregion

    # region placeholder verification

    def test_placeholders_are_verified_against_real_objects(self):
        db = self.database()
        verified = db.dynamic.verify_captures([{'placeholder': '<job>', 'value': 'researcher'},
                                               {'placeholder': '<job>', 'value': 'not_a_job'}])
        self.assertEqual(verified[0]['object_existence'], 'CONFIRMED')
        self.assertEqual(verified[0]['resolved_from']['source'], 'game')
        self.assertEqual(verified[1]['object_existence'], 'NOT_FOUND')

    def test_economic_placeholder_splits_category_and_resource(self):
        db = self.database()
        verified = db.dynamic.verify_captures(
            [{'placeholder': '<economic_category>[_<resource>]', 'value': 'planet_jobs_energy'}])
        self.assertEqual(verified[0]['object_existence'], 'CONFIRMED')
        self.assertEqual(verified[0]['object_type'], 'economic_category + resource')

    def test_enum_values_resolve_from_the_corpus(self):
        db = self.database()
        verified = db.dynamic.verify_captures(
            [{'placeholder': 'enum[economic_modifier_type]', 'value': 'mult'},
             {'placeholder': 'enum[economic_modifier_type]', 'value': 'nope'}])
        self.assertEqual([entry['object_existence'] for entry in verified], ['CONFIRMED', 'NOT_FOUND'])

    # endregion

    # region mod declarations

    def test_mod_trigger_is_a_symbol(self):
        db = self.database()
        symbols = db.dynamic.symbols('fixture_trigger')
        self.assertEqual(len(symbols), 1)
        self.assertEqual(symbols[0].kind, 'trigger')
        self.assertEqual(symbols[0].origin['source'], 'mod')
        self.assertIn('always = yes', symbols[0].defined_text()['text'])

    def test_search_and_get_expose_mod_declarations(self):
        db = self.database()
        self.assertEqual(db.search('fixture_trigger')['status'], 'CONFIRMED_GAME_DATA')
        definition = db.get('fixture_trigger')
        self.assertEqual(definition['status'], 'CONFIRMED_GAME_DATA')
        self.assertIn('always = yes', definition['definitions'][0]['definition']['text'])

    def test_reported_type_can_be_used_as_a_filter(self):
        """The type a result reports must be accepted back by the next call."""
        db = self.database()
        row = db.search('fixture_trigger')['results'][0]
        self.assertEqual(row['type'], 'scripted_trigger')
        definition = db.get(row['name'], row['type'])
        self.assertEqual(definition['status'], 'CONFIRMED_GAME_DATA')
        self.assertIn('always = yes', definition['definitions'][0]['definition']['text'])

    def test_unknown_type_is_still_reported_when_it_is_not_a_definition_type(self):
        db = self.database()
        self.assertEqual(db.get('fixture_trigger', 'not_a_type')['status'], 'UNKNOWN_TYPE')

    def test_validation_confirms_mod_declarations(self):
        db = self.database()
        result = Validator(db).validate('fixture_trigger = yes', {'type': 'triggers', 'scope': 'planet'})
        self.assertEqual(result['status'], 'CONFIRMED_GAME_DATA')
        self.assertEqual(result['findings'][0]['code'], 'DEFINED_IN_GAME_DATA')
        self.assertTrue(result['coverage']['runtime_objects'])

    def test_validation_confirms_generated_modifiers(self):
        db = self.database()
        result = Validator(db).validate('planet_jobs_energy_produces_mult = 0.1',
                                        {'type': 'common/modifiers', 'schema': 'static_modifier', 'mode': 'body'})
        self.assertEqual(result['findings'][0]['code'], 'GENERATED_MODIFIER')
        self.assertEqual(result['findings'][0]['generated_from']['resource'], 'energy')

    def test_validation_checks_literal_object_references(self):
        db = self.database()
        known = Validator(db).validate('has_building = building_fixture_lab',
                                       {'type': 'trigger', 'scope': 'planet'})
        self.assertEqual(known['status'], 'CONFIRMED_GAME_DATA')
        self.assertEqual(known['findings'][0]['code'], 'OBJECT_REFERENCE')
        self.assertEqual(known['findings'][0]['object_type'], 'building')

        unknown = Validator(db).validate('has_building = building_fixture_lba',
                                         {'type': 'trigger', 'scope': 'planet'})
        self.assertEqual(unknown['status'], 'UNKNOWN')
        self.assertEqual(unknown['findings'][0]['code'], 'UNKNOWN_OBJECT_REFERENCE')
        self.assertEqual(unknown['findings'][0]['object_type'], 'building')
        self.assertEqual(unknown['findings'][0]['candidates'][0], 'building_fixture_lab')
        compact = project('stellaris_validate', unknown)
        self.assertEqual(compact['findings'][0]['object_type'], 'building')
        self.assertEqual(compact['findings'][0]['candidates'][0], 'building_fixture_lab')

    def test_reference_types_are_indexed_on_demand(self):
        db = self.database()
        self.assertNotIn('technology', db.dynamic.index.stats['types_requested'])
        known = Validator(db).validate('has_technology = tech_fixture_power',
                                       {'type': 'trigger', 'scope': 'country'})
        self.assertEqual(known['status'], 'CONFIRMED_GAME_DATA')
        self.assertEqual(known['findings'][0]['object_type'], 'technology')

        unknown = Validator(db).validate('has_technology = tech_fixture_powre',
                                         {'type': 'trigger', 'scope': 'country'})
        self.assertEqual(unknown['status'], 'UNKNOWN')
        self.assertEqual(unknown['findings'][0]['candidates'][0], 'tech_fixture_power')

    def test_typos_reach_mod_declarations(self):
        """A name guessed from memory is usually slightly wrong: it must still resolve."""
        result = self.database().search('fixture_triger')          # one letter missing
        self.assertIn('fixture_trigger', [row['name'] for row in result['results']])
        self.assertEqual(result['results'][0]['status'], 'SUGGESTION')

    def test_typos_reach_generated_modifiers(self):
        result = self.database().search('planet_jobs_energy_produces_mul')
        self.assertIn('planet_jobs_energy_produces_mult',
                      [row['name'] for row in result['results']])

    def test_typos_do_not_invent_names_without_data(self):
        """Without game/mod data the corpus-only behaviour must stay untouched."""
        result = self.database(game_data=False).search('fixture_triger')
        self.assertNotIn('fixture_trigger', [row['name'] for row in result.get('results', [])])

    def test_fuzzy_suggestion_never_becomes_evidence(self):
        result = self.database().search('planet_jobs_energy_produces_mul')
        self.assertTrue(all(row['status'] == 'SUGGESTION' for row in result['results']))
        # The same gate must hold for a misspelled mod declaration: the query itself is
        # wrong, so its hit may never be reported as a confirmed object.
        self.assertTrue(all(row['status'] == 'SUGGESTION'
                            for row in self.database().search('fixture_triger')['results']))

    def test_partial_name_of_a_declaration_is_confirmed_not_suggested(self):
        """A query that is the head of a real declaration is not a spelling guess.

        ``paladin`` -> ``paladin_ship``: every query token matches a real token and
        nothing is substituted, so the declaration status is what the caller asked for.
        The misspelling above must keep its SUGGESTION status in the very same run.
        """
        db = self.database()
        confirmed = project('stellaris_search', db.search('fixture'))
        self.assertEqual(confirmed['status'], 'CONFIRMED_GAME_DATA')
        self.assertIn('fixture_trigger', [row['name'] for row in confirmed['results']])
        missed = project('stellaris_search', db.search('fixture_triger'))
        self.assertNotIn('fixture_trigger', [row['name'] for row in missed.get('results', [])])
        self.assertIn('fixture_trigger', [row['name'] for row in missed.get('suggestions', [])])

    def test_dynamic_lookup_stays_unknown_without_a_game(self):
        db = self.database(game_data=False)
        self.assertEqual(Validator(db).validate('fixture_trigger = yes',
                                                {'type': 'triggers', 'scope': 'planet'})['status'], 'UNKNOWN')
        # The documented shape still matches structurally, as it did before.
        self.assertEqual(db.lookup('planet_jobs_energy_produces_mult', ('kind', 'modifier'))[0][1],
                         'TEMPLATE_MATCH')

    def test_text_mode_searches_script_lines(self):
        """Text mode answers "where is this written" with a root, a path and a line."""
        db = Database(game_data=True, environment=self.environment)
        result = db.search_text('always = yes', 'common/scripted_triggers', limit=5)
        self.assertEqual(result['status'], 'CONFIRMED_CWT')
        self.assertGreaterEqual(result['total'], 1)
        self.assertEqual(result['returned'], len(result['rows']))
        self.assertTrue(result['scan_complete'])
        row = result['rows'][0]
        self.assertTrue(row['file'].startswith('mod:'))
        self.assertIn('scripted_triggers', row['file'])
        self.assertEqual(row['line'], 2)
        self.assertIn('always = yes', row['text'])
        self.assertEqual(db.search_text('no_such_line_anywhere')['status'], 'NOT_FOUND')

    def test_text_mode_samples_relevant_directories_and_reports_honest_counts(self):
        db = Database(game_data=True, environment=self.environment)
        result = db.search_text('text_search_probe', limit=2)
        self.assertEqual(len(result['rows']), 2)
        self.assertEqual(result['returned'], 2)
        self.assertEqual(result['total'], 6)
        self.assertTrue(result['has_more'])
        self.assertTrue(result['scan_complete'])
        files = {row['file'] for row in result['rows']}
        self.assertTrue(any('common/scripted_effects/' in path for path in files))
        self.assertTrue(any('events/' in path for path in files))

    def test_dynamic_complex_enum_accepts_real_section_slots(self):
        db = self.database()
        result = Validator(db).validate('section = { slot = "mid" }',
                                        {'schema': 'global_ship_design'})
        mismatch = [row for row in result['findings'] if row['code'] == 'VALUE_MISMATCH']
        self.assertEqual(mismatch, [])
        self.assertTrue(any(row['identifier'] == 'slot' and row['status'] == 'CONFIRMED_CWT'
                            for row in result['findings']))

        invalid = Validator(db).validate('section = { slot = "imaginary" }',
                                         {'schema': 'global_ship_design'})
        mismatch = [row for row in invalid['findings'] if row['code'] == 'VALUE_MISMATCH']
        self.assertEqual(len(mismatch), 1)
        self.assertIn('complex_enum[section_slot]', mismatch[0]['message'])

    def test_complex_enum_without_game_data_is_unresolved_not_rejected(self):
        result = Validator(self.database(game_data=False)).validate(
            'section = { slot = "mid" }', {'schema': 'global_ship_design'})
        mismatch = [row for row in result['findings'] if row['code'] == 'VALUE_MISMATCH']
        self.assertEqual(mismatch, [])
        self.assertTrue(any(row['code'] == 'VALUE_REFERENCE_UNRESOLVED'
                            for row in result['findings']))

    def test_cardinality_warning_points_at_the_declaring_block(self):
        """A missing field belongs to the block that should declare it, not a sibling.

        Taken verbatim from the game's own ``create_normal_pirate_country``, which sets
        ``background`` and ``colors`` but no ``icon``: the warning must name the ``flag``
        block that owes the field, not the ``background`` block that happens to be first.
        """
        code = 'fixture_effect = {\n' \
               '\tcreate_country = {\n' \
               '\t\tflag = {\n' \
               '\t\t\tbackground = {\n\t\t\t\tcategory = "backgrounds"\n\t\t\t\tfile = "00_solid.dds"\n\t\t\t}\n' \
               '\t\t\tcolors = {\n\t\t\t\t"black"\n\t\t\t\t"null"\n\t\t\t\t"null"\n\t\t\t\t"null"\n\t\t\t}\n' \
               '\t\t}\n' \
               '\t}\n' \
               '}'
        result = Validator(self.database()).validate(code, {'schema': 'scripted_effect', 'mode': 'file'})
        warnings = [f for f in result['findings'] if f['code'] == 'CARDINALITY_MISMATCH']
        self.assertEqual(len(warnings), 1)
        warning = warnings[0]
        self.assertEqual(warning['field'], 'icon')
        self.assertEqual(warning['identifier'], 'flag')       # not ``background``
        self.assertEqual(warning['line'], 3)                  # the flag block, not its child

    def test_mod_declared_effect_is_confirmed_not_unknown(self):
        """The corpus cannot declare mod objects; the loaded data can, and must be asked."""
        result = Validator(self.database()).validate('fixture_effect = yes',
                                                     {'schema': 'scripted_effect', 'mode': 'body'})
        self.assertEqual(result['status'], 'CONFIRMED_GAME_DATA')
        self.assertEqual([f['code'] for f in result['findings']], ['DEFINED_IN_GAME_DATA'])
        self.assertNotIn('UNKNOWN_EFFECT', [f['code'] for f in result['findings']])

    # endregion

    # region reporting

    def test_doctor_reports_sources_and_index(self):
        db = self.database()
        report = db.doctor()
        self.assertTrue(report['enabled'])
        self.assertEqual(Path(report['environment']['game_root']), self.game_root)
        self.assertEqual(report['environment']['mod_root'], str(self.mod_root))
        self.assertEqual(report['index']['types']['economic_category'], 2)
        self.assertGreater(report['generated_modifiers'], 0)
        self.assertIn(report['version_check']['corpus'], (None, '4.5'))

    def test_a_new_mod_entry_appears_without_restarting(self):
        """Modding is iterative: a file written now must be visible in the next query."""
        with tempfile.TemporaryDirectory() as temp:
            game, mod = Path(temp) / 'game', Path(temp) / 'mod'
            write_tree(game, GAME_FILES)
            write_tree(mod, MOD_FILES)
            environment = environment_module.Environment(game_root=game, mod_root=mod, dynamic_ready=True)
            db = Database(game_data=True, environment=environment)
            db.dynamic.interval = 0.0                        # check on every access
            self.assertEqual(db.search('brand_new_trigger')['status'], 'NOT_FOUND')
            added = mod / 'common/scripted_triggers/01_added_later.txt'
            added.write_text('brand_new_trigger = {\n\talways = yes\n}\n', encoding='utf-8')
            result = db.search('brand_new_trigger')
            self.assertEqual(result['status'], 'CONFIRMED_GAME_DATA')
            self.assertEqual(result['results'][0]['source']['source'], 'mod')
            self.assertEqual(db.dynamic.report()['watch']['rebuilds'], 1)

    def test_a_new_mod_entry_is_also_reachable_by_typo(self):
        with tempfile.TemporaryDirectory() as temp:
            game, mod = Path(temp) / 'game', Path(temp) / 'mod'
            write_tree(game, GAME_FILES)
            write_tree(mod, MOD_FILES)
            environment = environment_module.Environment(game_root=game, mod_root=mod, dynamic_ready=True)
            db = Database(game_data=True, environment=environment)
            db.dynamic.interval = 0.0
            (mod / 'common/scripted_triggers/02_later.txt').write_text(
                'brand_new_trigger = {\n\talways = yes\n}\n', encoding='utf-8')
            result = db.search('brand_new_triger')           # one letter missing
            self.assertIn('brand_new_trigger', [row['name'] for row in result['results']])

    def test_an_edit_of_an_existing_file_is_noticed(self):
        """An edit that keeps file count and byte size must still refresh the snapshot."""
        with tempfile.TemporaryDirectory() as temp:
            game, mod = Path(temp) / 'game', Path(temp) / 'mod'
            write_tree(game, GAME_FILES)
            write_tree(mod, MOD_FILES)
            environment = environment_module.Environment(game_root=game, mod_root=mod, dynamic_ready=True)
            db = Database(game_data=True, environment=environment)
            db.dynamic.interval = 0.0
            # Before the edit the name is only reachable as a spelling suggestion.
            before = db.search('fixture_effecx')
            self.assertNotEqual(before['status'], 'CONFIRMED_GAME_DATA')
            self.assertNotIn('fixture_effecx', [row['name'] for row in before.get('results', [])])
            edited = mod / 'common/scripted_effects/00_fixture.txt'
            original = edited.read_text('utf-8')
            # Same length as `fixture_effect`, so only the timestamp can reveal it.
            edited.write_text(original.replace('fixture_effect', 'fixture_effecx'), encoding='utf-8')
            self.assertEqual(len(original), len(edited.read_text('utf-8')))
            self.assertEqual(db.search('fixture_effecx')['status'], 'CONFIRMED_GAME_DATA')

    def test_watching_can_be_switched_off(self):
        with tempfile.TemporaryDirectory() as temp:
            game, mod = Path(temp) / 'game', Path(temp) / 'mod'
            write_tree(game, GAME_FILES)
            write_tree(mod, MOD_FILES)
            environment = environment_module.Environment(game_root=game, mod_root=mod, dynamic_ready=True)
            db = Database(game_data=True, environment=environment, watch=False)
            db.dynamic.interval = 0.0
            self.assertEqual(db.search('fixture_trigger')['status'], 'CONFIRMED_GAME_DATA')
            (mod / 'common/scripted_triggers/03_later.txt').write_text(
                'later_trigger = {\n\talways = yes\n}\n', encoding='utf-8')
            self.assertEqual(db.search('later_trigger')['status'], 'NOT_FOUND')
            self.assertEqual(db.dynamic.report()['watch'], {'enabled': False, 'interval_seconds': 0.0,
                                                            'rebuilds': 0})

    def test_doctor_is_read_only_when_disabled(self):
        report = self.database(game_data=False).doctor()
        self.assertFalse(report['enabled'])
        self.assertIn('disabled', report['reason'])

    def test_doctor_reports_the_environment_built_lazily(self):
        """Reporting must not snapshot state before the lazy build runs.

        The CLI passes an environment in, but an embedder that only asks for a
        doctor run must still see the detected roots.
        """
        db = Database(game_data=True)
        with mock.patch.object(dynamic_module.environment_module, 'detect_environment',
                               return_value=self.environment):
            report = db.doctor()
        self.assertEqual(report['environment']['mod_root'], str(self.mod_root))
        self.assertEqual(report['index']['types']['economic_category'], 2)
        self.assertGreater(report['generated_modifiers'], 0)

    def test_doctor_survives_serialisation(self):
        json.dumps(self.database().doctor())

    # endregion


class ParserRobustnessCase(unittest.TestCase):
    """Vanilla script constructs that only appear once game files are read."""

    def test_optional_scope_link_keeps_its_question_mark(self):
        document = parse('owner? = { has_valid_civic = civic_x }\ncolony.controller? = { always = yes }\n',
                         '<fixture>')
        keys = [node.key for node in document.nodes]
        self.assertEqual(keys, ['owner?', 'colony.controller?'])

    def test_safe_assign_operator_is_still_an_operator(self):
        document = parse('value ?= 10\n', '<fixture>')
        self.assertEqual(document.nodes[0].operator, '?=')

    def test_crlf_offsets_survive_definitions(self):
        # Definition text is sliced from raw bytes; CRLF must not shift it.
        with tempfile.TemporaryDirectory() as temp:
            game = Path(temp) / 'game'
            mod = Path(temp) / 'mod'
            write_tree(game, {name: text.replace('\n', '\r\n') for name, text in GAME_FILES.items()})
            write_tree(mod, MOD_FILES)
            environment = environment_module.Environment(game_root=game, mod_root=mod, dynamic_ready=True)
            db = Database(game_data=True, environment=environment)
            definition = db.dynamic.index.get('scripted_trigger', 'fixture_trigger')
            self.assertTrue(definition.read_text().startswith('fixture_trigger = {'))


if __name__ == '__main__':
    unittest.main()
