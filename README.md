# Galgame MCP

一个面向 Windows 视觉小说/galgame 的本地 MCP 服务。它把一次游玩拆成三个可恢复层：

1. 观察：截图、OCR 或 Codex 视觉理解得到当前画面。
2. 结构化：记录场景、角色、台词、选项、路线变量、备注和输入动作。
3. 执行：激活游戏窗口、发送按键/鼠标输入，并把每一步写回同一份会话数据。

当前版本优先完成了“文本处理与会话数据核心”，同时提供 Windows 全屏桌面截图、可选的完整窗口后台捕获、Windows 原生 OCR、窗口激活、按键和点击接口。截图始终是完整主屏或完整游戏窗口，不接受裁切区域，也不会把图片默认返回给 Codex。

Windows 后端启动时会启用 Per-Monitor DPI 感知，统一截图、窗口矩形和输入坐标的物理像素；这避免高分辨率缩放桌面被错误地当成较小的逻辑分辨率。

项目采用 MIT 许可证。当前仓库发布的是通用的 MCP 数据与工具接口；桌面捕获、窗口后台读取、键鼠输入和 Windows 原生 OCR 属于 Windows 平台后端。

## 当前验证范围

截至当前版本，桌面捕获、后台窗口读取、Windows OCR、后台点击、选项识别和自动推进流程只在 Windows 上的《千恋＊万花》进行过实际运行测试。其他视觉小说尚未完成实机兼容性验证；这里的通用接口和配置能力是设计目标，不构成对其他游戏的兼容承诺。更换游戏时，请先按“首次使用、换游戏与换 Agent”中的冒烟流程逐项确认。

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

如果希望启用“完整窗口 Windows OCR 失败后”的 RapidOCR 保底，再安装：

```powershell
py -3 -m pip install -e ".[windows-ocr,rapidocr]"
```

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
command = "C:\\Path\\To\\Python\\python.exe"
args = ["-m", "galgame_mcp.server"]
# Optional per-server override; it takes precedence over GALGAME_MCP_DATA_DIR.
# args = ["-m", "galgame_mcp.server", "--data-dir", "D:\\path\\to\\vn-data"]
cwd = "D:\\path\\to\\auto_play_galgame"
startup_timeout_sec = 20
tool_timeout_sec = 120
default_tools_approval_mode = "prompt"

