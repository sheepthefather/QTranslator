"""翻译后端。

阶段二走云端。但管线验证（擦除 + 回填排版）不该被 API key 阻塞，
所以 :class:`MockTranslator` 是默认后端——它产出结构正确、长度合理的
假译文，足以把整条排版管线跑通并暴露问题。

设计要点：**整屏批翻**，把所有块作为一个 JSON 数组一次送出，要求返回
同 id 的译文数组。逐块翻会让同一屏里的术语不一致（"Policy" 一会儿"政策"
一会儿"策略"），而且丢失上下文。

切换到真实 API 只需换 :class:`OpenAICompatTranslator` 的 base_url 和 key。
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .layout import TextBlock

log = logging.getLogger(__name__)


@dataclass(slots=True)
class TranslateRequest:
    blocks: list[TextBlock]
    target_lang: str = "zh"
    source_lang: str = "auto"
    context: str = ""


class Translator(ABC):
    """翻译后端接口。"""

    @abstractmethod
    def translate(self, request: TranslateRequest) -> list[str]:
        """返回与 ``request.blocks`` 等长、按序对应的译文列表。"""


class MockTranslator(Translator):
    """假翻译器：用于在拿到 API key 之前验证擦除与回填排版。

    它不真翻译，而是产出一个**长度和字宽都合理**的占位文本——这样排版引擎
    面临的收缩/断行压力和真实译文接近，能真实暴露排版问题。

    对 CJK 目标语言产出中文占位（字号大、字宽方），对拉丁目标语言产出
    英文占位（字宽窄、词间可断）。这两者的排版行为差异很大，必须都覆盖。
    """

    # 常见词的中文映射，让占位译文看起来不至于完全是乱码。
    # 仅用于让开发者肉眼判断排版，不追求翻译质量。
    _GLOSS = {
        "ai": "人工智能",
        "research": "研究",
        "policy": "政策",
        "news": "新闻",
        "learn": "学习",
        "commitments": "承诺",
        "products": "产品",
        "safety": "安全",
        "world": "世界",
        "impact": "影响",
        "corporation": "公司",
        "benefits": "益处",
        "risks": "风险",
        "frontier": "前沿",
        "security": "安全",
    }

    def translate(self, request: TranslateRequest) -> list[str]:
        out: list[str] = []
        for block in request.blocks:
            out.append(
                _mock_zh(block.source_text)
                if request.target_lang.startswith("zh")
                else _mock_latin(block.source_text)
            )
        return out


def _mock_zh(text: str) -> str:
    """产出中文占位。用词表替换 + 未命中的词保留，使长度接近真实译文。"""
    words = re.findall(r"[A-Za-z']+|[^\sA-Za-z]", text)
    parts: list[str] = []
    for w in words:
        mapped = MockTranslator._GLOSS.get(w.lower())
        if mapped:
            parts.append(mapped)
        elif re.fullmatch(r"[A-Za-z']+", w):
            # 未命中词：用等长中文块代替，保持字数量级接近
            parts.append("词" * max(1, len(w) // 3))
        else:
            parts.append(w)
    joined = "".join(parts)
    # 中文按标点断句更自然
    joined = joined.replace(".", "。").replace(",", "，")
    return joined or text


def _mock_latin(text: str) -> str:
    """产出拉丁占位：词数保持接近，让断行压力接近真实英文译文。"""
    words = text.split()
    return " ".join("word" * 1 if len(w) < 4 else f"translation{i}" for i, w in enumerate(words))


class OpenAICompatTranslator(Translator):
    """OpenAI 兼容的翻译后端（GLM / DeepSeek / 硅基流动 / 通义 / OpenAI 均可）。

    只依赖 HTTP，不引入厂商 SDK——这些厂商的 SDK 迭代频繁，而 chat/completions
    这个接口形态足够稳定。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com/v1",
        model: str = "deepseek-chat",
        timeout: float = 60.0,
        max_retries: int = 3,
        disable_reasoning: bool = True,
    ) -> None:
        if not api_key:
            raise ValueError("api_key 为空")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.disable_reasoning = disable_reasoning

    def translate(self, request: TranslateRequest) -> list[str]:
        last_error: Exception | None = None

        for attempt in range(self.max_retries):
            try:
                content = self._call(request)
            except _RetryableError as exc:
                last_error = exc
                # 指数退避：过载通常几秒内缓解。首次等待 1.5s，之后 3s、6s。
                delay = 1.5 * (2**attempt)
                log.warning(
                    "翻译 API 暂时不可用（第 %d/%d 次）：%s，%.1fs 后重试",
                    attempt + 1, self.max_retries, exc, delay,
                )
                if attempt < self.max_retries - 1:
                    time.sleep(delay)
                continue
            return _parse_translations(content, len(request.blocks))

        raise RuntimeError(f"翻译 API 重试 {self.max_retries} 次仍失败：{last_error}")

    def _call(self, request: TranslateRequest) -> str:
        """发一次请求，返回 message.content。"""
        payload = _build_payload(request, self.model, self.disable_reasoning)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            # 429/5xx 是可重试的；4xx（除 429）是请求本身有问题，重试无用
            if exc.code == 429 or exc.code >= 500:
                raise _RetryableError(f"HTTP {exc.code}: {detail}") from exc
            raise RuntimeError(f"翻译 API 返回 {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            # 网络问题可重试——用户的网络可能只是抖了一下
            raise _RetryableError(f"无法连接翻译 API: {exc.reason}") from exc

        return _extract_content(data)


class _RetryableError(Exception):
    """可以重试的错误（过载 / 限流 / 网络抖动）。"""


def _extract_content(data: dict) -> str:
    """从响应里取出模型的最终回答。

    两个必须处理的现实情况：

    1. **厂商在 HTTP 200 里返回业务错误。** 智谱在模型过载时返回
       ``{"error": {"code": "1305", "message": "该模型当前访问量过大"}}``
       而 HTTP 状态是 200。必须检查 error 字段，否则会把错误当成功解析，
       然后在 JSON 解析处报一个莫名其妙的错。
    2. **推理模型把思考过程放在 ``reasoning_content``。** glm-4.7-flash 这类
       模型的 ``content`` 是最终答案、``reasoning_content`` 是思考链，两者分开。
       正常情况只读 content；但若 content 为空而 reasoning 里有 JSON（模型被
       token 上限截断时会这样），退回读 reasoning，比直接失败好。
    """
    if isinstance(data.get("error"), dict):
        err = data["error"]
        code = err.get("code", "?")
        message = err.get("message", "")
        # 过载类错误码可重试
        if str(code) in {"1305", "1302", "1303", "1304"}:
            raise _RetryableError(f"[{code}] {message}")
        raise RuntimeError(f"翻译 API 业务错误 [{code}]: {message}")

    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"翻译 API 返回无 choices: {json.dumps(data, ensure_ascii=False)[:300]}")

    message = choices[0].get("message") or {}
    content = (message.get("content") or "").strip()
    if content:
        return content

    # content 为空：可能是输出全被思考占满。退回读 reasoning_content。
    reasoning = (message.get("reasoning_content") or "").strip()
    if reasoning:
        finish = choices[0].get("finish_reason")
        log.warning(
            "翻译响应 content 为空（finish_reason=%s），退回解析 reasoning_content。"
            "这通常意味着 max_tokens 太小、输出被思考链占满。",
            finish,
        )
        return reasoning
    raise RuntimeError("翻译 API 返回内容为空")


