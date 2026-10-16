"""无边框窗口边缘缩放的回归测试。

这类逻辑错了不会抛异常，只会表现为"拖边缘没反应"或"拖边缘变成了拖窗口"，
很难靠肉眼定位。所以直接断言命中判定和事件拦截行为。

用法::

    python tools/test_resizer.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from qtranslator.ui.resizer import Edge, FramelessResizer  # noqa: E402

W, H = 800, 600
_FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if not ok:
        _FAILED.append(name)
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail and not ok else ''}")


def test_edge_hit() -> None:
    """8 个方向 + 中心都要判对。"""
    win = QWidget()
    win.resize(W, H)
    r = FramelessResizer(win)

    cases = [
        ((3, H // 2), Edge.LEFT),
        ((W - 3, H // 2), Edge.RIGHT),
        ((W // 2, 3), Edge.TOP),
        ((W // 2, H - 3), Edge.BOTTOM),
        ((3, 3), Edge.LEFT | Edge.TOP),
        ((W - 3, 3), Edge.RIGHT | Edge.TOP),
        ((3, H - 3), Edge.LEFT | Edge.BOTTOM),
        ((W - 3, H - 3), Edge.RIGHT | Edge.BOTTOM),
        ((W // 2, H // 2), Edge.NONE),
    ]
    for (x, y), want in cases:
        got = r.hit(QPoint(x, y))
        check(f"命中 ({x},{y}) = {want!r}", got == want, f"得到 {got!r}")

    # 四角必须是 corner
    for x, y in [(3, 3), (W - 3, 3), (3, H - 3), (W - 3, H - 3)]:
        check(f"({x},{y}) 是角", r.hit(QPoint(x, y)).is_corner)


def test_every_edge_has_cursor() -> None:
    """每个可命中的边缘都要有对应的光标形状——漏一个用户就不知道该能拖。"""
    win = QWidget()
    win.resize(W, H)
    r = FramelessResizer(win)
    seen = set()
    for x in (3, W // 2, W - 3):
        for y in (3, H // 2, H - 3):
            seen.add(r.hit(QPoint(x, y)))

    for edge in seen:
        if edge is Edge.NONE:
            continue
        r.update_cursor(QPoint(3, 3))  # 先设成别的
        # 找到该 edge 的一个代表点
        probe = {
            Edge.LEFT: (3, H // 2), Edge.RIGHT: (W - 3, H // 2),
            Edge.TOP: (W // 2, 3), Edge.BOTTOM: (W // 2, H - 3),
            Edge.LEFT | Edge.TOP: (3, 3), Edge.RIGHT | Edge.TOP: (W - 3, 3),
            Edge.LEFT | Edge.BOTTOM: (3, H - 3), Edge.RIGHT | Edge.BOTTOM: (W - 3, H - 3),
        }[edge]
        r.update_cursor(QPoint(*probe))
        shape = win.cursor().shape()
        check(f"{edge!r} 有光标反馈", shape != Qt.CursorShape.ArrowCursor, f"得到 {shape}")


def test_edge_width_shrinks_on_tiny_window() -> None:
    """窗口很小时边缘宽度要收缩，否则整个窗口都成了边缘、中间没法操作。"""
    win = QWidget()
    win.resize(W, H)
    r = FramelessResizer(win)
    check("正常窗口边缘宽度 = 默认值", r._edge_width() == 6, f"得到 {r._edge_width()}")

    win.resize(120, 120)
    small = r._edge_width()
    check("小窗口边缘宽度收缩", small < 6, f"得到 {small}")
    # 中心仍必须可命中（不被判为边缘）
    check("小窗口中心不是边缘", r.hit(QPoint(60, 60)) is Edge.NONE)


def test_maximized_disables_edges() -> None:
    """最大化时不允许拖边缘缩放——与系统窗口管理器行为一致。"""
    win = QWidget()
    win.resize(W, H)
    r = FramelessResizer(win)
    # 用一个假的 isMaximized 覆盖，避免真的最大化（headless 下无意义）
    win.isMaximized = lambda: True  # type: ignore[method-assign]
    check("最大化时所有边缘失效", r.hit(QPoint(3, 3)) is Edge.NONE)


def test_center_passes_through() -> None:
    """中心区域不能被拦截，否则画布无法平移。

    边缘按下能否真正开始缩放取决于平台（``startSystemResize`` 需要真实的
    windowHandle，未显示的控件没有）。所以这里只断言**分流**：中心必须放行，
    而边缘要么被拦截、要么因无 handle 而失败——绝不能悄悄放行到子控件。
    """
    win = QWidget()
    win.resize(W, H)
    r = FramelessResizer(win)

    def press(x: float, y: float) -> bool:
        ev = QMouseEvent(
            QEvent.Type.MouseButtonPress, QPointF(x, y),
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        return r.eventFilter(win, ev)

    check("中心按下放行", not press(W // 2, H // 2))

    # 边缘：用未显示的控件时 startSystemResize 必然失败，但命中判定必须正确
    win.isMaximized = lambda: False  # type: ignore[method-assign]
    check("边缘命中判定正确", r.hit(QPoint(3, H // 2)) == Edge.LEFT)
    check("无 windowHandle 时边缘不误放行到子控件", not press(W // 2, H // 2))


def test_non_target_widget_ignored() -> None:
    """其他窗口的控件不能被拦截——过滤器装在 QApplication 上，看得到全部事件。"""
    win = QWidget()
    win.resize(W, H)
    other = QWidget()  # 独立顶层控件，不属于 win
    other.resize(W, H)
    r = FramelessResizer(win)

    ev = QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(3, 3),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    check("不相关控件的事件放行", not r.eventFilter(other, ev))


def test_popup_menu_not_intercepted() -> None:
    """弹出窗（菜单等）的事件**绝不能**被拦截。

    弹出窗在 Qt 里是独立顶层窗口（``isWindow() == True``），但
    ``parentWidget()`` 仍指向宿主窗口。早期用父链判断归属，于是菜单被当成
    宿主的一部分：坐标换算用错参考系算出远离窗口的点，若那点落在边缘判定区，
    过滤器就把事件吃掉——菜单收不到点击，表现为**点 `···` 卡住**，
    切到别的窗口再切回来才恢复。

    修复是用 ``window()`` 而非父链判断归属。这条测试把它钉住。
    """
    from PySide6.QtWidgets import QMenu

    win = QWidget()
    win.resize(W, H)
    r = FramelessResizer(win)

    menu = QMenu(win)
    menu.addAction("测试项")
    menu.popup(QPoint(10, 10))

    check("菜单的 window() 是自己（是顶层窗口）", menu.isWindow())
    check("菜单不算宿主窗口的客户区", not r._belongs(menu, None))
    check("菜单坐标不参与窗口换算", r._to_window(menu, _fake_event(10, 10)) is None)

    check(
        "菜单上的点击放行（否则菜单会卡住）",
        not r.eventFilter(menu, _press(10, 10)),
    )
    menu.close()


def _fake_event(x: float, y: float):
    """构造只有 position() 的假事件，供 _to_window 的坐标换算测试使用。"""

    class _E:
        def position(self):
            return QPointF(x, y)

    return _E()


def _press(x: float, y: float, button=Qt.MouseButton.LeftButton) -> QMouseEvent:
    return QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(x, y),
        button, button, Qt.KeyboardModifier.NoModifier,
    )


def test_cursor_set_on_widget_under_pointer() -> None:
    """光标必须设在鼠标所在的子控件上，否则会被子控件自己的光标盖住。

    真实 bug：画布设了手形光标（提示可拖动平移），``setCursor`` 优先于父级，
    于是窗口上设的双箭头被盖住——**左右边缘看不到缩放光标、以为不能缩放**，
    而上下边缘（落在工具栏/状态栏上）却正常。这种"部分方向失效"最容易
    被当成玄学。

    这条测试用两个子控件（一个自带手形光标、一个不带）验证：
    边缘处两者都显示缩放光标，且离开边缘后**恢复各自原本的光标**。
    """
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QMouseEvent

    win = QWidget()
    win.resize(W, H)

    # 左边一个设了手形光标的子控件（模拟画布），右边一个不设（模拟状态栏）
    left = QWidget(win)
    left.setGeometry(0, 0, W // 2, H)
    left.setCursor(Qt.CursorShape.OpenHandCursor)
    right = QWidget(win)
    right.setGeometry(W // 2, 0, W // 2, H)

    r = FramelessResizer(win)

    def move(widget: QWidget, x: float, y: float) -> None:
        ev = QMouseEvent(
            QEvent.Type.MouseMove, QPointF(x, y),
            Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        r.eventFilter(widget, ev)

    # 左边缘：事件在 left 控件上，局部坐标 (3, H/2)
    move(left, 3, H // 2)
    check(
        "左边缘：自带光标的控件上显示缩放光标",
        left.cursor().shape() == Qt.CursorShape.SizeHorCursor,
        f"得到 {left.cursor().shape()}",
    )

    # 右边缘：事件在 right 控件上
    move(right, 3, H // 2)  # right 的局部 x=3 对应窗口 x=W/2+3，不在边缘
    # 真正落在右边缘的局部坐标
    move(right, W // 2 - 3, H // 2)
    check(
        "右边缘：无自带光标的控件上也显示缩放光标",
        right.cursor().shape() == Qt.CursorShape.SizeHorCursor,
        f"得到 {right.cursor().shape()}",
    )

    # 回到左控件中心：必须恢复它原本的手形光标
    move(left, W // 4, H // 2)
    check(
        "离开边缘后恢复原本光标（手形）",
        left.cursor().shape() == Qt.CursorShape.OpenHandCursor,
        f"得到 {left.cursor().shape()}",
    )


def main() -> int:
    QApplication.instance() or QApplication(sys.argv)
    test_edge_hit()
    test_every_edge_has_cursor()
    test_edge_width_shrinks_on_tiny_window()
    test_maximized_disables_edges()
    test_center_passes_through()
    test_non_target_widget_ignored()
    test_popup_menu_not_intercepted()
    test_cursor_set_on_widget_under_pointer()

    print()
    if _FAILED:
        print(f"{len(_FAILED)} 项失败:")
        for name in _FAILED:
            print(f"  - {name}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
