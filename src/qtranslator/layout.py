"""排版数据模型与块聚类。

阶段一一行就是一个独立单元。阶段二必须先把行聚成**段落块**——因为回填时
译文要按"块"重新断行排版，而不是逐行替换（逐行替换必然错位：英文一行翻成中文
通常更短，中文一行翻成英文通常更长，行数和行长都不可能对上）。

块聚类的判据（几何规则，不依赖模型）：
- 行高相近（同一块的文字字号一致）
- 行间距接近（相邻行的垂直间隙稳定）
- 左边缘对齐，或水平投影显著重叠
- 垂直间隙过大则断块（段间距 > 段内行距）

样式提取同样是几何+像素统计：
- 文字颜色：对框内**暗于背景**的像素取中位数（而非直接取最暗，避免被抗锯齿边缘带偏）
- 背景色：取框外一圈像素的中位数（框内被文字占据，取框外更准）
- 粗体：用笔画宽度估计（见 :func:`estimate_bold`）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median

import numpy as np
from PIL import Image

from .coords import PixelRect
from .ocr import TextLine

# 判定"两行属于同一块"的阈值
_LINE_GAP_RATIO = 0.6
"""行间隙 / 行高 超过此值就认为断块（段间距）。"""

_HEIGHT_TOLERANCE = 0.35
"""两行高度差异超过较大者的这个比例，就认为字号不同，不属于同一块。"""

_ALIGN_TOLERANCE = 0.02
"""左边缘对齐容差，相对块宽度。"""


@dataclass(slots=True)
class BlockStyle:
    """一个块的排版样式。回填时尽量复刻它，让译文看起来像原文。"""

    color: tuple[int, int, int] = (0, 0, 0)
    """文字颜色 RGB。"""

    bg: tuple[int, int, int] = (255, 255, 255)
    """背景色 RGB。擦除和回填都可能用到。"""

    line_height_px: int = 20
    """原文的行高（中位数）。回填时作为起始字号基准。"""

    bold: bool = False

    align: str = "left"
    """``left`` / ``center`` / ``right``。"""

    @property
    def is_dark_text_on_light(self) -> bool:
        return _luminance(self.color) < _luminance(self.bg)


@dataclass(slots=True)
class TextBlock:
    """一个段落块。翻译和回填的最小单位。"""

    lines: list[TextLine]
    bbox: PixelRect
    style: BlockStyle
    translation: str | None = None

    @property
    def source_text(self) -> str:
        """原文，行间以空格连接。

        **不要**用换行连接：原文的换行是排版折行的结果，不是语义换行。
        保留它会误导 LLM 以为那里是句子边界。空格连接更接近其真实语义，
        译文再按块宽度重新断行。
        """
        return " ".join(line.text.strip() for line in self.lines if line.text.strip())

    @property
    def line_count(self) -> int:
        return len(self.lines)


def build_blocks(lines: list[TextLine], image: Image.Image) -> list[TextBlock]:
    """把 OCR 行聚成段落块并提取样式。

    分两步切分：**先横切成带，再在带内竖切成列，最后聚类**。

    为什么不能只做一次全局竖切：一个页面常常同时有「通栏的页头」和
    「两栏的正文」。全局垂直投影下，页头横跨整个宽度，于是找不到贯穿所有行的
    空白竖带 —— 正文两栏就切不开（实测英文样本正是如此）。反过来若放宽度量，
    页面被从中间切开，页头被拦腰截断（实测中文样本正是如此）。

    先横切再竖切才对：横切把页头和正文分开，各自内部的列结构才能被正确识别。
    """
    if not lines:
        return []

    # 用 (带序, 列序, 块序) 显式记录阅读顺序，而不是事后按坐标重排。
    # 按 y 重排会被 1-2px 的坐标噪声干扰：导航栏各项目 y 相差 1-3px，
    # 重排后 x=931 的项会跑到 x=25 的前面（实测如此）。
    # 切分阶段已经知道正确的顺序，直接沿用即可。
    ordered_groups: list[list[TextLine]] = []
    for band in split_bands(lines):
        for column in split_columns(band, image.size):
            for group in _group_lines(column):
                ordered_groups.append(group)

    return [
        TextBlock(
            lines=group,
            bbox=_union_bbox(group),
            style=_extract_style(group, image),
        )
        for group in ordered_groups
    ]


def split_bands(lines: list[TextLine]) -> list[list[TextLine]]:
    """按水平投影把行切成若干"带"（页头 / 正文 / 页脚）。

    判据是**行间距**：带内的行间距稳定且较小，带与带之间会有一个明显更大的
    间隙。阈值取行高的 1.6 倍——实测页头与正文之间的间隙（约 150px）远大于
    段内行距（3-12px），这个判据非常稳。

    注意这与 :func:`_group_lines` 里的断块判据（0.6 倍行高）不同，且必须更大：
    断块是"段落之间"，断带是"版块之间"。段落间距通常只有行高的零点几倍，
    而版块间距是数倍。
    """
    if not lines:
        return []

    ordered = sorted(lines, key=lambda ln: ln.bbox.y)
    median_h = median(ln.bbox.h for ln in ordered)
    max_gap = max(8, int(1.6 * median_h))

    bands: list[list[TextLine]] = []
    current: list[TextLine] = [ordered[0]]

    for line in ordered[1:]:
        prev_bottom = max(ln.bbox.bottom for ln in current)
        if line.bbox.y - prev_bottom > max_gap:
            bands.append(current)
            current = [line]
        else:
            current.append(line)

    bands.append(current)
    return bands


def split_columns(lines: list[TextLine], image_size: tuple[int, int]) -> list[list[TextLine]]:
    """在给定的行集合内，按垂直投影切列。

    做法：把所有行的水平跨度投到 x 轴上，找**足够宽的空白竖带**作为列分隔。

    "足够宽"取两个判据中较小的那个：

    - **行高的 1.2 倍** —— 版面分栏间隙通常与行高同量级。
    - **文字总宽度的 4%** —— 宽版面上分栏间隙的绝对值会随版面放大，
      固定按行高算会太严。

    取较小值是为了在两种版面上都能切：单纯用行高倍数时，实测样本图的
    44px 间隙与阈值 45px 只差 1px，任何 OCR 抖动都会导致切分失败（这是
    一个真实的隐患，回归测试正是用另一种行高分布把它暴露了出来）。

    同时用文字总宽度百分比做下限，是因为词间距（约 1% 宽度）和分栏间隙
    （通常 > 4% 宽度）之间有明确的数量级差，这个判据不依赖行高。

    只在一两行上出现的空白（比如短行的右侧留白）不算分隔——否则每行都会
    因为自身没占满宽度而把版面切碎，这一点由"贯穿所有行"的投影统计保证。
    """
    if not lines:
        return []

    median_h = median(ln.bbox.h for ln in lines)
    span = max(ln.bbox.right for ln in lines) - min(ln.bbox.x for ln in lines)
    min_gap = max(12, int(min(1.2 * median_h, 0.04 * span)))

    occupied = np.zeros(image_size[0], dtype=np.int32)
    for line in lines:
        x0 = max(0, line.bbox.x)
        x1 = min(image_size[0], line.bbox.right)
        if x1 > x0:
            occupied[x0:x1] += 1

    gaps = _find_gaps(occupied, min_gap)
    if not gaps:
        return [lines]

    cuts = [(g0 + g1) // 2 for g0, g1 in gaps]
    return _partition_by_cuts(lines, cuts)


def _find_gaps(occupied: np.ndarray, min_gap: int) -> list[tuple[int, int]]:
    """找出所有宽度 >= min_gap 的零覆盖区间（左右边缘的空白不算）。"""
    gaps: list[tuple[int, int]] = []
    start = -1

    for x, count in enumerate(occupied):
        if count == 0:
            if start < 0:
                start = x
        else:
            if start >= 0:
                if x - start >= min_gap:
                    gaps.append((start, x))
                start = -1

    # 末尾的空白带不切分（版面右侧留白），所以不处理 start>=0 的收尾情况
    return gaps


def _partition_by_cuts(lines: list[TextLine], cuts: list[int]) -> list[list[TextLine]]:
    """按切分点把行分到各列。行归属于它**中心点**所在的那一列。

    用中心点而非左边缘：左边缘在居中对齐的文本上会漂移。
    """
    if not cuts:
        return [lines]

    columns: list[list[TextLine]] = [[] for _ in range(len(cuts) + 1)]
    for line in lines:
        center = line.bbox.x + line.bbox.w / 2
        idx = 0
        for i, cut in enumerate(cuts):
            if center > cut:
                idx = i + 1
        columns[idx].append(line)

    # 丢弃空列，保持列顺序
    return [c for c in columns if c]


def _group_lines(lines: list[TextLine]) -> list[list[TextLine]]:
    """按几何规则把行切成段落块，并按阅读顺序返回。

    **必须跟所有未闭合的块比较，不能只跟上一行比。** 因为阅读顺序是全局的：
    示例图里左标题（3 行）和右段落（4 行）在垂直方向交错，阅读顺序会呈现
    「标题1、段落1、标题2、段落2…」的交替。若只看上一行，标题在第 2 行就被
    段落打断，之后每个标题行都会各自成块——实测正是如此。

    正确做法：对每个新行，在所有「可续接的块」里找它该并入的那个。
    """
    groups: list[list[TextLine]] = []

    for line in lines:
        target = None
        best_gap = None
        for group in groups:
            if _same_block(group[-1], line, group):
                # 有多个候选时取垂直间隙最小的——它才是真正紧邻的那一块
                gap = line.bbox.y - group[-1].bbox.bottom
                if best_gap is None or gap < best_gap:
                    target = group
                    best_gap = gap

        if target is None:
            groups.append([line])
        else:
            target.append(line)

    # 按 (量化后的块顶 y, 块左 x) 排序输出。
    #
    # y 必须**量化**而不能直接比：同一行带内并排的多个单行块（如导航栏），
    # 其 y 相差仅 1-3px（实测 12/12/13/14/15），直接比 y 会让这些项按 OCR
    # 的抖动顺序排列，而不是左到右。量化步长取块高的一半——同一视觉行内的
    # 抖动（几 px）会被归入同一档，而真正的换行（至少一个行高）仍会分档。
    def sort_key(group: list[TextLine]) -> tuple[int, int]:
        top = min(ln.bbox.y for ln in group)
        left = min(ln.bbox.x for ln in group)
        h = median(ln.bbox.h for ln in group)
        bucket = int(top / max(1, h * 0.5))
        return (bucket, left)

    groups.sort(key=sort_key)
    return groups


def _same_block(prev: TextLine, cur: TextLine, group: list[TextLine]) -> bool:
    """判断 cur 是否应续接到 ``prev``（组内最后一行）所在的块之后。"""
    prev_box = prev.bbox
    cur_box = cur.bbox

    # 1) 字号要一致。用整组的中位行高而非只看 prev：标题行的行高会逐行波动
    #    （50→56→60），跟单行比会在某一行偶然超出容差而错误断块。
    group_h = median(ln.bbox.h for ln in group)
    big = max(group_h, cur_box.h)
    if big > 0 and abs(group_h - cur_box.h) / big > _HEIGHT_TOLERANCE:
        return False

    # 2) 垂直方向必须紧邻。间隙过大说明是不同段落
    gap = cur_box.y - prev_box.bottom
    if gap < 0:
        # 垂直重叠：可能是同一视觉行被切成两行，或列分割出错。不并块。
        return False
    if gap > _LINE_GAP_RATIO * group_h:
        return False

    # 3) 水平位置要能对上：左边缘对齐，或水平投影显著重叠
    group_left = min(b.bbox.x for b in group)
    group_right = max(b.bbox.right for b in group)
    group_w = max(1, group_right - group_left)

    left_delta = abs(cur_box.x - group_left)
    if left_delta <= _ALIGN_TOLERANCE * group_w:
        return True

    overlap = min(cur_box.right, group_right) - max(cur_box.x, group_left)
    return overlap > 0.5 * min(cur_box.w, group_w)


def _union_bbox(lines: list[TextLine]) -> PixelRect:
    x0 = min(ln.bbox.x for ln in lines)
    y0 = min(ln.bbox.y for ln in lines)
    x1 = max(ln.bbox.right for ln in lines)
    y1 = max(ln.bbox.bottom for ln in lines)
    return PixelRect(x0, y0, x1 - x0, y1 - y0)


def _extract_style(lines: list[TextLine], image: Image.Image) -> BlockStyle:
    """从像素统计里提取样式。"""
    bbox = _union_bbox(lines)
    arr = np.asarray(image.convert("RGB"))

    text_color = _sample_text_color(arr, bbox, lines)
    bg_color = _sample_bg_color(arr, bbox)
    line_h = int(median(ln.bbox.h for ln in lines))

    return BlockStyle(
        color=text_color,
        bg=bg_color,
        line_height_px=max(8, line_h),
        bold=estimate_bold(arr, lines, bg_color),
        align=_detect_align(lines, bbox),
    )


def _sample_text_color(
    arr: np.ndarray, bbox: PixelRect, lines: list[TextLine]
) -> tuple[int, int, int]:
    """文字颜色：在行框内取"与背景差异最大"的那一撮像素的中位数。

    直接取最暗像素会被抗锯齿边缘和噪点带偏；取中位数更稳。
    """
    pixels = _collect_pixels(arr, [ln.bbox for ln in lines])
    if pixels.size == 0:
        return (0, 0, 0)

    bg = _sample_bg_color(arr, bbox)
    bg_lum = _luminance(bg)
    lums = pixels @ np.array([0.299, 0.587, 0.114])

    # 背景偏亮就取最暗的 20%，反之取最亮的 20%
    if bg_lum >= 128:
        thresh = np.percentile(lums, 20)
        sel = pixels[lums <= thresh]
    else:
        thresh = np.percentile(lums, 80)
        sel = pixels[lums >= thresh]

    if sel.size == 0:
        sel = pixels
    return tuple(int(v) for v in np.median(sel, axis=0))  # type: ignore[return-value]


def _sample_bg_color(arr: np.ndarray, bbox: PixelRect) -> tuple[int, int, int]:
    """背景色：取框外一圈像素的中位数。

    取框外而不是框内，因为框内被文字占据——尤其是大标题，文字像素可能比背景还多。
    """
    pad = max(2, int(0.15 * bbox.h))
    outer = PixelRect(bbox.x - pad, bbox.y - pad, bbox.w + 2 * pad, bbox.h + 2 * pad)
    outer = outer.clamp_within(PixelRect(0, 0, arr.shape[1], arr.shape[0]))
    if outer.w <= 0 or outer.h <= 0:
        return (255, 255, 255)

    region = arr[outer.y : outer.bottom, outer.x : outer.right].reshape(-1, 3)
    if region.size == 0:
        return (255, 255, 255)

    # 用中位数而不是均值：均值会被框内漏进来的文字像素拉偏
    return tuple(int(v) for v in np.median(region, axis=0))  # type: ignore[return-value]


def _collect_pixels(arr: np.ndarray, rects: list[PixelRect]) -> np.ndarray:
    chunks = []
    h, w = arr.shape[:2]
    for r in rects:
        r = r.clamp_within(PixelRect(0, 0, w, h))
        if r.w <= 0 or r.h <= 0:
            continue
        chunks.append(arr[r.y : r.bottom, r.x : r.right].reshape(-1, 3))
    if not chunks:
        return np.empty((0, 3), dtype=arr.dtype)
    return np.concatenate(chunks, axis=0)


def estimate_bold(arr: np.ndarray, lines: list[TextLine], bg: tuple[int, int, int]) -> bool:
    """用笔画宽度 / 行高估计是否粗体。

    做法：把行框二值化（文字=1），按行统计"连续前景像素游程"的中位数，
    即平均笔画宽度。正常字重下笔画宽度约为行高的 8-12%，粗体约 15%+。
    """
    bg_lum = _luminance(bg)
    runs: list[int] = []
    h, w = arr.shape[:2]

    for line in lines:
        r = line.bbox.clamp_within(PixelRect(0, 0, w, h))
        if r.w <= 0 or r.h <= 0:
            continue
        region = arr[r.y : r.bottom, r.x : r.right]
        lum = region @ np.array([0.299, 0.587, 0.114])
        mask = lum < bg_lum - 40 if bg_lum >= 128 else lum > bg_lum + 40

        for row in mask:
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
        return False

    stroke = median(runs)
    line_h = median(ln.bbox.h for ln in lines)
    if line_h <= 0:
        return False
    return stroke / line_h >= 0.14


def _detect_align(lines: list[TextLine], bbox: PixelRect) -> str:
    """按左右边缘的离散程度判断对齐方式。"""
    if len(lines) < 2 or bbox.w <= 0:
        return "left"

    lefts = [ln.bbox.x for ln in lines]
    rights = [ln.bbox.right for ln in lines]

    left_spread = (max(lefts) - min(lefts)) / bbox.w
    right_spread = (max(rights) - min(rights)) / bbox.w

    if left_spread <= right_spread:
        return "left"
    if right_spread < 0.05 and left_spread > 0.1:
        return "right"
    return "center"


def _luminance(rgb: tuple[int, int, int]) -> float:
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
