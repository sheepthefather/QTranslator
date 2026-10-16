"""可缩放、可拖拽的图片画布。

自己实现而不是用 QGraphicsView：QGraphicsView 的坐标系和缩放逻辑会跟
``coords.py`` 里那套物理像素约定打架，而本项目所有坐标换算都必须走那一个
地方。自己画虽然代码多一些，但缩放平移的数学完全可控。

缩放平移的模型：

- ``_scale`` —— 显示比例。1.0 表示 1 物理像素对 1 逻辑像素。
- ``_offset`` —— 图片左上角在控件坐标系里的位置。

任何缩放操作都**保持光标（或视图中心）下的那个点不动**——这是所有看图工具的
标准行为，用户拖到某个细节上滚轮放大时，期望放大的就是那一点。
"""

from __future__ import annotations

import logging

from PIL import Image
from PySide6.QtCore import QPoint, QPointF, QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QMouseEvent, QPainter, QPen, QPixmap, QWheelEvent
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..coords import PixelRect
from ..layout import TextBlock
from ..ocr import TextLine

log = logging.getLogger(__name__)

MAX_SCALE = 12.0
# 滚轮每格缩放倍率。1.0015^120 ≈ 1.2，即每格约放大 20%
_WHEEL_STEP = 1.0015

_COLOR_HIGH = QColor(64, 200, 120)
_COLOR_MID = QColor(230, 190, 60)
_COLOR_LOW = QColor(230, 90, 90)
_COLOR_BLOCK = QColor(64, 160, 255)


