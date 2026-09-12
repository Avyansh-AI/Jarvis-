#!/usr/bin/env python3
"""
JARVIS - web server + brain.

Serves ONLY the viewer/ folder on port 4700 and exposes the chat brain:

    POST /chat   {"question": "...", "session": "..."}
                 -> {"answer": "...", "nodes": [0, 3, 7], ...}

The Anthropic API key is read from config.json in the PROJECT ROOT, which is
git-ignored and lives outside viewer/, so the browser can never reach it.
If no key is configured, /chat falls back to the `claude -p` CLI.

    python3 server.py          # then open http://localhost:4700

Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections import OrderedDict
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build  # noqa: E402  (reuse the scanner from Stage 1)

HERE = os.path.dirname(os.path.abspath(__file__))
VIEWER_DIR = os.path.join(HERE, "viewer")
GRAPH_DATA_JS = os.path.join(VIEWER_DIR, "graph-data.js")
CONFIG_PATH = os.path.join(HERE, "config.json")

PORT = int(os.environ.get("JARVIS_PORT", "4700"))
DEFAULT_NOTES_DIR = os.path.join(HERE, "notes")

# --- tuning ------------------------------------------------------------------
TOP_K = 6              # how many notes get sent to the model
MAX_NOTE_CHARS = 2200  # per note, in the prompt
MAX_TOKENS = 500
HISTORY_TURNS = 8      # user+assistant pairs kept per session
MIN_SCORE = 2.0        # below this the question is treated as small talk
REL_CUTOFF = 0.4       # keep notes scoring at least this fraction of the best hit
SESSION_CAP = 40

DEFAULT_CONFIG = {
    "api_key": "PUT-YOUR-KEY-HERE",
    "model": "claude-opus-4-8",
    "notes_dir": "",
}

SYSTEM_PROMPT = """\
You are JARVIS, an assistant that answers questions using only the user's own notes.

Rules:
- Answer ONLY from the notes supplied in the user's message. Do not use outside knowledge.
- Keep it to 2-3 short sentences. No preamble, no restating the question.
- Do NOT recite or quote the note back - the relevant note is already open on screen.
- Cite which notes you used by referring to them naturally (e.g. "your note on X says...").
- If the supplied notes do not cover the question, say so plainly in one sentence and
  do not guess. Admitting ignorance is always better than inventing a detail.
- If no notes were supplied at all, the user is making small talk: reply briefly and
  in character, and do not pretend to have read anything.
"""


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
def ensure_config() -> dict:
    """Create config.json with placeholders if it doesn't exist yet."""
    if not os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump(DEFAULT_CONFIG, fh, indent=2)
                fh.write("\n")
            print("JARVIS: created %s - paste your Anthropic API key in there." % CONFIG_PATH)
        except OSError as exc:  # read-only disk, etc.
            print("JARVIS: could not write config.json (%s)" % exc, file=sys.stderr)
        return dict(DEFAULT_CONFIG)
    return load_config()


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    except (OSError, ValueError) as exc:
        print("JARVIS: config.json unreadable (%s) - using defaults." % exc, file=sys.stderr)
    return cfg


def has_api_key(cfg: dict) -> bool:
    key = str(cfg.get("api_key") or "").strip()
    return bool(key) and not key.upper().startswith("PUT-YOUR")


def notes_dir(cfg: dict) -> str:
    raw = cfg.get("notes_dir") or os.environ.get("JARVIS_NOTES") or DEFAULT_NOTES_DIR
    return os.path.abspath(os.path.expanduser(str(raw)))


# ---------------------------------------------------------------------------
# the graph (notes) - kept in memory, refreshed when files change
# ---------------------------------------------------------------------------
_GRAPH_CACHE = {"graph": None, "stamp": 0.0}


def newest_mtime(root: str) -> float:
    newest = 0.0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.lower().endswith(".md"):
                try:
                    newest = max(newest, os.path.getmtime(os.path.join(dirpath, name)))
                except OSError:
                    pass
    return newest


def graph_from_js() -> dict:
    """Last resort: read viewer/graph-data.js straight off disk."""
    try:
        with open(GRAPH_DATA_JS, "r", encoding="utf-8") as fh:
            text = fh.read()
        start = text.index("const GRAPH = ") + len("const GRAPH = ")
        end = text.rindex("};") + 1
        return json.loads(text[start:end])
    except Exception:
        return {"nodes": [], "links": []}


def load_graph(force: bool = False) -> dict:
    """Build (or rebuild) the in-memory index of notes."""
    cfg = load_config()
    root = notes_dir(cfg)
    stamp = newest_mtime(root) if os.path.isdir(root) else 0.0

    if force or _GRAPH_CACHE["graph"] is None or stamp > _GRAPH_CACHE["stamp"]:
        if os.path.isdir(root):
            graph = build.build_graph(root)
            build.write_graph_data(graph, GRAPH_DATA_JS)
            _GRAPH_CACHE["graph"] = graph
            _GRAPH_CACHE["stamp"] = max(stamp, time.time())
        else:
            print("JARVIS: notes folder not found: %s" % root, file=sys.stderr)
            _GRAPH_CACHE["graph"] = graph_from_js()
            _GRAPH_CACHE["stamp"] = stamp
    return _GRAPH_CACHE["graph"]