[mcp_servers.galgame.env]
GALGAME_MCP_DATA_DIR = "D:\\path\\to\\auto_play_galgame\\.galgame_sessions"
```

Python 路径按实际安装位置替换。数据目录优先级为：命令行 `--data-dir`、环境变量
`GALGAME_MCP_DATA_DIR`、当前工作目录下的 `.galgame_sessions`。因此可以让多个
项目各自通过 `cwd` 使用自己的默认目录，也可以把所有会话集中到一个独立目录；
路径不会写死在源码中。连接后可调用 `get_storage_info` 查看 MCP 实际解析到的路径。

不要在这里设置 `enabled_tools` 静态白名单：MCP 会自动暴露源码中注册的完整工具，避免新增接口被 Codex 客户端侧白名单挡住。不同游戏的差异应通过当前会话的 `configure_game_layout` 和 `configure_game_actions` 配置；这样换游戏无需修改全局配置。

## 首次使用、换游戏与换 Agent

建议把下面流程作为每个新对话的固定开场。它不会依赖某个游戏标题或某个
Agent 的记忆，游戏差异都保存在会话 profile 中。

1. **安装并确认连接。** 在项目目录执行安装命令，然后用
   `codex mcp list` 确认 `galgame` 已连接。第一次连接后重启 Codex，确保新
   工具表已经加载；以后新增工具不需要再编辑 `enabled_tools`。
2. **决定新建还是恢复。** 新存档使用
   `start_session(game_name="...")`；继续旧存档先调用 `list_sessions()`，再用
   `set_active_session(session_id="...")`。不要为了“试一下”重新创建同名会话，
   否则会把剧情记录分成两条路线。
3. **绑定窗口但不要先抢焦点。** 调用
   `attach_game(window_title="...", advance_key="SPACE", choice_mode="click")`。
   默认只校验窗口并记录矩形，不会把游戏切到前台；后台模式要求窗口仍然存在，
   最小化或独占渲染可能无法被 `PrintWindow` 读取。窗口模式最容易验证，且不会
   改变原始截图尺寸。
4. **先做一次完整基准观察。** 调用
   `observe_game(capture_mode="window", include_image=false)`，检查返回的
   `width`、`height`、`window` 和 `capture_scope`。重启 MCP 后第一次应为
   `window_full`；后续在已经确认 profile 的情况下才可能是
   `window_dialogue_region`。区域帧是额外的快速 OCR 图片，不是把完整窗口压缩或
   移位；需要视觉核对时传 `include_image=true`，会返回完整窗口。
5. **按实际画面配置布局。** 只有在基准截图确认位置后，才调用
   `configure_game_layout` 写入 `dialogue_region`、可选的 `speaker_region` 和
   `choice_region`。姓名/对白符号通过 profile 的 marker 数组提供，不要把某个
   游戏的括号写进代码；看不清符号时宁可暂时留空，让通用解析保留
   `unparsed_lines` 和 `noise_flags`。
6. **配置游戏动作和等待策略。** 用 `configure_game_actions` 描述本游戏的
   `next_line`、`hide_ui`、`return_game` 等动作；用下面的
   `configure_game_timing` 选择固定等待或文本稳定等待。动作名称只是会话数据，
   换游戏时重新配置，不修改全局 MCP。
7. **做小范围冒烟测试。** 先观察一帧，再调用一次
   `advance_game(background=true)`，确认返回的
   `input_verification.method` 是 `bottom_textbox`，并检查
   `changed`、`capture_scope` 和 `processed_text`。然后用较小的
   `play_until_choice(max_steps=3)` 验证 OCR、推进、设置页恢复和选项停止；出现
   `choice_detected` 后由 Codex 选择，不要在未读图时连续发送额外点击。
8. **进入无人值守循环。** 冒烟测试通过后调用 `play_until_choice`。它在本地
   捕获、OCR、解析、去重、记录和推进，默认不设步数上限；达到真实选项、
   OCR/输入安全错误或 `compaction_due` 时才把汇总 JSON 返回给 Codex。
   返回 `dialogue_not_detected`、
   `timing_settle_timeout` 或 `ocr_unavailable` 时先读附带的完整窗口图，再决定
   一次人工接管动作；不要用 ESC、Ctrl 或空格猜测游戏状态。
9. **交给新的对话或 Agent。** 新 Agent 先读本 README，然后调用
   `get_current_state()`、`get_codex_context()` 和 `get_compaction_status()`；
   从返回的 `session_id`、`game` profile、当前对白和省流摘要继续。已有会话不要
   再 `start_session`，也不要删除 `events.jsonl` 或 `compactions/`；如果只是换
   对话，数据和窗口绑定仍在本机。

### 点击后等待与打字机动画

当前默认是 `strategy="fixed"`，默认点击后只等待 `0.05` 秒；这是为已经关闭
打字机动画、追求速度的游戏保留的快速路径。新游戏如果有打字机动画，首次测试后
应改成 `strategy="text_hash"`：MCP 会在本地轮询底部对白框，不发送额外输入，
要求文本先不同于点击前内容，并连续若干次保持同一个哈希，才把这一帧交给正常的
剧情处理。

```text
configure_game_timing(profile={
  "strategy": "text_hash",
  "post_click_wait_seconds": 0.05,
  "settle_timeout_seconds": 5.0,
  "settle_poll_seconds": 0.15,
  "stable_samples": 3,
  "require_text_change": true,
  "transition_wait_seconds": 3.0,
  "transition_accelerate": false,
  "transition_accelerate_delay_seconds": 0.6,
  "transition_probe_interval_seconds": 0.2
})
```

`post_click_wait_seconds` 是点击后的第一次采样延迟，不是整个动画的硬性结束
时间；`settle_poll_seconds`、`stable_samples` 和 `settle_timeout_seconds` 才
共同决定动画检测的安全程度。若动画在超时前没有形成“文本变化且稳定”的状态，
`play_until_choice` 会以 `timing_settle_timeout` 停止，并返回最后一帧，绝不会把
未确认完成的动画继续点击过去。若游戏只是短暂转场而不是打字机，可以继续使用
`fixed`，并用 `transition_wait_seconds` 给空对白框一个有界的本地重试窗口。

某些游戏允许在转场中再次点击来缩短淡入淡出。这个行为默认关闭，只有在确认
该游戏确实支持后才配置 `transition_accelerate=true`。MCP 会在点击前保存一张完整
窗口帧，点击后最多检查三张完整窗口帧：必须连续没有对白、选项、未知文字或设置
界面，并且相对点击前的画面差异达到本地阈值，才判定为高置信度转场。随后等待
`transition_accelerate_delay_seconds`，最多发送一次同类型推进输入。画面稳定但 OCR
为空、Pillow 不可用、或者出现任何可疑文字时，都不会发送这次额外输入，而是按
`ocr_uncertain`/普通转场等待路径交给 Codex 复核。`text_hash` 策略不会启用转场加速，
因为它本身已经负责等待打字机动画完成。

单次试验也可以临时覆盖策略，例如
`advance_game(wait_strategy="text_hash", wait_seconds=0.05)`；稳定参数需要通过
`configure_game_timing` 保存，便于后续调用和新的 Agent 复用。profile 的默认值不会拖慢
已经关闭动画的旧游戏。

字段范围由 MCP 本地校验：`post_click_wait_seconds` 为 0–10 秒，
`transition_wait_seconds` 为 0–10 秒，`settle_timeout_seconds` 为 0–30 秒，
`settle_poll_seconds` 为 0.02–2 秒，`stable_samples` 为 1–10；超出范围会被
限制到边界，`transition_accelerate_delay_seconds` 为 0.1–3 秒，
`transition_probe_interval_seconds` 为 0.05–2 秒，类型错误会直接报错。传
`configure_game_timing(profile={})` 可清除
自定义 profile，恢复快速的 `fixed` 默认值。

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
parse_text(raw_text="小葵：今日は一緒に帰らない？\n1. はい\n2. いいえ")
record_parsed_text(raw_text="...", screenshot_path="...")
```

