"""Exercise the actual Windows EXE extracted to a new, Unicode path."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='stellaris-release-') as temp:
        root = Path(temp) / '中文 Mod 路径'
        root.mkdir()
        (root / 'descriptor.mod').write_text('name="Release smoke"', encoding='utf-8')
        scripts = root / 'common' / 'scripted_triggers'
        scripts.mkdir(parents=True)
        (scripts / 'smoke.txt').write_text('release_smoke_trigger = { always = yes }', encoding='utf-8')
        with zipfile.ZipFile(args.archive) as archive:
            archive.extractall(root)
        exe = root / 'StellarisModderAgent' / 'StellarisModderAgent-server.exe'
        gui = exe.with_name('StellarisModderAgent.exe')
        import struct
        def subsystem(path):
            data = path.read_bytes()
            pe_offset = struct.unpack_from('<I', data, 0x3c)[0]
            return struct.unpack_from('<H', data, pe_offset + 24 + 68)[0]
        assert subsystem(gui) == 2, 'GUI executable must use Windows subsystem'
        assert subsystem(exe) == 3, 'MCP helper must retain console pipes'
        env = dict(os.environ)
        for key in ('PYTHONPATH', 'PYTHONHOME', 'STELLARIS_MOD_ROOT', 'STELLARIS_MOD_DIR',
                    'STELLARIS_GAME_ROOT', 'STELLARIS_GAME_DIR'):
            env.pop(key, None)
        env['PATH'] = ''  # The release must not depend on Python installed on PATH.
        env['STELLARIS_UPDATE_CHECK'] = '0'
        options = dict(cwd=temp, env=env, capture_output=True, encoding='utf-8', timeout=90)

        def run(*arguments, input=''):
            result = subprocess.run([str(exe), *arguments], input=input, **options)
            assert result.returncode == 0, result.stderr + result.stdout
            return result.stdout

        stats = json.loads(run('cli', '--no-game-data', 'corpus'))
        assert stats['PARSED'] == 173 and stats['lossless_roundtrip'], stats
        snippets = run('--print-only', '--no-game-data')
        assert str(exe) in snippets and 'start.py' not in snippets, snippets
        doctor = json.loads(run('cli', '--mod-root', str(root), 'doctor'))
        assert doctor['environment']['mod_root'] == str(root), doctor
        messages = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
             'params': {'protocolVersion': '2025-11-25'}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
        ]
        for name, arguments in [
            ('stellaris_list', {}),
            ('stellaris_search', {'query': 'release_smoke_trigger', 'type': 'trigger'}),
            ('stellaris_validate', {'code': 'always = yes', 'context': 'trigger'}),
            ('stellaris_doctor', {}),
        ]:
            messages.append({'jsonrpc': '2.0', 'id': len(messages), 'method': 'tools/call',
                             'params': {'name': name, 'arguments': arguments}})
        replies = [json.loads(line) for line in run('serve', '--mod-root', str(root), input=''.join(
            json.dumps(message) + '\n' for message in messages)).splitlines()]
        assert len(replies) == len(messages) - 1, replies
        assert replies[0]['result']['serverInfo']['version'] == '0.1.3', replies[0]
        assert len(replies[1]['result']['tools']) == 4, replies[1]
        for reply in replies:
            assert 'error' not in reply and not reply['result'].get('isError'), reply
        assert 'CONFIRMED_GAME_DATA' in json.dumps(replies[3]), replies[3]

        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        url = f'http://127.0.0.1:{port}/mcp'
        # Logs go to a file so the child cannot block on a full stdout pipe.
        with (root / 'http.log').open('w', encoding='utf-8') as log:
            process = subprocess.Popen([str(exe), '--http', '--no-game-data', '--no-update-check',
                                        '--port', str(port)], cwd=temp, env=env,
                                       stdout=log, stderr=log, stdin=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 40
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                def post(message):
                    request = urllib.request.Request(url, data=json.dumps(message).encode(),
                                                     headers={'Content-Type': 'application/json'})
                    with opener.open(request, timeout=10) as response:
                        return json.load(response)
                while True:
                    try:
                        reply = post(messages[0])
                        break
                    except urllib.error.URLError:
                        if time.monotonic() >= deadline or process.poll() is not None:
                            raise
                        time.sleep(0.2)
                assert reply['result']['serverInfo']['version'] == '0.1.3', reply
                assert len(post(messages[2])['result']['tools']) == 4
                reply = post({'jsonrpc': '2.0', 'id': 10, 'method': 'tools/call',
                              'params': {'name': 'stellaris_search',
                                         'arguments': {'query': 'has_background_job', 'type': 'trigger'}}})
                assert not reply['result']['isError'] and 'CONFIRMED_CWT' in json.dumps(reply), reply
            finally:
                process.terminate()
                process.wait(timeout=10)
        print(json.dumps({'status': 'passed', 'corpus_files': stats['PARSED'],
                          'version': '0.1.3', 'checks': ['Windows GUI subsystem', 'console MCP helper', 'relocated Unicode path', 'empty PATH',
                          'CLI corpus roundtrip', 'EXE client configuration', 'manual mod directory',
                          'stdio handshake and all four tools', 'HTTP handshake/list/search']}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
