"""Modern desktop shell. All Tk work stays on the main thread."""
import datetime
from dataclasses import replace
import json
import os
from pathlib import Path
import queue
from tkinter import Label as TkLabel, filedialog, messagebox

import customtkinter as ctk

from . import __version__
from .desktop_runtime import Settings, ProcessTask, app_root, client_config, load_settings, save_settings
from .folder_picker import select_folders

BG = '#0b1020'
SIDE = '#101729'
CARD = '#151e32'
BORDER = '#27344d'
TEXT = '#edf3fc'
MUTED = '#91a1bb'
ACCENT = '#66e3bb'
BLUE = '#8aaeff'
RED = '#c64b5b'
SPINNER = ('◴', '◷', '◶', '◵')
PROGRESS_PREFIX = '@@GUI_PROGRESS@@'


class Desktop(ctk.CTk):
    def __init__(self, settings_path=None):
        ctk.set_appearance_mode('dark')
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
        self.update_available = False
        self.running = False
        self.closing = False
        self._button_mode = None
        self._spinner_index = 0
        self._font_cache = {}
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
        self.mod = ctk.StringVar()
        self.mod_roots = list(self.settings.mod_roots)
        self.game_data = ctk.BooleanVar(value=self.settings.game_data)
        self.watch = ctk.BooleanVar(value=self.settings.watch)
        self._overview()
        self._data_page()
        self._clients()
        self._logs()
        self._about()
        self.show_page('overview')
        self.refresh_corpus()
        self.log('面板已就绪。点击「启动服务」后，AI 客户端即可通过 HTTP 连接。')
        if warning:
            self.log(warning)
        self.after(100, self.poll)

    def font(self, size=14, weight='normal'):
        key = (size, weight)
        if key not in self._font_cache:
            self._font_cache[key] = ctk.CTkFont(family='Microsoft YaHei UI', size=size, weight=weight)
        return self._font_cache[key]

    def label(self, parent, text, size=14, color=TEXT, **kwargs):
        return ctk.CTkLabel(parent, text=text, text_color=color, font=self.font(size), **kwargs)

    def button(self, parent, text, command, primary=False, **kwargs):
        fg_color = kwargs.pop('fg_color', ACCENT if primary else '#23324b')
        hover_color = kwargs.pop('hover_color', '#8defd0' if primary else '#304360')
        return ctk.CTkButton(parent, text=text, command=command, height=40, corner_radius=9,
                             font=self.font(14), fg_color=fg_color,
                             hover_color=hover_color,
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
        side.grid_rowconfigure(8, weight=1)
        self.label(side, '✦', 48, ACCENT).grid(row=0, column=0, sticky='w', padx=25, pady=(27, 0))
        self.label(side, 'STELLARIS', 22).grid(row=1, column=0, sticky='w', padx=25)
        self.label(side, 'MODDER  /  AGENT', 12, MUTED).grid(row=2, column=0, sticky='w', padx=25, pady=(0, 36))
        for row, (key, text) in enumerate([('overview', '服务控制台'), ('data', '数据与更新'),
                                            ('clients', '客户端接入'), ('logs', '运行日志'),
                                            ('about', '关于软件')], 3):
            button = self.button(side, text, lambda k=key: self.show_page(k), anchor='w', width=174)
            button.grid(row=row, column=0, padx=18, pady=6)
            self.nav[key] = button
        self.side_status = self.label(side, '●  服务未启动', 12, MUTED)
        self.side_status.grid(row=9, column=0, sticky='w', padx=25, pady=(0, 8))
        self.label(side, f'桌面控制面板  /  v{__version__}', 11, MUTED).grid(row=10, column=0, sticky='w', padx=25, pady=(0, 25))

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
        self.service_button = self.button(actions, '启动服务', self.toggle_service, primary=True, width=150)
        self.service_button.pack(side='left')
        self.copy_http_button = self.button(actions, '复制 HTTP 配置',
                                            lambda: self.copy(client_config(self.settings, 'HTTP')), width=150)
        self.copy_http_button.pack(side='right')
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
        entry.bind('<FocusOut>', lambda _: self.save())
        entry.bind('<Return>', lambda _: self.save())
        self.editable.append(entry)
        for text, variable in [('读取游戏 / Mod 数据', self.game_data), ('自动发现 Mod 改动', self.watch)]:
            switch = ctk.CTkSwitch(row, text=text, variable=variable, font=self.font(12),
                                   progress_color=ACCENT, button_color='#f2f7ff', width=160,
                                   command=self.save)
            switch.pack(side='left', padx=(22, 0))
            self.editable.append(switch)
        self.label(page, '首次启动会建立索引。关闭面板会停止由此面板启动的服务。', 12, MUTED).pack(anchor='w', pady=(18, 0))
        self.toast = self.label(page, '', 12, ACCENT)
        self.toast.pack(anchor='w', pady=4)

    def _data_page(self):
        page = self.page('data', 'WORKSPACE / 02', '数据与更新', '选择数据位置，管理随工具附带的 CWT 规则语料。')
        body = ctk.CTkScrollableFrame(page, fg_color='transparent', corner_radius=0)
        body.pack(fill='both', expand=True)
        self.data_scroll = body
        paths = self.card(body)
        paths.pack(fill='x')
        self.label(paths, '工作空间路径', 17).pack(anchor='w', padx=22, pady=(18, 0))
        self.label(paths, '游戏目录留空时自动检测；可添加多个 Mod 源文件夹。更改会自动保存，下次启动服务时生效。', 12, MUTED).pack(anchor='w', padx=22, pady=(4, 14))
        self.label(paths, '游戏安装目录', 12, MUTED).pack(anchor='w', padx=22)
        game_row = ctk.CTkFrame(paths, fg_color='transparent')
        game_row.pack(fill='x', padx=22, pady=(4, 14))
        game_entry = ctk.CTkEntry(game_row, textvariable=self.game, placeholder_text='自动检测',
                                  height=36, fg_color=BG, border_color=BORDER, font=self.font(12))
        game_entry.pack(side='left', fill='x', expand=True)
        game_entry.bind('<FocusOut>', lambda _: self.save())
        game_entry.bind('<Return>', lambda _: self.save())
        game_browse = self.button(game_row, '浏览', self.browse_game, width=74)
        game_browse.pack(side='left', padx=(10, 0))
        self.editable.extend([game_entry, game_browse])
        self.label(paths, 'Mod 目录', 12, MUTED).pack(anchor='w', padx=22)
        mod_row = ctk.CTkFrame(paths, fg_color='transparent')
        mod_row.pack(fill='x', padx=22, pady=(4, 12))
        mod_entry = ctk.CTkEntry(mod_row, textvariable=self.mod, placeholder_text='输入 Mod 目录后按回车添加',
                                 height=36, fg_color=BG, border_color=BORDER, font=self.font(12))
        mod_entry.pack(side='left', fill='x', expand=True)
        mod_entry.bind('<Return>', lambda _: self.add_mod_root())
        mod_browse = self.button(mod_row, '浏览', self.browse_mod, width=74)
        mod_browse.pack(side='left', padx=(10, 0))
        self.editable.extend([mod_entry, mod_browse])
        self.label(paths, '源文件夹', 12, MUTED).pack(anchor='w', padx=22, pady=(0, 6))
        self.source_grid = ctk.CTkFrame(paths, fg_color='transparent')
        self.source_grid.pack(fill='x', padx=22)
        self.source_grid.bind('<Configure>', lambda event: self._layout_sources(event.width))
        self.source_hint = self.label(paths, '', 11, MUTED, wraplength=650, justify='left')
        self.source_hint.pack(anchor='w', padx=22, pady=(2, 14))
        self._source_tiles = []
        self._source_delete_buttons = []
        self._source_columns = 0
        self._render_sources()
        update = self.card(body)
        update.pack(fill='x', pady=(18, 0))
        self.label(update, 'CWT 规则语料', 17).pack(anchor='w', padx=22, pady=(18, 4))
        self.corpus_info = self.label(update, '正在读取本地语料…', 12, MUTED)
        self.corpus_info.pack(anchor='w', padx=22)
        self.update_message = self.label(update, '点击检查更新，查看上游是否有新的规则。', 13, BLUE, wraplength=710, justify='left')
        self.update_message.pack(anchor='w', padx=22, pady=(14, 10))
        self.progress = ctk.CTkProgressBar(update, height=5, progress_color=ACCENT,
                                           fg_color=BORDER, mode='determinate')
        self.progress.pack(fill='x', padx=22, pady=(0, 6))
        self.progress.set(0.0)
        self.progress_label = self.label(update, '', 11, MUTED)
        self.progress_label.pack(anchor='w', padx=22, pady=(0, 12))
        row = ctk.CTkFrame(update, fg_color='transparent')
        row.pack(fill='x', padx=22, pady=(0, 20))
        self.update_button = self.button(row, '检查更新', self.start_job, width=150)
        self.update_button.pack(side='left')
        self.label(row, '更新前请先停止服务', 12, MUTED).pack(side='right')

    def _render_sources(self):
        for child in self.source_grid.winfo_children():
            child.destroy()
        self._source_tiles = []
        self._source_delete_buttons = []
        self._source_columns = 0
        self.source_hint.configure(text='')
        if not self.mod_roots:
            self.label(self.source_grid, '尚未添加源文件夹', 12, MUTED).grid(row=0, column=0, sticky='w', pady=(0, 6))
            return
        for root in self.mod_roots:
            tile = ctk.CTkFrame(self.source_grid, width=80, height=21, fg_color='#23324b', corner_radius=3,
                                border_width=1, border_color=BORDER)
            tile.grid_propagate(False)
            name = Path(root).name or root
            name = name if len(name) <= 9 else name[:8] + '…'
            title = TkLabel(tile, text=name, font=('Microsoft YaHei UI', 11), bg='#23324b', fg=TEXT,
                            borderwidth=0, highlightthickness=0, padx=0, pady=0, anchor='w')
            title.place(x=4, y=2, relwidth=1, width=-24, relheight=1, height=-4)
            close = TkLabel(tile, text='×', font=('Arial', 11), bg='#34445d', fg=TEXT,
                            borderwidth=0, cursor='hand2')
            close.bind('<Button-1>', lambda _, value=root: self.remove_mod_root(value))
            for widget in (tile, title, close):
                widget.bind('<Enter>', lambda _, t=tile, b=close, value=root: self._show_source_remove(t, b, value), add='+')
                widget.bind('<Leave>', lambda _, t=tile, b=close: self.after(60, self._hide_source_remove, t, b), add='+')
            self._source_tiles.append(tile)
            self._source_delete_buttons.append(close)
        self._layout_sources(self.source_grid.winfo_width())

    def _layout_sources(self, width):
        if not self._source_tiles:
            return
        tile_width = self._source_tiles[0].winfo_reqwidth()
        columns = min(8, max(1, width // (tile_width + 16)))
        if columns == self._source_columns:
            return
        self._source_columns = columns
        for index, tile in enumerate(self._source_tiles):
            tile.grid(row=index // columns, column=index % columns, sticky='w', padx=(0, 8), pady=(0, 8))

    def _show_source_remove(self, tile, button, root):
        if tile.winfo_exists() and not (self.service.active or self.running or self.job.active):
            button.place(relx=1, x=-2, y=2, anchor='ne', width=16, height=16)
            self.source_hint.configure(text=root)

    def _hide_source_remove(self, tile, button):
        if not tile.winfo_exists():
            return
        x, y = tile.winfo_pointerx(), tile.winfo_pointery()
        if tile.winfo_rootx() <= x < tile.winfo_rootx() + tile.winfo_width() and \
                tile.winfo_rooty() <= y < tile.winfo_rooty() + tile.winfo_height():
            return
        button.place_forget()
        self.source_hint.configure(text='')

    def add_mod_root(self):
        return self.add_mod_roots([self.mod.get().strip()])

    def add_mod_roots(self, paths):
        if self.service.active or self.running or self.job.active:
            return False
        if not paths:
            return False
        try:
            roots = []
            seen = {os.path.normcase(value) for value in self.mod_roots}
            for raw in paths:
                if not raw.strip():
                    continue
                root = str(Path(raw).resolve())
                if not Path(root).is_dir():
                    raise ValueError('Mod 目录不存在，请重新选择。')
                if os.path.normcase(root) not in seen:
                    roots.append(root)
                    seen.add(os.path.normcase(root))
            if not roots:
                return False
            settings = replace(self.settings, mod_roots=self.mod_roots + roots)
            save_settings(self.settings_path, settings, check_paths=False)
        except (ValueError, OSError) as error:
            messagebox.showerror('无法添加源文件夹', str(error), parent=self)
            return False
        self.settings = settings
        self.mod_roots.extend(roots)
        self.mod.set('')
        self._render_sources()
        for root in roots:
            self.log('已添加源文件夹：' + root)
        return True

    def remove_mod_root(self, root):
        if self.service.active or self.running or self.job.active or root not in self.mod_roots:
            return False
        roots = [value for value in self.mod_roots if value != root]
        settings = replace(self.settings, mod_roots=roots)
        try:
            save_settings(self.settings_path, settings, check_paths=False)
        except (ValueError, OSError) as error:
            messagebox.showerror('无法移除源文件夹', str(error), parent=self)
            return False
        self.settings = settings
        self.mod_roots = roots
        self._render_sources()
        self.log('已移除源文件夹：' + root)
        return True

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

    def _about(self):
        page = self.page('about', 'WORKSPACE / 05', '关于软件', 'Stellaris Modder Agent')
        card = self.card(page)
        card.pack(fill='x')
        for title, value in [('版本', __version__),
                             ('用途', '为 Stellaris Mod 提供 CWT 规则查询、脚本验证与 MCP 服务。'),
                             ('许可', 'MIT；内置 CWT 语料的许可见 THIRD_PARTY_NOTICES.md。'),
                             ('项目', 'github.com/LT-Wistara/stellaris-modder-agent-tool')]:
            self.label(card, title, 12, MUTED).pack(anchor='w', padx=22, pady=(16, 2))
            self.label(card, value, 14, TEXT, wraplength=700, justify='left').pack(
                anchor='w', padx=22, pady=(0, 5))
        self.label(card, '', 5).pack(pady=(0, 8))

    def show_page(self, key):
        for name, page in self.pages.items():
            if name == key:
                page.grid(row=0, column=0, sticky='nsew')
            else:
                page.grid_remove()
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
        return Settings(port, self.game.get().strip(), list(self.mod_roots),
                        self.game_data.get(), self.watch.get()).validate()

    def save(self):
        try:
            settings = self.current_settings()
            if settings == self.settings:
                return True
            save_settings(self.settings_path, settings)
            self.settings = settings
            self.endpoint.configure(text=settings.url)
            self.log('设置已保存。')
            self.toast.configure(text='设置已保存。')
            return True
        except (ValueError, OSError) as error:
            messagebox.showerror('无法保存设置', str(error), parent=self)
            return False

    def browse_game(self):
        path = filedialog.askdirectory(parent=self, title='选择目录')
        if path:
            self.game.set(path)
            self.save()

    def browse_mod(self):
        try:
            paths = select_folders(self)
        except OSError:
            path = filedialog.askdirectory(parent=self, title='选择 Mod 目录')
            paths = [path] if path else []
        if paths:
            self.add_mod_roots(paths)

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
        mode = ('stopping' if self.service.stopping and busy else
                'running' if self.running else 'starting' if busy else 'idle')
        self._render_service_button(mode, working)
        update_text = ('检查中…' if self.job_action == 'check' else '更新中…') if working else (
            '更新语料' if self.update_available else '检查更新')
        update_state = 'disabled' if working or (self.update_available and busy) else 'normal'
        if self.update_button.cget('text') != update_text or self.update_button.cget('state') != update_state:
            self.update_button.configure(text=update_text, state=update_state,
                                         fg_color=ACCENT if self.update_available else '#23324b',
                                         text_color='#09241d' if self.update_available else TEXT)
        for widget in self.editable:
            state = 'disabled' if busy or working else 'normal'
            if widget.cget('state') != state:
                widget.configure(state=state)

    def _render_service_button(self, mode, working):
        state = 'disabled' if working or mode in ('starting', 'stopping') else 'normal'
        if self._button_mode == (mode, state):
            return
        previous = self._button_mode[0] if self._button_mode else None
        self._button_mode = (mode, state)
        if mode in ('starting', 'stopping'):
            self.service_button.configure(text=SPINNER[self._spinner_index], state='disabled',
                                          fg_color='#485467', hover_color='#485467',
                                          text_color=TEXT, text_color_disabled=TEXT,
                                          font=self.font(25))
            if previous not in ('starting', 'stopping'):
                self.after(90, self._spinner_tick)
            cursor = 'watch'
        else:
            running = mode == 'running'
            self.service_button.configure(text='停止服务' if running else '启动服务', state=state,
                                          fg_color=RED if running else ACCENT,
                                          hover_color='#e26371' if running else '#8defd0',
                                          text_color=TEXT if running else '#09241d', font=self.font(14))
            cursor = 'hand2' if state == 'normal' else 'arrow'
        self.service_button._canvas.configure(cursor=cursor)
        if self.service_button._text_label is not None:
            self.service_button._text_label.configure(cursor=cursor)

    def _spinner_tick(self):
        if not self._button_mode or self._button_mode[0] not in ('starting', 'stopping') or self.closing:
            return
        self._spinner_index = (self._spinner_index + 1) % len(SPINNER)
        self.service_button.configure(text=SPINNER[self._spinner_index])
        self.after(90, self._spinner_tick)

    def toggle_service(self):
        if self.running:
            self.stop_service()
        else:
            self.start_service()

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

    def start_job(self):
        action = 'update' if self.update_available else 'check'
        if self.job.active or (action == 'update' and (self.service.active or self.running)):
            return
        self.job_action = action
        arguments = ['gui-check'] if action == 'check' else [
            'cli', 'update-corpus', '--apply', '--timeout', '20', '--gui-progress']
        try:
            self.job.start(arguments)
            text = '正在检查上游版本…' if action == 'check' else '正在下载并验证语料…'
            self.update_message.configure(text=text, text_color=BLUE)
            self.log(text)
            self.progress.set(0.0)
            self.progress_label.configure(text='检查中…' if action == 'check' else '准备更新…')
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
                if kind == 'job' and payload.startswith(PROGRESS_PREFIX):
                    try:
                        progress = json.loads(payload[len(PROGRESS_PREFIX):])
                        self.progress.set(progress['fraction'])
                        self.progress_label.configure(text=progress['label'])
                    except (ValueError, KeyError, TypeError):
                        self.log(payload)
                else:
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
                if code != 0:
                    self.update_message.configure(text='操作未完成，请查看运行日志后重试。', text_color='#ff9b9b')
                    self.progress_label.configure(text='操作未完成')
                elif self.job_action == 'check':
                    try:
                        status = json.loads(output.splitlines()[-1])
                        current = status['local'] == status['upstream']
                        self.update_available = not current
                        text = '当前语料已是最新版本。' if current else '发现新的规则语料，停止服务后点击「更新语料」。'
                        self.update_message.configure(text=text, text_color=ACCENT)
                        self.progress.set(1.0)
                        self.progress_label.configure(text='检查完成')
                    except (ValueError, KeyError, IndexError):
                        self.update_message.configure(text='无法读取检查结果，请查看运行日志。', text_color='#ff9b9b')
                        self.progress_label.configure(text='检查结果不可用')
                else:
                    self.update_available = False
                    self.refresh_corpus()
                    self.update_message.configure(text='语料更新完成。下次启动服务将使用新规则。', text_color=ACCENT)
                    self.progress.set(1.0)
                    self.progress_label.configure(text='更新完成 · 100%')
                    self.log('语料更新完成。')
        self.controls()
        if self.closing and not self.service.active and not self.job.active:
            self.destroy()
        else:
            self.after(100, self.poll)

    def close(self):
        if self.job.active and self.job_action == 'update':
            self._show_exit_dialog()
            return
        self._finish_close()

    def _show_exit_dialog(self):
        if getattr(self, '_exit_dialog', None) and self._exit_dialog.winfo_exists():
            self._exit_dialog.focus_force()
            return
        dialog = ctk.CTkToplevel(self)
        self._exit_dialog = dialog
        dialog.title('语料正在更新')
        dialog.geometry('420x180')
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.configure(fg_color=BG)
        self.label(dialog, '语料正在更新', 18).pack(anchor='w', padx=24, pady=(20, 8))
        self.label(dialog, '现在退出会中断更新。', 13, MUTED).pack(anchor='w', padx=24)
        row = ctk.CTkFrame(dialog, fg_color='transparent')
        row.pack(side='bottom', fill='x', padx=24, pady=20)
        self.button(row, '继续等待', dialog.destroy, width=120).pack(side='left')
        self.button(row, '仍然退出', self._force_close, width=120,
                    fg_color=RED, hover_color='#e26371').pack(side='right')
        dialog.grab_set()
        dialog.focus_force()

    def _force_close(self):
        if getattr(self, '_exit_dialog', None) and self._exit_dialog.winfo_exists():
            self._exit_dialog.grab_release()
            self._exit_dialog.destroy()
        self._finish_close()

    def _finish_close(self):
        self.closing = True
        self.job.stop()
        self.service.stop()
        self.withdraw()


def main():
    ctk.set_default_color_theme('blue')
    app = Desktop()
    app.mainloop()
    return 0
