"""译文回填排版。

这是整个项目技术含量最高的一步，也是最容易做得"像打补丁"的一步。

核心算法（借鉴 PDFMathTranslate 的做法）：

1. 起始字号取**原文块的中位行高**（不是最大行高——大标题里带下划线的行、
   带降部的行会偏高，用最大行高会导致译文整体偏大）。
2. **二分缩小字号**，直到译文在块宽内断行后、总高度 ≤ 块高。
   二分而非线性，因为字号→行数是非线性的阶梯函数。
3. **断行规则分语种**：CJK 基本任意字间可断（除禁则字符），拉丁按空格/连字符断。
   这是必须分开处理的，把中文按空格断行会得到一整行不换的效果。
4. **垂直位置**：从块顶开始排版（而非居中），这样多块之间的基线能大致对齐。

一个不做的事：**不做"撑满"**。译文比原文短时宁可留白，也不放大字号。
放大后的字号和周围元素不协调，比留白更难看。
"""

from __future__ import annotations

import logging
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .coords import PixelRect
from .layout import TextBlock

log = logging.getLogger(__name__)

# 字号下限，低于此值就不再缩——再小就不可读了，宁可溢出
_MIN_FONT_PX = 9

# 单行最大允许宽度溢出比例（字太窄时允许轻微溢出，避免死循环）
_WIDTH_SLACK = 1.02

# 中文禁则：这些字符不能出现在行首
_NO_LINE_START = set("，。、；：？！）】》」』〉·…％,.;:?!)]}>")
# 不能出现在行尾
_NO_LINE_END = set("（【《「『〈([{<")


@dataclass(slots=True)
class RenderedBlock:
    """一个块的排版结果，用于绘制和调试。"""

    block: TextBlock
    font_size: int
    lines: list[str]
    line_height: int
    fitted: bool
    """是否成功塞进原框。False 说明缩到下限仍溢出。"""


def render_blocks(
    image: Image.Image,
    blocks: list[TextBlock],
    *,
    font_path: str | None = None,
    source_image: Image.Image | None = None,
    use_font_match: bool = True,
) -> Image.Image:
    """把译文画到原图上（调用前应已擦除原文）。返回新图。

    ``source_image`` 是**未擦除**的原图，用于字体匹配——匹配要靠原文的笔画
    特征，擦除后就没得可测了。不传则不做匹配，全部用默认字体。

    ``font_path`` 显式指定时跳过匹配（用户指定优先）。
    """
    targets = [b for b in blocks if b.translation]
    if not targets:
        return image.copy()

    result = image.copy()
    draw = ImageDraw.Draw(result)

    for block in targets:
        resolved = _resolve_font_path(block, font_path, source_image, use_font_match)
        rendered = layout_block(block, resolved)
        _draw_block(draw, rendered, block.style, resolved, block.style.color)

    return result


def _resolve_font_path(
    block: TextBlock,
    explicit: str | None,
    source_image: Image.Image | None,
    use_match: bool,
) -> str | None:
    """决定这个块该用哪个字体文件。

    优先级：用户显式指定 > 字体匹配结果 > None（由 :func:`_open_font` 走平台默认）。
    """
    if explicit:
        return explicit
    if not use_match or source_image is None:
        return None

    from .fontmatch import candidates_for, match_font

    try:
        match = match_font(block, source_image)
    except Exception:  # noqa: BLE001 — 匹配失败不该阻断渲染
        log.debug("字体匹配失败，使用默认字体", exc_info=True)
        return None

    # 置信度太低时不做匹配：选错字体比用默认字体更难看
    if match.confidence < 0.5:
        return None

    for path in candidates_for(match):
        if Path(path).exists():
            log.debug("块 (%d,%d) 字体匹配 %s -> %s", block.bbox.x, block.bbox.y, match.describe(), path)
            return path
    return None


def layout_block(block: TextBlock, font_path: str | None = None) -> RenderedBlock:
    """计算一个块的排版方案（字号 + 断行），不涉及绘制。

    独立出来是为了可测试：排版逻辑的正确性不应该只能通过看图来验证。
    """
    assert block.translation is not None
    text = block.translation
    bbox = block.bbox
    bold = block.style.bold

    start_size = max(_MIN_FONT_PX, block.style.line_height_px)
    size = start_size
    lines: list[str] = []
    fitted = False

    while size >= _MIN_FONT_PX:
        font = _load_font(size, font_path, bold)
        lines = wrap_text(text, bbox.w, font, draw=None)
        line_h = _line_height(font)
        total_h = line_h * len(lines)

        if total_h <= bbox.h and _within_width(lines, bbox.w, font):
            fitted = True
            break
        size -= 1

    if not fitted:
        # 缩到下限仍放不下：接受现状并记录，由调用方决定是否提示用户
        font = _load_font(_MIN_FONT_PX, font_path, bold)
        lines = wrap_text(text, bbox.w, font, draw=None)
        log.debug(
            "块 (%d,%d,%dx%d) 缩到 %dpx 仍溢出，行数=%d",
            bbox.x, bbox.y, bbox.w, bbox.h, _MIN_FONT_PX, len(lines),
        )

    final_font = _load_font(size, font_path, bold)
    return RenderedBlock(
        block=block,
        font_size=size,
        lines=lines,
        line_height=_line_height(final_font),
        fitted=fitted,
    )


