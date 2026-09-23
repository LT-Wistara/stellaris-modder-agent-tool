"""Skeleton scan of the game and mod script files declared by CWT type rules.

The bundled corpus already says *where* every definition type lives: each
``type[x]`` rule carries ``path`` / ``path_extension`` / ``name_field``.  Scanning
exactly those directories -- instead of the whole game tree -- is what keeps this
step cheap enough to run lazily inside a request.

Read order is ``[game, mod, mod, ...]``: configured Mod folders are all scanned
after the game. Later folders win when they define the same name.

Only the members a consumer actually needs are extracted (see ``EXTRACTORS``);
the parsed document is dropped afterwards so the index stays small.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
import math
import os
import re
import time

from .parser import parse_file, values

# A script identifier: ``scripted_effect``, ``event.x.1``, ``country_event``. A leading
# digit is never part of one, which is what keeps bare numbers out of the call index.
IDENTIFIER_RE = re.compile(r'[A-Za-z_][\w.:]*')

MAX_FILES = 20000
MAX_SECONDS = 20.0
DEFAULT_EXTENSION = '.txt'
# Members kept per definition type. Anything absent keeps only its name.
ECONOMIC_MEMBERS = (
    'modifier_category', 'parent', 'use_for_ai_budget',
    'generate_add_modifiers', 'generate_mult_modifiers',
)
TRIGGERED_MEMBERS = (
    'triggered_cost_modifier', 'triggered_produces_modifier',
    'triggered_upkeep_modifier', 'triggered_logistics_modifier',
)
DEFAULT_MODIFIER_CATEGORY = 'economic_unit'


@dataclass
class DirRule:
    directory: str                  # relative directory, 'game/' prefix removed
    extension: str = DEFAULT_EXTENSION
    file_stem: str | None = None
    name_field: str | None = None
    name_from_file: bool = False
    type_per_file: bool = False
    skip_root: tuple = ()
    include: frozenset = frozenset()
    exclude: frozenset = frozenset()
    starts_with: tuple = ()
    prefix: str = ''


@dataclass
class Definition:
    name: str
    type: str
    source: str                     # 'game' | 'mod'
    rel_path: str                   # relative to the entry root
    abs_path: str
    line: int
    start: int
    end: int
    subtypes: tuple = ()
    data: dict = dataclass_field(default_factory=dict)

    @property
    def id(self):
        return f'{self.source}:{self.rel_path}:{self.line}'

    def as_dict(self):
        return {'name': self.name, 'type': self.type, 'source': self.source,
                'path': self.rel_path, 'line': self.line,
                'subtypes': list(self.subtypes)}

    def read_text(self):
        """Definition text, sliced from disk on demand; never cached in memory.

        Offsets address the source exactly as the parser saw it, so the raw bytes are
        decoded here too: ``Path.read_text`` would translate CRLF and shift every
        offset after the first line.
        """
        try:
            source = Path(self.abs_path).read_bytes().decode('utf-8', errors='replace')
        except OSError:
            return ''
        return source[self.start:self.end]


def _meta_nodes(db, node):
    return db.meta.get(id(node), [])


def _filter_sets(db, node):
    """Include/exclude key filters plus prefixes of one ``type[...]`` rule."""
    include, exclude, starts = set(), set(), []
    for meta in _meta_nodes(db, node):
        if meta.key.casefold() != 'type_key_filter':
            continue
        raw = meta.raw or ''
        target = exclude if '<>' in raw else include
        target.update(value.casefold() for value in values(meta))
        if meta.children:
            target.update(child.key.casefold() for child in meta.children if child.children is None)
    for value in db.annotations(node, 'starts_with'):
        starts.append(value.casefold())
    return include, exclude, tuple(starts)


def dir_rules(db, type_name):
    """Turn every ``type[x]`` declaration of one type into directory rules."""
    rules = []
    for typedef in db.types.get(type_name, []):
        paths = [child.value for child in typedef.children or []
                 if child.key == 'path' and child.value]
        if not paths:
            continue
        extension = next((child.value for child in typedef.children or []
                          if child.key == 'path_extension' and child.value), DEFAULT_EXTENSION)
        if not extension.startswith('.'):
            extension = '.' + extension
        stem = next((child.value for child in typedef.children or []
                     if child.key == 'path_file' and child.value), None)
        name_field = next((child.value for child in typedef.children or []
                           if child.key == 'name_field'), None)
        include, exclude, starts = _filter_sets(db, typedef)
        skip = values(typedef.field('skip_root_key')) if typedef.field('skip_root_key') else []
        if not isinstance(skip, (list, tuple)):
            skip = [skip]
        common = dict(
            extension=extension, file_stem=stem, name_field=name_field,
            name_from_file=bool(typedef.field('name_from_file')),
            type_per_file=bool(typedef.field('type_per_file')),
            skip_root=tuple(str(x).casefold() for x in skip),
            include=frozenset(include), exclude=frozenset(exclude), starts_with=starts,
            prefix=(typedef.field('type_key_prefix').value
                    if typedef.field('type_key_prefix') else '') or '')
        for path in paths:
            directory = path.replace('\\', '/').strip('/')
            if directory.startswith('game/'):
                directory = directory[len('game/'):]
            rules.append(DirRule(directory=directory, **common))
    return rules


def _scalar(node):
    return node.value if node is not None else None


def _presence_set(node, key):
    """Sorted tuple of the block's members: JSON-safe and order-preserving."""
    block = node.field(key)
    if block is None:
        return ()
    result = {value.casefold() for value in values(block)}
    if block.children:
        result.update(child.key.casefold() for child in block.children if child.children is None)
    return tuple(sorted(name for name in result if name))


