# AI Video Smooth Runbook - 2026-05-19

Goal: make the Jimeng AI video workflow fast, stable, and review-safe.

Core rule:

```text
Codex may prepare, fill, download, notify, and package.
Codex must not click generate or approve a segment without explicit user approval.
```

## State Machine

Use `gate_status.json` as the source of truth.

| State | Meaning | Allowed next step |
|---|---|---|
| `not_returned` | Segment has not been generated or returned. | Prepare preflight. |
| `preflight_blocked` | Requirements, prompt, page, or approval is missing. | Fix blockers and show user again. |
| `ready_for_user_generation_approval` | Page, settings, prompt, and screenshot are ready. | Wait for user to say `确认生成 Segment XX`. |
| `generating` | Jimeng generation has been clicked after user approval. | Wait, then download. |
| `generated_pending_review` | Video is downloaded and review package is ready. | Send DingTalk, then wait for user decision. |
| `reviewed` | User approved the segment. | Extract tail frame and prepare next segment. |
| `must_rerun` | User rejected the segment. | Prepare rerun preflight for the same segment. |
| `blocked_by_segment_XX_rerun` | A later segment depends on a rejected earlier segment. | Do not use this output. |

## Jimeng Dialogue Policy

The dedicated browser/session is reusable, but the Jimeng dialogue is not globally reusable.

```text
New video project -> create/bind one new Jimeng dialogue.
Same video project -> all Segment / rerun / take work stays in that bound dialogue.
Next video project -> create/bind a different dialogue.
```

For the current run only, the accepted Jimeng dialogue display name is:

```text
AI视频真人_学习效果与成果类_速算技巧
```

Do not reuse this dialogue for the next video project.

## Per-Segment Flow

### 0. Verify Jimeng Dialogue Name

Before any prompt is filled, the current Jimeng dialogue must match the current video project's bound dialogue name exactly.

For the current run, the bound dialogue name is:

```text
AI视频真人_学习效果与成果类_速算技巧
```

If the page shows a different dialogue name, Codex must stop and ask the user whether to select the bound dialogue, rename, or create a new dialogue. Codex must not infer from a similar name.

Verification command:

```bash
python3 scripts/ai_video_trial.py verify-jimeng-dialogue \
  --run-dir "data/runs/<run-id>" \
  --session jimeng-video
```

Before any stage transition, run:

```bash
python3 scripts/ai_video_trial.py continue-safe \
  --run-dir "data/runs/<run-id>" \
  --stage preflight \
  --segment XX \
  --require-browser
```

For generation, add `--user-confirmed` only after the user explicitly confirms the current approval package.

If the report says `Name only in input/search box: True`, the dialogue is not verified. This means Codex only searched for the name; it did not find or enter a matching dialogue.

### 1. Prepare Preflight

```bash
python3 scripts/ai_video_trial.py preflight-jimeng \
  --run-dir "data/runs/<run-id>" \
  --segment XX \
  --session jimeng-video \
  --allow-rerun
```

Preflight must check:

- correct segment exists
- creative requirements confirmed
- prompt marked `已确认`
- previous segment is user-approved
- current Jimeng dialogue is the corresponding confirmed dialogue
- mode is video generation
- model is `Seedance 2.0 Fast VIP`
- reference type is `全能参考`
- ratio is `9:16`
- duration is correct
- prompt says no subtitles, no text stickers, no Logo

### 2. Present To User Before Generation

Before clicking generate, Codex must show the user:

- current Jimeng URL
- current dialogue name
- screenshot path
- model / reference / ratio / duration
- exact prompt to be submitted
- any missing reference upload or continuity limitation

Wait for exact approval:

```text
确认生成 Segment XX
```

Codex must not self-supply this approval.

### 3. Click Generate Only After Approval

After user approval, mark state:

```text
generating
```

Then click Jimeng generate.

If captcha, paid confirmation, quota warning, wrong dialogue, wrong setting, or page uncertainty appears, stop and ask user.

### 4. Download Result

```bash
python3 scripts/ai_video_trial.py download-segment \
  --run-dir "data/runs/<run-id>" \
  --segment XX \
  --take A \
  --session jimeng-video
```

Expected output:

```text
returned/SegmentXX_takeA.mp4
downloads/segment_XX_A.json
```

### 5. Create Review Package

Do not pass `--decision` at this stage.

```bash
python3 scripts/ai_video_trial.py review-segment \
  --run-dir "data/runs/<run-id>" \
  --segment XX \
  --video "data/runs/<run-id>/returned/SegmentXX_takeA.mp4"
```

This should leave the segment in:

```text
generated_pending_review
```

### 6. Notify DingTalk

```bash
set -a
source .env
set +a

python3 scripts/ai_video_trial.py notify-dingtalk \
  --run-dir "data/runs/<run-id>" \
  --segment XX \
  --total-segments 5 \
  --review-file "data/runs/<run-id>/reviews/segment_XX/segment_review.md" \
  --jimeng-url "https://jimeng.jianying.com/ai-tool/generate?type=video&workspace=13101107985676" \
  --decision "待用户审核" \
  --reviewer "请审核 Segment XX，未通过前不继续下一段"
```

### 7. Wait For User Decision

Accepted decisions:

```text
Segment XX 可进入下一段
Segment XX 建议重跑
Segment XX 必须重跑
```

Record the decision only after the user says it:

```bash
python3 scripts/ai_video_trial.py record-review-decision \
  --run-dir "data/runs/<run-id>" \
  --segment XX \
  --decision 可进入下一段 \
  --reviewer "user"
```

For rejected segments:

```bash
python3 scripts/ai_video_trial.py record-review-decision \
  --run-dir "data/runs/<run-id>" \
  --segment XX \
  --decision 必须重跑 \
  --reviewer "user" \
  --note "User rejected: wrong dialogue / bad continuity / subtitles / visual issue"
```

### 8. Continue Only After Approval

If approved:

```bash
python3 scripts/ai_video_trial.py extract-tail-frame \
  --run-dir "data/runs/<run-id>" \
  --segment XX
```

Then prepare the next segment preflight.

If rejected:

- do not use downstream generated segments
- mark downstream as blocked
- rerun the rejected segment in the correct dialogue

## Current Recovery Point

Current run:

```text
data/runs/20260515-162712-jh-v10-解决学习问题-繁體-台灣-口播-横版-800x1000-202501
```

Current truth after reconciliation:

- Segment 01 approved.
- Segment 02 approved for workflow continuation, with embedded subtitle risk accepted.
- Segment 03 approved.
- Segment 04 approved.
- Segment 05 TakeC accepted by the user for stitching/editing.
- Smart edit v1 is the current review cut.

Before continuing, run:

```bash
python3 scripts/ai_video_trial.py reconcile-state \
  --run-dir "data/runs/20260515-162712-jh-v10-解决学习问题-繁體-台灣-口播-横版-800x1000-202501"
```

Next correct action:

```text
Review smart edit v1, then proceed to programmatic subtitles / CTA / final delivery package.
```

Dedicated Jimeng browser rule:

```text
Future Jimeng generation must use scripts/open_jimeng_dedicated_chrome.sh, not the user's daily browser.
```
