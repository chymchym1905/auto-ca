# PyInstaller spec for the Tower of Fantasy CA automation.
#
# Build:  .venv\Scripts\python.exe -m PyInstaller tof-auto-ca.spec --noconfirm
# Output: dist\ToF-Auto-CA.exe  (single file, no console window)
#
# config.json, templates\ and replays\ deliberately live *next to the exe* rather
# than inside it (see config.config_path / icons.templates_dir /
# replay.replays_dir, which all switch on sys.frozen): they are hand-edited and
# written at runtime, which a one-file bundle's temp extraction dir cannot
# support. build.py copies the templates and a starter config beside the exe.

from PyInstaller.utils.hooks import collect_dynamic_libs

# winsdk is a *namespace* package (winsdk.__file__ is None) whose submodules are
# imported lazily, so PyInstaller's analysis cannot see them. Listing exactly the
# WinRT namespaces ocr.py touches keeps the bundle small — collect_submodules on
# the whole of winsdk would pull in hundreds of unused Windows namespaces.
winsdk_modules = [
    'winsdk',
    'winsdk._winrt',
    'winsdk.system',
    'winsdk.windows',
    'winsdk.windows.foundation',
    'winsdk.windows.foundation.collections',
    'winsdk.windows.globalization',
    'winsdk.windows.graphics',
    'winsdk.windows.graphics.imaging',
    'winsdk.windows.media',
    'winsdk.windows.media.ocr',
    'winsdk.windows.storage',
    'winsdk.windows.storage.streams',
]

a = Analysis(
    ['main.py'],
    pathex=['src'],                     # the src/ layout main.py adds at runtime
    binaries=collect_dynamic_libs('winsdk'),   # _winrt.pyd
    datas=[],
    hiddenimports=winsdk_modules + [
        'pydirectinput',
        'win32gui', 'win32con', 'win32process', 'win32api',
    ],
    hookspath=[],
    runtime_hooks=[],
    # Qt ships several large modules this app never touches; dropping them keeps
    # the exe from carrying a browser engine and a 3D renderer around.
    excludes=[
        'PyQt6.QtWebEngineCore', 'PyQt6.QtWebEngineWidgets', 'PyQt6.QtQml',
        'PyQt6.QtQuick', 'PyQt6.Qt3DCore', 'PyQt6.QtMultimedia',
        'PyQt6.QtBluetooth', 'PyQt6.QtNetwork', 'PyQt6.QtPositioning',
        'PyQt6.QtSql', 'PyQt6.QtTest', 'PyQt6.QtDesigner',
        'tkinter', 'matplotlib', 'scipy', 'pandas', 'pytest',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='ToF-Auto-CA',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,          # GUI app: no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
