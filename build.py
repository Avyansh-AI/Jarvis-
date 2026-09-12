#!/usr/bin/env python3
"""
JARVIS - Stage 1: THE GALAXY
============================

Scans every .md file in a notes folder and writes viewer/graph-data.js:

    const GRAPH = { nodes: [...], links: [...] }

Every node gets a numeric `id` equal to its position in the nodes array - the
later stages (chat, fly-to-source, total recall) look nodes up by that index,
so do not change it.

Standard library only. No pip installs.

Usage:
    python3 build.py                     # scan ./notes
    python3 build.py ~/Documents/notes   # scan somewhere else
    python3 build.py ~/notes -o viewer/graph-data.js
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_NOTES_DIR = os.path.join(HERE, "notes")
DEFAULT_OUT = os.path.join(HERE, "viewer", "graph-data.js")

EXCERPT_CHARS = 700
MIN_TITLE_LEN_FOR_MENTION = 4  # ignore titles like "Q3" so we don't link everything

# --- markdown stripping -------------------------------------------------------
FENCE_RE = re.compile(r"```.*?```", re.S)
INLINE_CODE_RE = re.compile(r"`([^`]*)`")
HTML_RE = re.compile(r"<[^>]+>")
IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
WIKILINK_RE = re.compile(r"\[\[([^\]\|#\n]+)(?:[|#][^\]\n]*)?\]\]")
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
BQUOTE_RE = re.compile(r"^\s{0,3}>\s?", re.M)
BULLET_RE = re.compile(r"^\s{0,3}[-*+]\s+", re.M)
RULE_RE = re.compile(r"^\s{0,3}([-*_]\s*){3,}$", re.M)
FRONTMATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.S)
WS_RE = re.compile(r"[ \t]+")
NL_RE = re.compile(r"\n{3,}")

# Words that are too common to be worth matching on.
STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "about", "what",
    "when", "where", "which", "who", "whom", "how", "why", "are", "was", "were",
    "has", "have", "had", "its", "his", "her", "their", "our", "your", "you",
    "can", "could", "would", "should", "does", "did", "done", "get", "got",
    "jarvis", "please", "show", "give", "find", "list",
}


def clean_markdown(raw: str) -> str:
    """Flatten markdown to readable prose for excerpts and keyword scoring."""
    text = FRONTMATTER_RE.sub("", raw)
    text = FENCE_RE.sub(" ", text)
    text = IMAGE_RE.sub(" ", text)
    text = LINK_RE.sub(r"\1", text)
    text = HTML_RE.sub(" ", text)
    text = INLINE_CODE_RE.sub(r"\1", text)
    text = HEADING_RE.sub("", text)
    text = BQUOTE_RE.sub("", text)
    text = BULLET_RE.sub("", text)
    text = RULE_RE.sub(" ", text)
    # keep [[wikilink]] targets as plain words so they read naturally
    text = WIKILINK_RE.sub(r"\1", text)
    text = text.replace("\\", " ")
    text = WS_RE.sub(" ", text)
    text = NL_RE.sub("\n\n", text)
    return text.strip()


def make_excerpt(text: str, limit: int = EXCERPT_CHARS) -> str:
    """~`limit` characters of prose, cut on a word or sentence boundary."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    cut = flat[:limit]
    for boundary in (". ", "! ", "? ", "; ", ", ", " "):
        idx = cut.rfind(boundary)
        if idx > limit * 0.6:
            cut = cut[: idx + 1].rstrip()
            break
    else:
        cut = cut.rstrip()
    return cut.rstrip(",;:. ") + "…"


H1_RE = re.compile(r"^\s{0,3}#\s+(.+?)\s*$", re.M)
TITLE_MD_RE = re.compile(r"[*_`]+")


def title_from_h1(raw: str) -> str:
    """Prefer the note's own '# Title' over its filename, if it has one."""
    m = H1_RE.search(raw or "")
    if not m:
        return ""
    title = WIKILINK_RE.sub(r"\1", LINK_RE.sub(r"\1", m.group(1)))
    title = TITLE_MD_RE.sub("", title).strip(" \t#-")
    return re.sub(r"\s+", " ", title)


def title_from_filename(path: str) -> str:
    stem = os.path.splitext(os.path.basename(path))[0]
    stem = stem.replace("_", " ").replace("-", " ")
    stem = re.sub(r"\s+", " ", stem).strip()
    # 2026-01-04 meeting -> meeting
    stem = re.sub(r"^\d{4}[- ]\d{2}[- ]\d{2}\s*", "", stem)
    return stem or "untitled"


def normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower()).strip()


def stem(word: str) -> str:
    """Very light stemmer, applied to BOTH questions and notes so it stays consistent."""
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def tokenise(text: str) -> list:
    return [stem(t) for t in normalise(text).split()
            if len(t) > 2 and t not in STOPWORDS]


