"""配置管理。

优先级：环境变量 > 配置文件 > 内置默认。
环境变量优先是为了让 CI / 临时调试不必往磁盘写 key。

配置文件位置遵循各平台惯例（Windows 用 %APPDATA%，Linux 用 XDG_CONFIG_HOME）。
key 存在明文 JSON 里——这是桌面小工具的常见做法，但要清楚它的局限：
同机器上的其他进程可读。真要保护应当用系统钥匙串（keyring），
那是后续工作。
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# 各厂商的 OpenAI 兼容端点。用户改 base_url 即可切换。
PROVIDER_PRESETS = {
    # 智谱 GLM。注意 glm-4.7-flash 是**推理模型**：每次请求会先生成大量
    # reasoning token（实测 759 输出里 666 是思考），token 消耗和延迟约为
    # 非推理模型的 4 倍。追求低延迟可换 glm-4-flash。
    "glm": ("https://open.bigmodel.cn/api/paas/v4", "glm-4.7-flash"),
    "glm-flash": ("https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
    # DeepSeek。deepseek-flash 是推理模型，但我们用 reasoning_effort=none
    # 关掉了推理链：实测 6.2s / 1214 reasoning token → 1.0s / 0 token，
    # 而且译文质量更好（保留了 "AI" 缩写而非译成"人工智能"）。
    "deepseek": ("https://api.deepseek.com", "deepseek-flash"),
    "deepseek-pro": ("https://api.deepseek.com", "deepseek-v4-pro"),
    "siliconflow": ("https://api.siliconflow.cn/v1", "Qwen/Qwen2.5-7B-Instruct"),
    "dashscope": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "ollama": ("http://localhost:11434/v1", "qwen2.5:7b"),
}


@dataclass
class Config:
    """应用配置。"""

    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    target_lang: str = "zh"
    source_lang: str = "auto"
    hotkey: str = "<ctrl>+<alt>+q"
    font_path: str = ""
    migan_model: str = ""
    disable_reasoning: bool = True
    """关掉推理模型的思考链。翻译短文本不需要推理，而推理模型会为每屏
    生成上千个 reasoning token。默认开启；若某厂商不认这个参数导致报错，
    设 false。"""
    extra: dict = field(default_factory=dict)

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key.strip())

    def resolved_font(self) -> str | None:
        return self.font_path or None

    def resolved_migan(self) -> Path | None:
        if not self.migan_model:
            return None
        p = Path(self.migan_model)
        return p if p.exists() else None


def config_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "QTranslator"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "qtranslator"


def config_path() -> Path:
    return config_dir() / "config.json"


def load() -> Config:
    """读取配置。环境变量覆盖文件，文件缺失时用默认值。"""
    cfg = Config()

    path = config_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for key, value in data.items():
                if hasattr(cfg, key) and key != "extra":
                    setattr(cfg, key, value)
                else:
                    cfg.extra[key] = value
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("配置文件读取失败（%s），使用默认配置: %s", path, exc)

    # 环境变量覆盖
    env_map = {
        "QTRANSLATOR_API_KEY": "api_key",
        "QTRANSLATOR_BASE_URL": "base_url",
        "QTRANSLATOR_MODEL": "model",
        "QTRANSLATOR_TARGET_LANG": "target_lang",
        "QTRANSLATOR_FONT": "font_path",
        "QTRANSLATOR_MIGAN_MODEL": "migan_model",
    }
    for env, attr in env_map.items():
        value = os.environ.get(env)
        if value:
            setattr(cfg, attr, value)

    # 布尔项要单独处理：字符串 "false" 是真值，直接 setattr 会得到错误结果
    raw = os.environ.get("QTRANSLATOR_DISABLE_REASONING")
    if raw is not None:
        cfg.disable_reasoning = raw.strip().lower() not in {"0", "false", "no", "off", ""}

    return cfg


def save(cfg: Config) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(cfg), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info("配置已保存到 %s", path)
    return path


def apply_provider(cfg: Config, provider: str) -> Config:
    """按预设厂商填 base_url 和 model。"""
    preset = PROVIDER_PRESETS.get(provider)
    if preset is None:
        raise ValueError(f"未知厂商: {provider}（可选: {', '.join(PROVIDER_PRESETS)}）")
    cfg.base_url, cfg.model = preset
    return cfg
