"""Clawd Bot: a Claude-powered chat app with a Grok-style interface, in its own window.

    python server.py              # opens the app window
    python server.py --no-open    # just run the server

Serves index.html on localhost. Replies come from one of two engines:
  claude-code  runs your Claude Code sign-in (`claude -p`), no API key; every chat is a real session
  api          the official Anthropic SDK with an API key
"auto" (the default) uses Claude Code when it's signed in, else the API key.
Your credentials never reach the page. Chats and settings live in ~/.clawd-bot.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import request as urlrequest

try:
    import anthropic
except ImportError:  # the launcher normally prevents this; give a readable hint if not
    sys.exit("The 'anthropic' package is missing. Run setup.bat (or: pip install anthropic).")

import claude_code

HOST, PORT = "127.0.0.1", int(os.environ.get("CLAWD_BOT_PORT", 8787))
URL = f"http://{HOST}:{PORT}"
PAGE = Path(__file__).with_name("index.html")
STORE = Path(os.environ.get("CLAWD_BOT_HOME") or Path.home() / ".clawd-bot")
CHATS = STORE / "chats"
CONFIG = STORE / "config.json"
ID_RE = re.compile(r"^[0-9a-f]{32}$")

MODELS = [
    {"id": "claude-opus-5-5", "label": "Opus 5.5", "blurb": "Most capable everyday model"},
    {"id": "claude-sonnet-5-5", "label": "Sonnet 5.5", "blurb": "Fast and capable"},
    {"id": "claude-fable-5-1", "label": "Fable 5.1", "blurb": "Deepest reasoning, slower"},
    {"id": "claude-haiku-4-5", "label": "Haiku 4.5", "blurb": "Fastest and cheapest"},
]
MAX_TOKENS = 64000
MAX_CONTINUATIONS = 6          # pause_turn resumes (server-side search hit its step limit)
FALLBACK_BETA = "server-side-fallback-2026-06-01"
FALLBACK_MODEL = "claude-opus-4-8"

PERSONA = (
    "You are Clawd Bot, an assistant powered by Claude. Be direct and concise: answer first, "
    "explain after. Be warm, with a little dry humor when it fits, and never preachy. Say plainly "
    "when you're unsure or don't know. Use markdown, with code blocks for code. When you used web "
    "search, ground your claims in what you found."
)
ENGINES = ("auto", "claude-code", "api")
WORKSPACES = STORE / "workspaces"   # one folder per bot; Claude Code names the session after it
DEFAULTS = {
    "model": "claude-opus-5-5",   # used by any bot that doesn't pick its own model
    "anthropic_key": "",
    "engine": "auto",
}

# ------------------------------------------------------------------------ bots
BOTS = STORE / "bots.json"
SHAPES = ("crab", "scout", "byte", "muse", "coach")   # the pixel shapes the page can draw
BOT_ID_RE = re.compile(r"^[a-z0-9-]{1,32}$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

STARTER_BOTS = [
    {"id": "clawd", "name": "Clawd", "tagline": "All-rounder", "shape": "crab", "color": "#D97757",
     "instructions": PERSONA, "model": "", "web_search": True, "think": False,
     "starters": ["What's in the news today?", "Explain how async/await works, with a short example.",
                  "Brainstorm five side projects I could build with LLMs."]},
    {"id": "scout", "name": "Scout", "tagline": "Researcher that cites its sources", "shape": "scout", "color": "#2fb5a0",
     "instructions": (
         "You are Scout, a research analyst. For anything that could have changed recently, search first, "
         "then answer. Lead with a three-line summary, then the details. Keep facts and your own inference "
         "clearly apart, name your sources inline, and say how confident you are. If sources disagree, show "
         "both sides. Keep it tight: no filler, no hype."),
     "model": "claude-opus-5-5", "web_search": True, "think": True,
     "starters": ["Brief me on the biggest AI news this week.", "Compare the top three options for local LLMs.",
                  "What changed in the latest Python release?"]},
    {"id": "byte", "name": "Byte", "tagline": "Senior engineer for code and debugging", "shape": "byte", "color": "#5b8def",
     "instructions": (
         "You are Byte, a senior software engineer pairing with a developer. Give working code first and "
         "keep explanations short. Ask a question only when you're truly blocked; otherwise state your "
         "assumption and go. Point out bugs, edge cases and tradeoffs you notice. Prefer small, readable "
         "solutions and match the user's language and style. Put all code in fenced blocks."),
     "model": "claude-opus-5-5", "web_search": False, "think": True,
     "starters": ["Review this function for bugs and edge cases.", "Write a Python script that renames files by date.",
                  "Explain this stack trace and how to fix it."]},
    {"id": "muse", "name": "Muse", "tagline": "Writing partner and idea generator", "shape": "muse", "color": "#a77be6",
     "instructions": (
         "You are Muse, a creative writing partner. Offer several distinct options instead of one safe "
         "answer. Use vivid, specific language and a real voice, never corporate filler. When a request is "
         "vague, make a bold choice and ask at most one clarifying question at the end. Match the tone the "
         "user wants, whether that's funny, warm or sharp."),
     "model": "claude-sonnet-5-5", "web_search": False, "think": False,
     "starters": ["Give me ten name ideas for a pixel-art crab mascot.", "Help me write a short, friendly message declining a meeting.",
                  "Write a punchy two-line product tagline for a local-first chat app."]},
    {"id": "coach", "name": "Coach", "tagline": "Training plans and form cues", "shape": "coach", "color": "#e6a23c",
     "instructions": (
         "You are Coach, a strength and fitness coach. Ask about goals, experience, equipment, schedule and "
         "any injuries before building a plan, unless they're already given. Give clear programs with sets, "
         "reps, rest and progression, plus short form cues. Be encouraging but honest. You are not a doctor: "
         "for pain, injury or medical conditions, suggest seeing a professional."),
     "model": "claude-sonnet-5-5", "web_search": False, "think": False,
     "starters": ["Build me a 4-day push/pull/legs split.", "How do I break through a bench press plateau?",
                  "Give me a 20-minute full-body workout with just dumbbells."]},
]


def load_bots():
    """All bots; writes the starter set the first time."""
    try:
        bots = json.loads(BOTS.read_text(encoding="utf-8")).get("bots")
        if isinstance(bots, list) and bots:
            return bots
    except (OSError, ValueError):
        pass
    save_bots(STARTER_BOTS)
    return [dict(b) for b in STARTER_BOTS]


def save_bots(bots):
    STORE.mkdir(parents=True, exist_ok=True)
    tmp = BOTS.with_suffix(".tmp")
    tmp.write_text(json.dumps({"bots": bots}, indent=2), encoding="utf-8")
    tmp.replace(BOTS)


def clean_bot(bot_id, raw):
    """A validated bot dict from page input; raises ValueError with a readable reason."""
    def text(key, limit, required=False):
        value = str(raw.get(key) or "").strip()
        if required and not value:
            raise ValueError(f"{key} is required")
        return value[:limit]

    if raw.get("shape") not in SHAPES:
        raise ValueError("unknown shape")
    if not COLOR_RE.match(str(raw.get("color") or "")):
        raise ValueError("color must look like #rrggbb")
    starters = [str(s).strip()[:200] for s in (raw.get("starters") or []) if str(s).strip()][:4]
    return {
        "id": bot_id, "name": text("name", 30, required=True), "tagline": text("tagline", 80),
        "shape": raw["shape"], "color": raw["color"], "instructions": text("instructions", 8000),
        "model": text("model", 60), "web_search": bool(raw.get("web_search")),
        "think": bool(raw.get("think")), "starters": starters,
    }


# --------------------------------------------------------------------- config

def load_config():
    try:
        saved = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    return {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}


def save_config(cfg):
    STORE.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    tmp.replace(CONFIG)


def api_key(cfg):
    env = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if env:
        return env, "environment"
    key = (cfg.get("anthropic_key") or "").strip()
    return (key, "saved") if key else ("", None)


def resolve_engine(cfg, status=None):
    """Which engine will answer: 'claude-code', 'api', or 'none' (demo mode)."""
    choice = cfg.get("engine", "auto")
    key, _ = api_key(cfg)
    if choice == "api":
        return "api" if key else "none"
    status = status or claude_code.auth_status()
    if choice == "claude-code":
        return "claude-code" if status["logged_in"] else "none"
    if status["logged_in"]:
        return "claude-code"
    return "api" if key else "none"


def public_config(cfg):
    key, source = api_key(cfg)
    status = claude_code.auth_status()
    resolved = resolve_engine(cfg, status)
    return {
        "engine": cfg.get("engine", "auto"),
        "engine_in_use": resolved,
        "ready": resolved != "none",
        "claude_code": status,
        "has_key": bool(key),
        "key_hint": ("…" + key[-4:]) if key else "",
        "key_source": source,
        "model": cfg["model"],
        "models": MODELS,
    }


# --------------------------------------------------------------------- Claude

def build_request(model, think, web, system, messages):
    """Keyword arguments for messages.stream(), following each model's rules.

    - Opus 5.5 / Fable 5.1: thinking is always on; "Think" raises effort and asks
      for readable summaries. Omitting `thinking` runs adaptive with hidden text.
    - Sonnet 5.5: thinking can't be "disabled"; "between_tools" turns it off.
    - Haiku 4.5: the older budget_tokens form.
    """
    kwargs = {"model": model, "max_tokens": MAX_TOKENS, "system": system, "messages": messages}
    if model.startswith("claude-haiku"):
        if think:
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": 8000}
    elif model.startswith("claude-sonnet-5-5"):
        kwargs["thinking"] = ({"type": "adaptive", "display": "summarized"} if think
                              else {"type": "between_tools"})
    elif model.startswith(("claude-opus-5", "claude-fable-5")):
        kwargs["output_config"] = {"effort": "high" if think else "low"}
        if think:
            kwargs["thinking"] = {"type": "adaptive", "display": "summarized"}
    if web:
        dynamic = model.startswith(("claude-opus-5", "claude-opus-4-8", "claude-sonnet-5"))
        kwargs["tools"] = [{
            "type": "web_search_20260209" if dynamic else "web_search_20250305",
            "name": "web_search", "max_uses": 5,
        }]
    return kwargs


def supports_fallback(model):
    """Refusal fallbacks are on by default for the models that can decline."""
    return model.startswith(("claude-opus-5", "claude-fable-5"))


def friendly_error(exc):
    if isinstance(exc, anthropic.AuthenticationError):
        return "Anthropic rejected the API key. Check it in Settings."
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "Your key doesn't have access to this model or tool."
    if isinstance(exc, anthropic.NotFoundError):
        return "Model not found. Pick another from the model menu."
    if isinstance(exc, anthropic.RateLimitError):
        return "Rate limited, or out of credits on your Anthropic account. Try again shortly."
    if isinstance(exc, anthropic.APIConnectionError):
        return "Couldn't reach Anthropic. Check your internet connection."
    if isinstance(exc, anthropic.BadRequestError):
        return f"Anthropic rejected the request: {getattr(exc, 'message', exc)}"[:400]
    if isinstance(exc, anthropic.APIStatusError):
        return f"Anthropic error {exc.status_code}: {getattr(exc, 'message', exc)}"[:400]
    return f"{type(exc).__name__}: {exc}"[:400]


def open_stream(client, kwargs, fallback):
    if fallback:
        return client.beta.messages.stream(
            betas=[FALLBACK_BETA], fallbacks=[{"model": FALLBACK_MODEL}], **kwargs)
    return client.messages.stream(**kwargs)


def run_turn(emit, client, kwargs, fallback, state):
    """One streamed request. Translates Claude's events for the page; returns the final message."""
    blocks = {}
    started = False
    try:
        with open_stream(client, kwargs, fallback) as stream:
            for ev in stream:
                started = True
                kind = ev.type
                if kind == "content_block_start":
                    block = ev.content_block
                    blocks[ev.index] = {"type": block.type, "name": getattr(block, "name", None), "json": ""}
                    if block.type == "web_search_tool_result" and isinstance(block.content, list):
                        for r in block.content:
                            if getattr(r, "url", None):
                                state["results"].append({"url": r.url, "title": getattr(r, "title", "") or ""})
                elif kind == "content_block_delta":
                    delta = ev.delta
                    if delta.type == "text_delta":
                        state["got_text"] = True
                        emit({"type": "delta", "text": delta.text})
                    elif delta.type == "thinking_delta":
                        emit({"type": "thinking", "text": delta.thinking})
                    elif delta.type == "input_json_delta" and ev.index in blocks:
                        blocks[ev.index]["json"] += delta.partial_json
                    elif delta.type == "citations_delta":
                        url = getattr(delta.citation, "url", None)
                        if url:
                            state["citations"].append(
                                {"url": url, "title": getattr(delta.citation, "title", "") or ""})
                elif kind == "content_block_stop":
                    info = blocks.get(ev.index)
                    if info and info["type"] == "server_tool_use" and info["name"] in ("web_search", "web_fetch"):
                        try:
                            args = json.loads(info["json"] or "{}")
                        except ValueError:
                            args = {}
                        what = args.get("query") or args.get("url") or ""
                        verb = "Searching the web" if info["name"] == "web_search" else "Reading"
                        emit({"type": "step", "text": f"{verb}: {what}" if what else verb})
            return stream.get_final_message()
    except anthropic.BadRequestError as exc:
        # An unsupported fallback setup is rejected before any output: retry once without it.
        if fallback and not started and "fallback" in str(exc).lower():
            return run_turn(emit, client, kwargs, None, state)
        raise