解析器会保守地返回 `speaker`、`dialogue`、`choices`、`choice_records`、`unparsed_lines` 和非破坏性的 `noise_flags`；不确定的行不会被静默丢弃。噪声标记只提示分隔线/UI 残留、替换字符、字符间异常空格、未闭合 marker 或符号过密等情况，`raw_text` 始终保留。

固定布局游戏的姓名/对白符号不在代码中预设；调用 `configure_game_layout` 后，解析器才会按该会话 profile 的 marker 和位置分类。未配置 profile 时只使用明确的 `name: dialogue`、数字选项等保守规则，Windows OCR 在姓名或中日韩对白字符之间插入的空格仍会被清理。

```text
.galgame_sessions/<session_id>/
  session.json
  events.jsonl             # 追加式原始事件日志；长时间运行不会反复重写 session.json
  frames/*.png
  compactions/segment_*.json  # Codex 确认后的省流历史
  codex_context.md       # 调用 export_session 后生成
  timeline.jsonl         # 调用 export_session 后生成
```

## 当前工具

- 数据：`start_session`、`list_sessions`、`get_storage_info`、`set_active_session`、`get_current_state`、`record_scene`、`record_observation`、`parse_text`、`record_parsed_text`、`record_dialogue`、`record_choice`、`set_story_variable`、`add_note`、`search_story`、`get_codex_context`、`get_compaction_status`、`get_compaction_request`、`save_compaction`、`export_session`、`close_session`
- 配置：`configure_game_layout`、`configure_game_actions`、`configure_game_timing`
- 观察：`capture_screen`、`ocr_image`、`observe_game`
- 执行：`attach_game`、`focus_game_window`、`press_key`、`background_press_key`、`hold_key`、`click_screen`、`background_click`、`background_scroll`、`perform_game_action`、`wait`、`advance_game`、`play_until_choice`、`select_choice`
- 资源：`galgame://active/context`

