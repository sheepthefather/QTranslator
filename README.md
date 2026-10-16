# QTranslator

跨平台（Windows / Linux）截图翻译工具。截取屏幕区域，识别其中文字，翻译后
**擦掉原文并按原排版贴回译文**——模仿微信截图翻译的效果。

```
热键 Ctrl+Alt+Q → 框选区域 → 点「翻译」→ 得到译文覆盖图
```

## 特性

- **保留排版**：先做版面分析（横切带 → 竖切列 → 聚类成段落块），译文按原块
  的位置和字号重新排版，而不是逐行替换
- **擦除原文**：按背景复杂度自动选档（纯色填色 / TELEA / MI-GAN），不留残影
- **整屏批翻**：一次送出所有文本块并带上下文，术语在同一屏内保持一致
- **字体匹配**：用像素统计估计衬线/无衬线和字重，从系统字体里选最接近的
- **图片查看器**：无边框窗口，滚轮缩放（以光标为锚点）、拖拽平移、边缘拖拽缩放
- 可只 OCR 不翻译，也可只复制原图——不必每次都跑完整流程

## 安装

```bash
pip install -r requirements.txt
python -m qtranslator
```

启动后常驻托盘，按 `Ctrl+Alt+Q` 或左键单击托盘图标开始。

## 使用

1. 按 `Ctrl+Alt+Q`（或点托盘图标）
2. 屏幕变暗后**拖动框选**要翻译的区域，松开鼠标
3. 弹出预览窗，点「翻译并覆盖」
4. 切换「原文 / 译文」对比，或保存/复制结果

框选时按 `Esc` 或点右键取消。打开窗口时**不会自动识别**——它先当图片查看器
用，识别和翻译由你点按钮触发。点「翻译」时若还没识别过，会自动补一次 OCR。

### 操作

| 操作 | 方式 |
|---|---|
| 缩放图片 | 滚轮（以光标为锚点）/ 工具栏 `+` `-` |
| 平移图片 | 按住左键拖动 |
| 适应窗口 / 100% | 双击图片，或 `Ctrl+0` / `Ctrl+1` |
| 拖动窗口 | 按住工具栏空白处 |
| 缩放窗口 | 拖窗口四边或四角 |
| 最大化 | 双击工具栏空白处 |
| 切换原文/译文 | 工具栏 `‹` `›`，或 `Ctrl+←` / `Ctrl+→` |
| 识别 / 翻译 | `Ctrl+R` / `Ctrl+T` |
| 复制原图（图片） | `Ctrl+Shift+C` |
| 复制文本 / 保存图片 | `Ctrl+C` / `Ctrl+S` |
| 关闭 | `Esc` |

「···」按钮是溢出菜单：显示检测框 / 识别文字 / 段落块，以及各种复制与保存。

### 三个「复制」的区别

| 功能 | 给什么 | 何时可用 |
|---|---|---|
| 复制原图 | 原始截图（图片） | 随时，不需要识别 |
| 复制译文图片 | 擦除+回填后的图 | 翻译完成后 |
| 复制文本 | 识别或译文的文字 | 识别完成后 |

## 配置翻译服务

接入任意 OpenAI 兼容接口。默认无 key，此时用占位文本（`MockTranslator`）
让管线仍可跑通。

```bash
export QTRANSLATOR_API_KEY=xxx
export QTRANSLATOR_BASE_URL=https://api.deepseek.com
export QTRANSLATOR_MODEL=deepseek-flash
```

内置厂商预设（`config.py` 的 `PROVIDER_PRESETS`）：`deepseek`、`deepseek-pro`、
`glm`、`glm-flash`、`siliconflow`、`dashscope`、`openai`、`ollama`。

配置文件：Windows `%APPDATA%/QTranslator/config.json`，
Linux `~/.config/qtranslator/config.json`。

### 建议关闭推理链

推理模型会为每屏生成上千个 reasoning token，而翻译短文本不需要思考。
实测（4 个短句）：

| 模型 / 参数 | 耗时 | reasoning tokens |
|---|---|---|
| DeepSeek 默认 | 6.2s | 1214 |
| **`reasoning_effort: none`** | **1.0s** | **0** |

