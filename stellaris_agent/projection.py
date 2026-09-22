"""Agent-facing views: names, tiers and declaration text, nothing else.

Every byte of every response is charged to the model's context, so the default payload
carries only what the next decision needs:

* ``search`` -> names, reusable types and declaration text, split by evidence tier (an
  exact hit and a spelling guess must never look alike), plus at most one line of guidance;
* ``get`` -> the identifier, the declaration type and the sliced source text;
* ``validate`` -> the findings, with a one-line legend for the statuses that appear.

There is one output shape per tool, so no tool takes a ``detail`` argument.

Scores, coverage and the full status definitions are static or human-facing, and are
already stated once in the MCP ``instructions``; they stay on the payload that the CLI
and the Python API return, which is where a caller goes when it wants them.

No MCP response carries a ``source`` block.  The corpus provenance object
(``file``/``line``/``end_line``/``commit``) and the game-data origin object are internal
evidence: they stay on the ``Database`` payload that the CLI and the Python API return.
A game/mod declaration keeps its single compact ``where`` pointer, which carries the same
location in one short string.

Search also trims itself: only candidates close to the best score survive, and each list
is capped, so one lookup cannot flood a context window.  ``more`` reports what was left out.
"""
from copy import deepcopy

CONFIRMED = ('CONFIRMED_CWT', 'CONFIRMED_GAME_DATA')
EVIDENCE = CONFIRMED + ('TEMPLATE_MATCH',)
LEGEND = {
    'CONFIRMED_CWT': 'declared by the bundled rules',
    'CONFIRMED_GAME_DATA': 'declared by the loaded game/mod data',
    'TEMPLATE_MATCH': 'shape matched, object not proven',
    'SUGGESTION': 'candidate only',
    'UNKNOWN': 'no evidence here, not proof of absence',
    'UNRESOLVED': 'needs context the validator cannot see',
}
NO_GAME_DATA_NOTE = ('game/mod data is not loaded, so names declared by mods cannot be '
                     'confirmed; run stellaris_doctor for the detected sources')
NOT_FOUND_TIP = ('No match in the loaded CWT corpus; the name may be defined by a mod. '
                 'Run stellaris_doctor to see which data sources are loaded.')

def select(value, keys):
    return {key: deepcopy(value[key]) for key in keys if key in value}


def unique(rows):
    seen, result = set(), []
    for row in rows:
        key = (row['name'], row['type'])
        if key not in seen:
            seen.add(key)
            result.append(row)
    return result


def legend_for(statuses):
    return '; '.join(f'{status}={LEGEND[status]}' for status in statuses if status in LEGEND)


def scopes_view(value):
    """Modifier scopes: the resolved list, or what could not be resolved."""
    if not value:
        return None
    if value.get('supported_scopes'):
        return {'scopes': list(value['supported_scopes'])}
    categories = value.get('unresolved_categories')
    return {'unresolved_categories': list(categories)} if categories else None


def definition_rows(payload):
    """Project the reusable part of a get response for embedding in search rows."""
    by_id = {row['id']: row for row in payload.get('definitions', [])}
    rows, index = [], {}
    for row in payload.get('definitions', []):
        owner = by_id.get(row.get('definition_ref'), row)
        text = owner['definition']['text']
        key = (row['type'], text)
        if key in index:
            rows.append({'type': row['type'], 'same_as': index[key]})
            continue
        index[key] = len(rows)
        item = {'type': row['type'], 'text': text}
        scopes = scopes_view(row.get('scope_resolution'))
        if scopes:
            item.update(scopes)
        if row.get('origin'):
            # Where a game/mod declaration lives is not derivable from the corpus, and it
            # tells the modder whether the name is theirs to edit.
            source = row.get('source') or {}
            item['where'] = '%s:%s:%s' % (source.get('source', 'game'), source.get('file'),
                                          source.get('line'))
        for field in ('calls', 'calls_more'):
            if row.get(field):
                item[field] = deepcopy(row[field])
        rows.append(item)
    return rows