常规输入工具只操作当前 Windows 前台窗口；高层自动游玩工具默认使用 `capture_mode="auto"`：已绑定游戏窗口时，进程重启后的第一次观察保存完整窗口，后续已知对白框优先捕获文本框区域；未绑定时读取完整全屏桌面。绑定窗口的 `attach_game` 默认只验证并记录窗口，不会切到前台；只有显式传 `focus_window=true` 才会激活它。截图和 OCR 默认也不聚焦窗口；需要明确激活时才传 `focus_before_capture=true`。需要强制桌面捕获时传 `capture_mode="desktop"`；需要游戏留在后台读取时可使用 `observe_game(capture_mode="window")`，它用完整窗口建立坐标基准，之后可返回 `capture_scope="window_dialogue_region"` 的小区域帧。区域帧不是把完整截图压缩或移动，而是额外保存的快速 OCR 帧；区域捕获为空、被遮挡或 OCR 无有效文字时会自动回退 `capture_scope="window_full"`。`include_image=true` 始终请求完整窗口图像。`PrintWindow` 对当前《千恋＊万花》实测可以读取被 Codex 窗口遮挡的画面；某些独占 GPU 或最小化游戏仍可能返回黑帧。发送输入前的聚焦只在明确选择前台路径时执行，正常、最大化和全屏窗口不会被 `SW_RESTORE` 改成普通窗口；如果 Windows 拒绝后台进程抢焦点，MCP 会临时连接输入队列后立即解除。`press_key`、`hold_key` 和 `click_screen` 默认会把动作写入时间线；鼠标点击会先发一个绝对坐标移动事件，再发按下/抬起事件，并检查每个 `SendInput` 的返回值，注入失败会直接报错。`advance_game` 还会在本地比较输入前后的底部文本框 OCR，只返回 `input_verification` 摘要，不把场景/立绘变化当成推进成功。方向键支持 `UP`/`DOWN`/`LEFT`/`RIGHT` 及 `ARROWUP`/`ARROWDOWN` 等别名；本游戏的右键用于切换 UI 显示状态，`ESC` 只用于隐藏/显示 UI，自动恢复设置页永远不会发送它。

如果不希望游戏抢到前台，可使用 `background_press_key`、`background_click`、`background_scroll`；`advance_game`、`play_until_choice` 和 `select_choice` 默认使用后台窗口消息，需要前台输入时显式传 `background=false`。底层 `background_*` 接口默认用 `delivery="post"` 通过 `PostMessageW` 排队；高层自动游玩工具的后台输入默认用 `background_input_method="send"`，通过带超时的 `SendMessageTimeoutW` 直接调用窗口过程，实测对本游戏更可靠。两种方式都不调用 `SetForegroundWindow`、不移动真实鼠标；`queued=true`/`delivered=true` 只代表系统层结果，不代表游戏一定执行了动作。只读 Win32 消息的引擎适合这几条路径；Raw Input、DirectInput、部分 Unity/独占渲染输入路径可能忽略它们，此时应显式回退到前台 `SendInput` 或游戏专用适配器。窗口消息点击和滚轮的坐标使用屏幕坐标，MCP 会在本地转换鼠标移动消息的客户区坐标。`click_screen(input_method="touch")` 还提供显式 Windows 触摸注入备用路径。后台模式下设置页恢复也使用窗口消息点击；如果未找到明确“回到游戏/返回游戏”按钮，仍不会猜测输入。

完整窗口 OCR 先正常使用 Windows OCR。已配置 `dialogue_region` 的游戏会先走快速对白区域；快速区域为空、只有人物名或只有 VOICE/AUTO 等界面残留时，MCP 会捕获完整窗口并再次使用 Windows OCR。只有完整窗口 Windows OCR 仍没有可用剧情文本时，才会在同一张完整窗口截图上调用可选的 RapidOCR PP-OCRv6-small ONNX 后端。RapidOCR 只作为识别失败保底，不参与快速区域的正常路径，也不再执行旧的 2 倍 focused OCR。两个后端的执行状态、可用性、解析是否足以作为剧情文本和耗时会记录在 `ocr_backends`；RapidOCR 成功且解析出对白/选项时才替换当前结果。仍无法确认时，MCP 会保存并返回截图，标记 `ocr_uncertain`，由 Codex 进行一次视觉复核，自动游玩在此处停止。不会做全屏候选搜索、对比度增强或多轮重试。

RapidOCR 是可选后端，不改变现有 Windows OCR 返回结构。安装：

```powershell
py -3 -m pip install -e ".[windows-ocr,rapidocr]"
```

`rapidocr>=3.9` 默认使用 PP-OCRv6 small 检测/识别模型；本项目额外安装 `onnxruntime`，保持推理在本地 CPU 完成。首次 Windows OCR 前会预加载 ONNX Runtime 的本地 DLL，但不会初始化 RapidOCR 模型，因此正常 Windows OCR 仍保持快速；只有保底真正触发时才承担 RapidOCR 的模型初始化和推理耗时。`execution_success` 表示后端是否完成了一次调用，`usable` 表示是否返回了文本或区域，`story_usable` 才表示当前解析结果是否足以作为剧情文本。后端可用但返回空结果时不会被误报成执行失败。

