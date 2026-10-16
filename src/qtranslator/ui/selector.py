"""全屏选区窗。

方案 3 的关键：这是一个**普通窗口**，只是被拉成全屏大小。它不需要
``WS_EX_LAYERED``/``WS_EX_TRANSPARENT`` 那套点击穿透，也不需要 X11 的
``XShapeCombineRectangles`` input shape——因此 Windows 与 Linux 走完全相同的
代码路径，Wayland 下也不需要降级。

窗口显示的是**已经抓好的静态截图**，不是实时屏幕。所以「冻结屏幕」的效果天然就有。
代价是用户拖选时看到的是快照而非实时画面——对截图翻译场景这正是想要的。

坐标处理：整张图等比缩放显示。用户拖选得到 DisplayRect，通过 ViewTransform
反算回物理像素 PixelRect。只有这一个地方做换算。
"""

from __future__ import annotations

import logging

from PIL import Image
from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QGuiApplication,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QWidget

from ..coords import DisplayRect, PixelRect, Point, ViewTransform

log = logging.getLogger(__name__)

# 未选中区域的暗化程度
_DIM_ALPHA = 110
_MIN_SELECTION_PX = 8


class SelectionOverlay(QWidget):
    """全屏选区窗。用户拖出一个矩形后发 :attr:`selected`。

    取消（Esc / 右键）发 :attr:`cancelled`。
    """

    selected = Signal(PixelRect)
    cancelled = Signal()

    def __init__(self, image: Image.Image, transform: ViewTransform, parent: QWidget | None = None):
        super().__init__(parent)
        self._image = image
        self._transform = transform
        self._pixmap = _to_pixmap(image)

        self._origin: QPoint | None = None
        self._current: QPoint | None = None
        self._dragging = False

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)

        # 覆盖整个虚拟桌面（多显示器的并集），这样跨屏拖选也不会被裁掉
        self.setGeometry(_virtual_desktop_logical_geometry())

        # 显示尺寸 = 图片物理尺寸 / scale
        self._display_w = image.width / transform.scale
        self._display_h = image.height / transform.scale

    # ---------- 事件 ----------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            self.cancelled.emit()
            self.close()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._origin = event.position().toPoint()
        self._current = self._origin
        self._dragging = True
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._current = event.position().toPoint()
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or not self._dragging:
            return
        self._dragging = False
        self._current = event.position().toPoint()

        rect = self._display_selection()
        if rect is None:
            self.update()
            return

        pixel = self._transform.display_to_pixel(rect)
        pixel = pixel.clamp_within(PixelRect(0, 0, self._image.width, self._image.height))
        if not pixel.is_valid(_MIN_SELECTION_PX):
            log.debug("选区过小，忽略: %r", pixel)
            self._origin = self._current = None
            self.update()
            return

        self.selected.emit(pixel)
        self.close()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
            self.close()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self._origin and self._current:
            self.mouseReleaseEvent(
                QMouseEvent(  # type: ignore[arg-type]
                    QMouseEvent.Type.MouseButtonRelease,
                    self._current.toPointF(),
                    Qt.MouseButton.LeftButton,
                    Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.NoModifier,
                )
            )
            return
        super().keyPressEvent(event)

    # ---------- 绘制 ----------

    def paintEvent(self, event) -> None:  # noqa: ARG002
        painter = QPainter(self)
        try:
            self._paint(painter)
        finally:
            # 必须显式 end()：paintEvent 里若抛异常而不结束 painter，
            # Qt 会在 endPaint 时报「active painter」并可能崩溃
            painter.end()

    def _paint(self, painter: QPainter) -> None:
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        # 1) 画底图（冻结的屏幕快照），缩放到显示尺寸
        target = QRect(0, 0, int(self._display_w), int(self._display_h))
        painter.drawPixmap(target, self._pixmap)

        sel = self._display_selection()

        # 2) 暗化：整屏先压暗，再把选区挖亮
        if sel is None:
            painter.fillRect(self.rect(), QColor(0, 0, 0, _DIM_ALPHA))
        else:
            dim = QColor(0, 0, 0, _DIM_ALPHA)
            r = _to_qrect(sel)
            painter.fillRect(QRect(0, 0, self.width(), r.top()), dim)
            painter.fillRect(QRect(0, r.bottom(), self.width(), self.height() - r.bottom()), dim)
            painter.fillRect(QRect(0, r.top(), r.left(), r.height()), dim)
            painter.fillRect(QRect(r.right(), r.top(), self.width() - r.right(), r.height()), dim)

            painter.setPen(QPen(QColor(64, 160, 255), 2))
            painter.drawRect(r)
            self._draw_size_hint(painter, sel)

        # 3) 全屏提示条
        self._draw_hint(painter)

    def _draw_size_hint(self, painter: QPainter, sel: DisplayRect) -> None:
        """在选区上方显示物理像素尺寸——用户实际关心的就是这个数。"""
        px = self._transform.display_to_pixel(sel)
        label = f"{px.w} × {px.h}"

        painter.setFont(self.font())
        metrics = painter.fontMetrics()
        tw = metrics.horizontalAdvance(label) + 14
        th = metrics.height() + 8

        # 优先画在选区上方；贴到屏幕顶端时改画在下方
        label_y = sel.y - th - 6 if sel.y - th - 6 > 0 else sel.bottom + 6
        box = QRect(int(sel.x), int(label_y), tw, th)

        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(24, 24, 26, 235))
        painter.drawRoundedRect(box, 4, 4)
        painter.setPen(QColor(90, 170, 255))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, label)

    def _draw_hint(self, painter: QPainter) -> None:
        """屏幕顶部的操作提示条。样式与预览窗的深色工具栏保持一致。"""
        text = "拖动选择要翻译的区域　·　Esc / 右键取消"
        painter.setFont(self.font())
        metrics = painter.fontMetrics()
        tw = metrics.horizontalAdvance(text) + 32
        th = metrics.height() + 16
        box = QRect((self.width() - tw) // 2, 22, tw, th)

        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(43, 43, 46, 245))
        painter.drawRoundedRect(box, 6, 6)
        painter.setPen(QColor(225, 225, 228))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)

    # ---------- 辅助 ----------

    def _display_selection(self) -> DisplayRect | None:
        if self._origin is None or self._current is None:
            return None
        rect = DisplayRect.from_corners(
            Point(self._origin.x(), self._origin.y()),
            Point(self._current.x(), self._current.y()),
        )
        if rect.w < 1 or rect.h < 1:
            return None
        return rect