def search_view(payload):
    rows = payload.get('results', [])
    total = payload.get('total', len(rows))
    query = payload.get('query')
    confirmed, templates, suggestions = [], [], []
    for row in rows:
        item = {'name': row['name'], 'type': row['type']}
        if row.get('details') is not None:
            item['definitions'] = definition_rows(row['details'])
        if row['status'] in CONFIRMED:
            confirmed.append(item)
        elif row['status'] == 'TEMPLATE_MATCH':
            # The row names the template; what matched is the caller's own concrete name.
            item['name'] = query or row['name']
            templates.append(item)
        else:
            suggestions.append(item)
    confirmed = unique(confirmed)
    confirmed_keys = {(row['name'], row['type']) for row in confirmed}
    templates = unique(row for row in templates
                       if (row['name'], row['type']) not in confirmed_keys)
    template_keys = {(row['name'], row['type']) for row in templates}
    suggestions = unique(row for row in suggestions
                         if (row['name'], row['type']) not in confirmed_keys | template_keys)
    result = {'status': payload['status']}
    if confirmed:
        result['results'] = confirmed
    if templates:
        result['templates'] = templates
    if suggestions:
        result['suggestions'] = suggestions
    hidden = total - sum(len(result.get(key, ())) for key in ('results', 'templates', 'suggestions'))
    if hidden > 0:
        result['more'] = hidden
    if payload['status'] == 'NOT_FOUND':
        result['tips'] = NOT_FOUND_TIP
    elif result.get('suggestions') and (confirmed or templates):
        result['tips'] = 'suggestions are candidates, not confirmed interfaces'
    elif result.get('suggestions'):
        result['tips'] = ('candidates only: each definition belongs to the suggested spelling, '
                          'not to the query')
    elif templates:
        result['tips'] = 'the documented shape matched; whether the object exists is not established'
    return result


def definition_view(payload):
    result = {'status': payload['status'], 'name': payload['name']}
    result['definitions'] = definition_rows(payload)
    if payload['status'] == 'NOT_FOUND':
        result['tips'] = NOT_FOUND_TIP
    return result


def finding_view(row):
    item = select(row, ('status', 'code', 'identifier', 'line', 'message',
                        'expected', 'scope', 'supported_scopes', 'candidates', 'field',
                        'object_type'))
    if row.get('end_line') not in (None, row.get('line')):
        item['end_line'] = row['end_line']
    if row.get('suggestions'):
        item['suggestions'] = [suggestion['name'] for suggestion in row['suggestions']]
    scopes = scopes_view(row.get('scope_resolution'))
    if scopes:
        item.update(scopes)
    return item


def validation_view(payload):
    findings = [finding_view(row) for row in payload['findings']
                if row['status'] != 'CONFIRMED_CWT']
    result = {'status': payload['status'], 'findings': findings}
    if payload.get('counts'):
        result['counts'] = deepcopy(payload['counts'])
    legend = legend_for(sorted({row['status'] for row in findings}))
    if legend:
        result['legend'] = legend
    if not (payload.get('coverage') or {}).get('runtime_objects'):
        result['note'] = NO_GAME_DATA_NOTE
    return result


def project(tool, payload):
    """Return a detached compact response, including early errors and file reads."""
    if tool == 'stellaris_search' and 'results' in payload:
        return search_view(payload)
    if tool in ('stellaris_get', 'stellaris_get_definition') and 'definitions' in payload:
        return definition_view(payload)
    if tool == 'stellaris_validate' and 'findings' in payload:
        return validation_view(payload)
    if tool == 'stellaris_search_text':
        return select(payload, ('status', 'query', 'rows', 'returned', 'has_more',
                                'scan_complete', 'total', 'matched_at_least',
                                'truncated', 'note'))
    if tool == 'stellaris_doctor':
        return select(payload, ('status', 'game_data_enabled', 'enabled', 'reason', 'seconds',
                                'truncated', 'watch', 'sources', 'environment', 'index', 'generated_modifiers',
                                'version_check', 'note'))
    return select(payload, ('status', 'code', 'message', 'type', 'scope', 'candidates',
                            'kind', 'path', 'content', 'files', 'available', 'hint'))
