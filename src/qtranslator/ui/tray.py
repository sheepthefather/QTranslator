"""托盘图标与全局热键。

热键用 ``pynput``：它在 Windows 走 ``SetWindowsHookEx``，X11 走 XRecord，
都是成熟的路径。**Wayland 下 pynput 不可用**——这正是托盘菜单必须存在的理由：
它是不依赖全局热键的兜底入口。任何用热键的功能都要有托盘等价物。

注意 pynput 的回调跑在它自己的监听线程里，不能直接碰 Qt 控件，
必须通过信号跨线程投递到主线程。
"""

from __future__ import annotations

import logging
import sys

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

log = logging.getLogger(__name__)


def make_tray_icon() -> QIcon:
    """程序化画一个托盘图标，省掉打包时处理资源文件。"""
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(64, 160, 255), 2))
    painter.drawRect(5, 7, 22, 17)
    painter.drawLine(5, 13, 27, 13)
    painter.drawText(11, 26, "译")
    painter.end()

    return QIcon(pixmap)


class HotkeyManager(QObject):
    """全局热键。把平台监听线程的回调转成 Qt 信号。"""

    triggered = Signal()

    def __init__(self, combo: str = "<ctrl>+<alt>+q", parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._combo = combo
        self._listener = None

    @property
    def combo(self) -> str:
        return self._combo

    def start(self) -> bool:
        """注册热键。返回是否成功——失败时调用方应提示用户改用托盘菜单。"""
        if sys.platform == "linux" and _is_wayland():
            log.warning(
                "检测到 Wayland 会话：pynput 全局热键不可用，请通过托盘菜单触发。"
                "（Wayland 需要走 GlobalShortcuts portal，属于阶段三四的工作）"
            )
            return False

        try:
            from pynput import keyboard

            self._listener = keyboard.GlobalHotKeys({self._combo: self._on_hotkey})
            self._listener.daemon = True
            self._listener.start()
            log.info("全局热键已注册: %s", self._combo)
            return True
        except Exception:  # noqa: BLE001 — 权限/依赖缺失都可能，一律降级到托盘
            log.exception("注册全局热键失败，将只能通过托盘菜单触发")
            self._listener = None
            return False

    def stop(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:  # noqa: BLE001
                log.debug("停止热键监听时出错", exc_info=True)
            self._listener = None

    def _on_hotkey(self) -> None:
        # 运行在 pynput 的监听线程。emit 是线程安全的，Qt 会排队到主线程执行。
        self.triggered.emit()


def _is_wayland() -> bool:
    import os

    return os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland" or bool(
        os.environ.get("WAYLAND_DISPLAY")
    )


class TrayController(QObject):
    """托盘图标 + 右键菜单。"""

    capture_requested = Signal()
    quit_requested = Signal()

    def __init__(self, hotkey_label: str | None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._tray = QSystemTrayIcon(make_tray_icon(), self)
        self._tray.setToolTip("QTranslator — 截图翻译")

        menu = QMenu()

        self._act_capture = QAction("截图 OCR", menu)
        self._act_capture.triggered.connect(self.capture_requested.emit)
        menu.addAction(self._act_capture)

        menu.addSeparator()

        hint = QAction(
            f"热键：{hotkey_label}" if hotkey_label else "热键不可用（Wayland 请用本菜单）",
            menu,
        )
        hint.setEnabled(False)
        menu.addAction(hint)

        menu.addSeparator()

        act_quit = QAction("退出", menu)
        act_quit.triggered.connect(self.quit_requested.emit)
        menu.addAction(act_quit)

        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_activated)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # 左键单击也触发截图，符合截图工具的普遍习惯
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.capture_requested.emit()

    def show(self) -> None:
        self._tray.show()

    def hide(self) -> None:
        self._tray.hide()

    def notify(self, title: str, message: str) -> None:
        if QSystemTrayIcon.supportsMessages():
            self._tray.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 3000)
