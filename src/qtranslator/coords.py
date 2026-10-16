"""坐标系约定。

整条处理管线（截图裁剪 / OCR / 擦除 / 回填）统一在**物理像素**坐标系里工作，
因为多显示器混合缩放（Windows 主屏 150% + 副屏 100%）和 Linux HiDPI 下，
屏幕逻辑坐标与物理像素不是 1:1。

只有两个坐标系：

- :class:`PixelRect` —— 管线坐标系。物理像素。所有送进 OCR、擦除、回填的坐标都是它。
- :class:`DisplayRect` —— 窗口显示坐标系。Qt 逻辑像素。只在选区窗和预览窗内部出现。

两者之间只允许通过本模块的 :func:`display_to_pixel` / :func:`pixel_to_display` 互转。
不要在别处手写乘除缩放比——违反这一条会在加多显示器支持时到处漏算。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, NamedTuple, Sequence


class Point(NamedTuple):
    x: float
    y: float


@dataclass(frozen=True, slots=True)
class PixelRect:
    """物理像素坐标系下的矩形。左闭右开：``x``/``y`` 含，``x+w``/``y+h`` 不含。"""

    x: int
    y: int
    w: int
    h: int

    def __post_init__(self) -> None:
        if self.w < 0 or self.h < 0:
            raise ValueError(f"PixelRect 宽高不可为负: {self!r}")

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    @property
    def area(self) -> int:
        return self.w * self.h

    def to_slice(self) -> tuple[slice, slice]:
        """转成 numpy/PIL 的 ``[y, x]`` 切片。"""
        return slice(self.y, self.bottom), slice(self.x, self.right)

    @classmethod
    def from_corners(cls, p0: Point, p1: Point) -> "PixelRect":
        """由拖拽的两个角点构造，自动归一化方向。"""
        x0, x1 = sorted((p0.x, p1.x))
        y0, y1 = sorted((p0.y, p1.y))
        x, y = int(round(x0)), int(round(y0))
        return cls(x, y, int(round(x1)) - x, int(round(y1)) - y)

    @classmethod
    def from_quad(cls, quad: Iterable[Sequence[float]]) -> "PixelRect":
        """由 OCR 返回的四点多边形求外接矩形。"""
        pts = [(float(p[0]), float(p[1])) for p in quad]
        if not pts:
            raise ValueError("quad 为空")
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return cls.from_corners(Point(min(xs), min(ys)), Point(max(xs), max(ys)))

    def expand(self, margin: int) -> "PixelRect":
        """向外扩张若干像素。擦除时用它留出回填余量。"""
        return PixelRect(self.x - margin, self.y - margin, self.w + 2 * margin, self.h + 2 * margin)

    def clamp_within(self, bounds: "PixelRect") -> "PixelRect":
        """裁剪到 bounds 之内。多显示器拼接或拖选越界时用。"""
        x0 = max(self.x, bounds.x)
        y0 = max(self.y, bounds.y)
        x1 = min(self.right, bounds.right)
        y1 = min(self.bottom, bounds.bottom)
        return PixelRect(x0, y0, max(0, x1 - x0), max(0, y1 - y0))

    def is_valid(self, min_size: int = 1) -> bool:
        return self.w >= min_size and self.h >= min_size


@dataclass(frozen=True, slots=True)
class DisplayRect:
    """窗口显示坐标系下的矩形（Qt 逻辑像素）。"""

    x: float
    y: float
    w: float
    h: float

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h

    @classmethod
    def from_corners(cls, p0: Point, p1: Point) -> "DisplayRect":
        x0, x1 = sorted((p0.x, p1.x))
        y0, y1 = sorted((p0.y, p1.y))
        return cls(x0, y0, x1 - x0, y1 - y0)


@dataclass(frozen=True, slots=True)
class ViewTransform:
    """显示与物理像素之间的变换。

    阶段一只抓光标所在的一个显示器，所以只需要一个统一缩放比。
    将来支持多显示器拼接时，把这个类扩展成 per-monitor 的列表，
    调用点不用改——这正是把换算收进本模块的目的。
    """

    scale: float
    """物理像素 / 逻辑像素。Windows 150% 缩放下约为 1.5，HiDPI Linux 下可能为 2.0。"""

    offset_x: int = 0
    """被显示图片左上角在虚拟桌面物理像素坐标中的位置。单显示器时为 0。"""

    offset_y: int = 0

    def __post_init__(self) -> None:
        if self.scale <= 0:
            raise ValueError(f"scale 必须为正: {self.scale!r}")

    def pixel_to_display(self, rect: PixelRect) -> DisplayRect:
        s = self.scale
        return DisplayRect(
            (rect.x - self.offset_x) / s,
            (rect.y - self.offset_y) / s,
            rect.w / s,
            rect.h / s,
        )

    def display_to_pixel(self, rect: DisplayRect) -> PixelRect:
        """把用户拖选的显示矩形换算回物理像素。这是最容易被漏算的一步。"""
        s = self.scale
        x = int(round(rect.x * s)) + self.offset_x
        y = int(round(rect.y * s)) + self.offset_y
        w = int(round(rect.w * s))
        h = int(round(rect.h * s))
        return PixelRect(x, y, w, h)

    def display_point_to_pixel(self, x: float, y: float) -> Point:
        s = self.scale
        return Point(x * s + self.offset_x, y * s + self.offset_y)

    def fit_scale(self, image_px: tuple[int, int], view_px: tuple[int, int]) -> "ViewTransform":
        """计算把整张图等比塞进视图所需的缩放，用于预览窗的「适应窗口」。"""
        iw, ih = image_px
        vw, vh = view_px
        if iw <= 0 or ih <= 0 or vw <= 0 or vh <= 0:
            return self
        return ViewTransform(min(vw / iw, vh / ih), self.offset_x, self.offset_y)
