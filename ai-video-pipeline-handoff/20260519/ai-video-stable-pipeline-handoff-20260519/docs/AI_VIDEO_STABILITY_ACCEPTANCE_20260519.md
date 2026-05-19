# AI Video Stability Acceptance - 2026-05-19

## Verdict

Current process is stable enough for the next video to start, with guardrails.

It is not fully automatic, and should not be treated as unattended automation. It is stable as a gated production workflow:

```text
prepare/name-gate -> prompt approval -> dedicated-browser preflight -> user-confirmed generation -> review -> smart edit -> reconcile -> package
```

## What Is Now Protected

- Daily browser is separated from Jimeng automation.
- Every video must bind its own Jimeng dialogue.
- Every generation approval package includes Name Confirmation.
- `continue-safe` blocks generation without explicit user confirmation.
- `continue-safe` verifies package/handoff readiness.
- `reconcile-state` catches state/file mismatches.
- DingTalk manifests use timestamps and no longer overwrite old records.
- Delivery packages are copied without symlinks and include hash verification.

## Validation Run

Current run:

```text
data/runs/20260515-162712-jh-v10-解决学习问题-繁體-台灣-口播-横版-800x1000-202501
```

Checks performed:

```text
python3 -m py_compile scripts/ai_video_trial.py scripts/smart_edit.py
python3 scripts/ai_video_trial.py continue-safe --stage package ...
python3 scripts/ai_video_trial.py continue-safe --stage generation --segment 5 --require-browser
python3 scripts/ai_video_trial.py reconcile-state ...
```

Results:

- Syntax check: PASS
- Package-stage `continue-safe`: PASS
- Generation-stage `continue-safe` without user confirmation: BLOCKED as intended
- `reconcile-state`: already_consistent
- Delivery package hash: verified

## Remaining Non-Goals

These are intentionally not solved yet:

- Fully unattended Jimeng generation
- Headless browser generation
- Automatic face identity verification
- Automatic OCR-based subtitle correctness
- Automatic final creative judgment

## Required Operating Rule

For the next video:

1. Create a new run with `new-video-run`, including `--final-video-name`, `--jimeng-dialogue`, and `--workspace-url`.
2. Create/bind a new Jimeng dialogue for that video.
3. Use the dedicated browser profile only.
4. Run `continue-safe` before any generation.
5. Do not pass `--user-confirmed` unless the user explicitly approved the current Name Confirmation and prompt package.

## Decision

Stable enough to proceed to the next video or to finalize the current video with subtitles/CTA.

Recommendation: continue with guardrails, not full automation.