# ---------------------------------------------------------------------------
# retrieval - keyword overlap, title matches weigh extra
# ---------------------------------------------------------------------------
SMALLTALK_RE = re.compile(
    r"^\s*(hi|hey|hello|yo|hiya|good\s+(morning|afternoon|evening|night)|morning|"
    r"thanks|thank\s+you|cheers|ta|bye|goodbye|see\s+you|how\s+are\s+you|"
    r"how'?s\s+it\s+going|what'?s\s+up|sup|who\s+are\s+you|what\s+can\s+you\s+do|help)\b",
    re.I,
)


def is_smalltalk(question: str) -> bool:
    return bool(SMALLTALK_RE.match(question or ""))


def rank_notes(question: str, nodes: list) -> list:
    """Return [(node_index, score), ...] best first."""
    q_tokens = build.tokenise(question)
    if not q_tokens:
        return []
    q_norm = " " + build.normalise(question) + " "
    scored = []
    for node in nodes:
        text = node.get("text") or node.get("excerpt") or ""
        body_tokens = build.tokenise(text)[:900]
        if not body_tokens:
            continue
        counts = {}
        for tok in body_tokens:
            counts[tok] = counts.get(tok, 0) + 1
        score = 0.0
        for tok in q_tokens:
            hit = counts.get(tok, 0)
            if hit:
                # saturating term frequency: repeating a word helps, a little
                score += 1.0 + min(1.5, 0.25 * (hit - 1))
        # title matches weigh extra
        label = node.get("label") or ""
        label_norm = build.normalise(label)
        label_tokens = set(label_norm.split())
        for tok in set(q_tokens):
            if tok in label_tokens:
                score += 3.0
        if label_norm and len(label_norm) >= 4 and label_norm in q_norm:
            score += 6.0  # the exact title was spoken
        if score > 0:
            scored.append((node["id"], round(score, 2)))
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return scored


def note_context(node: dict) -> str:
    text = (node.get("text") or node.get("excerpt") or "").strip()
    if len(text) > MAX_NOTE_CHARS:
        text = text[:MAX_NOTE_CHARS].rsplit(" ", 1)[0] + "…"
    return "[%d] %s  (folder: %s)\n%s" % (node["id"], node.get("label"), node.get("group"), text)


# ---------------------------------------------------------------------------
# the model
# ---------------------------------------------------------------------------
def call_anthropic(system: str, messages: list, cfg: dict) -> str:
    model = cfg.get("model") or DEFAULT_CONFIG["model"]
    payload = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": system,
        "messages": messages,
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "content-type": "application/json",
            "x-api-key": str(cfg.get("api_key") or ""),
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    return "".join(parts).strip()


def call_claude_cli(system: str, messages: list, cfg: dict) -> str:
    """Fallback for when there is no API key: shell out to `claude -p`."""
    convo = []
    for msg in messages:
        prefix = "User" if msg.get("role") == "user" else "Assistant"
        convo.append("%s: %s" % (prefix, msg.get("content", "")))
    prompt = system.strip() + "\n\n" + "\n\n".join(convo) + "\n\nAssistant:"
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, timeout=180,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "No API key in config.json, and the `claude` CLI is not installed. "
            "Paste your Anthropic API key into config.json, or install Claude Code."
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("`claude -p` timed out.")
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 or not out:
        err = (proc.stderr or "").strip()
        raise RuntimeError("`claude -p` failed: %s" % (err or "no output"))
    return out


def answer_question(system: str, messages: list, cfg: dict) -> tuple:
    """-> (answer_text, source_label)"""
    if has_api_key(cfg):
        try:
            return call_anthropic(system, messages, cfg), "anthropic"
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:400]
            except Exception:
                pass
            raise RuntimeError("Anthropic API error %s: %s" % (exc.code, detail or exc.reason))
        except urllib.error.URLError as exc:
            raise RuntimeError("Could not reach the Anthropic API (%s)." % exc.reason)
    return call_claude_cli(system, messages, cfg), "claude-cli"


# ---------------------------------------------------------------------------
# sessions - short per-session memory so follow-ups work
# ---------------------------------------------------------------------------
SESSIONS = OrderedDict()


def get_session(sid: str) -> tuple:
    if not sid or sid not in SESSIONS:
        sid = sid if sid and re.fullmatch(r"[A-Za-z0-9_-]{6,64}", sid) else uuid.uuid4().hex
        SESSIONS[sid] = {"history": [], "touched": time.time()}
    session = SESSIONS[sid]
    session["touched"] = time.time()
    SESSIONS.move_to_end(sid)
    while len(SESSIONS) > SESSION_CAP:
        SESSIONS.popitem(last=False)
    return sid, session


