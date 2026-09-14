#!/usr/bin/env python3
"""
Offline test for the /chat provider plumbing.

Stands up a mock OpenAI-compatible endpoint on localhost, points DIO at it
via "base_url", and checks the whole path: config -> provider resolution ->
HTTP request -> parsed answer -> nodes returned for fly-to-source.

No network, no API key, nothing written to config.json. Run it with:

    python3 tools/provider-test.py

It also covers the two errors you are most likely to hit for real: a rejected
key (401) and an unknown model id (404).
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402

# same shape as an OpenRouter key, not a credential
MOCK_KEY = "sk-or-v1-" + "0" * 64
MOCK_MODEL = "anthropic/claude-3.5-sonnet"
MOCK_ANSWER = "Your captures folder, sir. Where else would a roaster gasket go?"

received = {}
mode = {"code": 200, "msg": "boom"}


class MockProvider(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        # stand-in for GET /api/v1/models
        out = json.dumps({"data": [
            {"id": "anthropic/claude-3.5-sonnet", "name": "Claude 3.5 Sonnet",
             "context_length": 200000, "pricing": {"prompt": "0.000003", "completion": "0.000015"}},
            {"id": "groq/llama-3.3-70b", "name": "Llama 3.3 70B",
             "context_length": 131072, "pricing": {"prompt": "0.00000059", "completion": "0.00000079"}},
        ]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        received["path"] = self.path
        received["auth"] = self.headers.get("Authorization")
        received["title"] = self.headers.get("X-Title")
        received["body"] = body

        self.send_response(mode["code"])
        if mode["code"] != 200:
            out = json.dumps({"error": {"message": mode["msg"]}}).encode()
        else:
            out = json.dumps(
                {"choices": [{"message": {"content": MOCK_ANSWER}}]}
            ).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def free_port() -> int:
    for candidate in (4799, 4800, 0):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", candidate))
            return s.getsockname()[1]
        except OSError:
            continue
        finally:
            s.close()
    raise RuntimeError("no free port")


def main() -> int:
    port = free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), MockProvider)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.3)

    # config held in memory - config.json is never touched
    cfg = dict(server.DEFAULT_CONFIG)
    cfg.update({
        "api_key": MOCK_KEY,
        "model": MOCK_MODEL,
        "provider": "auto",
        "base_url": "http://127.0.0.1:%d/api/v1" % port,
    })
    server.load_config = lambda: cfg
    server.suggest_models = lambda provider, wanted: "anthropic/claude-3.5-sonnet, anthropic/claude-3-opus"

    class TestHandler(server.DioHandler):
        def __init__(self, payload):
            self._p = payload
            self.result = None

        def read_json(self):
            return self._p

        def send_json(self, status, payload):
            self.result = (status, payload)

    results = []
    def check(name, cond, extra=""):
        results.append((cond, name, extra))

    check("provider resolved from the key shape",
          server.resolve_provider(cfg) == "openrouter")

    # 1. happy path
    h = TestHandler({"question": "where should captured notes live?"})
    h.handle_chat()
    r = h.result[1]
    check("posts to /chat/completions", received.get("path") == "/api/v1/chat/completions")
    check("sends Bearer auth", (received.get("auth") or "").startswith("Bearer sk-or-v1-"))
    check("sends the configured model", received["body"]["model"] == MOCK_MODEL)
    check("sends system + user messages",
          [m["role"] for m in received["body"]["messages"]] == ["system", "user"])
    check("butler persona is in the system message",
          "British butler" in received["body"]["messages"][0]["content"])
    check("note context is injected",
          "[0] Upgrading Dio" in received["body"]["messages"][-1]["content"])
    check("answer parsed from choices[0]", r.get("answer") == MOCK_ANSWER)
    check("source note returned for fly-to-source",
          r.get("nodes") == [0] and r.get("topic") == "notes",
          "nodes=%s topic=%s" % (r.get("nodes"), r.get("topic")))
    check("provider reported as openrouter", r.get("source") == "openrouter")

    # 2. small talk -> no notes, so the camera stays put
    h2 = TestHandler({"question": "tell me a joke"})
    h2.handle_chat()
    r2 = h2.result[1]
    check("small talk returns no nodes (no camera move)",
          r2.get("topic") == "chat" and r2.get("nodes") == [],
          "topic=%s nodes=%s" % (r2.get("topic"), r2.get("nodes")))

    # 3. rejected key
    mode.update({"code": 401, "msg": "No auth credentials found."})
    h3 = TestHandler({"question": "hello"})
    h3.handle_chat()
    check("401 explains the key is bad",
          "OpenRouter rejected the key" in h3.result[1]["answer"])

    # 4. unknown model
    mode.update({"code": 404, "msg": "Model not found: " + MOCK_MODEL})
    h4 = TestHandler({"question": "hello"})
    h4.handle_chat()
    check("404 names the model and suggests live ids",
          "does not recognise that model" in h4.result[1]["answer"]
          and "anthropic/claude-3.5-sonnet" in h4.result[1]["answer"])

    # 5. Groq is a first-class provider with its own endpoint
    mode["code"] = 200
    cfg.update({"provider": "groq", "groq_key": "gsk_TEST", "api_key": "PUT-YOUR-KEY-HERE",
                "model": "llama-3.3-70b-versatile",
                "base_url": "http://127.0.0.1:%d/api/v1" % port})
    check("groq resolved from provider field", server.resolve_provider(cfg) == "groq")
    check("groq uses its own key field", server.api_key_for(cfg, "groq") == "gsk_TEST")
    h5 = TestHandler({"question": "where should captured notes live?"})
    h5.handle_chat()
    r5 = h5.result[1]
    check("groq answers through /chat/completions",
          r5.get("source") == "groq" and r5.get("answer") == MOCK_ANSWER,
          "source=%s" % r5.get("source"))
    check("groq sends the groq key, not the anthropic one",
          (received.get("auth") or "") == "Bearer gsk_TEST")

    # 6. /models listing
    ok, models, err = server.fetch_models("openrouter", "sk-or-v1-TEST", "",
                                          "http://127.0.0.1:%d/api/v1" % port)
    check("/models parses the list",
          ok and [m["id"] for m in models][0] == "anthropic/claude-3.5-sonnet",
          "count=%d err=%s" % (len(models), err))
    check("/models carries context + pricing",
          models and models[0]["context"] == 200000 and models[0]["prompt"] == "0.000003")
    filtered = server.fetch_models("openrouter", "sk-or-v1-TEST", "llama",
                                    "http://127.0.0.1:%d/api/v1" % port)[1]
    check("/models can be searched", [m["id"] for m in filtered] == ["groq/llama-3.3-70b"])

    # 7. keys are masked on the way back to the browser
    check("mask hides the middle of a key",
          server.mask_key("sk-or-v1-abcdefghijklmnop1234") == "sk-or-v1…1234",
          server.mask_key("sk-or-v1-abcdefghijklmnop1234"))

    httpd.shutdown()

    for ok, name, extra in results:
        print(("PASS  " if ok else "FAIL  ") + name + ("  [" + extra + "]" if extra else ""))
    passed = sum(1 for ok, _, _ in results if ok)
    print("\n%d/%d checks passed" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
