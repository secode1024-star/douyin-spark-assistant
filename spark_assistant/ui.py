"""Local-only Chinese desktop UI for Spark Assistant."""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
from datetime import datetime
import os
from pathlib import Path
import sys
from uuid import uuid4

from PySide6.QtCore import Qt, QTime, QTimer, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFormLayout, QFrame, QGridLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
    QPushButton, QScrollArea, QStackedWidget, QStyle, QSystemTrayIcon,
    QTableWidget, QTableWidgetItem, QTimeEdit, QVBoxLayout, QWidget,
)

from .bridge import WorkerBridge
from . import __version__
from .validation import normalize_recipient, validate_message


STYLE = """
QWidget { font-family: 'Microsoft YaHei UI', 'PingFang SC', 'Noto Sans CJK SC', sans-serif;
    font-size: 13px; color: #2B303C; }
QMainWindow, #Shell { background: #F5F6F8; }
#ScrollablePage, #ScrollViewport { background: #F5F6F8; }
#Sidebar { background: #FFFFFF; border-right: 1px solid #E8EBEF; }
QLabel { background: transparent; }
#Brand { font-size: 23px; font-weight: 700; color: #EF684E; }
#Eyebrow { font-size: 11px; color: #9BA3B0; letter-spacing: 1px; }
#PageTitle { font-size: 25px; font-weight: 700; color: #242A35; }
#Subtitle, #Muted { color: #7C8594; }
#Muted { font-size: 12px; }
#Card, #StatCard { background: white; border: 1px solid #E5E9EF; border-radius: 12px; }
#StatValue { font-size: 26px; font-weight: 700; }
#CardTitle { font-size: 16px; font-weight: 600; }
#AccountBadge { background: #FFF0EB; color: #BE5943; border-radius: 8px; padding: 8px 14px; }
#StatusStrip { background: #ECEFF3; border-radius: 8px; color: #6F7989; padding: 10px 14px; }
QPushButton { background: white; border: 1px solid #DDE2E9; border-radius: 7px;
    padding: 9px 15px; font-weight: 500; }
QPushButton:hover { border-color: #F19B88; background: #FFF9F6; }
QPushButton:pressed { background: #FFEDE7; }
QPushButton:disabled { color: #A9AFB9; background: #F2F4F7; border-color: #E6E9ED; }
QPushButton[primary="true"] { background: #EE7459; color: white; border-color: #EE7459; }
QPushButton[primary="true"]:hover { background: #E76448; }
QPushButton[primary="true"]:disabled { background: #EAC3B9; border-color: #EAC3B9; }
QPushButton[danger="true"] { color: #C45C55; }
QPushButton[nav="true"] { text-align: left; padding: 13px 18px; border: none;
    background: transparent; color: #707A8B; font-size: 14px; }
QPushButton[nav="true"]:hover { background: #F7F8FA; }
QPushButton[nav="true"]:checked { background: #FFF0EA; color: #DD674C; font-weight: 600; }
QLineEdit, QComboBox, QTimeEdit { border: 1px solid #DDE3EA; border-radius: 7px;
    background: #FFFFFF; padding: 9px 11px; min-height: 19px; selection-background-color: #EE7459; }
QLineEdit:focus, QComboBox:focus, QTimeEdit:focus { border-color: #EE7459; }
QComboBox::drop-down, QTimeEdit::up-button, QTimeEdit::down-button { border: none; width: 22px; }
QCheckBox { spacing: 9px; padding: 5px 0; }
QCheckBox::indicator { width: 17px; height: 17px; border: 1px solid #CBD2DC;
    border-radius: 4px; background: white; }
QCheckBox::indicator:checked { background: #EE7459; border-color: #EE7459; }
QTableWidget { border: 1px solid #E4E8EF; border-radius: 9px; background: white;
    gridline-color: #EFF1F5; selection-background-color: #FFF0E9; selection-color: #2B303C;
    alternate-background-color: #FCFCFD; outline: 0; }
QHeaderView::section { background: #FAFBFC; color: #8991A0; border: none;
    border-bottom: 1px solid #E7EBF0; padding: 12px 10px; font-size: 12px; }
QTableWidget::item { padding: 10px; border-bottom: 1px solid #F0F2F5; }
QTableWidget::item:selected { background: #FFF0E9; color: #2B303C; }
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
QScrollBar::handle:vertical { background: #D5DAE3; border-radius: 3px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QMenu { background: white; border: 1px solid #E1E5EB; padding: 6px; }
QMenu::item { padding: 9px 26px; border-radius: 4px; }
QMenu::item:selected { background: #FFF0EA; }
QDialog { background: #F8F9FB; }
QToolTip { background: #303847; color: white; border: none; padding: 7px; }
"""

STATUS_LABELS = {
    "pending": "等待执行", "running": "正在发送", "persisted_in_chat": "网页刷新后可见",
    "uncertain": "待人工核对", "failed": "执行失败", "missed": "跨天跳过",
    "cancelled": "已取消", "login_required": "需要重新登录", "unavailable": "暂时无法执行",
}

SYNC_LABELS = {"complete": "本次会话读取完成", "partial": "部分会话", "failed": "失败",
               "unsupported": "暂未支持", "verification_required": "需要官网验证",
               "never": "尚未读取", "not_synced": "尚未读取", "running": "正在读取"}
SUBMISSION_LABELS = {"not_submitted": "尚未提交", "possible": "可能已提交", "confirmed": "网页已核对",
                     "submitted": "已提交，待核对", "unknown": "尚不明确"}


def _friend_name(friend):
    return _text(friend.get("display_name") or friend.get("remark") or friend.get("nickname") or "未命名好友")


def _friend_reference(friend):
    return _text(friend.get("platform_id") or friend.get("identity_ref") or friend.get("profile_url"))


def _text(value):
    return "" if value is None else str(value)


def _time_text(value):
    if not value:
        return "—"
    text = _text(value)
    try:
        return datetime.fromisoformat(text).strftime("%m-%d %H:%M:%S")
    except ValueError:
        return text.replace("T", " ")[:19]


def _label(text, name=None, word_wrap=False):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    if name:
        label.setObjectName(name)
    label.setWordWrap(word_wrap)
    return label


def _button(text, callback=None, primary=False, danger=False):
    button = QPushButton(text)
    if primary:
        button.setProperty("primary", True)
    if danger:
        button.setProperty("danger", True)
    if callback:
        button.clicked.connect(callback)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


def _card():
    card = QFrame()
    card.setObjectName("Card")
    layout = QVBoxLayout(card)
    layout.setContentsMargins(24, 22, 24, 22)
    layout.setSpacing(14)
    return card, layout


def _table(headers):
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().hide()
    table.verticalHeader().setDefaultSectionSize(51)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.setShowGrid(False)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    table.horizontalHeader().setStretchLastSection(True)
    return table