def _triggered(node, key):
    result = []
    for block in [child for child in node.children or [] if child.key == key]:
        entry = {'key': (block.field('key').value if block.field('key') else None),
                 'modifier_types': _presence_set(block, 'modifier_types'),
                 'use_parent_icon': bool(block.field('use_parent_icon'))}
        if entry['key']:
            result.append(entry)
    return result


def extract(db, type_name, node):
    """Members a consumer needs; an unknown type keeps only its name."""
    data = {}
    if type_name == 'economic_category':
        data.update(
            modifier_category=_scalar(node.field('modifier_category')) or DEFAULT_MODIFIER_CATEGORY,
            parent=_scalar(node.field('parent')),
            generate_add_modifiers=_presence_set(node, 'generate_add_modifiers'),
            generate_mult_modifiers=_presence_set(node, 'generate_mult_modifiers'))
        for member in TRIGGERED_MEMBERS:
            data[member] = _triggered(node, member)
    for enum_name, member_path in db.complex_enum_sources.get(type_name, ()):
        target = node
        for key in member_path:
            target = target.field(key) if target is not None else None
        if target is None:
            continue
        members = {value.casefold() for value in values(target) if value}
        members.update(child.key.casefold() for child in target.children or [] if child.key)
        if members:
            data.setdefault('complex_enums', {}).setdefault(enum_name, []).extend(sorted(members))
    return data


TEXT_DIRECTORIES = ('common', 'events', 'history', 'missions', 'interface',
                    'localisation', 'gfx', 'sound', 'map', 'prescripted_countries')
TEXT_EXTENSIONS = frozenset(('.txt', '.gui', '.yml', '.asset', '.gfx', '.sfx'))
TEXT_ROOT_PREFIXES = {'game': 'game:', 'mod': 'mod:'}
TEXT_SNIPPET_LIMIT = 110
MAX_TEXT_LIMIT = 500
MAX_TEXT_SECONDS = 20.0

CALL_DIRECTORIES = ('common', 'events', 'history', 'missions')
CALL_EXTENSIONS = frozenset(('.txt', '.gui'))
MAX_CALL_FILES = 12000
MAX_CALL_SECONDS = 25.0
MAX_CALLEES = 200_000
_SNIPPET_LIMIT = 76


