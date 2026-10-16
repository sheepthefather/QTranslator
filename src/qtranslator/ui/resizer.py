"""无边框窗口的边缘拖拽缩放。

Qt 去掉 ``FramelessWindowHint`` 后系统就不再提供缩放边框，必须自己实现。
这段逻辑要在**两个平台**上都成立，所以只用 Qt 的跨平台能力
（``windowHandle().startSystemResize``），不用任何平台 API。

做法分三层：

1. **命中判定**：鼠标在窗口边缘若干像素内时，算出该往哪个方向缩放
   （上/下/左/右/四个角，共 8 个方向）。
2. **光标反馈**：进入边缘时把光标换成对应的双箭头形状——没有这个反馈
   用户根本不知道可以拖。
3. **拖动**：按下后在边缘区域内移动即触发系统缩放。

为什么用 ``startSystemResize`` 而不是自己 ``setGeometry``：自己算几何要处理
最小尺寸约束、屏幕边界、以及 DPI 换算，而且拖动时的视觉反馈（Windows 的
半透明预览、Linux 的 compositor 行为）都得自己模拟。交给系统做既省事又
和平台观感一致。

**命中区域要够宽**（默认 6px）。边框只有 1px 的话用户极难点到，体验很差。
同时窗口太小的时候要收缩这个宽度，否则整个窗口都是边缘、中间没法操作。
"""

from __future__ import annotations

from enum import IntFlag

from PySide6.QtCore import QObject, QPoint, Qt
from PySide6.QtWidgets import QWidget

# 边缘命中宽度（像素）。6px 在 100% DPI 下比较舒适。
EDGE = 6

# 窗口小于这个尺寸时，边缘宽度按比例收缩，保证中间留出可操作区域。
_MIN_INNER = 60


class Edge(IntFlag):
    """命中的边缘。用位标志以便表达"左上角"这类组合。"""

    NONE = 0
    TOP = 1
    BOTTOM = 2
    LEFT = 4
    RIGHT = 8

    @property
    def is_corner(self) -> bool:
        return (
            bool(self & (self.TOP | self.BOTTOM))
            and bool(self & (self.LEFT | self.RIGHT))
        )


_CURSORS = {
    Edge.TOP: Qt.CursorShape.SizeVerCursor,
    Edge.BOTTOM: Qt.CursorShape.SizeVerCursor,
    Edge.LEFT: Qt.CursorShape.SizeHorCursor,
    Edge.RIGHT: Qt.CursorShape.SizeHorCursor,
    Edge.TOP | Edge.LEFT: Qt.CursorShape.SizeFDiagCursor,
    Edge.BOTTOM | Edge.RIGHT: Qt.CursorShape.SizeFDiagCursor,
    Edge.TOP | Edge.RIGHT: Qt.CursorShape.SizeBDiagCursor,
    Edge.BOTTOM | Edge.LEFT: Qt.CursorShape.SizeBDiagCursor,
}

_QT_EDGES = {
    Edge.TOP: Qt.Edge.TopEdge,
    Edge.BOTTOM: Qt.Edge.BottomEdge,
    Edge.LEFT: Qt.Edge.LeftEdge,
    Edge.RIGHT: Qt.Edge.RightEdge,
}


