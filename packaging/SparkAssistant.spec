# Build this spec on the target OS and architecture. No private state is included.
from pathlib import Path
import sys

sys.path.insert(0, str(Path(SPECPATH).parent))
from spark_assistant import __version__

root = Path(SPECPATH).parent
browser = root / '.local' / 'browsers'
if not browser.is_dir() or not any(browser.glob('chromium-*')):
    raise RuntimeError('Run playwright install chromium with PLAYWRIGHT_BROWSERS_PATH=.local/browsers first')
datas = [(str(root / 'selectors.json'), '.'), (str(root / 'assets'), 'assets'),
         (str(root / 'build/build-info.json'), '.'),
         (str(root / 'LICENSE'), '.'), (str(root / 'THIRD_PARTY_NOTICES.md'), '.'),
         (str(root / 'build/third-party-licenses'), 'licenses')]
# macOS browser .app bundles must stay intact. Analysis would reclassify their
# Mach-O files, rewrite library paths, and individually resign incomplete bundles.
# build-macos.sh copies them unchanged after BUNDLE and then signs the outer app.
if sys.platform != 'darwin':
    for pattern in ('chromium-*', 'ffmpeg-*', 'winldd-*'):
        datas += [(str(folder), 'browsers/' + folder.name) for folder in browser.glob(pattern) if folder.is_dir()]
a = Analysis([str(root / 'desktop_main.py')], pathex=[str(root)], binaries=[], datas=datas,
             hiddenimports=['spark_assistant.worker'],
             excludes=['PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',
                       'PySide6.QtQml', 'PySide6.QtQuick', 'tkinter'],
             noarchive=False)
if sys.platform == 'win32':
    # Qt on Windows uses the OS ICU with unversioned exports. A build host may
    # have a third-party ICU on PATH with the same filename but incompatible
    # versioned exports. Do not let it shadow Windows' system library. Browser
    # files in their own subdirectories are deliberately unaffected.
    a.binaries = [item for item in a.binaries
                  if item[0].lower() not in {'icuuc.dll', 'icudt78.dll'}]
pyz = PYZ(a.pure)
icon = str(root / 'assets' / ('icon.icns' if sys.platform == 'darwin' else 'icon.ico'))
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='SparkAssistant',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, icon=icon)
executables = [exe]
if sys.platform == 'win32':
    worker = EXE(pyz, a.scripts, [], exclude_binaries=True, name='SparkWorker',
                 debug=False, strip=False, upx=False, console=True, icon=icon)
    executables.append(worker)
coll = COLLECT(*executables, a.binaries, a.datas, strip=False, upx=False, name='SparkAssistant')
if sys.platform == 'darwin':
    app = BUNDLE(coll, name='SparkAssistant.app', icon=icon,
                 bundle_identifier='org.sparkassistant.desktop',
                 info_plist={'CFBundleDisplayName': '火花助手',
                             'CFBundleShortVersionString': __version__,
                             'NSHighResolutionCapable': True,
                             'LSMinimumSystemVersion': '13.0'})
