#!/usr/bin/env python3
"""One-file launcher for the Stellaris Agent MCP server.

Everything the old launchers did is folded into this single script, so a user
runs one program instead of a launcher plus an installer plus a diagnostic:

  * checks the Python version and where the bundled corpus lives,
  * finds the Stellaris installation and reads a manually configured mod,
  * builds the index once and reports it,
  * starts the MCP server, and
  * prints ready-to-paste client configuration for the running server.

Modes are detected, so the same file works for a human and for an agent:

  double-click / ``python start.py``   interactive: serve over HTTP, print snippets
  ``python start.py serve``            stdio: what an MCP client spawns
  piped stdin (no console)             stdio automatically, nothing extra printed
  ``python start.py --print-only``     print the snippets, start nothing

Only the standard library is used.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import sys
import time

MIN_PYTHON = (3, 9)
DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 8765
# How long an interactive start waits for the "is the corpus current?" answer
# before giving up and launching anyway.
CORPUS_CHECK_TIMEOUT = 5.0
# How long the y/N question waits for an answer. A hidden or service console has
# a stdin that passes ``isatty()`` but nobody to type into it, so the question has
# to expire instead of holding the server hostage.
PROMPT_TIMEOUT = 30.0
GAME_ENV = 'STELLARIS_GAME_ROOT'
MOD_ENV = 'STELLARIS_MOD_ROOT'
ROOT = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
LINE = '=' * 66
THIN = '-' * 66


def configure_console():
    """Chinese output on a Windows console without depending on its code page."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, 'reconfigure', None)
        if reconfigure is not None:
            try:
                reconfigure(encoding='utf-8', errors='replace')
            except (ValueError, OSError):
                pass


def say(text=''):
    print(text, flush=True)


def is_interactive():
    """True when a human is watching: no piped stdin, no MCP arguments."""
    try:
        return bool(sys.stdin) and sys.stdin.isatty()
    except (ValueError, OSError, AttributeError):
        return False


def user_environment(name):
    """A value the user set as a Windows user environment variable, if any.

    The MCP client spawns this script from its own environment, which on Windows
    does not always inherit freshly set user variables, so the registry is
    consulted once as a fallback.
    """
    if sys.platform != 'win32':
        return None
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
            value, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    return value or None


def parse_arguments(argv):
    parser = argparse.ArgumentParser(
        prog=Path(sys.executable).name if getattr(sys, 'frozen', False) else 'start.py',
        description='Stellaris Agent MCP server: start it and print client configuration.')
    parser.add_argument('command', nargs='?', default=None, choices=['serve'],
                        help="'serve' starts stdio (what MCP clients use); omitted means "
                             "interactive HTTP mode when a console is attached")
    parser.add_argument('--http', action='store_true', help='force HTTP mode')
    parser.add_argument('--stdio', action='store_true', help='force stdio mode')
    parser.add_argument('--host', default=os.environ.get('STELLARIS_MCP_HOST', DEFAULT_HOST),
                        help='bind address; anything but loopback needs --allow-remote')
    parser.add_argument('--port', type=int,
                        default=int(os.environ.get('STELLARIS_MCP_PORT') or DEFAULT_PORT),
                        help=f'HTTP port (default {DEFAULT_PORT})')
    parser.add_argument('--allow-remote', action='store_true',
                        help='permit binding beyond loopback; there is no authentication, so put '
                             'a TLS reverse proxy in front of it')
    parser.add_argument('--no-game-data', action='store_true',
                        help='read only the bundled CWT corpus')
    parser.add_argument('--no-watch', action='store_true',
                        help='do not re-index game/mod files when they change')
    parser.add_argument('--game-root', metavar='DIR', help='Stellaris installation directory')
    parser.add_argument('--mod-root', metavar='DIR', action='append',
                        help='Mod directory to index; repeat for multiple Mods')
    parser.add_argument('--print-only', action='store_true',
                        help='print configuration and exit without starting a server')
    parser.add_argument('--no-update-check', action='store_true',
                        help='do not look for a newer CWT corpus when starting interactively '
                             '(also STELLARIS_UPDATE_CHECK=0)')
    parser.add_argument('--update-timeout', type=float, default=CORPUS_CHECK_TIMEOUT,
                        help='seconds to wait for that check before starting anyway '
                             '(default %(default)s)')
    return parser.parse_args(argv)


def choose_mode(args):
    if args.print_only:
        return 'http'
    if args.stdio:
        return 'stdio'
    if args.http:
        return 'http'
    if args.command == 'serve':
        return 'stdio'
    if is_interactive():
        return 'http'
    return 'stdio'


