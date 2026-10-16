"""文字擦除。

分两档，**按背景复杂度自动选**，而不是一律上模型：

- **fill** —— 用采样的背景色直接填。纯色/均匀渐变背景（UI 截图的主流）下
  这是最优解：微秒级、零伪影，而且比任何 inpainting 都干净，因为背景本来就
  是纯色，插值只会引入无意义的纹理。
- **telea** —— OpenCV 的 TELEA 修复。背景有轻微纹理或渐变时用。
- **migan** —— MI-GAN ONNX（MIT）。照片、复杂纹理背景。CPU 上几十毫秒。

选档逻辑见 :func:`choose_mode`：统计 mask 周边像素的方差，方差低就说明背景平坦。

一个关键细节：**mask 要外扩**。按文字检测框严格擦会在笔画边缘留一圈鬼影
（抗锯齿像素没被覆盖）。外扩几个像素，同时 mask 要贴合文字笔画而非整个矩形，
否则大标题的行间空白区域也会被抹掉，重建时反而留下明显色块。
"""

from __future__ import annotations

import logging
from enum import Enum
from pathlib import Path

import numpy as np
from PIL import Image

from .coords import PixelRect
from .layout import TextBlock
from .ocr import TextLine

log = logging.getLogger(__name__)

# 擦除时 mask 相对文字框的外扩像素
_MASK_PAD = 3

# mask 的形态学膨胀迭代次数。1 次 = 外扩 1px，用来吃掉抗锯齿最外圈残留。
_MASK_DILATE = 1

# 背景方差低于此值判定为"平坦"，直接用填色
_FLAT_VARIANCE = 90.0

# 文字二值化的亮度差阈值。
# 必须足够小才能包住抗锯齿边缘——见 _ink_mask 的说明。
_INK_DELTA = 18


class EraseMode(str, Enum):
    FILL = "fill"
    TELEA = "telea"
    MIGAN = "migan"
    AUTO = "auto"


def erase_text(
    image: Image.Image,
    blocks: list[TextBlock],
    mode: EraseMode = EraseMode.AUTO,
    *,
    migan_model: Path | None = None,
) -> Image.Image:
    """擦掉所有块的原文，返回新图（不修改入参）。

    AUTO 会对每个块单独选档（见 :func:`_resolve_modes`），然后按档分组处理，
    这样同一个 MI-GAN 会话只初始化一次。
    """
    result = image.copy()
    targets = [b for b in blocks if b.translation]
    if not targets:
        return result

    modes = _resolve_modes(result, targets, mode, migan_model)
    grouped: dict[EraseMode, list[TextBlock]] = {}
    for block, one_mode in zip(targets, modes, strict=False):
        grouped.setdefault(one_mode, []).append(block)

    for one_mode, group in grouped.items():
        if one_mode is EraseMode.FILL:
            result = _erase_fill(result, group)
        elif one_mode is EraseMode.TELEA:
            result = _erase_telea(result, group)
        elif one_mode is EraseMode.MIGAN:
            result = _erase_migan(result, group, migan_model)

    return result


def _resolve_modes(
    image: Image.Image,
    blocks: list[TextBlock],
    mode: EraseMode,
    migan_model: Path | None,
) -> list[EraseMode]:
    """把 AUTO 解析成具体模式。返回与 ``blocks`` 等长的模式列表。"""
    arr = np.asarray(image.convert("RGB"))
    out: list[EraseMode] = []

    has_migan = migan_model is not None and migan_model.exists()

    for block in blocks:
        if mode is not EraseMode.AUTO:
            out.append(mode)
            continue

        variance = _background_variance(arr, block)
        if variance < _FLAT_VARIANCE:
            out.append(EraseMode.FILL)
        elif has_migan:
            out.append(EraseMode.MIGAN)
        else:
            # 没有 MI-GAN 模型时 TELEA 是合理退路：纹理背景上不如 MI-GAN，
            # 但比填色好，且零额外依赖
            out.append(EraseMode.TELEA)

    if out:
        counts: dict[str, int] = {}
        for m in out:
            counts[m.value] = counts.get(m.value, 0) + 1
        log.debug("擦除模式分配: %s", counts)
    return out