class CallIndex:
    """Where script files reference each identifier.

    Built by a lexical scan rather than by re-parsing every file: the question a modder
    asks is "who calls this name", which needs every *occurrence*, not a syntax tree.
    Entries are stored as ``path -> [lines]`` so one file is spelled once however many
    times it is referenced, and a caller can be handed a bounded list of real call sites
    instead of running its own grep over the game installation.
    """

    def __init__(self):
        self.by_name = {}
        self._snippets = {}

    def positions(self, name):
        return list(self.by_name.get((name or '').casefold()) or ())

    def snippet(self, path, line):
        """The text of one call site, trimmed: enough to see which field uses the name."""
        key = (path, line)
        cached = self._snippets.get(key)
        if cached is not None:
            return cached
        text = ''
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as stream:
                for number, raw in enumerate(stream, 1):
                    if number == line:
                        text = raw.strip()
                        break
        except OSError:
            text = ''
        if len(text) > _SNIPPET_LIMIT:
            text = text[:_SNIPPET_LIMIT - 1].rstrip() + '…'
        if len(self._snippets) > 4096:
            self._snippets.clear()
        self._snippets[key] = text
        return text

    def summary(self, name, limit=3):
        """Call sites of one identifier, capped, in scan order (game before mod)."""
        positions = self.positions(name)
        return [{'file': path, 'line': line, 'text': self.snippet(path, line)}
                for path, line in positions[:limit]]


def build_call_index(environment, max_files=MAX_CALL_FILES, max_seconds=MAX_CALL_SECONDS):
    """Scan script files for identifier occurrences; ``None`` when there is no source."""
    entries = environment.entries() if environment is not None else []
    if not entries:
        return None
    index = CallIndex()
    started = time.perf_counter()
    scanned = 0
    for _source, root in entries:
        for directory in CALL_DIRECTORIES:
            base = Path(root) / directory
            if not base.is_dir():
                continue
            for path in base.rglob('*'):
                if path.suffix.casefold() not in CALL_EXTENSIONS or not path.is_file():
                    continue
                if scanned >= max_files or time.perf_counter() - started > max_seconds:
                    return index
                scanned += 1
                try:
                    with open(path, 'r', encoding='utf-8', errors='replace') as stream:
                        for number, raw in enumerate(stream, 1):
                            line = raw.rstrip('\r\n')
                            if not line or '=' not in line:
                                continue
                            cut = line.find('#')
                            if cut >= 0:
                                line = line[:cut]
                            for match in IDENTIFIER_RE.finditer(line):
                                token = match.group(0).rstrip('.')
                                if not token or '_' not in token:
                                    continue
                                index.by_name.setdefault(token.casefold(), []).append(
                                    (str(path), number))
                                if len(index.by_name) > MAX_CALLEES:
                                    return index
                except OSError:
                    continue
    return index


LOCALISATION_LANGUAGES = ('simp_chinese', 'english')
_LOCALISATION_LIMIT = 200
_LOCALISATION_VALUE_LIMIT = 200_000
_LOCALISATION_QUOTE_RE = re.compile(r'''
    ^\s*([A-Za-z0-9_.\-]+)\s*:\s*(?:\d+\s*)?
    (?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')
    ''', re.VERBOSE)
_LOCALISATION_MARKUP_RE = re.compile(r'[£$@#][A-Za-z0-9_.]*\$?')


def _localisation_language(path):
    """Which language an ``_l_<lang>.yml`` file belongs to, if it is indexed at all."""
    stem = path.stem.casefold()
    for language in LOCALISATION_LANGUAGES:
        if stem.endswith('_l_' + language):
            return language
    return None


def _read_localisation(path):
    """Yield ``(key, value)`` of one yml file, or nothing when it cannot be read."""
    try:
        with open(path, 'r', encoding='utf-8-sig', errors='replace') as stream:
            for raw in stream:
                line = raw.strip()
                if not line or line.startswith('#'):
                    continue
                match = _LOCALISATION_QUOTE_RE.match(raw)
                if match is None:
                    continue
                value = match.group(2) if match.group(2) is not None else match.group(3)
                if not value:
                    continue
                # ``$key$`` / ``£icon£`` are references and icons, not display text.
                cleaned = _LOCALISATION_MARKUP_RE.sub('', value).strip()
                if cleaned:
                    yield match.group(1), cleaned
    except OSError:
        return


