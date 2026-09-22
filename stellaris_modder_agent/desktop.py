"""Modern desktop shell. All Tk work stays on the main thread."""
import datetime
import json
from pathlib import Path
import queue
import sys
from tkinter import filedialog, messagebox

import customtkinter as ctk

from . import __version__
from .desktop_runtime import Settings, ProcessTask, app_root, client_config, load_settings, save_settings

BG = '#0b1020'
SIDE = '#101729'
CARD = '#151e32'
BORDER = '#27344d'
TEXT = '#edf3fc'
MUTED = '#91a1bb'
ACCENT = '#66e3bb'
BLUE = '#8aaeff'


class Desktop(ctk.CTk):
    def __init__(self, settings_path=None):
        super().__init__()
        self.title('Stellaris Modder Agent')
        self.geometry('1160x790')
        self.minsize(1020, 720)
        self.configure(fg_color=BG)
        self.settings_path = Path(settings_path) if settings_path else app_root() / 'gui-settings.json'
        self.settings, warning = load_settings(self.settings_path)
        self.events = queue.Queue()
        self.service = ProcessTask(self.events, 'service')
        self.job = ProcessTask(self.events, 'job')
        self.job_action = None
        self.running = False
        self.closing = False
        self.editable = []
        self.pages = {}
        self.nav = {}
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._sidebar()
        self.content = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        self.content.grid(row=0, column=1, sticky='nsew', padx=32, pady=28)
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(0, weight=1)
        self.port = ctk.StringVar(value=str(self.settings.port))
        self.game = ctk.StringVar(value=self.settings.game_root)
        self.mod = ctk.StringVar(value=self.settings.mod_root)
        self.game_data = ctk.BooleanVar(value=self.settings.game_data)
        self.watch = ctk.BooleanVar(value=self.settings.watch)
        self._overview()
        self._data_page()
        self._clients()
        self._logs()
        self.show_page('overview')
        self.refresh_corpus()
        self.log('面板已就绪。点击「启动服务」后，AI 客户端即可通过 HTTP 连接。')
        if warning:
            self.log(warning)
        self.after(100, self.poll)

    @staticmethod
    def font(size=14, weight='normal'):
        return ctk.CTkFont(family='Microsoft YaHei UI', size=size, weight=weight)

    def label(self, parent, text, size=14, color=TEXT, **kwargs):
        return ctk.CTkLabel(parent, text=text, text_color=color, font=self.font(size), **kwargs)

    def button(self, parent, text, command, primary=False, **kwargs):
        return ctk.CTkButton(parent, text=text, command=command, height=40, corner_radius=9,
                             font=self.font(14), fg_color=ACCENT if primary else '#23324b',
                             hover_color='#8defd0' if primary else '#304360',
                             text_color='#09241d' if primary else TEXT,
                             text_color_disabled='#63718a', **kwargs)

    def card(self, parent, **kwargs):
        return ctk.CTkFrame(parent, fg_color=CARD, corner_radius=16,
                           border_width=1, border_color=BORDER, **kwargs)

    def _sidebar(self):
        side = ctk.CTkFrame(self, width=214, fg_color=SIDE, corner_radius=0)
        side.grid(row=0, column=0, sticky='nsew')
        side.grid_propagate(False)
        side.grid_columnconfigure(0, weight=1)
        side.grid_rowconfigure(7, weight=1)
        self.label(side, '✦', 48, ACCENT).grid(row=0, column=0, sticky='w', padx=25, pady=(27, 0))
        self.label(side, 'STELLARIS', 22).grid(row=1, column=0, sticky='w', padx=25)
        self.label(side, 'MODDER  /  AGENT', 12, MUTED).grid(row=2, column=0, sticky='w', padx=25, pady=(0, 36))
        for row, (key, text) in enumerate([('overview', '服务控制台'), ('data', '数据与更新'),
                                            ('clients', '客户端接入'), ('logs', '运行日志')], 3):
            button = self.button(side, text, lambda k=key: self.show_page(k), anchor='w', width=174)
            button.grid(row=row, column=0, padx=18, pady=6)
            self.nav[key] = button
        self.side_status = self.label(side, '●  服务未启动', 12, MUTED)
        self.side_status.grid(row=8, column=0, sticky='w', padx=25, pady=(0, 8))
        self.label(side, f'桌面控制面板  /  v{__version__}', 11, MUTED).grid(row=9, column=0, sticky='w', padx=25, pady=(0, 25))

    def page(self, key, eyebrow, title, subtitle):
        frame = ctk.CTkFrame(self.content, fg_color=BG)
        frame.grid(row=0, column=0, sticky='nsew')
        self.pages[key] = frame
        self.label(frame, eyebrow, 11, ACCENT).pack(anchor='w')
        self.label(frame, title, 30).pack(anchor='w', pady=(8, 2))
        self.label(frame, subtitle, 13, MUTED).pack(anchor='w', pady=(0, 24))
        return frame

    def _overview(self):
        page = self.page('overview', 'WORKSPACE / 01', '服务控制台', '连接你的 Mod 工作空间，让 AI 查询真实规则与代码。')
        hero = self.card(page)
        hero.pack(fill='x')
        self.status_label = self.label(hero, '●  服务未启动', 21)
        self.status_label.pack(anchor='w', padx=24, pady=(22, 4))
        self.status_detail = self.label(hero, '准备就绪，启动后即可接入你的 AI 客户端。', 13, MUTED)
        self.status_detail.pack(anchor='w', padx=24)
        self.endpoint = self.label(hero, self.settings.url, 17, BLUE)
        self.endpoint.pack(anchor='w', padx=24, pady=(18, 16))
        actions = ctk.CTkFrame(hero, fg_color='transparent')
        actions.pack(fill='x', padx=24, pady=(0, 24))
        self.start_button = self.button(actions, '启动服务', self.start_service, primary=True, width=150)
        self.start_button.pack(side='left')
        self.stop_button = self.button(actions, '停止服务', self.stop_service, width=120, state='disabled')
        self.stop_button.pack(side='left', padx=10)
        self.button(actions, '复制地址', lambda: self.copy(self.endpoint.cget('text')), width=110).pack(side='right')
        metrics = ctk.CTkFrame(page, fg_color='transparent')
        metrics.pack(fill='x', pady=18)
        metrics.grid_columnconfigure((0, 1, 2), weight=1, uniform='metric')
        self.metrics = []
        for i, title in enumerate(['CWT 规则文件', '语料目标版本', '本地连接']):
            card = self.card(metrics)
            card.grid(row=0, column=i, sticky='ew', padx=(0, 12) if i < 2 else 0)
            self.label(card, title, 12, MUTED).pack(anchor='w', padx=18, pady=(15, 4))
            value = self.label(card, '—', 24, ACCENT if i == 0 else TEXT)
            value.pack(anchor='w', padx=18, pady=(0, 15))
            self.metrics.append(value)
        self.metrics[2].configure(text='HTTP / MCP')
        options = self.card(page)
        options.pack(fill='x')
        self.label(options, '运行选项', 16).pack(anchor='w', padx=22, pady=(18, 14))
        row = ctk.CTkFrame(options, fg_color='transparent')
        row.pack(fill='x', padx=22, pady=(0, 18))
        self.label(row, '端口', 13, MUTED).pack(side='left', padx=(0, 12))
        entry = ctk.CTkEntry(row, textvariable=self.port, width=90, height=34, fg_color=BG, border_color=BORDER)
        entry.pack(side='left')
        self.editable.append(entry)
        for text, variable in [('读取游戏 / Mod 数据', self.game_data), ('自动发现 Mod 改动', self.watch)]:
            switch = ctk.CTkSwitch(row, text=text, variable=variable, font=self.font(12),
                                   progress_color=ACCENT, button_color='#f2f7ff', width=160)
            switch.pack(side='left', padx=(22, 0))
            self.editable.append(switch)
        self.label(page, '首次启动会建立索引。关闭面板会停止由此面板启动的服务。', 12, MUTED).pack(anchor='w', pady=(18, 0))
        self.toast = self.label(page, '', 12, ACCENT)
        self.toast.pack(anchor='w', pady=4)

    def _data_page(self):
        page = self.page('data', 'WORKSPACE / 02', '数据与更新', '选择数据位置，管理随工具附带的 CWT 规则语料。')
        paths = self.card(page)
        paths.pack(fill='x')
        self.label(paths, '工作空间路径', 17).pack(anchor='w', padx=22, pady=(18, 0))
        self.label(paths, '留空时自动检测；修改设置后，下次启动服务生效。', 12, MUTED).pack(anchor='w', padx=22, pady=(4, 14))
        for title, var in [('游戏安装目录', self.game), ('Mod 目录', self.mod)]:
            self.label(paths, title, 12, MUTED).pack(anchor='w', padx=22)
            row = ctk.CTkFrame(paths, fg_color='transparent')
            row.pack(fill='x', padx=22, pady=(4, 14))
            entry = ctk.CTkEntry(row, textvariable=var, placeholder_text='自动检测', height=36,
                                fg_color=BG, border_color=BORDER, font=self.font(12))
            entry.pack(side='left', fill='x', expand=True)
            browse = self.button(row, '浏览', lambda v=var: self.browse(v), width=74)
            browse.pack(side='left', padx=(10, 0))
            self.editable.extend([entry, browse])
        self.save_button = self.button(paths, '保存设置', self.save, width=110)
        self.save_button.pack(anchor='e', padx=22, pady=(0, 18))
        self.editable.append(self.save_button)
        update = self.card(page)
        update.pack(fill='x', pady=(18, 0))
        self.label(update, 'CWT 规则语料', 17).pack(anchor='w', padx=22, pady=(18, 4))
        self.corpus_info = self.label(update, '正在读取本地语料…', 12, MUTED)
        self.corpus_info.pack(anchor='w', padx=22)
        self.update_message = self.label(update, '点击检查更新，查看上游是否有新的规则。', 13, BLUE, wraplength=710, justify='left')
        self.update_message.pack(anchor='w', padx=22, pady=(14, 10))
        self.progress = ctk.CTkProgressBar(update, height=4, progress_color=ACCENT, fg_color=BORDER, mode='indeterminate')
        self.progress.pack(fill='x', padx=22, pady=(0, 14))
        self.progress.set(0)
        row = ctk.CTkFrame(update, fg_color='transparent')
        row.pack(fill='x', padx=22, pady=(0, 20))
        self.check_button = self.button(row, '检查更新', lambda: self.start_job('check'), width=120)
        self.check_button.pack(side='left')
        self.update_button = self.button(row, '更新语料', lambda: self.start_job('update'), primary=True, width=120)
        self.update_button.pack(side='left', padx=10)
        self.label(row, '更新前请先停止服务', 12, MUTED).pack(side='right')

    def _clients(self):
        page = self.page('clients', 'WORKSPACE / 03', '客户端接入', '复制配置到你的 MCP 客户端，重新加载后即可使用四个工具。')
        self.config_mode = ctk.StringVar(value='HTTP')
        ctk.CTkSegmentedButton(page, values=['HTTP', 'stdio', 'Codex'], variable=self.config_mode,
                               command=lambda _: self.refresh_config(), font=self.font(14), height=40,
                               selected_color='#2c5b53', selected_hover_color='#356e63', fg_color=CARD,
                               unselected_color=CARD).pack(anchor='w', pady=(0, 18))
        self.config_hint = self.label(page, '', 13, MUTED, wraplength=760, justify='left')
        self.config_hint.pack(anchor='w', pady=(0, 14))
        self.config_text = ctk.CTkTextbox(page, fg_color=CARD, border_width=1, border_color=BORDER,
                                         corner_radius=14, font=('Consolas', 14), text_color=TEXT, height=320, wrap='word')
        self.config_text.pack(fill='both', expand=True)
        row = ctk.CTkFrame(page, fg_color='transparent')
        row.pack(fill='x', pady=(16, 0))
        self.button(row, '复制配置', lambda: self.copy(self.config_text.get('1.0', 'end-1c')), primary=True).pack(side='left')
        self.label(page, '工具：stellaris_search  ·  stellaris_list  ·  stellaris_validate  ·  stellaris_doctor', 12, MUTED).pack(anchor='w', pady=(16, 0))

    def _logs(self):
        page = self.page('logs', 'WORKSPACE / 04', '运行日志', '查看服务启动、语料下载与错误信息。日志仅保留在当前窗口。')
        self.log_text = ctk.CTkTextbox(page, fg_color='#0d1526', text_color='#b9c9e1', font=('Consolas', 12),
                                     border_width=1, border_color=BORDER, corner_radius=14, wrap='word')
        self.log_text.pack(fill='both', expand=True)
        self.log_text.configure(state='disabled')
        row = ctk.CTkFrame(page, fg_color='transparent')
        row.pack(fill='x', pady=(16, 0))
        self.button(row, '复制日志', lambda: self.copy(self.log_text.get('1.0', 'end-1c'))).pack(side='left')

    def show_page(self, key):
        self.pages[key].tkraise()
        for name, button in self.nav.items():
            button.configure(fg_color='#203a3b' if name == key else SIDE,
                             text_color=ACCENT if name == key else MUTED)
        if key == 'clients':
            self.refresh_config()

    def refresh_corpus(self):
        from .corpus import CONFIG_DIR, read_manifest
        manifest = read_manifest()
        count = len(list(CONFIG_DIR.rglob('*.cwt')))
        self.metrics[0].configure(text=str(count))
        self.metrics[1].configure(text=str(manifest.get('stellaris_version', '未知')))
        self.corpus_info.configure(text=f"本地 {count} 个文件   /   提交 {manifest.get('commit_sha', 'unknown')[:10]}\n"
                                       + str(manifest.get('repository', '自定义语料')))

    def current_settings(self):
        try:
            port = int(self.port.get())
        except ValueError:
            raise ValueError('端口必须是 1 到 65535 之间的整数。') from None
        return Settings(port, self.game.get().strip(), self.mod.get().strip(),
                        self.game_data.get(), self.watch.get()).validate()

    def save(self):
        try:
            settings = self.current_settings()
            save_settings(self.settings_path, settings)
            self.settings = settings
            self.endpoint.configure(text=settings.url)
            self.log('设置已保存。')
            self.toast.configure(text='设置已保存。')
            return True
        except (ValueError, OSError) as error:
            messagebox.showerror('无法保存设置', str(error), parent=self)
            return False

    def browse(self, variable):
        path = filedialog.askdirectory(parent=self, title='选择目录')
        if path:
            variable.set(path)

    def copy(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)
        self.log('已复制到剪贴板。')
        self.toast.configure(text='已复制到剪贴板。')

    def refresh_config(self):
        try:
            settings = self.current_settings()
            text = client_config(settings, self.config_mode.get())
        except ValueError as error:
            text = '请先修正运行设置：' + str(error)
        self.config_text.configure(state='normal')
        self.config_text.delete('1.0', 'end')
        self.config_text.insert('1.0', text)
        self.config_text.configure(state='disabled')
        self.config_hint.configure(text='HTTP 连接需要先在控制台启动服务，并保持面板开启。'
                                   if self.config_mode.get() == 'HTTP' else
                                   '客户端会自动启动独立的 stdio 服务，无需开启面板。配置已包含当前路径与数据选项。')

    def controls(self):
        busy = self.service.active or self.running
        working = self.job.active
        self.start_button.configure(state='disabled' if busy or working else 'normal')
        self.stop_button.configure(state='normal' if busy and not self.service.stopping else 'disabled')
        self.check_button.configure(state='disabled' if working else 'normal')
        self.update_button.configure(state='disabled' if busy or working else 'normal')
        for widget in self.editable:
            widget.configure(state='disabled' if busy or working else 'normal')

    def start_service(self):
        if self.service.active or self.job.active or not self.save():
            return
        try:
            self.service.start(['--http', '--no-update-check', '--port', str(self.settings.port)] + self.settings.flags())
            self.status_label.configure(text='●  正在启动', text_color=BLUE)
            self.status_detail.configure(text='正在读取语料与构建索引，请稍候…')
            self.side_status.configure(text='●  正在启动', text_color=BLUE)
            self.log('正在启动本地 MCP 服务…')
        except OSError as error:
            self.log('启动失败：' + str(error))
            self.status_label.configure(text='●  启动失败', text_color='#ff9b9b')
            self.status_detail.configure(text='请在运行日志中查看原因。')
        self.controls()

    def stop_service(self):
        self.service.stop()
        self.status_label.configure(text='●  正在停止', text_color=MUTED)
        self.controls()

    def start_job(self, action):
        if self.job.active or (action == 'update' and (self.service.active or self.running)):
            return
        self.job_action = action
        arguments = ['gui-check'] if action == 'check' else ['cli', 'update-corpus', '--apply', '--timeout', '20']
        try:
            self.job.start(arguments)
            text = '正在检查上游版本…' if action == 'check' else '正在下载并验证语料，请保持窗口开启…'
            self.update_message.configure(text=text, text_color=BLUE)
            self.log(text)
            self.progress.start()
        except OSError as error:
            self.update_message.configure(text='无法执行：' + str(error), text_color='#ff9b9b')
        self.controls()

    def log(self, text):
        self.log_text.configure(state='normal')
        timestamp = datetime.datetime.now().strftime('%H:%M:%S')
        self.log_text.insert('end', f'[{timestamp}]  {text}\n')
        if int(self.log_text.index('end-1c').split('.')[0]) > 2000:
            self.log_text.delete('1.0', '501.0')
        self.log_text.see('end')
        self.log_text.configure(state='disabled')

    def poll(self):
        for _ in range(150):
            try:
                kind, event, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if event == 'log':
                self.log(payload)
            elif event == 'ready' and not self.service.stopping:
                self.running = True
                self.status_label.configure(text='●  服务运行中', text_color=ACCENT)
                self.status_detail.configure(text='已就绪。前往「客户端接入」复制连接配置。')
                self.side_status.configure(text='●  服务运行中', text_color=ACCENT)
            elif event == 'done' and kind == 'service':
                code, stopped, output = payload
                self.running = False
                failed = not stopped
                self.status_label.configure(text='●  启动未完成' if failed else '●  服务已停止',
                                            text_color='#ff9b9b' if failed else TEXT)
                self.status_detail.configure(text='请查看运行日志；若已有实例占用端口，可修改端口后重试。' if failed else '随时可以重新启动服务。')
                self.side_status.configure(text='●  服务未运行', text_color=MUTED)
                self.log(f'服务已退出（{code}）。')
            elif event == 'done' and kind == 'job':
                code, stopped, output = payload
                self.progress.stop()
                self.progress.set(0)
                if code != 0:
                    self.update_message.configure(text='操作未完成，请查看运行日志后重试。', text_color='#ff9b9b')
                elif self.job_action == 'check':
                    try:
                        status = json.loads(output.splitlines()[-1])
                        current = status['local'] == status['upstream']
                        text = '当前语料已是最新版本。' if current else '发现新的规则语料，停止服务后点击「更新语料」。'
                        self.update_message.configure(text=text, text_color=ACCENT)
                    except (ValueError, KeyError, IndexError):
                        self.update_message.configure(text='无法读取检查结果，请查看运行日志。', text_color='#ff9b9b')
                else:
                    self.refresh_corpus()
                    self.update_message.configure(text='语料更新完成。下次启动服务将使用新规则。', text_color=ACCENT)
                    self.log('语料更新完成。')
        self.controls()
        if self.closing and not self.service.active and not self.job.active:
            self.destroy()
        else:
            self.after(100, self.poll)

    def close(self):
        if self.job.active and self.job_action == 'update':
            messagebox.showinfo('语料正在更新', '请等待更新结束后关闭，以免中断语料安装。', parent=self)
            return
        self.closing = True
        self.job.stop()
        self.service.stop()
        self.withdraw()


def main():
    ctk.set_appearance_mode('dark')
    ctk.set_default_color_theme('blue')
    app = Desktop()
    app.mainloop()
    return 0
