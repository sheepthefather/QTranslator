"""应用装配。

把截图 → 选区 → OCR → 预览这条链串起来，并管理跨窗口的生命周期。

一个容易踩的坑：**选区窗必须等截图完成后再显示**，否则用户会看到它自己
出现在底图里（自拍效应）。所以顺序是「先抓图 → 再显示覆盖窗」，而不是
「显示暗化层 → 抓图」。这也是方案 3 比全屏浮层简单的地方——浮层方案里
这个顺序问题很难处理，因为浮层本身就是要显示的东西。
"""

from __future__ import annotations

import logging
import sys

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from .capture import Capture, ScreenCapturer
from .config import Config, load as load_config
from .coords import PixelRect, ViewTransform
from .ocr import OcrEngine
from .translate import build_translator
from .ui.preview import PreviewWindow
from .ui.selector import SelectionOverlay
from .ui.tray import HotkeyManager, TrayController

log = logging.getLogger(__name__)

DEFAULT_HOTKEY = "<ctrl>+<alt>+q"


class QTranslatorApp(QObject):
    """应用主控。持有长生命周期对象（截图器、OCR 引擎、翻译器），窗口按需创建。"""

    def __init__(self, qt_app: QApplication, config: Config | None = None) -> None:
        super().__init__()
        self._qt_app = qt_app
        self._config = config or load_config()
        self._capturer = ScreenCapturer()
        self._engine = OcrEngine()
        self._translator = build_translator(
            self._config.api_key or None,
            base_url=self._config.base_url,
            model=self._config.model,
            disable_reasoning=self._config.disable_reasoning,
        )

        self._overlay: SelectionOverlay | None = None
        self._preview: PreviewWindow | None = None
        self._busy = False

        hotkey = self._config.hotkey or DEFAULT_HOTKEY
        self._hotkeys = HotkeyManager(hotkey, self)
        self._hotkeys.triggered.connect(self.begin_capture)

        self._tray = TrayController(_hotkey_label(hotkey), self)
        self._tray.capture_requested.connect(self.begin_capture)
        self._tray.quit_requested.connect(self.quit)

    # ---------- 启动 ----------

    def start(self) -> None:
        self._tray.show()
        hotkey = self._config.hotkey or DEFAULT_HOTKEY
        hotkey_ok = self._hotkeys.start()

        if hotkey_ok:
            self._tray.notify("QTranslator 已启动", f"按 {_hotkey_label(hotkey)} 开始截图 OCR")
        else:
            self._tray.notify("QTranslator 已启动", "全局热键不可用，请用托盘菜单开始")

        if not self._config.has_api_key:
            from .config import config_path

            log.warning(
                "未配置翻译 API key，翻译将使用占位文本（MockTranslator）。"
                "设置环境变量 QTRANSLATOR_API_KEY 或写入 %s 可启用真实翻译。",
                config_path(),
            )

        # 后台预热 OCR 模型，避免第一次框选时卡一两秒
        QTimer.singleShot(500, self._preload_ocr)

    def _preload_ocr(self) -> None:
        import threading

        def load() -> None:
            try:
                self._engine.preload()
            except Exception:  # noqa: BLE001 — 预热失败不影响主流程，真正调用时会再报
                log.exception("OCR 模型预热失败")

        threading.Thread(target=load, name="ocr-preload", daemon=True).start()

    # ---------- 截图流程 ----------

    def begin_capture(self) -> None:
        if self._busy:
            log.debug("已有一次截图流程在进行，忽略本次触发")
            return
        if self._overlay is not None:
            self._overlay.close()
            self._overlay = None

        self._busy = True

        # 先隐藏已有预览窗，避免它被拍进截图里
        if self._preview is not None:
            self._preview.hide()

        # 延迟一拍再抓图：让隐藏窗口的重绘先落到屏幕上，
        # 同时给托盘菜单足够时间收起（否则菜单会出现在截图里）
        QTimer.singleShot(180, self._do_capture)

    def _do_capture(self) -> None:
        try:
            capture = self._capturer.grab()
        except Exception as exc:  # noqa: BLE001
            log.exception("截图失败")
            self._busy = False
            self._tray.notify("截图失败", str(exc))
            return

        self._show_overlay(capture)

    def _show_overlay(self, capture: Capture) -> None:
        transform = ViewTransform(scale=capture.scale_hint)
        log.debug(
            "截图 %dx%d，scale=%.2f", capture.image.width, capture.image.height, transform.scale
        )

        overlay = SelectionOverlay(capture.image, transform)
        overlay.selected.connect(self._on_region_selected)
        overlay.cancelled.connect(self._on_capture_cancelled)
        overlay.destroyed.connect(lambda *_: self._clear_overlay())

        self._overlay = overlay
        overlay.showFullScreen()
        overlay.raise_()
        overlay.activateWindow()
        overlay.setFocus(Qt.FocusReason.OtherFocusReason)

    def _on_capture_cancelled(self) -> None:
        self._busy = False
        self._restore_preview()

    def _clear_overlay(self) -> None:
        self._overlay = None

    def _on_region_selected(self, rect: PixelRect) -> None:
        overlay = self._overlay
        self._overlay = None

        if overlay is not None:
            image = overlay._image  # noqa: SLF001 — overlay 即将销毁，取一次底图
        else:
            self._busy = False
            return

        cropped = image.crop(_to_pil_box(rect))
        self._busy = False

        if cropped.width < 4 or cropped.height < 4:
            self._tray.notify("选区太小", "请重新选择一个更大的区域")
            self._restore_preview()
            return

        self._open_preview(cropped)

    # ---------- 预览 ----------

    def _open_preview(self, image) -> None:
        if self._preview is not None:
            self._preview.close()

        preview = PreviewWindow(
            image,
            self._engine,
            self._translator,
            target_lang=self._config.target_lang,
            font_path=self._config.resolved_font(),
            migan_model_path=str(self._config.resolved_migan() or ""),
        )
        preview.destroyed.connect(lambda *_: self._clear_preview())
        self._preview = preview
        preview.show()
        preview.raise_()
        preview.activateWindow()

        # 刻意**不**自动跑 OCR。窗口打开时就是个纯图片查看器：能缩放、能拖拽、
        # 能看清图内容。识别和翻译由用户点按钮触发——用户框选后常常只是想
        # 仔细看看这块图，自动 OCR 会白等几秒并让界面闪一下检测框。

    def _restore_preview(self) -> None:
        if self._preview is not None:
            self._preview.show()
            self._preview.raise_()

    def _clear_preview(self) -> None:
        self._preview = None

    # ---------- 退出 ----------

    def quit(self) -> None:
        log.info("正在退出")
        self._hotkeys.stop()
        self._tray.hide()
        if self._overlay is not None:
            self._overlay.close()
        if self._preview is not None:
            self._preview.close()
        self._capturer.close()
        self._qt_app.quit()


