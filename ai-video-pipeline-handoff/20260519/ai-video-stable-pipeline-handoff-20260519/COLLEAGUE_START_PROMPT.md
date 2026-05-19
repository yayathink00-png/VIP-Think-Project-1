# Prompt For Colleague

Copy this prompt into Codex when starting or continuing the AI video production workflow.

```text
你现在要接手一个半自动 AI 视频生产 pipeline。目标不是盲目全自动，而是稳定、安全、可审核地完成每一条 AI 视频。

请先阅读当前目录里的 handoff 包：

- README.md
- PIPELINE_OVERVIEW.md
- docs/AI_VIDEO_NAME_GATE.md
- docs/JIMENG_DEDICATED_BROWSER.md
- docs/AI_VIDEO_STABILITY_V0_1.md
- docs/AI_VIDEO_STABILITY_ACCEPTANCE_20260519.md

你必须遵守以下规则：

1. 每条新视频必须创建或绑定一个专属即梦对话。
   - Dedicated browser 可以复用。
   - Jimeng dialogue 不能跨视频复用。
   - 同一条视频的所有 segment / rerun / take 必须留在同一个已绑定对话里。

2. 每次生成前必须执行 Name Gate，向我确认：
   - 当前本地视频项目名
   - 本条视频绑定的即梦对话名
   - 最终成片名
   - 即梦 workspace URL

3. 在我明确审核并回复确认前，不允许点击生成。
   - 你可以准备提示词、检查页面、生成审核包。
   - 你不能擅自点击即梦生成按钮。

4. 必须使用专用即梦浏览器，不要使用我的日常浏览器。
   - 启动脚本：scripts/open_jimeng_dedicated_chrome.sh

5. 每个阶段继续前，都要用 continue-safe 检查。
   - generation 阶段无用户确认时应该被 BLOCK。
   - package 阶段必须确认状态一致、文件存在、没有 symlink 风险。

6. 出现以下情况必须停下来问我：
   - 即梦对话名不确定
   - workspace 不确定
   - 最终成片名不确定
   - 页面看起来是在搜索结果而不是已选中对话
   - 人物形象变化
   - 口播/字幕/画面明显不对
   - 需要重跑或进入下一段

请先不要操作即梦生成。先梳理当前项目状态，告诉我：

- 当前 run-dir 是什么
- Name Gate 是否已绑定
- 下一步处于哪个阶段
- 需要我审核什么
- 你会执行哪些安全检查

然后等我确认再继续。
```

## Short Version

```text
接手这个 AI 视频 pipeline。先读 README.md、PIPELINE_OVERVIEW.md、AI_VIDEO_NAME_GATE.md、JIMENG_DEDICATED_BROWSER.md。核心规则：每条视频一个专属即梦对话；生成前必须确认项目名、即梦对话名、最终成片名、workspace；没有我明确确认不能点生成；只用专用即梦浏览器；每阶段用 continue-safe；先汇报当前状态和下一步审核点，等我确认。
```
