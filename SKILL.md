---
name: gdocsync
description: "Keep Google Docs in step with a repo's Markdown docs: push Markdown changes into the mapped Google Doc, word by word so comments on unchanged text survive, with links between docs pointing to each other's Google Docs and headings. Stops when the Google Doc was edited since the last push. Use when the user invokes /gdocsync, asks to sync, update or publish a Markdown doc to its Google Doc, asks which Google Docs lag their Markdown, wants to browse all docs against their Google Docs, or wants a Google Doc made from a Markdown file."
argument-hint: "[status | browse | push FILE | create FILE | map FILE DOC_ID] [repo path]"
---

# gdocsync

Markdown is the source; each mapped Google Doc follows it. `gdocsync.py` talks to the Google Docs and
Drive APIs directly with gdocs-mcp's sign-in (`~/.gdocs-mcp`).

```bash
python3 -I ~/.claude/skills/gdocsync/gdocsync.py --repo <repo> status
python3 -I ~/.claude/skills/gdocsync/gdocsync.py --repo <repo> push <file.md> --dry-run
python3 -I ~/.claude/skills/gdocsync/gdocsync.py --repo <repo> push <file.md>
python3 -I ~/.claude/skills/gdocsync/gdocsync.py --repo <repo> push <file.md> --target <T.md> [--dry-run]
python3 -I ~/.claude/skills/gdocsync/gdocsync.py --repo <repo> create <file.md> [--title T]
python3 -I ~/.claude/skills/gdocsync/gdocsync.py --repo <repo> map <file.md> <document id>
python3 -I ~/.claude/skills/gdocsync/gdocsync.py --repo <repo> export [<file.md>…] [--to DIR]
python3 -I ~/.claude/skills/gdocsync/gdocsync.py --repo <repo> browse
```

`browse` is the project browser: it exports every mapped doc and hands over to docdiff on the export
(run it with `run_in_background`, then open the `URL:` it prints in the built-in browser). The page
lists every Markdown file, its name a link to the doc below, with a link to its Google Doc, where it
stands in one sentence (as `status` says it) and its open comments. The sentence covers the three
versions of a doc: committed, uncommitted edits, and the Google Doc ("Uncommitted edits, already in the
Google Doc"), with a link to each comparison that has something: "uncommitted edits ↗" opens a second
docdiff page of the repo's uncommitted edits (against `HEAD`, started in the background) at that doc,
"what a push would change ↓" the doc below. Below, every mapped doc shows with its comments marked, one
that differs as what a push would change, to accept and publish there with "⇪ publish accepted". The
local page's link changes when its server restarts: run `browse` again.

`status` says the same sentence per doc: whether the Markdown is committed, whether the Google Doc
matches it (or how many changes a push would make), and whether the Google Doc was edited since the
last push or never pushed from here.

`export` shows what a push would change in a diff tool of your choice. It writes a git repo
(`~/.gdocsync/<repo>/<repo>-google/` by default) whose tag `google` holds each Google Doc's text and
whose working tree holds its Markdown's, both as the plain text a push compares, so
`git -C <dir> diff --word-diff google` or any viewer that takes a base revision shows the push. Beside
them, `comments.json` holds each doc's open comments: `{file: [{quote, author, date, time, text,
replies: [{author, time, text}], fate, url}]}`, `fate` being `goes` when a push deletes all of the comment's anchor, `edited` when it changes
some of it, and `url` opening the Google Doc on the comment; `docs.json` lists every Markdown file but
README.md, with its Google Doc and where it stands, `[{file, title, url, local (committed, uncommitted or
new), unpublished (changes a push would make), edited (paragraphs edited in the Google Doc since the last
push; null if never pushed), stand (the sentence), comments, local_diff}]`, in the order README.md links them and titled with its words for each (others after, titled by their first
heading).

`push --target T` publishes only some of the changes: T is a Markdown file made from the last export,
the Google Doc's text with the changes to publish taken in (a diff tool's accepted copy, say). The push
brings the Google Doc to T; paragraphs that then read as in the Markdown get its styles and links,
the others keep the Google Doc's, and words inserted into those start plain. Every change in T was
chosen, so it needs no `--force`, even on a doc never pushed. It stops if the Google Doc or the
Markdown changed since the export: export again and make T from that.

Which Google Doc belongs to which file is per repo and per machine (`~/.gdocsync/<repo>/map.json`); a
project's own instructions say which repo it syncs and with what care.

## What a push does

1. Reads the Google Doc and compares it with what the last push left there
   (`~/.gdocsync/<repo>/snapshots/`). If someone edited it since in a way the push would undo (text the
   Markdown lacks, or text removed that the Markdown still has), it lists those paragraphs and exits 2.
2. Compares the Markdown blocks (headings, paragraphs, list items, tables) with the Google Doc's,
   bottom up. A changed block that still shares at least 40% of its words with one of the same kind is
   edited word by word; the rest are inserted or deleted whole. A comment keeps its anchor unless its
   own words change, and an edited heading keeps its id, so links to it hold. The summary line counts
   open comments anchored in text that changes: one whose words all go loses its anchor.
3. Restyles every paragraph (bold, italic, code in Consolas) and sets every link: a link to another
   mapped Markdown file goes to that file's Google Doc, at the heading its anchor names (`#heading=h.…`),
   or to the section on GitHub while that Google Doc lacks the heading; other relative links go to GitHub; `TBD …` text stays text. Restyling keeps text, so comments stay.
4. Saves the new snapshot.

Headings map `#` → Heading 1, `##` → Heading 2, `###` → Heading 3; `---` rules are dropped, as in the
shared docs.

## Rules

- Run `status` first, then `push --dry-run`, and show the user the dry run's summary line and any
  comments it says will lose their anchor; for more than a few changes, show them rendered (`export`). Push to a Google Doc other people read only on the user's go.
- When the push stops on edits made in the Google Doc, read them to the user; merge the ones they want
  into the Markdown (then commit as usual), and push again. `--force` overwrites them: only on the
  user's word.
- A doc never pushed by gdocsync has no snapshot, so its own edits can't be told apart: the push refuses
  without `--force`. Compare by hand first (the dry run lists every change: `~` a paragraph's edits, `-` and `+` blocks out and in),
  or publish the changes one by one with `--target`.
- `invalid_grant` or "sign-in expired": only the user can fix it, with `npx gdocs-mcp auth`.
- Sharing a new Google Doc is the user's decision; `create` leaves it private.

## Limits

- The Markdown subset is the design docs': headings, paragraphs, `-` and `1.` lists (one level),
  pipe tables, `**bold**`, `*italic*`, `` `code` ``, inline and reference links. Nested lists, images and
  footnotes are not converted.
- A changed table is replaced whole, so comments inside it are lost.
- Inserting text right before a table needs an empty paragraph above the table, as API-made tables have.