def find_notes(notes_dir: str) -> list:
    """Every .md file under notes_dir, skipping dot-directories."""
    found = []
    for root, dirs, files in os.walk(notes_dir):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            if name.lower().endswith(".md") and not name.startswith("."):
                found.append(os.path.join(root, name))
    return sorted(found)


def read_note(path: str, notes_dir: str) -> dict:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        raw = fh.read()
    body = clean_markdown(raw)
    # the note's own H1 wins; otherwise fall back to a tidy filename
    title = title_from_h1(raw) or title_from_filename(path)
    # drop a leading H1 that just repeats the title
    first, _, rest = body.partition("\n")
    if normalise(first) == normalise(title) and len(first) < 120:
        body = rest.lstrip("\n").strip() or body
    rel = os.path.relpath(path, notes_dir)
    folder = os.path.dirname(rel)
    group = os.path.basename(folder) if folder else "notes"
    return {
        "title": title,
        "label": title,
        "group": group,
        "folder": folder or ".",
        "path": rel.replace(os.sep, "/"),
        "abs_path": path,
        "text": body,
        "excerpt": make_excerpt(body),
        "words": len(body.split()),
        "mtime": int(os.path.getmtime(path)),
        "wikilinks": [w.strip() for w in WIKILINK_RE.findall(raw)],
    }


def build_links(notes: list) -> list:
    """Link notes that share [[wikilinks]] or mention each other's title."""
    by_norm = {}
    for i, note in enumerate(notes):
        by_norm.setdefault(normalise(note["title"]), i)
        by_norm.setdefault(normalise(title_from_filename(note["abs_path"])), i)

    pairs = set()
    for i, note in enumerate(notes):
        # 1. explicit [[wikilinks]]
        for target in note["wikilinks"]:
            j = by_norm.get(normalise(target))
            if j is not None and j != i:
                pairs.add(tuple(sorted((i, j))))

        # 2. one note mentions another note's title
        hay = " " + normalise(note["text"]) + " "
        for j, other in enumerate(notes):
            if i == j:
                continue
            title = normalise(other["title"])
            if len(title) < MIN_TITLE_LEN_FOR_MENTION:
                continue
            if re.search(r"\b" + re.escape(title) + r"\b", hay):
                pairs.add(tuple(sorted((i, j))))

    return [{"source": a, "target": b} for a, b in sorted(pairs)]


def build_graph(notes_dir: str) -> dict:
    if not os.path.isdir(notes_dir):
        raise SystemExit(
            "JARVIS: notes folder not found: %s\n"
            "Create it, or point build.py at your notes: python3 build.py /path/to/notes"
            % notes_dir
        )
    notes = find_notes(notes_dir)
    raw_notes = [read_note(p, notes_dir) for p in notes]
    links = build_links(raw_notes)

    degree = {i: 0 for i in range(len(raw_notes))}
    neighbours = {i: set() for i in range(len(raw_notes))}
    for link in links:
        a, b = link["source"], link["target"]
        degree[a] += 1
        degree[b] += 1
        neighbours[a].add(b)
        neighbours[b].add(a)

    nodes = []
    for i, note in enumerate(raw_notes):
        nodes.append(
            {
                # Stage 2+ look nodes up by this index. Keep id == array position.
                "id": i,
                "label": note["title"],
                "group": note["group"],
                "folder": note["folder"],
                "path": note["path"],
                "excerpt": note["excerpt"],
                "words": note["words"],
                "mtime": note["mtime"],
                "degree": degree[i],
                "neighbors": sorted(neighbours[i]),
                "text": note["text"],
            }
        )

    return {
        "notes_dir": os.path.abspath(notes_dir),
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "nodes": nodes,
        "links": links,
    }


def write_graph_data(graph: dict, out_path: str) -> None:
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    payload = json.dumps(graph, indent=2, ensure_ascii=False)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("// JARVIS knowledge galaxy - generated by build.py, do not edit.\n")
        fh.write(
            "// source: %s | notes: %d | links: %d | built: %s\n"
            % (graph["notes_dir"], len(graph["nodes"]), len(graph["links"]), graph["generated"])
        )
        fh.write("const GRAPH = ")
        fh.write(payload)
        fh.write(";\n\nif (typeof window !== 'undefined') { window.GRAPH = GRAPH; }\n")
    print(
        "JARVIS: indexed %d note(s) and %d link(s) -> %s"
        % (len(graph["nodes"]), len(graph["links"]), out_path)
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build the JARVIS knowledge graph.")
    parser.add_argument("notes_dir", nargs="?", default=DEFAULT_NOTES_DIR,
                        help="folder of .md notes (default: ./notes)")
    parser.add_argument("-o", "--out", default=DEFAULT_OUT,
                        help="output js file (default: viewer/graph-data.js)")
    args = parser.parse_args(argv)

    notes_dir = os.path.abspath(os.path.expanduser(args.notes_dir))
    graph = build_graph(notes_dir)
    write_graph_data(graph, os.path.abspath(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