def _to_qrect(rect: DisplayRect) -> QRect:
    """DisplayRect（浮点）→ QRect（整型）。绘制 API 只吃整型。"""
    return QRect(
        int(round(rect.x)),
        int(round(rect.y)),
        max(1, int(round(rect.w))),
        max(1, int(round(rect.h))),
    )


def _to_pixmap(image: Image.Image) -> QPixmap:
    """PIL 图 → QPixmap。固定走 RGB888，避免不同平台字节序差异。"""
    rgb = image.convert("RGB")
    qimg = QImage(
        rgb.tobytes(),
        rgb.width,
        rgb.height,
        rgb.width * 3,
        QImage.Format.Format_RGB888,
    )
    # QImage 不持有 buffer，必须 copy 后再转，否则 PIL 对象回收后是悬垂指针
    return QPixmap.fromImage(qimg.copy())


def _virtual_desktop_logical_geometry() -> QRect:
    """所有显示器在逻辑坐标下的并集。多显示器下覆盖层要铺满整个桌面。"""
    app = QGuiApplication.instance()
    if app is None:
        return QRect(0, 0, 1920, 1080)
    union = QRect()
    for screen in QGuiApplication.screens():
        union = union.united(screen.geometry())
    return union if not union.isNull() else QRect(0, 0, 1920, 1080)
