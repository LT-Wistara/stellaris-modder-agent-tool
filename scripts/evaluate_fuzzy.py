#!/usr/bin/env python3
"""Reproducible corpus-derived ranking evaluation; no external data or packages."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import random
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from stellaris_agent.index import Database
from stellaris_agent.environment import Environment
from stellaris_agent.server import Server
from stellaris_agent.validate import Validator


def make_cases(db):
    """Disjoint source names for calibration/holdout; perturb actual CWT names.

    Five mutations model the ways an LLM misremembers an identifier: a dropped
    token, an invented token, a misspelling, an adjacent transposition and a
    separator error. `truncation` covers "mostly right, locally wrong" - the last
    token typed only half way.
    """
    rng = random.Random(20260919)
    cases = []
    for kind in ('trigger', 'effect', 'modifier'):
        names = sorted({s.name for s in db.symbols if s.kind == kind and not s.template.dynamic
                        and s.name.isascii() and all(t.isalpha() for t in s.name.split('_'))
                        and 3 <= len(s.name.split('_')) <= 7
                        and max(map(len, s.name.split('_'))) >= 5})
        rng.shuffle(names)
        for index, name in enumerate(names[:60]):
            parts = name.split('_')
            longest = max(range(len(parts)), key=lambda i: len(parts[i]))
            word = parts[longest]
            deletion = word[:len(word)//2] + word[len(word)//2+1:]
            transpose = next(i for i in range(len(word)-1) if word[i] != word[i+1])
            swapped = word[:transpose]+word[transpose+1]+word[transpose]+word[transpose+2:]
            typo_parts = list(parts)
            typo_parts[longest] = deletion
            swap_parts = list(parts)
            swap_parts[longest] = swapped
            truncate_parts = list(parts)
            truncate_parts[longest] = word[:max(4, len(word)//2)]
            changes = [('missing_token', '_'.join(parts[:1]+parts[2:])),
                       ('extra_token', '_'.join(parts[:1]+['extra']+parts[1:])),
                       ('typo', '_'.join(typo_parts)), ('transposition', '_'.join(swap_parts)),
                       ('truncation', '_'.join(truncate_parts)),
                       ('separator', '_'.join(parts[:1]+[''.join(parts[1:3])]+parts[3:]))]
            for mutation, query in changes:
                # Another exact CWT identifier is a legitimate separate intent.
                if db.lookup(query, ('kind', kind)):
                    continue
                cases.append({'split': 'calibration' if index < 30 else 'holdout',
                              'mutation': mutation, 'query': query, 'type': kind, 'expected': name})
    negatives = [('country_resource_energy', 'modifier'), ('has_quantum_banana', 'trigger'),
                 ('any_quantum_banana', 'trigger'), ('country_quuxvortex_wombat', 'modifier'),
                 ('planet_wombat_dancing', 'modifier'), ('create_quuxvortex_wombat', 'effect'),
                 ('zzzz_completely_fake_api_98765', 'trigger'), ('banana_resource_pancake', 'modifier'),
                 ('has_zzqv_nonsense', 'trigger'), ('every_wombat_quantum', 'effect'),
                 ('set_zzqv_wombat', 'effect'), ('country_wombat_resource_energy', 'modifier')]
    cases.extend({'split': 'negative', 'mutation': 'unrelated', 'query': q, 'type': t, 'expected': None}
                 for q, t in negatives)
    return cases


def make_permutation_cases(db):
    """Reordered identifiers: the same tokens, but not a name anyone typed.

    These have no correct answer. They measure the false-suggestion rate of a shuffled
    query: a scorer that does not really enforce token order treats them exactly like
    the real name they were built from.
    """
    rng = random.Random(20260919)
    cases = []
    for kind in ('trigger', 'effect', 'modifier'):
        names = sorted({s.name for s in db.symbols if s.kind == kind and not s.template.dynamic
                        and s.name.isascii() and all(t.isalpha() for t in s.name.split('_'))
                        and 3 <= len(s.name.split('_')) <= 5})
        rng.shuffle(names)
        for name in names[:50]:
            parts = name.split('_')
            for label, shuffled in (('reverse', list(reversed(parts))),
                                    ('rotate', parts[-1:] + parts[:-1])):
                if shuffled == parts:
                    continue
                query = '_'.join(shuffled)
                # Skip when the shuffle happens to spell another real identifier.
                if db.lookup(query, ('kind', kind)):
                    continue
                cases.append({'split': 'permutation', 'mutation': label, 'query': query,
                              'type': kind, 'expected': None, 'source': name})
    return cases


def evaluate(db, cases):
    groups = defaultdict(lambda: {'queries': 0, 'hit_at_1': 0, 'hit_at_5': 0, 'returned': 0,
                                  'ranked_anywhere': 0})
    misses, timings, snapshots = [], [], []
    for case in cases:
        start = time.perf_counter()
        result = db.search(case['query'], case['type'], 100)
        timings.append(1000*(time.perf_counter()-start))
        # Overloads remain separate result locations, but are one candidate name.
        names = list(dict.fromkeys(row['name'] for row in result['results']))
        related = list(dict.fromkeys(row['name'] for row in result.get('related', ())))
        expected = case['expected']
        rank = names.index(expected)+1 if expected in names else None
        for key in (case['split'], case['split']+'/'+case['mutation']):
            group = groups[key]
            group['queries'] += 1
            group['hit_at_1'] += rank == 1
            group['hit_at_5'] += rank is not None and rank <= 5
            group['returned'] += bool(names)
            group['ranked_anywhere'] += bool(expected) and expected in related
        if expected and (rank is None or rank > 5):
            misses.append({**case, 'rank': rank, 'top_5': names[:5], 'related_top': related[:5]})
        snapshots.append({**case, 'rank': rank, 'top_5': names[:5]})
    ordered = sorted(timings)
    return {'groups': dict(groups), 'misses': misses, 'snapshots': snapshots,
            'latency_ms': {'median': round(statistics.median(timings), 2),
                           'p95': round(ordered[int(.95*(len(ordered)-1))], 2),
                           'max': round(max(timings), 2)}}


def evaluate_reference_grounding():
    """Offline anti-hallucination cases for literal game-object references."""
    files = {
        'common/buildings/00_eval.txt': 'building_eval_lab = { base_buildtime = 120 }\n',
        'common/technology/00_eval.txt': 'tech_eval_power = { area = physics tier = 1 }\n',
    }
    cases = [
        {'name': 'known_building', 'code': 'has_building = building_eval_lab',
         'context': {'type': 'trigger', 'scope': 'planet'},
         'status': 'CONFIRMED_GAME_DATA', 'finding': 'OBJECT_REFERENCE'},
        {'name': 'misspelled_building', 'code': 'has_building = building_eval_lba',
         'context': {'type': 'trigger', 'scope': 'planet'},
         'status': 'UNKNOWN', 'finding': 'UNKNOWN_OBJECT_REFERENCE',
         'candidate': 'building_eval_lab'},
        {'name': 'invented_building', 'code': 'has_building = building_quantum_banana',
         'context': {'type': 'trigger', 'scope': 'planet'},
         'status': 'UNKNOWN', 'finding': 'UNKNOWN_OBJECT_REFERENCE'},
        {'name': 'known_technology', 'code': 'has_technology = tech_eval_power',
         'context': {'type': 'trigger', 'scope': 'country'},
         'status': 'CONFIRMED_GAME_DATA', 'finding': 'OBJECT_REFERENCE'},
        {'name': 'misspelled_technology', 'code': 'has_technology = tech_eval_powre',
         'context': {'type': 'trigger', 'scope': 'country'},
         'status': 'UNKNOWN', 'finding': 'UNKNOWN_OBJECT_REFERENCE',
         'candidate': 'tech_eval_power'},
        {'name': 'invented_technology', 'code': 'has_technology = tech_quantum_banana',
         'context': {'type': 'trigger', 'scope': 'country'},
         'status': 'UNKNOWN', 'finding': 'UNKNOWN_OBJECT_REFERENCE'},
    ]
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        for relative, source in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding='utf-8')
        db = Database(game_data=True,
                      environment=Environment(game_root=root, dynamic_ready=True),
                      watch=False)
        rows = []
        for case in cases:
            result = Validator(db).validate(case['code'], case['context'])
            finding = next((row for row in result.get('findings', [])
                            if row['code'] in ('OBJECT_REFERENCE', 'UNKNOWN_OBJECT_REFERENCE')), {})
            candidates = finding.get('candidates', [])
            passed = (result.get('status') == case['status']
                      and finding.get('code') == case['finding']
                      and (not case.get('candidate') or case['candidate'] in candidates))
            rows.append({'name': case['name'], 'passed': passed,
                         'expected_status': case['status'], 'actual_status': result.get('status'),
                         'expected_finding': case['finding'], 'actual_finding': finding.get('code'),
                         'candidates': candidates})
    return {'cases': len(rows), 'passed': sum(row['passed'] for row in rows),
            'failed': sum(not row['passed'] for row in rows), 'results': rows}


def evaluate_one_call_search():
    """MCP search must carry the declaration that formerly required a get call."""
    server = Server(Database())
    server.initialized = True

    def call(name, arguments=None):
        reply = server.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                 'params': {'name': name, 'arguments': arguments or {}}})
        return json.loads(reply['result']['content'][0]['text'])

    exact = call('stellaris_search', {'query': 'has_background_job', 'type': 'trigger'})
    typo = call('stellaris_search', {'query': 'has_backgroud_job', 'type': 'trigger'})
    template = call('stellaris_search', {'query': 'country_resource_max_energy_add',
                                         'type': 'modifier'})
    schema = call('stellaris_search', {'query': 'common/buildings.cwt'})
    default = call('stellaris_search', {'query': 'job'})
    tools = {tool['name'] for tool in server.dispatch(
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})['result']['tools']}
    cases = [
        ('exact_definition', bool(exact.get('results', [{}])[0].get('definitions'))),
        ('suggestion_definition', typo.get('suggestions', [{}])[0].get('name') == 'has_background_job'
         and bool(typo['suggestions'][0].get('definitions'))),
        ('template_definition', bool(template.get('templates', [{}])[0].get('definitions'))),
        ('schema_via_search', schema.get('status') == 'CONFIRMED_CWT'
         and 'base_buildtime' in schema.get('content', '')),
        ('default_five', sum(len(default.get(key, ()))
                             for key in ('results', 'templates', 'suggestions')) <= 5),
        ('get_not_advertised', 'stellaris_get' not in tools and len(tools) == 4),
    ]
    rows = [{'name': name, 'passed': passed} for name, passed in cases]
    return {'cases': len(rows), 'passed': sum(row['passed'] for row in rows),
            'failed': sum(not row['passed'] for row in rows), 'results': rows}


def evaluate_usage_feedback():
    """Regression cases reported from real DSH use of text search and validation."""
    files = {
        'common/astral_actions/00_probe.txt': 'usage_probe = early\n',
        'common/scripted_effects/99_probe.txt':
            'usage_probe = one\nusage_probe = two\nusage_probe = three\n',
        'events/99_probe.txt': 'usage_probe = event_one\nusage_probe = event_two\n',
        'common/ship_sizes/00_probe.txt': '''probe_ship = {
    section_slots = {
        "bow" = { locator = "part1" }
        "mid" = { locator = "part2" }
        "stern" = { locator = "part3" }
    }
}
''',
    }
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        for relative, source in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding='utf-8')
        db = Database(game_data=True,
                      environment=Environment(game_root=root, dynamic_ready=True),
                      watch=False)
        text_result = db.search_text('usage_probe', limit=2)
        text_files = {row['file'] for row in text_result.get('rows', ())}
        slot = Validator(db).validate('section = { slot = "mid" }',
                                      {'schema': 'global_ship_design'})
        bare = Validator(db).validate('x = yes', 'scripted_effect')
        explicit = Validator(db).validate('x = yes', {'schema': 'scripted_effect'})
        cases = [
            ('text_cross_directory',
             any('common/scripted_effects/' in path for path in text_files)
             and any('events/' in path for path in text_files)),
            ('text_count_semantics', text_result.get('returned') == 2
             and text_result.get('total') == 6 and text_result.get('has_more') is True
             and text_result.get('scan_complete') is True),
            ('dynamic_section_slot', slot.get('status') == 'CONFIRMED_CWT'
             and not any(row.get('code') == 'VALUE_MISMATCH'
                         for row in slot.get('findings', ()))),
            ('bare_schema_context', bare == explicit),
        ]
    rows = [{'name': name, 'passed': passed} for name, passed in cases]
    return {'cases': len(rows), 'passed': sum(row['passed'] for row in rows),
            'failed': sum(not row['passed'] for row in rows), 'results': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT/'docs/FUZZY_EVALUATION.json')
    parser.add_argument('--sweep', action='store_true', help='Calibrate cutoffs on calibration names only')
    args = parser.parse_args()
    db = Database()
    cases = make_cases(db) + make_permutation_cases(db)
    report = {'dataset': db.dataset, 'seed': 20260919, 'cases': len(cases)}
    if args.sweep:
        from stellaris_agent import index, retrieval
        originals = (retrieval.MIN_SCORE, index.MIN_SCORE)
        sweep_cases = [c for c in cases if c['split'] != 'holdout']
        report['cutoff_sweep'] = {}
        for cutoff in (45.0, 50.0, 55.0, 60.0, 65.0, 70.0):
            retrieval.MIN_SCORE = index.MIN_SCORE = cutoff
            evaluated = evaluate(db, sweep_cases)
            report['cutoff_sweep'][str(cutoff)] = {k: v for k, v in evaluated['groups'].items()
                                                   if '/' not in k}
            print('cutoff', cutoff, report['cutoff_sweep'][str(cutoff)], flush=True)
        retrieval.MIN_SCORE, index.MIN_SCORE = originals
    report['selected_cutoff'] = __import__('stellaris_agent.retrieval', fromlist=['MIN_SCORE']).MIN_SCORE
    report['evaluation'] = evaluate(db, cases)
    report['evaluation']['groups'] = {k: v for k, v in report['evaluation']['groups'].items()}
    report['reference_grounding'] = evaluate_reference_grounding()
    report['one_call_search'] = evaluate_one_call_search()
    report['usage_feedback'] = evaluate_usage_feedback()
    args.output.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({k: v for k, v in report['evaluation'].items() if k not in ('snapshots', 'misses')}, indent=2))
    print('reference_grounding:', json.dumps(report['reference_grounding'], indent=2))
    print('one_call_search:', json.dumps(report['one_call_search'], indent=2))
    print('usage_feedback:', json.dumps(report['usage_feedback'], indent=2))
    print('report:', str(args.output))


if __name__ == '__main__':
    main()
