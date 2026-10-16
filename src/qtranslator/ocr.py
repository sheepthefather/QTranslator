"""OCR 封装。

用 RapidOCR（PP-OCRv5 的 ONNX 移植）。选它而不是 PaddleOCR 本体：纯 onnxruntime
依赖、跨平台二进制干净、模型随包分发，而且它已经实现了 DB 检测的完整后处理
（阈值化 → 轮廓 → unclip 膨胀 → 框排序），自己用 ort 重写这些是纯浪费。

输出是四点多边形 + 文字 + 置信度，正是后续排版回填需要的。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from PIL import Image

from .coords import PixelRect

log = logging.getLogger(__name__)


@dataclass(slots=True)
class TextLine:
    """一行识别结果。阶段一一行即一块；阶段三会用几何规则把行聚成段落块。"""

    quad: list[tuple[float, float]]
    """四点多边形，物理像素坐标，顺序为左上→右上→右下→左下。"""

    text: str
    score: float

    @property
    def bbox(self) -> PixelRect:
        return PixelRect.from_quad(self.quad)

    @property
    def height(self) -> int:
        """外接矩形高度，回填时用来估算字号。"""
        return self.bbox.h

    @property
    def center_y(self) -> float:
        return (min(p[1] for p in self.quad) + max(p[1] for p in self.quad)) / 2


@dataclass(slots=True)
class OcrResult:
    lines: list[TextLine] = field(default_factory=list)
    elapsed_ms: float = 0.0

    @property
    def full_text(self) -> str:
        """按阅读顺序拼接的纯文本，给阶段二的翻译用。"""
        return "\n".join(line.text for line in self.lines)

    @property
    def mean_score(self) -> float:
        if not self.lines:
            return 0.0
        return sum(line.score for line in self.lines) / len(self.lines)


class OcrEngine:
    """懒加载的 OCR 引擎。模型首次调用时才载入——启动时载会拖慢托盘图标出现。"""

    def __init__(self, *, min_score: float = 0.5) -> None:
        self._engine: Any | None = None
        self.min_score = min_score

    def _ensure(self) -> Any:
        if self._engine is None:
            from rapidocr_onnxruntime import RapidOCR

            log.info("正在加载 RapidOCR 模型…")
            self._engine = RapidOCR()
            log.info("RapidOCR 就绪")
        return self._engine

    def preload(self) -> None:
        """提前载入模型，避免用户第一次框选时卡顿。可在后台线程调用。"""
        self._ensure()

    def recognize(self, image: Image.Image) -> OcrResult:
        """识别一张 PIL 图。坐标以图片自身左上角为原点（物理像素）。"""
        import time

        engine = self._ensure()
        arr = np.asarray(image.convert("RGB"))

        t0 = time.perf_counter()
        raw, _ = engine(arr)
        elapsed = (time.perf_counter() - t0) * 1000

        return OcrResult(lines=_parse(raw, self.min_score), elapsed_ms=elapsed)


def _parse(raw: Any, min_score: float) -> list[TextLine]:
    """把 RapidOCR 的原始输出转成 TextLine 列表。

    RapidOCR 的返回是 ``[[quad, text, score], ...]``，无结果时返回 ``None``。
    不同版本对 score 的类型不一致（float 或 str），这里统一转 float。
    """
    if not raw:
        return []

    lines: list[TextLine] = []
    for item in raw:
        try:
            quad_raw, text, score_raw = item[0], item[1], item[2]
            score = float(score_raw)
        except (IndexError, TypeError, ValueError):
            log.debug("跳过无法解析的 OCR 条目: %r", item)
            continue

        if score < min_score or not text.strip():
            continue

        quad = [(float(p[0]), float(p[1])) for p in quad_raw]
        if len(quad) != 4:
            continue
        lines.append(TextLine(quad=quad, text=text, score=score))

    return sort_reading_order(lines)


def sort_reading_order(lines: list[TextLine]) -> list[TextLine]:
    """按阅读顺序排序：先按 y，同一视觉行再按 x。

    阶段一用最简单的 y-then-x 排序，**刻意不做行带聚类**。原因是实测发现：
    示例图里左侧大标题（行高 50-60px，占 y 185-366）与右侧段落（行高 28px，
    占 y 228-358）在垂直方向**互相重叠**，任何基于 y 重叠的行带算法都会把这两列
    揉进同一批带里，再用 x 排序反而把右侧段落的行序打乱（实测确实如此）。

    正确的做法是**先做列分割，再在列内按 y 排**——那是阶段三行聚类的工作，
    现在硬塞进来只会得到一个既复杂又不对的折中。y-then-x 在以下情形成立：
    单列文本流（本文档的常态）。它唯一会错的是"同一视觉行上有多个框"
    （如左右两栏的表头），那属于阶段三的列分割要解决的问题。

    顺序很重要：阶段二的整屏批翻会把所有行按此顺序拼成上下文送进 LLM，
    顺序错了上下文就错了。
    """
    return sorted(lines, key=lambda ln: (round(ln.center_y / 2.0), ln.bbox.x))
