# AI Video Name Gate

Goal: prevent Codex from generating in the wrong Jimeng dialogue or carrying one video's dialogue into another video project.

## Rule

Every generation must pass the name gate.

```text
Dedicated browser session can be reused.
Jimeng dialogue cannot be globally reused.
One video project = one bound Jimeng dialogue.
All Segment / rerun / take work for that video stays in that dialogue.
The next video project creates and binds a new dialogue.
```

## Three Names To Confirm

Before any Jimeng generation, Codex must show the user:

| Name | Meaning |
|---|---|
| Current video project | Local run/project identity |
| Bound Jimeng dialogue | The exact Jimeng conversation for this video only |
| Final video name | The user's intended output/delivery name |

If any of these are unclear, stop.

Bind these names into the state file:

```bash
python3 scripts/ai_video_trial.py bind-name-gate \
  --run-dir "data/runs/<run-id>" \
  --final-video-name "<最终成片名>" \
  --jimeng-dialogue "<本条视频专属即梦对话名>" \
  --workspace-url "<即梦 workspace URL>"
```

For a brand-new run, use the strict production entry:

```bash
python3 scripts/ai_video_trial.py new-video-run \
  --video "<source-video>" \
  --final-video-name "<最终成片名>" \
  --jimeng-dialogue "<本条视频专属即梦对话名>" \
  --workspace-url "<即梦 workspace URL>"
```

Do not start a production video with bare `prepare` unless the Name Gate will be bound immediately afterward.

Before continuing to generation, run:

```bash
python3 scripts/ai_video_trial.py continue-safe \
  --run-dir "data/runs/<run-id>" \
  --stage generation \
  --segment 1 \
  --require-browser
```

Without explicit user confirmation, this command must block.

## Current Video

For the current run:

```text
Current video project:
20260515-162712-jh-v10-解决学习问题-繁體-台灣-口播-横版-800x1000-202501

Bound Jimeng dialogue:
AI视频真人_学习效果与成果类_速算技巧

Final video name:
AI视频真人_学习效果与成果类_速算技巧_粤语_繁体_720×1280_202605
```

This dialogue is not reusable for the next video.

## Generation Approval Package

Every generation approval file must contain a `Name Confirmation` section. The user is not only approving a prompt; the user is also confirming:

- this is the correct video project
- this is the correct Jimeng dialogue for this video
- this is not a dialogue from a previous or future video
- the final output name is correct

## Stop Conditions

Stop immediately if:

- Jimeng page shows a different dialogue name
- the dialogue name is only visible in search/input, not selected
- final video name is missing or stale
- Codex corrected the dialogue after the user already approved generation
- Segment 02+ is about to use a different dialogue from Segment 01
- a new video is about to reuse an old video dialogue