def unique_sources(items, limit=12):
    seen, out = set(), []
    for item in items:
        if item["url"] not in seen and item["url"].startswith(("http://", "https://")):
            seen.add(item["url"])
            out.append(item)
    return out[:limit]


def stream_api(emit, key, cfg, body):
    ctx = reply_context(cfg, body)
    model, think, web, system, messages = ctx["model"], ctx["think"], ctx["web"], ctx["system"], ctx["messages"]
    client = anthropic.Anthropic(api_key=key, timeout=120.0)
    fallback = supports_fallback(model)
    state = {"citations": [], "results": [], "got_text": False}

    try:
        final = None
        for _ in range(MAX_CONTINUATIONS):
            final = run_turn(emit, client, build_request(model, think, web, system, messages), fallback, state)
            if final.stop_reason != "pause_turn":
                break
            messages.append({"role": "assistant", "content": final.content})  # resume the server-side search
    except anthropic.APIError as exc:
        return emit({"type": "error", "text": friendly_error(exc)})

    sources = unique_sources(state["citations"]) or unique_sources(state["results"], 6)
    if sources:
        emit({"type": "sources", "sources": sources})
    if final is not None and final.stop_reason == "refusal":
        category = getattr(getattr(final, "stop_details", None), "category", None)
        emit({"type": "error", "text": "Claude declined to answer this" + (f" ({category})." if category else ".")})
    elif final is not None and final.stop_reason == "max_tokens":
        emit({"type": "error", "text": "The reply hit the length limit and was cut off."})
    elif not state["got_text"]:
        emit({"type": "error", "text": "Claude returned no text."})
    emit({"type": "done"})