配置项 `disable_reasoning`（默认开）会发送 `reasoning_effort: none`。
若某厂商不认这个参数导致报错，设为 `false` 或 `QTRANSLATOR_DISABLE_REASONING=0`。

**换厂商时先跑一次探测**，因为各家的参数名和语义不统一（实测某家的
`reasoning_effort: minimal` 比默认还慢一倍）：

```bash
python tools/probe_api.py <base_url> <api_key> [model]
```

## 支持平台

| 平台 | 截图 | 全局热键 | 备注 |
|---|---|---|---|
| Windows | ✅ | ✅ | 完整支持 |
| Linux / X11 | ✅ | ✅ | 完整支持 |
| Linux / Wayland | ⚠️ | ❌ | 受限，见下 |

只抓**光标所在的显示器**，不支持跨屏框选。

**Wayland 限制**：`mss` 无法截图、`pynput` 无法注册全局热键。程序会检测到
Wayland 并提示改用托盘菜单，但截图仍然不可用。要支持需要换成 `xcap` +
xdg-desktop-portal。

## 目录结构

```
src/qtranslator/
  coords.py          坐标类型与转换（唯一允许做缩放的地方）
  capture.py         截图（mss）
  ocr.py             OCR 封装（RapidOCR）+ 阅读顺序排序
  layout.py          版面分析：横切带 / 竖切列 / 聚类成块 / 样式提取
  fontmatch.py       字体启发式匹配（serif-sans + 字重）
  translate.py       翻译后端（Mock + OpenAI 兼容）+ 整屏批翻
  erase.py           擦除（fill / TELEA / MI-GAN 自动选档）
  reflow.py          回填排版（shrink-to-fit + 语种断行 + 字体选择）
  pipeline.py        流水线装配
  config.py          配置管理
  ui/                选区窗、预览窗、画布、自绘工具栏、矢量图标、窗口缩放
  ui/tray.py         托盘 + 热键
  app.py             应用装配
tools/               冒烟测试、回归测试、API 探测（见下）
```

## 测试

```bash
python tools/test_layout.py      # 版面分析 + 断行（17 项）
python tools/test_zoom.py        # 图片缩放几何（45 项，含参数扫描）
python tools/test_resizer.py     # 窗口边缘缩放与光标（35 项）

python tools/smoke_ocr.py <图>              # OCR 准确率
python tools/smoke_stage2.py <图> --out out # 完整管线，输出对比图
python tools/probe_api.py <url> <key> [模型] # 探测翻译后端参数
```

回归测试用合成数据，不依赖 OCR 也不依赖样本图，跑得很快。它们针对的是
**无声失败**——版面分析和缩放逻辑错了不会抛异常，只会产出一张看起来不对劲
的图，肉眼很难判断边界情况。

## 已知限制

- **首次 OCR 推理约 3-6 秒**（模型图优化），之后 2.7-4.3 秒。启动时预热了模型，
  但没跑真实推理
- **OCR 会把紧邻的 CJK 文本并成一个框**（如导航栏的 `研究 政策`）。这是检测
  阶段的合并，不是排版问题
- **艺术字 / LOGO 识别差**（`ANTHROP\C` → `ANTHROPIC`）。装饰字体 OCR 天生吃亏
- **字体匹配只能对上「族」和「字重」**，区分不了两种不同的无衬线。要做到需要
  字体识别模型
- **译文缩到字号下限仍可能溢出**，此时会提示；目前没有手动调整的手段
- 图片放大到两端时**锚点会漂移**（占不满的轴被锁定居中，没有平移自由度）

## 致谢与许可

MIT License，见 [LICENSE](LICENSE)。

依赖全部为宽松许可：RapidOCR（Apache-2.0）、MI-GAN（MIT）、onnxruntime、
OpenCV、Pillow、PySide6（LGPL）。

**刻意没有引入**以下组件：NLLB-200（CC-BY-NC 非商用）、Surya 权重（非商用）、
manga-image-translator / BallonsTranslator / Pot Desktop（GPL/AGPL 传染性）。

版面分析与回填排版的思路参考了 [PDFMathTranslate](https://github.com/Byaidu/PDFMathTranslate)
（译文从原字号逐步缩小以塞进原框）和 manga-image-translator 的擦除流程。