def is_loopback(host):
    """True for the local-only bind addresses this launcher defaults to."""
    return host in ('127.0.0.1', 'localhost', '::1')


def detect_environment(game_data, explicit_game=None, explicit_mod=None):
    """(environment, health) or (None, None) when game data is switched off."""
    if not game_data:
        return None, None
    from stellaris_modder_agent import environment as environment_module
    environment = environment_module.detect_environment(explicit_game, explicit_mod)
    return environment, environment_module.inspect_mod_root(getattr(environment, 'mod_root', None))


def build_database(args, environment, background=False):
    from stellaris_modder_agent.index import Database
    watch = not args.no_watch and os.environ.get('STELLARIS_WATCH', '1').strip().lower() \
        not in ('0', 'false', 'no')
    database = Database(game_data=not args.no_game_data, environment=environment, watch=watch)
    if args.no_game_data:
        return database
    if background:
        # stdio: never index before answering. A client waiting for the handshake
        # reads a multi-second silence as a dead process ("Connection closed"),
        # so the index is built on first use instead.
        return database
    try:
        database.warm_game_data()
    except Exception as error:  # noqa: BLE001 - a cold server must still answer
        say('Game/mod data unavailable: ' + str(error))
    return database


def report(database, environment, health):
    say(LINE)
    say('  Stellaris Agent MCP - 启动器 / launcher')
    say(LINE)
    say('  Python      : ' + sys.version.split()[0] + '  (' + sys.executable + ')')
    say('  工具目录    : ' + str(ROOT))
    stats = database.stats
    say('  内置 CWT    : ' + str(stats['PARSED']) + ' 个文件, ' + str(stats['symbols']) + ' 个符号')
    if environment is None:
        say('  数据源      : 仅内置 CWT（--no-game-data），Mod 自定义接口不会被解析')
        return
    game_root = getattr(environment, 'game_root', None)
    mod_root = getattr(environment, 'mod_root', None)
    say('  游戏本体    : ' + (str(game_root) if game_root else '未检测到'))
    mod_roots = getattr(environment, 'mod_roots', []) or ([mod_root] if mod_root else [])
    say('  Mod 目录    : ' + (', '.join(map(str, mod_roots)) if mod_roots else '未配置'))
    if health and health.get('reason'):
        say('')
        say('  [注意] ' + health['reason'])
        if health.get('hint'):
            say('         建议：' + health['hint'])


def mcp_config(command, arguments):
    return json.dumps({'mcpServers': {'stellaris': {'command': command, 'args': arguments}}},
                      ensure_ascii=False, indent=2)


def codex_config(command, arguments):
    return ('[mcp_servers.stellaris]\n'
            'command = ' + json.dumps(command.replace('\\', '/'), ensure_ascii=False) + '\n'
            'args = ' + json.dumps([a.replace('\\', '/') for a in arguments], ensure_ascii=False) + '\n'
            'startup_timeout_sec = 60\n')


def print_snippets(http_url, client_flags=()):
    """Everything a client needs, in the shapes clients actually ask for.

    No credentials appear anywhere: the server is local-only and carries no
    authentication, so a snippet is just an address (or a launch command).
    """
    entry = str(ROOT / 'start.py')
    python = str(Path(sys.executable).resolve())
    stdio = {'command': python, 'args': (['serve'] if getattr(sys, 'frozen', False) else [entry, 'serve'])
             + list(client_flags)}

    say('')
    say(LINE)
    say('  一、本机 MCP 客户端（HTTP 方式，复制下面任意一段即可）')
    say(LINE)
    say('  地址 / URL: ' + http_url)
    say('  认证        : 无（只监听本机，无法从别的机器访问）')
    say('')
    say(THIN)
    say('  通用 mcp.json（Claude Desktop / Cursor / Cline 等）')
    say(THIN)
    say(json.dumps({'mcpServers': {'stellaris': {'url': http_url}}}, ensure_ascii=False, indent=2))
    say('')
    say(THIN)
    say('  DSH / 云工具等支持自定义 MCP 地址的客户端')
    say(THIN)
    say('  endpoint : ' + http_url)
    say('')
    say(LINE)
    say('  二、让客户端自己拉起服务器（stdio，不必手动开这个窗口）')
    say(LINE)
    say(THIN)
    say('  通用 mcp.json')
    say(THIN)
    say(mcp_config(stdio['command'], stdio['args']))
    say('')
    say(THIN)
    say('  Codex（codex.toml）')
    say(THIN)
    say(codex_config(stdio['command'], stdio['args']))
    say('')
    say(LINE)


