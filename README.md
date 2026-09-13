# JARVIS

An interactive 3D knowledge galaxy of your markdown notes, with a talking AI
brain, a voice, and the personality of a dry British butler.

Every note is a star. Folders are colours. Notes that mention each other are
joined by a thread of light. Ask a question and Jarvis answers **only** from
your notes, says it out loud, and flies the camera to the exact note it used so
you can see the receipt.

Standard library Python, a single HTML file, and a CDN script tag. No npm, no
build step, no framework.

---

## Quick start

```bash
git clone https://github.com/Avyansh-AI/Jarvis-.git
cd Jarvis-

# 1. paste your Anthropic API key into config.json (see below)
# 2. build the galaxy from your notes
python3 build.py

# 3. run it
python3 server.py
```

Then open **http://localhost:4700**.

## Install

1. **Python 3.8+**. That is the only requirement — everything is in the
   standard library. There is nothing to `pip install`.
2. **Add your API key.** Open `config.json` in the project root and paste in
   either an **Anthropic** or an **OpenRouter** key. The provider is detected
   from the key itself, so you normally only touch `api_key` and `model`:

   ```json
   {
     "api_key": "sk-or-v1-your-openrouter-key",
     "model": "anthropic/claude-3.5-sonnet",
     "notes_dir": "",
     "provider": "auto",
     "base_url": ""
   }
   ```

   | Key looks like | Provider used | Endpoint |
   |---|---|---|
   | `sk-or-…` | OpenRouter | `openrouter.ai/api/v1/chat/completions` |
   | `sk-ant-…` | Anthropic | `api.anthropic.com/v1/messages` |
   | `PUT-YOUR-KEY-HERE` | `claude -p` CLI | your machine |

   You can force one with `"provider": "openrouter" | "anthropic" | "cli"`, and
   point at a proxy with `"base_url"`. Model ids on OpenRouter look like
   `anthropic/claude-3.5-sonnet` — pick one from
   [openrouter.ai/models](https://openrouter.ai/models). If the model is wrong,
   Jarvis tells you so and suggests live ids it can actually see.

   `config.json` is **git-ignored**, and it lives outside `viewer/`, so the
   browser can never reach it. It is created for you with placeholders the first
   time you run `server.py`.

   **No API key?** Leave it as `PUT-YOUR-KEY-HERE` and Jarvis falls back to the
   `claude -p` CLI automatically. Everything works; it is just slower.
3. **Point it at your notes** (optional). By default it reads `./notes`. To use
   your own folder, either set `"notes_dir": "/full/path/to/notes"` in
   `config.json` or pass a folder to the builder:

   ```bash
   python3 build.py ~/Documents/notes
   ```

## Running

| Command | What it does |
|---|---|
| `python3 build.py [notes_dir]` | Scans every `.md` file and writes `viewer/graph-data.js`. Run this whenever you add or edit notes. |
| `python3 server.py` | Serves the viewer on **port 4700** and runs the brain. `JARVIS_PORT=8080 python3 server.py` to change the port. |

The server only ever serves the `viewer/` folder — requests for anything else
(including `config.json`) return 404.

## Using it

- **Click a star** — the camera flies to it, its neighbours light up, and the
  note's text opens in the side panel.
- **Type a question** in the bar at the bottom. Jarvis answers in two or three
  sentences, speaks it, and flies to the source note. Ask four or more notes'
  worth of question and it frames the whole cluster instead.
- **Press the 🎙** (Chrome/Edge) and just speak. Same flow.
- **Say "remember that…"** and Jarvis writes a real markdown note into
  `notes/captures/`, grows a new star next to whatever it relates to, and
  confirms with one dry line.
- **🔇** mutes the voice. The first click anywhere on the page enables audio —
  browsers block speech until you interact.

Good first questions: *"Where should captured notes live?"*, *"What are the open
questions about Jarvis?"*, and something your notes **don't** cover, to hear it
admit it.

## Settings

Click the **⚙** in the top right. You can paste an **OpenRouter** key and a
**Groq** key, press **Show models**, and pick from the live list — each entry
shows its context window and price. Choose **OpenRouter**, **Groq**,
**Anthropic** or the `claude -p` CLI as the active brain.

Keys are sent only to your own machine (`localhost`) and stored in
`config.json`, which is git-ignored. The browser never receives a full key back
— only a masked form like `sk-or-v1…5678`.

You never have to open Settings: `server.py` reads `config.json` on every
request, so editing that file by hand works exactly the same.

## How the links are made

Two notes are joined when one `[[wikilinks]]` to the other, or when one note
mentions the other's title. Titles come from the note's own `# Heading` when it
has one, otherwise from the filename.

## Files

```
build.py                 scans notes -> viewer/graph-data.js
server.py                static server (viewer/ only) + POST /chat + POST /remember
viewer/index.html        the galaxy, the ask bar, the voice
viewer/graph-data.js     generated - do not edit
notes/                   your notes (Upgrading Jarvis.md is the seed)
notes/captures/          created on your first "remember that..."
config.json              your API key - git-ignored, never committed
tools/viewer-harness.mjs headless test harness (node), no browser needed
```

Check the viewer logic without a browser:

```bash
node tools/viewer-harness.mjs      # 26 checks
```

## If something looks wrong

| Symptom | Cause |
|---|---|
| Black screen, "could not load 3d-force-graph" | The viewer pulls Three.js and `3d-force-graph` from `esm.sh`; check your connection or ad-blocker |
| Answers, but no voice | Click once anywhere — browsers block audio before a gesture. Check the 🔇 toggle |
| No 🎙 button | Your browser lacks `webkitSpeechRecognition`. Chrome and Edge have it; Safari and Firefox don't (speaking out still works) |
| "OpenRouter rejected the key" | The key in `config.json` is wrong or revoked |
| "OpenRouter does not recognise that model…" | Set `"model"` to a real OpenRouter id — the error lists live suggestions |
| "No API key in config.json…" | Paste your key, or install the `claude` CLI |
| An empty galaxy | Run `python3 build.py` — `viewer/graph-data.js` may be missing or stale |

## Security

`config.json` is listed in `.gitignore` and was ignored **before the file was
ever created**. It is stored in the project root, never under `viewer/`, so the
web server cannot serve it. It has never been committed in any revision.
