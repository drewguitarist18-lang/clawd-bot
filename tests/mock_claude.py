"""A stand-in for the `claude` CLI so the Claude Code engine can be tested offline.

It records how it was called, emits the documented `--output-format stream-json` events
(system/init, stream_event with raw API deltas, assistant, user tool_result, result), and replays
the exact "Not logged in" output a real, signed-out CLI produced.

Env: CLAWD_MOCK_STATE (json file: {"logged_in": bool}), CLAWD_MOCK_LOG (jsonl of calls),
     CLAWD_MOCK_SESSIONS (json list of known session ids).
"""
import json
import os
import sys
import uuid
from pathlib import Path

STATE = Path(os.environ["CLAWD_MOCK_STATE"])
LOG = Path(os.environ["CLAWD_MOCK_LOG"])
SESSIONS = Path(os.environ["CLAWD_MOCK_SESSIONS"])


def logged_in():
    try:
        return bool(json.loads(STATE.read_text()).get("logged_in"))
    except (OSError, ValueError):
        return True


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def parse(argv):
    flags, prompt, i = {}, None, 0
    valued = {"--system-prompt", "--model", "--effort", "--tools", "--allowedTools", "--session-id",
              "--resume", "--output-format"}
    while i < len(argv):
        a = argv[i]
        if a == "--":
            prompt = argv[i + 1] if i + 1 < len(argv) else None
            break
        if a in valued:
            flags[a] = argv[i + 1]
            i += 2
        else:
            flags[a] = True
            i += 1
    return flags, prompt


argv = sys.argv[1:]
if argv[:2] == ["auth", "login"]:   # pretend the user finished signing in
    STATE.write_text(json.dumps({"logged_in": True}))
    with LOG.open("a") as f:
        f.write(json.dumps({"login": argv[2:]}) + "\n")
    sys.exit(0)
if argv[:2] == ["auth", "status"]:
    ok = logged_in()
    print(json.dumps({"loggedIn": ok, "authMethod": "claude.ai" if ok else "none", "apiProvider": "firstParty"}))
    sys.exit(0 if ok else 1)

flags, prompt = parse(argv)
via = "arg"
if prompt is None:
    prompt, via = sys.stdin.read(), "stdin"
session = flags.get("--session-id") or flags.get("--resume") or str(uuid.uuid4())
with LOG.open("a") as f:
    f.write(json.dumps({"flags": {k: v for k, v in flags.items()}, "prompt": prompt, "via": via, "cwd": os.getcwd()}) + "\n")

known = json.loads(SESSIONS.read_text()) if SESSIONS.exists() else []
base = {"session_id": session}
out({"type": "system", "subtype": "init", "cwd": os.getcwd(), "session_id": session, "tools": [], "model": flags.get("--model", "?")})

if not logged_in():   # the real signed-out output, shape for shape
    out({"type": "system", "subtype": "status", "status": "requesting", **base})
    out({"type": "assistant", "message": {"model": "<synthetic>", "role": "assistant", "stop_reason": "stop_sequence",
         "content": [{"type": "text", "text": "Not logged in · Please run /login"}]}, **base})
    out({"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in · Please run /login",
         "stop_reason": "stop_sequence", "terminal_reason": "api_error", "total_cost_usd": 0, **base})
    sys.exit(1)

if "--resume" in flags and flags["--resume"] not in known:
    out({"type": "result", "subtype": "error_during_execution", "is_error": True,
         "result": f"No conversation found with session ID: {flags['--resume']}", **base})
    sys.exit(1)
if "--session-id" in flags:
    SESSIONS.write_text(json.dumps(known + [flags["--session-id"]]))


def event(ev):
    out({"type": "stream_event", "event": ev, "parent_tool_use_id": None, **base})


text_chunks = ["Hello from ", "Claude Code."]
tools_on = "WebSearch" in str(flags.get("--tools", ""))
streaming = "--include-partial-messages" in flags and "NOSTREAM" not in prompt

if tools_on:
    if streaming:
        event({"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "WebSearch", "input": {}}})
        for piece in ['{"que', 'ry": "clawd news"}']:
            event({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": piece}})
        event({"type": "content_block_stop", "index": 0})
    out({"type": "assistant", "message": {"model": "claude", "role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_1", "name": "WebSearch", "input": {"query": "clawd news"}}]}, **base})
    out({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": [
        {"type": "text", "text": 'Web search results for query: "clawd news"\n\nLinks: [{"title":"Example A","url":"https://example.com/a"},'
                                 '{"title":"Example B","url":"https://example.org/b"}]\n\nSummary...'}]}]}, **base})
    text_chunks = ["Found it: **", "42**."]

index = 1 if tools_on else 0
if streaming:
    event({"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}})
    for piece in text_chunks:
        event({"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": piece}})
    event({"type": "content_block_stop", "index": index})
full = "".join(text_chunks)
out({"type": "assistant", "message": {"model": "claude", "role": "assistant", "content": [{"type": "text", "text": full}]}, **base})
out({"type": "result", "subtype": "success", "is_error": False, "result": full, "terminal_reason": "completed", **base})
