# Windows GUI 发布版（0.1.3 / x64）

无需安装 Python。完整解压 StellarisModderAgent 文件夹，双击
StellarisModderAgent.exe 打开深色图形面板。主程序不会弹出命令行窗口。
请保留 StellarisModderAgent-server.exe 和 _internal 文件夹。

1. 将文件夹解压到有写入权限的位置，在「数据与更新」中输入路径并按回车添加 Mod 源文件夹，或点击「浏览」一次多选多个文件夹。增删会自动保存。
2. 点击「启动服务」，等待状态变为「服务运行中」。
3. 在控制台点击「复制 HTTP 配置」，粘贴到 MCP 客户端；其他接入方式见「客户端接入」。
4. 在「数据与更新」点击「检查更新」；发现新版本后，停止服务，同一按钮会切换为「更新语料」。下载与安装进度显示在按钮上方。

设置保存在 EXE 旁的 gui-settings.json；游戏目录留空时自动检测。没有添加 Mod 源文件夹时，若设置了 `STELLARIS_MOD_ROOT` 环境变量，后端仍会使用它。
首次启动可能需要建立游戏 / Mod 索引；进度与错误可在「运行日志」查看。
端口已被占用时，可修改端口后重新启动。
关闭窗口会停止此面板启动的服务。更新期间关闭窗口可选择继续等待或仍然退出；仍然退出会中断更新。
语料更新保存在 _internal/stellaris_modder_agent/data，重启后仍然有效。
请将工具放在有写权限的普通文件夹中。

stdio / Codex 配置由面板自动生成，调用同目录的服务程序，
无需开启图形面板。更新语料前也请关闭客户端独立启动的服务。
GUI 更新按钮更新的是 CWT 规则语料，而非 EXE 程序本身。

保留的命令行用法（可选）：

```powershell
.\StellarisModderAgent-server.exe serve
.\StellarisModderAgent-server.exe cli doctor
.\StellarisModderAgent-server.exe cli update-corpus --check
```

源码 GUI：

```powershell
python -m pip install -r requirements-gui.txt
python gui.py
```

从源码重新构建（Windows x64 Python）：

```powershell
python -m venv .venv-release
.\.venv-release\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv-release\Scripts\python.exe scripts/verify.py
.\.venv-release\Scripts\python.exe scripts/build_release.py --windows
.\.venv-release\Scripts\python.exe scripts/smoke_release.py Releases/stellaris-modder-agent-tool-0.1.3-windows-x64.zip
```

产物位于 Releases。采用目录式便携包，GUI 与服务共用运行库及语料。
