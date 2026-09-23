from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
import json
import re
import sys
import threading
import time

from . import dynamic as dynamic_module
from . import gamedata
from .parser import Node, parse, parse_file, metadata, meta_values, values
from .scoring import FULL_TEMPLATE_SCORE, MAX_SUGGESTION_SCORE, MIN_QUERY_SUPPORT, MIN_SCORE
from .templates import Template
from .retrieval import Retrieval

DATA = Path(__file__).resolve().parent / 'data'
NOT_FOUND = 'No matching declaration in the loaded rules and data.'
SUGGESTION_NOTE = 'Search candidates do not establish validity in a script context.'
RELATED_NOTE = ('Some names only share tokens with the query; they are browse context, '
                'not suggestions, and are listed separately.')
RELATED_LIMIT = 40
RELATED_MIN_SCORE = 25.0           # below this a name is not even useful browse context
RELATED_MIN_COVERAGE = 0.35        # related rows must explain a real share of the name
MAX_FILE_LINES = 800
ECONOMIC_MODIFIER_RE = re.compile(
    r'^([a-z0-9][a-z0-9_.-]*)_(cost|produces|upkeep|logistics)_(mult|add)$', re.I)
ECONOMIC_MODIFIER_DEFINITION = '''# Derived from the economic-category generator rules documented in modifiers.cwt:582-598.
# A structural match does not prove that the economic category/resource exists or that
# its detailed declaration enables this category/type combination.
<economic_category>_<resource>_enum[economic_modifier_category]_enum[economic_modifier_type] = { "AI Economy" }
# Resource-less form is generated for enabled _mult modifiers.
<economic_category>_enum[economic_modifier_category]_enum[economic_modifier_type] = { "AI Economy" }'''
REGISTRY_CONTAINERS = frozenset({
    'types', 'enums', 'complex_enums', 'values', 'modifiers', 'modifier_categories',
    'scopes', 'scope_groups', 'links', 'localisation_links', 'localisation_promotions',
    'localisation_commands', 'on_actions', 'game_rules', 'database_object_types', 'priorities',
})


def declaration(key):
    """Split ``kind[body]`` declarations, e.g. ``alias[trigger:x]``, ``complex_enum[y]``.

    The body may be empty (``enum_name = { }`` style placeholders are not declarations)
    and the kind may carry a qualifier prefix such as ``complex_``; both forms are kept
    verbatim so callers can decide what they support instead of silently treating an
    unrecognized construct as a literal value.
    """
    match = re.fullmatch(r'([a-zA-Z_]+)\[(.*)\]', key)
    return (match.group(1), match.group(2)) if match else (None, None)


def wants_localisation(query):
    """Whether a query should be looked up as a display name.

    The localisation index costs a second to build, so it is only paid for a query that
    could not be an identifier in the first place: identifiers are ASCII, display names
    frequently are not. This keeps English/ASCII searches exactly as cheap as before.
    """
    return any(ord(character) > 127 for character in (query or ''))


def identity(row):
    """Stable identity of one search candidate: the identifier and its canonical type.

    Used to decide tier membership and duplicates. Never compare whole result dicts:
    the same candidate is represented by rows carrying different annotation fields
    (`definition_count`, `definition_ids`, tier-specific keys), so equality would report
    two rows of one candidate as different candidates.
    """
    return (row['name'], row['type'])


@dataclass
class Symbol:
    name: str
    kind: str
    canonical_type: str
    node: Node
    path: str
    template: Template
    derived_rule: str | None = None
    derived_definition: str | None = None
    derived_source_line: int | None = None

    @property
    def id(self):
        return f'{self.node.document.path}:{self.node.line}:{self.node.start}'


