"""Desktop process control, kept independent from Tk for testing and stdio use."""
from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading


def app_root():
    return Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[1]


def backend_command():
    if getattr(sys, 'frozen', False):
        return [str(app_root() / 'StellarisModderAgent-server.exe')]
    return [sys.executable, str(app_root() / 'scripts' / 'windows_entry.py')]


@dataclass
class Settings:
    port: int = 8765
    game_root: str = ''
    mod_roots: list[str] = field(default_factory=list)
    game_data: bool = True
    watch: bool = True

    def validate(self, check_paths=True):
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise ValueError('端口必须是 1 到 65535 之间的整数。')
        if not isinstance(self.game_root, str):
            raise ValueError('游戏路径格式错误。')
        if check_paths and self.game_root and not Path(self.game_root).is_dir():
            raise ValueError('游戏目录不存在，请重新选择。')
        if not isinstance(self.mod_roots, list) or any(not isinstance(root, str) or not root for root in self.mod_roots):
            raise ValueError('Mod 目录格式错误。')
        if len({os.path.normcase(os.path.abspath(root)) for root in self.mod_roots}) != len(self.mod_roots):
            raise ValueError('Mod 目录不能重复。')
        if check_paths and any(not Path(root).is_dir() for root in self.mod_roots):
            raise ValueError('Mod 目录不存在，请移除或重新选择。')
        if not isinstance(self.game_data, bool) or not isinstance(self.watch, bool):
            raise ValueError('数据源开关格式错误。')
        return self

    def flags(self):
        flags = []
        if self.game_root:
            flags += ['--game-root', self.game_root]
        for root in self.mod_roots:
            flags += ['--mod-root', root]
        if not self.game_data:
            flags.append('--no-game-data')
        if not self.watch:
            flags.append('--no-watch')
        return flags

    @property
    def url(self):
        return f'http://127.0.0.1:{self.port}/mcp'


def load_settings(path):
    try:
        data = json.loads(Path(path).read_text('utf-8'))
        if not isinstance(data, dict):
            raise ValueError('设置文件必须是 JSON 对象。')
        if 'mod_roots' not in data and 'mod_root' in data:
            if not isinstance(data['mod_root'], str):
                raise ValueError('Mod 路径格式错误。')
            data['mod_roots'] = [data['mod_root']] if data['mod_root'] else []
        settings = Settings(**{key: value for key, value in data.items() if key in Settings.__dataclass_fields__})
        # Keep missing paths visible so the user can repair a moved installation.
        settings.validate(check_paths=False)
        return settings, None
    except FileNotFoundError:
        return Settings(), None
    except (OSError, ValueError, TypeError) as error:
        return Settings(), '无法读取设置，已恢复默认值：' + str(error)


def save_settings(path, settings, check_paths=True):
    settings.validate(check_paths=check_paths)
    path = Path(path)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def client_config(settings, mode, command=None):
    command = backend_command() if command is None else command
    if mode == 'HTTP':
        return json.dumps({'mcpServers': {'stellaris': {'url': settings.url}}}, ensure_ascii=False, indent=2)
    args = command[1:] + ['serve'] + settings.flags()
    if mode == 'Codex':
        return ('[mcp_servers.stellaris]\ncommand = ' + json.dumps(command[0].replace('\\', '/'), ensure_ascii=False)
                + '\nargs = ' + json.dumps([arg.replace('\\', '/') for arg in args], ensure_ascii=False)
                + '\nstartup_timeout_sec = 60\n')
    return json.dumps({'mcpServers': {'stellaris': {'command': command[0], 'args': args}}}, ensure_ascii=False, indent=2)


class ProcessTask:
    """One owned child; reader threads only post events, never touch the UI."""
    def __init__(self, events, kind):
        self.events = events
        self.kind = kind
        self.process = None
        self.stopping = False
        self.thread = None
        self.output = []

    @property
    def active(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self, arguments):
        if self.active:
            raise RuntimeError('任务已经在运行。')
        self.stopping = False
        self.output = []
        env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1', STELLARIS_UPDATE_CHECK='0')
        self.process = subprocess.Popen(backend_command() + list(arguments), cwd=app_root(), env=env,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, encoding='utf-8', errors='replace', bufsize=1,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        try:
            for line in self.process.stdout:
                line = line.rstrip()
                self.output.append(line)
                if len(self.output) > 2000:
                    del self.output[:500]
                self.events.put((self.kind, 'log', line))
                if self.kind == 'service' and 'Server is running.' in line:
                    self.events.put((self.kind, 'ready', None))
            code = self.process.wait()
            self.events.put((self.kind, 'done', (code, self.stopping, '\n'.join(self.output))))
        finally:
            self.process.stdout.close()

    def stop(self):
        self.stopping = True
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()


def update_status():
    from . import corpus
    status = corpus.upstream_status(timeout=8)
    if status is None:
        print('无法连接语料源，请检查网络后重试。离线服务仍可正常使用。')
        return 1
    corpus.write_status_cache(status)
    print(json.dumps(status, ensure_ascii=False))
    return 0