def _to_pil_box(rect: PixelRect) -> tuple[int, int, int, int]:
    """PixelRect → PIL crop 的 (left, upper, right, lower)。"""
    return rect.x, rect.y, rect.right, rect.bottom


def _hotkey_label(combo: str) -> str:
    """把 pynput 的 ``<ctrl>+<alt>+q`` 转成给人看的 ``Ctrl + Alt + Q``。"""
    parts = []
    for token in combo.split("+"):
        token = token.strip()
        if token.startswith("<") and token.endswith(">"):
            token = token[1:-1]
        if len(token) == 1:
            token = token.upper()
        else:
            token = token.capitalize()
        parts.append(token)
    return " + ".join(parts)


def run() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    qt_app = QApplication(sys.argv)
    qt_app.setApplicationName("QTranslator")
    # 关掉最后一个窗口就退出——本应用常驻托盘，窗口都是按需开关的
    qt_app.setQuitOnLastWindowClosed(False)

    if not _check_tray_available(qt_app):
        return 1

    translator = QTranslatorApp(qt_app)
    translator.start()

    return qt_app.exec()


def _check_tray_available(qt_app: QApplication) -> bool:
    """没有系统托盘时整个交互入口就没了，必须提前告知而不是静默失败。"""
    from PySide6.QtWidgets import QSystemTrayIcon

    if QSystemTrayIcon.isSystemTrayAvailable():
        return True

    QMessageBox.critical(
        None,
        "无法启动",
        "系统托盘不可用。QTranslator 依赖托盘图标作为入口，请检查桌面环境设置。",
    )
    return False
