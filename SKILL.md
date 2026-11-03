---
name: gdocsync
description: "Keep Google Docs in step with a repo's Markdown docs: push Markdown changes into the mapped Google Doc, paragraph by paragraph so comments on unchanged text survive, with links between docs pointing to each other's Google Docs and headings. Stops when the Google Doc was edited since the last push. Use when the user invokes /gdocsync, asks to sync, update or publish a Markdown doc to its Google Doc, asks which Google Docs lag their Markdown, or wants a Google Doc made from a Markdown file."
argument-hint: "[status | push FILE | create FILE | map FILE DOC_ID] [repo path]"
---

# gdocsync

Markdown is the source; each mapped Google Doc follows it. `gdocsync.py` talks to the Google Docs and
Drive APIs directly with gdocs-mcp's sign-in (`~/.gdocs-mcp`).

```bash
python3 -I ~/.claude/skills/gdocsync/gdocsync.py --repo <repo> status
python3 -I ~/.claude/skills/gdocsync/gdocsync.py --repo <repo> push <file.md> --dry-run
python3 -I ~/.claude/skills/gdocsync/gdocsync.py --repo <repo> push <file.md>
python3 -I ~/.claude/skills/gdocsync/gdocsync.py --repo <repo> create <file.md> [--title T]
python3 -I ~/.claude/skills/gdocsync/gdocsync.py --repo <repo> map <file.md> <document id>
```

Which Google Doc belongs to which file is per repo and per machine (`~/.gdocsync/<repo>/map.json`); a
project's own instructions say which repo it syncs and with what care.

## What a push does

1. Reads the Google Doc and compares it with what the last push left there
   (`~/.gdocsync/<repo>/snapshots/`). If someone edited it since in a way the push would undo (text the
   Markdown lacks, or text removed that the Markdown still has), it lists those paragraphs and exits 2.
2. Compares the Markdown blocks (headings, paragraphs, list items, tables) with the Google Doc's and
   rewrites only the ones whose text differs, bottom up. Comments anchored in unchanged paragraphs keep
   their anchors; the summary line counts open comments anchored in text that goes.
3. Restyles every paragraph (bold, italic, code in Consolas) and sets every link: a link to another
   mapped Markdown file goes to that file's Google Doc, at the heading its anchor names (`#heading=h.…`),
   or to the section on GitHub while that Google Doc lacks the heading; other relative links go to GitHub; `TBD …` text stays text. Restyling keeps text, so comments stay.
4. Saves the new snapshot.

Headings map `#` → Heading 1, `##` → Heading 2, `###` → Heading 3; `---` rules are dropped, as in the
shared docs.

## Rules

- Run `status` first, then `push --dry-run`, and show the user the dry run's summary line and any
  comments it says will lose their anchor. Push to a Google Doc other people read only on the user's go.
- When the push stops on edits made in the Google Doc, read them to the user; merge the ones they want
  into the Markdown (then commit as usual), and push again. `--force` overwrites them: only on the
  user's word.
- A doc never pushed by gdocsync has no snapshot, so its own edits can't be told apart: the push refuses
  without `--force`. Compare by hand first (the dry run lists every paragraph out and in).
- `invalid_grant` or "sign-in expired": only the user can fix it, with `npx gdocs-mcp auth`.
- Sharing a new Google Doc is the user's decision; `create` leaves it private.

## Limits

- The Markdown subset is the design docs': headings, paragraphs, `-` and `1.` lists (one level),
  pipe tables, `**bold**`, `*italic*`, `` `code` ``, inline and reference links. Nested lists, images and
  footnotes are not converted.
- A changed table is replaced whole, so comments inside it are lost.
- Inserting text right before a table needs an empty paragraph above the table, as API-made tables have.
