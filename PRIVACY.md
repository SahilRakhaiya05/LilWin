# Privacy Policy

**Last updated: March 22, 2026**

LilWin (**lil agents** for Windows) was built with privacy in mind. It runs on **your PC**, does not require an account, and **does not send your conversations or a usage profile to a lil-agents backend** — the app is not a hosted service and has no company-run analytics in the product.

---

## Your data stays on your PC

LilWin is a **local Windows** app (Python / PyQt6) that shows animated characters near the **taskbar** and uses bundled assets. It may store **app preferences** and related files (for example under your project’s `config/` when developing, or next to a built `.exe` when distributed that way) and, for some features, **user-local data** under `%LOCALAPPDATA%\lil-agents\` (for example logs, or optional local secrets for integrations such as **OpenClaw**). **Only on your machine** — we do not operate a central database of users or your chat content for this app.

LilWin does not exist to “phone home” your project paths, file contents, or chat transcripts to a lil-agents server. **Multi-walker channels** in the current app are **in-process memory** only, not uploaded to a shared cloud service as part of the app’s design.

---

## AI and provider CLIs

**Before you use chat, install the provider CLIs you need** (for example **Claude Code**, **Gemini**, **Codex**, **Copilot**, **OpenCode**, or **OpenClaw**) and ensure they are on your **`PATH`**, the same way you would run them in a normal terminal. The app spawns and connects to those tools **locally**.

When you use a character’s chat, **the provider’s CLI** (or linked external terminal) handles the conversation. **LilWin does not replace that pipeline with its own cloud**; it is not a separate layer that records your full transcript for a lil-agents server. Any data sent to **Anthropic, Google, Microsoft, OpenAI, or other vendors** is governed by **their** terms, APIs, and privacy policies, not by this document. **You** control which CLIs you install and which API keys or logins you configure for them.

---

## No accounts (for this app)

**No LilWin account, no in-app login to “lil agents” as a service, and no first-party user database** in the app itself. (Third-party tools you attach may still have their own logins, as they always do.)

---

## Changes

If this policy changes, the **“Last updated”** date at the top will be revised.

---

[← Back to README](./README.md)
