# Galgame MCP

一个面向 Windows 视觉小说/galgame 的本地 MCP 服务。它把一次游玩拆成三个可恢复层：

1. 观察：截图、OCR 或 Codex 视觉理解得到当前画面。
2. 结构化：记录场景、角色、台词、选项、路线变量、备注和输入动作。
3. 执行：激活游戏窗口、发送按键/鼠标输入，并把每一步写回同一份会话数据。

当前版本优先完成了“文本处理与会话数据核心”，同时提供 Windows 全屏桌面截图、可选的完整窗口后台捕获、Windows 原生 OCR、窗口激活、按键和点击接口。截图始终是完整主屏或完整游戏窗口，不接受裁切区域，也不会把图片默认返回给 Codex。

项目采用 MIT 许可证。当前仓库发布的是通用的 MCP 数据与工具接口；桌面捕获、窗口后台读取、键鼠输入和 Windows 原生 OCR 属于 Windows 平台后端。

## 安装

在项目目录执行：

```powershell
py -3 -m pip install -e .
```

基础安装只要求 MCP Python SDK；截图使用 Windows 原生 GDI。建议 Windows 上额外安装本地 OCR：

```powershell
py -3 -m pip install -e ".[windows-ocr]"
```

该可选依赖调用 Windows 自带 OCR，OCR 和文本解析均在本机完成；未安装时仍可使用手动记录，或显式 `include_image=true` 让 Codex 视觉兜底。

开发和打包检查可以安装：

```powershell
py -3 -m pip install -e ".[dev,windows-ocr]"
```

`mcp` 依赖暂时限制在 1.x，是为了保持当前 `FastMCP` 接口兼容；迁移到 MCP SDK 2.x 后再放宽版本上限。Tesseract 只是可选的外部系统程序，不会随 Python 包自动安装。

为了提高连续游玩的响应速度，截图 PNG 默认使用低压缩级别；如果更在意会话截图占用的磁盘空间，可以设置 `GALGAME_MCP_PNG_COMPRESSION=6`，代价是每次捕获更慢。

## 连接 Codex

官方 OpenAI 文档支持把本地 stdio MCP 配置在 `config.toml`，也支持用 `codex mcp add` 添加。安装本项目后，可以在本目录执行：

```powershell
codex mcp add galgame -- py -3 -m galgame_mcp.server
codex mcp list
```

如果 Codex 启动时找不到当前目录中的包，改用绝对 Python 路径，并在 `config.toml` 中指定 `cwd`：

```toml
[mcp_servers.galgame]
command = "C:\\Users\\ASUS\\AppData\\Local\\Programs\\Python\\Python312\\python.exe"
args = ["-m", "galgame_mcp.server"]
cwd = "D:\\codex_project\\auto_play_galgame"
startup_timeout_sec = 20
tool_timeout_sec = 60
default_tools_approval_mode = "prompt"

[mcp_servers.galgame.env]
GALGAME_MCP_DATA_DIR = "D:\\codex_project\\auto_play_galgame\\.galgame_sessions"
```

Python 路径按实际安装位置替换。也可以不设置 `GALGAME_MCP_DATA_DIR`，默认写入项目下的 `.galgame_sessions`。

示例配置中的 `enabled_tools` 是节省 token 的高层工具白名单；它保留自动游玩和精简上下文接口，省略独立的原始截图/OCR/手动记录工具。需要低层调试时再从白名单中补回对应工具。

## 文本处理的最小工作流

连接 MCP 后，Codex 可以按下面的顺序使用：

```text
start_session(game_name="某视觉小说")
capture_screen()
record_observation(
  scene_id="prologue-001",
  speaker="角色A",
  text="今天也要加油。",
  choices=["去教室", "回家"],
  source="codex-vision",
  confidence=0.98,
)
get_codex_context()
record_choice(options=["去教室", "回家"], selected_index=1, source="codex")
press_key("ENTER")
wait(1.0)
capture_screen()
```

`record_observation` 会把一次画面理解拆成可检索事件；`get_codex_context` 返回 JSON 结构和 Markdown；`export_session` 可以生成导出的 JSON、`codex_context.md` 或 `timeline.jsonl`。每个会话都位于：

如果先拿到的是整段 OCR/剪贴板文本，可以直接使用：

```text
parse_text(raw_text="[小葵]\n今日は一緒に帰らない？\n1. はい\n2. いいえ")
record_parsed_text(raw_text="...", screenshot_path="...")
```

