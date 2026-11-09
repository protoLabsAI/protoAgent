# Use browser automation

The bundled **Agent Browser** plugin lets the agent browse websites and lets you
interact with the same browser in the **Browser** panel. It starts disabled.

## Enable and start

1. Open **Settings → Plugins → Installed**, find **Agent Browser**, and enable it.
2. Open its **Browser** view. If the setup banner reports a missing CLI, choose
   **Download agent-browser**. The first browser command can also download the
   pinned CLI when automatic downloading is enabled.
3. If Chrome is missing, choose **Install Chrome**. This download starts only
   when you request it. On Linux ARM64, use a system Chromium installation;
   see [runtime requirements](/reference/browser-automation#the-cli-and-its-chrome).
4. Wait for the setup banners to clear, then use **Start**. If a home URL is
   configured, the panel opens it automatically.
5. Ask: “Open example.com and summarize the page.” Confirm the Browser panel
   shows the page and inspect the tool result in chat.

The browser runs on the agent’s machine. Your laptop’s browser login is not
shared automatically with it.

Each agent has its own browser. Two agents on the same machine never see each
other’s pages or share each other’s logins. To give every chat its own browser
too, set **Session scope** to `per_session`. A chat browser shuts down after 30
idle minutes. The panel always shows the agent’s main browser.

## Interact with a page

Use the Browser panel to click, type, scroll, or complete a login. You and the
agent control the same browser; steer the agent to wait while you enter information.
A configured browser profile can preserve cookies and login state across sessions.
Treat that profile as access to the accounts signed in there.

For a document, ask the agent to save a screenshot or PDF and offer it as a file
artifact. Open the resulting card and download the copy you want to keep. Browser
captures are temporary: the capture directory is pruned past its file/size limits.
PDF output is always US Letter; see [capture limits](/reference/browser-automation#screenshots-and-pdfs-are-fenced).

<!-- docs-media: browser-control -->
<figure class="docs-media"><video controls playsinline preload="none" poster="/media/browser-control/v1-poster.png" aria-label="Navigate public example pages in the live Browser panel, then ask the agent to save a screenshot." style="max-width:100%"><source src="/media/browser-control/v1.mp4"><a href="/media/browser-control/v1.mp4">Download video</a></video><p><a href="/media/browser-control/v1.mp4">Open video</a> · <a href="/media/browser-control/v1-steps.txt">Read the text walkthrough</a></p><figcaption>The Browser panel and agent share the same session. This silent recording shows manual navigation and a real saved screenshot; no login is required.</figcaption></figure>
<!-- /docs-media -->

## Approve a submission

The agent fills forms but doesn't send them without you. When it tries to submit a
form, place an order, send a message or delete something, the chat shows an
approval card such as *Submit the form on boards.greenhouse.io — button “Submit
application”*.

1. Check the page in the Browser panel. Is the form complete, and is it the right
   page?
2. Choose **Allow** to run that one action, or **Don't allow** to stop it. If you
   decline, the agent leaves the page as it is and asks you how to proceed.

A scheduled, background or delegated run can't show you a card, so its submit is
refused and the page is left for you. To also approve JavaScript, uploads or
downloads, or to turn the check off, change **Confirm actions**; see
[the reference](/reference/browser-automation#consequential-actions-ask-you-first).

## Change browser settings

Open **Settings → Plugins → Agent Browser**. Useful controls include the home URL,
browser profile, visible window (`headed`), allowed domains, and action confirmation.
Launch settings take effect after closing and reopening the browser session.
The [settings reference](/reference/browser-automation#settings) lists every field.

## Recover from a failure

- **Download or installation failed:** read the banner’s error, correct the cause,
  then use **Retry**. An unsupported platform needs a compatible manual install.
- **Wrong CLI found:** set an absolute path in **agent-browser binary** and retry.
- **“The browser profile is already in use”:** another browser has that profile
  directory open. With `per_session`, only one chat at a time can use a profile.
  Close the other chat’s browser, or clear **Browser profile dir**.
- **Site refuses automation:** complete a login manually or use a headed session
  with a profile. Some sites still refuse automated access; repeated retries may
  not help.
- **Saved capture has no download:** ask the agent to offer the existing file as an
  artifact. Large files can exceed the artifact limit; inspect the tool’s warning.

For tool schemas, capture behavior, and panel transport/auth details, use the
[browser reference](/reference/browser-automation).

<span id="earlier-section-links"></span>

<a id="browser-automation"></a>
<a id="the-cli-and-its-chrome"></a>
<a id="how-the-agent-uses-it"></a>
<a id="screenshots-and-pdfs-are-fenced"></a>
<a id="the-browser-panel"></a>
<a id="settings"></a>
<a id="pages-that-block-automation"></a>
<a id="where-things-live"></a>

The former technical sections are preserved in [Browser automation reference](/reference/browser-automation).