更接近无人值守的通用流程是：

```text
start_session(game_name="某视觉小说")
attach_game(window_title="游戏窗口标题", advance_key="SPACE", advance_hold_seconds=0, choice_mode="number")
# 每个游戏单独配置对白框、姓名框、选项区和 OCR 符号；以下符号仅是示例占位符
configure_game_layout(profile={
  "dialogue_region": {"x": 0.1, "y": 0.7, "width": 0.8, "height": 0.25, "coordinate_space": "normalized"},
  "speaker_region": {"x": 0, "y": 0, "width": 1, "height": 0.4, "coordinate_space": "dialogue_region"},
  "choice_region": {"x": 0.2, "y": 0.2, "width": 0.6, "height": 0.5, "coordinate_space": "normalized"},
  "speaker_markers": [{"open": "<NAME>", "close": "</NAME>", "allow_unclosed": true}],
  "dialogue_markers": [{"open": "<TEXT>", "close": "</TEXT>"}],
  "choice_layout": "vertical"
})
configure_game_actions(actions={
  "hide_ui": {"kind": "click", "target": "window_center", "button": "right", "delivery": "send"},
  "return_game": {"kind": "click", "target": "window_normalized", "x": 0.5, "y": 0.85, "delivery": "send"},
  "next_line": {"kind": "click", "target": "window_center", "button": "left", "delivery": "send"},
  "page_down": {"kind": "scroll", "direction": "down", "delivery": "post"}
})
observe_game()                   # 本地截图 + Windows OCR + 解析；默认只返回 processed_text
get_codex_context()             # Codex 根据截图和上下文判断是否推进或选项
advance_game()                  # 默认后台推进；返回底部文本框 input_verification
perform_game_action(action="next_line")
select_choice(option_index=2, choice_id="choice_...")  # 默认后台选项；等待 0.25 秒
observe_game()
```

`configure_game_actions` 是跨游戏的动作适配层：profile 只描述 `click`、`key`、`scroll`、`hold`、`wait`、`focus` 六类安全动作，不写入游戏标题专用代码。点击可以使用屏幕坐标，也可以使用窗口中心或 `window_normalized` 的 0 到 1 相对坐标；`delivery` 支持 `post` 和 `send`。之后调用 `perform_game_action` 执行命名动作，`parameters` 可以在单次调用中覆盖 profile 的按键、方向、坐标或等待时间。绑定窗口时 click/key/scroll 默认走后台消息，hold/focus/wait 默认保持前台或本地执行；需要改变默认行为时显式传 `background`。`hide_ui`、`return_game` 等名称只是 profile 示例，不会被代码写死，换游戏只需重新配置映射。`advance_game` 等高层工具仍保留，适合默认推进流程。

### 分段剧情压缩

会话默认在检查点与 `events.jsonl` 原始日志合计达到 256 KB 后提示压缩；也可以通过环境变量 `GALGAME_MCP_COMPACTION_THRESHOLD_BYTES` 调整阈值。`get_codex_context` 会同时返回 `compaction.summary_due`、已经生成的 `compacted_summaries` 和尚未压缩的近期事件。达到阈值后，Codex 应按下面的顺序工作：

```text
get_compaction_request()       # MCP 只返回一个有界的原始事件段和 summary_contract
# Codex 生成详细结构化 summary，至少包含 story_summary
save_compaction(request_id="compact_...", summary={
  "story_summary": "...",
  "key_facts": [], "characters": [], "choices": [], "decisions": [],
  "unresolved_threads": [], "important_quotes": [], "ocr_uncertainties": [],
  "route_implications": [], "variables": {}, "last_known_state": {}, "loss_notes": []
})
```

MCP 会用请求时的事件数量、序号范围和 SHA-256 校验原始前缀没有变化，先把总结写入 `compactions/segment_*.json`，再从活动会话和 `events.jsonl` 中清除这段原始事件；校验失败时不会删除任何数据。总结提交成功后，MCP 还会删除不再被活动事件、当前状态或省流总结引用的 `frames/` 原始截图，并在返回值的 `raw_artifacts` 中报告清理数量；仍被未压缩事件引用的截图会保留。省流文件保留剧情摘要、人物、选项与实际决定、路线变量、未解决伏笔、重要短句和 OCR 不确定性，后续 `get_codex_context` 会把它与新产生的 JSON 事件一起返回。未解决选项会保护其原始事件，不会被压缩切断。该流程只由 Codex 负责语义总结，MCP 负责阈值、完整性校验和可恢复落盘；不会把未经模型确认的摘要当成事实。