class Database:
    def __init__(self, config=None, game_data=False, environment=None, watch=True):
        started = time.perf_counter()
        self.config = Path(config) if config else DATA / 'config'
        # Game/mod data is opt-in and lazily indexed: the library default stays
        # CWT-only and hermetic, while the server and CLI enable it explicitly.
        self.game_data = bool(game_data)
        # Watching keeps a long-running server current while the user edits their mod.
        self.watch = bool(watch) and self.game_data
        self.environment = environment
        self._dynamic_registry = None
        self._retrieval_extended = False
        # Guard the lazy game/mod registry: the HTTP server answers on several
        # threads, so two simultaneous first requests must not both build it.
        self._dynamic_lock = threading.Lock()
        files = sorted(self.config.rglob('*.cwt'))
        if not files:
            raise ValueError(f'No CWT files in {self.config}')
        self.documents = {}
        self.symbols = []
        self.by_name = defaultdict(list)
        self.by_id = {}
        self.by_node = {}
        self.by_file = defaultdict(list)
        self.aliases = defaultdict(list)
        self.singles = defaultdict(list)
        self.schemas = defaultdict(list)
        self.types = defaultdict(list)
        self.enums = defaultdict(list)
        self.complex_enums = defaultdict(list)
        self.complex_enum_sources = defaultdict(list)
        self.categories = defaultdict(list)
        self.scope_aliases = {}
        self.scope_parents = {}
        self.links = defaultdict(list)
        self.meta = {}
        self.metadata_inventory = Counter()
        self.derived_templates = []
        for file in files:
            relative = file.relative_to(self.config).as_posix()
            doc = parse_file(file, relative)
            self.documents[relative] = doc
            canonical = relative[:-4]

            def visit(node, ancestors):
                decl, body = declaration(node.key)
                name, kind = node.key, 'field' if ancestors else 'schema'
                if decl:
                    name, kind = body, decl
                    if decl == 'alias' and ':' in body:
                        kind, name = body.split(':', 1)
                elif ancestors == ['modifiers']:
                    kind = 'modifier'
                elif node.operator is None:
                    kind = 'value'
                symbol = Symbol(name, kind, canonical, node, '/'.join(ancestors + [node.key]), Template(name))
                self.symbols.append(symbol)
                self.by_name[name.casefold()].append(symbol)
                if name != node.key:
                    self.by_name[node.key.casefold()].append(symbol)
                self.by_id[symbol.id] = symbol
                self.by_node[id(node)] = symbol
                self.by_file[canonical].append(symbol)
                self.meta[id(node)] = metadata(node)
                self.metadata_inventory.update(m.key for m in self.meta[id(node)])
                if decl == 'alias':
                    self.aliases[kind].append(symbol)
                if decl == 'single_alias':
                    self.singles[name].append(node)
                if decl == 'type':
                    self.types[name].append(node)
                # Bare ``enum[x]`` nodes inside a schema are value references, not
                # declarations.  Registering those empty leaves made every value fail
                # the vacuous ``all([])`` membership check.
                if decl == 'enum' and node.operator is not None and node.children is not None:
                    self.enums[name].append(node)
                if decl == 'complex_enum' and node.operator is not None and node.children is not None:
                    self.complex_enums[name].append(node)
                if not ancestors and not decl and node.children is not None:
                    self.schemas[node.key].append(node)
                if ancestors == ['modifier_categories']:
                    self.categories[node.key.casefold()].append(node)
                if ancestors == ['links']:
                    self.links[node.key.casefold()].append(node)
                if ancestors == ['scopes']:
                    aliases = values(node.field('aliases'))
                    for alias in aliases + [node.key]:
                        self.scope_aliases[alias.casefold()] = aliases[0] if aliases else node.key.casefold()
                    if node.field('is_subscope_of'):
                        for alias in aliases:
                            self.scope_parents[alias] = node.field('is_subscope_of').value
                for child in node.children or []:
                    visit(child, ancestors + [node.key])

            for node in doc.nodes:
                visit(node, [])
        self._index_complex_enum_sources()
        self._install_derived_templates()
        self.templates = ([s for s in self.symbols if s.template.dynamic and s.kind not in ('value', 'type')]
                          + self.derived_templates)
        manifest = self.config.parent / 'UPSTREAM.json'
        self.upstream = json.loads(manifest.read_text('utf-8')) if manifest.exists() else {'custom_corpus': True}
        self.dataset = {'commit': self.upstream.get('commit_sha')}
        indexed_symbols = self.symbols + self.derived_templates
        self.registry_ids = frozenset(s.id for s in indexed_symbols if self.is_registry_symbol(s))
        self.search_ids = frozenset(s.id for s in indexed_symbols
                                    if s.id in self.registry_ids and self.is_public_identifier(s))
        self.schema_files = frozenset(path for path in self.documents if '/' in path)
        self.file_lines = {path: len(doc.source.splitlines()) for path, doc in self.documents.items()}
        # Keep corpus-calibrated token weights unchanged. Public eligibility is applied
        # before shortlisting/alignment, not after truncating search results.
        self.retrieval = Retrieval(self.symbols)
        self.stats = {'CWT_FILES': len(files), 'PARSED': len(self.documents), 'FAILED': 0,
                      'symbols': len(self.symbols), 'startup_seconds': round(time.perf_counter()-started, 3)}

    def _index_complex_enum_sources(self):
        """Map dynamic enum declarations to the object type and member path they name.

        For example, the corpus declares ``complex_enum[section_slot]`` at
        ``game/common/ship_sizes`` with the path ``name/section_slots/enum_name``.
        The outer ``name`` and terminal ``enum_name`` are metasyntax; a concrete
        ``ship_size`` definition therefore contributes the direct keys inside its
        ``section_slots`` block.
        """
        types_by_path = defaultdict(list)
        for type_name, declarations in self.types.items():
            for node in declarations:
                for child in node.children or []:
                    if child.key != 'path' or not child.value:
                        continue
                    path = child.value.replace('\\', '/').strip('/')
                    if path.startswith('game/'):
                        path = path[len('game/'):]
                    types_by_path[path].append(type_name)

        def member_paths(node, trail=()):
            found = []
            for child in node.children or []:
                if child.key == 'enum_name':
                    found.append(trail)
                elif child.children is not None:
                    found.extend(member_paths(child, trail + (child.key,)))
            return found

        for enum_name, declarations in self.complex_enums.items():
            for node in declarations:
                source = node.field('path')
                shape = node.field('name')
                if source is None or not source.value or shape is None:
                    continue
                path = source.value.replace('\\', '/').strip('/')
                if path.startswith('game/'):
                    path = path[len('game/'):]
                for type_name in types_by_path.get(path, ()):
                    for member_path in member_paths(shape):
                        entry = (enum_name, member_path)
                        if entry not in self.complex_enum_sources[type_name]:
                            self.complex_enum_sources[type_name].append(entry)

    @property
    def dynamic(self):
        """Lazily attached game/mod data registry, refreshed when files change.

        The build takes seconds and must happen once, so it runs under a lock:
        the HTTP server handles requests on several threads and a client that
        calls the very first request after startup would otherwise start a
        second build. Callers that cannot afford the wait (an MCP handshake)
        simply never touch this property until they need it.
        """
        if self._dynamic_registry is None:
            with self._dynamic_lock:
                if self._dynamic_registry is None:
                    self._dynamic_registry = dynamic_module.attach(
                        self, environment=self.environment, enabled=self.game_data, watch=self.watch)
            return self._dynamic_registry
        if not self._dynamic_lock.acquire(blocking=False):
            return self._dynamic_registry  # another thread is already refreshing
        try:
            if self._dynamic_registry.refresh_if_stale():
                # The snapshot changed, so the fuzzy index has to follow it.
                self._retrieval_extended = False
                self.extend_retrieval()
        finally:
            self._dynamic_lock.release()
        return self._dynamic_registry

    def warm_game_data(self):
        """Index game/mod data once.

        Call this when a human is watching (the HTTP startup banner): it reports
        which sources were read. The stdio path deliberately skips it, because a
        client waiting for the handshake treats a multi-second silence as a
        failed process; those requests build the index on first use instead.
        """
        registry = self.dynamic
        registry.enabled  # triggers the lazy build
        self.extend_retrieval()
        self.stats['game_data'] = registry.report()
        return self.stats['game_data']

    def extend_retrieval(self):
        """Fold game/mod names into the fuzzy index.

        Typo tolerance is the point: a name guessed from memory is usually slightly
        wrong, and a modder's own declarations and the game's generated modifiers are
        exactly the names nobody can look up in the corpus. Built lazily, once, and
        only when game/mod data is actually loaded.
        """
        if self._retrieval_extended or not self.game_data or not self.dynamic.built:
            return False
        symbols = self.dynamic.all_symbols()
        if not symbols:
            self._retrieval_extended = True
            return False
        for symbol in symbols:
            self.by_id.setdefault(symbol.id, symbol)
        self.retrieval = Retrieval(self.symbols + symbols)
        self._retrieval_extended = True
        self.stats['retrieval_names'] = self.retrieval.total_names
        return True

    def doctor(self):
        """Report detected data sources and what was indexed from them."""
        report = {'status': 'CONFIRMED_CWT', 'game_data_enabled': self.game_data}
        # Set before the early return below: whether the corpus is behind upstream
        # has nothing to do with whether game data was loaded.
        report['update'] = self._cached_update_status()
        if not self.game_data:
            report.update({'enabled': False,
                           'reason': 'Game/mod data is disabled for this process.'})
            return report
        report.update(self.dynamic.report())
        report['version_check'] = self._version_check(report.get('environment') or {})
        report['sources'] = self._source_summary(report.get('environment') or {}, report)
        report['note'] = ('Read-only report. Game detection checks explicit configuration, '
                          'Steam and default locations. Mod data is read only from explicitly '
                          'configured directories.')
        return report

    @staticmethod
    def _cached_update_status():
        """The last launch-time corpus check, when one is still fresh.

        Read from cache on purpose: ``stellaris_doctor`` is the offline entry
        point and must never wait on the network. ``None`` means nobody has
        checked recently, not that the corpus is current.
        """
        try:
            from .corpus import read_status_cache
            return read_status_cache()
        except Exception:  # noqa: BLE001 - a broken cache must not break the report
            return None

    def _source_summary(self, environment, report):
        """State plainly which sources are usable here, and how to get the missing one."""
        project = 'https://github.com/LT-Wistara/stellaris-modder-agent-tool'
        mod_roots = environment.get('mod_roots') or ([environment['mod_root']] if environment.get('mod_root') else [])
        available = bool(mod_roots)
        index = report.get('index') or {}
        mod = {'root': environment.get('mod_root'), 'available': available,
               'roots': mod_roots,
               'mod_name': environment.get('mod_name'),
               'mod_supported_version': environment.get('mod_supported_version')}
        if not available:
            mod['note'] = ('No Mod source folder was configured for this server. Add Mod directories '
                           'in the GUI or pass --mod-root for each folder to index them.')
            mod['project'] = project
        return {'game': {'root': environment.get('game_root'),
                         'version': environment.get('game_version'),
                         'definitions': index.get('definitions')},
                'mod': mod,
                'corpus': {'target_version': self.upstream.get('stellaris_version'),
                           'project': project},
                'project': project}

    def _version_check(self, environment):
        """Compare corpus, mod and installed versions; drift explains mismatches."""
        def parts(value):
            return tuple(int(x) for x in re.findall(r'\d+', str(value))[:2])

        corpus = self.upstream.get('stellaris_version')
        game = environment.get('game_version')
        supported = environment.get('mod_supported_version')
        warnings = []
        if corpus and game and parts(corpus) and parts(game) and parts(corpus) != parts(game):
            warnings.append(f'The corpus targets Stellaris {corpus} but {game} is installed; '
                            'generated names follow the installed game data.')
        if supported and game and parts(supported) and parts(game) and parts(supported) != parts(game):
            warnings.append(f'This mod declares supported_version="{supported}" while {game} is installed.')
        return {'corpus': corpus, 'game': game, 'mod_supported': supported, 'warnings': warnings}

    @staticmethod
    def is_dynamic(symbol):
        return getattr(symbol, 'dynamic', False)

    def _install_derived_templates(self):
        """Index generator rules documented as prose without changing pinned CWT bytes.

        The upstream corpus describes economic-category modifiers in comments because
        their concrete names depend on game objects.  Represent that documented family
        as one virtual modifier declaration and keep it at TEMPLATE_MATCH evidence.
        """
        source = ('modifiers = {\n\t'
                  '<economic_category>_<resource>_enum[economic_modifier_category]_enum[economic_modifier_type]'
                  ' = { "AI Economy" }\n}')
        node = parse(source, '<derived/economic_modifiers.cwt>').nodes[0].children[0]
        symbol = Symbol(
            node.key, 'modifier', 'modifiers', node, 'modifiers/' + node.key,
            Template(node.key), derived_rule='economic_modifier',
            derived_definition=ECONOMIC_MODIFIER_DEFINITION, derived_source_line=591)
        self.derived_templates.append(symbol)
        self.by_name[symbol.name.casefold()].append(symbol)
        self.by_id[symbol.id] = symbol
        self.by_node[id(node)] = symbol
        self.meta[id(node)] = []

    def annotations(self, node, key):
        annotations = self.meta[id(node)] if id(node) in self.meta else metadata(node)
        return [v for m in annotations if m.key == key for v in values(m)]

    def provenance(self, node):
        symbol = self.by_node.get(id(node))
        if symbol and symbol.derived_source_line:
            return {'file': 'modifiers.cwt', 'line': symbol.derived_source_line,
                    'end_line': 598, 'commit': self.upstream.get('commit_sha'),
                    'derivation': 'economic_category_generator'}
        return {'file': node.document.path, 'line': node.line, 'end_line': node.end_line,
                'commit': self.upstream.get('commit_sha')}

    def resolve_type(self, requested):
        if not requested or requested == 'all':
            return None, None
        q = requested.replace('\\', '/').removesuffix('.cwt').strip('/')
        aliases = {x: x.rstrip('s') for x in ('trigger', 'triggers', 'effect', 'effects', 'modifier', 'modifiers')}
        if q in aliases:
            return ('kind', aliases[q]), None
        if q in self.by_file:
            return ('file', q), None
        choices = sorted(p for p in self.by_file if p.rsplit('/', 1)[-1] == q)
        if len(choices) == 1:
            return ('file', choices[0]), None
        if not choices:
            # A filter that resolves to nothing is the easiest way to lose time: the name
            # usually *is* valid, it is just a schema type rather than a document path
            # (``scripted_effect``), or a registry kind. List the types that actually
            # contain the requested spelling first, so the answer is visible in the
            # candidate list itself instead of only in the hint.
            names = sorted(self.types)
            near = [n for n in names if q and (n.startswith(q) or q.startswith(n))][:12]
            hint = ('Use a bundled CWT path from stellaris_list, a registry kind '
                    '(trigger/effect/modifier), or a schema as '
                    'stellaris_validate context={"schema": <name>}.')
            if q in names:
                hint = (f"'{requested}' is a type[...] rule with no CWT file of its own; pass it "
                        'as stellaris_validate context={"schema": "' + requested + '"}. '
                        'stellaris_search accepts it directly as type=.')
            return None, {'status': 'UNKNOWN_TYPE', 'type': requested, 'candidates': near,
                          'hint': hint}
        return None, {'status': 'AMBIGUOUS_TYPE', 'type': requested, 'candidates': choices}

    @staticmethod
    def accepts(symbol, selected):
        if selected is None:
            return True
        if selected[0] == 'dynamic_type':
            # A definition type only game/mod data declares (scripted_trigger, job, ...):
            # it filters dynamic symbols by their corpus type name.
            return getattr(symbol, 'dynamic', False) and symbol.canonical_type == selected[1]
        return symbol.kind == selected[1] if selected[0] == 'kind' else symbol.canonical_type == selected[1]

    def selection(self, type):
        """Resolve a type filter, falling back to a game/mod data definition type.

        ``scripted_trigger`` and friends are declared by the corpus as ``type[...]``
        rules without a CWT file of their own, so a filter naming them is valid for
        dynamic lookups even though ``resolve_type`` cannot map it to a document.
        """
        selected, error = self.resolve_type(type)
        if error and self.game_data and type:
            name = type.replace('\\', '/').strip('/')
            if name in self.types:
                return ('dynamic_type', name), None
        return selected, error

    @staticmethod
    def is_registry_symbol(symbol):
        if symbol.derived_rule:
            return True
        if '/' in symbol.node.document.path:
            # Explicit global API aliases remain global if a future/custom corpus
            # moves them into a subdirectory. GUI/economic schema aliases do not.
            return (declaration(symbol.node.key)[0] == 'alias'
                    and symbol.kind in ('trigger', 'effect', 'modifier'))
        # A registry's container is not an identifier; don't allow get-by-id to
        # turn a whole modifiers/enums/scopes registry into one giant definition.
        return not (symbol.node.parent is None and symbol.node.key in REGISTRY_CONTAINERS)

    def accepts_registry(self, symbol, selected):
        if self.is_dynamic(symbol):
            return self.accepts(symbol, selected)
        return symbol.id in self.registry_ids and self.accepts(symbol, selected)

    @staticmethod
    def is_public_identifier(symbol):
        """Registry declarations are public; their parameters and values are not.

        Root declarations and immediate entries of registry containers are the
        searchable units. Keep full descendants in the semantic/get indexes.
        Subdirectory eligibility is already restricted to explicit global aliases.
        """
        if symbol.derived_rule:
            return True
        node = symbol.node
        if '/' in node.document.path:
            return Database.is_registry_symbol(symbol)
        return (node.parent is None or
                (node.parent.parent is None and node.parent.key in REGISTRY_CONTAINERS))

    def large_schema_selection(self, selected):
        return (selected is not None and selected[0] == 'file'
                and selected[1] + '.cwt' in self.schema_files
                and self.file_lines[selected[1] + '.cwt'] > MAX_FILE_LINES)

    def accepts_search(self, symbol, selected):
        if self.is_dynamic(symbol):
            # Game/mod declarations are top-level definitions; the corpus-only
            # public-identifier boundary does not apply to them.
            return self.accepts(symbol, selected)
        if self.large_schema_selection(selected):
            return self.accepts(symbol, selected)
        return symbol.id in self.search_ids and self.accepts(symbol, selected)

    def accepts_definition(self, symbol, selected):
        # Direct definition lookup retains fields for existing callers/debugging;
        # only public search imposes the independent-identifier boundary.
        if self.large_schema_selection(selected):
            return self.accepts(symbol, selected)
        return self.accepts_registry(symbol, selected)

    def list_files(self):
        return {'files': sorted(self.documents)}

    @staticmethod
    def schema_hint(path):
        return {'status': 'USE_SCHEMA_FILE', 'path': path,
                'message': 'Use stellaris_list to select a real path, then stellaris_search(query=path) for the complete schema.'}

    def lookup(self, query, selected=None, registry_only=False, dynamic=False):
        accepts = self.accepts_definition if registry_only else self.accepts
        exact = [s for s in self.by_name.get(query.casefold(), []) if accepts(s, selected)]
        if exact:
            return [(s, 'CONFIRMED_CWT', None) for s in exact]
        if dynamic and self.game_data:
            found = [s for s in self.dynamic.symbols(query) if accepts(s, selected)]
            if found:
                return [(s, 'CONFIRMED_GAME_DATA', None) for s in found]
        return [(s, 'TEMPLATE_MATCH', captures) for s in self.templates if accepts(s, selected)
                for captures in [self.template_captures(s, query)] if captures is not None]

    def template_captures(self, symbol, query):
        """Substitutions of one template, verified against game/mod data when loaded."""
        if symbol.derived_rule == 'economic_modifier':
            entry = self.dynamic.modifier(query) if self.game_data else None
            if entry is not None:
                captures = [
                    {'placeholder': '<economic_category>', 'value': entry['economic_category'],
                     'object_existence': 'CONFIRMED',
                     'resolved_from': {'source': entry['source'], 'path': entry['path'],
                                       'line': entry['line']}},
                    {'placeholder': 'enum[economic_modifier_category]', 'value': entry['category'],
                     'object_existence': 'CONFIRMED'},
                    {'placeholder': 'enum[economic_modifier_type]', 'value': entry['modifier_type'],
                     'object_existence': 'CONFIRMED'}]
                if entry.get('resource'):
                    captures.insert(1, {'placeholder': '<resource>', 'value': entry['resource'],
                                        'object_existence': 'CONFIRMED'})
                return captures
            match = ECONOMIC_MODIFIER_RE.fullmatch(query)
            if not match:
                return None
            prefix, category, modifier_type = match.groups()
            captures = [
                {'placeholder': '<economic_category>[_<resource>]', 'value': prefix,
                 'object_existence': 'UNRESOLVED'},
                {'placeholder': 'enum[economic_modifier_category]', 'value': category,
                 'object_existence': 'UNRESOLVED'},
                {'placeholder': 'enum[economic_modifier_type]', 'value': modifier_type,
                 'object_existence': 'UNRESOLVED'},
            ]
            return self.dynamic.verify_captures(captures) if self.game_data else captures
        captures = symbol.template.exact(query)
        if captures is None:
            return None
        return self.dynamic.verify_captures(captures) if self.game_data else captures

    def modifier_scopes(self, symbol):
        if getattr(symbol, 'node', None) is None:
            return None  # game/mod data symbols carry their scopes in the payload
        if symbol.node.parent is None or symbol.node.parent.key != 'modifiers':
            return None
        categories = values(symbol.node)
        scopes, unresolved, origins = [], [], []
        for category in categories:
            nodes = self.categories.get(category.casefold(), [])
            if not nodes or any(n.field('supported_scopes') is None for n in nodes):
                unresolved.append(category)
            for node in nodes:
                scopes.extend(values(node.field('supported_scopes')))
                origins.append(self.provenance(node))
        scopes = list(dict.fromkeys(scopes))
        return {'status': 'UNRESOLVED' if unresolved else 'CONFIRMED_CWT',
                'code': 'UNRESOLVED_MODIFIER_CATEGORY' if unresolved else 'RESOLVED_MODIFIER_CATEGORY',
                'categories': categories, 'supported_scopes': scopes, 'unresolved_categories': unresolved,
                'resolved': None if unresolved else f'{symbol.name} = {{ {" ".join(scopes)} }}',
                'category_sources': origins}

    def source(self, symbol):
        if self.is_dynamic(symbol):
            return symbol.defined_text()
        if symbol.derived_definition is not None:
            return {'text': symbol.derived_definition,
                    'start_line': symbol.derived_source_line,
                    'end_line': 598}
        node = symbol.node
        source = node.document.source
        start, end = node.start, node.end
        if symbol.kind in ('trigger', 'effect') and declaration(node.key)[0] == 'alias':
            siblings = node.parent.children if node.parent else node.document.nodes
            index = next(i for i, x in enumerate(siblings) if x is node)
            first, last = index, index
            def blank_gap(left, right):
                return bool(re.search(r'\n[\t \r]*\n', source[left.end:right.start]))
            while first and not blank_gap(siblings[first-1], siblings[first]):
                first -= 1
            while last+1 < len(siblings) and not blank_gap(siblings[last], siblings[last+1]):
                last += 1
            start, end = siblings[first].start, siblings[last].end
        # Pull contiguous leading comments up to the previous blank line.
        line_start = source.rfind('\n', 0, start) + 1
        start = line_start
        while start > 0:
            previous = source.rfind('\n', 0, max(0, start-1)) + 1
            text = source[previous:start].strip()
            if not text.startswith('#'):
                break
            start = previous
        line_end = source.find('\n', end)
        if line_end >= 0 and source[end:line_end].strip().startswith('#'):
            end = line_end
        return {'text': source[start:end], 'start_line': source.count('\n', 0, start)+1,
                'end_line': source.count('\n', 0, end)+1}

    def result(self, symbol, status, captures=None, full=False):
        if self.is_dynamic(symbol):
            result = {'status': status, 'name': symbol.name, 'kind': symbol.kind,
                      'type': symbol.canonical_type, 'id': symbol.id, 'path': symbol.path,
                      'source': dict(symbol.origin), 'origin': 'game/mod data'}
            if symbol.generated is not None:
                entry = symbol.generated
                result['derivation'] = ('Generated by the economic-category mechanic from game/mod '
                                        'data; the CWT corpus only documents the rule.')
                result['generated_from'] = {key: entry.get(key) for key in
                                            ('economic_category', 'resource', 'category',
                                             'modifier_type', 'triggered')}
                scopes = entry.get('supported_scopes') or []
                if scopes:
                    result['scope_resolution'] = {
                        'status': 'CONFIRMED_CWT', 'code': 'RESOLVED_MODIFIER_CATEGORY',
                        'categories': [entry.get('modifier_category')], 'supported_scopes': scopes,
                        'unresolved_categories': [],
                        'resolved': f'{symbol.name} = {{ {" ".join(scopes)} }}',
                        'category_sources': []}
            if full:
                result['definition'] = self.source(symbol)
                result['annotations'] = []
                result['fields'] = []
            return result
        result = {'status': status, 'name': symbol.name, 'kind': symbol.kind,
                  'type': symbol.canonical_type, 'id': symbol.id, 'path': symbol.path,
                  'source': self.provenance(symbol.node)}
        if captures is not None:
            result.update(matched_template=symbol.name, substitutions=captures,
                          note='Template structure matches; referenced game/mod objects are not loaded.')
        if symbol.derived_rule:
            result['derivation'] = ('Documented economic-category generator rule; detailed game object '
                                    'declarations are not loaded, so this remains TEMPLATE_MATCH.')
        if full:
            result['raw'] = symbol.node.raw
            result['definition'] = self.source(symbol)
            result['annotations'] = [{'key': m.key, 'operator': m.operator, 'values': values(m)} for m in self.meta[id(symbol.node)]]
            result['fields'] = [{'key': c.key, 'operator': c.operator, 'value': c.value,
                                 'block': c.children is not None, 'source': self.provenance(c)}
                                for c in symbol.node.children or []]
            if symbol.kind == 'modifier':
                result['scope_resolution'] = self.modifier_scopes(symbol)
        return result

    def search_text(self, query, type=None, limit=20):
        """Literal text search over the detected game/mod sources.

        ``type`` filters the directories to scan, so a schema path such as
        ``common/buildings`` searches that directory only. Without game/mod data there
        is nothing to search: the bundled corpus is a schema, not the game's scripts.
        """
        if not self.game_data:
            return {'status': 'NOT_FOUND', 'query': query, 'rows': [], 'returned': 0,
                    'has_more': False, 'scan_complete': False, 'matched_at_least': 0,
                    'truncated': True,
                    'note': 'Text search reads the game/mod scripts; no data source is loaded. '
                            'Run stellaris_doctor to see what was detected.'}
        directories = None
        if isinstance(type, str) and type.strip():
            candidate = type.strip().replace('\\', '/').strip('/')
            if candidate.endswith('.cwt'):
                candidate = candidate[:-4]
            directories = (candidate,) if '/' in candidate else None
        return gamedata.search_text(self.environment, query, directories, limit)

    def search(self, query, type=None, limit=20):
        if not isinstance(query, str) or not query.strip() or len(query) > 512:
            raise ValueError('query must contain 1..512 characters')
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError('limit must be 1..500')
        query = query.strip()
        selected, error = self.selection(type)
        if error:
            return error
        if selected and selected[0] == 'file' and selected[1] + '.cwt' in self.schema_files and not self.large_schema_selection(selected):
            if not any(self.accepts_registry(s, selected) for s in self.by_file[selected[1]]):
                return self.schema_hint(selected[1] + '.cwt')
        ranked = {}

        def offer(symbol, status, match, captures=None, matched_by=None):
            row = self.result(symbol, status, captures)
            row.update(match)
            if matched_by:
                # The query was a display name, not an identifier: say so, otherwise the
                # answer looks like it ignored what was asked.
                row['matched_by'] = matched_by
            if status == 'SUGGESTION':
                # Keep the candidate keys of the earlier release as aliases.
                row.update(score=match['match_score'], reason=match['match_kind'])
            row['evidence'] = status
            prior = ranked.get(symbol.id)
            if prior is None or row['match_score'] > prior['match_score']:
                ranked[symbol.id] = row

        # CWT evidence first: it decides status and always outranks a suggestion.
        for symbol in self.by_name.get(query.casefold(), []):
            if self.accepts_search(symbol, selected):
                offer(symbol, 'CONFIRMED_CWT', {'match_score': 100, 'match_kind': 'exact'})
        # Names declared by game/mod data are exact evidence too, but never claim CWT.
        if self.game_data:
            self.dynamic.enabled      # build the data index once (lazy, cached)
            self.extend_retrieval()   # and let typos reach those names as well
            for symbol in self.dynamic.symbols(query):
                if self.accepts(symbol, selected):
                    offer(symbol, 'CONFIRMED_GAME_DATA', {'match_score': 100, 'match_kind': 'game_data'})
            # Display names (Chinese above all) are how a modder usually knows a thing,
            # while every interface is keyed by identifier. A localisation value that is
            # itself a declared object name is exact evidence for that object, so the
            # query never has to be translated by hand first.
            if wants_localisation(query):
                localisation = self.dynamic.localisation
                for key in (localisation.values(query) if localisation is not None else ()):
                    for symbol in self.dynamic.symbols(key):
                        if self.accepts(symbol, selected):
                            offer(symbol, 'CONFIRMED_GAME_DATA',
                                  {'match_score': 100, 'match_kind': 'game_data'},
                                  matched_by='localisation')
        for symbol in self.templates:
            if self.accepts_search(symbol, selected):
                captures = self.template_captures(symbol, query)
                if captures is not None:
                    offer(symbol, 'TEMPLATE_MATCH', {'match_score': FULL_TEMPLATE_SCORE,
                                                     'match_kind': 'template_exact'}, captures)
        for symbol in self.by_file.get(query.casefold(), []):
            if self.accepts_search(symbol, selected):
                offer(symbol, 'SUGGESTION', {'match_score': MAX_SUGGESTION_SCORE, 'match_kind': 'file'})
        # Identifier retrieval: one score per unique name, then fan out to its
        # real symbol locations (overloads and duplicates stay independent rows).
        for match in self.retrieval.find(query, lambda s: self.accepts_search(s, selected)):
            # Two very different situations reach this loop:
            #   * the candidate name *extends* the query (``paladin`` -> ``paladin_ship``):
            #     the query is a partial spelling of a name that really is declared, so
            #     the row can be graded as the declaration it is instead of forcing a
            #     second call to find out whether the object exists;
            #   * the candidate merely *resembles* the query (``..._mul`` -> ``..._mult``):
            #     the query itself is misspelled, so the hit stays a SUGGESTION. Promoting
            #     it would launder the caller's typo into "confirmed to exist", which is
            #     exactly what retrieval must never do (see retrieval.py).
            folded = query.casefold()
            exact = self.dynamic.lookup(match.name) if self.game_data else []
            # Pure extension: every query token matched a real token of the candidate,
            # with nothing substituted. A partial token (``..._mul`` for ``..._mult``)
            # counts as a substitution, so a misspelled query keeps its SUGGESTION status
            # instead of being laundered into "confirmed to exist"; a genuinely partial
            # name (``paladin`` for ``paladin_ship``) has no substitution and is graded as
            # the declaration it is. Candidate-only tokens (``ship``) are expected here.
            extension = (folded and match.name.casefold().startswith(folded)
                         and not (match.token_diff or {}).get('substitutions'))
            if exact and extension:
                for symbol in exact:
                    if self.accepts(symbol, selected):
                        offer(symbol, 'CONFIRMED_GAME_DATA',
                              {'match_score': match.match_score, 'match_kind': 'game_data_prefix'})
                continue
            row = match.as_dict()
            for symbol in match.symbols:
                offer(symbol, 'SUGGESTION', row)
        def order(row):
            # Game/mod data rows have no PSI node; keep the CWT ordering intact.
            symbol = self.by_id.get(row['id'])
            node = symbol.node if symbol is not None else None
            return (-row['match_score'], node is not None and node.parent is not None,
                    row['name'], row['type'], node.start if node is not None else 0)
        rows = sorted(ranked.values(), key=order)
        # Two tiers with different jobs: `results` are ranked suggestions that explain a
        # real share of the query, `related` only proves the query shares tokens with
        # real identifiers and is offered for browsing. The two tiers must not share a
        # candidate, so the split is decided by identity, never by dict equality:
        # `deduplicate` returns enriched copies, so `row in suggestions` would compare a
        # plain row against a summarised one and never match.
        suggestions = self.deduplicate(
            [row for row in rows
             if row['match_score'] >= MIN_SCORE and row.get('query_support', 1.0) >= MIN_QUERY_SUPPORT])
        ranked = {identity(row) for row in suggestions}
        for row in rows:
            if row['status'] in ('CONFIRMED_CWT', 'TEMPLATE_MATCH'):
                # Evidence rows are the CWT declarations themselves and can legitimately
                # share a name with a suggestion, so they are not browse material either.
                ranked.add(identity(row))
        related = self.deduplicate(
            [row for row in rows
             if identity(row) not in ranked and row['match_score'] >= RELATED_MIN_SCORE
             and row.get('candidate_coverage', 1.0) >= RELATED_MIN_COVERAGE], summarize=False)
        payload = {'status': suggestions[0]['status'] if suggestions else 'NOT_FOUND', 'query': query,
                   'total': len(suggestions), 'truncated': len(suggestions) > limit,
                   'results': suggestions[:limit]}
        if related:
            payload['related'] = [self.related_row(row) for row in related[:RELATED_LIMIT]]
            payload['related_total'] = len(related)
        payload['note'] = NOT_FOUND if not suggestions else (RELATED_NOTE if related else SUGGESTION_NOTE)
        return payload

    @staticmethod
    def deduplicate(rows, summarize=True):
        """One row per identifier, so overloads cannot fill the Top-N list.

        The best-scoring location represents its name; the other locations stay reachable
        through `definition_count` / `definition_ids` and through `get_definition`, which
        still returns every overload. Exact and template rows are already the CWT
        declarations themselves, so their evidence rows are left untouched - only fuzzy
        suggestions are collapsed.
        """
        seen, order = {}, []
        for row in rows:
            if row['status'] in ('CONFIRMED_CWT', 'TEMPLATE_MATCH'):
                order.append(row)
                continue
            group = seen.get(identity(row))
            if group is None:
                seen[identity(row)] = [row]
                order.append(row)
            else:
                group.append(row)
        if summarize:
            for group in seen.values():
                extra = {'definition_count': len(group)}
                if len(group) > 1:
                    extra['definition_ids'] = [row['id'] for row in group]
                # Replace in place: callers hold references into `order`.
                order[order.index(group[0])] = {**group[0], **extra}
        return order

    @staticmethod
    def related_row(row):
        """Weak token-overlap row: browse context only, never a ranked suggestion."""
        keep = ('name', 'kind', 'type', 'id', 'path', 'source', 'match_score', 'match_kind',
                'query_support', 'candidate_coverage', 'token_diff', 'evidence')
        return {key: row[key] for key in keep if key in row}

    def get(self, target, type=None):
        """Dispatch catalog paths separately from registry identifiers; never read
        caller-supplied filesystem paths. Documents are the already parsed corpus.
        """
        if not isinstance(target, str) or not target.strip() or len(target) > 512:
            raise ValueError('target must contain 1..512 characters')
        name = target.strip()
        path = name.replace('\\', '/')
        # IDs contain .cwt:line:offset; they must continue to be identifier lookups.
        if path.casefold().endswith('.cwt') or ('/' in path and '.cwt:' not in path):
            parts = path.split('/')
            if path.startswith('/') or ':' in path or '..' in parts:
                return {'status': 'NOT_FOUND', 'path': name, 'message': 'Use an exact bundled relative path from stellaris_list.'}
            path = '/'.join(part for part in parts if part and part != '.')
            if path.startswith('config/'):
                path = path[len('config/'):]
            # The same whole-file limit applies regardless of directory or type.
            if path in self.documents:
                if self.file_lines[path] > MAX_FILE_LINES:
                    return {'status': 'USE_SEARCH', 'path': path,
                            'message': f'Target file is too large ({self.file_lines[path]} lines; exceeds {MAX_FILE_LINES} lines). Use stellaris_search with type="{path[:-4]}" to find a specific identifier and its declaration.'}
                return {'status': 'CONFIRMED_CWT', 'kind': 'schema_file' if '/' in path else 'registry_file', 'path': path,
                        'content': self.documents[path].source}
            return {'status': 'NOT_FOUND', 'path': name, 'message': 'No bundled CWT at this path. Use stellaris_list; do not guess schema paths.'}
        return self._get_registry_definition(name, type)

    def get_definition(self, name, type=None):
        """Compatibility entry point; shares the same registry/schema dispatcher."""
        return self.get(name, type)

    @staticmethod
    def _attach_calls(rows, index):
        """One compact call-site block per row; the reason to prefer this over grep."""
        if index is None:
            return
        for row in rows:
            positions = index.positions(row['name'])
            if not positions:
                continue
            row['calls'] = [{'file': path, 'line': line, 'text': index.snippet(path, line)}
                            for path, line in positions[:3]]
            if len(positions) > 3:
                row['calls_more'] = len(positions) - 3

    def _get_registry_definition(self, name, type=None):
        if not isinstance(name, str) or not name.strip() or len(name) > 512:
            raise ValueError('name must contain 1..512 characters')
        selected, error = self.selection(type)
        if error:
            return error
        if selected and selected[0] == 'file' and selected[1] + '.cwt' in self.schema_files and not self.large_schema_selection(selected):
            if not any(self.accepts_registry(s, selected) for s in self.by_file[selected[1]]):
                return self.schema_hint(selected[1] + '.cwt')
        if name in self.by_id and self.accepts(self.by_id[name], selected):
            symbol = self.by_id[name]
            large_schema = self.large_schema_selection(('file', symbol.canonical_type))
            if symbol.node.document.path in self.schema_files and symbol.id not in self.registry_ids and not large_schema:
                return self.schema_hint(symbol.node.document.path)
            matches = [(symbol, 'CONFIRMED_CWT', None)] if symbol.id in self.registry_ids or large_schema else []
        else:
            matches = self.lookup(name.strip(), selected, registry_only=True)
        rows = [self.result(*match, full=True) for match in matches]
        if not rows and self.game_data:
            # The corpus cannot declare mod/game objects; read them from the sources.
            for symbol in self.dynamic.symbols(name.strip()):
                if self.accepts_definition(symbol, selected) or self.accepts(symbol, selected):
                    rows.append(self.result(symbol, 'CONFIRMED_GAME_DATA', None, full=True))
        source_groups = {}
        for row in rows:
            definition = row['definition']
            key = (row['source']['file'], definition['start_line'],
                   definition['end_line'], definition['text'])
            if key in source_groups:
                row['definition_ref'] = source_groups[key]
                del row['definition']
            else:
                source_groups[key] = row['id']
        # Call sites are the part a modder would otherwise grep the whole installation
        # for; they are attached here because this is the only place that knows the name
        # a caller actually asked about.
        self._attach_calls(rows, self.dynamic.call_index if (self.game_data and rows) else None)
        return {'status': rows[0]['status'] if rows else 'NOT_FOUND', 'name': name,
                'definitions': rows, 'dataset': self.dataset,
                'note': NOT_FOUND if not rows else 'All matching definitions retained; use id to select a specific location.'}
