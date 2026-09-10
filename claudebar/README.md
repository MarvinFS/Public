# ClaudeBar

Windows system tray app that tracks Claude Code and OpenAI Codex usage. Inspired by [CodexBar](https://github.com/steipete/CodexBar), which runs only on macOS.

![ClaudeBar Main Window](mainwindow.jpg)

## What the panel shows

One engine at a time, switched with the buttons in the header.

For each rate-limit window (5-hour session and weekly): a bar with a tick at the point an even spend would have reached, how much is left, when it resets, and whether the remaining allowance lasts until the reset or runs out first at the pace spent so far.

Below the bars: API-equivalent cost and token counts for today, this month, and the last 31 days, today's output tokens and cache reads, a 7-day daily cost chart, and the model that dominates the month's cost.

The right-click tray menu shows a short summary of both engines without opening the panel.

## Supported engines

ClaudeBar reads OAuth tokens from the CLI credential files only.

Claude Code CLI: session and weekly limits, tokens per model, and costs from the JSONL logs. Requires an installed and authenticated Claude Code CLI.

Codex CLI: session and weekly limits, tokens, and costs from the local logs. Requires Codex CLI with `codex login` completed.

Not supported: the Claude Desktop app and the ChatGPT web interface, which authenticate with browser cookies that the usage APIs do not accept, and GitHub Copilot, whose metrics API serves organisation and enterprise accounts only.

## Install and run

Requires Windows 10 or 11 and Python 3.11 or newer. The [Releases](https://github.com/MarvinFS/Public/releases) page has a signed installer if you would rather skip Python.

```powershell
pip install -r requirements.txt
python src/main.py
```

The app starts in the tray. Left-click the icon to open the panel, right-click for the menu. Escape closes the panel.

## Build the executable

```powershell
.\build.ps1            # writes dist\ClaudeBar.exe, about 20 MB
.\build.ps1 -Install   # also copies it to the Startup folder
.\build.ps1 -Clean     # rebuilds from scratch
```

To use your own icon, put `app_icon.png` (tray and header) and `app_icon.ico` (the exe) in `resources/icons/`.

## Configuration

Settings live in `%LOCALAPPDATA%\ClaudeBar\config.json`:

```json
{
  "refresh_interval": 300,
  "cli_timeout": 30,
  "warning_threshold": 80,
  "critical_threshold": 95,
  "show_notifications": true,
  "start_minimized": true,
  "currency": "USD",
  "claude_enabled": true,
  "codex_enabled": true,
  "window_x": null,
  "window_y": null
}
```

`refresh_interval` is in seconds. The two thresholds turn the tray icon yellow and red. `currency` accepts USD, EUR, GBP, RUB, or RON. The engine toggles hide an engine you do not use. `window_x` and `window_y` hold the last dragged panel position.

## Where the numbers come from

Rate limits come from the Anthropic OAuth API, with the token from `~/.claude/.credentials.json`, and from the ChatGPT backend API, with the token from `~/.codex/auth.json`. Tokens and costs come from the JSONL logs in `~/.claude/projects/` and `~/.codex/`.

Costs are API-equivalent estimates, not subscription charges: every token is priced at what the same call would cost on the public API. Rates come from models.dev, downloaded at most once a day and cached at `%LOCALAPPDATA%\ClaudeBar\models_dev.json`. Until a download has succeeded the bundled tables apply and the footer says "bundled rates". Claude 1-hour cache writes cost 2x the input rate and 5-minute writes 1.25x. OpenAI requests with more than 272K input tokens use the long-context tier. Models from other providers, such as a local model through Codex, count towards tokens but not cost.

## Expired tokens

Claude OAuth tokens last about 8 hours. When the token has expired the panel keeps showing the last data, marked with an amber "cached from 2h ago", from `%LOCALAPPDATA%\ClaudeBar\snapshot_cache.json`. To get a fresh token, open Claude Code in any terminal: it refreshes the token on startup. ClaudeBar never stores or refreshes credentials itself.

## Troubleshooting

If the tray icon is missing, open the taskbar overflow arrow. Windows hides new tray icons by default.

If no Claude data appears, use Claude Code once so that `~/.claude/projects/` exists, and run `claude /usage` to check that the CLI is signed in.

If the Codex view shows an error, install Codex CLI and run `codex login`.

The panel opens near the tray on the primary monitor. Drag it anywhere and it stays there across restarts. If that spot is no longer on any monitor when the app starts, the panel returns to the default position. "Reset window position" in the tray menu does the same on demand.

## License

MIT