def _build_payload(request: TranslateRequest, model: str, disable_reasoning: bool = True) -> dict:
    """构造请求。整屏批翻 + 上下文 + 术语一致性约束。"""
    items = [
        {"id": i, "text": block.source_text}
        for i, block in enumerate(request.blocks)
        if block.source_text.strip()
    ]

    system = (
        "You are a professional UI translator. Translate each text segment from "
        f"{request.source_lang} to {request.target_lang}.\n"
        "Rules:\n"
        "1. Return ONLY a JSON object: {\"translations\": [{\"id\": N, \"text\": \"...\"}]}\n"
        "2. Keep the same number of items and the same ids.\n"
        "3. Translate the whole screen as one coherent page: keep terminology "
        "consistent across segments (e.g. always translate 'Policy' the same way).\n"
        "4. Apply the following to brand and product names — these are the most "
        "common translation errors in UI text:\n"
        "   a. NEVER translate the meaning of a name. Reproduce it as an "
        "untranslated token.\n"
        "   b. Names written in ALL CAPS, in mixed case with unusual "
        "capitalisation, or containing symbols (e.g. ANTHROPIC, ANTHROP\\C, "
        "GitHub, iPhone, Claude) are almost always brands. Keep them verbatim.\n"
        "   c. Do NOT translate a name by its etymology or dictionary meaning. "
        "For example 'Anthropic' is a company name and must stay 'Anthropic' — "
        "it does NOT mean '人类学' / anthropology.\n"
        "   d. Company, product, and model names are names even when they look "
        "like ordinary words.\n"
        "5. Preserve the original punctuation style.\n"
        "6. Do NOT add explanations, notes, or markdown fences.\n"
        "7. Keep translations concise — they must fit back into the original layout."
    )
    user = json.dumps(
        {
            "context": request.context or "A web page screenshot.",
            "segments": items,
        },
        ensure_ascii=False,
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }

    if disable_reasoning:
        # 关掉推理链。翻译短文本不需要思考过程，而推理模型默认会为每屏
        # 生成上千个 reasoning token——实测 3 句短文本烧掉 1214 个。
        #
        # 用 reasoning_effort=none 而不是其他名字相近的参数，因为
        # tools/probe_api.py 实测对比过：
        #   - reasoning_effort=none   -> 0 token,  1.0s（且译文质量更好）
        #   - thinking={type:disabled}-> 0 token,  1.0s
        #   - effort=low              -> 233 token（该字段语义与名字不符）
        #   - reasoning_effort=minimal-> 2180 token, 11.5s（比默认还慢！）
        # 各厂商字段名和语义都不统一，只能实测确定。
        payload["reasoning_effort"] = "none"

    return payload


