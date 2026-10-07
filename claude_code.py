"""The Claude Code engine: every chat is a real Claude Code session.

Clawd Bot runs the `claude` CLI in print mode (`claude -p`) with streaming JSON, so it uses
your Claude Code sign-in and needs no API key. Each chat maps to one session (resumed on the
next message), and each bot runs in its own workspace folder, so your chats also show up in
Claude Code and in the Claude Office view as that bot.

Sign in once with:  claude auth login
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW: don't flash a console when run from pythonw
STATUS_TTL = 15
INLINE_PROMPT_LIMIT = 6000   # longer prompts go over stdin (Windows command lines are limited)

_status_cache = {"at": 0.0, "value": None}
_sessions_lock = threading.Lock()


# ------------------------------------------------------------------- the CLI

def find_command():
    """The command that runs Claude Code, as a list, or None if it isn't installed."""
    override = os.environ.get("CLAWD_BOT_CLAUDE_CMD")
    if override:
        return json.loads(override)
    exe = shutil.which("claude")
    if exe:
        return [exe]
    base = Path(os.environ.get("APPDATA", "")) / "Claude" / "claude-code"
    found = list(base.glob("*/*/claude.exe")) if base.exists() else []

    def version(path):
        return [int(p) if p.isdigit() else 0 for p in path.parent.parent.name.split(".")]

    return [str(sorted(found, key=version)[-1])] if found else None


def auth_status(force=False):
    """{'available', 'logged_in', 'method'}; cached briefly because it spawns a process."""
    now = time.time()
    if not force and _status_cache["value"] and now - _status_cache["at"] < STATUS_TTL:
        return _status_cache["value"]
    command = find_command()
    value = {"available": False, "logged_in": False, "method": None}
    if command:
        value["available"] = True
        try:
            done = subprocess.run([*command, "auth", "status"], capture_output=True, text=True,
                                  timeout=20, creationflags=NO_WINDOW)
            data = json.loads(done.stdout or "{}")
            value["logged_in"] = bool(data.get("loggedIn"))
            value["method"] = data.get("authMethod")
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    _status_cache.update(at=now, value=value)
    return value


def start_login():
    """Open Claude Code's own sign-in (a console window, then your browser). You finish it there."""
    command = find_command()
    if not command:
        return False
    # The tests swap in a mock CLI; don't pop a console for that.
    flags = NO_WINDOW if os.environ.get("CLAWD_BOT_CLAUDE_CMD") else 0x00000010   # CREATE_NEW_CONSOLE
    try:
        subprocess.Popen([*command, "auth", "login", "--claudeai"], cwd=str(Path.home()), creationflags=flags)
    except OSError:
        return False
    _status_cache["value"] = None   # so the next status check really asks
    return True


def effort_for(model, think):
    if model.startswith("claude-haiku"):
        return None   # Haiku doesn't take an effort setting
    return "high" if think else "low"


def build_args(command, model, think, web, system, session_id, resume):
    args = [*command, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
            "--system-prompt", system, "--model", model]
    effort = effort_for(model, think)
    if effort:
        args += ["--effort", effort]
    # A chat bot only ever needs to look things up. No file or shell tools, ever.
    args += ["--tools", "WebSearch,WebFetch", "--allowedTools", "WebSearch,WebFetch"] if web else ["--tools", ""]
    args += ["--resume", session_id] if resume else ["--session-id", session_id]
    return args


# ------------------------------------------------------------- chat -> session

def _sessions_path(store):
    return Path(store) / "cc_sessions.json"


def _load_sessions(store):
    try:
        return json.loads(_sessions_path(store).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _remember_session(store, chat_id, session_id, turns):
    with _sessions_lock:
        data = _load_sessions(store)
        data[chat_id] = {"session": session_id, "turns": turns}
        path = _sessions_path(store)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)


def forget_session(store, chat_id):
    with _sessions_lock:
        data = _load_sessions(store)
        if data.pop(chat_id, None) is not None:
            _sessions_path(store).write_text(json.dumps(data), encoding="utf-8")