class LocalisationIndex:
    """Display name <-> localisation key, per language.

    A modder thinks in the language the game shows, but every tool takes identifiers.
    The bridge is the localisation key, which for game objects is the object name itself
    (``paladin_ship: "圣武士舰船"``), so a value lookup answers "which identifier is called
    this". Kept as two maps per language so neither lookup degrades into a scan.
    """

    def __init__(self):
        self.by_value = {}      # language -> display text -> [localisation keys]
        self.by_key = {}        # language -> localisation key -> display text
        self.stats = {}

    def values(self, text):
        """Localisation keys whose display text is exactly ``text``, Chinese first."""
        folded = (text or '').strip().casefold()
        if not folded:
            return []
        found = []
        for language in LOCALISATION_LANGUAGES:
            for key in (self.by_value.get(language) or {}).get(folded, ()):
                if key not in found:
                    found.append(key)
        return found[:_LOCALISATION_LIMIT]

    def display(self, key):
        """``(value, language)`` pairs of one key, Chinese first."""
        folded = (key or '').strip().casefold()
        if not folded:
            return []
        result = []
        for language in LOCALISATION_LANGUAGES:
            value = (self.by_key.get(language) or {}).get(folded)
            if value:
                result.append((value, language))
        return result

    def containing(self, value, limit=6):
        """Keys whose display text contains ``value``; a fallback for partial names."""
        folded = (value or '').strip().casefold()
        if not folded:
            return []
        result = []
        for language in LOCALISATION_LANGUAGES:
            for text, keys in (self.by_value.get(language) or {}).items():
                if folded in text:
                    for key in keys:
                        if key not in result:
                            result.append(key)
                            if len(result) >= limit:
                                return result
        return result

def build_localisation_index(environment, max_seconds=20.0):
    """Index the display names of the detected game/mod sources."""
    entries = environment.entries() if environment is not None else []
    if not entries:
        return None
    index = LocalisationIndex()
    started = time.perf_counter()
    for _source, root in entries:
        base = Path(root) / 'localisation'
        if not base.is_dir():
            continue
        for path in base.rglob('*.yml'):
            if not path.is_file():
                continue
            if time.perf_counter() - started > max_seconds:
                break
            language = _localisation_language(path)
            if language is None:
                continue
            by_value = index.by_value.setdefault(language, {})
            by_key = index.by_key.setdefault(language, {})
            for key, value in _read_localisation(path):
                folded = value.casefold()
                keys = by_value.setdefault(folded, [])
                if len(keys) < _LOCALISATION_LIMIT:
                    keys.append(key)
                by_key.setdefault(key.casefold(), value)
                if len(by_value) > _LOCALISATION_VALUE_LIMIT:
                    break
    index.stats = {language: len(table) for language, table in index.by_value.items()}
    return index


