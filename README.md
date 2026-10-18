# 火花助手 · Spark Assistant

一个本地运行的抖音网页版消息助手。Python + PySide6 + Playwright，MIT 开源。

用户自己扫码登录，从已有聊天好友中选择收件人，填写消息后可以立即发送，也可以设置每日时间，软件在托盘或菜单栏运行。没有业务服务器、网站账号、收费模块、遥测或检查更新功能。使用时需要联网访问抖音官方页面。

## 功能

- 抖音官方扫码登录，独立浏览器资料保存在本机。
- 登录核验后自动读取一次已有聊天会话，提供搜索、读取状态和明显的好友选择按钮。
- 每条配置同时支持“立即发送”和每日定时，关闭定时后仍可手动发送；可以勾选多项批量加入队列。
- 同一好友可以建立多条独立配置，分别设置内容和每日时间。重名好友按稳定身份区分。
- 定时任务每条配置每天执行一次；当天首次手动发送占用这次定时，之后仍可主动再次发送新消息。
- 排队和执行中防重复点击；上次结果未知时，先核对再确认发送新消息，程序不会自动重试未知请求。
- 时间统一按北京时间（UTC+8），与操作系统所在时区无关。
- 当天错过约定时间，继续执行原任务；跨天跳过，不积压历史消息。
- 全局暂停、单条任务启停、执行记录、桌面通知和可选登录自启。
- 关闭窗口进入托盘；在托盘/菜单栏点击“退出”才停止运行。
- 执行结果不确定时保留记录，不盲目重发。登录失效或网络异常导致明确未提交时，暂停派发；恢复核验后仅继续当天任务。
- 程序重启后未开始的手动批次需主动点击“继续未完成批次”，跨天跳过；已经开始但结果未知的消息不会再发。

## 安装和使用