def transcript_prompt(messages):
    """A fresh session has no memory, so give it the earlier turns as context."""
    *earlier, last = messages
    if not earlier:
        return last["content"]
    lines = [f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content']}" for m in earlier]
    return ("Here is our conversation so far, for context:\n\n" + "\n\n".join(lines)
            + "\n\nReply to the user's latest message:\n\n" + last["content"])


# ----------------------------------------------------------------- the stream

SIGN_IN_HINT = ("Claude Code isn't signed in. Open Settings and press \"Sign in to Claude Code\". "
                "(Or add an API key there.)")
LINKS_RE = re.compile(r"Links:\s*(\[)")


def _web_results(text):
    """Source links from a WebSearch tool result ('Links: [{"title":..,"url":..}]')."""
    found = []
    match = LINKS_RE.search(text or "")
    if match:
        try:
            items, _ = json.JSONDecoder().raw_decode(text[match.start(1):])
            found = [{"url": i["url"], "title": i.get("title", "")} for i in items if isinstance(i, dict) and i.get("url")]
        except ValueError:
            pass
    return found


def _text_of(content):
    if isinstance(content, str):
        return content
    return "".join(p.get("text", "") for p in content or [] if isinstance(p, dict))


def _step_for(name, args):
    if name == "WebSearch":
        return f"Searching the web: {args.get('query', '')}".rstrip(": ")
    if name == "WebFetch":
        return f"Reading: {args.get('url', '')}".rstrip(": ")
    return None


class Run:
    """Reads one `claude -p` run and turns its JSON lines into page events."""

    def __init__(self, emit):
        self.emit = emit
        self.blocks = {}
        self.sources = []
        self.announced = set()     # tool_use ids we already showed as a step
        self.streamed_text = False
        self.final_text = ""
        self.error = None
        self.session_missing = False
        self.model_text = []

    def handle(self, obj):
        kind = obj.get("type")
        if kind == "stream_event" and not obj.get("parent_tool_use_id"):
            self.stream_event(obj.get("event") or {})
        elif kind == "assistant" and not obj.get("parent_tool_use_id"):
            self.assistant(obj.get("message") or {})
        elif kind == "user":
            for part in (obj.get("message") or {}).get("content") or []:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    self.sources += _web_results(_text_of(part.get("content")))
        elif kind == "result":
            self.result(obj)

    def stream_event(self, ev):
        kind = ev.get("type")
        if kind == "content_block_start":
            block = ev.get("content_block") or {}
            self.blocks[ev.get("index")] = {"type": block.get("type"), "name": block.get("name"),
                                            "id": block.get("id"), "json": ""}
        elif kind == "content_block_delta":
            delta = ev.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                self.streamed_text = True
                self.emit({"type": "delta", "text": delta["text"]})
            elif delta.get("type") == "thinking_delta" and delta.get("thinking"):
                self.emit({"type": "thinking", "text": delta["thinking"]})
            elif delta.get("type") == "input_json_delta" and ev.get("index") in self.blocks:
                self.blocks[ev["index"]]["json"] += delta.get("partial_json", "")
        elif kind == "content_block_stop":
            info = self.blocks.get(ev.get("index"))
            if info and info["type"] == "tool_use":
                try:
                    args = json.loads(info["json"] or "{}")
                except ValueError:
                    args = {}
                self.announce(info["id"], info["name"], args)

    def announce(self, tool_id, name, args):
        step = _step_for(name, args)
        if step and tool_id not in self.announced:
            self.announced.add(tool_id)
            self.emit({"type": "step", "text": step})

    def assistant(self, message):
        parts = message.get("content") or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text")
        if message.get("model") == "<synthetic>":   # Claude Code's own notices, e.g. "Not logged in"
            self.error = self.error or text
            return
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "tool_use":
                self.announce(part.get("id"), part.get("name"), part.get("input") or {})
        if text:
            self.model_text.append(text)

    def result(self, obj):
        text = obj.get("result") if isinstance(obj.get("result"), str) else ""
        if obj.get("is_error") or str(obj.get("subtype", "")).startswith("error"):
            self.error = self.error or text or "Claude Code reported an error."
        elif text:
            self.final_text = text
        if "No conversation found" in (self.error or ""):
            self.session_missing = True


def _friendly(error):
    if "Not logged in" in error or "/login" in error or "Invalid API key" in error:
        return SIGN_IN_HINT
    return error[:400]


def stream_reply(emit, *, store, workspace, chat_id, messages, model, think, web, system):
    """Stream one reply through Claude Code. Returns True when it finished cleanly."""
    command = find_command()
    if not command:
        emit({"type": "error", "text": "Claude Code isn't installed. Install it, or add an API key in Settings."})
        return False
    user_turns = sum(1 for m in messages if m["role"] == "user")
    known = _load_sessions(store).get(chat_id) if chat_id else None
    resume = bool(known) and known.get("turns") == user_turns - 1   # session holds exactly the earlier turns
    session_id = known["session"] if resume else str(uuid.uuid4())
    prompt = messages[-1]["content"] if resume else transcript_prompt(messages)

    Path(workspace).mkdir(parents=True, exist_ok=True)
    run = Run(emit)
    args = build_args(command, model, think, web, system, session_id, resume)
    use_stdin = len(prompt) > INLINE_PROMPT_LIMIT
    if not use_stdin:
        args += ["--", prompt]
    try:
        proc = subprocess.Popen(args, cwd=str(workspace), stdin=subprocess.PIPE if use_stdin else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    except OSError as exc:
        emit({"type": "error", "text": f"Couldn't start Claude Code: {exc}"})
        return False
    stderr_tail = []
    threading.Thread(target=lambda: stderr_tail.extend(proc.stderr.readlines()[-20:]), daemon=True).start()
    if use_stdin:
        threading.Thread(target=lambda: (proc.stdin.write(prompt), proc.stdin.close()), daemon=True).start()
    try:
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("{"):
                try:
                    run.handle(json.loads(line))
                except ValueError:
                    continue
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:   # the page hit Stop, or the stream broke: don't leave it running
            proc.kill()

    if run.session_missing and resume:      # the session was deleted on the Claude Code side
        forget_session(store, chat_id)
        return _retry_fresh(emit, store, workspace, chat_id, messages, model, think, web, system)
    if run.error:
        emit({"type": "error", "text": _friendly(run.error)})
        return False

    if not run.streamed_text:   # no partial events arrived: fall back to the finished message
        text = run.final_text or "".join(run.model_text)
        if text:
            emit({"type": "delta", "text": text})
        else:
            emit({"type": "error", "text": "Claude returned no text." + (f" {''.join(stderr_tail)[-200:]}" if stderr_tail else "")})
            return False
    seen, sources = set(), []
    for s in run.sources:
        if s["url"] not in seen and s["url"].startswith(("http://", "https://")):
            seen.add(s["url"])
            sources.append(s)
    if sources:
        emit({"type": "sources", "sources": sources[:12]})
    if chat_id:
        _remember_session(store, chat_id, session_id, user_turns)
    return True


def _retry_fresh(emit, store, workspace, chat_id, messages, model, think, web, system):
    """Re-run as a brand-new session with the history replayed in the prompt."""
    _remember_session(store, chat_id, "", -99)   # guarantees resume=False on the retry
    return stream_reply(emit, store=store, workspace=workspace, chat_id=chat_id, messages=messages,
                        model=model, think=think, web=web, system=system)
