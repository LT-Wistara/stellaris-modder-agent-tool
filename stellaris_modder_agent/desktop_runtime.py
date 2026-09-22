"""Desktop process control, kept independent from Tk for testing and stdio use."""
from dataclasses import asdict, dataclass
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
    mod_root: str = ''
    game_data: bool = True
    watch: bool = True

    def validate(self):
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise ValueError('端口必须是 1 到 65535 之间的整数。')
        for value, label in ((self.game_root, '游戏'), (self.mod_root, 'Mod')):
            if not isinstance(value, str):
                raise ValueError(label + '路径格式错误。')
            if value and not Path(value).is_dir():
                raise ValueError(label + '目录不存在，请重新选择。')
        if not isinstance(self.game_data, bool) or not isinstance(self.watch, bool):
            raise ValueError('数据源开关格式错误。')
        return self

    def flags(self):
        flags = []
        for option, value in (('--game-root', self.game_root), ('--mod-root', self.mod_root)):
            if value:
                flags += [option, value]
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
        settings = Settings(**{key: value for key, value in data.items() if key in Settings.__dataclass_fields__})
        # Keep missing paths visible so the user can repair a moved installation.
        if not isinstance(settings.port, int) or isinstance(settings.port, bool) or not 1 <= settings.port <= 65535:
            raise ValueError('端口格式错误。')
        if not all(isinstance(v, str) for v in (settings.game_root, settings.mod_root)):
            raise ValueError('路径格式错误。')
        if not all(isinstance(v, bool) for v in (settings.game_data, settings.watch)):
            raise ValueError('开关格式错误。')
        return settings, None
    except FileNotFoundError:
        return Settings(), None
    except (OSError, ValueError, TypeError) as error:
        return Settings(), '无法读取设置，已恢复默认值：' + str(error)


def save_settings(path, settings):
    settings.validate()
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