def reply_context(cfg, body):
    """What every engine needs: the bot, model, switches, system prompt and clean history."""
    bot = next((b for b in load_bots() if b["id"] == body.get("bot_id")), None)
    instructions = ((bot or {}).get("instructions") or "").strip() or PERSONA
    return {
        "bot": bot,
        "model": (body.get("model") or (bot or {}).get("model") or cfg["model"]).strip(),
        "think": bool(body.get("think", (bot or {}).get("think", False))),
        "web": bool(body.get("web_search", (bot or {}).get("web_search", False))),
        "system": f"{instructions}\n\nToday's date: {date.today().isoformat()}.",
        "messages": [
            {"role": m["role"], "content": str(m.get("content", ""))}
            for m in (body.get("messages") or [])[-40:]
            if m.get("role") in ("user", "assistant") and str(m.get("content", "")).strip()
        ],
    }


def stream_claude_code(emit, cfg, body):
    ctx = reply_context(cfg, body)
    chat_id = body.get("chat_id") if ID_RE.match(str(body.get("chat_id") or "")) else None
    ok = claude_code.stream_reply(
        emit, store=STORE, workspace=WORKSPACES / ((ctx["bot"] or {}).get("id") or "chat"), chat_id=chat_id,
        messages=ctx["messages"], model=ctx["model"], think=ctx["think"], web=ctx["web"], system=ctx["system"])
    if ok:
        emit({"type": "done"})