class FramelessResizer(QObject):
    """给无边框窗口加边缘拖拽缩放。

    **为什么用应用级事件过滤器**：无边框窗口里的子控件（工具栏、画布）几乎铺满
    整个窗口，鼠标事件都被子控件吃掉，窗口自己收不到——直接在窗口里覆写
    mousePressEvent 是收不到边缘点击的。事件过滤器能在事件到达子控件之前
    看到它，从而在边缘处拦截。

    被拦截的事件返回 True（消费掉），就不会再传给子控件——否则拖边缘会同时
    触发画布平移或工具栏拖动。

    继承 QObject 是必需的：``installEventFilter`` 只接受 QObject。
    """

    def __init__(self, window: QWidget, *, edge: int = EDGE) -> None:
        super().__init__(window)
        self._window = window
        self._edge_px = edge
        self._active: Edge = Edge.NONE
        self._resizing = False
        # 被接管过光标的控件 -> 它原本的光标。用 id() 做键，因为 QWidget
        # 不可哈希为字典键时可能被析构（这里只是短期缓存，够用）。
        self._base_cursors: dict[int, object] = {}

    def install(self) -> None:
        """在应用上安装事件过滤器，开始拦截边缘鼠标事件。"""
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
            self._app = app

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 — Qt 接口
        """拦截发往本窗口任意子控件的鼠标事件。"""
        from PySide6.QtCore import QEvent

        etype = event.type()

        if etype == QEvent.Type.MouseButtonPress:
            if self._belongs(obj, event) and self._try_press(obj, event):
                return True
            return False

        if etype == QEvent.Type.MouseMove:
            if self._belongs(obj, event) and not self._resizing:
                pos = self._to_window(obj, event)
                if pos is not None:
                    # 在边缘则换成缩放光标并消费事件，避免落到子控件上。
                    # 光标要设在事件接收者上——它自己可能设了光标（如画布的手形）
                    # 会盖住我们在窗口上设的（见 update_cursor 的说明）。
                    widget = obj if isinstance(obj, QWidget) else None
                    if self.update_cursor(pos, widget):
                        return True
            return False

        if etype == QEvent.Type.MouseButtonRelease:
            self.release()
            return False

        if etype == QEvent.Type.Leave:
            self.leave()
            return False

        return False

    def _belongs(self, obj, event) -> bool:
        """事件是否发生在**本窗口的客户区**内。

        用 ``window()``（所属顶层窗口）判断，**不能用 parentWidget() 链条**。

        原因：弹出式控件（``QMenu``、下拉框、工具提示）在 Qt 里是**独立的顶层
        窗口**（``isWindow() == True``），但它们的 ``parentWidget()`` 仍指向
        宿主窗口。用父链判断会把它们误认为属于本窗口，于是：

        1. 过滤器的坐标换算 ``obj.mapTo(window, ...)`` 在弹出窗上用错参考系，
           算出远离窗口的点；
        2. 那个点若恰好落在窗口边缘判定区内，过滤器就返回 True 把事件吃掉，
          而事件本该给菜单——菜单收不到点击，看起来就是**卡住**（切到别的
           窗口再切回来才恢复，因为那时菜单已关闭）。
        """
        widget = obj if isinstance(obj, QWidget) else None
        if widget is None:
            return False
        # window() 对弹出窗返回它自己，对普通子控件返回宿主顶层窗口
        return widget.window() is self._window

    def _to_window(self, obj, event):
        """把事件坐标从 ``obj`` 的坐标系转成窗口客户区坐标。

        只对**同一顶层窗口内**的控件有意义。弹出窗（菜单等）的坐标参考系不同，
        换算结果无意义——所以用 :meth:`_belongs` 的同一判据先挡住，
        返回 None 让调用方放弃处理。
        """
        if not isinstance(obj, QWidget):
            return None
        if obj.window() is not self._window:
            return None
        local = event.position().toPoint()
        return obj.mapTo(self._window, local)

    def _try_press(self, obj, event) -> bool:
        """尝试开始缩放。

        ``obj`` 是事件的接收者（eventFilter 的第一个参数）。**不能用
        ``event.widget()``** —— 那是 Qt4 的 API，Qt5/6 的 QMouseEvent 没有
        这个方法（实测直接 AttributeError）。
        """
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        pos = self._to_window(obj, event)
        if pos is None:
            return False
        return self.press_at(pos)

    def press_at(self, pos: QPoint) -> bool:
        edge = self.hit(pos)
        if edge is Edge.NONE:
            return False

        handle = self._window.windowHandle()
        if handle is None:
            return False

        qt_edges = Qt.Edge(0)
        for bit, qt_edge in _QT_EDGES.items():
            if edge & bit:
                qt_edges |= qt_edge

        # 交给系统做缩放：它会处理最小尺寸、屏幕边界、平台视觉反馈
        self._resizing = handle.startSystemResize(qt_edges)
        self._active = edge
        return True

    def _edge_width(self) -> int:
        """实际边缘宽度。窗口很小时收缩，避免整个窗口都是边缘。"""
        w, h = self._window.width(), self._window.height()
        limit = max(2, min(w, h) // 2 - _MIN_INNER)
        return max(2, min(self._edge_px, limit))

    def hit(self, pos: QPoint) -> Edge:
        """判断窗口坐标 ``pos`` 落在哪条边/角上。"""
        if self._window.isMaximized() or self._window.isFullScreen():
            # 最大化时不允许拖拽缩放——系统窗口管理器也是这个行为
            return Edge.NONE

        w, h = self._window.width(), self._window.height()
        e = self._edge_width()
        x, y = pos.x(), pos.y()

        edge = Edge.NONE
        if y <= e:
            edge |= Edge.TOP
        elif y >= h - e:
            edge |= Edge.BOTTOM
        if x <= e:
            edge |= Edge.LEFT
        elif x >= w - e:
            edge |= Edge.RIGHT
        return edge

    def update_cursor(self, pos: QPoint, on_widget: QWidget | None = None) -> bool:
        """按位置更新光标形状。返回是否命中边缘（调用方据此拒绝其他处理）。

        **光标必须设在鼠标当前所在的子控件上，不能只设在窗口上。**

        ``QWidget.setCursor()`` 设的光标优先于父级：画布自己设了手形光标
        （表示可拖动平移），于是鼠标在画布上时，窗口上设的双箭头会被画布的
        手形盖住。表现为**左右边缘看不到缩放光标、以为不能缩放**，而上下边缘
        （落在工具栏/状态栏上，它们没设自己的光标）却正常。

        离开边缘时**恢复该控件原本的光标**，否则画布会一直显示双箭头、
        丢掉"可拖动平移"的手形提示。
        """
        edge = self.hit(pos)
        target = on_widget if on_widget is not None else self._window

        if edge is Edge.NONE:
            original = self._base_cursors.get(id(target))
            if original is not None:
                target.setCursor(original)
            return False

        # 第一次接管这个控件的光标时，先记下它原本的光标
        if id(target) not in self._base_cursors:
            self._base_cursors[id(target)] = target.cursor()
        target.setCursor(_CURSORS[edge])
        return True

    def release(self) -> None:
        self._resizing = False
        self._active = Edge.NONE

    def leave(self) -> None:
        """鼠标离开窗口时恢复默认光标。"""
        if not self._resizing:
            self._window.unsetCursor()