def port_in_use(host, port):
    """(True, payload) when something already answers MCP on that port."""
    import urllib.error
    import urllib.request
    url = http_url(host, port)
    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                       'params': {'protocolVersion': '2025-06-18', 'capabilities': {},
                                  'clientInfo': {'name': 'start.py', 'version': '1'}}}).encode('utf-8')
    request = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            payload = response.read(1_000_000).decode('utf-8')
            result = json.loads(payload).get('result', {})
            info = result.get('serverInfo', {}) if isinstance(result, dict) else {}
            return isinstance(info, dict) and info.get('name') == 'stellaris-modder-agent-tool', payload
    except urllib.error.HTTPError as error:
        return False, ''
    except (urllib.error.URLError, socket.timeout, OSError, ValueError, AttributeError):
        return False, ''


def http_url(host, port):
    return f'http://[{host}]:{port}/mcp' if ':' in host else f'http://{host}:{port}/mcp'


def serve_http(database, host, port, allow_remote, url, client_flags=()):
    from stellaris_modder_agent.http_server import create_http_server
    try:
        server = create_http_server(database, host, port, allow_remote=allow_remote)
    except ValueError as error:
        say('')
        say('[错误] ' + str(error))
        return 4
    except OSError as error:
        say('')
        say(f'[错误] 无法监听 {host}:{port} -> {error}')
        say(f'       换端口：python start.py --port {port + 1}')
        return 3
    say('')
    say(LINE)
    say('  服务器已启动，保持本窗口开着（关闭窗口 = 停止服务器）')
    say('  Server is running. Keep this window open; closing it stops the server.')
    say(LINE)
    print_snippets(http_url(host, server.server_port), client_flags)
    if sys.stdin and sys.stdin.isatty():
        say('  提示：把上面的片段贴进你的 MCP 客户端后，重新加载客户端即可使用。')
        say('        按 Ctrl+C 停止服务器。')
        say('')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        say('')
        say('已停止。 / stopped.')
    finally:
        server.server_close()
    return 0


def write_crash_log(error):
    """Keep a traceback next to the tool: a double-clicked window vanishes."""
    import datetime
    import traceback
    try:
        path = ROOT / 'start-error.log'
        with open(path, 'a', encoding='utf-8') as handle:
            handle.write('\n=== {0} ===\n'.format(datetime.datetime.now().isoformat(timespec='seconds')))
            handle.write('python: {0}\n'.format(sys.version.replace('\n', ' ')))
            handle.write('cwd: {0}\n'.format(os.getcwd()))
            traceback.print_exception(type(error), error, error.__traceback__, file=handle)
        return path
    except OSError:
        return None


def update_check_enabled(args):
    if getattr(args, 'no_update_check', False):
        return False
    return os.environ.get('STELLARIS_UPDATE_CHECK', '1').strip().lower() not in ('0', 'false', 'no')


def input_ready(timeout):
    """True when a keypress can actually be read within *timeout* seconds.

    ``sys.stdin.isatty()`` does not answer "is a human there?": a process started
    from a hidden window, a service or a scheduled task inherits a console that
    passes the check and then never receives a keystroke. Waiting on a real
    readiness check is what keeps those launches from hanging forever.
    """
    try:
        if os.name == 'nt':
            import msvcrt
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if msvcrt.kbhit():
                    return True
                time.sleep(0.05)
            return False
        import select
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        return bool(ready)
    except Exception:  # noqa: BLE001 - if we cannot tell, ask normally
        return True


def ask_yes_no(prompt, timeout=PROMPT_TIMEOUT):
    """A yes/no question; anything but a clear yes keeps the status quo.

    Silence keeps it too. An unattended launch answers nothing at all, and the
    only safe reading of that is "do not touch my files".
    """
    if timeout is not None and not input_ready(timeout):
        say('')
        say('  没有收到回答，按"不更新"继续。 / No answer; continuing without updating.')
        return False
    try:
        answer = input(prompt)
    except (EOFError, KeyboardInterrupt):
        say('')
        return False
    return answer.strip().lower() in ('y', 'yes', '是')