下载：[最新发布版本](https://github.com/secode1024-star/douyin-spark-assistant/releases/latest) · [0.2.1 Windows 安装包](https://github.com/secode1024-star/douyin-spark-assistant/releases/download/v0.2.1/SparkAssistant-Setup-0.2.1-windows-x64.exe)。每个发布文件附带 SHA256 校验文件。

Windows 使用 `SparkAssistant-Setup-0.2.1-windows-x64.exe`，默认安装到当前用户目录，不需要安装 Python。Mac 分别构建 Apple 芯片 `macos-arm64.dmg` 与 Intel `macos-x64.dmg`，拖入“应用程序”后运行。

当前 Windows 安装包在本机生成和验证；Mac 构建脚本与 GitHub Actions 已提供，尚未在 macOS 实机验收。开发构建没有购买 Windows 签名或 Apple Developer ID / 公证，系统可能显示未识别开发者提示；本项目不要求关闭系统安全防护。

1. 打开软件，点击“打开官网登录”，在独立浏览器的抖音官网首页点击官方登录入口并扫码；如官网要求验证，请本人完成。
2. 登录完成后，关闭这个专用浏览器的全部窗口和标签页。软件会核验账号并自动读取已有聊天好友；到“好友与发送”查看读取状态，必要时点击“重新读取”。新导入的好友不会自动发送。这里只读取聊天会话，不代表完整互关名单。
3. 点击“添加发送项”及“选择好友”，在可搜索列表中选择好友，填写单行消息。点击行上的“立即发送”，或勾选多行再点击“一键发送已选项目”；需要每天发送时设置时间并打开定时开关。没有读到目标好友时，请先在抖音里手动发一条消息，再回来刷新。
4. 保持电脑开机、联网且未休眠；可以关闭主窗口让软件驻留托盘。
5. 在“执行记录”查看结果。`网页刷新后可见` 只用于能唯一关联我方新增消息并在刷新后核对同一标识的页面；`待人工核对` 应先在手机检查，不要通过新建相同任务直接重发。当前抖音公开页面已确认我方消息方向，但未找到可靠稳定的消息编号，因此执行一次发送后会保守显示待核对，不代表一定发送失败。

新建、修改或重新启用的定时任务从下一次预约时间生效，不追溯创建/修改之前的时间。手动任务入队后保留提交时的正文和原预约时间；修改正文或时间只影响后续配置，修改收件人或删除配置会取消其尚未执行的旧任务。明确未提交的失败使用原请求和原正文重试；已提交或待核对的旧记录不会重试。需要主动再发时，点击“再次发送”；前次结果未知则点击“核对后再次发送”，确认后按当前配置创建一条独立新消息。需要每天多个定时点，可建立多条配置。

0.2.1 使用 v3 数据库，升级前在原目录创建 `app.db.v1-backup-*.sqlite3` 或 `app.db.v2-backup-*.sqlite3` 备份。旧配置、历史及原请求编号保留；v1 的旧备注配置必须重新选择身份明确的好友。重新登录或切换账号会取消旧队列并要求核对，不能因新账号有相同备注就继续发送。

### 显示“等待执行”却没有发送

先检查“好友与发送”页面右上角：重新登录后显示“核对后继续”，普通暂停时显示“继续全部”。确认当前账号、好友和正文后，点击对应按钮恢复；已经排队的项目无需再次点击发送。如果重启过程序，还需点击“继续未完成批次”。这些操作只恢复当天尚未开始的请求，不会重发结果未知的旧消息。

## 当前边界

- 只支持一个抖音账号、1–500 字符的单行纯文字，按分钟设置每日时间。
- 本版按用户确认的范围，仅支持已有聊天会话。读取状态表示本次会话扫描的完整性，不表示全部关注或互关好友。列表按需加载，未读到某人不能据此断言从未聊过。
- 群聊、身份不明或无法核对当前收件人的项目不允许发送；列表出现某人也不保证平台允许私信，发送前还会核验收件人和输入框。
- 网页核对使用我方方向及可验证的稳定消息标识，不再把对方同文或同文数量增加作为成功证据。当前生产适配没有稳定消息编号，会保留待核对。网页证据仍不等同服务端回执、对方已读或火花延续成功。真实网页变化可能需要修改 `selectors.json` 与适配代码。
- 同一账号的任务串行执行；电脑休眠、关机、程序退出或网页加载会影响实际时间，不能保证秒级准点。
- 登录状态会失效；跨电脑、跨系统迁移登录资料未验证。主动重新登录会暂停旧任务，完成核验后须检查好友并手动恢复。
- 只有“打开官网登录”显示浏览器。核验、读取和发送均在后台进行，进度显示在软件中；遇到官网验证会暂停并提示本人打开登录，不自动弹出浏览器。
- 源码中的旧 CLI demo 和桌面版使用不同数据目录；安装桌面版后需重新扫码，避免把开发测试账号打进安装包。

## 本地数据

| 系统 | 默认路径 |
| --- | --- |
| Windows | `%LOCALAPPDATA%\SparkAssistant` |
| macOS | `~/Library/Application Support/SparkAssistant` |

`app.db` 保存任务与记录，`profiles/default` 保存浏览器登录资料，`accounts/default/sends.sqlite3` 保存发送防重记录。只在本地存储；不要将这些目录提交 Git 或上传问题反馈。退出抖音账号会清除应用管理的浏览器资料，保留任务与发送记录；Windows 卸载也默认保留这些资料。若要全部删除，请先退出软件再删除上述专用目录。

## 从源码运行

需要 Python 3.12。Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:PLAYWRIGHT_BROWSERS_PATH = "$PWD\.local\browsers"
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe desktop_main.py
```

macOS：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.local/browsers"
.venv/bin/python -m playwright install chromium
.venv/bin/python desktop_main.py
```

## 构建安装包

Windows 需要 [NSIS](https://nsis.sourceforge.io/Download)；安装到默认位置或把 `makensis.exe` 加入 PATH：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build-windows.ps1
# 或传入已有编译器
powershell -ExecutionPolicy Bypass -File scripts/build-windows.ps1 -NsisPath 'C:\Program Files (x86)\NSIS\makensis.exe'
```

macOS 在对应架构机器上运行：

```bash
bash scripts/build-macos.sh
```

产物在 `release/`，含 SHA256。Mac 需要在 Mac 上构建；不能在 Windows 上直接产生已验证的 Mac 安装包。`.github/workflows/build.yml` 可在 GitHub 手动触发，或推送 `v*` 标签后构建三个平台的安装产物，不需要自建服务器。该工作流只上传 Actions 构建产物，不自动发布 Release。应用本身不会检查 GitHub 更新。

构建后可运行 `.venv/Scripts/python.exe scripts/smoke_release.py` 验证 Windows 冻结程序；Mac 使用 `.venv/bin/python scripts/smoke_release.py --app-dir dist/SparkAssistant.app`。该检查只访问本机模拟页面，用临时资料验证 UI、进程通信、浏览器启动、会话保存和防重复；GitHub 构建也会执行它。

## 测试

常规离线测试使用临时数据库、模拟工作进程和本地网页，不能向真实好友发送：

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_store.py -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_ui.py -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_worker.py -v
# 真实浏览器访问 localhost 模拟页面，验证持久化及发送逻辑
$env:SPARK_BROWSER_INTEGRATION = '1'
$env:SPARK_SMOKE_TEST = '1'
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe desktop_main.py --browser-smoke-test .local/browser-check.json
```

`test_demo.py` 和 `test_worker.py` 的浏览器测试均使用项目下载的 Chromium，需要先按源码运行步骤安装浏览器。Windows 的浏览器资料测试应始终使用同一个普通桌面用户身份，隔离执行身份可能无法解密先前保存的 Cookie。

## 项目结构

```text
desktop_main.py            桌面及工作进程入口
spark_assistant/
  ui.py / bridge.py        窗口、托盘、工作进程协议
  store.py / scheduler.py  SQLite 与持久调度
  worker.py                抖音网页操作及发送保护
  chat_directory.py        已有会话目录和身份绑定定位
  paths.py / autostart.py  数据目录与登录启动
douyin_demo.py             已验证的聊天操作核心与旧 CLI
selectors.json            网页适配选择器
packaging/                PyInstaller 和 Windows 安装脚本
scripts/                  构建与开发工具
tests/                    离线回归测试
```

源代码采用 [MIT](LICENSE)；依赖适用各自许可证，见 [第三方说明](THIRD_PARTY_NOTICES.md)。本项目与抖音官方无关联，请仅在自己授权的账号和会话中使用。

完整使用步骤和本次验证边界见 [0.2.1 测试说明](docs/0.2.1测试说明.md)。