def search_text(environment, query, directories=None, limit=20, max_seconds=MAX_TEXT_SECONDS):
    """Literal text search across the detected game/mod sources.

    Answers "where is this string written" without leaving the tool: every hit carries
    its root (``game:``/``mod:``) and path, so provenance is never in doubt, and the
    scan is confined to the detected sources instead of an open filesystem walk.
    """
    term = (query or '').strip()
    if not term or len(term) > 256:
        return {'status': 'NOT_FOUND', 'query': query, 'rows': [], 'returned': 0,
                'has_more': False, 'scan_complete': True, 'total': 0, 'truncated': False,
                'note': 'query must contain 1..256 characters'}
    try:
        limit = max(1, min(int(limit), MAX_TEXT_LIMIT))
    except (TypeError, ValueError):
        limit = 20
    folded = term.casefold()
    wanted = tuple(d.strip('/ ') for d in (directories or ()) if d and d.strip('/ ')) or TEXT_DIRECTORIES
    entries = environment.entries() if environment is not None else []
    if not entries:
        return {'status': 'NOT_FOUND', 'query': query, 'rows': [], 'returned': 0,
                'has_more': False, 'scan_complete': False, 'matched_at_least': 0,
                'truncated': True,
                'note': 'No game or mod data source was detected.'}
    started = time.perf_counter()

    # Inventory by parent directory, then scan one file from every directory per
    # round.  Even if the time budget expires, alphabetically early folders cannot
    # consume the whole request before scripted_effects/events are inspected.
    file_groups = {}
    for source, root in entries:
        prefix = TEXT_ROOT_PREFIXES.get(source, source + ':')
        for directory in wanted:
            base = Path(root) / directory
            if not base.is_dir():
                continue
            for path in base.rglob('*'):
                if path.suffix.casefold() not in TEXT_EXTENSIONS or not path.is_file():
                    continue
                relative = str(path.relative_to(root)).replace('\\', '/')
                group = (source, relative.rpartition('/')[0])
                file_groups.setdefault(group, []).append((path, prefix, relative))
    active = [(group, sorted(paths, key=lambda row: row[2].casefold()), 0)
              for group, paths in sorted(file_groups.items(), key=lambda row: row[0])]
    scan_order = []
    while active:
        next_round = []
        for group, paths, cursor in active:
            scan_order.append((group, *paths[cursor]))
            if cursor + 1 < len(paths):
                next_round.append((group, paths, cursor + 1))
        active = next_round

    group_stats = {}
    matched_at_least, scan_complete = 0, True
    for group, path, prefix, relative in scan_order:
        if time.perf_counter() - started > max_seconds:
            scan_complete = False
            break
        hits, lines = 0, 0
        try:
            with open(path, 'r', encoding='utf-8-sig', errors='replace') as stream:
                for number, raw in enumerate(stream, 1):
                    lines = number
                    if folded in raw.casefold():
                        hits += 1
                    if number % 256 == 0 and time.perf_counter() - started > max_seconds:
                        scan_complete = False
                        break
        except OSError:
            scan_complete = False
            continue
        stats = group_stats.setdefault(group, {'hits': 0, 'lines': 0, 'files': []})
        stats['hits'] += hits
        stats['lines'] += lines
        if hits:
            matched_at_least += hits
            record = {'path': path, 'prefix': prefix, 'relative': relative,
                      'hits': hits, 'lines': lines, 'group': group}
            stats['files'].append(record)
        if not scan_complete:
            break

    # Rank directories by a density-aware score, then take files round-robin across
    # those directories.  Result rows are round-robin too, so one large file cannot
    # monopolise the response.
    ranked_groups = sorted(
        (stats for stats in group_stats.values() if stats['hits']),
        key=lambda stats: (-(stats['hits'] / math.sqrt(max(stats['lines'], 1))),
                           -stats['hits']))
    for stats in ranked_groups:
        stats['files'].sort(key=lambda row: (-(row['hits'] / math.sqrt(max(row['lines'], 1))),
                                             -row['hits'], row['relative'].casefold()))
    selected_files = []
    cursor = 0
    while len(selected_files) < limit:
        added = False
        for stats in ranked_groups:
            if cursor < len(stats['files']):
                selected_files.append(stats['files'][cursor])
                added = True
                if len(selected_files) >= limit:
                    break
        if not added:
            break
        cursor += 1

    per_file_rows = []
    for record in selected_files:
        hits = []
        try:
            with open(record['path'], 'r', encoding='utf-8-sig', errors='replace') as stream:
                for number, raw in enumerate(stream, 1):
                    if folded not in raw.casefold():
                        continue
                    text = raw.strip()
                    if len(text) > TEXT_SNIPPET_LIMIT:
                        text = text[:TEXT_SNIPPET_LIMIT - 1].rstrip() + '…'
                    hits.append({'file': record['prefix'] + record['relative'],
                                 'line': number, 'text': text})
                    if len(hits) >= limit:
                        break
        except OSError:
            scan_complete = False
        if hits:
            per_file_rows.append(hits)

    rows, cursor = [], 0
    while len(rows) < limit:
        added = False
        for hits in per_file_rows:
            if cursor < len(hits):
                rows.append(hits[cursor])
                added = True
                if len(rows) >= limit:
                    break
        if not added:
            break
        cursor += 1

    returned = len(rows)
    has_more = matched_at_least > returned
    common = {'query': query, 'rows': rows, 'returned': returned,
              'has_more': has_more, 'scan_complete': scan_complete,
              # Compatibility for existing clients; unlike the old probe counter this
              # now means either undisplayed confirmed hits or an incomplete scan.
              'truncated': has_more or not scan_complete}
    if scan_complete:
        common['total'] = matched_at_least
    else:
        common['matched_at_least'] = matched_at_least
    if not rows:
        note = ('No line in the loaded sources contains this text.' if scan_complete else
                'No match was found before the bounded scan ended; results are incomplete.')
        return {'status': 'NOT_FOUND' if scan_complete else 'UNKNOWN', **common, 'note': note}
    return {'status': 'CONFIRMED_CWT', **common}


