# 语音包批量生成工具（MiniMax 异步 TTS）

把**语音目录 Excel** 批量转成可交付的**多语言语音包**：默认走「**一次转换 = 一个 MiniMax 任务**」的批量链路——
把选中语言的**全部词条合并成一个 zip**（成员名是 `d0001Q001.txt` 这种纯字母数字编码），以 `text_file_id` 调用 MiniMax
异步 TTS 接口（`/v1/t2a_async_v2`），下载结果 tar 后按编码解包归位到各语言，输出 `<输出目录>/<语言>/<语音ID>.mp3`，
并为每种语言打出 tar 语音包 `<语言>.tar`。共三种合成模式，由配置键 `tts_mode` 切换（`batch` / `sync` / `async`，见
[第 1 节](#1-工作原理)）。

- **GUI**（`run_gui.py`，PySide6）：面向业务/运营同学，可打包成 Windows 免安装 `exe`，双击即用，**无需安装 Python**；
  主窗口 = 顶部「语音目录」共享区 +「批量转换」「单条转换」「[任务中心](#gui任务中心页签)」三个页签 + 底部输出目录/供应商栏——
  batch 模式**提交即返**，后续刷新/下载打包/重试都在任务中心完成，任务记录持久化在 `tasks.json`。
- **CLI**（`main.py`）：面向开发者与脚本化批量处理（batch 模式**阻塞等待**到全部音频落盘，行为不变）。
- 解析器与 TTS 供应商都是**可插拔**的：新增一种目录格式或换一家语音厂商，不用改主干代码。

目录

- [1. 工作原理](#1-工作原理)
  - [GUI「任务中心」页签](#gui任务中心页签)
  - [GUI「单条转换」页签（MiniMax 同步接口）](#gui单条转换页签minimax-同步接口)
- [2. Windows 用户：零安装使用](#2-windows-用户零安装使用)
- [3. 开发者：安装与使用](#3-开发者安装与使用)
- [4. 配置说明](#4-配置说明)
- [5. 语音目录格式与解析器扩展](#5-语音目录格式与解析器扩展)
- [6. 供应商扩展](#6-供应商扩展)
- [7. 输出结构与注意事项](#7-输出结构与注意事项)
- [8. 自行打包（免安装 exe）](#8-自行打包免安装-exe)
- [9. 运行测试](#9-运行测试)
- [10. 目录结构](#10-目录结构)
- [11. 常见问题](#11-常见问题)

---

## 1. 工作原理

```
语音目录.xlsx ─▶ 解析器 ─▶ 全部语言合并成 1 个 zip ─▶ 上传 ─▶ 创建 1 个异步任务 ─▶ 轮询 ─▶ 下载结果 tar
              xlsx_9lang   d0001Q001.txt …          files/upload   t2a_async_v2(text_file_id)

        ─▶ 按编码解包归语言 ─▶ 逐条落盘 + 每语言打 tar
                             <语言>/<语音ID>.mp3 + <语言>.tar
```

| 环节 | 说明 |
|------|------|
| 解析 | 读取语音目录，得到 `{语言 → [(语音ID, 文本), …]}`；解析器可自动检测（`auto`）或手工指定 |
| 合成 | 由配置键 `tts_mode` 决定，默认 `batch`：**所有选中语言的词条合并为一个 zip、只创建 1 个 MiniMax 任务**整批转换（链路见下）。三种模式统一策略：空文本条目跳过并计入缺失；单条失败降级记 warning 继续；认证/配额类全局错误立即终止整批（快速失败） |
| 打包 | **转换完成即落盘**：结果 tar 按成员编码解出后，每条立即写 `<输出目录>/<语言>/<语音ID>.mp3`；该语言结束后把成功条目打成 `<输出目录>/<语言>.tar`（tar 内顶层扁平，只有 `<语音ID>.mp3`） |
| 报告 | 每种语言输出「成功 / 缺失 / 未知」三个计数，缺失项列出语音 ID |
| 任务记录 | GUI 的 batch 模式把提交的任务写进 `tasks.json`（语言清单、编码↔语言映射、词条快照、状态、结果 `file_id`），由「任务中心」页签接管完整生命周期；CLI 不落任务记录，见 [GUI「任务中心」页签](#gui任务中心页签) |

**默认 batch 模式完整链路（一次转换 = 1 个任务，与语言数无关）**：

1. **合并打包 zip**：把全部选中语言的每个非空条目写成一个 `<目录码><语音ID>.txt` 成员（目录码按语言顺序取
   `d0001`、`d0002`…，故成员名形如 `d0001Q001.txt`，**纯字母数字**，不含中文与非 ASCII 路径），全部打包成**一个**
   zip。语言 ↔ 目录码的映射（`entry_map`）与词条快照一起持久化在任务记录里，用于下载后归位与重试；
2. **上传**：`POST /v1/files/upload`（`purpose=t2a_async_input`）→ 返回整型 `file_id`（记为 `upload_file_id`），
   上传用的临时 zip 用完即清理；
3. **创建任务**：`POST /v1/t2a_async_v2` 以 `text_file_id` 传入上一步的 `file_id`——**一个任务整批转换 zip 内全部
   文本**（跨所有语言），返回 `task_id` 与 `usage_characters`；
4. **轮询**：`GET /v1/query/t2a_async_query_v2?task_id=` 查询状态直至 `success` 返回结果 `file_id`（failed/expired/
   超时抛错）。CLI 按 `poll_interval` 间隔原地阻塞轮询（`poll_timeout` 默认 `1200` 秒）；GUI batch 模式**不轮询**，
   改由「任务中心」手动/自动刷新，每次刷新对每条非终态记录做一次查询；
5. **下载**：`GET /v1/files/retrieve_content?file_id=` 取回结果 tar，成员名形如 `…_d0001Q001/content-…_d0001Q001.mp3`；
6. **解包**：从成员名提取 `<目录码><语音ID>` 编码，得到 `{d0001Q001 → mp3 字节}`；提不出编码的成员记 warning 丢弃；
7. **按编码归语言 + 落盘**：逐条按 `entry_map[目录码] → 语言` 还原归属，每条立即写 `<输出目录>/<语言>/<语音ID>.mp3`，
   每语言再打一个 `<语言>.tar`；结果计数写回任务记录（`language_results`）。

**三种 tts_mode 模式对比**：

| 模式（`tts_mode`） | 调用次数（一次转换） | 速度特点 | 适用场景 |
|------|------|------|------|
| `batch`（默认） | **1 次异步任务**（含全部选中语言的全部条目） | 服务端整批处理，约数分钟；本地只需等待+下载解包 | 正式批量出包（默认选择） |
| `sync` | 每条 1 次同步请求（`POST /v1/t2a_v2`，并发 `concurrency`） | 音频 hex 随响应返回、无轮询，短文本秒级（实测约 0.7 秒/条） | 小批量/试用/演示；超过 `sync_max_chars` 的条目自动回退异步任务链路 |
| `async` | 每条 1 次异步任务（`POST /v1/t2a_async_v2` 直传 text，并发 `concurrency`） | 逐条创建+轮询，受并发与服务端队列影响 | 需要逐条独立任务、条目间互不影响时 |

> batch 模式下 GUI 与 CLI 行为不同：GUI「批量转换」页签点「开始转换」**提交即返**（任务入库后由任务中心接管），
> CLI 则阻塞到「轮询 → 下载 → 解包 → 落盘」全部完成。`sync` / `async` 逐条模式在 GUI 与 CLI 都是**阻塞等待**。

> 任务成功后返回的结果文件**约 9 小时内有效**，超时需重新提交任务；CLI 在任务完成后立即下载，正常无需关心；
> GUI 任务中心里「待下载」超过 8 小时会给出告警，过期/丢失可用「重试」重新提交（详见 [GUI「任务中心」页签](#gui任务中心页签)）。

### GUI「任务中心」页签

batch 模式提交即返，任务的后续生命周期集中在这里管理。

**任务状态机（生命周期）**：

```
[批量转换页签「开始转换」/ 任务中心「重试」── 提交 1 个任务、写入 tasks.json]
                          │
                          ▼
                   processing 处理中
                      │
                      ├─ 查询仍是处理中 ────────▶（留在 processing，继续刷新）
                      ├─ 查询返回 failed ───────▶ failed   失败    ┐
                      ├─ 查询返回 expired ──────▶ expired  已过期  ├─「重试」＝用记录里的词条快照
                      ├─ 查询「任务不存在」──────▶ lost     记录丢失 ┘   重新提交（新 task_id、
                      │                                                 attempts+1，回到 processing）
                      └─ 查询返回 success ──────▶ success  待下载
                                                  │
                                    点「下载并打包」┤
                                                  ▼
                                         downloading 下载中
                                            │          │
                          解包+逐条落盘+打 tar          下载/解包失败（状态回滚）
                                            ▼          ▼
                              packaged 已完成      success 待下载
                              （终态）             （可再点一次「下载并打包」）
```

| 状态（`tasks.json`） | 界面显示 | 含义 | 可做的操作 |
|------|------|------|------|
| `processing` | 处理中 | 已提交，服务端整批转换中 | 刷新 |
| `success` | 待下载 | 服务端已完成，结果 `file_id` 已拿到（约 9 小时过期） | 刷新、**下载并打包** |
| `downloading` | 下载中 | 下载/解包/打包进行中（中间态，防重入） | 等待 |
| `packaged` | 已完成 | 已落盘并生成各语言 tar（终态） | 删除记录（取结果用底部栏「打开」打开输出目录） |
| `failed` | 失败 | 服务端任务处理失败 | **重试** |
| `expired` | 已过期 | 结果文件在服务端已过期 | **重试** |
| `lost` | 记录丢失 | 查询返回「任务不存在」 | **重试** |

**列表列**（与界面表头一致）：任务ID / 语言（本次任务包含的全部语言）/ 条数 / 状态 / 已提交（相对时间，如「3分钟前」）/
结果 `file_id`（还没拿到结果时显示上传用的 `file_id`）/ 最近错误/缺失（失败与过期原因、下载与解包 warning；已完成的任务
显示「全部成功」或「缺失 N 条」）。状态列按颜色区分，支持多选（多选时「下载并打包」「重试」按选中顺序逐条排队执行）；
列表底部固定一行「结果文件约 9 小时过期」的提示。

**操作**（都在后台线程串行执行，界面不卡）：

| 按钮 | 行为 |
|------|------|
| 刷新 | 对选中记录逐条调用查询接口更新状态；未选中时刷新全部非终态记录（终态记录跳过，不会把 `packaged` 查回退） |
| 自动刷新（每 60 秒） | 勾选后每 60 秒自动刷新一次全部非终态记录，默认关闭 |
| 下载并打包 | 下载结果 tar → 按编码解包 → 逐条落盘 `<语言>/<语音ID>.mp3` → 每语言生成 `<语言>.tar` → 置 `packaged`；中途失败会回滚为「待下载」，可再次点击重试 |
| 重试 | 对 `failed` / `expired` / `lost` 记录，用保存的**词条快照**重新打包提交，得到新 `task_id`（`attempts` +1） |
| 删除记录 | 弹确认框后仅删 `tasks.json` 里的记录，**不影响已落盘的音频与 tar** |

取结果也可以随时点主窗口底部栏的「打开」，直接打开输出目录。

**⚠ 9 小时过期警示**：任务成功后结果文件约 **9 小时**内可下载。记录处于「待下载」且**已提交超过 8 小时**仍未打包时，
任务中心会在日志里给出 `⚠ 已提交超 8 小时仍未下载，结果文件约 9 小时过期，请尽快「下载并打包」` 告警（每条任务只提示
一次），页签底部的提示行也长期标明有效期，状态栏同时告警。建议：**开启「自动刷新」，任务完成后尽快下载并打包**；一旦过期，只能「重试」重新
提交并再次消耗配额。

**任务记录文件 `tasks.json`**：与 `config.json` **同目录**——源码运行在项目根（`项目根/tasks.json`），打包版在
**exe 旁**（`exe 同目录/tasks.json`）。JSON 结构为 `{"version": 1, "tasks": [记录…]}`（新的在前），采用
「写临时文件 + 原子替换」保存，程序崩溃不会写出半截文件；文件损坏时会自动改名备份为 `tasks.json.corrupt-<时间戳>.bak`
并从空开始。记录内含词条**文本快照**（供「重试」原样重提交），不含 API Key；换机器时把 `config.json` 与
`tasks.json` 一起拷走即可延续任务列表。**删除 `tasks.json` 只会丢任务列表，不影响已落盘的语音文件。**

离线演示：把供应商换成内置的 `fake`（不联网、不消耗配额），可完整验证解析与打包链路，见 [CLI 用法](#cli-用法)。

### GUI「单条转换」页签（MiniMax 同步接口）

「单条转换」页签位于「批量转换」与「任务中心」之间，用于**一次只转一条**——抽一条词条即时出音频，试听文本与音色效果。
它不走批量链路，而是直接调用 MiniMax **同步**接口 `POST /v1/t2a_v2`：**一次请求直接返回音频**，不创建任务、
无需轮询、也不进任务中心。

```
顶部「语音目录」区选文件 ─▶ 「加载目录」 ─▶ 选语言 ─▶ 选语音ID（可关键字过滤，文本预览） ─▶ 「转换」 ─▶ <输出目录>/<语言>/<语音ID>.mp3
```

**操作流程**：

1. 在顶部共享的「语音目录」区选目录文件（选好解析器），点「加载目录」解析出全部语言与词条（解析结果与批量页签共用）；
2. **先选语言**，再**选语音 ID**（可用关键字过滤框缩小范围）——选中后可看到该条**文本预览**，空文本条目会标注
   「无法合成」且「转换」按钮禁用；
3. 点「转换」，音频同步返回后立即落盘；成功后状态行显示输出路径，可点「**打开所在目录**」直接定位文件。

**输出**：只写单条 `<输出目录>/<语言>/<语音ID>.mp3`（输出目录是主窗口底部栏的全局设置，批量与单条共用），
**不打 `<语言>.tar` 包**。

**参数**：音色、语速、音量、音调、采样率等合成参数统一在底部栏「**⚙ 设置**」对话框里配置（保存即写回 `config.json`，
批量与单条共用同一份），主界面不再常驻参数字段。

**注意**：

| 事项 | 说明 |
|------|------|
| 空文本 | 文本预览区标注「无法合成」并禁用「转换」按钮，**不发请求、不消耗 API 配额** |
| 取消 | 页签**不提供「取消」按钮**：同步请求一旦发出即不可中断，只能等待其返回（不给虚假期待） |
| 离线演示 | 供应商选 `fake` 可完整走一遍该流程（不联网、不扣配额），适合无 Key 时演示界面操作 |
| 多供应商 | 统一走供应商中间层接口 `synthesize_one`：MiniMax 覆盖为同步 `/v1/t2a_v2`；未覆盖的供应商由基类默认实现自动委托其批量接口逐条合成，开箱即兼容 |

---

## 2. Windows 用户：零安装使用

**你不需要安装 Python，也不需要装任何依赖。**

1. **下载**：到仓库的 **Releases** 页面下载最新版的 `t2s-voice-tool.exe`
   （维护者打 `v*` 标签后由 GitHub Actions 自动构建并发布到 Release，无需登录即可下载）；
   没有 Release 时，也可到 Actions 最新一次 `build` 运行的产物里下载 `t2s-voice-tool-windows-latest`。
   exe 已内置 Python 运行时、PySide6、openpyxl、requests——**不需要安装任何依赖**。
2. **放置**：新建一个自己的文件夹（例如 `D:\语音工具\`），把 `t2s-voice-tool.exe` 放进去，**双击运行**。
   - 首次运行若出现 Windows SmartScreen 蓝框：点「更多信息」→「仍要运行」。
   - 单文件版首次启动需自解压，慢几秒属正常现象。
   - 建议不要把 exe 放在桌面/下载目录直接跑，放独立文件夹便于管理它生成的 `config.json`、`tasks.json` 和 `语音包\`。
3. **填 API Key**（两种方式任选）：
   - 在 GUI 底部栏选好供应商，点「**⚙ 设置**」在对话框里填 **API Key**（以及音色 ID 等），点「保存」即写回
     `config.json`（Key 输入框带 👁 显示切换，其余合成参数也都在这个对话框里）；
   - 或在 exe 同目录手工新建 `config.json`（模板见 [第 4 节](#4-配置说明)），保存后重启 GUI（启动自动加载配置）。

   > 打包版是「冻结」运行：配置永远读写 **exe 同目录的 `config.json`**（任务记录 `tasks.json` 也在 exe 旁），
   > 源码目录里的 `config.json` 不会被用到。
4. **转换（「批量转换」页签）**：顶部「语音目录」区「浏览…」选中 `.xlsx`、选好解析器 → 点「**加载目录**」→
   在语言复选列表勾选要转的语言（**默认全选；全部不勾选 = 全部语言**，另有「全选 / 清空」按钮）→ 确认底部输出目录
   （默认 exe 同目录的 `语音包\`）→ 点「**开始转换**」。
   默认 `batch` 模式下这一步**提交即返**：全部语言合并成一个 zip、只创建 **1 个** MiniMax 任务，弹窗提示任务 ID 后
   自动跳到「任务中心」页签；中途可「取消」（仅取消提交前的本地工作/逐条模式的进行）。
   `tts_mode` 为 `sync` / `async` 时不跳页签，进度条、状态行与可折叠的「详细日志」都在批量页签内，跑完直接落盘。
5. **取结果（「任务中心」页签）**：选中任务 → 点「**刷新**」（或勾选「自动刷新（每 60 秒）」）→ 状态变成
   「**待下载**」后点「**下载并打包**」→ 音频落盘为 `语音包\<语言>\<语音ID>.mp3`，并生成 `<语言>.tar`，状态变
   「已完成」。用底部栏的「**打开**」直接取结果。失败/过期/丢失的任务点「**重试**」会按原词条重新提交。
   **结果文件约 9 小时过期，超 8 小时未下载界面会告警，请尽快下载并打包**（详见 [任务中心](#gui任务中心页签)）。

**自定义解析脚本**：把写好的 `.py`（见 [第 5 节](#5-语音目录格式与解析器扩展)）放到 **exe 同目录的 `parsers_custom\`**
文件夹里，重启 exe 即可在「解析器」下拉框中看到它（无需重新打包）。

> 打包版只包含 GUI。需要在命令行里跑批的，请用下一节的 Python 环境方式。

---

## 3. 开发者：安装与使用

### 环境要求

Python 3.8+。GUI 视图层基于 **PySide6**（Fusion 风格 + 内置 QSS），Windows / Linux 跨平台，随 pip 一起安装；
无显示服务器的环境（CI / SSH 纯命令行）可设 `QT_QPA_PLATFORM=offscreen` 跑自动化（测试与自检脚本即如此）。

> **Linux 桌面用户注意**：Qt 6.5+ 的 xcb 插件依赖系统库 `libxcb-cursor0`，缺失会在启动时直接崩溃
> （报 `Could not load the Qt platform plugin "xcb"`）。**仅源码运行需要安装它**（打包版 exe 已内置该库，不受影响）：
> `sudo apt install libxcb-cursor0`（Fedora: `dnf install xcb-util-cursor`；Arch: `pacman -S libxcb-cursor`）。
> GUI 启动前会自动检查并给出上述提示。

### 安装依赖

```bash
pip install -r requirements.txt
```

```
requests>=2.31.0
openpyxl>=3.0
PySide6>=6.5
```

### 启动 GUI

```bash
python3 run_gui.py
```

主窗口自上而下：顶部「**语音目录**」共享区（目录文件 + 浏览 + 解析器 +「加载目录」，批量与单条页签共用解析结果）、
「**批量转换**」「**单条转换**」「**任务中心**」三个页签（batch 模式提交的任务在任务中心刷新状态、下载并打包、重试，
见 [GUI「任务中心」页签](#gui任务中心页签)）、底部栏（输出目录 + 浏览 + 打开 ｜ 供应商下拉 + 音色摘要 +「⚙ 设置」）。
配置**启动自动加载**；供应商与合成参数（API Key、音色、语速/音量/音调、采样率/比特率/格式/声道、base_url 等）都收纳在
「⚙ 设置」对话框里，保存即写回 `config.json`，主界面不再常驻参数字段，也没有单独的「加载配置/保存配置」按钮。

### CLI 用法

`main.py --help` 实际输出：

```
usage: main.py [-h] [--parser PARSER] [--language LANGUAGES]
               [--provider PROVIDER] [--config CONFIG] [--output OUTPUT]
               input_file

语音目录批量转语音包工具（MiniMax 异步 TTS，并发逐条合成）

positional arguments:
  input_file            语音目录文件（如 9国语种 xlsx）

optional arguments:
  -h, --help            show this help message and exit
  --parser PARSER       解析器名或 auto 自动检测（默认 auto）
  --language LANGUAGES  只转换指定语言，可多次指定；默认全部语言
  --provider PROVIDER   供应商（默认取配置 provider；离线演示用 fake）
  --config CONFIG       配置文件路径
  --output OUTPUT       输出目录（默认 <输入文件所在目录>/语音包）

示例:
  python main.py "9国语种...xlsx"                      # 全部语言，自动识别解析器
  python main.py list.xlsx --language 俄语 --language 日语
  python main.py list.xlsx --provider fake --output /tmp/out   # 离线演示
```

参数说明：

| 参数 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `input_file` | 是 | — | 语音目录文件路径（当前内置解析器支持 `.xlsx`） |
| `--parser` | 否 | `auto` | 解析器名（如 `xlsx_9lang`）；`auto` 由解析器各自 `detect()` 打分取最高 |
| `--language` | 否 | 全部 | 只转换指定语言，**可重复传**（`--language 俄语 --language 日语`）；名字须与表头语言列完全一致 |
| `--provider` | 否 | 取配置 `provider` | 供应商名（`minimax` / `fake`） |
| `--config` | 否 | 项目根 `config.json` | 配置文件路径 |
| `--output` | 否 | `<输入文件所在目录>/语音包` | 输出目录 |

常用示例：

```bash
# 全部语言，自动识别解析器
python main.py "9国语种+译文-中性语音列表.xlsx"

# 只转两种语言，指定解析器与输出目录
python main.py list.xlsx --parser xlsx_9lang --language 俄语 --language 日语 --output /tmp/out

# 离线演示（不联网、不消耗配额，只验证解析+打包）
python main.py list.xlsx --provider fake --output /tmp/demo

# 使用另一份配置（例如另一个 API Key）
python main.py list.xlsx --config /path/to/other_config.json
```

CLI 的 batch 模式**行为不变：全程阻塞**（提交 1 个任务 → 原地轮询 → 下载 → 解包 → 按语言落盘打 tar 才返回），
不写 `tasks.json`、不使用「任务中心」（任务中心是 GUI 的能力）。需要「提交即返、稍后取包」请用 GUI。

退出码：`0` 全部成功；`1` 出错或存在缺失条目；`130` 被取消（`Ctrl+C`）。
运行过程中每步都会打印 `[阶段] 消息`（`parse` / `zip` / `create` / `poll` / `download` / `package` / `done`；
`poll` 行原地刷新不刷屏）。

---

## 4. 配置说明

配置文件为 JSON。结构 = 顶层选择 `provider` + 每个供应商一个配置块，便于并存多家厂商（下例即内置默认值
`PROVIDER_DEFAULTS`，把 `api_key` 换成自己的 Key、`voice_setting.voice_id` 换成目标音色即可用）：

```json
{
    "provider": "minimax",
    "providers": {
        "minimax": {
            "api_key": "",
            "base_url": "https://api.minimaxi.com",
            "model": "speech-2.8-turbo",
            "voice_setting": {
                "voice_id": "male-qn-qingse",
                "speed": 1.0,
                "vol": 1.0,
                "pitch": 0
            },
            "audio_setting": {
                "audio_sample_rate": 32000,
                "bitrate": 128000,
                "format": "mp3",
                "channel": 2
            },
            "poll_interval": 10,
            "poll_timeout": 1200,
            "concurrency": 5,
            "tts_mode": "batch",
            "sync_max_chars": 9000,
            "request_timeout": 30,
            "upload_timeout": 120,
            "download_timeout": 60
        }
    }
}
```

`providers.minimax` 各字段（缺省即取内置默认值 `t2s_tool/config.py:PROVIDER_DEFAULTS`）：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `api_key` | string | 是 | — | MiniMax API Key（Bearer Token） |
| `base_url` | string | 否 | `https://api.minimaxi.com` | 接口基址，一般不用改 |
| `model` | string | 否 | `speech-2.8-turbo` | 模型标识符，见下表 |
| `voice_setting.voice_id` | string | 是 | `male-qn-qingse` | 音色 ID（整批共用一个音色） |
| `voice_setting.speed` | float | 否 | `1.0` | 语速，0.5 ~ 2.0 |
| `voice_setting.vol` | float | 否 | `1.0` | 音量，0 ~ 10 |
| `voice_setting.pitch` | int | 否 | `0` | 音调，-12 ~ 12 |
| `audio_setting.audio_sample_rate` | int | 否 | `32000` | 采样率（Hz），建议 32000 |
| `audio_setting.bitrate` | int | 否 | `128000` | 比特率（bps） |
| `audio_setting.format` | string | 否 | `mp3` | 音频格式 |
| `audio_setting.channel` | int | 否 | `2` | 声道（1=单声道，2=立体声） |
| `poll_interval` | number | 否 | `10` | 任务轮询间隔（秒）：CLI batch 模式对整批任务、sync/async 模式对单条任务轮询；GUI 任务中心「刷新」是单次查询，不受它约束 |
| `poll_timeout` | number | 否 | `1200` | **单个任务**轮询超时（秒，默认 20 分钟）；batch 模式一次任务要整批转换全部语言，文本很长或服务端排队久时可继续调大（如 `1800`~`3600`）。CLI 超时抛错，GUI 任务中心靠刷新驱动、不用它 |
| `concurrency` | int | 否 | `5` | 并发数（**仅 `tts_mode` 为 sync/async 的逐条模式生效**，batch 模式不用它）；免费/限流账户建议调低为 `2`~`3`，过大易触发 429 限流 |
| `tts_mode` | string | 否 | `batch` | 合成模式：`batch`=**所有语言词条合并为一个 zip、以 `text_file_id` 创建 1 个任务**整批转换（默认）；`sync`=逐条并发同步接口；`async`=逐条并发异步任务（对比见 [第 1 节](#1-工作原理)） |
| `sync_max_chars` | int | 否 | `9000` | sync 模式下单条文本最大字符数，超过的条目自动回退异步任务链路兜底 |

另有网络超时项一般无需改：`request_timeout`=30s（单次 HTTP 请求）、`upload_timeout`=120s（batch 模式 zip 上传）、
`download_timeout`=60s（结果下载）；

**支持的模型**（以账户实际权限为准，报「model 无权限」时换一项）：

| 模型标识符 | 说明 |
|-----------|------|
| `speech-2.8-turbo` | 2.8T turbo 速度版（推荐，默认） |
| `speech-2.8-hd` | 2.8T 高清版 |
| `speech-2.6-hd` | 2.6T 高清版 |
| `speech-2.6-turbo` | 2.6T turbo 速度版 |

### 旧版配置自动迁移

老版本 `config.json` 是扁平结构（顶层直接放 `api_key` / `model` / `voice_setting` / `audio_setting`）。
加载时会自动识别并迁移成 `{"provider": "minimax", "providers": {"minimax": {...}}}`，缺失字段补默认值，
**无需手工改写**；在 GUI「⚙ 设置」对话框里点「保存」后即以新结构回写。

### 配置与任务记录的存放位置

| 运行方式 | 默认配置文件 | 任务记录（任务中心） | 默认输出目录 | 自定义解析器目录 |
|----------|--------------|----------------------|--------------|------------------|
| 打包版（exe 双击） | `exe 同目录/config.json` | `exe 同目录/tasks.json` | `exe 同目录/语音包` | `exe 同目录/parsers_custom` |
| 源码运行 | `项目根/config.json` | `项目根/tasks.json` | `<输入文件所在目录>/语音包`（CLI）／`项目根/语音包`（GUI） | `项目根/parsers_custom` |

`tasks.json` 由 GUI 自动创建与维护（与 `config.json` 同目录，见 [任务中心](#gui任务中心页签)）；CLI 不使用它。
删掉 `tasks.json` 只是清空任务列表，不会删除已落盘的音频与 tar。

### 安全提醒

- **切勿**把含真实 `api_key` 的 `config.json` 提交到版本库（`.gitignore` 已忽略 `config.json`）。
- `tasks.json` 不含 Key，但保存了**词条文本快照**（语音目录里的文案）与任务/文件 ID；外发或提交版本库前请确认
  文案是否敏感（建议一并加入 `.gitignore`）。
- 分享打包版给他人时只给 exe，让对方自己填自己的 Key；不要把带 Key 的整个文件夹压缩包外发。
- 疑似泄露时立刻到 MiniMax 控制台吊销/轮换 Key。

---

## 5. 语音目录格式与解析器扩展

### 内置解析器 `xlsx_9lang`（9 国语种语音列表）

规则（对应 `t2s_tool/parsers/xlsx_9lang.py`）：

- 只读第一个工作表；**首行为表头**，表头列名（除跳过列外）即语言名。
- **首列 = 语音 ID**，必须匹配正则 `^Q\d+$`（如 `Q001`、`Q8107`）；首列为空的行跳过，
  ID 非法但有内容的行记入解析警告并跳过，重复 ID 去重并告警。
- 跳过列：`AP音效名称`、`场景详细描述`。
- 单元格文本里的换行会被替换成空格并 `strip()`；空文本仍保留条目（合成阶段跳过，最终计入缺失）。
- 每语言条目按语音 ID 数字部分升序排列。
- `detect()`：文件是 `.xlsx` 且表头除跳过列外还有 ≥2 个语言列 → 打分 80（供 `auto` 自动选中）。

示例目录（列顺序任意，`AP音效名称`/`场景详细描述` 可有可无）：

| 语音ID | 中文 | 英文 | 俄语 | AP音效名称 | 场景详细描述 |
|--------|------|------|------|-----------|--------------|
| Q001 | 您好，请系好安全带 | Hello, please fasten your seatbelt | Здравствуйте, пристегните ремень | ding | 登机提示 |
| Q003 | 航班即将起飞 | The flight is about to take off | Рейс скоро вылетает | bell | 广播 |

产出 `语音包/中文/Q001.mp3`、`语音包/中文.tar` …（语言数不限于 9 种）。

### 写一个新解析器

继承 `ParserBase`、加 `@register_parser`，把文件放进 **`parsers_custom/`**（源码模式＝项目根，打包模式＝exe 同目录）
即可被自动加载，出现在 GUI「解析器」下拉框 / CLI `--parser <name>`；文件名以 `_` 开头的会被忽略。
自定义脚本在打包版里同样可用，**不需要重新打包 exe**。

最小示例（`parsers_custom/csv_simple.py`）：

```python
# -*- coding: utf-8 -*-
"""示例：语音目录为 CSV，首列语音ID，其余列为各语言文本。"""
from __future__ import annotations

import os

from t2s_tool.errors import ParseError
from t2s_tool.models import LanguagePack, ParseResult, VoiceEntry
from t2s_tool.parsers import register_parser
from t2s_tool.parsers.base import ParserBase


@register_parser
class CsvSimpleParser(ParserBase):
    name = "csv_simple"          # 必填且全局唯一，CLI --parser 用的就是它
    display_name = "简单CSV(自定义)"  # GUI 下拉框显示名
    extensions = (".csv",)

    def detect(self, path: str) -> int:
        """0-100 匹配分；不识别必须返回 0（auto 才不会误选）。"""
        if not str(path).lower().endswith(".csv") or not os.path.exists(path):
            return 0
        try:
            with open(path, encoding="utf-8-sig") as f:
                head = f.readline().strip().split(",")
        except Exception:
            return 0
        return 60 if len(head) >= 3 else 0

    def parse(self, path: str) -> ParseResult:
        warnings, buckets = [], {}
        with open(path, encoding="utf-8-sig") as f:
            header = [h.strip() for h in f.readline().rstrip("\n").split(",")]
            for rno, line in enumerate(f, 2):
                if not line.strip():
                    continue
                cols = [c.strip() for c in line.rstrip("\n").split(",")]
                vid = cols[0]
                if not vid.startswith("Q"):
                    warnings.append(f"第{rno}行语音ID非法，已跳过: {vid}")
                    continue
                for i, lang in enumerate(header[1:], 1):
                    if not lang:
                        continue
                    text = cols[i] if i < len(cols) else ""
                    buckets.setdefault(lang, []).append(VoiceEntry(vid, text))
        if not buckets:
            raise ParseError(f"未从 {path} 解析出任何语言列")
        packs = [LanguagePack(lang, entries) for lang, entries in buckets.items()]
        return ParseResult(path, self.name, packs, warnings)
```

要点：返回的 `ParseResult.languages` 里每个 `LanguagePack` 的 `language` 会直接当输出子目录名
（非法字符会被替换为 `_`），`entries` 的 `voice_id` 决定输出文件名 `<语音ID>.mp3`。
batch 模式下 zip 成员名是 `<目录码><语音ID>.txt`（如 `d0001Q001.txt`），语言名不进 zip、只用于本地目录与 `<语言>.tar`
命名，所以语言列叫「中文」还是「zh-CN」都不影响接口调用。

---

## 6. 供应商扩展

供应商接口（`t2s_tool/providers/base.py`）只要实现两个方法：

```python
# t2s_tool/providers/myvendor.py  —— 骨架
from __future__ import annotations

from typing import Callable, List

from ..errors import ConfigError
from ..models import CancelToken, ProviderResult, StageEvent, VoiceEntry
from . import register_provider
from .base import TTSProvider


@register_provider
class MyVendorProvider(TTSProvider):
    name = "myvendor"            # 全局唯一，config 的 provider / CLI --provider 用它
    display_name = "我的语音厂商"  # GUI 下拉框显示名

    def validate_config(self, config: dict) -> None:
        """启动前校验配置块，不合法就抛 ConfigError。"""
        if not config.get("api_key"):
            raise ConfigError("myvendor 配置缺少 api_key")

    def synthesize_batch(self, entries: List[VoiceEntry], config: dict,
                         on_event: Callable[[StageEvent], None],
                         cancel_token: CancelToken,
                         on_entry=None) -> ProviderResult:
        """把一批条目合成为音频。返回 ProviderResult(audio_by_id, warnings)。

        - audio_by_id: {语音ID: mp3 字节}，缺失的 ID 直接不放（打包时计入「缺失」）
        - on_event: 上报进度，StageEvent(stage, message, current, total)，
          stage 现取 parse/zip/create/poll/download/package/done（轮询在供应商内部完成，不必单独上报）
        - cancel_token: 每个循环调用 cancel_token.throw_if_cancelled() 以支持 GUI「取消」
        - on_entry: 可选回调 (entry, mp3字节)，每条拿到音频立即调用（主流程用于边转边落盘）
        """
        audio = {}
        for i, e in enumerate(entries, 1):
            cancel_token.throw_if_cancelled()
            if e.is_empty:
                continue
            audio[e.voice_id] = self._synth_one(e, config)   # 自行实现 HTTP 调用
            if on_entry is not None:
                on_entry(e, audio[e.voice_id])
            on_event(StageEvent("download", f"{i}/{len(entries)}", i, len(entries)))
        return ProviderResult(audio, [])
```

> **可选：覆盖 `synthesize_one` 给「单条转换」一条更优路径。** 单条转换页签统一走中间层接口
> `provider.synthesize_one(entry, config, cancel_token) -> mp3 字节`；基类默认实现会委托
> `synthesize_batch([entry])`，所以新供应商**不写也能用**单条转换。若平台有同步单条接口，建议像
> `MinimaxProvider.synthesize_one` 那样覆盖它（一次请求直接返回音频，不经过批量通道）。

写完后在 `t2s_tool/providers/__init__.py` 的 `load_providers()` 里加一行导入
（`from . import minimax, fake, myvendor`）即完成注册；配置里把 `provider` 换成 `"myvendor"`，
并在 `providers` 下新增同名配置块即可。可参考最简实现 `t2s_tool/providers/fake.py`
（离线假数据）与生产实现 `t2s_tool/providers/minimax.py`。

> **batch（单任务）链路与「任务中心」目前是 minimax 专属**：`pipeline` 在 `tts_mode=batch` 且 provider 为 `minimax`
> 时走「跨语言合并 zip → `submit_batch` → 轮询/刷新 → 按编码解包」的编排（GUI 还会写 `tasks.json`）。新供应商若要享受
> 同样的单任务 + 任务中心能力，需提供等价的四个能力（提交 `submit_batch`、查询 `query_task`、下载 `_download_tar`、
> 按编码解包 `extract_named_mp3s`），并在 `pipeline` 的 batch 分支与 `t2s_tool/tasks.py` 里放宽 provider 判断；否则
> 它会退回 `synthesize_batch` 的逐条路径，GUI 也只是阻塞跑完、不进任务中心。

> 与解析器不同：供应商目前只能内置在代码里（`providers_custom/` 外部目录未开放），因此打包版无法热加载新供应商，
> 需要改代码后重新打包。

---

## 7. 输出结构与注意事项

```
输出目录/
├── 中文/                 # 每种语言一个子目录
│   ├── Q001.mp3
│   └── Q003.mp3
├── 中文.tar              # 同语言的 tar：顶层扁平，只含 Q001.mp3 …
├── 英文/
│   └── …
└── 英文.tar
```

- **tar 内为扁平结构**（成员名就是 `<语音ID>.mp3`，不含语言目录），可直接作为语音包交付。
- **空文本条目跳过合成**：不进 zip、不请求 API、不落盘，计入该语言的「缺失」，CLI 退出码变为 `1`。日志提示：
  batch 模式为 `[package] <语言>: 空文本跳过 N 条: Q0xx…`；sync/async 模式为 `空文本跳过合成: Q0xx`。
- **缺失/未知**：`缺失` = 目录里有但没拿到音频；`未知` = 音频里有但目录里没有的语音 ID（正常应为 0）。
  batch 模式的结果 tar 按 `<目录码><语音ID>` 编码归位，某条目在结果包里找不到对应 mp3 即计入该语言缺失并记 warning；
  提取不出编码的成员、编码重复（后者覆盖）、mp3 数据为空都会记 warning（记录在任务记录的「最近错误」里）。
- **结果下载有效期**：MiniMax 任务成功后的音频**约 9 小时内**可下载，超时后任务变「已过期」、只能重新提交（再次消耗配额）。
  CLI 下载即走，基本不受影响；GUI batch 模式把任务留在「待下载」，**超 8 小时未下载任务中心会告警**，请尽快点
  「下载并打包」，并及时把 `语音包/` 目录归档，不要指望隔几天再「补下载」。
- **重跑会覆盖**同名 mp3/tar 并再次消耗配额；只想补跑缺失语言时，用 `--language` 限定范围（GUI 里在语言列表只勾这些语言）。
- **失败的影响范围**：batch 模式一次转换只有 1 个任务，所以任务级失败（`failed` / `expired` / 查询超时 / 下载中断）会让
  本次转换的**全部语言条目**计入缺失——CLI 重跑对应范围即可，GUI 直接在任务中心点「重试」（用词条快照重新提交，不用重新
  选语言）；sync/async 逐条模式下单条失败只记 warning 继续，缺失体现在计数里。
  但**认证失败 / 配额超限（429）属全局错误，会立即终止整批**，先解决 Key/额度问题再重跑。

API 参考：
[创建异步语音合成任务](https://platform.minimaxi.com/docs/api-reference/speech-t2a-async-create) ·
[查询任务状态](https://platform.minimaxi.com/docs/api-reference/speech-t2a-async-query)

---

## 8. 自行打包（免安装 exe）

打包配置：根目录 [`t2s_tool_gui.spec`](t2s_tool_gui.spec) —— PyInstaller **onefile + windowed**
（`runtime_tmpdir=None` 单文件自解压、`console=False` 无黑窗、`upx=False`），产物名 `t2s-voice-tool`。
打包与运行**同一份配置**同时适用于 Windows 和 Linux。

> 只有**构建者**需要 Python 与 PyInstaller；最终用户拿到 exe 即可，什么都不用装。

### Windows 本机

在装有 Python 的 Windows 机器上，于项目根目录双击/命令行执行：

```bat
scripts\build_windows.bat
```

产物：`dist\t2s-voice-tool.exe`（把这一个文件发给用户即可）。

### Linux 本机

```bash
bash scripts/build_linux.sh
```

PySide6 随 `pip install -r requirements.txt` 安装并由 PyInstaller 自动收编，无需系统级 GUI 包（`tkinter` 已不再使用）。

产物：`dist/t2s-voice-tool`（Linux 无 exe 概念，用户加执行权限后直接运行）。
注意 Linux 打包产物**不可**在 Windows 上运行，反之亦然——需要哪个平台就在哪个平台构建。

### GitHub Actions 云端构建（免本机环境）

工作流 [`.github/workflows/build.yml`](.github/workflows/build.yml)：向 `main`/`master` 推送，或在
Actions 页手动 **Run workflow**，即并行跑 `windows-latest` 与 `ubuntu-latest` 两个 job（Python 3.11，
`pip install -r requirements.txt pyinstaller` → `pyinstaller --clean --noconfirm t2s_tool_gui.spec`）。
产物以 artifact 形式挂在每次运行末尾：**Actions → 选这次 run → Artifacts →
`t2s-voice-tool-windows-latest` / `t2s-voice-tool-ubuntu-latest`** 下载，解压 `dist/` 即得可执行文件。
发版时给对应 commit 打 tag，再从该 run 手动下载 artifact 上传到 **Release** 供用户取包。

打包要点：

- 打包前确认 `requirements.txt` 含 `openpyxl`（`xlsx_9lang` 解析器依赖它），spec 已把它写成 hidden import，
  漏装会导致 exe 在「加载目录」时报缺模块；PySide6 由 PyInstaller 自动收编，无需额外配置。
- exe 的「可写目录」就是 exe 所在目录（见 `t2s_tool/paths.py`）：`config.json`、`tasks.json`、`语音包\`、
  `parsers_custom\` 都在这里；若放在 `C:\Program Files\` 这类受保护路径下会写不进去，请放用户可写目录
  （写不进 `tasks.json` 时任务中心无法保存记录，会直接报「无法写入任务记录文件」）。
- 个别杀毒软件会对未签名的 PyInstaller 单文件误报；签名或改用 `--onedir` 目录版可规避，分发给内部用户时说明即可。

---

## 9. 运行测试

```bash
pip install -r requirements-dev.txt && python3 -m pytest tests/ -q
```

测试全部离线（用内置 `fake` 供应商与临时目录），**不联网、不消耗任何 API 配额**，可在 CI 中直接跑。
任务中心相关测试同样离线：查询 / 上传 / 下载接口都被替换成假响应，结果 tar 由测试自己构造。

按模块分工：

| 测试文件 | 覆盖内容 |
|----------|----------|
| `tests/test_config.py` | 默认值（`PROVIDER_DEFAULTS`）、旧版扁平配置自动迁移 |
| `tests/test_parsers.py` | `xlsx_9lang` 解析规则、`detect()` 打分 |
| `tests/test_packager.py` | 逐条落盘 `<语言>/<ID>.mp3`、`<语言>.tar` 扁平结构、全缺失不生成规则 |
| `tests/test_minimax_provider.py` | 供应商 HTTP 层：zip 上传、`text_file_id` 建任务、轮询、结果 tar 按编码解包，及 sync/async 逐条模式 |
| `tests/test_pipeline.py` | 端到端编排（`fake` 供应商）：语言筛选、未知语言/供应商报错、取消、输出目录创建 |
| `tests/test_tasks.py` | **任务中心数据层**：`tasks.json` 读写（原子替换 / 损坏备份）、生命周期状态机（`processing`/`success`/`downloading`/`packaged`/`failed`/`expired`/`lost`）、刷新、下载打包按编码归语言、重试、过期判定 |
| `tests/test_task_controller.py` | **任务中心控制器**：刷新 / 下载打包 / 重试 / 删除的后台排队串行、错误回传、记录变更通知 |
| `tests/test_gui_controller.py` | 批量转换控制器：解析与语言选项、配置读写、启动成功、取消、无显示环境 smoke |
| `tests/test_gui_qt_smoke.py` | PySide6 视图层冒烟（offscreen）：三页签与共享目录加载、语言全选/清空、单条联动与空文本禁用、设置对话框回写 |

### GUI 全流程真实链路自检

```bash
python3 scripts/gui_e2e_check.py
```

以 PySide6 **offscreen** 模式启动真实 GUI 并驱动真实控件（加载目录 → 配置自动加载与「⚙ 设置」保存回写 → 全选语言 →
batch 提交 → 任务中心刷新/下载打包 → mp3/tar 产物校验 → async 模式取消），逐步输出 PASS/FAIL。该脚本走 MiniMax
**真实接口**，消耗约 6 条短文本（2 语言 × 3 条）+ 取消阶段少量提交的配额（工作目录 `/tmp/gui_e2e`）；
适合在改动供应商链路或 GUI 后手工确认端到端可用，不建议挂 CI。

---

## 10. 目录结构

```
.
├── run_gui.py                 # GUI 入口（打包主程序）
├── main.py                    # CLI 入口
├── config.json                # 配置文件（已 gitignore，勿提交真实 Key）
├── tasks.json                 # GUI 任务中心记录（自动生成，与 config.json 同目录；含词条快照，不含 Key）
├── requirements.txt           # 运行依赖（requests、openpyxl、PySide6）
├── t2s_tool_gui.spec          # PyInstaller 打包配置（onefile + windowed）
├── scripts/
│   ├── build_windows.bat      # Windows 本机构建脚本
│   ├── build_linux.sh         # Linux 本机构建脚本
│   └── gui_e2e_check.py       # GUI 全流程真实链路自检（PySide6 offscreen 驱动真实控件）
├── .github/workflows/build.yml# GitHub Actions 双平台云端构建
├── parsers_custom/            # （可选）自定义解析脚本目录，用户自建
├── tests/                     # 离线单测（fake 供应商 + 临时目录），见第 9 节
│   ├── test_single.py         # 「单条转换」离线单测（fake 供应商）
│   └── test_gui_qt_smoke.py   # PySide6 视图层冒烟（offscreen）
└── t2s_tool/
    ├── paths.py               # 冻结感知：exe 旁 vs 项目根（config.json / tasks.json / parsers_custom / 语音包）
    ├── config.py              # 配置加载、旧版迁移、默认值（PROVIDER_DEFAULTS）、保存
    ├── models.py              # VoiceEntry / ParseResult / ProviderResult / LanguageReport / JobReport(submitted_tasks) / CancelToken …
    ├── errors.py              # APIError / ConfigError / ParseError / FileError / CancelledError
    ├── pipeline.py            # 端到端编排：解析 → 合并 zip 单任务 → （submit_only 记录 / 轮询下载）→ 按语言落盘
    ├── tasks.py               # 任务中心数据层与生命周期：TaskRecord + tasks.json 存储 + 刷新/下载打包/重试
    ├── packager.py            # 落盘 <语言>/<ID>.mp3 + <语言>.tar
    ├── parsers/               # base.py(ParserBase) + xlsx_9lang + placeholder + 注册/热加载
    ├── providers/             # base.py(TTSProvider) + minimax（上传/建任务/查询/解包）+ fake + 注册表
    └── gui/                   # app.py(PySide6 视图层：语音目录共享区 + 批量转换/单条转换/任务中心三页签)
                               # controller.py(转换逻辑/后台线程) + task_controller.py(任务中心逻辑/后台线程)
```

新需求一律走 `main.py` / `run_gui.py`。

---

## 11. 常见问题

### API 配额不足

**表现**：报「配额超限」「quota」「limit」或 429。
**解决**：登录 MiniMax 控制台确认额度；等待重置或申请提额；用 `--language` 拆小范围重跑；batch 模式**一次转换只创建
1 个任务**、请求数最少（按字符量 `usage_characters` 计费，拆任务并不省额度），若用 sync/async 逐条模式则调低
`concurrency` 减少请求频率；先用 `--provider fake` 验证目录格式没问题，再动真格消耗配额。

### 合成速度 / 如何提速

默认 `tts_mode: "batch"`：**9 国 × 118 条 = 一个 zip、只创建 1 个任务**，服务端整批处理约**数分钟**（等待即可，
完成后逐条落盘）。语言越多、条目越多只是让这一个任务更久，不会变成多次任务。小批量试跑或想逐条快速出结果，可临时把
`tts_mode` 改为 `sync`（实测短文本约 **0.7 秒/条**，并发 5，长文本自动回退异步链路）；调大 `concurrency` 可提速逐条
模式，但过高可能触发限流——**429 会整批快速终止**，免费/限流账户建议 `2`~`3` 起步，稳定后再逐步上调。

### 认证失败

**表现**：`认证失败，API Key 无效或已过期`、`401 Unauthorized`、`403 访问被拒绝`。
**解决**：确认 `providers.minimax.api_key` 正确无多余空格；Key 未过期且开通了 TTS 权限；
打包版检查的是 **exe 同目录**那份 `config.json`（改完重启 GUI 生效，或直接在「⚙ 设置」对话框里改并保存）；换 `model` 试一下权限差异。

### 网络错误

**表现**：`网络连接失败`、`请求失败`、`请求超时`。
**解决**：检查外网可达 `api.minimaxi.com`、代理/防火墙设置；把 `request_timeout`/`download_timeout`
调大；网络抖动时直接重跑对应语言即可（GUI batch 任务已提交过的，到「任务中心」点「刷新」看状态，
提交阶段就失败的点「重试」）。

### 任务查询超时

**表现**：`轮询: 任务查询超时（已等待 1200 秒）`（CLI batch / sync / async 模式）。
**解决**：`poll_timeout` 按**单个任务**计，默认 `1200` 秒（20 分钟）。batch 模式这一次任务要整批转换**所有语言**的条目
（条目越多、文本越长等待越久），全量跑时可调到 `1800`~`3600`；（逐条模式下）适当调低 `concurrency` 缓解服务端排队；
`poll_interval` 默认 10 秒，等待久时可适当放大以减少请求数。CLI 超时后任务其实还在服务端跑，可稍后重跑，或改用 GUI：
**提交即返 + 任务中心多次「刷新」**，不受 `poll_timeout` 约束。MiniMax 服务繁忙时稍后再刷新。

### 任务处理失败 / 结果异常

**表现**：`任务处理失败（API 返回 failed 状态）`、`任务已过期`、或完成后大量条目计入「缺失」。
**解决**：检查文本是否含超长内容或异常字符；缩短文本、换 `voice_id` 重试。batch 模式一次转换只有 1 个任务，任务级失败
会让本次全部条目计入缺失：CLI 重跑对应语言；GUI 直接在「任务中心」选中该记录点「**重试**」——用记录里的词条快照重新
提交（新 `task_id`、尝试次数 +1），不用重新选语言、不用重新解析目录。

### 任务中心相关

- **提交后列表里没有记录**：任务记录只在 **GUI** 的 batch 模式写入（`tasks.json`）；CLI 跑批不落记录，`fake` 供应商
  与非 batch 模式也不落。打包版检查 **exe 同目录**的 `tasks.json` 是否可写（放 `Program Files` 会写失败并报错）。
- **一直是「处理中」**：点「刷新」触发一次查询（不选中记录即刷新全部非终态）；勾选「自动刷新（每 60 秒）」交给它自己刷。刷新是**每条记录一次查询
  请求**，任务很多时稍等片刻。
- **「下载并打包」点了没反应 / 报「任务正在下载打包中」**：同一记录的操作在后台串行，等前一个完成；只有「待下载」
  状态的记录可以下载打包。
- **下载中途失败**：状态自动回滚为「待下载」并记「最近错误」，再点一次「下载并打包」即可（不必重新提交）。
- **状态显示「已过期」/「记录丢失」**：结果文件已不可下载（约 9 小时有效期），点「**重试**」重新提交并再次消耗配额。
- **「删除记录」后音频不见了**：删除只清 `tasks.json` 里的条目，不碰已落盘的 `<语言>/<语音ID>.mp3` 与 `<语言>.tar`；
  音频不在多半是当时没点「下载并打包」，或输出目录选错了。
- **重复下载会怎样**：同名 mp3/tar 会被覆盖（内容一致），不额外消耗配额（音频已在本地结果包里）。

### 解析相关

**表现**：`没有解析器能识别该文件`、`未找到任何语言列`、日志里大量「语音ID非法，已跳过」。
**解决**：确认首列表头是语言名、首列数据是 `Q` 开头数字 ID；确认没有把说明行/合并行放在首行；
换格式就写自定义解析器丢进 `parsers_custom/`（[第 5 节](#5-语音目录格式与解析器扩展)），或在 GUI 下拉框手工指定解析器。

### 打包 / exe 相关

- **双击没反应 / 首次启动慢**：onefile 需自解压，等几秒；被杀软拦截时加信任或改用目录版。
- **exe 报 `No module named 'openpyxl'`**：构建环境未装 `openpyxl`，补进 `requirements.txt` 后重新打包。
- **GUI 打不开**：确认 `pip install -r requirements.txt` 已装好 PySide6（`PySide6>=6.5`）后重试；
  Linux 桌面**源码运行**若报 `Could not load the Qt platform plugin "xcb"` 或启动即崩溃，是缺 `libxcb-cursor0`
  （Qt 6.5+ 依赖），运行 `sudo apt install libxcb-cursor0` 即可（打包版 exe 已内置该库，不受影响；
  启动前会自动检查并提示对应发行版的命令）；
  Linux 无显示服务器的纯命令行环境只适合跑测试/自检脚本（它们设 `QT_QPA_PLATFORM=offscreen`）。
- **配置写了但不生效**：确认改的是 exe 同目录的 `config.json`，改完重启 GUI（启动自动加载；或用「⚙ 设置」改，保存即写回）。
- **任务中心列表是空的**：任务记录读的是 **exe 同目录的 `tasks.json`**；换文件夹存放 exe 等于换了一份记录
  （把旧 `tasks.json` 一起拷过去即可延续列表）。
- **输出目录写入失败**：exe 所在目录不可写（如 `Program Files`）或磁盘满，换到用户目录。
- **Actions 里没有 artifact**：只有构建成功的 job 才上传产物，先看该 job 日志中的 PyInstaller 报错。

---

## 许可与致谢

文本转语音能力由 [MiniMax 开放平台](https://platform.minimaxi.com/) 提供。
