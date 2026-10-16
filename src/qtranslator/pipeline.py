"""阶段二流水线：把 OCR 结果变成"译文覆盖原图"。

把各环节串起来并集中管理"每一步产出什么"的契约，避免调用方自己拼装顺序
（顺序错了不会报错，只会得到一张看起来不对劲的图，很难排查）。

管线顺序固定为：聚类 → 翻译 → 擦除 → 回填。
**擦除必须在回填之前**，且擦除要用块的原始文字位置——回填会改变图，
但块的 bbox 是相对原图记录的，两者不能混用同一份坐标假设。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from PIL import Image

from .erase import EraseMode, erase_text
from .layout import TextBlock, build_blocks
from .ocr import OcrResult, TextLine
from .reflow import RenderedBlock, layout_block, render_blocks
from .translate import TranslateRequest, Translator, apply_translations

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Stage2Result:
    """一次完整处理的结果。"""

    original: Image.Image
    """原始截图（未修改）。"""

    translated: Image.Image
    """擦除原文 + 回填译文后的图。"""

    blocks: list[TextBlock] = field(default_factory=list)
    rendered: list[RenderedBlock] = field(default_factory=list)

    erase_seconds: float = 0.0
    translate_seconds: float = 0.0
    reflow_seconds: float = 0.0

    @property
    def overflow_blocks(self) -> list[RenderedBlock]:
        """缩到下限仍放不下的块。给 UI 提示用。"""
        return [r for r in self.rendered if not r.fitted]


def process(
    image: Image.Image,
    lines: list[TextLine],
    translator: Translator,
    *,
    target_lang: str = "zh",
    source_lang: str = "auto",
    context: str = "",
    erase_mode: EraseMode = EraseMode.AUTO,
    migan_model=None,
    font_path: str | None = None,
    use_font_match: bool = True,
) -> Stage2Result:
    """跑完整条阶段二流水线。"""
    blocks = build_blocks(lines, image)
    if not blocks:
        return Stage2Result(original=image, translated=image.copy())

    t0 = time.perf_counter()
    request = TranslateRequest(
        blocks=blocks, target_lang=target_lang, source_lang=source_lang, context=context
    )
    translations = translator.translate(request)
    apply_translations(blocks, translations)
    t_translate = time.perf_counter() - t0

    t0 = time.perf_counter()
    erased = erase_text(image, blocks, erase_mode, migan_model=migan_model)
    t_erase = time.perf_counter() - t0

    t0 = time.perf_counter()
    translated = render_blocks(
        erased,
        blocks,
        font_path=font_path,
        # 传**未擦除**的原图用于字体匹配：匹配要靠原文的笔画特征，
        # 擦除后就没得可测了
        source_image=image,
        use_font_match=use_font_match,
    )
    t_reflow = time.perf_counter() - t0

    rendered = [layout_block(b, font_path) for b in blocks if b.translation]

    log.info(
        "阶段二完成：%d 块，翻译 %.2fs，擦除 %.2fs，回填 %.2fs",
        len(blocks), t_translate, t_erase, t_reflow,
    )

    return Stage2Result(
        original=image,
        translated=translated,
        blocks=blocks,
        rendered=rendered,
        translate_seconds=t_translate,
        erase_seconds=t_erase,
        reflow_seconds=t_reflow,
    )


def process_from_ocr(
    image: Image.Image,
    ocr_result: OcrResult,
    translator: Translator,
    **kwargs,
) -> Stage2Result:
    """便利入口：直接从 OCR 结果出发。"""
    return process(image, ocr_result.lines, translator, **kwargs)
