import argparse
import json
import os
from pathlib import Path
import sys

from . import environment as environment_module
from .index import Database
from .server import serve
from .validate import Validator


def main(argv=None):
    parser = argparse.ArgumentParser(description='Offline Stellaris CWT evidence tools / MCP server')
    parser.add_argument('--game-root', metavar='DIR',
                        help='Stellaris installation directory; overrides automatic detection')
    parser.add_argument('--mod-root', metavar='DIR',
                        help='mod directory containing this tool; overrides automatic detection')
    parser.add_argument('--no-game-data', action='store_true',
                        help='read only the bundled CWT corpus; never touch the game or the mod')
    parser.add_argument('--no-watch', action='store_true',
                        help='do not re-index game/mod files when they change (long-running servers '
                             'watch by default so new mod entries appear without a restart)')
    commands = parser.add_subparsers(dest='command', required=True)
    search = commands.add_parser('search', help='Search registry identifiers (use list/get for schemas)')
    search.add_argument('query')
    search.add_argument('-t', '--type')
    search.add_argument('--limit', type=int, default=20)
    definition = commands.add_parser('get', help='Registry definition or complete subdirectory CWT')
    definition.add_argument('name')
    definition.add_argument('-t', '--type')
    validate = commands.add_parser('validate', help='Validate a script or stdin')
    source = validate.add_mutually_exclusive_group()
    source.add_argument('--code')
    source.add_argument('--file', type=Path)
    validate.add_argument('--context', help='Type string or JSON context object')
    for name in ('list', 'corpus', 'stats'):
        commands.add_parser(name)
    commands.add_parser('doctor', help='Report detected data sources and what was indexed')
    update = commands.add_parser('update-corpus',
                                 help='Refresh the bundled CWT corpus from its upstream repository')
    update.add_argument('--check', action='store_true',
                        help='report the difference and change nothing (this is the default)')
    update.add_argument('--apply', action='store_true',
                        help='download, verify and install the upstream snapshot; without it the '
                             'command only reports the difference')
    update.add_argument('--repository', default=None,
                        help='upstream repository as owner/name (default: the one in UPSTREAM.json)')
    update.add_argument('--branch', default=None, help='branch to follow')
    update.add_argument('--commit', default=None, help='pin one exact commit')
    server = commands.add_parser('serve')
    server.add_argument('--http', action='store_true', help='Use stateless Streamable HTTP instead of stdio')
    server.add_argument('--host', default='127.0.0.1',
                        help='bind address; anything but loopback needs --allow-remote')
    server.add_argument('--port', type=int, default=8765)
    server.add_argument('--allow-remote', action='store_true',
                        help='permit binding beyond loopback; there is no authentication, so put a '
                             'TLS reverse proxy in front of it')
    args = parser.parse_args(argv)
    # The corpus updater touches only the bundled snapshot, so it must not pay for
    # environment detection or an index build on the way in.
    if args.command == 'update-corpus':
        from .corpus import main as update_corpus
        forwarded = ['--check'] if not args.apply else ['--apply']
        if args.repository:
            forwarded += ['--repository', args.repository]
        if args.branch:
            forwarded += ['--branch', args.branch]
        if args.commit:
            forwarded += ['--commit', args.commit]
        return update_corpus(forwarded)
    try:
        game_data = not args.no_game_data
        environment = (environment_module.detect_environment(args.game_root, args.mod_root)
                       if game_data else None)
        watch = (not args.no_watch
                 and os.environ.get('STELLARIS_WATCH', '1').strip().lower() not in ('0', 'false', 'no'))
        db = Database(game_data=game_data, environment=environment, watch=watch)
        if args.command == 'serve':
            # State the detected sources on stderr: a folder extracted one level
            # too high would otherwise degrade to CWT-only answers in silence.
            if environment is not None:
                game_root = getattr(environment, 'game_root', None)
                mod_root = getattr(environment, 'mod_root', None)
                health = environment_module.inspect_mod_root(mod_root)
                print('Stellaris data: game=' + (str(game_root) if game_root else 'not detected')
                      + ' | mod=' + (str(mod_root) if mod_root else 'not detected'), file=sys.stderr)
                if health['reason']:
                    print('Warning: ' + health['reason'], file=sys.stderr)
                if health['hint']:
                    print('Hint: ' + health['hint'], file=sys.stderr)
            else:
                print('Stellaris data: disabled by --no-game-data; only the bundled CWT corpus is '
                      'read, so mod-defined interfaces stay unresolved.', file=sys.stderr)
            # HTTP: warm up now, while a human watches the banner, so the first
            # request is fast. stdio: never block the handshake -- a client reads
            # multi-second silence as a dead process ("Connection closed"), so
            # those requests build the index on first use instead.
            if args.http and game_data and not os.environ.get('STELLARIS_SKIP_WARMUP'):
                try:
                    db.warm_game_data()
                except Exception as error:  # noqa: BLE001 - a cold server must still answer
                    print('Game/mod data unavailable: ' + str(error), file=sys.stderr)
            if args.http:
                from .http_server import serve_http
                if getattr(args, 'allow_remote', False):
                    # serve_http owns the refusal rule; the flag just states intent.
                    os.environ['STELLARIS_ALLOW_REMOTE'] = '1'
                serve_http(db, args.host, args.port)
            else:
                serve(db)
            return 0
        if args.command == 'doctor':
            result = db.doctor()
        elif args.command == 'list':
            result = db.list_files()
        elif args.command == 'search':
            result = db.search(args.query, args.type, args.limit)
        elif args.command == 'get':
            result = db.get(args.name, args.type)
        elif args.command == 'validate':
            code = args.code if args.code is not None else (args.file.read_text('utf-8-sig') if args.file else sys.stdin.read())
            context = json.loads(args.context) if args.context and args.context.lstrip().startswith('{') else args.context
            result = Validator(db).validate(code, context)
        elif args.command == 'corpus':
            for doc in db.documents.values():
                if doc.roundtrip() != doc.source:
                    raise ValueError('Lossless roundtrip failed: ' + doc.path)
            result = {**db.stats, 'lossless_roundtrip': True, 'metadata_inventory': dict(db.metadata_inventory),
                      'upstream': db.upstream}
        else:
            result = {**db.stats, 'upstream': db.upstream, 'types': sorted(db.by_file)}
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get('status') in ('NOT_FOUND', 'UNKNOWN', 'AMBIGUOUS_TYPE', 'UNKNOWN_TYPE', 'USE_SEARCH', 'USE_SCHEMA_FILE') else 0
    except (OSError, ValueError, RecursionError) as error:
        print(str(error), file=sys.stderr)
        return 1