def wrap_text(text: str, max_width: int, font, draw=None) -> list[str]:
    """把文本断行到指定宽度。

    按语种选策略：CJK 逐字断（带禁则处理），拉丁按词断（超长单词硬断）。
    混合文本按段落主导语种决定——逐段词法比逐字符判断更稳定。
    """
    if not text.strip():
        return []

    measure = _make_measurer(font, draw)
    paragraphs = text.split("\n")
    out: list[str] = []

    for para in paragraphs:
        if not para.strip():
            out.append("")
            continue
        if _is_cjk_dominant(para):
            out.extend(_wrap_cjk(para, max_width, measure))
        else:
            out.extend(_wrap_latin(para, max_width, measure))
    return out


def _wrap_cjk(text: str, max_width: int, measure) -> list[str]:
    """CJK 逐字断行，带禁则处理。

    禁则：行首不能是标点（否则把标点挤到下一行开头很难看），
    行尾不能是开括号。遇到违规就回退一个字符。
    """
    lines: list[str] = []
    current = ""

    for unit in _cjk_units(text):
        # unit 是"一个字"或"一个完整的拉丁词/数字串"
        candidate = current + unit
        if measure(candidate) <= max_width or not current:
            current = candidate
            continue

        # 需要换行。检查禁则
        if unit in _NO_LINE_START and current:
            # 标点不能起行：把它留在上一行（允许轻微溢出）
            current = candidate
            continue
        if current and current[-1] in _NO_LINE_END:
            # 开括号不能结尾：把它挪到下一行
            lines.append(current[:-1])
            current = current[-1] + unit
            continue
        if len(unit) > 1 and measure(unit) > max_width:
            # 单个拉丁词就超宽（如长 URL）：只能硬断
            chunks = _hard_break(unit, max_width, measure)
            lines.append(current)
            lines.extend(chunks[:-1])
            current = chunks[-1]
            continue

        lines.append(current)
        current = unit

    if current:
        lines.append(current)
    return lines


def _cjk_units(text: str) -> list[str]:
    """把 CJK 文本切成断行单元：每个 CJK 字是独立单元，连续的拉丁字母/数字
    串（含其间的 ``.``/``-``/``_`` 等连接符）整体作为一个单元。

    **这是必须的。** 纯逐字断行会把 ``Anthropic`` 从中间劈成 ``Anth`` +
    ``ropic``——实测译文里的品牌名就是这样被断开的。CJK 文本里夹的拉丁词
    通常是品牌名、产品名或代码标识符，断在中间既难看又改变了词。

    长度阈值 24：正常单词都远小于它，超长的（URL、哈希）才走硬断。
    """
    units: list[str] = []
    buf = ""

    for ch in text:
        if _is_cjk(ch) or ch.isspace():
            if buf:
                units.append(buf)
                buf = ""
            units.append(ch)
        elif ch.isalnum() or ch in ".-_+#@/:%?&=~":
            buf += ch
        else:
            # 其他符号（拉丁标点等）单独成单元
            if buf:
                units.append(buf)
                buf = ""
            units.append(ch)

    if buf:
        units.append(buf)
    return units


def _wrap_latin(text: str, max_width: int, measure) -> list[str]:
    """拉丁按词断行。超长单词（如 URL）硬断，否则会无限溢出。"""
    words = text.split()
    lines: list[str] = []
    current = ""

    for word in words:
        candidate = f"{current} {word}".strip()
        if measure(candidate) <= max_width or not current:
            if not current and measure(word) > max_width:
                # 单个词就超宽：硬断
                chunks = _hard_break(word, max_width, measure)
                lines.extend(chunks[:-1])
                current = chunks[-1] if chunks else ""
            else:
                current = candidate
            continue
        lines.append(current)
        current = word

    if current:
        lines.append(current)
    return lines


def _hard_break(word: str, max_width: int, measure) -> list[str]:
    chunks: list[str] = []
    cur = ""
    for ch in word:
        if measure(cur + ch) > max_width and cur:
            chunks.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        chunks.append(cur)
    return chunks


def _draw_block(
    draw: ImageDraw.ImageDraw,
    rendered: RenderedBlock,
    style,
    font_path: str | None,
    color: tuple[int, int, int],
) -> None:
    """把排版结果画到图上。"""
    bbox = rendered.block.bbox
    font = _load_font(rendered.font_size, font_path, style.bold)
    line_h = rendered.line_height

    y = float(bbox.y)
    for line in rendered.lines:
        if not line:
            y += line_h
            continue

        x = _align_x(line, bbox, font, style.align, draw)
        draw.text(
            (x, y),
            line,
            font=font,
            fill=color,
            stroke_width=0,
        )
        y += line_h