def remember_turn(session: dict, question: str, answer: str) -> None:
    session["history"].append({"role": "user", "content": question})
    session["history"].append({"role": "assistant", "content": answer})
    session["history"] = session["history"][-(HISTORY_TURNS * 2):]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class JarvisHandler(SimpleHTTPRequestHandler):
    server_version = "JARVIS/1.0"

    def __init__(self, *args, **kwargs):
        kwargs["directory"] = VIEWER_DIR
        super().__init__(*args, **kwargs)

    # -- security: never serve anything outside viewer/ -----------------------
    def translate_path(self, path):
        local = os.path.realpath(super().translate_path(path))
        root = os.path.realpath(VIEWER_DIR)
        if local != root and not local.startswith(root + os.sep):
            return os.path.join(root, "__forbidden__")
        return local

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def log_message(self, fmt, *args):
        sys.stderr.write("JARVIS %s - %s\n" % (self.address_string(), fmt % args))

    # -- chat ------------------------------------------------------------------
    def do_POST(self):
        route = self.path.split("?")[0].rstrip("/")
        if route == "/chat":
            self.handle_chat()
        else:
            self.send_json(404, {"error": "Not found: %s" % route})

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def handle_chat(self) -> None:
        cfg = load_config()
        payload = self.read_json()
        question = str(payload.get("question") or "").strip()
        if not question:
            self.send_json(400, {"error": "Ask me something."})
            return
        if len(question) > 2000:
            question = question[:2000]

        sid, session = get_session(str(payload.get("session") or ""))
        graph = load_graph()
        nodes = graph.get("nodes", [])

        # 1. retrieve
        ranked = rank_notes(question, nodes)
        top = ranked[:TOP_K]
        best = top[0][1] if top else 0.0
        strong = [pair for pair in top
                  if pair[1] >= MIN_SCORE and pair[1] >= REL_CUTOFF * best]
        topic = "chat" if (is_smalltalk(question) or not strong) else "notes"
        if topic == "chat":
            strong = []
        used_ids = [idx for idx, _ in (strong or [])]

        # 2. build the prompt
        if used_ids:
            blocks = "\n\n---\n\n".join(note_context(nodes[i]) for i in used_ids)
            user_turn = (
                "%s\n\nHere are the only notes available to you:\n\n%s\n\n"
                "Answer from these notes only, in 2-3 sentences."
                % (question, blocks)
            )
        else:
            user_turn = (
                "%s\n\n(No notes matched this, so it is small talk or something I "
                "have no notes on. Reply briefly and in character; do not invent "
                "note contents.)" % question
            )

        messages = list(session["history"]) + [{"role": "user", "content": user_turn}]

        # 3. ask the model
        try:
            answer, source = answer_question(SYSTEM_PROMPT, messages, cfg)
        except RuntimeError as exc:
            self.send_json(200, {
                "answer": "Apologies, sir - something went wrong: %s" % exc,
                "nodes": [],
                "session": sid,
                "topic": "error",
                "source": "error",
                "error": str(exc),
            })
            return
        except Exception as exc:  # never crash the server on a bad key / bad JSON
            self.send_json(200, {
                "answer": "Apologies, sir - an unexpected fault: %s" % exc,
                "nodes": [],
                "session": sid,
                "topic": "error",
                "source": "error",
                "error": str(exc),
            })
            return

        remember_turn(session, question, answer)
        self.send_json(200, {
            "answer": answer,
            "nodes": used_ids,
            "titles": [nodes[i].get("label") for i in used_ids],
            "scores": [score for _, score in (strong or [])],
            "session": sid,
            "topic": topic,
            "source": source,
            "notes": len(nodes),
        })


def main() -> int:
    if not os.path.isdir(VIEWER_DIR):
        print("JARVIS: viewer/ folder missing. Run: python3 build.py", file=sys.stderr)
        return 1
    cfg = ensure_config()
    graph = load_graph()
    mode = "Anthropic API (%s)" % cfg.get("model") if has_api_key(cfg) else "`claude -p` CLI"

    print("JARVIS online -> http://localhost:%d" % PORT)
    print("  serving : %s  (only this folder is reachable)" % VIEWER_DIR)
    print("  notes   : %s  (%d indexed)" % (notes_dir(cfg), len(graph.get("nodes", []))))
    print("  brain   : %s" % mode)
    if not has_api_key(cfg):
        print("            (paste your key into config.json to use the API instead)")
    print("Ctrl+C to stop.")

    ThreadingHTTPServer.allow_reuse_address = True
    with ThreadingHTTPServer(("0.0.0.0", PORT), JarvisHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nJARVIS: shutting down.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