def demo_reply(emit, messages):
    last = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
    text = (
        "**Demo mode.** I'm Clawd Bot, but I'm not connected to Claude yet, so I can't think for real.\n\n"
        "Open **Settings** (bottom left) and press **Sign in to Claude Code**, or paste an API key "
        "from console.anthropic.com. Then ask me again.\n\n"
        f"For now I'll just echo you: > {last[:200]}"
    )
    emit({"type": "step", "text": "Warming up"})
    time.sleep(0.4)
    for word in re.findall(r"\S+\s*", text):
        emit({"type": "delta", "text": word})
        time.sleep(0.02)
    emit({"type": "done"})


def list_live_models(key):
    client = anthropic.Anthropic(api_key=key, timeout=20.0)
    return [m.id for m in client.models.list(limit=100) if m.id.startswith("claude-")]


# --------------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ClawdBot/2.0"

    # -- plumbing ----------------------------------------------------------
    def log_message(self, *args):
        pass

    def trusted(self):
        """Only talk to our own page: blocks other sites and DNS-rebinding tricks."""
        if self.headers.get("Host", "") not in (f"{HOST}:{PORT}", f"localhost:{PORT}"):
            return False
        origin = self.headers.get("Origin")
        return not origin or origin in (f"http://{HOST}:{PORT}", f"http://localhost:{PORT}")

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        return json.loads(self.raw_body or b"{}")

    def route(self, method):
        # Always consume the request body, even for routes that ignore it: on a
        # keep-alive connection, unread bytes would corrupt the next request.
        length = int(self.headers.get("Content-Length") or 0)
        if length > 5_000_000:
            self.close_connection = True
            return self.send_json({"error": "body too large"}, 413)
        self.raw_body = self.rfile.read(length) if length else b""
        if not self.trusted():
            return self.send_json({"error": "forbidden"}, 403)
        path = self.path.split("?")[0]
        try:
            handler = getattr(self, f"{method}_{'_'.join(p for p in path.split('/') if p) or 'index'}", None)
            if handler:
                return handler()
            if path.startswith("/api/chats/"):
                return self.chat_item(method, path.rsplit("/", 1)[1])
            if path.startswith("/api/bots/"):
                return self.bot_item(method, path.rsplit("/", 1)[1])
            self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # keep the server alive; tell the page
            try:
                self.send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            except OSError:
                pass

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_DELETE(self):
        self.route("DELETE")

    # -- pages and settings --------------------------------------------------
    def GET_index(self):
        body = PAGE.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def GET_api_ping(self):
        self.send_json({"app": "clawd-bot"})

    def GET_api_config(self):
        self.send_json(public_config(load_config()))

    def POST_api_config(self):
        incoming, cfg = self.read_json(), load_config()
        if isinstance(incoming.get("model"), str) and incoming["model"].strip():
            cfg["model"] = incoming["model"].strip()
        if incoming.get("engine") in ENGINES:
            cfg["engine"] = incoming["engine"]
        if incoming.get("clear_key"):
            cfg["anthropic_key"] = ""
        elif isinstance(incoming.get("anthropic_key"), str) and incoming["anthropic_key"].strip():
            cfg["anthropic_key"] = incoming["anthropic_key"].strip()
        save_config(cfg)
        self.send_json(public_config(cfg))

    def GET_api_test(self):
        """Check the engine that will answer: Claude Code's sign-in, or the API key."""
        cfg = load_config()
        status = claude_code.auth_status(force=True)
        engine = resolve_engine(cfg, status)
        if engine == "claude-code":
            return self.send_json({"ok": True, "engine": engine, "detail": f"Signed in to Claude Code ({status['method']})."})
        key, _ = api_key(cfg)
        if engine == "api":
            try:
                return self.send_json({"ok": True, "engine": engine, "models": list_live_models(key),
                                       "detail": "API key works."})
            except anthropic.APIError as exc:
                return self.send_json({"ok": False, "engine": engine, "error": friendly_error(exc)})
        if cfg.get("engine") == "claude-code":
            hint = claude_code.SIGN_IN_HINT if status["available"] else "Claude Code isn't installed."
        else:
            hint = "Not connected. Press \"Sign in to Claude Code\" in Settings, or add an API key."
        self.send_json({"ok": False, "engine": engine, "error": hint})

    # -- bots ----------------------------------------------------------------
    def GET_api_bots(self):
        self.send_json({"bots": load_bots(), "shapes": list(SHAPES)})

    def POST_api_bots(self):
        self.send_json({"id": "bot-" + uuid.uuid4().hex[:10]})

    def bot_item(self, method, bot_id):
        if not BOT_ID_RE.match(bot_id):
            return self.send_json({"error": "bad id"}, 400)
        bots = load_bots()
        if method == "DELETE":
            if len(bots) <= 1:
                return self.send_json({"error": "Keep at least one bot."}, 400)
            save_bots([b for b in bots if b["id"] != bot_id])
            return self.send_json({"ok": True})
        try:
            bot = clean_bot(bot_id, self.read_json())
        except ValueError as exc:
            return self.send_json({"error": str(exc)}, 400)
        save_bots([bot if b["id"] == bot_id else b for b in bots] if any(b["id"] == bot_id for b in bots) else bots + [bot])
        self.send_json({"ok": True, "bot": bot})

    def POST_api_signin(self):
        """Start Claude Code's sign-in. The user completes it in the window and browser it opens."""
        if claude_code.start_login():
            return self.send_json({"ok": True})
        self.send_json({"ok": False, "error": "Claude Code isn't installed on this computer."})

    def POST_api_quit(self):
        self.send_json({"ok": True})
        threading.Timer(0.3, self.server.shutdown).start()

    # -- saved chats ---------------------------------------------------------
    def GET_api_chats(self):
        items = []
        for path in CHATS.glob("*.json") if CHATS.exists() else []:
            try:
                chat = json.loads(path.read_text(encoding="utf-8"))
                items.append({"id": path.stem, "title": chat.get("title", "New chat"),
                              "bot": chat.get("bot", ""), "updated": chat.get("updated", 0)})
            except (OSError, ValueError):
                continue
        items.sort(key=lambda c: c["updated"], reverse=True)
        self.send_json({"chats": items})

    def chat_item(self, method, chat_id):
        if not ID_RE.match(chat_id):
            return self.send_json({"error": "bad id"}, 400)
        path = CHATS / f"{chat_id}.json"
        if method == "GET":
            try:
                return self.send_json(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                return self.send_json({"error": "not found"}, 404)
        if method == "DELETE":
            path.unlink(missing_ok=True)
            return self.send_json({"ok": True})
        chat = self.read_json()
        messages = [
            {"role": m["role"], "content": str(m.get("content", "")),
             "sources": m.get("sources") or [], "thinking": str(m.get("thinking") or ""),
             "thinkMs": m.get("thinkMs") or 0, "steps": m.get("steps") or []}
            for m in chat.get("messages", []) if m.get("role") in ("user", "assistant")
        ]
        CHATS.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "id": chat_id, "title": str(chat.get("title") or "New chat")[:80],
            "bot": str(chat.get("bot") or "")[:32],
            "messages": messages, "updated": time.time(),
        }), encoding="utf-8")
        self.send_json({"ok": True})

    def POST_api_chats(self):
        self.send_json({"id": uuid.uuid4().hex})

    # -- chat ------------------------------------------------------------------
    def POST_api_chat(self):
        body = self.read_json()
        cfg = load_config()
        engine = resolve_engine(cfg)
        key, _ = api_key(cfg)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def emit(event):
            self.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")
            self.wfile.flush()

        try:
            if engine == "claude-code":
                stream_claude_code(emit, cfg, body)
            elif engine == "api":
                stream_api(emit, key, cfg, body)
            else:
                demo_reply(emit, body.get("messages") or [])
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # page went away (Stop button or closed window)


# ---------------------------------------------------------------------- launch

def app_window_command():
    """Edge or Chrome in app mode (no tabs or address bar), like a desktop program."""
    candidates = [shutil.which("msedge"), shutil.which("chrome")]
    for root in filter(None, (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                              os.environ.get("LOCALAPPDATA"))):
        candidates += [
            Path(root) / "Microsoft/Edge/Application/msedge.exe",
            Path(root) / "Google/Chrome/Application/chrome.exe",
        ]
    for path in candidates:
        if path and Path(path).exists():
            return [str(path), f"--app={URL}", "--window-size=1120,800"]
    return None


def open_window():
    command = app_window_command()
    if command:
        subprocess.Popen(command)
    else:
        webbrowser.open(URL)


def already_running():
    try:
        with urlrequest.urlopen(URL + "/api/ping", timeout=1.5) as resp:
            return json.loads(resp.read()).get("app") == "clawd-bot"
    except (OSError, ValueError):
        return False


def main():
    if already_running():
        if "--no-open" not in sys.argv:
            open_window()
        return
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    if "--no-open" not in sys.argv:
        open_window()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