class FriendPicker(QDialog):
    """Select an identity-bearing loaded chat; names are display text only."""

    def __init__(self, contacts, parent=None):
        super().__init__(parent)
        self.contacts = list(contacts)
        self.selected_contact = None
        self.setWindowTitle("选择聊天好友")
        self.resize(660, 460)
        layout = QVBoxLayout(self)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索备注、昵称或身份标识")
        self.search.setObjectName("friendSearchInput")
        layout.addWidget(self.search)
        self.table = _table(["好友", "身份标识", "可用状态"])
        self.table.setObjectName("friendPickerTable")
        self.table.setColumnWidth(0, 155)
        self.table.setColumnWidth(1, 220)
        self.table.doubleClicked.connect(self._accept_friend)
        layout.addWidget(self.table, 1)
        self.hint = _label("只能选择已读取的聊天好友。没找到时，请先在抖音里手动发一条消息，再回来刷新。", "Muted", True)
        layout.addWidget(self.hint)
        buttons = QDialogButtonBox()
        buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole).clicked.connect(self.reject)
        choose = buttons.addButton("选择这个好友", QDialogButtonBox.ButtonRole.AcceptRole)
        choose.setProperty("primary", True)
        choose.clicked.connect(self._accept_friend)
        layout.addWidget(buttons)
        self.search.textChanged.connect(self._populate)
        self._populate()

    def _populate(self):
        query = self.search.text().strip().casefold()
        self.visible_contacts = [friend for friend in self.contacts if not query or query in
                                 (" ".join([_friend_name(friend), _text(friend.get("nickname")),
                                            _friend_reference(friend)])).casefold()]
        self.table.setRowCount(len(self.visible_contacts))
        for row, friend in enumerate(self.visible_contacts):
            status = "可发送" if friend.get("sendable") else (friend.get("blocked_reason") or "等待身份核验")
            for col, value in enumerate([_friend_name(friend), _friend_reference(friend), status]):
                item = QTableWidgetItem(_text(value))
                item.setToolTip(_text(value))
                self.table.setItem(row, col, item)

    def _accept_friend(self, *_):
        row = self.table.currentRow()
        if row < 0 or row >= len(self.visible_contacts):
            self.hint.setText("请先选择一个好友。")
            return
        friend = self.visible_contacts[row]
        if not friend.get("sendable"):
            self.hint.setText(friend.get("blocked_reason") or "这个好友尚未完成身份及私信路径核验，暂时不能选择。")
            return
        self.selected_contact = friend
        self.accept()


class RuleDialog(QDialog):
    def __init__(self, contacts, rule=None, parent=None, *, contact=None, scheduled=False, immediate=False):
        super().__init__(parent)
        self.rule = rule
        self.contacts = [friend for friend in contacts if isinstance(friend, dict)]
        self.selected_contact = None
        self._had_time = bool(rule and rule.get("send_time"))
        self.setWindowTitle("编辑发送项" if rule else "添加发送项")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 24, 26, 24)
        layout.setSpacing(18)
        layout.addWidget(_label("选择好友，写下一句问候", "CardTitle"))
        form = QFormLayout()
        form.setVerticalSpacing(16)
        self.recipient = QLineEdit()
        self.recipient.setObjectName("recipientInput")
        self.recipient.setReadOnly(True)
        self.recipient.setPlaceholderText("从已读取的聊天好友中选择")
        recipient_row = QHBoxLayout()
        recipient_row.addWidget(self.recipient, 1)
        self.choose_friend_button = _button("选择好友", self._choose_friend)
        self.choose_friend_button.setObjectName("chooseFriendButton")
        recipient_row.addWidget(self.choose_friend_button)
        self.message = QLineEdit()
        self.message.setObjectName("messageInput")
        # Qt counts UTF-16 units for maxLength; validate Python characters on
        # save so pasted messages and emoji are never silently shortened.
        self.message.setPlaceholderText("输入要发送的一句话，最多 500 字")
        self.send_time = QTimeEdit(QTime(9, 0))
        self.send_time.setObjectName("sendTimeInput")
        self.send_time.setDisplayFormat("HH:mm")
        self.enabled = QCheckBox("每天定时发送（关闭后仍可立即发送）")
        self.enabled.setChecked(scheduled)
        self.send_time.setEnabled(scheduled)
        self.enabled.toggled.connect(self.send_time.setEnabled)
        form.addRow("聊天好友", recipient_row)
        form.addRow("发送内容", self.message)
        form.addRow("每日时间", self.send_time)
        form.addRow("", self.enabled)
        layout.addLayout(form)
        layout.addWidget(_label(
            "定时任务每天一次；首次手动发送会提前完成当天定时任务。\n"
            "你可以再次主动发送。结果不明确时，先核对再确认发送新消息。北京时间，跨天定时任务跳过。",
            "Muted", True,
        ))
        self.error = _label("", "DialogError", True)
        self.error.setStyleSheet("color: #C55C4A;")
        layout.addWidget(self.error)
        buttons = QDialogButtonBox()
        cancel = buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        save = buttons.addButton("保存并加入发送队列" if immediate else "保存发送项", QDialogButtonBox.ButtonRole.AcceptRole)
        save.setProperty("primary", True)
        save.setObjectName("saveRuleButton")
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self._validate)
        layout.addWidget(buttons)
        if rule:
            self.recipient.setText(_text(rule["recipient"]))
            self.message.setText(_text(rule["message"]))
            if rule.get("send_time"):
                self.send_time.setTime(QTime.fromString(rule["send_time"][:5], "HH:mm"))
            self.enabled.setChecked(bool(rule["enabled"]))
            selected = next((friend for friend in self.contacts if friend.get("id") == rule.get("contact_id")), None)
            if selected and selected.get("sendable") and not rule.get("needs_review"):
                self.set_contact(selected)
            else:
                self.error.setText("旧绑定需要核对。请点击“选择好友”重新关联已读取的聊天好友。")
        elif contact:
            self.set_contact(contact)

    def set_contact(self, contact):
        self.selected_contact = contact
        self.recipient.setText(_friend_name(contact))
        self.recipient.setToolTip(_friend_reference(contact))
        self.error.setText("")

    def _choose_friend(self):
        picker = FriendPicker(self.contacts, self)
        if picker.exec() == QDialog.DialogCode.Accepted:
            self.set_contact(picker.selected_contact)

    def values(self):
        if not self.selected_contact or not self.selected_contact.get("sendable"):
            raise ValueError("请通过“选择好友”关联已读取并核验的聊天好友。")
        return {
            "recipient": normalize_recipient(_friend_name(self.selected_contact)),
            "contact_id": self.selected_contact["id"],
            "message": validate_message(self.message.text()),
            "send_time": self.send_time.time().toString("HH:mm") if self.enabled.isChecked() or self._had_time else None,
            "enabled": self.enabled.isChecked(),
        }

    def _validate(self):
        try:
            self.values()
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        self.accept()


