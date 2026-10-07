"""A tiny stand-in for api.anthropic.com so Clawd Bot's real code paths run offline.

It emits the documented Messages streaming events (message_start, content_block_*,
thinking/text/input_json/citations deltas, server_tool_use + web_search_tool_result,
message_delta, message_stop), and the SDK parses them, so shape mistakes show up.
Key accepted: "test-key". Anything else gets a 401.

Control: POST /_config {"reject_fallbacks": true}; GET /_last for what was received.
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
CALLS = []                  # every /v1/messages request body, newest last
SETTINGS = {"reject_fallbacks": False}


def event(kind, **fields):
    return kind, {"type": kind, **fields}


def message_start(model):
    return event("message_start", message={
        "id": "msg_mock", "type": "message", "role": "assistant", "model": model, "content": [],
        "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 1}})


def text_block(index, pieces, citation=None):
    yield event("content_block_start", index=index, content_block={"type": "text", "text": ""})
    for i, piece in enumerate(pieces):
        yield event("content_block_delta", index=index, delta={"type": "text_delta", "text": piece})
        if citation and i == 0:
            yield event("content_block_delta", index=index, delta={"type": "citations_delta", "citation": citation})
    yield event("content_block_stop", index=index)


def finish(stop_reason):
    yield event("message_delta", delta={"stop_reason": stop_reason, "stop_sequence": None}, usage={"output_tokens": 12})
    yield event("message_stop")


def reply_events(body):
    """Build the event stream for one request, based on what it asked for."""
    model = body["model"]
    last = body["messages"][-1]
    last_text = last["content"] if isinstance(last["content"], str) else ""
    tools = [t["type"] for t in body.get("tools", [])]
    thinking = (body.get("thinking") or {}).get("type")
    yield message_start(model)
    index = 0

    if last["role"] == "assistant":  # resuming after pause_turn
        yield from text_block(index, [" ...and the rest."])
        yield from finish("end_turn")
        return
    if "REFUSE" in last_text:
        yield from finish("refusal")
        return
    if thinking in ("adaptive", "enabled"):
        yield event("content_block_start", index=index, content_block={"type": "thinking", "thinking": "", "signature": ""})
        for piece in ["Let me ", "think about it."]:
            yield event("content_block_delta", index=index, delta={"type": "thinking_delta", "thinking": piece})
        yield event("content_block_delta", index=index, delta={"type": "signature_delta", "signature": "sig"})
        yield event("content_block_stop", index=index)
        index += 1
    if tools:
        yield event("content_block_start", index=index, content_block={
            "type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {}})
        for piece in ['{"que', 'ry": "clawd news"}']:
            yield event("content_block_delta", index=index, delta={"type": "input_json_delta", "partial_json": piece})
        yield event("content_block_stop", index=index)
        index += 1
        yield event("content_block_start", index=index, content_block={
            "type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": [
                {"type": "web_search_result", "url": "https://example.com/a", "title": "Example A",
                 "encrypted_content": "x", "page_age": None},
                {"type": "web_search_result", "url": "https://example.org/b", "title": "Example B",
                 "encrypted_content": "x", "page_age": None}]})
        yield event("content_block_stop", index=index)
        index += 1
        citation = {"type": "web_search_result_location", "url": "https://example.com/a",
                    "title": "Example A", "encrypted_index": "x", "cited_text": "..."}
        yield from text_block(index, ["Found it: **", "42**."], citation)
        yield from finish("end_turn")
    elif "PAUSE" in last_text:
        yield from text_block(index, ["Starting the answer"])
        yield from finish("pause_turn")
    else:
        yield from text_block(index, ["Hello", " from", " plain", " chat."])
        yield from finish("end_turn")


class Mock(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def handle(self):
        try:
            super().handle()
        except ConnectionError:
            pass  # the client closed a keep-alive connection; nothing to report

    def json_reply(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authed(self):
        if self.headers.get("x-api-key") == "test-key":
            return True
        self.json_reply({"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}, 401)
        return False

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/_last":
            return self.json_reply(CALLS)
        if not self.authed():
            return
        if path == "/v1/models":
            return self.json_reply({"data": [
                {"id": m, "type": "model", "display_name": m, "created_at": "2026-01-01T00:00:00Z"}
                for m in ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5", "some-other-model"]],
                "has_more": False, "first_id": "a", "last_id": "b"})
        self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        path = self.path.split("?")[0]
        if path == "/_config":
            SETTINGS.update(json.loads(raw))
            return self.json_reply(SETTINGS)
        if not self.authed():
            return
        if path != "/v1/messages":
            return self.send_error(404)
        body = json.loads(raw)
        CALLS.append({"body": body, "beta": self.headers.get("anthropic-beta")})
        if body.get("fallbacks") and SETTINGS["reject_fallbacks"]:
            return self.json_reply({"type": "error", "error": {
                "type": "invalid_request_error", "message": "fallbacks is not supported for this model"}}, 400)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for kind, data in reply_events(body):
            self.wfile.write(f"event: {kind}\ndata: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()
        self.close_connection = True


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Mock).serve_forever()
