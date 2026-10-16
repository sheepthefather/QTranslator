"""结果预览窗。

参照 Windows 截图工具的样式：无系统标题栏，顶部自绘工具栏；中间是可缩放
拖拽的图片画布；底部状态栏。

工具栏按钮（左 → 右）：

    返回 / 前进（视图切换） │ 裁剪 │ 复制 │ 编辑 │ OCR │ 翻译 │ 保存 │ ··· │ 缩放 │ 适应 │ 1:1 │ 最小化 │ 最大化 │ 关闭

「···」是溢出菜单，放次要的开关（检测框 / 识别文字 / 段落块）和对比模式，
避免工具栏堆得太满。

所有耗时操作（OCR / 翻译）跑在后台线程。翻译要联网，可能几秒，卡住 UI 会让
用户以为程序挂了。
"""

from __future__ import annotations

import logging

from PIL import Image
from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMenu,
    QVBoxLayout,
    QWidget,
)

from ..erase import EraseMode
from ..ocr import OcrEngine, OcrResult
from ..pipeline import Stage2Result, process_from_ocr
from ..translate import Translator
from .canvas import ImageCanvas
from .chrome import ACCENT, IconButton, StatusLabel, TitleBar
from .resizer import FramelessResizer

log = logging.getLogger(__name__)

MIN_W, MIN_H = 640, 420


class _OcrWorker(QThread):
    """后台跑 OCR。识别大图要几百毫秒到数秒，不能卡 UI 线程。"""

    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, engine: OcrEngine, image: Image.Image) -> None:
        super().__init__()
        self._engine = engine
        self._image = image

    def run(self) -> None:
        try:
            result = self._engine.recognize(self._image)
        except Exception as exc:  # noqa: BLE001 — 线程内异常必须转成信号
            log.exception("OCR 失败")
            self.failed.emit(str(exc))
            return
        self.finished_ok.emit(result)