后台游玩可以在绑定后使用：

```text
observe_game(capture_mode="window", focus_before_capture=false)
advance_game(background=true, wait_seconds=0.05)
select_choice(option_index=2, background=true)
```

后台 `advance_game` 直接向窗口中心发送后台左键，不再发送后台空格；底部文本框 OCR 只负责验证点击后的文本是否变化。这样兼容只接受鼠标消息的视觉小说，也避免无效键盘消息拖慢推进。后台 `select_choice` 在当前会话配置了 `choice_region` 时，会先对完整窗口做一次本地 OCR，用选项文字的 bounding box 计算屏幕坐标并发送窗口消息点击；因此不要求游戏支持数字键，也不移动真实鼠标。找不到选项坐标时才回退到配置的数字键模式。显式 `mode="click"` 可以省略 `x`、`y` 让同一套 OCR 定位生效；未配置 `choice_region` 时仍需传入坐标或使用数字/方向键模式。

如果希望 Codex 只在需要决策时介入，可以使用：

```text
play_until_choice(background=true, wait_seconds=0.05, transition_wait_seconds=1.2)
```

该工具在 MCP 本地循环捕获、OCR、解析并保存每条对白，然后自动推进；默认停止条件是识别到真实选项、OCR/输入无法安全确认，或原始事件存储达到压缩阈值。达到 `compaction_due` 后，Codex 应先调用 `get_compaction_request`、生成并 `save_compaction`，再继续游玩；此时不会把整个原始 batch 再传给 Codex，只返回压缩状态和计数。正常帧只读取底部对白框；如果推进后对白框暂时为空，会在 `transition_wait_seconds` 的有界时间内用递增短等待重试。只有配置并确认 `transition_accelerate=true` 时，才会按上面的完整窗口多帧规则最多额外点击一次；默认不会在转场中盲点。等待结束后才对完整窗口做一次专门的选项 OCR，避免每帧扫描全屏，也避免在转场中误选。`max_steps` 和 `max_batch_chars` 仍可显式传入，仅用于冒烟测试或调用方自己的响应边界；省略它们时不设默认步数/批次字符上限。设置页会尝试点击明确的“回到游戏/返回游戏”，OCR 空帧、未识别对白等均归入安全停机；如果因 `dialogue_not_detected` 或 `ocr_unavailable` 停止，会自动附带最后一张完整窗口图并标记 `manual_intervention.required=true`，让 Codex 读图后用 `record_observation` 或一次 `advance_game` 接管。中间正常帧不会逐次传给 Codex，因此适合连续无人值守游玩。

每次本地解析还会附带精简的 `evidence`：`channels` 分开表示 `dialogue`、`speaker`、`choice`、`system_ui`、`unknown_text` 和转场状态，`safe_to_advance=false` 时不会把未知文字当作普通对白继续推进。`ui_lines` 记录 `SAVE/LOAD/VOICE` 等残留，`unknown_lines` 记录无法按当前布局确认的文字；它们不会从原始 OCR 中删除。`unknown_text_detected` 是需要 Codex 接管的安全停机原因。对白会生成稳定的 `episode_id`，便于跨帧去重和后续压缩；只有姓名框的旧版兼容路径仍可推进，因为部分引擎会把省略号等极短对白漏给 OCR。

如果默认队列方式没有推进，可改用直接窗口过程方式：

```text
advance_game(background=true, background_input_method="send", wait_seconds=0.05)
background_press_key(window_title="游戏窗口标题", key="SPACE", delivery="send")
background_scroll(window_title="游戏窗口标题", direction="down", delivery="post")
```

`background=true` 不支持 `hold_seconds`，因为千恋＊万花的后台推进使用离散左键；需要持续按键的其他游戏应使用前台 `hold_key` 或显式调用 `background_press_key`。`input_verification` 只以底部文本框变化为准。

追求最快推进时可以传 `advance_game(wait_seconds=0, transition_wait_seconds=0)`；如果游戏在推进后会短暂清空对白框，可保留默认的 `transition_wait_seconds=1.2`，或按游戏实际转场速度调整。该等待只在对白框消失时触发，普通对白不会额外变慢。

