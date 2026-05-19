# Jimeng Dedicated Browser - Stable Production Guardrail

Goal: keep Jimeng generation isolated from the user's daily browser so Codex does not steal focus, use the wrong tab, or collide with normal browsing.

## Rule

Codex may only operate Jimeng in the dedicated automation browser/profile.

Do not use the user's main Chrome/Safari/Arc window for generation.

The dedicated browser is a stable workbench. It does not mean every video uses the same Jimeng dialogue.

```text
One video project = one newly created/bound Jimeng dialogue.
All Segment / rerun / take work for that video stays in the bound dialogue.
The next video project creates and binds a different dialogue.
```

Before generation, also follow:

```text
docs/AI_VIDEO_NAME_GATE.md
```

## Launch

```bash
scripts/open_jimeng_dedicated_chrome.sh
```

Default profile:

```text
~/.codex/jimeng-chrome-profile
```

Default workspace:

```text
https://jimeng.jianying.com/ai-tool/generate?type=video&workspace=13101107985676
```

To override:

```bash
JIMENG_CHROME_PROFILE_DIR="$HOME/.codex/jimeng-chrome-profile-speed-math" \
scripts/open_jimeng_dedicated_chrome.sh "https://jimeng.jianying.com/ai-tool/generate?type=video&workspace=13101107985676"
```

## First-Time Setup

1. Run the launch command.
2. Log into Jimeng inside this dedicated Chrome window.
3. Install or enable the OpenCLI Browser Bridge extension in this profile if browser automation is needed.
4. Keep this window reserved for Jimeng automation only.

## Pre-Generation Checklist

Before Codex fills or clicks anything:

- Dedicated Chrome profile is open.
- Correct Jimeng workspace URL is visible.
- Correct dialogue name for the current video project is visible.
- Video generation mode is active.
- Reference video/tail frame status is verified.
- Prompt and settings have been shown to the user.
- User has explicitly replied with the required confirmation phrase.

## Stop Conditions

Stop immediately if:

- Login expired.
- Captcha or payment confirmation appears.
- Workspace or current project's bound dialogue name is unclear.
- A new video project is about to reuse a previous project's dialogue.
- A later segment/rerun/take is about to move out of the current project's bound dialogue.
- User manually takes over the automation window mid-run.
- OpenCLI reports a different session or page than expected.

## Why Not Headless Yet

Jimeng has login, upload, quota, result-menu, and generation-state UI. Headless automation would hide the exact failure modes that previously caused wrong-dialogue and no-review mistakes. Use a visible dedicated browser first; only consider headless after two complete videos pass without manual browser correction.