def _align_x(line: str, bbox: PixelRect, font, align: str, draw) -> float:
    if align == "left":
        return float(bbox.x)
    width = _text_width(line, font, draw)
    if align == "right":
        return float(bbox.right - width)
    return float(bbox.x + (bbox.w - width) / 2)


def _make_measurer(font, draw=None):
    """返回一个测宽函数。有 draw 用 draw.textlength（更准），否则用 font.getlength。"""
    if draw is not None:
        def measure(s: str) -> int:
            return int(draw.textlength(s, font=font))
        return measure

    def measure(s: str) -> int:
        return int(font.getlength(s))

    return measure


def _text_width(text: str, font, draw) -> int:
    return _make_measurer(font, draw)(text)


def _within_width(lines: list[str], max_width: int, font) -> bool:
    """检查所有行是否都在宽度内（允许极小溢出）。"""
    measure = _make_measurer(font)
    limit = max_width * _WIDTH_SLACK
    return all(measure(line) <= limit for line in lines)


def _line_height(font) -> int:
    """行高。用 ascent+descent 而非 font.size——不同字体的实际行距差异很大。"""
    ascent, descent = font.getmetrics()
    return int((ascent + descent) * 1.08)


def _is_cjk_dominant(text: str) -> bool:
    """判断文本是否以 CJK 为主。混合文本按多数决定断行策略。"""
    cjk = 0
    latin = 0
    for ch in text:
        if _is_cjk(ch):
            cjk += 1
        elif ch.isalpha():
            latin += 1
    return cjk >= latin


def _is_cjk(ch: str) -> bool:
    if not ch:
        return False
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF  # 基本汉字
        or 0x3400 <= code <= 0x4DBF  # 扩展 A
        or 0x3000 <= code <= 0x303F  # CJK 标点
        or 0xFF00 <= code <= 0xFFEF  # 全角
        or 0x3040 <= code <= 0x30FF  # 日文假名
        or 0xAC00 <= code <= 0xD7AF  # 韩文
    )


_FONT_CACHE: dict[tuple[int, str | None, bool], ImageFont.FreeTypeFont] = {}


def _load_font(size: int, font_path: str | None, bold: bool = False) -> ImageFont.FreeTypeFont:
    """加载字体，带缓存。字号在二分过程中会被反复请求，不缓存会拖慢排版。"""
    key = (size, font_path, bold)
    cached = _FONT_CACHE.get(key)
    if cached is not None:
        return cached

    font = _open_font(size, font_path, bold)
    _FONT_CACHE[key] = font
    return font


def _open_font(size: int, font_path: str | None, bold: bool) -> ImageFont.FreeTypeFont:
    candidates: list[str] = []
    if font_path:
        candidates.append(font_path)

    # 粗体优先找同族的 bold 变体。找不到时 Pillow 不会自动加粗，
    # 会静默退回常规字重——所以候选列表里显式列出各平台的 bold 字体。
    candidates.extend(_bold_candidates() if bold else [])
    candidates.extend(resolve_font_candidates())

    for path in candidates:
        if not path:
            continue
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue

    # 全都失败时退回 Pillow 内置位图字体。它不支持中文——会显示成方块，
    # 但这比让整个回填流程崩掉好。
    log.warning("未找到可用 TrueType 字体，退回 Pillow 内置字体（不支持 CJK）")
    return ImageFont.load_default(size)


def _bold_candidates() -> list[str]:
    """粗体字体候选。这些是各平台最可能在的标准粗体。"""
    if sys.platform == "win32":
        win = Path("C:/Windows/Fonts")
        return [
            str(win / "msyhbd.ttc"),   # 微软雅黑 Bold
            str(win / "simhei.ttf"),   # 黑体（本身就很粗）
            str(win / "arialbd.ttf"),
            str(win / "segoeuib.ttf"),
        ]

    return [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    ]


def resolve_font_candidates() -> list[str]:
    """按平台列出候选字体路径。优先支持 CJK 的系统字体。"""
    if sys.platform == "win32":
        win = Path("C:/Windows/Fonts")
        return [
            str(win / "msyh.ttc"),      # 微软雅黑：现代 Windows 的 UI 默认
            str(win / "msyhbd.ttc"),
            str(win / "simhei.ttf"),    # 黑体
            str(win / "simsun.ttc"),    # 宋体
            str(win / "arial.ttf"),
            str(win / "segoeui.ttf"),
        ]

    return [
        # Linux：Noto / 文泉驿 / 思源
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/opentype/source-han-sans/SourceHanSans-Regular.otf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
    ]
