"""自绘标题栏与工具栏控件。

参照 Windows 截图工具的样式：无系统标题栏，顶部一条自绘工具栏承担
"图标按钮 + 拖拽窗口 + 最小化/最大化/关闭"三件事。

**为什么无边框**：系统标题栏在 Windows 上是浅灰渐变、Linux 上各桌面环境
样式不一，跟深色工具栏拼在一起很割裂。自绘能保证两个平台观感一致。
代价是要自己实现拖动、缩放、最大化——见 :class:`TitleBar` 和
:class:`FramelessWindow`。
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPaintEvent
from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QToolButton, QWidget

from .icons import draw_icon

# 深色主题配色。集中在这里便于将来做浅色主题。
BG = QColor(43, 43, 46)
BG_HOVER = QColor(64, 64, 68)
BG_PRESSED = QColor(80, 80, 86)
FG = QColor(225, 225, 228)
FG_DIM = QColor(150, 150, 156)
ACCENT = QColor(76, 160, 255)
DANGER_HOVER = QColor(196, 43, 28)


class IconButton(QToolButton):
    """纯图标按钮。图标用矢量路径绘制，随 DPI 清晰。"""

    def __init__(
        self,
        icon_name: str,
        tooltip: str = "",
        *,
        size: int = 36,
        icon_size: int = 20,
        danger: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._icon_name = icon_name
        self._icon_size = icon_size
        self._danger = danger
        self._hovered = False
        self._pressed = False

        self.setFixedSize(size, size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        if tooltip:
            self.setToolTip(tooltip)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def set_icon_name(self, name: str) -> None:
        self._icon_name = name
        self.update()

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self._pressed = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self._pressed = True
        self.update()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._pressed = False
        self.update()
        super().mouseReleaseEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: ARG002
        painter = QPainter(self)
        try:
            self._paint(painter)
        finally:
            painter.end()

    def _paint(self, painter: QPainter) -> None:
        rect = self.rect()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        if self._pressed or self._hovered:
            bg = DANGER_HOVER if self._danger else (BG_PRESSED if self._pressed else BG_HOVER)
            # 圆角高亮比直角更接近参照图的观感
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(bg)
            painter.drawRoundedRect(QRectF(rect).adjusted(2, 4, -2, -4), 4, 4)

        if not self.isEnabled():
            color = QColor(108, 108, 114)
        elif self._hovered and self._danger:
            color = QColor(255, 255, 255)
        else:
            color = FG

        pad = (rect.width() - self._icon_size) / 2
        target = QRectF(pad, pad, self._icon_size, self._icon_size)
        draw_icon(painter, self._icon_name, target, color, width=2.0)


class ToolbarSeparator(QWidget):
    """工具栏里的一组按钮之间的细竖线。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # 宽度 = 竖线本身 1px + 两侧各 6px 间距。参照图里组间留白比组内
        # 明显更宽，这个不对称是"分组"能被看出来的关键。
        self.setFixedWidth(13)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: ARG002
        painter = QPainter(self)
        try:
            painter.setPen(QColor(72, 72, 78))
            x = self.width() // 2
            painter.drawLine(x, 9, x, self.height() - 9)
        finally:
            painter.end()


class TitleBar(QWidget):
    """自绘标题栏 / 工具栏。

    承担三件事：
    1. 放置图标按钮（子控件，布局管理）
    2. 空白区域拖动窗口
    3. 双击空白区域切换最大化
    """

    drag_requested = Signal(QPoint)  # 全局坐标下的拖动起点

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(48)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(8, 0, 8, 0)
        # 组内间距 2px：按钮本身 36px 宽、图标 20px，视觉上已有留白，
        # 再给大间距会显得松散
        self._layout.setSpacing(2)

        # 拖动状态：记录按下位置与窗口原位置，move 时算增量
        self._drag_origin: QPoint | None = None
        self._window_origin: QPoint | None = None

    def add(self, widget: QWidget) -> None:
        self._layout.addWidget(widget)

    def add_separator(self) -> None:
        self._layout.addWidget(ToolbarSeparator(self))

    def add_stretch(self) -> None:
        self._layout.addStretch(1)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: ARG002
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), BG)
            # 底部一条细分割线，跟内容区拉开层次
            painter.setPen(QColor(28, 28, 30))
            painter.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        finally:
            painter.end()

    # ---------- 拖动窗口 ----------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        # 只有点在没有子控件的地方才开始拖——子控件（按钮）要自己处理点击
        if event.button() == Qt.MouseButton.LeftButton and self.childAt(event.position().toPoint()) is None:
            window = self.window()
            if window.isMaximized():
                # 已最大化时拖动应该"还原并跟随光标"，这是 Windows 的行为
                self.window().showNormal()
            self._drag_origin = event.globalPosition().toPoint()
            self._window_origin = window.pos()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_origin is None or self._window_origin is None:
            return
        delta = event.globalPosition().toPoint() - self._drag_origin
        self.window().move(self._window_origin + delta)
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_origin = None
        self._window_origin = None
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if self.childAt(event.position().toPoint()) is None:
            window = self.window()
            if window.isMaximized():
                window.showNormal()
            else:
                window.showMaximized()
            event.accept()


class StatusLabel(QWidget):
    """状态栏。左侧提示文字，右侧缩放比例。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(26)
        self._message = ""
        self._zoom = ""

    def set_message(self, text: str) -> None:
        self._message = text
        self.update()

    def set_zoom(self, scale: float) -> None:
        self._zoom = f"{scale * 100:.0f}%"
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: ARG002
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), BG)
            painter.setPen(QColor(30, 30, 32))
            painter.drawLine(0, 0, self.width(), 0)

            font = QFont()
            font.setPointSize(9)
            painter.setFont(font)

            painter.setPen(FG_DIM)
            metrics = painter.fontMetrics()
            text = metrics.elidedText(self._message, Qt.TextElideMode.ElideRight, self.width() - 90)
            painter.drawText(
                QRectF(12, 0, self.width() - 90, self.height()),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                text,
            )

            if self._zoom:
                painter.setPen(FG)
                painter.drawText(
                    QRectF(self.width() - 78, 0, 66, self.height()),
                    Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                    self._zoom,
                )
        finally:
            painter.end()
