"""矢量图标绘制。

**为什么不加载图片资源**：图标若做成 PNG/SVG 文件，就要处理打包时的资源路径，
而本项目要求单目录即可运行。用 QPainterPath 直接画路径，没有文件依赖，
且任意 DPI 都清晰。

所有图标都在 **24×24 的坐标系**里定义，由 :func:`draw_icon` 缩放到目标尺寸。
这样路径数据与图标大小解耦，改尺寸不用改路径。
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap

# 图标设计尺寸
CANVAS = 24.0


def _pen(color: QColor, width: float = 1.8) -> QPen:
    pen = QPen(color, width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def _arrow(painter: QPainter, path: QPainterPath, color: QColor) -> None:
    painter.setPen(_pen(color))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(path)


# ---------- 各图标的路径构造 ----------


def _path_back() -> QPainterPath:
    p = QPainterPath()
    p.moveTo(14, 5)
    p.lineTo(6, 12)
    p.lineTo(14, 19)
    return p


def _path_forward() -> QPainterPath:
    p = QPainterPath()
    p.moveTo(10, 5)
    p.lineTo(18, 12)
    p.lineTo(10, 19)
    return p


def _path_crop() -> QPainterPath:
    """经典裁剪标记：两条"L"形折线交叉，形成重叠的裁剪框。"""
    p = QPainterPath()
    # 左上折线：从上到下，再向右
    p.moveTo(7, 2.5)
    p.lineTo(7, 17)
    p.lineTo(21.5, 17)
    # 右下折线：从左到右，再向下
    p.moveTo(2.5, 7)
    p.lineTo(17, 7)
    p.lineTo(17, 21.5)
    return p


def _path_copy() -> QPainterPath:
    """两个错开的圆角矩形（表示"复制"）。

    后框画在右下、前框在左上，两框错开约 3px。错开量不能太小——太小两个框
    会糊成一团认不出来；太大则显得松散。
    """
    p = QPainterPath()
    p.addRoundedRect(QRectF(3.5, 3.5, 12, 12), 2, 2)
    p.addRoundedRect(QRectF(8.5, 8.5, 12, 12), 2, 2)
    return p


def _path_copy_image() -> QPainterPath:
    """"复制图片"：两张叠起来的图（相框 + 内部的山形）。

    与 :func:`_path_copy` 的区别是加入了图片意象（山形 + 太阳点），
    用于区分"复制图片"和"复制文本"这类纯几何图形看不出来的语义。
    """
    p = QPainterPath()
    # 后面那张（只画左上两段的轮廓，避免线条太密）
    p.moveTo(8, 5)
    p.lineTo(19, 5)
    p.lineTo(19, 16)
    # 前面那张，完整相框
    p.addRoundedRect(QRectF(4, 8, 12, 12), 2, 2)
    # 内部山形：两个三角折线
    p.moveTo(5.5, 18)
    p.lineTo(9, 13.5)
    p.lineTo(11.5, 16)
    p.lineTo(14.5, 12)
    p.lineTo(14.5, 18)
    return p


def _path_pencil() -> QPainterPath:
    p = QPainterPath()
    p.moveTo(4, 20)
    p.lineTo(5, 15)
    p.lineTo(16, 4)
    p.lineTo(20, 8)
    p.lineTo(9, 19)
    p.closeSubpath()
    return p


def _path_ocr() -> QPainterPath:
    """扫描框的四角 + 中间一条扫描线，表示"识别文字"。

    四个角要够长够粗才认得出是"取景框"。最初画成 4px 长的短线，渲染出来
    像散落的碎屑。
    """
    p = QPainterPath()
    arm = 5.0
    m = 3.5
    far = CANVAS - 3.5
    # 左上
    p.moveTo(m, m + arm); p.lineTo(m, m); p.lineTo(m + arm, m)
    # 右上
    p.moveTo(far - arm, m); p.lineTo(far, m); p.lineTo(far, m + arm)
    # 左下
    p.moveTo(m, far - arm); p.lineTo(m, far); p.lineTo(m + arm, far)
    # 右下
    p.moveTo(far - arm, far); p.lineTo(far, far); p.lineTo(far, far - arm)
    # 中间的扫描线
    p.moveTo(7.5, 12)
    p.lineTo(16.5, 12)
    return p


def _path_translate() -> QPainterPath:
    """翻译：左侧一个方框（源），右侧一个箭头指向外（译出）。

    箭头不要从框里穿出来——最初画成从框中心出发，跟框边框撞在一起。
    改成画在框的右侧独立区域，两段式箭头。
    """
    p = QPainterPath()
    p.addRoundedRect(QRectF(3, 5.5, 11, 13), 2, 2)
    # 框内一条横线，暗示"文字"
    p.moveTo(6, 9.5); p.lineTo(11, 9.5)
    p.moveTo(6, 13); p.lineTo(11, 13)
    # 右侧箭头
    p.moveTo(16.5, 12)
    p.lineTo(21, 12)
    p.moveTo(18.5, 9.5)
    p.lineTo(21, 12)
    p.lineTo(18.5, 14.5)
    return p


def _path_save() -> QPainterPath:
    """下箭头 + 底横线（保存/下载）。箭头要够长，否则挤成一团。"""
    p = QPainterPath()
    p.moveTo(12, 3)
    p.lineTo(12, 14.5)
    p.moveTo(7.5, 10)
    p.lineTo(12, 14.5)
    p.lineTo(16.5, 10)
    p.moveTo(4.5, 19.5)
    p.lineTo(19.5, 19.5)
    return p


def _path_zoom_in() -> QPainterPath:
    p = QPainterPath()
    p.addEllipse(QRectF(3, 3, 13, 13))
    p.moveTo(19, 19)
    p.lineTo(14, 14)
    p.moveTo(6.5, 9.5)
    p.lineTo(12.5, 9.5)
    p.moveTo(9.5, 6.5)
    p.lineTo(9.5, 12.5)
    return p


def _path_zoom_out() -> QPainterPath:
    p = QPainterPath()
    p.addEllipse(QRectF(3, 3, 13, 13))
    p.moveTo(19, 19)
    p.lineTo(14, 14)
    p.moveTo(6.5, 9.5)
    p.lineTo(12.5, 9.5)
    return p


def _path_fit() -> QPainterPath:
    """"适应窗口"：一个方框 + 内部四角向外的箭头。

    最初只画四个向内收的箭头，渲染出来跟"关闭 X"几乎分不清。加一个外框
    作为语境，就明确是"把内容装进框里"。
    """
    p = QPainterPath()
    p.addRoundedRect(QRectF(3, 3, 18, 18), 2.5, 2.5)
    # 内部四个向外的小箭头
    p.moveTo(8.5, 8.5); p.lineTo(6.5, 6.5)
    p.moveTo(15.5, 8.5); p.lineTo(17.5, 6.5)
    p.moveTo(8.5, 15.5); p.lineTo(6.5, 17.5)
    p.moveTo(15.5, 15.5); p.lineTo(17.5, 17.5)
    return p


def _path_actual_size() -> QPainterPath:
    p = QPainterPath()
    p.addRoundedRect(QRectF(3, 6, 18, 12), 2, 2)
    p.moveTo(8, 12)
    p.lineTo(16, 12)
    return p


def _path_more() -> QPainterPath:
    p = QPainterPath()
    for cx in (6, 12, 18):
        p.addEllipse(QPointF(cx, 12), 1.4, 1.4)
    return p


def _path_minimize() -> QPainterPath:
    p = QPainterPath()
    p.moveTo(5, 12)
    p.lineTo(19, 12)
    return p


def _path_maximize() -> QPainterPath:
    p = QPainterPath()
    p.addRect(QRectF(5.5, 5.5, 13, 13))
    return p


def _path_restore() -> QPainterPath:
    p = QPainterPath()
    p.addRect(QRectF(4.5, 8.5, 11, 11))
    p.moveTo(8.5, 8.5)
    p.lineTo(8.5, 4.5)
    p.lineTo(19.5, 4.5)
    p.lineTo(19.5, 15.5)
    p.lineTo(15.5, 15.5)
    return p


def _path_close() -> QPainterPath:
    p = QPainterPath()
    p.moveTo(6, 6)
    p.lineTo(18, 18)
    p.moveTo(18, 6)
    p.lineTo(6, 18)
    return p


_PATHS = {
    "back": _path_back,
    "forward": _path_forward,
    "crop": _path_crop,
    "copy": _path_copy,
    "copy_image": _path_copy_image,
    "pencil": _path_pencil,
    "ocr": _path_ocr,
    "translate": _path_translate,
    "save": _path_save,
    "zoom_in": _path_zoom_in,
    "zoom_out": _path_zoom_out,
    "fit": _path_fit,
    "actual": _path_actual_size,
    "more": _path_more,
    "minimize": _path_minimize,
    "maximize": _path_maximize,
    "restore": _path_restore,
    "close": _path_close,
}

# 实心填充的图标（点阵、以及勾选标记）特殊处理
_FILLED = {"more"}


def draw_icon(
    painter: QPainter,
    name: str,
    rect: QRectF,
    color: QColor,
    width: float = 1.8,
) -> None:
    """在 ``rect`` 内绘制图标，等比缩放到 24×24 设计坐标系。"""
    factory = _PATHS.get(name)
    if factory is None:
        return

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    size = min(rect.width(), rect.height())
    scale = size / CANVAS
    painter.translate(rect.center())
    painter.scale(scale, scale)
    painter.translate(-CANVAS / 2, -CANVAS / 2)

    path = factory()
    if name in _FILLED:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawPath(path)
    else:
        _arrow(painter, path, color)
        # 圆角矩形类图标要描边而非填充
        if name in {"copy", "maximize", "restore"}:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)

    painter.restore()


def make_icon_pixmap(name: str, size: int, color: QColor) -> QPixmap:
    """生成图标位图，用于托盘等需要 QIcon 的地方。"""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        draw_icon(painter, name, QRectF(0, 0, size, size), color)
    finally:
        painter.end()
    return pixmap
