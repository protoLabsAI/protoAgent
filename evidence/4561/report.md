# #4561 acceptance pre-check — 2026-11-01

**Result: the ≤6-action target is not met. The no-model path passes.** Epic remains open; Josh's hands-on test, real Claude-plan authentication, and the first model reply remain human steps.

## Release and isolation

Tested published **protolabs-agent 0.225.0**, resolved with a fresh uv cache and `--refresh`. Installed command:

```sh
uvx --refresh --from protolabs-agent protoagent serve --port 54888
```

The exact executed launcher is `launch.py`: it creates HOME with `mktemp -d`, changes into it, and execs that command with a minimal environment (HOME, PATH, LANG, UV_CACHE_DIR, UV_PYTHON_INSTALL_DIR). This prevents inherited agent configuration, authentication and model credentials. Version metadata is in `version.txt`; `server-final.log` records startup, requests and shutdown. Port 54888 was selected with an ephemeral socket bind, outside 7870–7880. Both temporary HOMEs and their package caches were removed, both audit server processes terminated, and no listener remained on 54888 (`teardown.json`, empty `port-after-teardown.txt`). No real HOME configuration was written and no pre-existing agent was stopped. The release's normal discovery boot sweep did discover peers; no fleet-management actions were taken.

The README's primary instruction is now **Download the desktop app**, not uvx. The live Download page offers the requested uvx route under **Prefer to run it yourself?**. This audit follows that documented package route; it does not establish desktop installation or WKWebView behavior. Download page and PyPI package both reported 0.225.0. uv was already installed; installing uv is outside the count.

## Path A: action count

**14 actions projected from README to first message sent; FAIL versus ≤6.** This counts the entire OAuth ceremony as **one action**, as requested, and preserves the default Basic archetype. Live execution stopped at the authorization handoff. No authenticated reply is claimed.

| # | User action | Evidence / status |
|---|---|---|
| 1 | Open the GitHub README | Live, `readme-01.png` |
| 2 | Click **Download the desktop app** | Live, `download.png`; use its documented uvx alternative |
| 3 | Type/run the uvx command above | Live, `launch.py`, `server-final.log` |
| 4 | Open http://127.0.0.1:54888/app/ | Live, run3 screenshot 01 |
| 5 | Click **Next** on Welcome | Live, run3 screenshot 02 |
| 6 | Click **Claude subscription** | Live, run3 screenshot 03 |
| 7 | OAuth sign-in (one counted action) | Click **Sign in with Claude** was live; stop at handoff, run3 screenshot 04. The remaining ceremony requires vendor approval, pasting the returned code, and **Complete sign-in**; none were performed. All are folded into this one OAuth action. |
| 8 | Click **Load models** | Source-derived; selects the first returned model. Initial unsigned probe fails. `onSignedIn` clears models/probe ref but is not a dependency of the auto-probe effect, so explicitly load after authentication. Actual provider success remains unverified. |
| 9 | Click **Next** after automatic connection test passes | Source-derived; automatic testing is not a user action |
| 10 | Fill **Agent name** | Source-derived for A; exercised live in B. Basic is preselected, no click needed. |
| 11 | Click **Next** to review | Source-derived for A; exercised live in B |
| 12 | Click **Finish** | Source-derived for A; exercised live in B |
| 13 | Fill the chat message | Source-derived; no authenticated message sent |
| 14 | Click **Send** | Source-derived; no authenticated message sent |

If counting the sign-in launch, vendor approval, code paste and completion click separately, the same journey is **17** actions. If a successful authentication automatically populates models despite the inspected dependency behavior, subtract one (13 under the requested OAuth convention). Keeping the default agent name subtracts one more. None meets six. Clicking the already-selected Basic card would add one. Clicking a starter suggestion instead of filling and sending can reduce the chat portion by one; still over six.

Source used: `SetupWizard.tsx` at checkout SHA `649cda1f9cdaf0c2d3aa2db7bc5bd4ddfdb8db48`. Downloaded `v0.225.0` version is byte-for-byte identical (`source/SetupWizard.release.tsx`). It confirms four wizard screens, automatic connection testing, Basic default, and the Finish transition. `source/OAuthAccount.tsx` and `source/ChatSessionSlot.tsx` record the OAuth and chat controls. This is an inspection-derived completion count, not an unattended OAuth acceptance result.

## Path B and dead ends

| Check | Result | Observation |
|---|---|---|
| Explore without a model completes wizard and opens console | PASS | Explore → name → Next → Finish; setup completion toast, wizard gone |
| Chat explicitly names missing model | PASS | “No model connected. The agent can't reply yet.”; composer disabled |
| Banner links to model setup | PASS | **Connect a model** opens Settings → Model, **Model & routing**, with **Add a connection** visible. URL remains `/app/` because settings is an overlay. |
| Next enabled after failed connection | FIXED on tested release/path | Real failed automatic test against `http://127.0.0.1:1/v1`, model `audit-no-such-model`; Next disabled, “Connection failed — fix it to continue.” |
| Raw `[Errno …]` on Finish | FIXED on tested no-model path | Finish succeeded without raw exception. Authenticated Claude Finish not tested. |
| “No messages in this session.” first screen | FIXED | Starter suggestions shown; absence explicitly asserted |
| Subscription copy implies CLI required | FIXED | “Sign-in happens right here in the browser — no command-line tool and no separate login needed.” The separate Claude Code chat suggestion still correctly labels its own CLI dependency. |

## Evidence and qualifications

- `summary.json`: combined verdict and action list.
- `run3/acceptance/`: eight passing wizard steps, screenshots 01–08, `summary.json`.
- `final-verify/no-model-verification/`: two passing banner/destination assertions, screenshots and `summary.json`.
- `negative/failed-connection/`: three passing negative-test steps, screenshots and `summary.json`.
- `readme-01.png`, `readme-02-expanded.png`, `download.png`: live instructions. Expanding the README's alternative routes was audit reconnaissance, not a prerequisite in the primary Download-page journey counted above.
- `*.flow.ts`, `playwright.config.ts`, logs and text snapshots: reproducible harness and raw results.

Used the repository's `runFlow` / `defineFlow` harness with FLOW_BASE_URL, FLOW_OUT_DIR and a non-secret placeholder FLOW_TOKEN. Since a fresh server uses open mode, the placeholder is removed before navigation; no real credential was injected. Earlier attempts are preserved: sandbox port binding failed with `[Errno 1] Operation not permitted`; a launch inherited PROTOAGENT_UI and exited 2 with “Headless setup cannot complete: no model api_key”; token/selector mismatches caused initial harness failures. A new HOME, minimal environment and corrected selectors resolved these. They are audit setup failures, not release dead ends. For the negative test only, the disposable instance's `.setup-complete` marker was removed to reopen the wizard, then restored for final verification. No mock API responses were used.

The board work-location tool was unavailable. All durable evidence is inside the assigned checkout; no external scratch repository, commit, push, PR, or epic closure was made.
