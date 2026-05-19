# AI Video Stability v0.1

Goal: make one AI video production run stable before expanding toward more automation.

## Principle

Do not optimize for "fully automatic" yet. Optimize for:

- one source of truth
- no wrong Jimeng dialogue
- no unapproved generation click
- no overwritten notifications
- repeatable delivery package
- user's daily browser remains usable

## Jimeng Dialogue Policy

Dedicated browser session is stable; Jimeng dialogue is project-specific.

```text
One new video project = one new Jimeng dialogue.
All segments, reruns, and takes of that same video project stay in that one dialogue.
The next video project must create and bind a new dialogue.
```

For the current run, the bound Jimeng dialogue is:

```text
AI视频真人_学习效果与成果类_速算技巧
```

Do not reuse this dialogue for a different video. Do not move a later segment of this video into another dialogue.

Every generation must pass the name gate:

```text
docs/AI_VIDEO_NAME_GATE.md
```

The generation approval package must show the current video project name, bound Jimeng dialogue name, and final video name.

## Source Of Truth

`gate_status.json` is the production state file.

For a new video, use the strict entry command:

```bash
python3 scripts/ai_video_trial.py new-video-run \
  --video "<source-video>" \
  --final-video-name "<最终成片名>" \
  --jimeng-dialogue "<本条视频专属即梦对话名>" \
  --workspace-url "<即梦 workspace URL>"
```

`prepare` still exists as a lower-level command, but `new-video-run` is the recommended production entry because it requires the Name Gate fields.

If a run already exists and needs binding, use:

```bash
python3 scripts/ai_video_trial.py bind-name-gate \
  --run-dir "data/runs/<run-id>" \
  --final-video-name "<最终成片名>" \
  --jimeng-dialogue "<本条视频专属即梦对话名>" \
  --workspace-url "<即梦 workspace URL>"
```

Before any stage transition, run the hard safety gate:

```bash
python3 scripts/ai_video_trial.py continue-safe \
  --run-dir "data/runs/<run-id>" \
  --stage package
```

For generation-stage checks:

```bash
python3 scripts/ai_video_trial.py continue-safe \
  --run-dir "data/runs/<run-id>" \
  --stage generation \
  --segment 1 \
  --require-browser
```

This must block unless the current approval package has been shown and the user has explicitly confirmed.

Before final edit, package, or handoff, run:

```bash
python3 scripts/ai_video_trial.py reconcile-state \
  --run-dir "data/runs/<run-id>"
```

If the user has accepted a segment despite known caveats:

```bash
python3 scripts/ai_video_trial.py reconcile-state \
  --run-dir "data/runs/<run-id>" \
  --accept-segments 5 \
  --decision "可进入剪辑/成片（用户接受 TakeC，已生成智能剪辑 v1）" \
  --reviewer user \
  --note "用户确认可拼接" \
  --write
```

## Dedicated Jimeng Browser

Jimeng generation must happen in the dedicated browser profile. This browser is the stable workbench, not the project identity:

```bash
scripts/open_jimeng_dedicated_chrome.sh
```

Runbook:

```text
docs/JIMENG_DEDICATED_BROWSER.md
```

The user's daily browser must not be used for Codex-controlled Jimeng generation.

The OpenCLI session name may remain:

```text
jimeng-dedicated
```

But the bound Jimeng dialogue inside that session must be checked against the current run's `gate_status.json`.

## Delivery Package

Create or refresh the human-facing delivery package:

```bash
python3 scripts/ai_video_trial.py package-delivery \
  --run-dir "data/runs/<run-id>" \
  --destination-root "/Users/yangyi/Documents/Codex/Advertising Automation-Assest section" \
  --name "AI视频真人_学习效果与成果类_速算技巧_粤语_繁体_720x1280_202605"
```

The command verifies the current video, writes `DELIVERY_MANIFEST.json`, and copies files without symlinks.

## DingTalk Notifications

`notify-dingtalk` now writes timestamped manifests:

```text
notifications/YYYYMMDD_HHMMSS_segment_XX_stage_dingtalk.json
```

This prevents review records from overwriting previous notifications.

## Current Run Reconciled State

Run:

```text
data/runs/20260515-162712-jh-v10-解决学习问题-繁體-台灣-口播-横版-800x1000-202501
```

Current truth:

- Segment 01 reviewed.
- Segment 02 reviewed with embedded subtitle risk accepted.
- Segment 03 reviewed.
- Segment 04 reviewed.
- Segment 05 reviewed for edit/package because the user accepted TakeC for拼接.
- Smart edit v1 is the current review cut.

## Stop Conditions

Stop the flow if any of these appear:

- `gate_status.json` disagrees with actual files or user decision.
- Jimeng page is not the dedicated browser/profile.
- workspace or current project's bound dialogue name is unclear.
- a new video is about to reuse a previous video's Jimeng dialogue.
- notification lacks a concrete prompt/file path.
- delivery package contains symlinks.
- current video hash cannot be computed.

## Next Stable Improvements

1. Add smarter visual QC fixtures: green page, wrong person, embedded text, black screen.
2. Add programmatic subtitles and CTA end card after smart edit.
3. Add one command that runs: reconcile -> continue-safe -> smart edit -> contact sheet -> package -> DingTalk.
4. Add a full orchestration command that runs: prepare/name-gate -> prompt approval -> preflight -> generation review -> smart edit -> package.