def _parse_translations(content: str, expected: int) -> list[str]:
    """解析 LLM 返回的 JSON，并做长度与 id 校验。

    LLM 偶尔会加上 markdown 围栏或前后废话，这里做容错。id 缺失的项退回原文，
    而不是整体失败——部分可用好过全部报错。
    """
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"翻译返回的不是合法 JSON: {text[:300]}") from exc

    items = data.get("translations", data if isinstance(data, list) else [])
    if not isinstance(items, list):
        raise RuntimeError(f"翻译返回结构异常: {type(items).__name__}")

    by_id: dict[int, str] = {}
    for it in items:
        if isinstance(it, dict) and "id" in it and "text" in it:
            try:
                by_id[int(it["id"])] = str(it["text"])
            except (TypeError, ValueError):
                continue

    missing = [i for i in range(expected) if i not in by_id]
    if missing:
        log.warning("翻译返回缺少 %d 个块（id=%s），这些块将保留原文", len(missing), missing[:10])

    return [by_id.get(i, "") for i in range(expected)]


def build_translator(
    api_key: str | None,
    *,
    base_url: str,
    model: str,
    disable_reasoning: bool = True,
) -> Translator:
    """按是否有 key 选择后端。无 key 时退回 mock，让管线仍可跑通。"""
    if api_key:
        return OpenAICompatTranslator(
            api_key, base_url=base_url, model=model, disable_reasoning=disable_reasoning
        )
    log.info("未配置 API key，使用 MockTranslator（仅用于验证排版管线）")
    return MockTranslator()


def apply_translations(blocks: list[TextBlock], translations: list[str]) -> None:
    """把译文写回块。空译文保留 None，回填时跳过该块。"""
    for block, text in zip(blocks, translations, strict=False):
        block.translation = text.strip() or None
