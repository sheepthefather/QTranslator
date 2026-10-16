"""排版逻辑回归测试。

这些是**无声失败**的检查：布局错了不会抛异常，只会产出一张看起来不对劲的图。
而这正是本项目最容易出错的地方（阶段二/三各踩过一次）。所以用合成的行框直接
断言切分结果，不依赖 OCR 也不依赖样本图。

用法::

    python tools/test_layout.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qtranslator.layout import split_bands, split_columns  # noqa: E402
from qtranslator.ocr import TextLine  # noqa: E402

_FAILED: list[str] = []


def line(x: int, y: int, w: int, h: int, text: str = "x") -> TextLine:
    """构造一个水平矩形行框（quad 顺序：左上、右上、右下、左下）。"""
    return TextLine(
        quad=[(x, y), (x + w, y), (x + w, y + h), (x, y + h)],
        text=text,
        score=0.99,
    )


def check(name: str, actual, expected) -> None:
    ok = actual == expected
    if not ok:
        _FAILED.append(name)
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        expected: {expected}")
        print(f"        actual  : {actual}")


def test_bands_split_header_from_body() -> None:
    """页头与正文之间的大间隙应切成两个带。"""
    lines = [
        line(25, 12, 144, 21, "LOGO"),
        line(446, 12, 115, 21, "Research"),
        # 约 150px 的大间隙 —— 版块分隔
        line(26, 185, 429, 50, "title line 1"),
        line(25, 247, 492, 56, "title line 2"),
    ]
    bands = split_bands(lines)
    check("bands: 页头/正文切成 2 带", len(bands), 2)
    check("bands: 页头含 2 行", len(bands[0]), 2)
    check("bands: 正文含 2 行", len(bands[1]), 2)


def test_bands_keep_paragraph_together() -> None:
    """段落内的正常行距不应被误切成带。"""
    lines = [line(30, 200 + i * 35, 400, 28) for i in range(4)]
    check("bands: 段内 4 行保持同带", len(split_bands(lines)), 1)


def test_columns_split_two_column_body() -> None:
    """两栏正文之间的大间隙应切成两列。"""
    lines = [
        line(24, 185, 580, 50, "left 1"),
        line(650, 228, 410, 28, "right 1"),
        line(25, 247, 492, 56, "left 2"),
        line(651, 263, 392, 28, "right 2"),
    ]
    cols = split_columns(lines, (1104, 606))
    check("columns: 切成 2 列", len(cols), 2)
    # 左列应全是 left，右列全是 right
    check("columns: 左列 2 行", len(cols[0]), 2)
    check("columns: 右列 2 行", len(cols[1]), 2)
    check(
        "columns: 左列内容正确",
        all("left" in ln.text for ln in cols[0]),
        True,
    )
    check(
        "columns: 右列内容正确",
        all("right" in ln.text for ln in cols[1]),
        True,
    )


def test_columns_do_not_split_on_word_spacing() -> None:
    """同一段落的词间距不应被误判成列分隔（英文最容易踩）。"""
    # 同一行带内多个词，间隙远小于行高
    lines = [
        line(100, 100, 80, 28, "word1"),
        line(190, 100, 80, 28, "word2"),
        line(280, 100, 80, 28, "word3"),
    ]
    cols = split_columns(lines, (800, 400))
    check("columns: 词间距不切列", len(cols), 1)


def test_columns_require_gap_through_all_lines() -> None:
    """只在部分行出现的右侧留白不应切列。"""
    lines = [
        line(100, 100, 300, 28, "long line here"),
        line(100, 140, 120, 28, "short"),  # 右侧大片空白
        line(100, 180, 300, 28, "another long"),
    ]
    cols = split_columns(lines, (600, 300))
    check("columns: 单行留白不切列", len(cols), 1)


def test_columns_ignore_outer_margins() -> None:
    """版面左右边缘的留白不应产生切分点。"""
    lines = [line(200, 100, 200, 28), line(200, 140, 200, 28)]
    cols = split_columns(lines, (1000, 300))
    check("columns: 左右外边距不切列", len(cols), 1)


def test_wrap_keeps_latin_words_intact() -> None:
    """CJK 文本里夹的拉丁词（品牌名）不能被从中间断开。

    实测真实译文 "AI 将对世界产生巨大影响。Anthropic 是一家公益公司…"
    在逐字断行下会被切成 "…Anthr" / "opic 是…"，把品牌名劈成两半。
    这是 CJK 段落里夹拉丁词的通用问题，不只是这一个例子。
    """
    from qtranslator.reflow import _load_font, _make_measurer, wrap_text

    font = _load_font(27, None, False)
    measure = _make_measurer(font)

    text = "AI 将对世界产生巨大影响。Anthropic 是一家公益公司，致力于确保其益处并降低其风险。"
    lines = wrap_text(text, 410, font)

    broken = [
        ln for ln in lines
        if "Anth" in ln and "Anthropic" not in ln
    ] + [
        ln for ln in lines
        if "opic" in ln and "Anthropic" not in ln
    ]
    check("wrap: 品牌名不被劈开", broken, [])

    # 拉丁词必须完整出现在某一行里
    check("wrap: Anthropic 完整存在", any("Anthropic" in ln for ln in lines), True)

    # 断行后每行都不应超宽（允许极小溢出）
    over = [ln for ln in lines if measure(ln) > 410 * 1.02]
    check("wrap: 每行不超宽", over, [])


def test_wrap_cjk_still_char_breaks() -> None:
    """纯 CJK 仍应逐字断行——别为了保词而退化成不换行。"""
    from qtranslator.reflow import _load_font, _make_measurer, wrap_text

    font = _load_font(27, None, False)
    measure = _make_measurer(font)
    lines = wrap_text("将安全置于前沿的人工智能研究与产品", 200, font)

    check("wrap: 纯 CJK 断成多行", len(lines) > 1, True)
    over = [ln for ln in lines if measure(ln) > 200 * 1.02]
    check("wrap: 纯 CJK 不超宽", over, [])


def main() -> int:
    test_bands_split_header_from_body()
    test_bands_keep_paragraph_together()
    test_columns_split_two_column_body()
    test_columns_do_not_split_on_word_spacing()
    test_columns_require_gap_through_all_lines()
    test_columns_ignore_outer_margins()
    test_wrap_keeps_latin_words_intact()
    test_wrap_cjk_still_char_breaks()

    print()
    if _FAILED:
        print(f"{len(_FAILED)} 项失败: {', '.join(_FAILED)}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