默认 `observe_game`、`advance_game` 和 `select_choice` 都会把截图留在会话目录，只向 Codex 返回 OCR 状态、`processed_text`、选项和必要动作结果；重复识别到同一对白时不会重复写入剧情事件。`configure_game_layout` 把游戏差异保存到会话：`speaker_markers` 和 `dialogue_markers` 是由游戏提供的 marker 数组，`dialogue_region` 是完整窗口中的文本框范围，`speaker_region` 默认相对于对白框，`choice_region` 默认相对于完整窗口；区域支持 `normalized`（0 到 1）和 `pixels`。OCR 丢失姓名框闭合符号时可为 speaker marker 设置 `allow_unclosed=true`。传 `{}` 可恢复不猜测符号的保守模式。窗口模式第一次仍保存完整窗口；同一 MCP 进程后续对白调用会把 profile 的 `dialogue_region` 作为 `capture_region` 保存为快速区域帧，并将 `capture_scope` 标为 `window_dialogue_region`。这不是缩放完整截图，完整首帧和任何回退帧仍保持原始窗口尺寸；如果区域捕获或区域 OCR 不可靠，结果会标为 `capture_fallback` 并回退完整窗口。未配置 `dialogue_region` 的游戏默认全屏 OCR。`advance_game` 的 `input_verification.method` 固定为 `bottom_textbox`：`changed=true` 只表示底部对白框文字发生变化；如果底部文本框不存在或 OCR 没有定位到它，会明确返回 `bottom_textbox_not_detected` 或 `ocr_unavailable`。需要持续按键的其他游戏可以显式调用 `hold_key` 或设置 `advance_hold_seconds`，但《千恋＊万花》的 Ctrl 是快进/跳过键，不应作为普通推进键配置。识别到系统设置页时会返回 `screen_type="settings"`，不把设置控件误记成剧情选项，并只在 OCR 找到“回到游戏/返回游戏”按钮时自动左键点击；找不到明确按钮时不会发送 ESC 或其他猜测输入。可传 `auto_return_from_settings=false` 禁用。`get_codex_context` 和 `galgame://active/context` 默认返回精简上下文，不包含原始 OCR、截图内容或截图事件。只有 OCR 失败或确实需要视觉判断时，才显式传 `include_image=true`。

MCP 本身不包含一个外部大模型，因此“看图并决定路线”由连接它的 Codex 完成；服务端负责窗口、截图、本地 OCR、文本解析、输入和可恢复记录。Windows OCR 在本地常驻一个复用 WinRT 环境的工作线程，避免每条对白重新启动 OCR。连接后 Codex 可以连续调用这些工具，不需要你逐步手动点击。

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

## 角色分类、误报和压缩交互

OCR 文本不会默认全部当作对白。配置了 `dialogue_region` 后，落在对白框内的普通文本才有资格成为 `dialogue`；落在已知选项区的文本才有资格成为 `choice`；其他位置保留在 `unparsed_lines`/`unknown_lines`，Evidence 会将其标记为未分类并阻止自动推进，等待 Codex 视觉复核。

默认至少需要两个有空间关系的选项行才会触发选项状态。单个以短横线、项目符号或 OCR 错误符号开头的对白会按其位置回收到对白，避免把“正常对白 + OCR 误识别的短横线”当成选项。确实存在单选项的游戏可以在 `configure_game_layout` 中显式设置 `choice_min_count=1`。

如果 Codex 视觉复核确认某条候选不是选项，调用：

```text
dismiss_choice(choice_id="choice_...", reason="false_positive_visual_review")
```

这会记录“误报已驳回”，不会伪造一个路线选择，也会解除该候选对压缩前缀的保护。

`play_until_choice` 在 `compaction_due` 时故意不返回整批原始对白，以减少 Codex token 消耗；原始事件仍保存在本地 `events.jsonl`。响应会返回 `batch_omitted_for_compaction.next_tool=get_compaction_request`、候选状态和阻塞原因。Codex 应调用 `get_compaction_request` 取得带 SHA-256 校验的有界事件段，写入 `save_compaction` 后再继续游玩。

## 许可证

本项目使用 [MIT License](LICENSE)。项目不包含任何游戏本体、游戏资源、存档或截图；使用者需要自行确认目标游戏和相关数据的版权、隐私及自动化使用边界。
