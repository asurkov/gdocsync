# gdocsync

Keeps Google Docs in step with a git repo's Markdown docs. The Markdown is the source; a push edits
only the words that changed, so a comment in the Google Doc keeps its anchor unless its own words change, and links
between docs point to each other's Google Docs. It is a [Claude Code](https://claude.com/claude-code)
skill (`/gdocsync`), and `gdocsync.py` also runs on its own. [SKILL.md](SKILL.md) says how it behaves.

## Setup on a new machine

### 1. Requirements

- Python 3.9 or later. The script uses only the standard library.
- git.
- Node.js, for `npx`: the Google sign-in is done once with [gdocs-mcp](https://github.com/Apurwa/gdocs-mcp).

### 2. Install the skill

```bash
git clone https://github.com/asurkov/gdocsync.git ~/.claude/skills/gdocsync
```

Claude Code picks it up from there as `/gdocsync`.

### 3. Sign in to Google

gdocsync uses the sign-in that gdocs-mcp stores in `~/.gdocs-mcp/`. Skip to the last step if that folder
already holds `credentials.json` and `token.json`.

1. In the [Google Cloud Console](https://console.cloud.google.com), create a project, or pick one.
2. Enable the **Google Docs API** and the **Google Drive API**.
3. Under **Google Auth Platform → Audience**, set the user type to External and add your Google account as
   a test user.
4. Under **Credentials**, create an **OAuth client ID** of type **Desktop app** and download its JSON file.
5. Run:

   ```bash
   npx gdocs-mcp auth ~/Downloads/client_secret_*.json
   ```

   It opens Google sign-in, then saves `credentials.json` and `token.json` to `~/.gdocs-mcp/`. Later
   sign-ins need only `npx gdocs-mcp auth`.

While the app's publishing status is **Testing**, Google lets its sign-in last 7 days: after that every
call fails with `invalid_grant`, and `npx gdocs-mcp auth` signs in again. Publishing the app (Audience →
Publish app) makes it last; Google then shows an "unverified app" warning at sign-in, which you can pass
for your own account.

### 4. Connect docs

Each Markdown file is tied to one Google Doc, per repo:

```bash
G=~/.claude/skills/gdocsync/gdocsync.py
python3 -I $G --repo <repo> map <file.md> <document id>   # a Google Doc that already exists
python3 -I $G --repo <repo> create <file.md>               # a new, private Google Doc
python3 -I $G --repo <repo> status
python3 -I $G --repo <repo> export                         # Google Docs vs Markdown, for a diff tool
```

The document id is the part of the Google Doc's URL between `/d/` and `/edit`.

The ties and what each push left in its Google Doc are kept in `~/.gdocsync/<repo name>/`, not in the
repo. When moving to a new machine, copy that folder over. Without it the docs can be mapped again, but
their first push can't tell edits made in the Google Doc from out-of-date text: compare with
`push <file.md> --dry-run`, then push with `--force`.
