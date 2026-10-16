"""阶段二冒烟测试：对样本图跑完整管线，输出对比图供肉眼验收。

用法::

    python tools/smoke_stage2.py <输入图> [--out 输出dir] [--lang zh]

产出 ``<name>.stage2.png``：译文覆盖原图的结果。
用 MockTranslator，不需要 API key——目的是验证**擦除 + 排版**这条管线，
译文本身是占位文本（长度和字宽刻意做得接近真实译文，让排版压力接近真实）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image  # noqa: E402

from qtranslator.config import load as load_config  # noqa: E402
from qtranslator.erase import EraseMode  # noqa: E402
from qtranslator.ocr import OcrEngine  # noqa: E402
from qtranslator.pipeline import process_from_ocr  # noqa: E402
from qtranslator.translate import build_translator  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("images", nargs="+")
    ap.add_argument("--out", default="out", help="输出目录")
    ap.add_argument("--lang", default="zh", help="目标语言")
    ap.add_argument("--erase", default="auto", choices=["auto", "fill", "telea", "migan"])
    ap.add_argument("--no-font-match", action="store_true", help="关闭字体匹配，全部用默认字体")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    engine = OcrEngine(min_score=0.3)
    print("加载 OCR 模型…")
    engine.preload()

    # 用真实配置：有 key 就走真实翻译，没有则退回 Mock。
    # （之前这里硬编码 MockTranslator，导致配了 key 也测不出真实翻译。）
    cfg = load_config()
    translator = build_translator(
        cfg.api_key or None,
        base_url=cfg.base_url,
        model=cfg.model,
        disable_reasoning=cfg.disable_reasoning,
    )
    print(f"翻译后端: {type(translator).__name__}"
          + (f" ({cfg.model})" if cfg.has_api_key else "（未配置 key，用占位文本）"))

    for path in args.images:
        p = Path(path)
        if not p.exists():
            print(f"跳过（不存在）: {p}")
            continue

        image = Image.open(p).convert("RGB")
        engine.recognize(image)  # 预热
        ocr = engine.recognize(image)

        result = process_from_ocr(
            image,
            ocr,
            translator,
            target_lang=args.lang,
            erase_mode=EraseMode(args.erase),
            context="A website screenshot.",
            use_font_match=not args.no_font_match,
        )

        out_path = out_dir / f"{p.stem}.stage2.png"
        result.translated.save(out_path)

        print("=" * 74)
        print(f"{p.name} -> {out_path}")
        print(
            f"  {len(result.blocks)} 块 · 翻译 {result.translate_seconds:.2f}s · "
            f"擦除 {result.erase_seconds:.2f}s · 回填 {result.reflow_seconds:.2f}s"
        )
        for i, blk in enumerate(result.blocks):
            r = next((x for x in result.rendered if x.block is blk), None)
            tag = "" if (r is None or r.fitted) else "  << 溢出"
            size = r.font_size if r else "-"
            nlines = len(r.lines) if r else "-"
            print(
                f"  #{i} ({blk.bbox.w}x{blk.bbox.h}) 字号={size} 行数={nlines}{tag}"
            )
            if not args.no_font_match:
                print(f"      字体: {_font_of(blk, image)}")
            print(f"      原文: {blk.source_text[:60]}")
            print(f"      译文: {(blk.translation or '')[:60]}")

    return 0


def _font_of(block, image) -> str:
    """报告该块匹配到的字体，失败时说明原因。"""
    try:
        from qtranslator.fontmatch import candidates_for, match_font

        match = match_font(block, image)
        if match.confidence < 0.5:
            return f"默认（匹配置信度过低 {match.confidence:.2f}）"
        for path in candidates_for(match):
            if Path(path).exists():
                return f"{match.describe()} -> {Path(path).name}"
        return f"{match.describe()} -> 无可用文件，用默认"
    except Exception as exc:  # noqa: BLE001
        return f"匹配出错: {exc}"


if __name__ == "__main__":
    raise SystemExit(main())
