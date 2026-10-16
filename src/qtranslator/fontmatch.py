"""字体启发式匹配。

目标：让译文看起来"像原来的字体"，而不是一律用同一个系统字体。

**不引入模型**（用户明确选择）。用像素统计做三个判断，然后从系统字体里挑：

1. **衬线 / 无衬线** —— 靠在笔画末端找"衬线突起"。做法：对每行文字取上下
   各一条水平扫描带，比较笔画在带内的宽度与中部宽度的差异。有衬线的字体
   在字形的上下边缘会有明显更宽的横向延伸。
2. **字重档位** —— 复用 :func:`~qtranslator.layout.estimate_bold` 的笔画宽度
   方法，但分成多档（细/常规/中粗/粗）。
3. **等宽 / 比例** —— 靠字符宽度的方差。等宽字体每个字符宽度一致，
   方差接近 0。

为什么不靠谱也要做：这三个特征能覆盖绝大多数 UI 字体的差异。真正难的是
"区分两种不同的无衬线"（如 Styrene vs Helvetica），那需要模型。但把
serif/sans 和字重对上，已经能消除最刺眼的违和感。
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from statistics import median

import numpy as np
from PIL import Image

from .coords import PixelRect
from .layout import TextBlock
from .ocr import TextLine

log = logging.getLogger(__name__)


SANS = "sans"
SERIF = "serif"
MONO = "mono"


@dataclass(slots=True)
class FontMatch:
    """匹配结果。"""

    family: str = SANS
    weight: str = "regular"
    """thin / regular / medium / bold / black"""

    mono: bool = False
    confidence: float = 0.0
    """0-1。低置信度时调用方可以退回默认字体。"""

    def describe(self) -> str:
        return f"{self.family}/{self.weight}{'/mono' if self.mono else ''} ({self.confidence:.2f})"


def match_font(block: TextBlock, image: Image.Image) -> FontMatch:
    """估计一个块的字体特征。

    **刻意不检测等宽字体。** 最初的实现靠"各行宽度 / 字符数"的变异系数判断
    等宽，结果在换行排版的段落上必然误判——折行后的文本每行字符数与宽度
    成正比，变异系数天然很小。而误判的后果很严重：等宽字体（Consolas 等）
    没有 CJK 字形，中文会渲染成方块。收益（识别代码字体）远小于风险。
    """
    lines = block.lines
    if not lines:
        return FontMatch()

    arr = np.asarray(image.convert("RGB"))
    bg_lum = _luminance(block.style.bg)

    serif_score = _serif_score(arr, lines, bg_lum)
    weight = _weight_of(arr, lines, bg_lum)

    family = SERIF if serif_score > 0.6 else SANS

    # 置信度：serif 判定噪声最大，取它离阈值 0.5 的距离作为主要依据。
    # 偏离阈值越远越可信。
    confidence = min(1.0, 0.5 + abs(serif_score - 0.5))

    return FontMatch(family=family, weight=weight, mono=False, confidence=confidence)


def _serif_score(arr: np.ndarray, lines: list[TextLine], bg_lum: float) -> float:
    """估计"有衬线"的程度。> 0.6 判为衬线体。

    做法：取每行文字的**顶部扫描带**和**底部扫描带**（各占行高 15%），
    统计其中的前景像素占比，与行中部（40%-60%）的前景占比比较。

    有衬线的字体在字形的上下边缘会有横向的衬线突起（比如 "I" 的上下横杠、
    "l" 的底脚），所以边缘带的前景占比会相对中部**更高**。无衬线字体的
    边缘一般是笔画的收窄（"n" 的肩部、圆弧的顶端），占比更低。

    这个判据在字号大时更可靠，小字（< 20px）抗锯齿噪声大，所以按行高加权。
    """
    ratios: list[float] = []
    weights: list[float] = []

    for line in lines:
        region = _crop(arr, line)
        if region.size == 0:
            continue
        ink = _ink_mask(region, bg_lum)
        h = ink.shape[0]
        if h < 8:
            continue

        band = max(1, int(h * 0.15))
        mid0, mid1 = int(h * 0.4), int(h * 0.6)

        edge = np.concatenate([ink[:band].ravel(), ink[-band:].ravel()])
        middle = ink[mid0:mid1].ravel()
        if middle.size == 0 or edge.mean() < 0.01:
            continue

        # 边缘/中部 的占比比。> 1 倾向衬线，< 1 倾向无衬线
        ratio = edge.mean() / max(1e-6, middle.mean())
        ratios.append(ratio)
        # 行高越大越可信
        weights.append(min(1.0, h / 40.0))

    if not ratios:
        return 0.5

    weighted = np.average(ratios, weights=weights)
    # 把 ratio 映射到 0-1。经验上无衬线约 0.6-0.9，衬线约 1.1-1.6
    return float(np.clip((weighted - 0.85) / 0.5 + 0.5, 0.0, 1.0))


def _weight_of(arr: np.ndarray, lines: list[TextLine], bg_lum: float) -> str:
    """按笔画宽度 / 行高把字重分档。

    阈值来自实测：常规字重约 0.08-0.11，中粗 0.12-0.14，粗 0.15-0.19，
    黑体 > 0.20。

    **只在多行块上区分 medium/bold/black。** 单行块（如导航项）的笔画宽度
    估计噪声很大——实测同为常规字重的相邻导航项，有的判 regular 有的判
    medium，纯属像素抖动。对单行块只区分"常规 / 粗"，把中间档位合并掉，
    避免给同一行带内的同款文字选出不同字体。
    """
    runs: list[int] = []
    for line in lines:
        region = _crop(arr, line)
        if region.size == 0:
            continue
        ink = _ink_mask(region, bg_lum)
        for row in ink:
            run = 0
            for val in row:
                if val:
                    run += 1
                elif run:
                    runs.append(run)
                    run = 0
            if run:
                runs.append(run)

    if not runs:
        return "regular"

    stroke = median(runs)
    line_h = median(ln.bbox.h for ln in lines)
    if line_h <= 0:
        return "regular"
    ratio = stroke / line_h

    if len(lines) == 1:
        # 单行块：粗/不粗两档
        return "bold" if ratio >= 0.14 else "regular"

    if ratio < 0.06:
        return "thin"
    if ratio < 0.12:
        return "regular"
    if ratio < 0.15:
        return "medium"
    if ratio < 0.20:
        return "bold"
    return "black"


def _mono_score(lines: list[TextLine]) -> tuple[bool, float]:
    """**已弃用，保留仅供调试。** 见 :func:`match_font` 的说明。

    这个判据在换行排版的文本上必然误判为等宽（折行后 w/len 天然稳定），
    而误判会选中无 CJK 字形的等宽字体，后果比不匹配更糟。
    """
    ratios = [ln.bbox.w / max(1, len(ln.text.strip())) for ln in lines if ln.text.strip()]
    if len(ratios) < 3:
        return False, 0.0

    mean = float(np.mean(ratios))
    if mean <= 0:
        return False, 0.0
    cv = float(np.std(ratios) / mean)
    return cv < 0.08, cv


def _crop(arr: np.ndarray, line: TextLine) -> np.ndarray:
    h, w = arr.shape[:2]
    b = line.bbox.clamp_within(PixelRect(0, 0, w, h))
    if b.w <= 0 or b.h <= 0:
        return np.empty((0, 0, 3), dtype=arr.dtype)
    return arr[b.y : b.bottom, b.x : b.right]


def _ink_mask(region: np.ndarray, bg_lum: float) -> np.ndarray:
    lum = region.astype(np.float32) @ np.array([0.299, 0.587, 0.114])
    if bg_lum >= 128:
        return lum < bg_lum - 40
    return lum > bg_lum + 40


def _luminance(rgb: tuple[int, int, int]) -> float:
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]


# ---------- 系统字体候选 ----------

# 每个 (family, weight) 组合的候选字体路径，按平台。
# 找不到精确匹配时按 weight 向相邻档位退化，再退到 family 的默认。
_WINDOWS_FONTS = Path("C:/Windows/Fonts")
_LINUX_FONTS = Path("/usr/share/fonts")


def _font_table() -> dict[tuple[str, str], list[str]]:
    """构造 (family, weight) → 候选路径 的映射。"""
    if sys.platform == "win32":
        w = _WINDOWS_FONTS
        return {
            (SANS, "thin"): [str(w / "msyhl.ttc"), str(w / "segoeuil.ttf")],
            (SANS, "regular"): [str(w / "msyh.ttc"), str(w / "segoeui.ttf")],
            (SANS, "medium"): [str(w / "msyh.ttc"), str(w / "seguisb.ttf")],
            (SANS, "bold"): [str(w / "msyhbd.ttc"), str(w / "segoeuib.ttf")],
            (SANS, "black"): [str(w / "simhei.ttf"), str(w / "msyhbd.ttc")],
            (SERIF, "regular"): [str(w / "simsun.ttc"), str(w / "times.ttf")],
            (SERIF, "bold"): [str(w / "simsun.ttc"), str(w / "timesbd.ttf")],
            (MONO, "regular"): [str(w / "consola.ttf"), str(w / "cour.ttf")],
            (MONO, "bold"): [str(w / "consolab.ttf"), str(w / "courbd.ttf")],
        }

    # Linux：Noto / 文泉驿 / DejaVu
    n = _LINUX_FONTS
    return {
        (SANS, "thin"): ["/usr/share/fonts/truetype/noto/NotoSansCJK-Thin.ttc"],
        (SANS, "regular"): [
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        ],
        (SANS, "medium"): [
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        ],
        (SANS, "bold"): [
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        ],
        (SANS, "black"): [
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        ],
        (SERIF, "regular"): [
            "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
        ],
        (SERIF, "bold"): [
            "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
        ],
        (MONO, "regular"): ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"],
        (MONO, "bold"): ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"],
    }


_WEIGHT_ORDER = ["thin", "regular", "medium", "bold", "black"]


def candidates_for(match: FontMatch) -> list[str]:
    """按匹配结果给出候选字体路径，可用的排前面。

    weight 先精确匹配，再向相邻档位退化（就近优先），最后退到 family 的
    regular。这样即使某档位字体缺失也能拿到一个合理的近似。
    """
    table = _font_table()
    family = match.family

    ordered_keys: list[tuple[str, str]] = []
    if match.weight in _WEIGHT_ORDER:
        idx = _WEIGHT_ORDER.index(match.weight)
        # 就近退化：先看自己，再看相邻
        for offset in _neighbor_offsets(idx, len(_WEIGHT_ORDER)):
            ordered_keys.append((family, _WEIGHT_ORDER[idx + offset]))
    ordered_keys.append((family, "regular"))

    # family 退化：serif → sans
    if family != SANS:
        ordered_keys.append((SANS, match.weight))
        ordered_keys.append((SANS, "regular"))

    out: list[str] = []
    seen: set[str] = set()
    for key in ordered_keys:
        for path in table.get(key, []):
            if path not in seen:
                seen.add(path)
                out.append(path)
    return out


def _neighbor_offsets(idx: int, total: int) -> list[int]:
    """从当前位置向外扩散的偏移序列：0, -1, +1, -2, +2 …"""
    offsets = [0]
    for d in range(1, total):
        if idx - d >= 0:
            offsets.append(-d)
        if idx + d < total:
            offsets.append(d)
    return offsets