def _background_variance(arr: np.ndarray, block: TextBlock) -> float:
    """块周边背景的像素方差。用来判断背景是否平坦。"""
    bbox = block.bbox
    pad = max(4, bbox.h // 4)
    h, w = arr.shape[:2]

    # 取块的上下两条带，避开块内文字
    regions = []
    top = PixelRect(bbox.x, bbox.y - pad, bbox.w, pad).clamp_within(PixelRect(0, 0, w, h))
    bottom = PixelRect(bbox.x, bbox.bottom, bbox.w, pad).clamp_within(PixelRect(0, 0, w, h))
    for r in (top, bottom):
        if r.w > 0 and r.h > 0:
            regions.append(arr[r.y : r.bottom, r.x : r.right].reshape(-1, 3))

    if not regions:
        return 0.0
    pixels = np.concatenate(regions, axis=0).astype(np.float32)
    return float(pixels.var(axis=0).mean())


def _build_mask(image: Image.Image, blocks: list[TextBlock]) -> np.ndarray:
    """构造 8 位 mask（255=要擦除），供 TELEA / MI-GAN 使用。

    mask 由**文字笔画**而非整块矩形构成：在块内做亮度二值化，把"与背景同色"的
    像素排除掉。这样大标题的行间空白不会被误擦——那些区域若被抹掉再重建，
    会留下明显色块，比不擦更难看。

    阈值与膨胀处理统一走 :func:`_ink_mask`，避免两处实现漂移。
    """
    w, h = image.size
    mask = np.zeros((h, w), dtype=np.uint8)
    arr = np.asarray(image.convert("RGB"))

    for block in blocks:
        if not block.translation:
            continue
        bbox = block.bbox.expand(_MASK_PAD).clamp_within(PixelRect(0, 0, w, h))
        if bbox.w <= 0 or bbox.h <= 0:
            continue

        region = arr[bbox.y : bbox.bottom, bbox.x : bbox.right]
        mask[bbox.y : bbox.bottom, bbox.x : bbox.right] = np.where(
            _ink_mask(region, block.style.bg), 255, 0
        ).astype(np.uint8)

    return mask


def _erase_fill(image: Image.Image, blocks: list[TextBlock]) -> Image.Image:
    """用采样的背景色直接填掉文字像素。

    平坦背景下这是最干净的做法：填色而非插值，因为纯色背景的"正确"结果就是
    那个颜色，插值反而会引入涂抹感。只填 mask 内的文字笔画像素，行间空白不动。
    """
    arr = np.asarray(image.convert("RGB")).copy()
    h, w = arr.shape[:2]

    for block in blocks:
        if not block.translation:
            continue
        bbox = block.bbox.expand(_MASK_PAD).clamp_within(PixelRect(0, 0, w, h))
        if bbox.w <= 0 or bbox.h <= 0:
            continue

        region = arr[bbox.y : bbox.bottom, bbox.x : bbox.right]
        mask = _ink_mask(region, block.style.bg)
        # 用该块自己的背景色填充，而不是全局背景色——同一屏里可能有
        # 深色按钮和浅色页面两种背景
        region[mask] = block.style.bg

    return Image.fromarray(arr, "RGB")


def _erase_telea(image: Image.Image, blocks: list[TextBlock]) -> Image.Image:
    """OpenCV TELEA 修复。背景有轻微渐变或纹理时使用。"""
    import cv2

    arr = np.asarray(image.convert("RGB")).copy()
    h, w = arr.shape[:2]
    mask = _build_mask(image, blocks)
    if not mask.any():
        return image

    # inpaint 要求 mask 是单通道 8 位，且 CV_8UC1/3 的输入类型要匹配
    bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    radius = max(3, int(0.08 * max(b.bbox.h for b in blocks if b.translation)))
    repaired = cv2.inpaint(bgr, mask, radius, cv2.INPAINT_TELEA)
    return Image.fromarray(cv2.cvtColor(repaired, cv2.COLOR_BGR2RGB), "RGB")


def _erase_migan(image: Image.Image, blocks: list[TextBlock], model: Path | None) -> Image.Image:
    """MI-GAN ONNX 修复。

    MI-GAN 的原生输入是 512×512 固定尺寸，所以要把块裁出来、缩放到 512、
    推理、再缩放回去贴回原位。这对每个块都要跑一次前向。

    模型文件不存在时退回 TELEA 并记日志——静默产出坏图比降级更糟。
    """
    if model is None or not model.exists():
        log.warning("MI-GAN 模型不存在（%s），退回 TELEA", model)
        return _erase_telea(image, blocks)

    import cv2
    import onnxruntime as ort

    arr = np.asarray(image.convert("RGB")).copy()
    session = _migan_session(model)
    h, w = arr.shape[:2]

    for block in blocks:
        if not block.translation:
            continue
        bbox = block.bbox.expand(_MASK_PAD + 4).clamp_within(PixelRect(0, 0, w, h))
        if bbox.w < 8 or bbox.h < 8:
            continue

        region = arr[bbox.y : bbox.bottom, bbox.x : bbox.right]
        mask = _ink_mask(region, block.style.bg)

        # 放大到 MI-GAN 的 512 输入；mask 同步缩放
        size = 512
        region_r = cv2.resize(region, (size, size), interpolation=cv2.INTER_LINEAR)
        mask_r = cv2.resize(
            mask.astype(np.uint8) * 255, (size, size), interpolation=cv2.INTER_NEAREST
        )

        inp = _migan_input(region_r, mask_r)
        out = session.run(None, {session.get_inputs()[0].name: inp})[0]

        repaired = _migan_output(out, region_r.shape[:2])
        repaired = cv2.resize(repaired, (bbox.w, bbox.h), interpolation=cv2.INTER_LINEAR)

        # 只贴回 mask 覆盖的区域，其余保留原像素，避免整块被重绘
        hole = cv2.resize(mask_r, (bbox.w, bbox.h), interpolation=cv2.INTER_NEAREST) > 127
        region[hole] = repaired[hole]

    return Image.fromarray(arr, "RGB")


_MIGAN_SESSION = None


def _migan_session(model: Path):
    """缓存 ONNX 会话。重复创建会话会重复分配内存和初始化线程池。"""
    global _MIGAN_SESSION
    if _MIGAN_SESSION is None:
        import onnxruntime as ort

        log.info("加载 MI-GAN 模型: %s", model)
        _MIGAN_SESSION = ort.InferenceSession(
            str(model), providers=["CPUExecutionProvider"]
        )
    return _MIGAN_SESSION


def _migan_input(region_rgb: np.ndarray, mask_r: np.ndarray) -> np.ndarray:
    """构造 MI-GAN 输入张量。

    MI-GAN 的约定：图像归一化到 [-1, 1]，mask 为 {0,1}，通道顺序 RGB，
    布局 NCHW，图像与 mask 在通道维拼接（4 通道）。
    """
    img = region_rgb.astype(np.float32) / 127.5 - 1.0
    m = (mask_r > 127).astype(np.float32)
    chw = np.concatenate(
        [np.transpose(img, (2, 0, 1)), m[None, :, :]], axis=0
    )
    return chw[None, ...].astype(np.float32)


def _migan_output(out: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    """把 MI-GAN 输出张量还原成 HWC uint8 图像。"""
    arr = np.squeeze(out, axis=0)  # (3, H, W)
    if arr.shape[0] == 3:
        arr = np.transpose(arr, (1, 2, 0))
    arr = (arr + 1.0) * 127.5
    return np.clip(arr, 0, 255).astype(np.uint8)


def _ink_mask(region: np.ndarray, bg: tuple[int, int, int]) -> np.ndarray:
    """区域内"文字笔画（含抗锯齿边缘）"的布尔 mask。

    两个关键点，都来自实测踩坑：

    1. **阈值要松。** 最初用"与背景亮度差 > 45"，结果只擦掉笔画核心，
       留下整圈抗锯齿的中间调像素，肉眼看到明显的文字残影（ghosting）。
       文字渲染的抗锯齿会让边缘像素从背景色平滑过渡到字色，中间那些像素
       与背景的差可能只有 10-30。阈值降到 18 才能把整圈都包住。

    2. **要做形态学膨胀。** 即便阈值放松，最外圈仍有亚像素级的过渡残留。
       膨胀 1-2 px 把它们一并纳入擦除范围。膨胀半径不能大——过大会吃掉
       邻近的装饰元素（如图标、边框）。

    只在**块的外扩矩形内**做，所以膨胀不会溢出去影响其他区域。
    """
    import cv2

    bg_lum = _luminance(bg)
    lum = region.astype(np.float32) @ np.array([0.299, 0.587, 0.114])

    if bg_lum >= 128:
        ink = lum < bg_lum - _INK_DELTA
    else:
        ink = lum > bg_lum + _INK_DELTA

    mask = ink.astype(np.uint8)
    # 膨胀：把抗锯齿外圈一并纳入。3x3 核迭代 1 次 = 外扩 1px，对文字足够，
    # 又不会明显侵蚀周边
    kernel = np.ones((3, 3), np.uint8)
    mask = cv2.dilate(mask, kernel, iterations=_MASK_DILATE)
    return mask.astype(bool)


def _luminance(rgb: tuple[int, int, int]) -> float:
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
