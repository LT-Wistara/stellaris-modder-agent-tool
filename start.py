#!/usr/bin/env python3
"""One-file launcher for the Stellaris Agent MCP server.

Everything the old launchers did is folded into this single script, so a user
runs one program instead of a launcher plus an installer plus a diagnostic:

  * checks the Python version and where the bundled corpus lives,
  * finds the Stellaris installation and the mod this folder lives in,
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

MIN_PYTHON = (3, 9)
DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 8765
# How long an interactive start waits for the "is the corpus current?" answer
# before giving up and launching anyway.
CORPUS_CHECK_TIMEOUT = 5.0
GAME_ENV = 'STELLARIS_GAME_ROOT'
MOD_ENV = 'STELLARIS_MOD_ROOT'
ROOT = Path(__file__).resolve().parent
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
        prog='start.py',
        description='Stellaris Agent MCP server: start it and print client configuration.')
    parser.add_argument('command', nargs='?', default=None,
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
    from stellaris_agent import environment as environment_module
    environment = environment_module.detect_environment(explicit_game, explicit_mod)
    return environment, environment_module.inspect_mod_root(getattr(environment, 'mod_root', None))


def build_database(args, environment, background=False):
    from stellaris_agent.index import Database
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
    say('  Mod 目录    : ' + (str(mod_root) if mod_root else '未检测到'))
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


def print_snippets(http_url):
    """Everything a client needs, in the shapes clients actually ask for.

    No credentials appear anywhere: the server is local-only and carries no
    authentication, so a snippet is just an address (or a launch command).
    """
    entry = str(ROOT / 'start.py')
    python = str(Path(sys.executable).resolve())
    stdio = {'command': python, 'args': [entry, 'serve']}

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
    url = f'http://{host}:{port}/mcp'
    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                       'params': {'protocolVersion': '2025-06-18', 'capabilities': {},
                                  'clientInfo': {'name': 'start.py', 'version': '1'}}}).encode('utf-8')
    request = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return True, response.read(400).decode('utf-8', 'replace')
    except urllib.error.HTTPError as error:
        # 401/403 still means a server is there.
        return error.code in (400, 401, 403, 406, 415), ''
    except (urllib.error.URLError, socket.timeout, OSError):
        return False, ''


def serve_http(database, host, port, allow_remote, url):
    from stellaris_agent.http_server import create_http_server
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
    print_snippets(url)
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


def ask_yes_no(prompt):
    """A yes/no question; anything that is not a clear yes keeps the status quo."""
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
        from stellaris_agent import corpus
    except Exception:  # noqa: BLE001 - the updater must never block startup
        return
    try:
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
    if not ask_yes_no('  现在更新？/ Update now? [y/N] '):
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
        say('')
        say('已中断。 / interrupted.')
        return 130
    except Exception as error:  # noqa: BLE001 - a launcher must never die silently
        say('')
        say('[错误] 启动失败：' + type(error).__name__ + ': ' + str(error))
        path = write_crash_log(error)
        if path is not None:
            say('       详细堆栈已写入：' + str(path))
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
            os.environ.get(GAME_ENV) or user_environment(GAME_ENV),
            os.environ.get(MOD_ENV) or user_environment(MOD_ENV))
        if interactive:
            # Say something right away: silence while the corpus is parsed looks
            # like a hang on a double-clicked window.
            say('正在启动 Stellaris Agent MCP，正在读取内置语料与索引…')
            say('Starting: reading the bundled corpus and building the index...')
        database = build_database(args, environment, background=(mode == 'stdio'))
    except (OSError, ValueError, RecursionError) as error:
        say('[错误] ' + str(error))
        return 1

    if mode == 'stdio':
        from stellaris_agent.server import serve
        serve(database)
        return 0

    host = args.host
    port = args.port
    url = f'http://{host}:{port}/mcp'

    if args.print_only:
        report(database, environment, health)
        print_snippets(url)
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
        print_snippets(url)
        return 0

    if interactive:
        report(database, environment, health)
    return serve_http(database, host, port, args.allow_remote, url)


if __name__ == '__main__':
    raise SystemExit(main())
