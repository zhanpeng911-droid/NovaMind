# -*- mode: python ; coding: utf-8 -*-
"""NovaMind GUI 打包配置（pyinstaller）。

打包命令：
    pyinstaller NovaMind.spec
产物在 dist/ 下。
"""

from pathlib import Path

import os
import sys

from PyInstaller.utils.hooks import collect_submodules

# 项目根目录：editable 安装的 novamind 包需要 pathex 才能被找到
# spec 由 pyinstaller exec 执行，无 __file__，用 SPECPATH（spec 文件所在目录）
PROJECT_ROOT = SPECPATH if "SPECPATH" in dir() else str(Path.cwd())

# Anaconda base 的 DLL（venv 基于 Anaconda 时，标准库 _ctypes/_lzma 等依赖这些）
_BASE_PREFIX = getattr(sys, "base_prefix", sys.prefix)
_ANACONDA_BIN = os.path.join(_BASE_PREFIX, "Library", "bin")
# libssl/libcrypto：_ssl.pyd/_hashlib.pyd 的依赖，缺失时 exe 内 import ssl 失败 →
# langchain/httpx 导入链崩溃 → windowed exe 静默退出（PATH 无 Library\bin 时 PyInstaller 解析不到）
_MISSING_DLLS = [
    "ffi.dll", "liblzma.dll", "libbz2.dll", "libmpdec-4.dll", "libexpat.dll", "sqlite3.dll",
    "libssl-3-x64.dll", "libcrypto-3-x64.dll",
]

binaries = []
for _dll in _MISSING_DLLS:
    _p = os.path.join(_ANACONDA_BIN, _dll)
    if os.path.exists(_p):
        binaries.append((_p, "."))

hiddenimports = [
    # uvicorn 动态导入的循环/协议
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    # pywebview 的 GUI 后端
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
    # dotenv
    "dotenv",
]
hiddenimports += collect_submodules("langchain_openai")
hiddenimports += collect_submodules("langchain_core")
hiddenimports += collect_submodules("novamind")

datas = [
    ("novamind/webui/static", "novamind/webui/static"),
    ("novamind/core/skill/builtin_skills", "novamind/core/skill/builtin_skills"),
    # 只读资源：doctor 诊断需要 docs/ 与 harness/policies.json
    ("docs", "docs"),
    ("harness", "harness"),
]

a = Analysis(
    ["packaging/gui_entry.py"],
    pathex=[PROJECT_ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pydoc"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="nova-mind-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # GUI 窗口，无控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