def confirm_corpus_update(args):
    """Offer to refresh a stale corpus before the server starts.

    Deliberately placed before the index is built: a corpus replaced here is then
    the one that gets indexed, so the update takes effect in this same launch.

    Only called for an interactive start. In stdio mode stdout carries JSON-RPC,
    so a question written there would corrupt the stream.

    Everything is best effort. A slow or absent network, an unreachable download
    host, or a failed verification all end with the launcher starting normally on
    whatever corpus is on disk -- never with a dead window.
    """
    try:
        from stellaris_modder_agent import corpus
    except Exception:  # noqa: BLE001 - the updater must never block startup
        return
    try:
        # Say something only when a network round trip is actually coming up: a
        # fresh cache answers instantly, and a line for that is just noise.
        if corpus.read_status_cache() is None:
            say('  正在检查内置语料是否有更新… / Checking for corpus updates...')
        status = corpus.cached_status(timeout=getattr(args, 'update_timeout',
                                                      CORPUS_CHECK_TIMEOUT))
    except Exception:  # noqa: BLE001
        return
    hint = corpus.update_hint(status)
    if not hint:
        return

    say('')
    for line in hint.splitlines():
        say(line)
    say('')
    if not ask_yes_no('  现在更新？/ Update now? [y/N]  (%d 秒后自动跳过 / skips in %ds)  '
                      % (int(PROMPT_TIMEOUT), int(PROMPT_TIMEOUT))):
        say('  已跳过，用当前语料启动。 / Skipped; starting with the corpus on disk.')
        return

    say('')
    code = corpus.main(['--apply'])
    say('')
    if code == 0:
        # The cached verdict still says "behind"; drop it so the next launch
        # re-checks instead of offering the update that just happened.
        try:
            corpus.cache_path().unlink(missing_ok=True)
        except OSError:
            pass
        say('  语料已更新，继续启动。 / Corpus updated; continuing to start.')
    else:
        say('  更新未完成，继续用现有语料启动。')
        say('  Update did not finish; starting with the corpus on disk.')


def main(argv=None):
    configure_console()
    try:
        return run(argv)
    except KeyboardInterrupt:
        print('已中断。 / interrupted.', file=sys.stderr)
        return 130
    except Exception as error:  # noqa: BLE001 - a launcher must never die silently
        print('[错误] 启动失败：' + type(error).__name__ + ': ' + str(error), file=sys.stderr)
        path = write_crash_log(error)
        if path is not None:
            print('       详细堆栈已写入：' + str(path), file=sys.stderr)
        else:
            import traceback
            traceback.print_exc()
        return 1


def run(argv):
    if sys.version_info < MIN_PYTHON:
        say(f'需要 Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 或更新版本，当前为 '
            f'{sys.version.split()[0]}。')
        say(f'Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required.')
        return 5
    sys.path.insert(0, str(ROOT))
    args = parse_arguments(sys.argv[1:] if argv is None else argv)
    mode = choose_mode(args)
    interactive = mode == 'http' and is_interactive()

    # Before anything else is built: ask about a stale corpus while the answer can
    # still change what gets indexed.
    if interactive and not args.print_only and update_check_enabled(args):
        confirm_corpus_update(args)

    try:
        environment, health = detect_environment(
            not args.no_game_data,
            args.game_root or os.environ.get(GAME_ENV) or user_environment(GAME_ENV),
            args.mod_root or os.environ.get(MOD_ENV) or user_environment(MOD_ENV))
        if interactive:
            # Say something right away: silence while the corpus is parsed looks
            # like a hang on a double-clicked window.
            say('正在启动 Stellaris Agent MCP，正在读取内置语料与索引…')
            say('Starting: reading the bundled corpus and building the index...')
        database = build_database(args, environment, background=(mode == 'stdio'))
    except (OSError, ValueError, RecursionError) as error:
        print('[错误] ' + str(error), file=sys.stderr)
        return 1

    if mode == 'stdio':
        from stellaris_modder_agent.server import serve
        serve(database)
        return 0

    host = args.host
    port = args.port
    url = http_url(host, port)
    client_flags = []
    if args.game_root:
        client_flags += ['--game-root', args.game_root]
    if environment is not None:
        for root in environment.mod_roots:
            client_flags += ['--mod-root', str(root)]
    if args.no_game_data:
        client_flags.append('--no-game-data')
    if args.no_watch:
        client_flags.append('--no-watch')

    if args.print_only:
        report(database, environment, health)
        print_snippets(url, client_flags)
        return 0

    busy, payload = port_in_use(host, port)
    if busy:
        say('')
        say(f'[提示] {host}:{port} 上已经有一个 MCP 服务器在运行，无需重复启动。')
        say(f'       An MCP server is already answering on {host}:{port}.')
        if payload:
            try:
                name = json.loads(payload)['result']['serverInfo']
                say('       现有服务: ' + json.dumps(name, ensure_ascii=False))
            except (ValueError, KeyError, TypeError):
                pass
        print_snippets(url, client_flags)
        return 0

    if interactive:
        report(database, environment, health)
    return serve_http(database, host, port, args.allow_remote, url, client_flags)


if __name__ == '__main__':
    raise SystemExit(main())
