# -*- mode: python ; coding: utf-8 -*-
"""Two native launchers, one shared onedir dependency directory.

Only installed package code and explicit source assets enter Analysis. External
runtime files are hash-checked and copied by build_support.py afterwards.
"""
import os
from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_submodules

PROJECT = Path(SPECPATH).resolve().parent
sys.path.insert(0, str(PROJECT / "packaging"))
from build_support import prepare_metadata, publishable_datas

metadata_root = Path(os.environ["WECHAT_BUILD_METADATA_ROOT"])
datas = prepare_metadata(metadata_root)
datas.append((str(PROJECT / "src/wechat_export_tool/assets/decode_voice_batch.cjs"),
              "wechat_export_tool/assets"))
hidden = collect_submodules("wechat_export_tool")
hidden += collect_submodules("chatlog_keeper.core")
hidden += ["unittest.mock", "chatlog_keeper.wechat_db", "chatlog_keeper.wechat_image",
           "chatlog_keeper.wechat_link_fetcher", "chatlog_keeper.wechat_contacts",
           "chatlog_keeper.wechat_key_identity", "chatlog_keeper.native_account_binding",
           "chatlog_keeper.participant_directory", "chatlog_keeper.participant_protocol"]
hidden = sorted(set(hidden))

shared = dict(
    pathex=[str(PROJECT / "src")], binaries=[], datas=datas,
    hiddenimports=hidden, hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=["pytest", "pip", "setuptools", "IPython", "matplotlib",
              "pandas", "numpy.tests", "PIL.tests"],
    noarchive=False, optimize=0,
)
cli = Analysis([str(PROJECT / "packaging/cli_entry.py")], **shared)
gui = Analysis([str(PROJECT / "packaging/gui_entry.py")], **shared)
cli_pyz = PYZ(cli.pure)
gui_pyz = PYZ(gui.pure)
cli_exe = EXE(cli_pyz, cli.scripts, [], exclude_binaries=True,
              name="wechat-export", debug=False, bootloader_ignore_signals=False,
              strip=False, upx=False, console=True, disable_windowed_traceback=False,
              contents_directory="_internal")
gui_exe = EXE(gui_pyz, gui.scripts, [], exclude_binaries=True,
              name="WeChatExportTool", debug=False, bootloader_ignore_signals=False,
              strip=False, upx=False, console=False, disable_windowed_traceback=True,
              contents_directory="_internal")
bundle = COLLECT(cli_exe, gui_exe, cli.binaries, gui.binaries,
                 publishable_datas(cli.datas), publishable_datas(gui.datas),
                 strip=False, upx=False, name="WeChatExportTool")
