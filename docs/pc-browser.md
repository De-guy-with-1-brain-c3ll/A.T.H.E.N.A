# PC browser bridge

In the control node, open **Settings → PC browser control**. The PC connection
can be checked there. Keyboard/mouse control defaults to OFF; only that
authenticated console can enable it. The AI tool has no enable action. The
setting resets to OFF whenever the PC receiver restarts.

Ask: “Open https://example.com on my computer.” Then: “Look at that page and tell
me what it shows.” With the switch enabled, you can ask it to click a visible
field, type text, or press a supported browser key.

This controls a **dedicated visible Edge window** on this Windows PC (Chromium
on other platforms). It does not
inspect existing Chrome/Edge tabs or control desktop applications. Log into Teams
manually in that window if you want that browser profile to have your session.
The profile lives in ignored `data/pc-browser-profile` and is not uploaded.
File downloads remain disabled in this browser; use ATHENA's approved downloader.

Only one viewport screenshot is captured per requested inspection (1280×720,
JPEG). There is no continuous screen recording or screenshot polling. Screenshots
are saved under ATHENA `data/reports` on the Pi. Qwen vision receives the screenshot
and the inspection question; DeepSeek receives only a short observation, never
the image's large base64 string. Vision output is capped at 500 tokens, retries
are disabled, and page text is treated as untrusted data, not instructions.

Screenshot reading uses your existing DashScope account and can incur additional
vision charges. `ATHENA_PC_VISION_MODEL` defaults to `qwen-vl-plus`;
`ATHENA_PC_VISION_BASE_URL` defaults to the Beijing compatible endpoint. Both can
be changed in the Pi environment if your account uses another region/workspace.
See [Alibaba's vision API documentation](https://www.alibabacloud.com/help/en/model-studio/qwen-vl-compatible-with-openai).

Do not request screenshots while sensitive credentials are visible. The current
LAN bridge authenticates requests but plain HTTP does not encrypt content. Use a
trusted home LAN only; use HTTPS/a secure tunnel for sensitive screens. Public
HTTP/HTTPS browser targets are supported; private IP and private-DNS resources
are blocked. This is not a hardened sandbox against hostile websites or DNS
rebinding. Do not expose the PC bridge publicly.

The PC must remain awake with the receiver running. Start/stop it with
`tools/setup_pc_inbox.py`. Windows uses your installed Edge browser. To use a
different supported Playwright channel, set `ATHENA_PC_BROWSER_CHANNEL` before
launching the receiver; other platforms need Playwright's Chromium installed.
