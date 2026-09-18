# 语音包批量生成工具（MiniMax 异步 TTS）

把**语音目录 Excel** 批量转成可交付的**多语言语音包**：默认「一次转换 = **1 个** MiniMax 任务」——
全部选中语言的词条合并成一个 zip 整批转换，完成后自动按语言落盘为 `<语言>/<语音ID>.mp3`，
并为每种语言打出可直接交付的 `<语言>.tar` 语音包。

- **GUI**（PySide6）：面向业务/运营同学，Windows 免安装 exe 双击即用，**无需安装 Python**；
  batch 模式提交即返，刷新/下载/重试都在「任务中心」完成
- **CLI**：面向开发者与脚本化批量处理，阻塞到全部音频落盘
- 解析器与 TTS 供应商**可插拔**：新增目录格式或更换语音厂商，不用改主干代码

## 快速开始

### Windows 用户（零安装，推荐）

1. 到 **[Releases](https://github.com/yuxilao/minimax_t2s_api/releases/latest)** 下载 `t2s-voice-tool.exe`，
   放进独立文件夹（如 `D:\语音工具\`）双击运行
2. 底部栏「**⚙ 设置**」填 MiniMax **API Key**（和音色 ID），保存
3. 顶部选 `.xlsx` 语音目录 →「**加载目录**」→ 勾选语言 →「**开始转换**」→ 到「**任务中心**」点「**下载并打包**」

### 源码运行（Windows / Linux）

```bash
pip install -r requirements.txt
python3 run_gui.py            # GUI
python3 main.py 目录.xlsx      # CLI
```

## 配置速览

GUI「⚙ 设置」对话框可视化编辑，保存即写回 `config.json`（打包版在 exe 同目录，源码运行在项目根）：

```json
{
    "provider": "minimax",
    "providers": {
        "minimax": {
            "api_key": "你的Key",
            "model": "speech-2.8-turbo",
            "voice_setting": { "voice_id": "male-qn-qingse", "speed": 1.0, "vol": 1.0, "pitch": 0 },
            "tts_mode": "batch"
        }
    }
}
```

其余字段（音频参数、轮询、并发等）都有合理默认值，完整说明见[使用说明 · 配置](docs/使用说明.md#6-配置说明)。

> **安全提醒**：切勿把含真实 Key 的 `config.json` 提交版本库或外发（`.gitignore` 已忽略）；
> 疑似泄露立即到 MiniMax 控制台吊销/轮换 Key。

## 输出结构

```
输出目录/
├── 中文/ Q001.mp3 …       # 每种语言一个子目录
├── 中文.tar …             # 同语言扁平 tar，可直接交付
└── 俄语/ … 俄语.tar
```

空文本条目自动跳过并计入「缺失」；任务结果约 9 小时内有效，请在任务中心尽快「下载并打包」。

## 文档

| 文档 | 内容 | 读者 |
|------|------|------|
| **[使用说明](docs/使用说明.md)** | 零安装部署、GUI 三页签详解、配置全字段、语音目录格式、完整 FAQ | 使用者 |
| **[技术文档](docs/技术文档.md)** | 工作原理与调用链路、CLI、解析器/供应商扩展、打包与测试、代码结构 | 开发者 |

## 常见问题（速览）

| 问题 | 处理 |
|------|------|
| 配额不足 / 429 | MiniMax 控制台确认额度；batch 模式请求数最少；逐条模式调低 `concurrency` |
| 认证失败（401/403） | 检查 Key 无空格、未过期、已开 TTS 权限；打包版看 exe 同目录的 `config.json` |
| exe 双击没反应 / SmartScreen | 「更多信息 → 仍要运行」；首次启动自解压慢几秒属正常 |
| GUI 打不开（Linux 源码） | `sudo apt install libxcb-cursor0`（打包版不受影响） |
| 任务一直「处理中」 | 任务中心点「刷新」或勾选自动刷新；结果约 9 小时过期，尽快下载 |

更多问题见[使用说明 · 常见问题](docs/使用说明.md#9-常见问题)。

---

## 许可与致谢

文本转语音能力由 [MiniMax 开放平台](https://platform.minimaxi.com/) 提供。
