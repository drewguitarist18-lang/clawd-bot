# Clawd Bot

A chat app with a Grok-style interface, run through **Claude Code**. It has
multiple chats and multiple bots, each bot with its own pixel-art sprite and its
own job. It opens in its own window (Edge or Chrome app mode: no tabs, no
address bar). Windows and Python 3.

Pairs with [**Claude Pet**](https://github.com/drewguitarist18-lang/claude-pet), the
desktop status pet and Claude Office that show each bot's live status.

## How it works

- **Every chat is a real Claude Code session.** Clawd Bot runs `claude -p`
  (print mode, streaming JSON) with your Claude Code sign-in. No API key needed.
- **Every bot runs in its own workspace folder** (`~/.clawd-bot/workspaces/<bot>`),
  so your bot chats are named after the bot in Claude Code, and the
  **Claude Office** view shows each one as that bot, with its own sprite shape,
  color and live status.
- Bots only get web lookup tools (search and fetch). They can't touch files or
  run commands.
- An **API key** still works as a fallback engine (official `anthropic` SDK).

## Run it

1. Sign in to Claude Code once (a browser window opens; you do this part):
   ```
   claude auth login
   ```
2. Double-click **Clawd Bot.bat**. The first run creates a `.venv` and installs
   the `anthropic` SDK (only used for the API-key fallback).

Settings (bottom left) shows which engine is answering and has a **Test
connection** button. Without either sign-in or a key it runs in demo mode.

## Bots

Five starters, each a different shape and function:

| Bot | Job | Defaults |
|---|---|---|
| Clawd (crab) | All-rounder | Search on |
| Scout (alien) | Researcher that cites its sources | Opus, Search + Think |
| Byte (robot) | Senior engineer for code and debugging | Opus, Think |
| Muse (ghost) | Writing partner and idea generator | Sonnet |
| Coach (lifter) | Training plans and form cues | Sonnet |

Click a bot to switch; click the selected one to edit it. You can change its
shape and color, its instructions, starter prompts, model, and Search and Think
defaults, or make new bots with **+ New bot**. Bots live in `~/.clawd-bot/bots.json`.

## Features

- Streaming replies, markdown with code blocks and tables, copy and retry
- **Search**: live web lookups with the query shown and a Sources list
- **Think**: more effort before answering, with a collapsible "Thought for Ns"
- Saved chats with the bot's sprite next to each one, Ctrl+K for a new chat
- Light and dark themes follow the system

## Where things live

| What | Where |
|---|---|
| Settings and optional API key | `~/.clawd-bot/config.json` (or `ANTHROPIC_API_KEY`) |
| Bots | `~/.clawd-bot/bots.json` |
| Chats | `~/.clawd-bot/chats/` (session ids in `cc_sessions.json`) |
| Bot workspaces | `~/.clawd-bot/workspaces/<bot>/` |

The server listens on localhost only and rejects requests from other sites or
hosts. Your credentials never reach the page.

## Notes

- Claude Code's own settings apply to these sessions too: your user-level
  `CLAUDE.md` and hooks load, which is how the Claude Office sees them.
- **Retry** and replacing a deleted session start a fresh Claude Code session
  with the conversation replayed, since a resumed session can't "un-say" a reply.

## Tests

`.venv\Scripts\python tests\smoke.py` runs the real server against two mocks:
`tests/mock_claude.py` (a stand-in `claude` CLI that follows the documented
stream-json events and replays the exact "Not logged in" output a signed-out CLI
gave) and `tests/mock_anthropic.py` (streams Messages API events, parsed by the
real SDK). 74 checks cover sessions and resume, retry replay, tool restrictions,
bots, search and sources, thinking, errors, saved chats and the localhost protections.

**It has not been run against a signed-in Claude Code or the real API**, only the
mocks, because the CLI on this machine isn't signed in yet. After `claude auth login`,
use Settings > Test connection, then send a message with Search off, then on.
