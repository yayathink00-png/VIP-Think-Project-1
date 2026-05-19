# AI Video Stable Pipeline Handoff

Version: 2026-05-19

This package explains the current semi-automatic AI video production pipeline.

It is designed for one practical goal:

```text
Make AI video production repeatable, reviewable, and hard to break.
```

It is not a fully unattended robot. The current stable mode is:

```text
Codex agent + local scripts + dedicated Jimeng browser + human review gates
```

## What This Pipeline Solves

- Prevents generation in the wrong Jimeng dialogue.
- Prevents Codex from clicking generate before user approval.
- Keeps the user's daily browser separate from Jimeng automation.
- Tracks the current video state in `gate_status.json`.
- Keeps segment review, rerun, edit, and delivery records traceable.
- Packages final deliverables without symlinks.

## Core Rule

Every video project must have its own Jimeng dialogue.

```text
One new video project = one bound Jimeng dialogue.
All segments, reruns, and takes for that video stay in that dialogue.
The next video project creates and binds a new dialogue.
```

The dedicated browser can be reused. The Jimeng dialogue cannot be reused across different videos.

## Package Contents

```text
docs/
  AI_VIDEO_STABILITY_V0_1.md
  AI_VIDEO_NAME_GATE.md
  JIMENG_DEDICATED_BROWSER.md
  AI_VIDEO_STABILITY_ACCEPTANCE_20260519.md
  AI_VIDEO_SMOOTH_RUNBOOK_20260519.md
  JIMENG_DIALOGUE_NAMING_DECISION_20260519.md

scripts/
  ai_video_trial.py
  smart_edit.py
  open_jimeng_dedicated_chrome.sh

PIPELINE_OVERVIEW.md
COLLEAGUE_START_PROMPT.md
MANIFEST.md
```

## Recommended Reading Order

1. Read `PIPELINE_OVERVIEW.md`.
2. Read `docs/AI_VIDEO_NAME_GATE.md`.
3. Read `docs/JIMENG_DEDICATED_BROWSER.md`.
4. Use `COLLEAGUE_START_PROMPT.md` as the first prompt in Codex.

## Minimum Operating Flow

```bash
python3 scripts/ai_video_trial.py new-video-run \
  --video "<source-video>" \
  --final-video-name "<final-video-name>" \
  --jimeng-dialogue "<one-video-only-jimeng-dialogue>" \
  --workspace-url "<jimeng-workspace-url>"
```

Before any generation:

```bash
python3 scripts/ai_video_trial.py continue-safe \
  --run-dir "data/runs/<run-id>" \
  --stage generation \
  --segment 1 \
  --require-browser
```

This should block unless the current approval package has been shown and the user has explicitly confirmed.

## Current Stability Level

Stable enough for guarded production.

Not yet solved:

- Fully unattended Jimeng generation
- Automatic face identity verification
- Automatic OCR subtitle correctness
- Automatic final creative judgment

Keep those as manual review gates until the pipeline has passed more complete videos.
