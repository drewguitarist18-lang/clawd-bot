"""End-to-end smoke test: the real server.py and the real Anthropic SDK against a mock API.

    .venv\\Scripts\\python tests\\smoke.py
"""
import http.client
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib import error, request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
APP, MOCK = 8791, 8792
URL = f"http://127.0.0.1:{APP}"
failures = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def call(path, body=None, method=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = request.Request(URL + path, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode()
    except error.HTTPError as e:
        return e.code, e.read().decode()


def chat(**body):
    body.setdefault("messages", [{"role": "user", "content": "hi"}])
    _, text = call("/api/chat", body)
    return [json.loads(l[5:]) for l in text.split("\n\n") if l.startswith("data:")]


def text_of(events):
    return "".join(e.get("text", "") for e in events if e["type"] == "delta")


def mock_calls():
    return json.loads(request.urlopen(f"http://127.0.0.1:{MOCK}/_last").read())


def mock_set(**settings):
    request.urlopen(request.Request(f"http://127.0.0.1:{MOCK}/_config", data=json.dumps(settings).encode(), method="POST"))


def set_cli(logged_in):
    Path(cli_dir, "state.json").write_text(json.dumps({"logged_in": logged_in}))


def cli_calls():
    log = Path(cli_dir, "calls.jsonl")
    return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []


def start(env_extra):
    env = {**os.environ, "CLAWD_BOT_PORT": str(APP), "CLAWD_BOT_HOME": str(home),
           "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{MOCK}",
           "CLAWD_BOT_CLAUDE_CMD": json.dumps([sys.executable, str(ROOT / "tests" / "mock_claude.py")]),
           "CLAWD_MOCK_STATE": str(Path(cli_dir, "state.json")), "CLAWD_MOCK_LOG": str(Path(cli_dir, "calls.jsonl")),
           "CLAWD_MOCK_SESSIONS": str(Path(cli_dir, "sessions.json")), **env_extra}
    p = subprocess.Popen([sys.executable, str(ROOT / "server.py"), "--no-open"], env=env)
    for _ in range(60):
        try:
            call("/api/ping")
            return p
        except OSError:
            time.sleep(0.1)
    raise SystemExit("server did not start")


# ---- request building per model (pure function, no network) ---------------------------------
import server  # noqa: E402

sys_msg, msgs = "sys", [{"role": "user", "content": "x"}]
b = server.build_request("claude-opus-5-5", True, False, sys_msg, msgs)
check("Opus 5.5 + Think: adaptive/summarized, effort high",
      b["thinking"] == {"type": "adaptive", "display": "summarized"} and b["output_config"] == {"effort": "high"})
b = server.build_request("claude-opus-5-5", False, False, sys_msg, msgs)
check("Opus 5.5 no Think: never sends disabled/budget thinking", "thinking" not in b and b["output_config"] == {"effort": "low"})
b = server.build_request("claude-sonnet-5-5", False, False, sys_msg, msgs)
check("Sonnet 5.5 no Think uses between_tools (not disabled)", b["thinking"] == {"type": "between_tools"})
b = server.build_request("claude-haiku-4-5", True, False, sys_msg, msgs)
check("Haiku 4.5 + Think uses budget_tokens below max_tokens",
      b["thinking"]["type"] == "enabled" and b["thinking"]["budget_tokens"] < b["max_tokens"])
check("search tool: dynamic variant on Opus, basic on Haiku",
      server.build_request("claude-opus-5-5", False, True, sys_msg, msgs)["tools"][0]["type"] == "web_search_20260209"
      and server.build_request("claude-haiku-4-5", False, True, sys_msg, msgs)["tools"][0]["type"] == "web_search_20250305")
check("no sampling params or prefill sent", all(k not in b for k in ("temperature", "top_p", "top_k")) and b["messages"][-1]["role"] == "user")

# ---- the server against the mock API --------------------------------------------------------
home = tempfile.mkdtemp(prefix="clawdbot-test-")
cli_dir = tempfile.mkdtemp(prefix="clawdbot-cli-")
set_cli(False)   # the API suites run with Claude Code signed out
mock = subprocess.Popen([sys.executable, str(ROOT / "tests" / "mock_anthropic.py"), str(MOCK)])
time.sleep(0.6)
server_proc = start({"ANTHROPIC_API_KEY": "test-key"})
try:
    s, body = call("/")
    check("serves the page", s == 200 and "Clawd" in body)
    cfg = json.loads(call("/api/config")[1])
    check("config reports key, never the key itself", cfg["has_key"] and "test-key" not in json.dumps(cfg) and cfg["key_hint"] == "…-key")
    check("model menu comes from the server", [m["id"] for m in cfg["models"]][0] == "claude-opus-5-5")
    t = json.loads(call("/api/test")[1])
    check("test connection lists live claude models", t["ok"] and "some-other-model" not in t["models"] and "claude-opus-5-5" in t["models"], t)

    # ---- bots: different shapes and functions --------------------------------------------
    listing = json.loads(call("/api/bots")[1])
    bots = listing["bots"]
    check("starter bots seeded with distinct shapes", [b["id"] for b in bots] == ["clawd", "scout", "byte", "muse", "coach"]
          and len({b["shape"] for b in bots}) == 5 and set(listing["shapes"]) == {b["shape"] for b in bots})
    check("bots differ in function (instructions, model, search, think)",
          len({b["instructions"] for b in bots}) == 5 and {b["model"] for b in bots} >= {"claude-opus-5-5", "claude-sonnet-5-5"}
          and bots[1]["web_search"] and not bots[2]["web_search"] and bots[2]["think"])
    chat(bot_id="scout", web_search=False, think=False)
    sent = mock_calls()[-1]["body"]
    check("a bot's own instructions become the system prompt", "You are Scout" in sent["system"] and "Today's date" in sent["system"], sent["system"][:80])
    chat(bot_id="byte", web_search=False)
    check("a different bot sends different instructions", "You are Byte" in mock_calls()[-1]["body"]["system"])
    chat(bot_id="nope", web_search=False)
    check("unknown bot falls back to the default personality", "Clawd Bot" in mock_calls()[-1]["body"]["system"])
    chat(bot_id="muse", web_search=False)
    check("bot's model is used when the page doesn't pick one", mock_calls()[-1]["body"]["model"] == "claude-sonnet-5-5")

    new_id = json.loads(call("/api/bots", {}, "POST")[1])["id"]
    mine = {"name": "Chef", "tagline": "Dinner help", "shape": "muse", "color": "#e5566e", "instructions": "You are Chef.",
            "model": "", "web_search": True, "think": False, "starters": ["What can I cook with eggs?", "", "Meal prep ideas"]}
    s, r = call(f"/api/bots/{new_id}", mine, "POST")
    saved_bot = json.loads(r)["bot"]
    check("custom bot created and cleaned", s == 200 and saved_bot["name"] == "Chef" and saved_bot["starters"] == ["What can I cook with eggs?", "Meal prep ideas"])
    check("custom bot listed after the starters", [b["id"] for b in json.loads(call("/api/bots")[1])["bots"]][-1] == new_id)
    check("bad shape rejected", call(f"/api/bots/{new_id}", {**mine, "shape": "dragon"}, "POST")[0] == 400)
    check("bad color rejected", call(f"/api/bots/{new_id}", {**mine, "color": "red"}, "POST")[0] == 400)
    check("empty name rejected", call(f"/api/bots/{new_id}", {**mine, "name": "  "}, "POST")[0] == 400)
    check("bad bot id rejected", call("/api/bots/..%5Cx", mine, "POST")[0] in (400, 404))
    call(f"/api/bots/{new_id}", {**mine, "name": "Chef 2"}, "POST")
    check("editing a bot updates it in place", [b["name"] for b in json.loads(call("/api/bots")[1])["bots"] if b["id"] == new_id] == ["Chef 2"])
    call(f"/api/bots/{new_id}", method="DELETE")
    check("custom bot deleted", new_id not in [b["id"] for b in json.loads(call("/api/bots")[1])["bots"]])
    for b in ["clawd", "scout", "byte", "muse"]:
        call(f"/api/bots/{b}", method="DELETE")
    check("the last bot can't be deleted", call("/api/bots/coach", method="DELETE")[0] == 400 and len(json.loads(call("/api/bots")[1])["bots"]) == 1)
    (Path(home) / "bots.json").unlink()   # back to the starter set for the rest of the run
    check("starters come back if the file is gone", len(json.loads(call("/api/bots")[1])["bots"]) == 5)

    ev = chat(web_search=False, think=False)
    check("plain chat streams text", text_of(ev) == "Hello from plain chat." and ev[-1]["type"] == "done", ev)
    sent = mock_calls()[-1]["body"]
    check("system persona + date, streaming on, default model",
          sent["model"] == "claude-opus-5-5" and sent["stream"] is True and "Clawd Bot" in sent["system"] and "Today's date" in sent["system"])
    check("refusal fallback beta sent for Opus", mock_calls()[-1]["beta"] and "server-side-fallback-2026-06-01" in mock_calls()[-1]["beta"] and sent["fallbacks"] == [{"model": "claude-opus-4-8"}])

    ev = chat(web_search=True, think=True)
    kinds = [e["type"] for e in ev]
    check("Think streams thinking text", "".join(e["text"] for e in ev if e["type"] == "thinking") == "Let me think about it.", kinds)
    check("web search shows the query as a step", {"type": "step", "text": "Searching the web: clawd news"} in ev, ev)
    check("answer text streams", text_of(ev) == "Found it: **42**.")
    sources = next((e["sources"] for e in ev if e["type"] == "sources"), [])
    check("citations used for sources", [s["url"] for s in sources] == ["https://example.com/a"], sources)

    ev = chat(messages=[{"role": "user", "content": "PAUSE please"}], web_search=False)
    check("pause_turn is resumed automatically", text_of(ev) == "Starting the answer ...and the rest.", ev)
    resume = mock_calls()[-1]["body"]["messages"]
    check("resume request carries the assistant turn", resume[-1]["role"] == "assistant")

    ev = chat(messages=[{"role": "user", "content": "REFUSE"}], web_search=False)
    check("refusal is reported plainly", any(e["type"] == "error" and "declined" in e["text"] for e in ev), ev)

    mock_set(reject_fallbacks=True)
    ev = chat(web_search=False)
    check("unsupported fallbacks are dropped and retried", text_of(ev) == "Hello from plain chat." and not any(e["type"] == "error" for e in ev), ev)
    check("retry went out without fallbacks", "fallbacks" not in mock_calls()[-1]["body"])
    mock_set(reject_fallbacks=False)

    ev = chat(model="claude-haiku-4-5", web_search=False)
    check("no fallbacks for Haiku", "fallbacks" not in mock_calls()[-1]["body"] and text_of(ev))
finally:
    server_proc.terminate()

# bad saved key (no env var) -> friendly error; no key -> demo mode
server_proc = start({"ANTHROPIC_API_KEY": ""})
try:
    call("/api/config", {"anthropic_key": "wrong-key"}, "POST")
    ev = chat(web_search=False)
    check("bad key gives a friendly error", ev and ev[0]["type"] == "error" and "rejected the API key" in ev[0]["text"], ev)
    t = json.loads(call("/api/test")[1])
    check("test connection reports the bad key", not t["ok"] and "rejected" in t["error"], t)
    call("/api/config", {"clear_key": True}, "POST")
    ev = chat(messages=[{"role": "user", "content": "hello there"}])
    check("no key falls back to demo mode", "Demo mode" in text_of(ev) and "hello there" in text_of(ev), ev)

    cid = json.loads(call("/api/chats", {}, "POST")[1])["id"]
    saved = {"title": "T", "bot": "scout", "messages": [{"role": "user", "content": "a"},
                                        {"role": "assistant", "content": "b", "thinking": "hmm", "thinkMs": 1200, "steps": ["Searching the web: q"], "sources": []}]}
    call(f"/api/chats/{cid}", saved, "POST")
    listed = json.loads(call("/api/chats")[1])["chats"]
    check("chat saved and listed", [c["id"] for c in listed] == [cid] and listed[0]["title"] == "T")
    back = json.loads(call(f"/api/chats/{cid}")[1])
    check("chat loads back with thinking and steps", back["messages"][1]["thinking"] == "hmm" and back["messages"][1]["steps"] == ["Searching the web: q"])
    check("chat remembers which bot it was with", back["bot"] == "scout" and listed[0]["bot"] == "scout")
    check("bad chat id rejected", call("/api/chats/..%5C..%5Cx")[0] in (400, 404))
    call(f"/api/chats/{cid}", method="DELETE")
    check("chat deleted", json.loads(call("/api/chats")[1])["chats"] == [])

    conn = http.client.HTTPConnection("127.0.0.1", APP, timeout=10)  # a browser reuses one connection
    statuses = []
    for _ in range(3):
        conn.request("POST", "/api/chats", body="{}", headers={"Content-Type": "application/json"})
        r = conn.getresponse(); r.read(); statuses.append(r.status)
        conn.request("GET", "/api/config"); r = conn.getresponse(); r.read(); statuses.append(r.status)
    conn.close()
    check("keep-alive connection survives POSTs with bodies", statuses == [200] * 6, statuses)
    check("foreign Origin blocked", call("/api/config", headers={"Origin": "https://evil.example"})[0] == 403)
    check("foreign Host blocked (DNS rebinding)", call("/api/config", headers={"Host": "evil.example:8791"})[0] == 403)
finally:
    server_proc.terminate()

# ---- the Claude Code engine (every chat is a real Claude Code session) -------------------------
set_cli(True)
server_proc = start({"ANTHROPIC_API_KEY": ""})
try:
    cfg = json.loads(call("/api/config")[1])
    check("signed-in Claude Code is picked automatically, no API key needed",
          cfg["engine_in_use"] == "claude-code" and cfg["ready"] and cfg["claude_code"]["logged_in"] and not cfg["has_key"], cfg)
    t = json.loads(call("/api/test")[1])
    check("test connection reports the Claude Code sign-in", t["ok"] and t["engine"] == "claude-code", t)

    cid = json.loads(call("/api/chats", {}, "POST")[1])["id"]
    first = [{"role": "user", "content": "hi"}]
    ev = chat(messages=first, chat_id=cid, bot_id="scout", model="claude-opus-5-5", web_search=False, think=False)
    check("reply streams through the CLI", text_of(ev) == "Hello from Claude Code." and ev[-1]["type"] == "done", ev)
    call1 = cli_calls()[-1]
    sess = call1["flags"].get("--session-id", "")
    import uuid as _uuid
    check("first message starts a new Claude Code session", bool(_uuid.UUID(sess)) and "--resume" not in call1["flags"], call1["flags"])
    check("bot instructions, model and effort are passed", "You are Scout" in call1["flags"]["--system-prompt"]
          and call1["flags"]["--model"] == "claude-opus-5-5" and call1["flags"]["--effort"] == "low")
    check("no tools when Search is off (chat only)", call1["flags"]["--tools"] == "" and "--allowedTools" not in call1["flags"])
    check("the bot gets its own workspace folder", Path(call1["cwd"]).name == "scout" and Path(call1["cwd"]).parent.name == "workspaces", call1["cwd"])
    check("streaming flags used", "--include-partial-messages" in call1["flags"] and call1["flags"]["--output-format"] == "stream-json")

    second = first + [{"role": "assistant", "content": "Hello from Claude Code."}, {"role": "user", "content": "and again"}]
    ev = chat(messages=second, chat_id=cid, bot_id="scout", web_search=False)
    call2 = cli_calls()[-1]
    check("next message resumes the same session with only the new text",
          call2["flags"].get("--resume") == sess and call2["prompt"] == "and again", call2)

    retry = second  # the page dropped the last answer and re-sent the same turns
    chat(messages=retry, chat_id=cid, bot_id="scout", web_search=False)
    call3 = cli_calls()[-1]
    check("retry starts a fresh session and replays the conversation",
          call3["flags"].get("--session-id") not in (None, sess) and "Assistant: Hello from Claude Code." in call3["prompt"]
          and call3["prompt"].endswith("and again"), call3)

    ev = chat(messages=[{"role": "user", "content": "news?"}], chat_id=json.loads(call("/api/chats", {}, "POST")[1])["id"],
              bot_id="scout", web_search=True, think=True)
    call4 = cli_calls()[-1]
    check("Search turns on only the web tools", call4["flags"]["--tools"] == "WebSearch,WebFetch"
          and call4["flags"]["--allowedTools"] == "WebSearch,WebFetch" and call4["flags"]["--effort"] == "high", call4["flags"])
    check("search step shows the query", {"type": "step", "text": "Searching the web: clawd news"} in ev, ev)
    check("answer + sources from the search results", text_of(ev) == "Found it: **42**." and
          [s["url"] for e in ev if e["type"] == "sources" for s in e["sources"]] == ["https://example.com/a", "https://example.org/b"], ev)

    chat(messages=[{"role": "user", "content": "hi"}], model="claude-haiku-4-5", web_search=False)
    check("Haiku gets no effort flag", "--effort" not in cli_calls()[-1]["flags"])

    long_text = "x" * 7000
    ev = chat(messages=[{"role": "user", "content": long_text}], web_search=False)
    check("long prompts go over stdin intact", cli_calls()[-1]["via"] == "stdin" and cli_calls()[-1]["prompt"] == long_text and text_of(ev))
    ev = chat(messages=[{"role": "user", "content": "NOSTREAM please"}], web_search=False)
    check("falls back to the finished message when no partial events arrive", text_of(ev) == "Hello from Claude Code.", ev)

    # the session vanished on the Claude Code side (deleted): resume fails, so we start fresh
    cid2 = json.loads(call("/api/chats", {}, "POST")[1])["id"]
    chat(messages=[{"role": "user", "content": "one"}], chat_id=cid2, web_search=False)
    Path(cli_dir, "sessions.json").write_text("[]")
    ev = chat(messages=[{"role": "user", "content": "one"}, {"role": "assistant", "content": "a"}, {"role": "user", "content": "two"}],
              chat_id=cid2, web_search=False)
    check("a deleted session is replaced transparently", text_of(ev) == "Hello from Claude Code." and not any(e["type"] == "error" for e in ev), ev)
    check("the replacement carries the history", "User: one" in cli_calls()[-1]["prompt"])

    call("/api/test")   # refresh the server's sign-in cache while still signed in
    set_cli(False)      # then sign out behind its back: the CLI's own "Not logged in" must be handled
    ev = chat(messages=[{"role": "user", "content": "hi"}], web_search=False)
    check("signed-out CLI gives a clear sign-in message, not the raw notice",
          ev and ev[0]["type"] == "error" and "Sign in to Claude Code" in ev[0]["text"] and "Not logged in" not in ev[0]["text"], ev)
    t = json.loads(call("/api/test")[1])
    check("test connection then tells you how to sign in", not t["ok"], t)
    call("/api/config", {"engine": "claude-code"}, "POST")
    ev = chat(messages=[{"role": "user", "content": "hi"}], web_search=False)
    check("forcing Claude Code while signed out falls to demo with guidance", "Demo mode" in text_of(ev) and "Sign in to Claude Code" in text_of(ev), ev)
    started = json.loads(call("/api/signin", {}, "POST")[1])["ok"]
    time.sleep(1.0)   # the login runs as its own process
    check("sign-in button starts Claude Code's own login", started and {"login": ["--claudeai"]} in cli_calls(), cli_calls()[-1])
    t = json.loads(call("/api/test")[1])
    check("after signing in, the app sees it", t["ok"] and t["engine"] == "claude-code", t)
    set_cli(False)   # back to signed out for the remaining checks
    check("bad engine value ignored", json.loads(call("/api/config", {"engine": "nonsense"}, "POST")[1])["engine"] == "claude-code")
finally:
    server_proc.terminate()

# auto: with Claude Code signed out, a saved/env API key takes over
set_cli(False)
server_proc = start({"ANTHROPIC_API_KEY": "test-key"})
try:
    call("/api/config", {"engine": "auto"}, "POST")
    cfg = json.loads(call("/api/config")[1])
    ev = chat(web_search=False)
    check("auto falls back to the API key when Claude Code is signed out",
          cfg["engine_in_use"] == "api" and text_of(ev) == "Hello from plain chat.", (cfg["engine_in_use"], ev))
    call("/api/config", {"engine": "api"}, "POST")
    set_cli(True)
    check("engine can be pinned to the API even when Claude Code is signed in",
          json.loads(call("/api/config")[1])["engine_in_use"] == "api" and text_of(chat(web_search=False)) == "Hello from plain chat.")
finally:
    server_proc.terminate()

mock.terminate()

print(f"\n{'ALL PASSED' if not failures else str(len(failures)) + ' FAILED: ' + ', '.join(failures)}")
sys.exit(1 if failures else 0)
