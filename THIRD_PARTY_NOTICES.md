# 第三方组件说明

火花助手自身源代码采用 MIT；它不改变所捆绑第三方组件的许可证。

| 组件 | 许可与来源 |
| --- | --- |
| Python | PSF License，https://www.python.org/downloads/ |
| PySide6 / Shiboken / Qt Core、Gui、Widgets | LGPL-3.0 等各组件适用许可，https://doc.qt.io/qtforpython-6/licenses.html |
| Playwright Python | Apache-2.0，https://github.com/microsoft/playwright-python |
| Chromium / Chrome for Testing | 浏览器发行包内的版权与第三方许可，https://www.chromium.org/chromium-projects/ |
| pyee / greenlet / typing_extensions | 安装包 licenses 目录中的各自许可 |

构建脚本收集已安装依赖的许可文件和版本清单到应用资源目录 `licenses/`。浏览器随附的许可保留在 `browsers/` 中。PyInstaller 的引导程序适用其许可例外；NSIS 的安装程序遵循其自身许可。

Qt/PySide 以可替换的共享库分发，应用没有阻止用户替换兼容库或为调试这些库修改后的版本进行反向工程。对应源码可从 https://download.qt.io/official_releases/QtForPython/ 和 https://download.qt.io/official_releases/qt/ 按所附版本清单取得。构建脚本和应用源代码一同提供，重新打包请保留第三方通知。

本项目的名称与原创图标不代表抖音官方。应用只在用户电脑上打开官方网页；不上传用户的登录资料、聊天内容或任务配置。