class MainWindow(QMainWindow):
    def __init__(self, store, scheduler, paths):
        super().__init__()
        self.store = store
        self.scheduler = scheduler
        self.paths = paths
        self._active_run = None
        self._quitting = False
        self._dispatch_hold_depth = 0
        self._held_rule_ids = set()
        self._session_validated = False
        self._friend_sync_attempted = False
        self._pending_friend_sync = False
        self._sync_epoch = None
        self._checked_rule_ids = set()
        self._populating_rules = False
        self._account_state = "unverified"
        self._account_detail = ""
        self.store.set_setting("account_ready", False)
        self.store.set_setting("late_policy", "same_day")
        if self.store.get_setting("login_review_required", False):
            self.store.set_setting("paused", True)
        self.bridge = WorkerBridge(paths, self)
        self.bridge.progress.connect(self._on_progress)
        self.bridge.result.connect(self._on_result)
        self.bridge.busy_changed.connect(self._on_busy)
        self.setWindowTitle(f"火花助手 {__version__}" if getattr(sys, "frozen", False) else "火花助手 · 源码运行")
        self.resize(1320, 820)
        self.setMinimumSize(1080, 700)
        self.setStyleSheet(STYLE)
        self.setWindowIcon(self._app_icon())
        self._build_ui()
        self._build_tray()
        self.refresh()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(3000)
        if paths.profile_dir.exists() and os.environ.get("SPARK_SMOKE_TEST") != "1":
            QTimer.singleShot(500, lambda: self._start_operation("check"))

    def _app_icon(self):
        path = Path(self.paths.resource_dir) / "assets" / "icon.png"
        if path.exists():
            return QIcon(str(path))
        image = QPixmap(64, 64)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#EE7459"))
        painter.drawRoundedRect(2, 2, 60, 60, 17, 17)
        painter.setPen(QColor("white"))
        font = painter.font()
        font.setPixelSize(34)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(image.rect(), Qt.AlignmentFlag.AlignCenter, "火")
        painter.end()
        return QIcon(image)

    def _build_ui(self):
        shell = QWidget()
        shell.setObjectName("Shell")
        self.setCentralWidget(shell)
        root = QHBoxLayout(shell)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(205)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(18, 30, 18, 22)
        side.setSpacing(8)
        side.addWidget(_label("火花助手", "Brand"))
        side.addWidget(_label("SPARK  /  LOCAL", "Eyebrow"))
        side.addSpacing(38)
        self.nav = []
        for index, title in enumerate(["抖音账号", "好友与发送", "执行记录", "软件设置"]):
            button = _button(f"{index + 1:02d}    {title}")
            button.setProperty("nav", True)
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, i=index: self._navigate(i))
            self.nav.append(button)
            side.addWidget(button)
        side.addStretch()
        self.sidebar_state = _label("●  调度待命", "Muted")
        side.addWidget(self.sidebar_state)
        side.addWidget(_label("免费 · 开源 · 数据保存在本机", "Muted", True))
        root.addWidget(sidebar)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(32, 28, 32, 22)
        body.setSpacing(18)
        self.pages = QStackedWidget()
        body.addWidget(self.pages, 1)
        self.status_strip = _label("准备就绪。首次使用请打开抖音官网登录，完成后关闭该浏览器窗口。", "StatusStrip", True)
        body.addWidget(self.status_strip)
        root.addWidget(content, 1)
        self._build_account_page()
        self._build_tasks_page()
        self._build_history_page()
        self._build_settings_page()
        self._navigate(0)

    def _page(self, title, subtitle, scrollable=False):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)
        layout.addWidget(_label(title, "PageTitle"))
        layout.addWidget(_label(subtitle, "Subtitle", True))
        layout.addSpacing(4)
        if scrollable:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            page.setObjectName("ScrollablePage")
            scroll.viewport().setObjectName("ScrollViewport")
            scroll.viewport().setAutoFillBackground(False)
            page.setAutoFillBackground(False)
            scroll.setWidget(page)
            self.pages.addWidget(scroll)
        else:
            self.pages.addWidget(page)
        return layout

    def _build_account_page(self):
        layout = self._page("从一声问候开始", "连接你的抖音账号，让日常联系更从容。", scrollable=True)
        card, inner = _card()
        header = QHBoxLayout()
        header.addWidget(_label("我的抖音账号", "CardTitle"))
        header.addStretch()
        self.account_badge = _label("尚未连接", "AccountBadge")
        header.addWidget(self.account_badge)
        inner.addLayout(header)
        self.account_description = _label(
            "打开抖音官网，点击官方登录入口并扫码。完成后关闭这次打开的浏览器窗口，程序会自动检查登录状态。",
            "Subtitle", True,
        )
        inner.addWidget(self.account_description)
        inner.addSpacing(12)
        buttons = QHBoxLayout()
        self.login_button = _button("打开官网登录", lambda: self._start_operation("login"), True)
        self.login_button.setObjectName("loginButton")
        self.check_button = _button("检查登录", lambda: self._start_operation("check"))
        self.logout_button = _button("退出账号", self._logout, danger=True)
        for button in [self.login_button, self.check_button, self.logout_button]:
            buttons.addWidget(button)
        buttons.addStretch()
        inner.addLayout(buttons)
        self.cancel_operation_button = _button("停止当前操作", self.bridge.cancel)
        self.cancel_operation_button.hide()
        inner.addWidget(self.cancel_operation_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(card)
        guide, guide_layout = _card()
        guide_layout.addWidget(_label("三步，连接你的抖音账号", "CardTitle"))
        for number, title, description in [
            ("01", "打开官网登录", "在打开的抖音官网点击“登录”，用手机抖音扫码。"),
            ("02", "完成后关闭浏览器", "确认官网显示已登录，再关闭这次打开的专用浏览器窗口。"),
            ("03", "自动读取聊天好友", "登录核验完成后自动读取一次已有会话，再为已读取的好友配置消息。"),
        ]:
            row = QHBoxLayout()
            badge = _label(number)
            badge.setStyleSheet("color: #E6785D; font-size: 16px; font-weight: 600;")
            badge.setFixedWidth(35)
            row.addWidget(badge)
            text = QVBoxLayout()
            text.addWidget(_label(title))
            text.addWidget(_label(description, "Muted", True))
            row.addLayout(text, 1)
            guide_layout.addLayout(row)
        layout.addWidget(guide)
        layout.addStretch()

    def _build_tasks_page(self):
        layout = self._page("好友与发送", "每个好友都可手动发送多次，也可每天定时发送一次；任务在后台执行。")
        layout.setSpacing(12)
        stats = QHBoxLayout()
        stats.setSpacing(14)
        self.stat_values = []
        for title in ["发送项", "定时开启", "近期已核对"]:
            card, card_layout = _card()
            card.setObjectName("StatCard")
            card_layout.setContentsMargins(19, 11, 19, 11)
            card_layout.setSpacing(6)
            card_layout.addWidget(_label(title, "Muted"))
            value = _label("0", "StatValue")
            card_layout.addWidget(value)
            self.stat_values.append(value)
            stats.addWidget(card)
        layout.addLayout(stats)
        directory = QHBoxLayout()
        self.friend_search = QLineEdit()
        self.friend_search.setObjectName("directorySearchInput")
        self.friend_search.setPlaceholderText("搜索好友或发送内容")
        self.friend_search.textChanged.connect(self.refresh)
        directory.addWidget(self.friend_search, 1)
        self.sync_button = _button("刷新聊天好友", lambda: self._start_operation("sync_friends"))
        self.sync_button.setObjectName("syncFriendsButton")
        directory.addWidget(self.sync_button)
        layout.addLayout(directory)
        self.friend_sync_status = _label("尚未读取已有会话。登录核验成功后会自动读取一次。", "Muted", True)
        self.friend_sync_status.setObjectName("friendSyncStatus")
        layout.addWidget(self.friend_sync_status)
        self.chat_scope_hint = _label(
            "只能为已读取的聊天好友设置发送。没找到好友时，请先在抖音里手动发一条消息，再回来刷新。",
            "Muted", True)
        self.chat_scope_hint.setObjectName("chatScopeHint")
        layout.addWidget(self.chat_scope_hint)
        controls = QHBoxLayout()
        self.add_button = _button("＋ 添加发送项", self._add_rule)
        self.add_button.setObjectName("addRuleButton")
        self.select_all_button = _button("全选当前列表", self._select_all_rules)
        self.clear_selection_button = _button("取消选择", self._clear_checked_rules)
        self.batch_send_button = _button("一键发送已选项目", self._send_selected, True)
        self.batch_send_button.setObjectName("sendSelectedButton")
        self.pause_button = _button("暂停全部", self._toggle_pause)
        for button in [self.add_button, self.select_all_button, self.clear_selection_button, self.batch_send_button]:
            controls.addWidget(button)
        controls.addStretch()
        controls.addWidget(self.pause_button)
        layout.addLayout(controls)
        self.batch_resume_button = _button("继续未完成批次", self._resume_batches)
        self.batch_resume_button.setObjectName("resumeBatchesButton")
        self.batch_resume_button.hide()
        layout.addWidget(self.batch_resume_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.rules_table = _table(["勾选", "好友", "发送内容", "每日时间", "定时", "今日结果", "操作"])
        self.rules_table.setObjectName("rulesTable")
        for index, width in enumerate([48, 135, 180, 80, 64, 112, 208]):
            self.rules_table.setColumnWidth(index, width)
        self.rules_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.rules_table.horizontalHeader().setStretchLastSection(False)
        self.rules_table.verticalHeader().setDefaultSectionSize(58)
        self.rules_table.doubleClicked.connect(self._edit_rule)
        self.rules_table.itemSelectionChanged.connect(self._update_selection_buttons)
        self.rules_table.itemChanged.connect(self._checked_changed)
        layout.addWidget(self.rules_table, 1)
        self.empty_tasks = _label("本次没有读到可用聊天好友。请先在抖音里手动发一条消息，再回来刷新。", "Muted", True)
        layout.addWidget(self.empty_tasks)
        edit_row = QHBoxLayout()
        self.edit_button = _button("编辑", self._edit_rule)
        self.toggle_button = _button("定时开启 / 关闭", self._toggle_rule)
        self.delete_button = _button("删除", self._delete_rule, danger=True)
        for button in [self.edit_button, self.toggle_button, self.delete_button]:
            edit_row.addWidget(button)
        edit_row.addStretch()
        layout.addLayout(edit_row)
        layout.addWidget(_label("北京时间 · 首次手动发送完成当天定时任务 · 可主动再次发送 · 结果不明先核对", "Muted", True))

    def _build_history_page(self):
        layout = self._page("执行记录", "展示最近 200 条计划与结果。结果待核对的消息不会自动重复发送。")
        row = QHBoxLayout()
        self.history_filter = QComboBox()
        for label, value in [("全部记录", "all"), ("网页刷新后可见", "persisted_in_chat"),
                             ("待人工核对", "uncertain"), ("异常与跳过", "attention")]:
            self.history_filter.addItem(label, value)
        self.history_filter.currentIndexChanged.connect(self._refresh_history)
        row.addWidget(self.history_filter)
        row.addStretch()
        row.addWidget(_button("刷新", self.refresh))
        layout.addLayout(row)
        self.history_table = _table(["好友 / 内容", "计划时间", "实际开始", "结果"])
        self.history_table.setObjectName("historyTable")
        self.history_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.history_table.setColumnWidth(1, 135)
        self.history_table.setColumnWidth(2, 135)
        self.history_table.itemSelectionChanged.connect(self._show_run_detail)
        layout.addWidget(self.history_table, 1)
        self.run_detail = _label("选择一条记录查看详情。", "StatusStrip", True)
        self.run_detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.run_detail.setMinimumHeight(70)
        layout.addWidget(self.run_detail)
        layout.addWidget(_label("“网页刷新后可见”仅为本地网页核对结果，不代表对方已读或手机送达。", "Muted", True))

    def _build_settings_page(self):
        layout = self._page("软件设置", "无需注册、充值或服务器。登录与任务数据由你掌握。", scrollable=True)
        card, inner = _card()
        inner.addWidget(_label("运行方式", "CardTitle"))
        self.startup_check = QCheckBox("登录电脑后自动启动")
        try:
            from .autostart import is_enabled
            self.startup_check.setChecked(is_enabled())
        except (ImportError, OSError):
            self.startup_check.setChecked(False)
        self.startup_check.toggled.connect(self._set_autostart)
        inner.addWidget(self.startup_check)
        self.notifications_check = QCheckBox("显示登录失效和执行异常通知")
        self.notifications_check.setChecked(bool(self.store.get_setting("notifications", True)))
        self.notifications_check.toggled.connect(lambda value: self.store.set_setting("notifications", value))
        inner.addWidget(self.notifications_check)
        inner.addWidget(_label("关闭窗口后隐藏到托盘；需要停止软件时，请从托盘菜单选择“退出”。", "Muted", True))
        layout.addWidget(card)
        policy, policy_layout = _card()
        policy_layout.addWidget(_label("发送规则", "CardTitle"))
        policy_layout.addWidget(_label("统一使用北京时间（UTC+8）", word_wrap=True))
        policy_layout.addWidget(_label("当天错过时间：原任务延迟执行\n跨天未发送：跳过该任务，不集中发送历史消息", "Muted", True))
        policy_layout.addWidget(_label("登录失效会停止派发任务。重新登录后，只处理当天仍有效的任务。", "Muted", True))
        layout.addWidget(policy)
        data, data_layout = _card()
        data_layout.addWidget(_label("本机数据", "CardTitle"))
        data_layout.addWidget(_label(f"软件版本：{__version__}", "Muted"))
        path = _label(str(self.paths.data_dir), "Muted", True)
        path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        data_layout.addWidget(path)
        data_layout.addWidget(_button("打开数据文件夹", self._open_data_dir), 0, Qt.AlignmentFlag.AlignLeft)
        data_layout.addWidget(_label("请勿分享 profiles 目录，其中包含你的抖音登录资料。", "Muted", True))
        layout.addWidget(data)
        layout.addStretch()

    def _build_tray(self):
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        self.tray.setToolTip("火花助手 · 本地运行")
        menu = QMenu(self)
        show = menu.addAction("打开火花助手")
        show.triggered.connect(self._show_window)
        self.tray_pause = menu.addAction("暂停全部任务")
        self.tray_pause.triggered.connect(self._toggle_pause)
        menu.addSeparator()
        menu.addAction("退出").triggered.connect(self.quit_app)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    def _navigate(self, index):
        self.pages.setCurrentIndex(index)
        for position, button in enumerate(self.nav):
            button.setChecked(position == index)

    def refresh(self):
        rules = self.store.list_rules()
        contacts = self.store.list_contacts()
        contacts_by_id = {friend["id"]: friend for friend in contacts}
        names = {}
        for friend in contacts:
            names.setdefault(_friend_name(friend), set()).add(friend["platform_id"])
        selected = self._selected_rule_id()
        states = self.store.today_rule_states()
        self._checked_rule_ids.intersection_update(rule["id"] for rule in rules)
        configured = {rule.get("contact_id") for rule in rules}
        rows = [(rule, contacts_by_id.get(rule.get("contact_id"))) for rule in rules]
        rows.extend((None, friend) for friend in contacts if friend["id"] not in configured)
        query = self.friend_search.text().strip().casefold()
        rows = [(rule, friend) for rule, friend in rows if not query or query in
                (" ".join([_text(rule.get("recipient")) if rule else _friend_name(friend),
                           _text(rule.get("message")) if rule else "", _friend_reference(friend or {})])).casefold()]
        self._populating_rules = True
        self.rules_table.setRowCount(len(rows))
        for row, (rule, friend) in enumerate(rows):
            rule_id = rule["id"] if rule else None
            state = states.get(rule_id, {})
            run = state.get("latest")
            retrying = bool(run and run["status"] == "failed" and run.get("submission") == "not_submitted")
            needs_confirmation = bool(state.get("requires_confirmation"))
            name = _friend_name(friend) if friend else rule["recipient"]
            if friend and len(names.get(name, set())) > 1:
                reference = _friend_reference(friend)
                name += "\nID …" + reference[-8:]
            reason = self._rule_send_reason(rule, state) if rule else "请先填写消息内容。"
            if rule and reason:
                self._checked_rule_ids.discard(rule_id)
            values = ["", name, rule["message"] if rule else "尚未填写消息", 
                      (rule.get("send_time") or "未设置") if rule else "未设置", "",
                      STATUS_LABELS.get(run["status"], run["status"]) if run else
                      (rule.get("blocked_reason") or "尚未发送") if rule else
                      (friend.get("blocked_reason") or "尚未配置"), ""]
            for col, value in enumerate(values):
                item = QTableWidgetItem(_text(value))
                item.setData(Qt.ItemDataRole.UserRole, rule_id)
                item.setToolTip(_friend_reference(friend or {}) if col == 1 else _text(value))
                if col == 0:
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable |
                                  (Qt.ItemFlag.ItemIsUserCheckable if rule and not reason else Qt.ItemFlag.NoItemFlags))
                    if rule:
                        item.setCheckState(Qt.CheckState.Checked if rule_id in self._checked_rule_ids else Qt.CheckState.Unchecked)
                    item.setToolTip(reason or "勾选后可一键发送此项。")
                    if retrying and not reason:
                        item.setToolTip(f'重试本日原消息：{run["message"]}')
                    elif needs_confirmation and not reason:
                        item.setToolTip("上一条结果不明确，批量发送前会请你核对并确认。")
                elif col == 2 and retrying:
                    item.setToolTip(f'当前配置：{value}\n本日重试仍使用原消息：{run["message"]}')
                self.rules_table.setItem(row, col, item)
            schedule = QCheckBox()
            schedule.setObjectName(f"schedule-{rule_id}" if rule else "unconfiguredSchedule")
            schedule.setChecked(bool(rule and rule["enabled"]))
            schedule.setEnabled(bool(rule) and not self._rule_is_busy(rule_id))
            schedule.setToolTip("只控制定时发送，关闭后仍可手动发送。")
            schedule.toggled.connect(lambda enabled, rid=rule_id: self._set_schedule(rid, enabled))
            schedule_box = QWidget()
            schedule_layout = QHBoxLayout(schedule_box)
            schedule_layout.setContentsMargins(12, 0, 0, 0)
            schedule_layout.addWidget(schedule)
            self.rules_table.setCellWidget(row, 4, schedule_box)
            actions = QWidget()
            action_layout = QHBoxLayout(actions)
            action_layout.setContentsMargins(4, 3, 4, 3)
            action_layout.setSpacing(5)
            if rule:
                send_text = ("核对后再次发送" if needs_confirmation else "重试原消息" if retrying else
                             "再次发送" if run and run["status"] == "persisted_in_chat" else "立即发送")
                send = _button(send_text, lambda checked=False, rid=rule_id: self._send_rule(rid))
                send.setObjectName(f"send-{rule_id}")
                send.setEnabled(not bool(reason) and self._session_validated)
                send.setToolTip(reason or ("先核对上一条消息，再主动创建一条新消息。" if needs_confirmation else
                                          f'仍发送本日原消息：{run["message"]}' if retrying else
                                          "在后台发送此项内容。首次手动发送会完成当天定时任务。"))
                edit = _button("编辑", lambda checked=False, rid=rule_id: self._edit_rule_id(rid))
                edit.setEnabled(not self._rule_is_busy(rule_id))
            else:
                send = _button("填写并发送", lambda checked=False, contact=friend: self._rule_dialog(contact=contact, immediate=True))
                edit = _button("设置定时", lambda checked=False, contact=friend: self._rule_dialog(contact=contact, scheduled=True))
                for button in (send, edit):
                    button.setEnabled(bool(friend.get("sendable")))
                    button.setToolTip(friend.get("blocked_reason") or "")
            for button in (send, edit):
                button.setStyleSheet("padding: 7px 8px; font-size: 12px;")
                action_layout.addWidget(button)
            self.rules_table.setCellWidget(row, 6, actions)
            if rule_id and rule_id == selected:
                self.rules_table.selectRow(row)
        self._populating_rules = False
        self.empty_tasks.setVisible(not rows)
        self.empty_tasks.setText("没有匹配的好友或发送项，请调整或清空搜索。" if query else
                                "本次没有读到可用聊天好友。请先在抖音里手动发一条消息，再回来刷新。未加载的会话可能未读到。")
        self.stat_values[0].setText(str(len(rules)))
        self.stat_values[1].setText(str(sum(bool(rule["enabled"]) for rule in rules)))
        sync = self.store.get_friend_sync()
        sync_status = SYNC_LABELS.get(sync.get("completeness"), sync.get("completeness") or "尚未读取")
        self.friend_sync_status.setText(
            f'本次读取聊天好友：{sync.get("count", 0)} 人 · 读取状态：{sync_status} · 最近读取：{_time_text(sync.get("last_sync"))}'
            + (f'\n{sync.get("detail")}' if sync.get("detail") else ""))
        self._resumable_batches = self.scheduler.list_resumable_batches()
        remaining = sum(batch.get("pending_count", 0) for batch in self._resumable_batches)
        self.batch_resume_button.setVisible(bool(remaining))
        self.batch_resume_button.setText(f"继续未完成批次（{remaining} 项）")
        self._refresh_history()
        self._update_selection_buttons()
        self._refresh_account()
        self._refresh_pause()

    def _rule_is_busy(self, rule_id):
        return bool(rule_id and (rule_id in self._held_rule_ids or
                    (self._active_run and self._active_run.get("rule_id") == rule_id)))

    def _rule_send_reason(self, rule, state=None):
        if not rule:
            return "请先填写消息内容。"
        if self._rule_is_busy(rule["id"]):
            return "这条发送项正在编辑或执行。"
        if not rule.get("sendable"):
            return rule.get("blocked_reason") or "请先核对并关联已读取的聊天好友。"
        active = (state or {}).get("active")
        if active and (active["status"] == "running" or active.get("trigger") == "manual"):
            return "此项正在排队或发送，请等待完成后再发下一条。"
        return ""

    def _checked_changed(self, item):
        if self._populating_rules or item.column() != 0:
            return
        rule_id = item.data(Qt.ItemDataRole.UserRole)
        if rule_id:
            if item.checkState() == Qt.CheckState.Checked:
                self._checked_rule_ids.add(rule_id)
            else:
                self._checked_rule_ids.discard(rule_id)
        self._update_selection_buttons()

    def _select_all_rules(self):
        for row in range(self.rules_table.rowCount()):
            item = self.rules_table.item(row, 0)
            if item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                item.setCheckState(Qt.CheckState.Checked)

    def _clear_checked_rules(self):
        self._checked_rule_ids.clear()
        self.refresh()

    def _refresh_history(self):
        if not hasattr(self, "history_table"):
            return
        runs = self.store.list_runs()
        self.stat_values[2].setText(str(sum(
            run["status"] == "persisted_in_chat"
            for run in runs
        )))
        selected = self.history_filter.currentData()
        if selected == "attention":
            runs = [run for run in runs if run["status"] in {"failed", "missed", "login_required", "unavailable"}]
        elif selected != "all":
            runs = [run for run in runs if run["status"] == selected]
        self._visible_runs = runs
        self.history_table.setRowCount(len(runs))
        for row, run in enumerate(runs):
            values = [f'{run["recipient"]}  ·  {run["message"]}', _time_text(run.get("scheduled_at")),
                      _time_text(run.get("started_at")), STATUS_LABELS.get(run["status"], run["status"])]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                if col == 3:
                    item.setForeground(QColor("#42856F" if run["status"] == "persisted_in_chat" else "#B66C42"))
                self.history_table.setItem(row, col, item)
        self._show_run_detail()

    def _refresh_account(self):
        ready = bool(self.store.get_setting("account_ready", False)) and self._session_validated
        state = "ready" if ready else self._account_state
        labels = {
            "unverified": "登录状态未验证", "checking": "正在检查登录", "ready": "已连接",
            "login_required": "需要登录", "unavailable": "登录状态待核验",
            "logging_in": "等待官网登录", "logging_out": "正在退出账号", "logged_out": "已退出账号",
        }
        self.account_badge.setText(labels.get(state, "登录状态待核验"))
        login_help = "打开抖音官网，点击官方登录入口并扫码。完成后关闭这次打开的浏览器窗口，程序会自动检查登录状态。"
        descriptions = {
            "ready": "当前登录已核验。程序会自动读取一次已有会话；到“好友与发送”为已读取的聊天好友配置消息。",
            "checking": self._account_detail or "正在加载聊天页面并核验登录，请稍候。",
            "logging_in": login_help,
            "logging_out": "正在清除本机保存的登录资料，请稍候。",
            "logged_out": "已退出抖音账号。" + login_help,
            "unverified": "尚未检查本机登录状态；已有登录资料时，可以先点击“检查登录”。\n" + login_help,
            "login_required": (self._account_detail + "\n" if self._account_detail else "") + login_help,
            "unavailable": (self._account_detail + "\n" if self._account_detail else "") +
                           "本次未能确认登录状态，暂不派发任务。可点击“检查登录”重新核验。",
        }
        if ready and self.store.get_setting("login_review_required", False):
            descriptions["ready"] += "\n已有任务保持暂停。请重新关联当前账号的好友，再到“好友与发送”点击“核对后继续”。"
        self.account_description.setText(descriptions.get(state, descriptions["unavailable"]))
        self.login_button.setText("重新打开官网登录" if ready else "打开官网登录")
        self.sync_button.setEnabled(ready and not self.bridge.busy)

    def _set_account_state(self, state, detail=""):
        self._account_state = state
        self._account_detail = detail
        self._session_validated = state == "ready"
        self.store.set_setting("account_ready", self._session_validated)

    def _refresh_pause(self):
        paused = bool(self.store.get_setting("paused", False))
        review_required = bool(self.store.get_setting("login_review_required", False))
        resume_text = "核对后继续" if review_required else "继续全部"
        self.pause_button.setText(resume_text if paused else "暂停全部")
        self.tray_pause.setText(("核对好友后继续任务" if review_required else "继续全部任务") if paused else "暂停全部任务")
        ready = bool(self.store.get_setting("account_ready", False)) and self._session_validated
        account_hint = {
            "unverified": "登录未验证", "checking": "正在检查登录", "login_required": "需要登录",
            "unavailable": "登录待核验", "logging_in": "等待官网登录", "logged_out": "已退出账号",
        }.get(self._account_state, "登录待核验")
        state = ("任务暂停，待核对好友" if review_required else "全部已暂停") if paused else (
            "正在检查登录" if self._account_state == "checking" else
            ("正在处理" if self.bridge.busy else ("调度运行中" if ready else account_hint))
        )
        self.sidebar_state.setText(f"●  {state}")
        self.tray.setToolTip(f"火花助手 · {state}")

    def _selected_rule_id(self):
        row = self.rules_table.currentRow()
        item = self.rules_table.item(row, 0) if row >= 0 else None
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _selected_rule(self):
        rule_id = self._selected_rule_id()
        return next((rule for rule in self.store.list_rules() if rule["id"] == rule_id), None)

    def _update_selection_buttons(self):
        selected = self._selected_rule_id()
        active = self._active_run and self._active_run.get("rule_id") == selected
        held = selected in self._held_rule_ids
        enabled = selected is not None and not active and not held
        for button in [self.edit_button, self.toggle_button, self.delete_button]:
            button.setEnabled(enabled)
            button.setToolTip("这条任务正在执行，请等待当前操作结束。" if active else
                              ("正在编辑或确认这条任务。" if held else ""))
        if hasattr(self, "batch_send_button"):
            count = len(self._checked_rule_ids)
            self.batch_send_button.setText(f"一键发送已选（{count}）" if count else "一键发送已选项目")
            self.batch_send_button.setEnabled(bool(count) and self._session_validated)
            self.clear_selection_button.setEnabled(bool(count))

    @contextmanager
    def _hold_dispatch(self):
        """Account exit and app-exit confirmations block new dispatch only."""
        self._dispatch_hold_depth += 1
        try:
            yield
        finally:
            self._dispatch_hold_depth -= 1

    @contextmanager
    def _hold_rule(self, rule_id):
        """Editing/deleting one rule must not stop unrelated friends' tasks."""
        if rule_id is None:
            yield
            return
        was_held = rule_id in self._held_rule_ids
        self._held_rule_ids.add(rule_id)
        self._update_selection_buttons()
        try:
            yield
        finally:
            if not was_held:
                self._held_rule_ids.discard(rule_id)
            self._update_selection_buttons()

    def _add_rule(self):
        self._rule_dialog()

    def _edit_rule(self):
        rule = self._selected_rule()
        if rule and not (self._active_run and self._active_run.get("rule_id") == rule["id"]):
            self._rule_dialog(rule)

    def _edit_rule_id(self, rule_id):
        rule = next((item for item in self.store.list_rules() if item["id"] == rule_id), None)
        if rule and not self._rule_is_busy(rule_id):
            self._rule_dialog(rule)

    def _rule_dialog(self, rule=None, *, contact=None, scheduled=False, immediate=False):
        rule_id = rule["id"] if rule else None
        queued = self.store.today_runs().get(rule_id)
        if self._active_run and self._active_run.get("rule_id") == rule_id:
            return
        with self._hold_rule(rule_id):
            dialog = RuleDialog(self.store.list_contacts(), rule, self, contact=contact,
                                scheduled=scheduled, immediate=immediate)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            try:
                saved_id = self.store.save_rule(**dialog.values(), rule_id=rule_id)
                text = "发送项已保存。可立即发送；开启定时后从下一次计划时间开始执行。"
                if self.store.get_setting("paused", False):
                    text = "任务已保存，当前全部任务仍暂停；手动恢复后才会执行。"
                if queued and queued.get("status") == "pending" and queued.get("trigger") == "manual":
                    text += "已排队的手动发送仍使用提交时的内容。"
                self.status_strip.setText(text)
                self.refresh()
            except (ValueError, RuntimeError) as exc:
                QMessageBox.warning(self, "任务未保存", str(exc))
                return
        if immediate:
            self._send_rule(saved_id)

    def _set_schedule(self, rule_id, enabled):
        if not rule_id or self._rule_is_busy(rule_id):
            return
        rule = next((item for item in self.store.list_rules() if item["id"] == rule_id), None)
        if not rule:
            return
        if enabled and not rule.get("send_time"):
            self._rule_dialog({**rule, "enabled": True})
            self.refresh()
            return
        try:
            self.store.set_rule_enabled(rule_id, enabled)
        except (ValueError, RuntimeError) as exc:
            self.status_strip.setText(str(exc))
        self.refresh()

    def _send_rule(self, rule_id):
        self._queue_manual([rule_id])

    def _send_selected(self):
        # Visible table order is the submitted order, with hidden selections
        # following in stable rule order. A set never determines batch order.
        visible = [self.rules_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
                   for row in range(self.rules_table.rowCount())]
        ordered = list(dict.fromkeys(visible + [rule["id"] for rule in self.store.list_rules()]))
        self._queue_manual([rule_id for rule_id in ordered if rule_id in self._checked_rule_ids])

    def _queue_manual(self, rule_ids):
        if not self._session_validated or not self.store.get_setting("account_ready", False):
            self.status_strip.setText("请先检查登录状态，再提交发送。")
            return
        states = self.store.today_rule_states()
        rules = {rule["id"]: rule for rule in self.store.list_rules()}
        allowed = [rule_id for rule_id in dict.fromkeys(rule_ids) if rule_id in rules and
                   not self._rule_send_reason(rules[rule_id], states.get(rule_id))]
        if not allowed:
            self.status_strip.setText("没有可加入队列的发送项；正在排队、编辑或发送的项目不会重复提交。")
            return
        uncertain_runs = [states[rule_id]["latest"] for rule_id in allowed
                          if states.get(rule_id, {}).get("requires_confirmation")]
        # Hold only selected rules while the confirmation's nested event loop
        # is open; unrelated timers remain free to run. Acknowledgements name
        # exact prior run IDs, so a stale confirmation cannot authorize a later
        # uncertain result.
        with ExitStack() as holds:
            for rule_id in allowed:
                holds.enter_context(self._hold_rule(rule_id))
            if uncertain_runs:
                examples = "\n".join(f'• {run["recipient"]}：{run["message"][:80]}' for run in uncertain_runs[:6])
                if len(uncertain_runs) > 6:
                    examples += f"\n另有 {len(uncertain_runs) - 6} 项，请一并核对执行记录。"
                answer = QMessageBox.question(
                    self, "核对后再次发送",
                    f"以下 {len(uncertain_runs)} 项上次可能已经发送：\n{examples}\n\n"
                    "请先在抖音聊天记录里核对。继续会按当前配置发送一条新消息，可能与上一条重复。\n"
                    "你已核对，并确定再次发送本次选中的消息吗？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    self.status_strip.setText("本次未提交任何新消息，请核对后再发送。")
                    return
            try:
                outcome = self.scheduler.enqueue_manual(
                    allowed, action_id=str(uuid4()),
                    acknowledge_uncertain_ids=tuple(run["id"] for run in uncertain_runs),
                )
            except (ValueError, RuntimeError) as exc:
                self.status_strip.setText(str(exc))
                return
        accepted = outcome.get("accepted", [])
        rejected = outcome.get("rejected", [])
        self._checked_rule_ids.difference_update(run["rule_id"] for run in accepted)
        detail = f"已加入后台发送队列 {len(accepted)} 项。"
        if rejected:
            detail += f"未加入 {len(rejected)} 项：" + "；".join(dict.fromkeys(
                item.get("detail", "此项不可发送") for item in rejected))
        if self.store.get_setting("paused", False):
            detail += "全部发送已暂停，点击“继续全部”后才会执行。"
        self.status_strip.setText(detail)
        self.refresh()

    def _resume_batches(self):
        if not self._session_validated:
            self.status_strip.setText("请先检查当前账号登录状态。")
            return
        count = 0
        try:
            for batch in self.scheduler.list_resumable_batches():
                count += self.scheduler.resume_batch(batch["id"])
        except (ValueError, RuntimeError) as exc:
            self.status_strip.setText(str(exc))
            self.refresh()
            return
        self.status_strip.setText(f"已恢复当天未开始的 {count} 项；跨天或结果未知的项目不会再发送。" +
                                  ("全局暂停仍生效。" if self.store.get_setting("paused", False) else ""))
        self.refresh()

    def _toggle_rule(self):
        rule = self._selected_rule()
        if rule and rule["id"] not in self._held_rule_ids and not (
            self._active_run and self._active_run.get("rule_id") == rule["id"]
        ):
            self._set_schedule(rule["id"], not rule["enabled"])

    def _delete_rule(self):
        rule = self._selected_rule()
        if not rule or rule["id"] in self._held_rule_ids or (
            self._active_run and self._active_run.get("rule_id") == rule["id"]
        ):
            return
        with self._hold_rule(rule["id"]):
            if QMessageBox.question(
                self, "删除任务", f'删除发给“{rule["recipient"]}”的这条任务？历史执行记录将保留。',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
            ) == QMessageBox.StandardButton.Yes:
                self.store.delete_rule(rule["id"])
                self.refresh()

    def _toggle_pause(self):
        paused = not bool(self.store.get_setting("paused", False))
        if not paused and self.store.get_setting("login_review_required", False):
            if not (self._session_validated and self.store.get_setting("account_ready", False)):
                self.status_strip.setText("请先完成登录核验，再检查当前账号及任务好友后恢复。已有任务仍暂停。")
                return
            self.store.set_setting("login_review_required", False)
        self.store.set_setting("paused", paused)
        self.status_strip.setText("已暂停新任务；正在执行的操作会继续完成。" if paused else "已恢复调度，只处理当天仍有效的任务。")
        self._refresh_pause()
        self._refresh_account()

    def _logout(self):
        if self.bridge.busy:
            return
        with self._hold_dispatch():
            if QMessageBox.question(
                self, "退出抖音账号", "将清除本机保存的抖音登录资料并停止派发任务，任务配置和执行记录会保留。继续吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
            ) == QMessageBox.StandardButton.Yes:
                self._start_operation("logout")

    def _start_operation(self, command):
        if self._quitting or self.bridge.busy:
            return
        descriptions = {"login": "正在打开抖音官网。请点击官网登录并扫码，完成后关闭本次浏览器的全部窗口和标签页，程序将自动检查。", "check": "正在后台核验本机保存的登录状态…",
                        "contacts": "正在后台读取局部聊天列表…", "sync_friends": "正在后台读取已有聊天会话，最多等待五分钟…",
                        "logout": "正在清除本机登录资料…"}
        if command in {"login", "logout"}:
            # The official window permits account switching. Without a stable
            # account identity, prior rules need an explicit review before reuse.
            if self.store.list_rules():
                self.store.set_setting("paused", True)
                self.store.set_setting("login_review_required", True)
                descriptions[command] += "已有任务已暂停；核验后请重新关联好友并手动恢复。"
            self.store.invalidate_account_epoch("重新登录或退出账号，原队列及好友绑定需要核对。")
            self._friend_sync_attempted = False
            self._pending_friend_sync = False
        if command == "sync_friends":
            if not self._session_validated:
                self.status_strip.setText("请先完成登录核验，再读取聊天好友。")
                return
            self._friend_sync_attempted = True
            self._pending_friend_sync = False
            self._sync_epoch = self.store.get_account()["account_epoch"]
            self.friend_sync_status.setText("正在后台读取已有会话… 最多五分钟；只显示已读取且身份明确的聊天好友。")
        self.status_strip.setText(descriptions.get(command, "正在处理…"))
        if command in {"login", "logout", "check"}:
            states = {"login": "logging_in", "logout": "logging_out", "check": "checking"}
            self._set_account_state(states[command], descriptions[command])
            self._refresh_account()
            self._refresh_pause()
        try:
            self.bridge.start(command)
        except (RuntimeError, OSError) as exc:
            if command != "sync_friends":
                self._set_account_state("unavailable", str(exc))
            else:
                self.store.apply_friend_sync({"friends": [], "coverage": {"status": "failed"},
                                              "detail": str(exc)}, account_epoch=self._sync_epoch)
                self.refresh()
            self.status_strip.setText(str(exc))
            self._refresh_account()
            self._refresh_pause()

    def _on_busy(self, busy):
        for button in [self.login_button, self.check_button, self.logout_button]:
            button.setEnabled(not busy)
        self.cancel_operation_button.setVisible(busy)
        self._update_selection_buttons()
        self._refresh_account()
        self._refresh_pause()

    def _on_progress(self, message):
        self.status_strip.setText(message)
        if self.bridge.command == "sync_friends":
            self.friend_sync_status.setText(f"正在后台读取已有会话… {message}")

    def _on_result(self, result):
        command = result.get("command")
        status = result.get("status", "failed")
        detail = str(result.get("detail") or "操作已结束。")
        if command == "sync_friends":
            try:
                metadata = self.store.apply_friend_sync(result, account_epoch=self._sync_epoch)
                if metadata.get("account_changed"):
                    self.store.set_setting("paused", True)
                    detail += "检测到账号变化，旧队列已取消；请重新核对好友并恢复。"
            except (ValueError, RuntimeError) as exc:
                detail = str(exc)
            if status == "login_required":
                self._set_account_state("login_required", detail)
            elif result.get("scope") == "account" and result.get("account_validated") is False:
                self._set_account_state("unavailable", detail)
            elif not self.store.get_account()["verified"]:
                self._set_account_state("unavailable", detail)
            elif self.store.get_setting("account_ready", False) is False:
                self._set_account_state("ready", detail)
            self.status_strip.setText(detail)
            self.refresh()
            return
        task_failure = (
            command == "send" and status == "failed" and result.get("scope") == "task"
            and result.get("submission") == "not_submitted" and result.get("account_validated") is True
        )
        account = result.get("account") or {}
        current_account = self.store.get_account()
        changed_account = (command == "check" and status == "ready" and account.get("verification") == "verified"
                           and account.get("platform_id") and current_account.get("platform_id")
                           and account["platform_id"] != current_account["platform_id"])
        account_mismatch = command == "send" and result.get("code") in {"account_mismatch", "account_identity_mismatch"}
        if changed_account or account_mismatch:
            self.store.invalidate_account_epoch("检测到抖音账号变化，旧队列已取消，需重新关联好友。")
            self.store.set_setting("paused", True)
            self._friend_sync_attempted = False
            self._pending_friend_sync = False
            detail += "检测到账号变化，已停止旧账号队列。"
        if command == "login" and status == "login_closed":
            # Closing the official page is not evidence of a valid login.
            # Let the completed worker release its profile before checking.
            self._set_account_state("checking", "登录窗口已关闭，正在检查登录状态…")
            self.status_strip.setText("登录窗口已关闭，正在检查登录状态…")
            self.refresh()
            QTimer.singleShot(250, self._check_after_login_closed)
            return
        if command == "send" and self._active_run:
            if (status in {"login_required", "unavailable"}
                    and result.get("scope") == "account" and result.get("submission") == "not_submitted"):
                # Only an explicit pre-submission account failure may be retried.
                self.scheduler.defer(self._active_run["id"], detail,
                                     submission="not_submitted", code=result.get("code", ""))
            else:
                terminal_status = ("uncertain" if status in {"login_required", "unavailable"}
                                   or result.get("submission") == "possible" else status)
                self.scheduler.complete(self._active_run["id"], terminal_status, detail,
                                        submission=result.get("submission"), code=result.get("code", ""))
            self._active_run = None
        if status == "ready":
            self._set_account_state("ready", detail)
            if command in {"check", "login"} and not self._friend_sync_attempted:
                self._pending_friend_sync = True
            if self.store.get_setting("login_review_required", False):
                detail += "已有任务保持暂停。请核对当前账号及任务好友，再点击“核对后继续”。"
        elif status == "login_required":
            self._set_account_state("login_required", detail)
        elif status in {"unavailable", "failed"} and not task_failure:
            self._set_account_state("unavailable", detail)
        elif status == "logged_out":
            self._set_account_state("logged_out", detail)
        if status == "logged_out":
            self.store.set_setting("paused", True)
            if self.store.list_rules():
                self.store.set_setting("login_review_required", True)
            detail += "任务已全部暂停；重新登录后请先检查收件人，再继续任务。"
        # Bare chat names do not identify recipients. Only the structured
        # sync_friends result above may update the loaded-chat directory.
        self.status_strip.setText(detail)
        if status in {"login_required", "uncertain", "failed", "unavailable"}:
            title = "请重新登录抖音" if status == "login_required" else "有一条执行结果需要关注"
            self._notify(title, detail)
        self.refresh()

    def _check_after_login_closed(self):
        if not self._quitting and not self.bridge.busy:
            self._start_operation("check")

    def _tick(self):
        if self._quitting or self.bridge.busy or self._dispatch_hold_depth:
            return
        try:
            if self._pending_friend_sync and self._session_validated:
                self._start_operation("sync_friends")
                return
            run = (self.scheduler.tick(exclude_rule_ids=tuple(self._held_rule_ids))
                   if self._held_rule_ids else self.scheduler.tick())
            if run:
                self._active_run = run
                self.bridge.start("send", recipient=run["recipient"], message=run["message"], request_id=run["id"],
                                  scheduled_date=run.get("scheduled_date") or run["scheduled_at"][:10],
                                  **{key: run.get(key) for key in ("contact_id", "platform_id", "profile_url", "conversation_id", "account_id", "account_epoch")})
                self.status_strip.setText(f'正在后台发送给“{run["recipient"]}”…')
            self._refresh_pause()
            if run or self.pages.currentIndex() in {1, 2}:
                self.refresh()
        except Exception as exc:
            if self._active_run:
                self.scheduler.complete(self._active_run["id"], "uncertain", "启动执行时出现异常，请核对后处理。")
                self._active_run = None
            self.store.set_setting("paused", True)
            self.status_strip.setText(f"调度已暂停：{exc}")
            self._refresh_pause()

    def _show_run_detail(self):
        if not hasattr(self, "run_detail"):
            return
        row = self.history_table.currentRow()
        if row < 0 or row >= len(self._visible_runs):
            self.run_detail.setText("选择一条记录查看详情。")
            return
        run = self._visible_runs[row]
        self.run_detail.setText(
            f'好友：{run["recipient"]}    内容：{run["message"]}\n'
            f'来源：{"定时" if run.get("trigger", "scheduled") == "scheduled" else "手动"}    '
            f'提交阶段：{SUBMISSION_LABELS.get(run.get("submission"), "尚不明确")}\n'
            f'结果：{STATUS_LABELS.get(run["status"], run["status"])}    结束：{_time_text(run.get("finished_at"))}\n'
            f'{run.get("detail") or "暂无补充信息。"}'
        )

    def _set_autostart(self, enabled):
        try:
            from .autostart import set_enabled
            set_enabled(enabled)
            self.store.set_setting("autostart", enabled)
        except Exception as exc:
            self.startup_check.blockSignals(True)
            self.startup_check.setChecked(not enabled)
            self.startup_check.blockSignals(False)
            QMessageBox.warning(self, "开机自启未更改", str(exc))

    def _open_data_dir(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.paths.data_dir)))

    def _notify(self, title, message):
        if self.store.get_setting("notifications", True) and self.tray.isVisible():
            self.tray.showMessage(title, message[:350], QSystemTrayIcon.MessageIcon.Information, 7000)

    def _show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _tray_activated(self, reason):
        if reason in {QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick}:
            self._show_window()

    def closeEvent(self, event):
        if self._quitting:
            event.accept()
            return
        if not self.tray.isVisible():
            event.ignore()
            self.quit_app()
            return
        event.ignore()
        self.hide()
        if not self.store.get_setting("tray_notice_shown", False):
            self.tray.showMessage("火花助手仍在运行", "窗口已隐藏到托盘，任务会继续执行。要停止软件，请在托盘菜单选择“退出”。")
            self.store.set_setting("tray_notice_shown", True)

    def quit_app(self):
        with self._hold_dispatch():
            if self.bridge.busy and QMessageBox.question(
                self, "退出火花助手", "当前操作仍在进行。退出会停止操作，发送结果可能需要人工核对。确定退出？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
            ) != QMessageBox.StandardButton.Yes:
                return
            self._quitting = True
        self.timer.stop()
        if self._active_run:
            self.scheduler.complete(self._active_run["id"], "uncertain", "程序在执行期间退出；请核对消息，不会自动重发。")
            self._active_run = None
        self.bridge.shutdown()
        self.tray.hide()
        self.close()
        QApplication.instance().quit()
