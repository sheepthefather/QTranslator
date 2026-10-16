"""屏幕捕获。

阶段一只抓**光标所在的那一个显示器**。跨屏拼接（虚拟桌面）留到后面——它需要
per-monitor 的变换列表，会把 ``ViewTransform`` 复杂化，而现在没有真实需求驱动。

用 ``mss``：纯 ctypes、无重依赖、Windows 与 X11 都成熟。
**注意 mss 在 Wayland 下完全不可用**，届时需要换成 ``xcap`` 走 xdg-desktop-portal。
这不是随手能改的，见 README 的「待接入的选择点」。
"""

from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass

import mss
import numpy as np
from PIL import Image

from .coords import PixelRect


@dataclass(frozen=True, slots=True)
class Capture:
    """一次全屏截图的结果。"""

    image: Image.Image
    """光标所在显示器的全屏图，RGB，尺寸为物理像素。"""

    origin: PixelRect
    """该显示器在整个虚拟桌面物理像素坐标系中的位置。

    单显示器时通常是 ``PixelRect(0, 0, w, h)``；多显示器时可能带非零偏移。
    OCR 结果用它反推回虚拟桌面坐标——阶段一用不到，但类型上先留好。
    """

    @property
    def scale_hint(self) -> float:
        """物理像素与逻辑像素的比例，供 ViewTransform 使用。

        从 Qt 的 ``QScreen.devicePixelRatio()`` 取，**不是**用物理宽度除以逻辑宽度：
        后者只在单显示器且 Qt 未做 DPI 虚拟化时才成立，多显示器混合缩放时会得到
        无意义的数（实测在次级屏上会算错，导致每次裁剪都偏移）。
        Qt 的 devicePixelRatio 是权威值；拿不到时退回 1.0。
        """
        return _device_pixel_ratio(self.origin)


def _device_pixel_ratio(monitor: PixelRect) -> float:
    """找到覆盖该显示器中心点的 QScreen，返回它的 devicePixelRatio。"""
    try:
        from PySide6.QtGui import QGuiApplication

        app = QGuiApplication.instance()
        if app is None:
            return 1.0

        # 用显示器物理中心反查对应的 QScreen：物理中心 / dpr 才是它的逻辑中心
        cx_px = monitor.x + monitor.w / 2
        cy_px = monitor.y + monitor.h / 2
        for screen in app.screens():
            ratio = screen.devicePixelRatio()
            if ratio <= 0:
                continue
            geo = screen.geometry()
            cx_log = geo.x() + geo.width() / 2
            cy_log = geo.y() + geo.height() / 2
            if abs(cx_log * ratio - cx_px) < monitor.w and abs(cy_log * ratio - cy_px) < monitor.h:
                return float(ratio)
    except Exception:  # noqa: BLE001 — 取不到就退回 1.0，不阻塞截图主流程
        pass
    return 1.0


def _cursor_position() -> tuple[int, int] | None:
    """光标在虚拟桌面物理像素坐标中的位置。"""
    if sys.platform == "win32":
        pt = ctypes.wintypes.POINT()  # type: ignore[attr-defined]
        if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):  # type: ignore[attr-defined]
            return int(pt.x), int(pt.y)
        return None
    # X11 下由 mss 无法直接取，回落到主显示器；后续可用 python-xlib 补
    return None


class ScreenCapturer:
    """按需抓取显示器。持有 mss 实例——它内部维护平台句柄，复用比重建快。"""

    def __init__(self) -> None:
        self._sct = mss.mss()

    def close(self) -> None:
        self._sct.close()

    def monitors(self) -> list[PixelRect]:
        """返回所有显示器的物理像素矩形（索引 0 是虚拟桌面全集，故跳过）。"""
        return [
            PixelRect(m["left"], m["top"], m["width"], m["height"])
            for m in self._sct.monitors[1:]
        ]

    def monitor_at_cursor(self) -> PixelRect:
        """光标所在显示器的物理像素矩形。光标取不到时退回主显示器。"""
        monitors = self.monitors()
        if not monitors:
            raise RuntimeError("未检测到任何显示器")
        pos = _cursor_position()
        if pos is not None:
            cx, cy = pos
            for mon in monitors:
                if mon.x <= cx < mon.right and mon.y <= cy < mon.bottom:
                    return mon
        return monitors[0]

    def grab(self, monitor: PixelRect | None = None) -> Capture:
        """抓取指定显示器（默认光标所在的那个）的整屏。"""
        target = monitor or self.monitor_at_cursor()
        raw = self._sct.grab(
            {"left": target.x, "top": target.y, "width": target.w, "height": target.h}
        )
        # mss 返回 BGRA，转成 PIL 的 RGB
        arr = np.frombuffer(raw.rgb, dtype=np.uint8).reshape(raw.height, raw.width, 3)
        return Capture(image=Image.fromarray(arr, "RGB"), origin=target)

    def grab_rect(self, rect: PixelRect) -> Image.Image:
        """抓虚拟桌面上的任意矩形（跨显示器时需要，阶段一仅用于兜底）。"""
        raw = self._sct.grab(
            {"left": rect.x, "top": rect.y, "width": rect.w, "height": rect.h}
        )
        arr = np.frombuffer(raw.rgb, dtype=np.uint8).reshape(raw.height, raw.width, 3)
        return Image.fromarray(arr, "RGB")