def _content_matches(node, body, depth=0):
    """Shallow content match used for subtype conditions; conservative on purpose."""
    if depth > 1:
        return False
    for rule in body:
        if rule.children is None:
            if rule.operator:
                actual = node.field(rule.key)
                if actual is None:
                    return False
                expected = (rule.value or '').casefold()
                if expected == 'yes':
                    continue
                if expected == 'no':
                    return False
                if (actual.value or '').casefold() != expected:
                    return False
            elif node.field(rule.key) is None:
                return False
        else:
            nested = node.field(rule.key)
            if nested is None:
                return False
            if rule.children and not _content_matches(nested, rule.children, depth + 1):
                return False
    return True


def _subtypes(db, typedef, node):
    matched = []
    for sub in typedef.children or []:
        key = sub.key or ''
        if not key.startswith('subtype['):
            continue
        name = key[len('subtype['):-1]
        include, exclude, starts = _filter_sets(db, sub)
        key_folded = (node.key or '').casefold()
        if include and key_folded not in include:
            continue
        if exclude and key_folded in exclude:
            continue
        if starts and not any(key_folded.startswith(prefix) for prefix in starts):
            continue
        body = [child for child in sub.children or []]
        if body and not _content_matches(node, body):
            continue
        matched.append(name)
    return tuple(matched)


def _accepts(rule, key):
    folded = key.casefold()
    if rule.include and folded not in rule.include:
        return False
    if folded in rule.exclude:
        return False
    if rule.starts_with and not any(folded.startswith(prefix) for prefix in rule.starts_with):
        return False
    if rule.prefix and not folded.startswith(rule.prefix.casefold()):
        return False
    return True


def needed_types(db, extra=()):
    """Types worth indexing: every placeholder type of a template plus extras.

    Templates decide this, so the scan follows the corpus instead of a hand-written
    list: ``job_<job>_add`` makes ``job`` necessary, nothing else does.
    """
    needed = set(extra) | set(db.complex_enum_sources)
    for symbol in db.templates:
        for kind, part in symbol.template.parts:
            if kind != 'placeholder' or part.startswith('enum['):
                continue
            name = part[1:-1].split('.')[0].strip()
            if name:
                needed.add(name)
    return {name for name in needed if name in db.types}


class DefinitionIndex:
    def __init__(self, definitions=None, stats=None):
        self.by_type = {}
        for definition in definitions or []:
            self.by_type.setdefault(definition.type, {})[definition.name.casefold()] = definition
        self.stats = dict(stats or {})

    def get(self, type_name, name):
        return self.by_type.get(type_name, {}).get((name or '').casefold())

    def names(self, type_name):
        return sorted(definition.name for definition in self.by_type.get(type_name, {}).values())

    def has_type(self, type_name):
        return type_name in self.by_type

    def report(self):
        return {'types': {name: len(entries) for name, entries in sorted(self.by_type.items())},
                **self.stats}


def _scanned_directories(db, environment, types=None):
    """Unique (relative, absolute) definition directories the scan would read."""
    wanted = sorted(types if types is not None else needed_types(db))
    entries = environment.entries() if environment is not None else []
    seen, directories = set(), []
    for type_name in wanted:
        for rule in dir_rules(db, type_name):
            for _source, root in entries:
                key = (str(root), rule.directory)
                if key in seen:
                    continue
                seen.add(key)
                directories.append(Path(root) / rule.directory)
    return directories


