"""探测一个 OpenAI 兼容后端支持哪些参数（推理开关 / 模型列表）。

用法::

    python tools/probe_api.py <base_url> <api_key> [model]

做三件事：
1. 列出可用模型（很多厂商支持 GET /models）。
2. 探测能否关闭推理——对推理模型，用 ``reasoning_effort: none`` 能大幅
   降低延迟和 token 消耗。
3. 对每个候选参数组合实测一次真实翻译，报告 reasoning_tokens 与耗时。

存在这个脚本的原因：各厂商关推理的方式不统一（``reasoning_effort`` /
``effort`` / ``thinking`` / ``chat_template_kwargs``），而且名字很像但
语义不同——实测 deepseek-flash 的 ``effort`` 参数取 low 时推理 token
反而更多，而 ``reasoning_effort: none`` 才真正归零。靠猜不行，必须实测。
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

SEGMENTS = [
    (0, "AI research and products that put safety at the frontier"),
    (1, "ANTHROPIC"),
    (2, "AI will have a vast impact on the world. Anthropic is a public benefit corporation."),
    (3, "Try Claude"),
]

SYSTEM = (
    "You are a professional UI translator. Translate to Chinese.\n"
    "Rule: names in ALL CAPS or mixed unusual case (ANTHROPIC, Claude, GitHub) "
    "are brand names — keep verbatim, do NOT translate their meaning.\n"
    'Return ONLY JSON: {"translations":[{"id":N,"text":"..."}]}'
)


def post(url: str, key: str, payload: dict, timeout: float = 120) -> tuple[dict, float]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        return {"error": {"code": exc.code, "message": body}}, time.perf_counter() - t0
    except urllib.error.URLError as exc:
        return {"error": {"code": "NETWORK", "message": str(exc.reason)}}, time.perf_counter() - t0
    return data, time.perf_counter() - t0


def list_models(base: str, key: str) -> None:
    req = urllib.request.Request(
        f"{base}/models", headers={"Authorization": f"Bearer {key}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"  无法列出模型: {exc}")
        return
    for m in data.get("data", []):
        effort = m.get("effort") or {}
        levels = effort.get("supported_levels")
        extra = f"  effort_levels={levels}" if levels else ""
        print(f"  - {m.get('id')}  ({m.get('name', '')}){extra}")


def try_params(base: str, key: str, model: str, label: str, extra: dict) -> None:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {
                "role": "user",
                "content": json.dumps(
                    {"segments": [{"id": i, "text": t} for i, t in SEGMENTS]},
                    ensure_ascii=False,
                ),
            },
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    payload.update(extra)

    data, elapsed = post(f"{base}/chat/completions", key, payload)

    if "error" in data:
        err = data["error"]
        print(f"  {label:34s} ERROR [{err.get('code')}] {str(err.get('message'))[:90]}")
        return

    usage = data.get("usage") or {}
    reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    content = (msg.get("content") or "").strip()
    reasoning_chars = len(msg.get("reasoning_content") or "")

    ok = "OK " if content.startswith("{") else "?? "
    print(
        f"  {label:34s} {ok} {elapsed:5.1f}s  reasoning_tokens={reasoning:<6} "
        f"completion={usage.get('completion_tokens')}"
    )
    if content:
        print(f"      {content[:220]}")


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2

    base = sys.argv[1].rstrip("/")
    key = sys.argv[2]
    model = sys.argv[3] if len(sys.argv) > 3 else ""

    print(f"base_url = {base}")
    print("可用模型:")
    list_models(base, key)

    if not model:
        print("\n未指定 model，跳过参数探测")
        return 0

    print(f"\n参数探测（model={model}）:")
    # 不同厂商的关闭方式不一样，逐个试。同一语义的不同字段名都试一遍，
    # 因为实测名字相近的字段效果可能完全相反。
    for label, extra in [
        ("(默认参数)", {}),
        ("reasoning_effort=none", {"reasoning_effort": "none"}),
        ("reasoning_effort=minimal", {"reasoning_effort": "minimal"}),
        ("effort=low", {"effort": "low"}),
        ("chat_template_kwargs.thinking=false", {"chat_template_kwargs": {"thinking": False}}),
        ("enable_thinking=false", {"enable_thinking": False}),
        ("thinking={type:disabled}", {"thinking": {"type": "disabled"}}),
    ]:
        try_params(base, key, model, label, extra)
        time.sleep(2)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