class ImageCanvas(QWidget):
    """显示图片，支持滚轮缩放、拖拽平移，并可叠加检测框/段落块。

    缩放下限 = **"至少一个方向贴满视图"** 的比例，即 ``min(vw/iw, vh/ih)``
    （也就是"适应窗口"的比例）。到达下限时，那个先贴满的方向严丝合缝，
    另一个方向对称留白——这正是微信图片查看器的行为（实测其截图：高度
    gap=0，宽度两侧各 58px）。

    **余量为 0。** 曾用 24px 余量算 fit/下限，结果图片永远比窗口小一圈、
    四周始终有缝，看起来"没贴边"。微信是严丝合缝的，所以这里不留余量。

    这个下限是**动态**的：它与控件尺寸和图片尺寸都有关，窗口一变下限就变。
    所以不能用固定常量，见 :meth:`_min_scale`。
    """

    scale_changed = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pixmap = QPixmap()
        self._image_size = (0, 0)

        self._lines: list[TextLine] = []
        self._blocks: list[TextBlock] = []
        # 叠加层默认**关闭**。它们是排查工具，不是常态显示——默认打开会让
        # 检测框和置信度标签糊满整张图，把真正要看的译文盖住，用户第一眼
        # 看到的是"一堆绿框"而不是翻译结果。
        self._show_boxes = False
        self._show_text = False
        self._show_blocks = False

        self._scale = 1.0
        self._offset = QPointF(0, 0)
        self._panning = False
        self._pan_start = QPoint()
        self._offset_start = QPointF()

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(320, 240)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    # ---------- 内容 ----------

    def set_image(self, image: Image.Image) -> None:
        """换图。保持当前缩放与位置不变——切换原文/译文时不该跳回原位。"""
        self._pixmap = _pil_to_pixmap(image)
        self._image_size = (self._pixmap.width(), self._pixmap.height())
        self.update()

    def set_lines(self, lines: list[TextLine]) -> None:
        self._lines = lines
        self.update()

    def set_blocks(self, blocks: list[TextBlock]) -> None:
        self._blocks = blocks
        self.update()

    def set_show_boxes(self, on: bool) -> None:
        self._show_boxes = on
        self.update()

    def set_show_text(self, on: bool) -> None:
        self._show_text = on
        self.update()

    def set_show_blocks(self, on: bool) -> None:
        self._show_blocks = on
        self.update()

    # ---------- 缩放 ----------

    @property
    def scale(self) -> float:
        return self._scale

    def _min_scale(self) -> float:
        """允许的最小缩放 = "至少一个方向贴满视图"的比例。

        等于 ``min(vw/iw, vh/ih)``——先达到的那个比值对应"先贴满"的那个方向。
        再往下缩，两个方向就都小于视图了（四周全留白），那是要避免的状态。

        动态计算而非固定常量：它取决于图片和控件两者的尺寸，窗口一变下限就变。
        控件还没有效尺寸时返回一个保护值，避免算出 0 或负数。
        """
        iw, ih = self._image_size
        if iw <= 0 or ih <= 0:
            return 0.01
        return min(self.width() / iw, self.height() / ih)

    def zoom_by(self, factor: float, anchor: QPointF | None = None) -> None:
        """以 ``anchor``（控件坐标，默认视图中心）为锚点缩放。

        缩放下限是 :meth:`_min_scale`（适应窗口比例），放大上限是
        :data:`MAX_SCALE`。缩到下限时图片正好填满视图，不会再出现小图浮在
        空白里的情况。
        """
        lower = self._min_scale()
        # 上限不能低于下限——窗口极窄时 _min_scale 可能超过 MAX_SCALE，
        # 那时以 _min_scale 为准，否则会算出空区间导致无法缩放
        upper = max(lower, MAX_SCALE)
        new_scale = _clamp(self._scale * factor, lower, upper)

        if abs(new_scale - self._scale) < 1e-6:
            # 比例没变（已在下限/上限）。仍要修正偏移：用户可能在下限处手动
            # 拖开过，此时再滚轮应该把它拉回居中，而不是完全无响应。
            self._clamp_offset()
            self.update()
            return

        if anchor is None:
            anchor = QPointF(self.width() / 2, self.height() / 2)

        # 保持锚点下的图片坐标不变：先算出锚点对应的图片坐标，缩放后再反解偏移。
        img_pt = (anchor - self._offset) / self._scale
        self._scale = new_scale
        self._offset = anchor - img_pt * self._scale

        # 缩放后立即夹紧。严格夹紧（不给松弛量）是刻意的——见 _clamp_offset
        # 的说明：松弛会让图片在边角锚点下被推出视图，露出黑边。
        self._clamp_offset()

        self.scale_changed.emit(self._scale)
        self.update()

    def fit_to_window(self) -> None:
        """整图适应窗口，并居中。这与缩放下限是同一个比例。

        不留余量：图片严丝合缝贴满较短的那个方向（微信图片查看器的行为）。
        """
        if self._image_size[0] <= 0 or self._image_size[1] <= 0:
            return
        # 下限公式即 fit 公式，直接复用，避免两处公式漂移
        self._scale = min(self._min_scale(), MAX_SCALE)
        self._center()
        self.scale_changed.emit(self._scale)
        self.update()

    def actual_size(self) -> None:
        """100% 显示（1 物理像素 = 1 逻辑像素），以视图中心为锚点。

        100% 可能小于缩放下限（窗口远小于图片时不会，反之会），
        :meth:`zoom_by` 里的夹紧会把它抬到下限——这是期望行为。
        """
        if self._scale:
            self.zoom_by(1.0 / self._scale)

    def reset_view(self) -> None:
        self._scale = 1.0
        self._offset = QPointF(0, 0)
        self.fit_to_window()

    def _center(self) -> None:
        iw = self._image_size[0] * self._scale
        ih = self._image_size[1] * self._scale
        self._offset = QPointF((self.width() - iw) / 2, (self.height() - ih) / 2)

    def _clamp_offset(self) -> None:
        """约束偏移。**占不满的轴无条件居中；溢出的轴严格夹在合法范围内。**

        两条规则都不留余地：

        - **该轴占不满**（缩放后尺寸 ≤ 视图）：正好居中。此时没有平移自由度，
          谈"保留锚点"或"保留用户拖的位置"都没有意义。
        - **该轴溢出**：夹在 ``[视图 - 图片, 0]``。上界 0 表示图片左/上边不能
          滑到视图右/下方，下界表示右/下边不能滑进视图内。

        **没有 relax 参数了。** 早期为了让锚点在"图片从溢出变为不溢出"的那一档
        不跳，给溢出轴加了 ¼ 视图的松弛量。后果是：锚点在边角时（如左上角
        (10,10)），缩放会把偏移推到 ``+31``，而合法上界是 0——图片左侧被推到
        视图右侧之外，画面右边露出一条边。用户必须拖一下才被夹回正常。
        实测这种越界在 432 组参数组合里出现 271 次，不是偶发。

        取舍：严格夹紧会让锚点在溢出边界处漂移（实测最多 54px），但**永远
        不会让图片滑出视图**。边界处几十像素的锚点偏差几乎察觉不到，而图片
        滑出去是显眼的视觉错误。所以选严格夹紧。
        """
        iw = self._image_size[0] * self._scale
        ih = self._image_size[1] * self._scale
        vw, vh = self.width(), self.height()

        if iw <= vw:
            self._offset.setX((vw - iw) / 2)  # 占不满 -> 居中，无例外
        else:
            self._offset.setX(_clamp(self._offset.x(), vw - iw, 0.0))

        if ih <= vh:
            self._offset.setY((vh - ih) / 2)
        else:
            self._offset.setY(_clamp(self._offset.y(), vh - ih, 0.0))

    # ---------- 交互 ----------

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta().y()
        if delta == 0:
            return
        self.zoom_by(_WHEEL_STEP**delta, event.position())
        event.accept()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._panning = True
            self._pan_start = event.position().toPoint()
            self._offset_start = QPointF(self._offset)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not self._panning:
            return
        delta = event.position().toPoint() - self._pan_start
        self._offset = self._offset_start + QPointF(delta.x(), delta.y())
        self._clamp_offset()
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._panning:
            self._panning = False
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        """双击在"适应窗口"和"100%"之间切换。"""
        if abs(self._scale - 1.0) < 0.01:
            self.fit_to_window()
        else:
            self.actual_size()
        event.accept()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)

        # 下限随视图大小变化，方向容易记反：
        #   _min_scale = min(avail_w/iw, avail_h/ih)  —— 视图越大，下限越高。
        # 所以**窗口变大**时下限上升，当前比例可能已经低于下限（图片比视图小
        # 了），必须抬上去，否则会违背"不能缩到比适应窗口更小"这条规则。
        # 窗口变小时下限下降，当前比例仍合法，不动它——用户的比例不该被改。
        lower = self._min_scale()
        if self._scale < lower:
            self._scale = lower
            self._center()
            self.scale_changed.emit(self._scale)

        self._clamp_offset()

    # ---------- 绘制 ----------

    def paintEvent(self, event) -> None:  # noqa: ARG002
        painter = QPainter(self)
        try:
            self._paint(painter)
        finally:
            # 提前 return 或异常都不能留下 active painter
            painter.end()

    def _paint(self, painter: QPainter) -> None:
        painter.fillRect(self.rect(), QColor(32, 32, 34))

        if self._pixmap.isNull():
            painter.setPen(QColor(140, 140, 145))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "无图片")
            return

        iw = self._image_size[0] * self._scale
        ih = self._image_size[1] * self._scale
        target = QRect(
            int(round(self._offset.x())),
            int(round(self._offset.y())),
            max(1, int(round(iw))),
            max(1, int(round(ih))),
        )

        # 放大到 2 倍以上时用最近邻，否则图片会糊成一片；缩小时用平滑
        painter.setRenderHint(
            QPainter.RenderHint.SmoothPixmapTransform, self._scale < 2.0
        )
        painter.drawPixmap(target, self._pixmap)

        if self._show_blocks and self._blocks:
            self._paint_blocks(painter)
        if self._lines and (self._show_boxes or self._show_text):
            self._paint_lines(painter)

    def _img_to_view(self, x: float, y: float) -> QPointF:
        """图片坐标 → 控件坐标。"""
        return QPointF(x * self._scale + self._offset.x(), y * self._scale + self._offset.y())

    def _paint_lines(self, painter: QPainter) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = QFont()
        font.setPointSize(8)
        painter.setFont(font)

        for line in self._lines:
            if self._show_boxes:
                color = _score_color(line.score)
                painter.setPen(QPen(color, 1.2))
                pts = [self._img_to_view(x, y) for x, y in line.quad]
                painter.drawPolygon(pts)

            if self._show_text:
                self._draw_label(painter, line)

    def _draw_label(self, painter: QPainter, line: TextLine) -> None:
        """标签画在框的上方内侧。

        缩放到很小的时候不画——标签会糊成一团，反而盖住文字。
        """
        if self._scale < 0.6:
            return

        bbox = line.bbox
        top_left = self._img_to_view(bbox.x, bbox.y)

        text = f"{line.text}  {line.score:.2f}"
        metrics = painter.fontMetrics()
        tw = metrics.horizontalAdvance(text) + 6
        th = metrics.height() + 2

        # 优先画在框内顶部，而不是框上方——画在上方会在密集排版里跟上一行
        # 的框重叠，视觉上更乱
        rect = QRect(int(top_left.x()), int(top_left.y()), tw, th)
        painter.fillRect(rect, QColor(20, 20, 20, 190))
        painter.setPen(_score_color(line.score))
        painter.drawText(rect.adjusted(3, 0, 0, 0), Qt.AlignmentFlag.AlignVCenter, text)

    def _paint_blocks(self, painter: QPainter) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(_COLOR_BLOCK, 1.6, Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for block in self._blocks:
            b = block.bbox
            tl = self._img_to_view(b.x, b.y)
            br = self._img_to_view(b.right, b.bottom)
            painter.drawRect(
                QRect(
                    int(tl.x()), int(tl.y()),
                    max(1, int(br.x() - tl.x())), max(1, int(br.y() - tl.y())),
                )
            )


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _score_color(score: float) -> QColor:
    if score >= 0.85:
        return _COLOR_HIGH
    if score >= 0.6:
        return _COLOR_MID
    return _COLOR_LOW


def _pil_to_pixmap(image: Image.Image) -> QPixmap:
    """PIL 图 → QPixmap。固定走 RGB888，避免平台字节序差异。"""
    rgb = image.convert("RGB")
    qimg = QImage(
        rgb.tobytes(), rgb.width, rgb.height, rgb.width * 3, QImage.Format.Format_RGB888
    )
    # QImage 不持有 buffer，必须 copy 后再转，否则 PIL 对象回收后是悬垂指针
    return QPixmap.fromImage(qimg.copy())
