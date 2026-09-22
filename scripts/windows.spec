from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files

root = Path(SPECPATH).parent
data = [(str(root / 'stellaris_modder_agent' / 'data'), 'stellaris_modder_agent/data'),
        (str(root / 'pyproject.toml'), '.')]
gui = Analysis([str(root / 'gui.py')], pathex=[str(root)],
               datas=data + collect_data_files('customtkinter'))
server = Analysis([str(root / 'scripts' / 'windows_entry.py')], pathex=[str(root)], datas=data)
gui_exe = EXE(PYZ(gui.pure), gui.scripts, [], exclude_binaries=True,
              name='StellarisModderAgent', console=False, upx=False)
server_exe = EXE(PYZ(server.pure), server.scripts, [], exclude_binaries=True,
                 name='StellarisModderAgent-server', console=True, upx=False)
bundle = COLLECT(gui_exe, server_exe, gui.binaries, server.binaries, gui.datas, server.datas,
                 name='StellarisModderAgent', upx=False)
