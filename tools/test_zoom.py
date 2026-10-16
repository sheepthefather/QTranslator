"""缩放 / 平移行为回归测试。

这些是**无声失败**的检查：缩放逻辑错了不会抛异常，只会让图片位置看起来
不对（贴在角上、该居中的没居中、拖动时另一个轴乱跑）。所以用合成数据
直接断言几何结果。

用法::

    python tools/test_zoom.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image  # noqa: E402
from PySide6.QtCore import QEvent, QPointF, QSize, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent, QResizeEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from qtranslator.ui.canvas import ImageCanvas  # noqa: E402

VIEW_W, VIEW_H = 900, 700
TOL = 0.75  # 亚像素容差


_FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if not ok:
        _FAILED.append(name)
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail and not ok else ''}")


def make(shape: tuple[int, int]) -> ImageCanvas:
    c = ImageCanvas()
    c.resize(VIEW_W, VIEW_H)
    c.set_image(Image.new("RGB", shape))
    c.fit_to_window()
    return c


def geom(c: ImageCanvas) -> tuple[float, float, float, float, float]:
    """返回 (缩放后宽, 缩放后高, X 边距, Y 边距, scale)。"""
    sw = c._image_size[0] * c.scale
    sh = c._image_size[1] * c.scale
    return sw, sh, c._offset.x(), c._offset.y(), c.scale


def test_floor_is_flush_one_axis() -> None:
    """到达缩放下限时，必须**恰好**有一个方向贴满。"""
    for shape in [(1398, 1000), (600, 1400), (1000, 1000), (2000, 500), (500, 2000)]:
        c = make(shape)
        sw, sh, ox, oy, s = geom(c)
        flush_x = abs(VIEW_W - sw) < TOL
        flush_y = abs(VIEW_H - sh) < TOL
        check(
            f"下限贴边 {shape}",
            flush_x or flush_y,
            f"X gap={VIEW_W - sw:.1f} Y gap={VIEW_H - sh:.1f}",
        )
        check(f"下限等于 min 比值 {shape}", abs(s - c._min_scale()) < 1e-9)


def test_unfilled_axis_is_centered() -> None:
    """占不满的那个维度必须**正好居中**——这是明确要求的规则。

    贴满的那个方向 gap=0；另一个方向两侧边距相等。
    """
    for shape in [(1398, 1000), (600, 1400), (1000, 1000), (2000, 500)]:
        c = make(shape)
        sw, sh, ox, oy, _ = geom(c)
        if sw < VIEW_W - TOL:
            expected = (VIEW_W - sw) / 2
            check(
                f"X 占不满时居中 {shape}",
                abs(ox - expected) < TOL,
                f"x={ox:.1f} expected={expected:.1f}",
            )
        if sh < VIEW_H - TOL:
            expected = (VIEW_H - sh) / 2
            check(
                f"Y 占不满时居中 {shape}",
                abs(oy - expected) < TOL,
                f"y={oy:.1f} expected={expected:.1f}",
            )


def test_partial_overflow_keeps_other_axis_centered() -> None:
    """一轴溢出、另一轴不足时，不足的那轴仍必须居中。

    这是最容易漏掉的中间态：图片已放大到宽度超出，但高度还没到。
    此时高度没有平移自由度，必须居中；宽度可拖。
    """
    c = ImageCanvas()
    c.resize(VIEW_W, VIEW_H)
    c.set_image(Image.new("RGB", (1200, 500)))
    c.fit_to_window()
    c.zoom_by(1.3)

    sw, sh, ox, oy, _ = geom(c)
    check("中间态：宽度确实溢出", sw > VIEW_W, f"sw={sw:.1f}")
    check("中间态：高度确实不足", sh < VIEW_H, f"sh={sh:.1f}")
    check(
        "中间态：不足的高度居中",
        abs(oy - (VIEW_H - sh) / 2) < TOL,
        f"y={oy:.1f} expected={(VIEW_H - sh) / 2:.1f}",
    )


def test_pan_preserves_centered_axis() -> None:
    """水平拖动不能破坏另一个轴的居中。"""
    c = ImageCanvas()
    c.resize(VIEW_W, VIEW_H)
    c.set_image(Image.new("RGB", (1200, 500)))
    c.fit_to_window()
    c.zoom_by(1.3)
    _, sh, _, oy_before, _ = geom(c)

    press = QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(400, 300),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    move = QMouseEvent(
        QEvent.Type.MouseMove, QPointF(300, 300),
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    release = QMouseEvent(
        QEvent.Type.MouseButtonRelease, QPointF(300, 300),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    c.mousePressEvent(press)
    c.mouseMoveEvent(move)
    c.mouseReleaseEvent(release)

    _, _, _, oy_after, _ = geom(c)
    check("拖动后 Y 仍居中", abs(oy_after - oy_before) < TOL, f"{oy_before:.1f} -> {oy_after:.1f}")


def test_zoom_out_from_any_corner_centers() -> None:
    """从任意角落缩到下界，结果都必须是贴边 + 居中（不受起始位置影响）。"""
    for corner in [(5, 5), (VIEW_W - 5, 5), (5, VIEW_H - 5), (VIEW_W - 5, VIEW_H - 5), (450, 350)]:
        c = ImageCanvas()
        c.resize(VIEW_W, VIEW_H)
        c.set_image(Image.new("RGB", (1398, 1000)))
        c.fit_to_window()
        c.zoom_by(3.0)
        for _ in range(25):
            c.zoom_by(1 / 1.25, QPointF(*corner))

        sw, sh, ox, oy, s = geom(c)
        check(f"角落 {corner} 到下限", abs(s - c._min_scale()) < 1e-9)
        flush = abs(VIEW_W - sw) < TOL or abs(VIEW_H - sh) < TOL
        check(f"角落 {corner} 贴边", flush)
        if sw < VIEW_W - TOL:
            check(f"角落 {corner} X 居中", abs(ox - (VIEW_W - sw) / 2) < TOL)
        if sh < VIEW_H - TOL:
            check(f"角落 {corner} Y 居中", abs(oy - (VIEW_H - sh) / 2) < TOL)


def test_zoom_anchor_exact() -> None:
    """放大时以光标为锚点，光标下的图片点不能移动。

    **只在两个轴都溢出时成立。** 占不满的轴被锁死在居中（这是硬规则），
    该轴上没有平移自由度，锚点自然无法保持。所以这里用四个角都溢出的
    大图来测，保证测的是锚点逻辑本身。
    """
    c = ImageCanvas()
    c.resize(VIEW_W, VIEW_H)
    c.set_image(Image.new("RGB", (1398, 1000)))
    c.fit_to_window()
    c.zoom_by(3.0)  # 放大到两个方向都溢出
    sw, sh = 1398 * c.scale, 1000 * c.scale
    check("锚点测试前提：两轴都溢出", sw > VIEW_W and sh > VIEW_H, f"{sw:.0f}x{sh:.0f}")

    worst = 0.0
    for a in [(50, 50), (850, 650), (450, 350), (20, 680), (880, 20)]:
        p = QPointF(*a)
        before = (p - c._offset) / c.scale
        c.zoom_by(1.25, p)
        after = (p - c._offset) / c.scale
        worst = max(worst, abs(before.x() - after.x()), abs(before.y() - after.y()))
    check("放大锚点精确（两轴均溢出）", worst < 0.01, f"漂移 {worst:.4f}px")


def test_centering_beats_anchor_on_underfilled_axis() -> None:
    """占不满的轴上，居中规则**优先于**锚点。

    这不是妥协而是唯一自洽的行为：该轴没有平移自由度。这条测试把"优先级"
    显式钉住——若将来有人把锚点提到居中之上，会立刻失败。
    """
    c = ImageCanvas()
    c.resize(VIEW_W, VIEW_H)
    c.set_image(Image.new("RGB", (600, 1400)))
    c.fit_to_window()  # 下限：宽 300（占不满）、高 700（贴满）

    p = QPointF(20, 20)  # 角落锚点，故意选会让宽度偏移的位置
    c.zoom_by(1.25, p)

    sw, sh = 600 * c.scale, 1400 * c.scale
    check("宽度仍占不满", sw < VIEW_W, f"w={sw:.0f}")
    check(
        "占不满的宽度轴仍居中（居中优先于锚点）",
        abs(c._offset.x() - (VIEW_W - sw) / 2) < TOL,
        f"x={c._offset.x():.1f} expected={(VIEW_W - sw) / 2:.1f}",
    )


def test_anchor_resumes_once_axis_overflows() -> None:
    """轴一旦溢出，锚点立刻恢复精确——居中锁定不该长期绑架缩放。"""
    c = ImageCanvas()
    c.resize(VIEW_W, VIEW_H)
    c.set_image(Image.new("RGB", (600, 1400)))
    c.fit_to_window()
    p = QPointF(20, 20)
    while 600 * c.scale <= VIEW_W:
        c.zoom_by(1.25, p)

    worst = 0.0
    for _ in range(3):
        before = (p - c._offset) / c.scale
        c.zoom_by(1.25, p)
        after = (p - c._offset) / c.scale
        worst = max(worst, abs(before.x() - after.x()), abs(before.y() - after.y()))
    check("溢出后锚点恢复精确", worst < 0.01, f"漂移 {worst:.4f}px")


def test_resize_keeps_scale_above_floor() -> None:
    """窗口变化后比例不得低于新下限（视图越大下限越高）。"""
    c = ImageCanvas()
    c.resize(1000, 700)
    c.set_image(Image.new("RGB", (800, 600)))
    c.fit_to_window()
    for vw, vh in [(300, 200), (1700, 1300), (600, 400), (2200, 1600), (900, 700)]:
        c.resize(vw, vh)
        c.resizeEvent(QResizeEvent(QSize(vw, vh), QSize(vw, vh)))
        check(f"resize {vw}x{vh} 不低于下限", c.scale >= c._min_scale() - 1e-9)


def test_no_overscroll() -> None:
    """放大后不能把图片拖到视图外。"""
    c = ImageCanvas()
    c.resize(VIEW_W, VIEW_H)
    c.set_image(Image.new("RGB", (1398, 1000)))
    c.fit_to_window()
    c.zoom_by(4.0)
    c._offset = QPointF(1e6, 1e6)
    c._clamp_offset()
    sw, sh, ox, oy, _ = geom(c)
    check("向右下越界被夹住", ox <= TOL and oy <= TOL, f"offset=({ox:.0f},{oy:.0f})")

    c._offset = QPointF(-1e6, -1e6)
    c._clamp_offset()
    sw, sh, ox, oy, _ = geom(c)
    check(
        "向左上越界被夹住",
        ox >= (VIEW_W - sw) - TOL and oy >= (VIEW_H - sh) - TOL,
        f"offset=({ox:.0f},{oy:.0f})",
    )


def test_no_violation_across_parameter_sweep() -> None:
    """全参数扫描：任意视图 × 任意图片 × 任意锚点 × 任意缩放序列，
    **每一步之后**都必须满足两条规则。

    这条测试来自一个真实 bug：早期给溢出轴加了 ¼ 视图的"松弛量"以保锚点，
    结果锚点在边角时图片会被推出视图（画面边露出一条黑边），用户必须拖一下
    才恢复。当时只测了少数固定参数，没扫到。这个扫描在修复前有 271 项违例。
    """
    views = [(1190, 413), (800, 600), (1400, 500), (600, 900), (1000, 1000), (1600, 420)]
    shapes = [(3024, 1100), (1100, 3024), (1920, 1080), (800, 600), (4000, 1000), (700, 1500)]
    anchors = [None, QPointF(10, 10), QPointF(500, 200), QPointF(1180, 400)]
    sequences = [
        (1.25, 1.25, 1.25, 0.8, 0.8, 0.8),
        (0.8, 0.8, 1.25, 1.25, 1.25, 0.8),
        (1.25, 0.8, 1.25, 0.8, 0.8, 1.25),
        (0.8, 1.25, 0.8, 1.25),
    ]

    violations: list[str] = []
    total = 0
    for vw, vh in views:
        for iw, ih in shapes:
            for anchor in anchors:
                for seq in sequences:
                    c = ImageCanvas()
                    c.resize(vw, vh)
                    c.set_image(Image.new("RGB", (iw, ih)))
                    c.fit_to_window()
                    for f in seq:
                        c.zoom_by(f, anchor)
                        total += 1
                        s = c.scale
                        sw, sh = iw * s, ih * s

                        # X 轴
                        if sw > vw + 0.6:
                            if not (vw - sw - 0.7 <= c._offset.x() <= 0.7):
                                violations.append(
                                    f"视图{vw}x{vh} 图{iw}x{ih} 锚{anchor} f={f} "
                                    f"X={c._offset.x():.2f} 越界"
                                )
                        elif abs(c._offset.x() - (vw - sw) / 2) > 0.7:
                            violations.append(
                                f"视图{vw}x{vh} 图{iw}x{ih} 锚{anchor} f={f} "
                                f"X 占不满但未居中"
                            )

                        # Y 轴
                        if sh > vh + 0.6:
                            if not (vh - sh - 0.7 <= c._offset.y() <= 0.7):
                                violations.append(
                                    f"视图{vw}x{vh} 图{iw}x{ih} 锚{anchor} f={f} "
                                    f"Y={c._offset.y():.2f} 越界"
                                )
                        elif abs(c._offset.y() - (vh - sh) / 2) > 0.7:
                            violations.append(
                                f"视图{vw}x{vh} 图{iw}x{ih} 锚{anchor} f={f} "
                                f"Y 占不满但未居中"
                            )

    check(f"参数扫描 {total} 步无违例", not violations, "; ".join(violations[:3]))


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    test_floor_is_flush_one_axis()
    test_unfilled_axis_is_centered()
    test_partial_overflow_keeps_other_axis_centered()
    test_pan_preserves_centered_axis()
    test_zoom_out_from_any_corner_centers()
    test_zoom_anchor_exact()
    test_centering_beats_anchor_on_underfilled_axis()
    test_anchor_resumes_once_axis_overflows()
    test_resize_keeps_scale_above_floor()
    test_no_overscroll()
    test_no_violation_across_parameter_sweep()
    _ = app

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