解析器会保守地返回 `speaker`、`dialogue`、`choices`、`choice_records` 和 `unparsed_lines`；不确定的行不会被静默丢弃。

```text
.galgame_sessions/<session_id>/
  session.json
  frames/*.png
  codex_context.md       # 调用 export_session 后生成
  timeline.jsonl         # 调用 export_session 后生成
```

## 当前工具

- 数据：`start_session`、`list_sessions`、`set_active_session`、`get_current_state`、`record_scene`、`record_observation`、`parse_text`、`record_parsed_text`、`record_dialogue`、`record_choice`、`set_story_variable`、`add_note`、`search_story`、`get_codex_context`、`export_session`、`close_session`
- 观察：`capture_screen`、`ocr_image`、`observe_game`
- 执行：`attach_game`、`focus_game_window`、`press_key`、`click_screen`、`wait`、`advance_game`、`select_choice`
- 资源：`galgame://active/context`

输入工具只操作当前 Windows 前台窗口；高层自动游玩工具默认使用 `capture_mode="auto"`：已绑定游戏窗口时读取完整窗口，未绑定时读取完整全屏桌面。需要强制桌面捕获时传 `capture_mode="desktop"`；需要游戏留在后台读取时可使用 `observe_game(capture_mode="window", focus_before_capture=false)`，它捕获游戏自己的完整窗口，不裁切、不固定屏幕坐标。`PrintWindow` 对当前《千恋＊万花》实测可以读取被 Codex 窗口遮挡的画面；某些独占 GPU 或最小化游戏仍可能返回黑帧。`press_key` 和 `click_screen` 默认会把动作写入时间线。

更接近无人值守的通用流程是：

```text
start_session(game_name="某视觉小说")
attach_game(window_title="游戏窗口标题", advance_key="SPACE", choice_mode="number")
observe_game()                   # 本地截图 + Windows OCR + 解析；默认只返回 processed_text
get_codex_context()             # Codex 根据截图和上下文判断是否推进或选项
advance_game()                  # 普通对白；默认等待 0.15 秒
select_choice(option_index=2, choice_id="choice_...")  # 选项；默认等待 0.25 秒
observe_game()
```

追求最快推进时可以传 `advance_game(wait_seconds=0)`；如果某个游戏的转场或文字动画还没有完成，再把等待调高到 `0.2`–`0.6` 秒。

默认 `observe_game`、`advance_game` 和 `select_choice` 都会把截图留在会话目录，只向 Codex 返回 OCR 状态、`processed_text`、选项和必要动作结果；重复识别到同一对白时不会重复写入剧情事件。`get_codex_context` 和 `galgame://active/context` 默认返回精简上下文，不包含原始 OCR、截图内容或截图事件。只有 OCR 失败或确实需要视觉判断时，才显式传 `include_image=true`。

MCP 本身不包含一个外部大模型，因此“看图并决定路线”由连接它的 Codex 完成；服务端负责窗口、截图、本地 OCR、文本解析、输入和可恢复记录。连接后 Codex 可以连续调用这些工具，不需要你逐步手动点击。

## 自动游玩路线

下一阶段可以增加按游戏引擎/游戏的适配器：

- Ren'Py：读取窗口画面并识别文本框/选项区域。
- TyranoScript/KAG：识别底部文本框和纵向选项。
- Unity/自研引擎：以截图 + OCR/视觉判断为通用后端。
- 决策策略：优先使用当前 `get_codex_context`，再将选择结果写入 `record_choice`，最后调用 `press_key` 或 `click_screen`。
- 启动与恢复：增加显式的 `launch_game`、`detect_game_state` 和 `play_until` 工具；启动游戏和选择路线属于有外部副作用的动作，建议按工具单独配置审批。

这里没有把任意可执行文件启动器偷偷放进核心数据层，避免一接入 MCP 就意外启动或操作错误程序；指定游戏的启动和状态适配应作为下一层配置实现。

## 开发检查

```powershell
py -3 -m pip install -e ".[dev,windows-ocr]"
py -3 -m compileall -q src
py -3 -m unittest discover -s tests -v
py -3 -m build
```

## 许可证

本项目使用 [MIT License](LICENSE)。项目不包含任何游戏本体、游戏资源、存档或截图；使用者需要自行确认目标游戏和相关数据的版权、隐私及自动化使用边界。