def fingerprint(db, environment, types=None):
    """Cheap change marker for the scanned definition directories.

    A modder adds entries while the server is running, so a long-lived process has
    to notice that its snapshot went stale.  The marker is ``(files, bytes, mtime
    sum)`` over the whitelisted directories: a directory walk instead of a parse,
    where an added file changes the count, and an edit changes the timestamp sum
    even when it keeps the file's size (a maximum timestamp would miss edits to any
    file that is not the newest one).
    """
    count, size, mtime_sum = 0, 0, 0
    for directory in _scanned_directories(db, environment, types):
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if not entry.is_file():
                            continue
                        stat = entry.stat()
                    except OSError:
                        continue
                    count += 1
                    size += stat.st_size
                    mtime_sum += stat.st_mtime_ns
        except OSError:
            continue
    return (count, size, mtime_sum)


def build_index(db, environment, types=None, max_files=MAX_FILES, max_seconds=MAX_SECONDS):
    """Scan the game and every configured Mod for requested definition types."""
    started = time.perf_counter()
    entries = environment.entries() if environment is not None else []
    wanted = sorted(types if types is not None else needed_types(db))
    definitions, errors, missing = [], [], []
    scanned_files = 0
    truncated = False
    for type_name in wanted:
        rules = dir_rules(db, type_name)
        if not rules:
            missing.append(type_name)
            continue
        typedefs = db.types.get(type_name, [])
        for rule in rules:
            for source, root in entries:
                directory = Path(root) / rule.directory
                if not directory.is_dir():
                    if source == 'game':
                        missing.append(rule.directory)
                    continue
                try:
                    children = sorted(directory.iterdir())
                except OSError as error:
                    errors.append(f'{rule.directory}: {error}')
                    continue
                for path in children:
                    if not path.is_file():
                        continue
                    if rule.file_stem and path.stem.casefold() != rule.file_stem.casefold():
                        continue
                    if not rule.file_stem and path.suffix.casefold() != rule.extension.casefold():
                        continue
                    if scanned_files >= max_files or time.perf_counter() - started > max_seconds:
                        truncated = True
                        break
                    scanned_files += 1
                    relative = f'{rule.directory}/{path.name}'
                    try:
                        document = parse_file(path, relative)
                    except Exception as error:  # noqa: BLE001 - one bad file never stops the scan
                        errors.append(f'{relative}: {error}')
                        continue
                    for definition in _definitions(db, typedefs, rule, document, source, path, relative, type_name):
                        definitions.append(definition)
                if truncated:
                    break
            if truncated:
                break
        if truncated:
            break
    stats = {'files': scanned_files, 'definitions': len(definitions), 'types_requested': wanted,
             'missing': sorted(set(missing)), 'errors': errors[:20], 'truncated': truncated,
             'seconds': round(time.perf_counter() - started, 3),
             'fingerprint': list(fingerprint(db, environment, wanted)),
             'entries': [(name, str(root)) for name, root in entries]}
    return DefinitionIndex(definitions, stats)


def _definitions(db, typedefs, rule, document, source, path, relative, type_name):
    """Yield definitions of one already parsed file."""
    for typedef in typedefs:
        for node in _candidate_nodes(rule, document):
            if node.children is None and not rule.type_per_file:
                continue
            if not _accepts(rule, node.key or ''):
                continue
            name = _definition_name(rule, node, path)
            if not name:
                continue
            # Slice from the start of the definition's own line: the parser's node
            # offset points at the value side for `key = { ... }` declarations.
            line_start = document.source.rfind('\n', 0, node.start) + 1
            yield Definition(
                name=name, type=type_name, source=source, rel_path=relative,
                abs_path=str(path), line=node.line, start=line_start, end=node.end,
                subtypes=_subtypes(db, typedef, node), data=extract(db, type_name, node))


def _candidate_nodes(rule, document):
    if rule.type_per_file:
        return list(document.nodes)
    if not rule.skip_root:
        return list(document.nodes)
    result = []
    for node in document.nodes:
        if (node.key or '').casefold() in rule.skip_root or 'any' in rule.skip_root:
            result.extend(node.children or [])
            continue
        result.append(node)
    return result


def _definition_name(rule, node, path):
    if rule.name_from_file:
        return path.stem
    if rule.name_field:
        if rule.name_field == '-':
            return node.value
        field = node.field(rule.name_field)
        return field.value if field is not None else None
    return node.key
