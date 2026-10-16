"""阶段一冒烟测试：对着真实样本图跑 OCR，检查准确率与阅读顺序。

用法::

    python tools/smoke_ocr.py <图片路径> [更多图片...]

不依赖 pytest，方便在没装测试框架的机器上直接跑。阅读顺序这一项是**回归测试**：
曾经用 y 重叠做行带聚类，在左右两列垂直重叠的版面上会把右侧段落行序打乱。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image  # noqa: E402

from qtranslator.ocr import OcrEngine  # noqa: E402


def main(paths: list[str]) -> int:
    if not paths:
        print(__doc__)
        return 2

    engine = OcrEngine(min_score=0.3)
    print("加载模型…")
    engine.preload()

    failures = 0
    for path in paths:
        p = Path(path)
        if not p.exists():
            print(f"跳过（不存在）: {p}")
            failures += 1
            continue

        image = Image.open(p).convert("RGB")
        engine.recognize(image)  # 首次推理含图优化开销，不计入耗时
        result = engine.recognize(image)

        print("=" * 72)
        print(f"{p.name}  {image.width}×{image.height}")
        print(
            f"{len(result.lines)} 行 · 均分 {result.mean_score:.3f} · "
            f"{result.elapsed_ms:.0f} ms"
        )
        print("=" * 72)
        for line in result.lines:
            b = line.bbox
            print(f"  y={b.y:4d} x={b.x:4d} {b.w:4d}x{b.h:3d}  {line.score:.2f}  {line.text}")

        if not _check_order(result.lines):
            failures += 1
            print("  !! 阅读顺序异常")

    return 1 if failures else 0


def _check_order(lines) -> bool:
    """同一列内 y 必须单调不降。若出现回跳说明排序逻辑退化了。"""
    if not lines:
        return True
    bad = 0
    prev = lines[0]
    for cur in lines[1:]:
        # 仅当两者 x 接近（同一列）时才要求 y 递增
        same_column = abs(cur.bbox.x - prev.bbox.x) < max(50, 0.5 * prev.bbox.w)
        if same_column and cur.bbox.y < prev.bbox.y - 2:
            bad += 1
        prev = cur
    return bad == 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