class _TranslateWorker(QThread):
    """后台跑阶段二管线（翻译 + 擦除 + 回填）。"""

    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        image: Image.Image,
        ocr: OcrResult,
        translator: Translator,
        *,
        target_lang: str,
        font_path: str | None,
        migan_model_path: str | None,
    ) -> None:
        super().__init__()
        self._image = image
        self._ocr = ocr
        self._translator = translator
        self._target_lang = target_lang
        self._font_path = font_path
        self._migan = migan_model_path

    def run(self) -> None:
        try:
            from pathlib import Path

            migan = Path(self._migan) if self._migan else None
            result = process_from_ocr(
                self._image,
                self._ocr,
                self._translator,
                target_lang=self._target_lang,
                font_path=self._font_path,
                erase_mode=EraseMode.AUTO,
                migan_model=migan,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("翻译管线失败")
            self.failed.emit(str(exc))
            return
        self.finished_ok.emit(result)


class PreviewWindow(QWidget):
    """无边框预览窗。自绘标题栏 + 可缩放画布 + 状态栏。"""

    def __init__(
        self,
        image: Image.Image,
        engine: OcrEngine,
        translator: Translator,
        *,
        target_lang: str = "zh",
        font_path: str | None = None,
        migan_model_path: str | None = None,
    ) -> None:
        super().__init__()
        self._image = image
        self._engine = engine
        self._translator = translator
        self._target_lang = target_lang
        self._font_path = font_path
        self._migan_model_path = migan_model_path

        self._ocr_worker: _OcrWorker | None = None
        self._tr_worker: _TranslateWorker | None = None
        self._ocr_result: OcrResult | None = None
        self._stage2: Stage2Result | None = None
        self._showing = "original"  # original | translated
        self._translate_after_ocr = False

        self.setWindowTitle("QTranslator")
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setMinimumSize(MIN_W, MIN_H)

        # 窗口大小按图片比例来，但不超出屏幕可用区域的 88%。
        # 固定尺寸会让宽图和竖图都别扭：宽图上下留大片空白，竖图左右留。
        screen = QGuiApplication.primaryScreen()
        avail_w, avail_h = (1280, 800)
        if screen is not None:
            geo = screen.availableGeometry()
            avail_w, avail_h = int(geo.width() * 0.88), int(geo.height() * 0.88)

        chrome_h = 48 + 26  # 工具栏 + 状态栏
        want_w = image.width + 40
        want_h = image.height + chrome_h + 40

        scale_down = min(1.0, avail_w / want_w, avail_h / want_h)
        self.resize(
            max(MIN_W, int(want_w * scale_down)),
            max(MIN_H, int(want_h * scale_down)),
        )

        self._build_ui()
        self._center_on_screen()

        # 无边框窗口没有系统缩放边框，自己接上。
        # 必须在 show() 之后才有 windowHandle，但 install() 只是装事件过滤器，
        # 过滤器内部会惰性取 handle，所以这里装没问题。
        self._resizer = FramelessResizer(self)
        self._resizer.install()

    # ---------- 构建 ----------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 画布必须先于工具栏创建：工具栏的缩放按钮要连到画布的方法上
        self._canvas = ImageCanvas()
        self._canvas.set_image(self._image)
        self._canvas.scale_changed.connect(self._on_scale_changed)

        root.addWidget(self._build_toolbar())
        root.addWidget(self._canvas, 1)

        self._status = StatusLabel()
        self._status.set_message("滚轮缩放 · 拖动平移 · 双击适应窗口　——　点「识别」或「翻译」开始处理")
        root.addWidget(self._status)

    def _build_toolbar(self) -> TitleBar:
        bar = TitleBar()

        # —— 视图切换（原文/译文）——
        self._btn_back = IconButton("back", "查看原文")
        self._btn_back.clicked.connect(lambda: self._show_view("original"))
        bar.add(self._btn_back)

        self._btn_forward = IconButton("forward", "查看译文")
        self._btn_forward.clicked.connect(lambda: self._show_view("translated"))
        self._btn_forward.setEnabled(False)
        bar.add(self._btn_forward)

        bar.add_separator()

        # —— 主要动作 ——
        self._btn_ocr = IconButton("ocr", "识别图中文字 (Ctrl+R)")
        self._btn_ocr.clicked.connect(self.run_ocr)
        bar.add(self._btn_ocr)

        # 「翻译」从一开始就可用：没识别过的话它会先自动补一次 OCR 再翻译，
        # 用户不需要先点「识别」。见 run_translate 的说明。
        self._btn_translate = IconButton("translate", "翻译并覆盖原文 (Ctrl+T)")
        self._btn_translate.clicked.connect(self.run_translate)
        bar.add(self._btn_translate)

        # 「复制原图」从一开始就可用：不需要识别也不需要翻译。
        # 用户框选后常常只是想把这块图截走，不该逼他先跑 OCR。
        self._btn_copy_original = IconButton("copy_image", "复制原图 (Ctrl+Shift+C)")
        self._btn_copy_original.clicked.connect(self._copy_original)
        bar.add(self._btn_copy_original)

        self._btn_copy = IconButton("copy", "复制识别/译文文本 (Ctrl+C)")
        self._btn_copy.clicked.connect(self._copy_text)
        self._btn_copy.setEnabled(False)
        bar.add(self._btn_copy)

        self._btn_save = IconButton("save", "保存图片 (Ctrl+S)")
        self._btn_save.clicked.connect(self._save_image)
        bar.add(self._btn_save)

        bar.add_separator()

        # —— 缩放 ——
        self._btn_zoom_out = IconButton("zoom_out", "缩小 (-)")
        self._btn_zoom_out.clicked.connect(lambda: self._canvas.zoom_by(1 / 1.25))
        bar.add(self._btn_zoom_out)

        self._btn_zoom_in = IconButton("zoom_in", "放大 (+)")
        self._btn_zoom_in.clicked.connect(lambda: self._canvas.zoom_by(1.25))
        bar.add(self._btn_zoom_in)

        self._btn_fit = IconButton("fit", "适应窗口 (Ctrl+0)")
        self._btn_fit.clicked.connect(self._canvas.fit_to_window)
        bar.add(self._btn_fit)

        self._btn_actual = IconButton("actual", "实际大小 100% (Ctrl+1)")
        self._btn_actual.clicked.connect(self._canvas.actual_size)
        bar.add(self._btn_actual)

        bar.add_separator()

        # —— 溢出菜单：次要开关 ——
        self._btn_more = IconButton("more", "更多选项")
        self._btn_more.clicked.connect(self._show_more_menu)
        bar.add(self._btn_more)

        bar.add_stretch()

        # —— 窗口控制 ——
        self._btn_min = IconButton("minimize", "最小化")
        self._btn_min.clicked.connect(self.showMinimized)
        bar.add(self._btn_min)

        self._btn_max = IconButton("maximize", "最大化")
        self._btn_max.clicked.connect(self._toggle_maximize)
        bar.add(self._btn_max)

        self._btn_close = IconButton("close", "关闭 (Esc)", danger=True)
        self._btn_close.clicked.connect(self.close)
        bar.add(self._btn_close)

        return bar

    def _center_on_screen(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        frame = self.frameGeometry()
        frame.moveCenter(geo.center())
        self.move(frame.topLeft())

    # ---------- 视图 ----------

    def _show_view(self, which: str) -> None:
        if which == "translated" and self._stage2 is None:
            return
        self._showing = which
        self._canvas.set_image(
            self._stage2.translated if which == "translated" else self._image
        )
        self._btn_back.setEnabled(which != "original")
        self._btn_forward.setEnabled(which != "translated")
        label = "译文" if which == "translated" else "原文"
        self._status.set_message(f"{label}视图")

    def _on_scale_changed(self, scale: float) -> None:
        self._status.set_zoom(scale)

    def _toggle_maximize(self) -> None:
        if self.isMaximized():
            self.showNormal()
            self._btn_max.set_icon_name("maximize")
            self._btn_max.setToolTip("最大化")
        else:
            self.showMaximized()
            self._btn_max.set_icon_name("restore")
            self._btn_max.setToolTip("还原")

    def _show_more_menu(self) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu{background:#2b2b2e;color:#e1e1e4;border:1px solid #46464c;padding:4px;}"
            "QMenu::item{padding:6px 22px;border-radius:4px;}"
            "QMenu::item:selected{background:#40404a;}"
            "QMenu::separator{height:1px;background:#46464c;margin:4px 8px;}"
        )

        act = menu.addAction("显示检测框")
        act.setCheckable(True)
        act.setChecked(self._canvas._show_boxes)  # noqa: SLF001
        act.toggled.connect(self._canvas.set_show_boxes)

        act = menu.addAction("显示识别文字")
        act.setCheckable(True)
        act.setChecked(self._canvas._show_text)  # noqa: SLF001
        act.toggled.connect(self._canvas.set_show_text)

        act = menu.addAction("显示段落块")
        act.setCheckable(True)
        act.setChecked(self._canvas._show_blocks)  # noqa: SLF001
        act.toggled.connect(self._canvas.set_show_blocks)

        menu.addSeparator()

        act = menu.addAction("复制原图")
        act.triggered.connect(self._copy_original)

        act = menu.addAction("复制译文图片")
        act.setEnabled(self._stage2 is not None)
        act.triggered.connect(self._copy_image)

        act = menu.addAction("复制识别/译文文本")
        act.setEnabled(self._ocr_result is not None)
        act.triggered.connect(self._copy_text)

        menu.addSeparator()

        act = menu.addAction("保存图片")
        act.triggered.connect(self._save_image)

        menu.exec(self._btn_more.mapToGlobal(self._btn_more.rect().bottomLeft()))

    # ---------- OCR ----------

    def run_ocr(self) -> None:
        if self._ocr_worker is not None and self._ocr_worker.isRunning():
            return
        self._btn_ocr.setEnabled(False)
        self._btn_translate.setEnabled(False)
        self._status.set_message("识别中…")

        worker = _OcrWorker(self._engine, self._image)
        worker.finished_ok.connect(self._on_ocr_done)
        worker.failed.connect(self._on_ocr_failed)
        worker.finished.connect(lambda: self._btn_ocr.setEnabled(True))
        self._ocr_worker = worker
        worker.start()

    def _on_ocr_done(self, result: OcrResult) -> None:
        self._ocr_result = result
        self._canvas.set_lines(result.lines)
        self._btn_copy.setEnabled(bool(result.lines))
        self._btn_translate.setEnabled(bool(result.lines))

        self._status.set_message(
            f"识别完成：{len(result.lines)} 行 · 平均置信度 {result.mean_score:.2f} · "
            f"耗时 {result.elapsed_ms:.0f} ms　（检测框可在「···」菜单里打开）"
        )

        # 用户点的是「翻译」，识别只是前置步骤——这里自动接上去
        if getattr(self, "_translate_after_ocr", False):
            self._translate_after_ocr = False
            self._start_translate()

    def _on_ocr_failed(self, message: str) -> None:
        self._translate_after_ocr = False
        self._status.set_message(f"识别失败：{message}")
        log.error("OCR 失败: %s", message)

    # ---------- 翻译 ----------

    def run_translate(self) -> None:
        """翻译并回填。

        窗口打开时不再自动识别，所以用户可能直接点「翻译」。这时先自动补一次
        OCR 再接着翻译——用户点「翻译」的意图是"给我译文"，让他先手动点一次
        「识别」是多余的步骤。用 ``_translate_after_ocr`` 标记这次识别完成后
        自动衔接。
        """
        if self._tr_worker is not None and self._tr_worker.isRunning():
            return
        if self._ocr_worker is not None and self._ocr_worker.isRunning():
            return

        if self._ocr_result is None:
            self._translate_after_ocr = True
            self._status.set_message("先识别文字，随后自动翻译…")
            self.run_ocr()
            return

        self._start_translate()

    def _start_translate(self) -> None:
        if self._ocr_result is None:
            self._status.set_message("没有可翻译的内容")
            return

        self._btn_translate.setEnabled(False)
        self._btn_ocr.setEnabled(False)
        self._status.set_message(
            f"翻译中…（{type(self._translator).__name__} → {self._target_lang}）"
        )

        worker = _TranslateWorker(
            self._image,
            self._ocr_result,
            self._translator,
            target_lang=self._target_lang,
            font_path=self._font_path,
            migan_model_path=self._migan_model_path,
        )
        worker.finished_ok.connect(self._on_translate_done)
        worker.failed.connect(self._on_translate_failed)
        worker.finished.connect(self._on_translate_finished)
        self._tr_worker = worker
        worker.start()

    def _on_translate_done(self, result: Stage2Result) -> None:
        self._stage2 = result
        self._btn_forward.setEnabled(True)

        # 段落到手了，画出来供核对排版；翻译完成后默认切到译文视图
        self._canvas.set_blocks(result.blocks)
        self._show_view("translated")

        over = result.overflow_blocks
        msg = (
            f"完成：{len(result.blocks)} 块 · 翻译 {result.translate_seconds:.2f}s · "
            f"擦除 {result.erase_seconds:.2f}s · 回填 {result.reflow_seconds:.2f}s"
        )
        if over:
            msg += f"　⚠ {len(over)} 个块缩到下限仍溢出"
        self._status.set_message(msg)

    def _on_translate_failed(self, message: str) -> None:
        self._status.set_message(f"翻译失败：{message}")
        log.error("翻译失败: %s", message)

    def _on_translate_finished(self) -> None:
        self._btn_translate.setEnabled(True)
        self._btn_ocr.setEnabled(True)

    # ---------- 剪贴板 / 保存 ----------

    def _copy_text(self) -> None:
        if self._stage2 is not None:
            text = "\n".join(
                b.translation or b.source_text for b in self._stage2.blocks
            )
        elif self._ocr_result is not None:
            text = self._ocr_result.full_text
        else:
            self._status.set_message("还没有可复制的内容")
            return
        QApplication.clipboard().setText(text)
        self._status.set_message("已复制文本到剪贴板")

    def _copy_image(self) -> None:
        """复制译文图片到剪贴板。没翻译过时退回复制原图。"""
        if self._stage2 is not None:
            self._copy_pil(self._stage2.translated, "已复制译文图片到剪贴板")
        else:
            self._copy_pil(self._image, "还没翻译，已复制原图到剪贴板")

    def _copy_original(self) -> None:
        """复制**原图**（未识别的原始截图）到剪贴板。

        与"复制文本"的区别：这个给的是图片，可以直接粘到聊天窗口、文档里。
        与"复制译文图片"的区别：这个不受翻译结果影响，随时可用——用户框选后
        常常只是想把这块图截下来用，根本不需要 OCR。
        """
        self._copy_pil(self._image, "已复制原图到剪贴板")

    def _copy_pil(self, image, message: str) -> None:
        """把 PIL 图放进剪贴板。"""
        from .canvas import _pil_to_pixmap

        QApplication.clipboard().setPixmap(_pil_to_pixmap(image))
        self._status.set_message(message)

    def _save_image(self) -> None:
        """保存图片。翻译过就存译文，否则存原图。"""
        if self._stage2 is not None:
            image, default_name, title = self._stage2.translated, "translated.png", "保存译文图片"
        else:
            image, default_name, title = self._image, "original.png", "保存原图"

        path, _ = QFileDialog.getSaveFileName(
            self, title, default_name, "PNG (*.png);;JPEG (*.jpg)"
        )
        if not path:
            return
        image.save(path)
        self._status.set_message(f"已保存到 {path}")

    # ---------- 键盘 ----------

    def keyPressEvent(self, event) -> None:
        key = event.key()
        mod = event.modifiers()

        if key == Qt.Key.Key_Escape:
            self.close()
            return
        if mod & Qt.KeyboardModifier.ControlModifier:
            if key == Qt.Key.Key_R:
                self.run_ocr()
                return
            if key == Qt.Key.Key_T:
                self.run_translate()
                return
            # Ctrl+Shift+C 复制原图，Ctrl+C 复制文本。
            # Shift 分支必须在前面判断——否则 Ctrl+Shift+C 会被当成 Ctrl+C。
            if key == Qt.Key.Key_C and mod & Qt.KeyboardModifier.ShiftModifier:
                self._copy_original()
                return
            if key == Qt.Key.Key_C:
                self._copy_text()
                return
            if key == Qt.Key.Key_S:
                self._save_image()
                return
            if key == Qt.Key.Key_0:
                self._canvas.fit_to_window()
                return
            if key == Qt.Key.Key_1:
                self._canvas.actual_size()
                return
            if key == Qt.Key.Key_Left:
                self._show_view("original")
                return
            if key == Qt.Key.Key_Right:
                self._show_view("translated")
                return

        if key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self._canvas.zoom_by(1.25)
            return
        if key in (Qt.Key.Key_Minus, Qt.Key.Key_Underscore):
            self._canvas.zoom_by(1 / 1.25)
            return
        super().keyPressEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # 首次显示时把图适配进窗口。
        #
        # 用 singleShot(0) 而不是直接调用：showEvent 触发时布局还没算完，
        # 画布的实际尺寸仍是旧值，fit 会算出错误比例（实测得到 109% 而不是
        # 适应窗口应有的比例）。延到事件循环下一轮，尺寸就准了。
        if not getattr(self, "_fitted_once", False):
            self._fitted_once = True
            QTimer.singleShot(0, self._initial_fit)

    def _initial_fit(self) -> None:
        self._canvas.fit_to_window()
        self._status.set_zoom(self._canvas.scale)

    def closeEvent(self, event) -> None:
        for worker in (self._ocr_worker, self._tr_worker):
            if worker is not None and worker.isRunning():
                worker.wait(3000)
        super().closeEvent(event)
