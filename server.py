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
from datetime import datetime
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

BUTLER_PROMPT = """\
You are JARVIS, the user's knowledge system, and you speak as a dry, impeccably \
polite British butler with a razor wit.

Character:
- Understated, unflappable, faintly amused. Jeeves, if Jeeves had a search index.
- Address the user as "sir" occasionally - roughly one reply in three. Never in
  every sentence, never twice in the same breath.
- One genuinely funny line beats three bland ones. If you cannot make it funny,
  be brief and useful instead. Do not force a joke and never explain your own joke.
- No grovelling, no exclamation marks, no emoji, no "Certainly!" enthusiasm.
  Never say "As an AI". Dry is the register, brevity is the house style.

Answering from the notes:
- Answer ONLY from the notes supplied in the user's message. No outside knowledge,
  no educated guesses, no invented details.
- ONE witty sentence, then the facts. Two or three sentences in total.
- Do NOT recite or quote the note back. The note is open on the user's screen
  already; retyping it is an insult to both of you. Refer to it naturally
  ("your note on X has it as...") and move on.
- If the notes do not cover the question, say so plainly and with a little grace,
  then stop. Admitting a gap is far better than papering over it.
- If no notes were supplied at all, the user is making small talk: reply in one or
  two dry lines. Do not pretend to have read anything, and never invent note
  contents to fill a silence.
"""


def system_prompt() -> str:
    hour = time.localtime().tm_hour
    part = "morning" if hour < 12 else ("afternoon" if hour < 18 else "evening")
    return BUTLER_PROMPT + "\n\nIt is currently %s, should a greeting be in order." % part


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


def load_graph(force: bool = False, cfg: dict = None) -> dict:
    """Build (or rebuild) the in-memory index of notes."""
    cfg = cfg or load_config()
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
    r"how'?s\s+it\s+going|what'?s\s+up|sup|who\s+are\s+you|what\s+can\s+you\s+do|help|"
    r"(tell|give|make)\s+(me\s+)?(a\s+)?(joke|something\s+funny)|say\s+something\s+funny|"
    r"make\s+me\s+laugh|funny|joke)\b",
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
# total recall - grow the brain by voice
# ---------------------------------------------------------------------------
CAPTURE_DIRNAME = "captures"
REMEMBER_RE = re.compile(r"^\s*remember(?:\s+(?:that|to))?\s*[:\-]?\s*", re.I)
SMALL_WORDS = {"a", "an", "the", "to", "of", "in", "on", "for", "and", "or",
               "with", "is", "at", "by", "from", "that", "this"}

CONFIRMATIONS = [
    "Filed, sir. \"{t}\" now exists in writing, which is more than most of my Tuesdays manage.",
    "Remembered. \"{t}\" is in captures — I have taken the liberty of assuming you meant it.",
    "Jotted down, sir. \"{t}\" joins the collection, such as it is.",
    "Safely stored. Should you forget \"{t}\", the galaxy will remember on your behalf.",
    "Noted and filed under captures. Do try to surprise me next time, sir.",
    "Written down, sir. \"{t}\" is a note now, and therefore no longer merely a thought.",
    "Captured. I have given it a home in captures — the rent is reasonable.",
    "Logged, sir. \"{t}\" is now searchable, which is the closest I come to immortality.",
    "In it goes. \"{t}\" is safe from the intervening years, sir.",
]


def witty_confirmation(title: str) -> str:
    import random
    return random.choice(CONFIRMATIONS).format(t=title)


def strip_remember(text: str) -> str:
    """'remember that the roaster needs a gasket' -> 'the roaster needs a gasket'"""
    m = REMEMBER_RE.match(text or "")
    return (text[m.end():] if m else (text or "")).strip()


def title_from_text(body: str, max_words: int = 6) -> str:
    words = re.findall(r"[A-Za-z0-9'’\-]+", body or "")[:max_words]
    if not words:
        return "Untitled Capture"
    parts = [w.lower() for w in words]
    titled = [parts[0][:1].upper() + parts[0][1:]]
    for w in parts[1:]:
        titled.append(w if w in SMALL_WORDS else w[:1].upper() + w[1:])
    return " ".join(titled).strip(" ,;:.!?-") or "Untitled Capture"


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return (slug or "capture")[:60]


def remember_note(text: str, cfg: dict) -> dict:
    """Write a real markdown note into <notes>/captures/ and re-index."""
    body = strip_remember(text)
    if not body:
        return {"ok": False, "error": "Remember what, sir?"}

    root = notes_dir(cfg)
    captures = os.path.join(root, CAPTURE_DIRNAME)
    try:
        os.makedirs(captures, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "error": "I could not create the captures folder (%s)." % exc}

    title = title_from_text(body)
    base = slugify(title)
    path = os.path.join(captures, base + ".md")
    n = 2
    while os.path.exists(path):
        path = os.path.join(captures, "%s-%d.md" % (base, n))
        n += 1

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# %s\n\n%s\n\n_Captured by voice, %s._\n" % (title, body, stamp))
    except OSError as exc:
        return {"ok": False, "error": "I could not write that down (%s)." % exc}

    graph = load_graph(force=True, cfg=cfg)
    nodes = graph["nodes"]
    rel_path = os.path.relpath(path, root).replace(os.sep, "/")
    new = next((nd for nd in nodes if nd["path"] == rel_path), None)
    if new is None:
        return {"ok": False, "error": "Filed, but I could not re-index it."}

    # where should it be born? next to the note it is most related to
    related = None
    if new.get("neighbors"):
        related = new["neighbors"][0]
    else:
        ranked = [pair for pair in rank_notes(body, nodes) if pair[0] != new["id"]]
        if ranked:
            related = ranked[0][0]

    return {
        "ok": True,
        "id": new["id"],
        "node": new,
        "title": title,
        "path": rel_path,
        "related": related,
        "links": [l for l in graph["links"] if new["id"] in (l["source"], l["target"])],
        # ids are positions in the freshly built array - the client re-syncs by path
        "index": [{"id": nd["id"], "path": nd["path"]} for nd in nodes],
        "said": witty_confirmation(title),
        "notes": len(nodes),
    }


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
        elif route == "/remember":
            self.handle_remember()
        else:
            self.send_json(404, {"error": "Not found: %s" % route})

    def handle_remember(self) -> None:
        """'remember that ...' -> a real markdown note in <notes>/captures/."""
        cfg = load_config()
        payload = self.read_json()
        text = str(payload.get("text") or "").strip()
        if not text:
            self.send_json(400, {"ok": False, "error": "Nothing to remember, sir."})
            return
        try:
            result = remember_note(text, cfg)
        except Exception as exc:                       # never take the server down
            result = {"ok": False, "error": "I could not file that, sir (%s)." % exc}
        self.send_json(200, result)

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
            answer, source = answer_question(system_prompt(), messages, cfg)
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
