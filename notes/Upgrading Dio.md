# Upgrading Dio

The short version: Dio currently knows exactly one thing, and that thing is this note.
The galaxy is empty, the index is cold, and the butler has nothing to be butler about.
This note is the seed. Everything else grows out of it.

## Where we are

Stage one gives us the shape of the thing: every markdown file becomes a star, every
folder becomes a colour, and any note that name-drops another note gets a thread of
light between them. Click a star and the camera falls toward it, the neighbours lean
in, and the text opens on the right. That is the whole trick, and it is enough to be
getting on with.

## What good looks like

- **Answers with receipts.** When Dio says something, the star it came from should
  light up. An answer you cannot trace is a rumour.
- **A voice, not a text box.** Dry, British, unhurried. Never breathless.
- **Total recall.** Say "remember that the roaster needs a new gasket" and it should
  exist as a real markdown file, findable, greppable, owned by me — not buried in a
  database I cannot read.

## Open questions

How do we keep the index fresh without a file watcher on every platform? Probably
`build.py` on demand plus a rebuild whenever a note is captured by voice. Rebuilding
the whole graph from disk is cheap when the graph is small, and if it ever gets slow
we can get clever. Later. Not now.

Should captured notes live in their own folder? Yes — `captures/`, inside the notes
directory, so they are unmistakably machine-written and easy to audit or delete in
a batch. A human should always be able to tell which thoughts were typed and which
were muttered at a microphone.

## Standing rules for future me

1. Never put a secret in the repository. The API key lives in `config.json`, which is
   git-ignored before the file is ever created.
2. Notes stay plain markdown. No front matter required, no proprietary format, no lock-in.
3. The butler does not recite. If the text is already on screen, there is no reason to
   read it aloud.
4. When something breaks, say so plainly and fix it. No euphemisms, no silent failures.
