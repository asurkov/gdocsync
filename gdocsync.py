#!/usr/bin/env python3
"""Keep Google Docs in step with a repo's Markdown docs.

Usage: gdocsync.py [--repo PATH] status
       gdocsync.py [--repo PATH] push FILE [--dry-run] [--force | --target T]
       gdocsync.py [--repo PATH] create FILE [--title TITLE]
       gdocsync.py [--repo PATH] map FILE DOC_ID
       gdocsync.py [--repo PATH] export [FILE...] [--to DIR]
       gdocsync.py [--repo PATH] browse

Markdown is the source; the Google Doc follows it. A push edits changed paragraphs word by word, and
inserts or deletes whole only the blocks added or removed, bottom up, so a comment keeps its anchor
unless its own words change; then it restyles every paragraph and
sets every link. A link to another mapped Markdown file goes to that file's Google Doc, at the heading
its anchor names; any other relative link goes to the file on GitHub.

Before a push, the Google Doc is compared with what the last push left in it. If someone edited it
since, the push lists those paragraphs and stops: merge them into the Markdown first, or pass --force.

push --target T brings the Google Doc to T instead: a Markdown file made from the last export, such as
its Google Doc text with some of the Markdown's changes taken in. Paragraphs that then read as in FILE
get FILE's styles and links; the others keep the Google Doc's. It stops if the Google Doc or FILE
changed since the export.

browse exports every mapped doc and opens docdiff on the export: a page that lists every Markdown file
with its Google Doc and where the two stand, and shows each doc that differs as what a push would
change, to accept and publish there.

State lives in ~/.gdocsync/<repo name>/: map.json (file -> document id) and snapshots/ (each Google
Doc's paragraphs as the last push left them). Credentials are gdocs-mcp's, in ~/.gdocs-mcp.
"""
import argparse, difflib, html, json, os, pathlib, re, shlex, string, subprocess, sys, time, urllib.error, urllib.parse
import urllib.request

AUTH = pathlib.Path('~/.gdocs-mcp').expanduser()
STATE = pathlib.Path('~/.gdocsync').expanduser()
DOCDIFF = pathlib.Path(__file__).resolve().parent.parent / 'docdiff' / 'docdiff.py'
DOCS = 'https://docs.googleapis.com/v1/documents'
DRIVE = 'https://www.googleapis.com/drive/v3/files'
LIST_PRESET = {'ul': 'BULLET_DISC_CIRCLE_SQUARE', 'ol': 'NUMBERED_DECIMAL_ALPHA_ROMAN'}
HEADINGS = {'h1': 'HEADING_1', 'h2': 'HEADING_2', 'h3': 'HEADING_3'}
STYLE_FIELDS = 'bold,italic,underline,link,weightedFontFamily,foregroundColor'
LINK_STYLE = {'underline': True, 'foregroundColor': {'color': {'rgbColor': {'red': 0.067, 'green': 0.333, 'blue': 0.8}}}}
SAME = 0.4  # share of words a paragraph keeps for it to be edited in place rather than replaced


def u16(s):
    """Length in UTF-16 code units, the unit of Google Docs indices."""
    return len(s.encode('utf-16-le')) // 2


# Google APIs, with gdocs-mcp's token. The refreshed access token is kept in memory only.

class Google:
    def __init__(self):
        try:
            self.token = json.loads((AUTH / 'token.json').read_text())
            self.client = json.loads((AUTH / 'credentials.json').read_text())['installed']
        except FileNotFoundError:
            sys.exit('no gdocs-mcp sign-in: run `npx gdocs-mcp auth`')
        self.access, self.expiry = self.token['access_token'], self.token.get('expiry_date', 0) / 1000

    def _auth(self):
        if self.expiry < time.time() + 60:
            body = urllib.parse.urlencode({'client_id': self.client['client_id'], 'client_secret': self.client['client_secret'],
                                           'refresh_token': self.token['refresh_token'], 'grant_type': 'refresh_token'}).encode()
            try:
                r = json.load(urllib.request.urlopen(self.client['token_uri'], body, timeout=30))
            except urllib.error.HTTPError as e:
                sys.exit(f'sign-in expired ({e.read().decode()[:80]}): run `npx gdocs-mcp auth`')
            self.access, self.expiry = r['access_token'], time.time() + r['expires_in']
        return {'Authorization': 'Bearer ' + self.access}

    def call(self, url, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data, {**self._auth(), 'Content-Type': 'application/json'})
        try:
            return json.load(urllib.request.urlopen(req, timeout=60))
        except urllib.error.HTTPError as e:
            sys.exit(f'{e.code} from {url.split("?")[0]}: {e.read().decode()[:400]}')

    def doc(self, doc_id):
        return self.call(f'{DOCS}/{doc_id}')

    def batch(self, doc_id, requests, revision):
        if requests:
            self.call(f'{DOCS}/{doc_id}:batchUpdate',
                      {'requests': requests, 'writeControl': {'requiredRevisionId': revision}})

    def create(self, title):
        return self.call(DOCS, {'title': title})['documentId']

    def open_comments(self, doc_id):
        """Unresolved comments: quote (the text they are anchored in, as it read when they were made; the API
        gives it HTML-escaped), author, date, time, text, replies (author, time, text), and url, which opens
        the doc on the comment."""
        fields = ('comments(id,resolved,quotedFileContent/value,author/displayName,createdTime,content,'
                  'replies(author/displayName,content,createdTime))')
        q = urllib.parse.urlencode({'fields': fields, 'pageSize': 100})
        return [{'quote': html.unescape(c.get('quotedFileContent', {}).get('value', '')), 'author': c['author']['displayName'],
                 'date': c['createdTime'][:10], 'time': c['createdTime'], 'text': c.get('content', ''),
                 'url': f'https://docs.google.com/document/d/{doc_id}/edit?disco={c["id"]}',
                 'replies': [{'author': r['author']['displayName'], 'time': r.get('createdTime'), 'text': r.get('content', '')}
                             for r in c.get('replies', [])]}
                for c in self.call(f'{DRIVE}/{doc_id}/comments?{q}').get('comments', []) if not c.get('resolved')]


# Markdown: the subset the design docs use.

def md_blocks(md):
    """[(kind, body)]: h1-h3, p, ul, ol (body = inline Markdown), table (body = rows of cells); and the
    reference-link definitions. Horizontal rules are dropped, as in the shared Google Docs."""
    refs = dict(re.findall(r'^\[([^\]]+)\]: (\S+)$', md, re.M))
    lines = [l for l in md.split('\n') if not re.match(r'^\[[^\]]+\]: \S+$', l)]
    out, i = [], 0
    while i < len(lines):
        l = lines[i]
        if not l.strip() or l.strip() == '---':
            i += 1
            continue
        m = re.match(r'^(#{1,3}) (.*)$', l)
        if m:
            out.append(('h' + str(len(m.group(1))), m.group(2)))
            i += 1
        elif l.startswith('|'):
            rows = []
            while i < len(lines) and lines[i].startswith('|'):
                if not re.match(r'^\|[-| :]+\|$', lines[i]):
                    rows.append([c.strip() for c in re.split(r'(?<!\\)\|', lines[i].strip().strip('|'))])
                i += 1
            out.append(('table', rows))
        elif re.match(r'^(- |\d+\. )', l):
            mark = re.match(r'^(- |\d+\. )', l).group(1)
            kind, ind, text = ('ul' if mark == '- ' else 'ol'), len(mark), [l[len(mark):]]
            i += 1
            while i < len(lines) and lines[i].startswith(' ' * ind) and lines[i].strip():
                text.append(lines[i].strip())
                i += 1
            out.append((kind, ' '.join(text)))
        else:
            text = [l.strip()]
            i += 1
            while i < len(lines) and lines[i].strip() and not re.match(r'^(#|- |\d+\. |\||---)', lines[i]):
                text.append(lines[i].strip())
                i += 1
            out.append(('p', ' '.join(text)))
    return out, refs


def runs(text, style=None):
    """Inline Markdown as [(text, style)], style keys bold, italic, code, link (the raw target)."""
    style = style or {}
    out, i, buf = [], 0, ''

    def flush():
        nonlocal buf
        if buf:
            out.append((buf, dict(style)))
            buf = ''
    while i < len(text):
        c = text[i]
        if c == '\\' and text[i + 1:i + 2] in set(string.punctuation):
            buf += text[i + 1]
            i += 2
            continue
        if c == '[':
            depth, j = 0, i
            while j < len(text):
                if text[j] == '`':
                    j = text.index('`', j + 1)
                elif text[j] == '[':
                    depth += 1
                elif text[j] == ']':
                    depth -= 1
                    if not depth:
                        break
                j += 1
            m = re.match(r'\]\(([^)\s]+)\)|\]\[([^\]]+)\]', text[j:]) if j < len(text) else None
            if m:
                flush()
                out += runs(text[i + 1:j], {**style, 'link': m.group(1) or ('ref:' + m.group(2))})
                i = j + m.end()
                continue
        if c == '`':
            j = text.index('`', i + 1)
            flush()
            out.append((text[i + 1:j], {**style, 'code': True}))
            i = j + 1
            continue
        if text.startswith('**', i):
            j = text.index('**', i + 2)
            flush()
            out += runs(text[i + 2:j], {**style, 'bold': True})
            i = j + 2
            continue
        if c == '*' and i + 1 < len(text) and text[i + 1] != ' ':
            j = text.index('*', i + 1)
            flush()
            out += runs(text[i + 1:j], {**style, 'italic': True})
            i = j + 1
            continue
        buf += c
        i += 1
    flush()
    return out


def plain(body):
    return ''.join(t for t, _ in runs(body))


QUOTES = str.maketrans('‘’“”', '\'\'""')


def norm(s):
    # Google Docs types curly quotes; the Markdown has straight ones.
    return ' '.join(s.translate(QUOTES).split())


def md_key(kind, body):
    if kind == 'table':
        return ('table', norm(' | '.join(plain(c) for row in body for c in row)))
    return (kind, norm(plain(body)))


# The Google Doc as blocks comparable with the Markdown ones.

def doc_blocks(doc):
    """Non-empty body paragraphs and tables: dicts with kind, text, start, end."""
    lists = doc.get('lists', {})
    out = []
    for e in doc['body']['content']:
        if 'paragraph' in e:
            p = e['paragraph']
            text = ''.join(r.get('textRun', {}).get('content', '') for r in p['elements']).rstrip('\n')
            if not text.strip():
                continue
            kind = {v: k for k, v in HEADINGS.items()}.get(p['paragraphStyle'].get('namedStyleType'), 'p')
            if 'bullet' in p:
                level = lists.get(p['bullet']['listId'], {}).get('listProperties', {}).get('nestingLevels', [{}])[0]
                kind = 'ul' if 'glyphSymbol' in level else 'ol'
            out.append({'kind': kind, 'text': text, 'start': e['startIndex'], 'end': e['endIndex'],
                        'heading': p['paragraphStyle'].get('headingId')})
        elif 'table' in e:
            cells = [c for row in table_rows(e['table']) for c in row]
            out.append({'kind': 'table', 'text': ' | '.join(cells), 'start': e['startIndex'], 'end': e['endIndex'], 'table': e['table']})
    return out


def table_rows(table):
    return [[''.join(r.get('textRun', {}).get('content', '') for c in cell['content'] if 'paragraph' in c
                     for r in c['paragraph']['elements']).strip()
             for cell in row['tableCells']] for row in table['tableRows']]


def doc_key(b):
    return (b['kind'], norm(b['text']))


# State.

class Repo:
    def __init__(self, path):
        top = subprocess.run(['git', '-C', path, 'rev-parse', '--show-toplevel'], capture_output=True, text=True)
        if top.returncode:
            sys.exit(f'{path}: not a git repository')
        self.root = pathlib.Path(top.stdout.strip())
        self.state = STATE / self.root.name
        (self.state / 'snapshots').mkdir(parents=True, exist_ok=True)
        remote = subprocess.run(['git', '-C', self.root, 'remote', 'get-url', 'origin'], capture_output=True, text=True).stdout.strip()
        m = re.match(r'(?:git@github\.com:|https://github\.com/)(.+?)(?:\.git)?$', remote)
        self.github = f'https://github.com/{m.group(1)}/blob/main/' if m else None

    @property
    def map(self):
        f = self.state / 'map.json'
        return json.loads(f.read_text()) if f.exists() else {}

    def set_map(self, name, doc_id):
        m = self.map
        m[name] = doc_id
        (self.state / 'map.json').write_text(json.dumps(m, indent=1, sort_keys=True) + '\n')

    def snapshot(self, name):
        f = self.state / 'snapshots' / f'{name}.json'
        return json.loads(f.read_text()) if f.exists() else None

    def save_snapshot(self, name, blocks):
        (self.state / 'snapshots' / f'{name}.json').write_text(json.dumps([list(doc_key(b)) for b in blocks], indent=0))


def slug(h):
    return re.sub(r'[^\w\- ]', '', re.sub(r'`', '', h.strip().lower())).replace(' ', '-')


class Links:
    """Link targets in the Markdown to URLs: mapped files to their Google Doc and heading."""
    def __init__(self, google, repo, name, refs):
        self.g, self.repo, self.name, self.refs, self.heads = google, repo, name, refs, {}

    def headings(self, name, doc=None):
        if name not in self.heads:
            doc = doc or self.g.doc(self.repo.map[name])
            self.heads[name] = {slug(b['text']): b['heading'] for b in doc_blocks(doc) if b.get('heading')}
        return self.heads[name]

    def url(self, target):
        if target.startswith('ref:'):
            target = self.refs.get(target[4:], target[4:])
        if re.match(r'[a-z]+:', target):
            return target
        path, _, anchor = target.partition('#')
        path = path[2:] if path.startswith('./') else path
        name = path or self.name
        if name in self.repo.map:
            h = self.headings(name).get(anchor) if anchor else None
            # A section the Google Doc does not have yet (it lags its Markdown) is linked on GitHub.
            if h or not anchor:
                return f'https://docs.google.com/document/d/{self.repo.map[name]}/edit' + (f'#heading={h}' if h else '')
        return (self.repo.github or '') + name + (f'#{anchor}' if anchor else '')


# Sync.

def text_requests(blocks, at):
    """Requests inserting Markdown text blocks at `at` and giving them paragraph, list and inline styles
    (links come later, once every heading has its id)."""
    text, paras = '', []
    for kind, body in blocks:
        start = at + u16(text)
        text += plain(body) + '\n'
        paras.append((kind, body, start, at + u16(text)))
    end = at + u16(text)
    reqs = [{'insertText': {'location': {'index': at}, 'text': text}},
            {'deleteParagraphBullets': {'range': {'startIndex': at, 'endIndex': end}}},
            {'updateTextStyle': {'range': {'startIndex': at, 'endIndex': end}, 'textStyle': {}, 'fields': STYLE_FIELDS}}]
    for kind, body, a, b in paras:
        reqs.append({'updateParagraphStyle': {'range': {'startIndex': a, 'endIndex': b},
                                              'paragraphStyle': {'namedStyleType': HEADINGS.get(kind, 'NORMAL_TEXT')},
                                              'fields': 'namedStyleType'}})
    k = 0
    while k < len(paras):
        if paras[k][0] in LIST_PRESET:
            m = k
            while m + 1 < len(paras) and paras[m + 1][0] == paras[k][0]:
                m += 1
            reqs.append({'createParagraphBullets': {'range': {'startIndex': paras[k][2], 'endIndex': paras[m][3] - 1},
                                                    'bulletPreset': LIST_PRESET[paras[k][0]]}})
            k = m + 1
        else:
            k += 1
    return reqs


def style_requests(body, start, links=None):
    """Inline styles of a paragraph starting at `start`; links only when `links` resolves them."""
    reqs, at = [], start
    for t, st in runs(body):
        ts = {}
        if st.get('bold'):
            ts['bold'] = True
        if st.get('italic'):
            ts['italic'] = True
        if st.get('code'):
            ts['weightedFontFamily'] = {'fontFamily': 'Consolas'}
        if st.get('link') and links:
            ts.update(LINK_STYLE, link={'url': links.url(st['link'])})
        if ts:
            reqs.append({'updateTextStyle': {'range': {'startIndex': at, 'endIndex': at + u16(t)}, 'textStyle': ts,
                                             'fields': ','.join(ts)}})
        at += u16(t)
    return reqs


def fill_table(g, doc_id, rows, at):
    """Insert a table at `at` and fill it, last cell first so earlier indices stay put."""
    doc = g.doc(doc_id)
    g.batch(doc_id, [{'insertTable': {'rows': len(rows), 'columns': len(rows[0]), 'location': {'index': at}}}], doc['revisionId'])
    doc = g.doc(doc_id)
    table = next(e for e in doc['body']['content'] if 'table' in e and e['startIndex'] >= at)
    cells = [(cell['content'][0]['startIndex'], plain(rows[r][c]))
             for r, row in enumerate(table['table']['tableRows']) for c, cell in enumerate(row['tableCells'])]
    reqs = [{'insertText': {'location': {'index': i}, 'text': t}} for i, t in reversed(cells) if t]
    g.batch(doc_id, reqs, doc['revisionId'])
    doc = g.doc(doc_id)
    table = next(e for e in doc['body']['content'] if 'table' in e and e['startIndex'] >= at)
    head = table['table']['tableRows'][0]
    g.batch(doc_id, [{'updateTextStyle': {'range': {'startIndex': head['startIndex'], 'endIndex': head['endIndex']},
                                          'textStyle': {'bold': True}, 'fields': 'bold'}}], doc['revisionId'])


def plan(gblocks, mblocks):
    keys = ([doc_key(b) for b in gblocks], [md_key(k, b) for k, b in mblocks])
    return [op for op in difflib.SequenceMatcher(None, *keys, autojunk=False).get_opcodes() if op[0] != 'equal']


def steps(gblocks, mblocks, styled=None):
    """What brings the Google Doc to the Markdown, top to bottom: ('edit', i, j) edits paragraph i in
    place into Markdown block j; ('swap', i1, i2, j1, j2) replaces blocks i1..i2 whole with j1..j2.
    styled: the Markdown blocks that get styles set afterwards (default all)."""
    out = []
    keys = ([doc_key(b) for b in gblocks], [md_key(k, b) for k, b in mblocks])
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, *keys, autojunk=False).get_opcodes():
        if tag == 'equal':
            # Alike but for spacing: restyling sets styles by offset, so the spacing is made exact too.
            out += [('edit', i, j) for i, j in zip(range(i1, i2), range(j1, j2))
                    if (styled is None or j in styled) and mblocks[j][0] != 'table'
                    and gblocks[i]['text'].translate(QUOTES) != plain(mblocks[j][1])]
            continue
        i, j = i1, j1
        for pi, pj in pairs(gblocks[i1:i2], mblocks[j1:j2]):
            if (i, j) != (i1 + pi, j1 + pj):
                out.append(('swap', i, i1 + pi, j, j1 + pj))
            out.append(('edit', i1 + pi, j1 + pj))
            i, j = i1 + pi + 1, j1 + pj + 1
        if (i, j) != (i2, j2):
            out.append(('swap', i, i2, j, j2))
    return out


def pairs(gblocks, mblocks):
    """Order-preserving (i, j) pairs of a changed run's paragraphs alike enough to edit in place."""
    sim = [[likeness(b, kind, body) for kind, body in mblocks] for b in gblocks]
    best = [[0.0] * (len(mblocks) + 1) for _ in range(len(gblocks) + 1)]
    for i in range(len(gblocks)):
        for j in range(len(mblocks)):
            best[i + 1][j + 1] = max(best[i][j + 1], best[i + 1][j], best[i][j] + sim[i][j] if sim[i][j] else 0)
    out, i, j = [], len(gblocks), len(mblocks)
    while i and j:
        if best[i][j] == best[i - 1][j]:
            i -= 1
        elif best[i][j] == best[i][j - 1]:
            j -= 1
        else:
            i, j = i - 1, j - 1
            out.append((i, j))
    return out[::-1]


def likeness(b, kind, body):
    """Share of words a Google Doc paragraph and a Markdown block have in common, in order; 0 when the
    paragraph can't be edited in place (another kind, a table, or not text alone, such as a chip)."""
    if b['kind'] != kind or kind == 'table' or u16(b['text']) + 1 != b['end'] - b['start']:
        return 0
    words = [[t for t in tokens(s.translate(QUOTES)) if not t.isspace()] for s in (b['text'], plain(body))]
    r = difflib.SequenceMatcher(None, *words, autojunk=False).ratio()
    return r if r >= SAME else 0


def tokens(s):
    return re.findall(r'\w+|\s+|[^\w\s]', s)


def word_edits(old, new):
    """Both texts as tokens, and the changes between them, compared with quotes folded. Changes only a
    space or a short word apart are one change, so a rewritten phrase reads, and goes in, whole."""
    a, b = tokens(old), tokens(new)
    sm = difflib.SequenceMatcher(None, [t.translate(QUOTES) for t in a], [t.translate(QUOTES) for t in b], autojunk=False)
    out = []
    for tag, a1, a2, b1, b2 in sm.get_opcodes():
        if tag == 'equal':
            continue
        if out and len(''.join(a[out[-1][2]:a1]).strip()) <= 3:
            out[-1] = ('replace', out[-1][1], a2, out[-1][3], b2)
        else:
            out.append((tag, a1, a2, b1, b2))
    return a, b, out


def edit_requests(b, body):
    """Requests editing Google Doc paragraph b into Markdown `body`: changed words only, bottom up, so a
    comment keeps its anchor unless its own words change."""
    a, new, ops = word_edits(b['text'], plain(body))
    at = [b['start']]
    for t in a:
        at.append(at[-1] + u16(t))
    reqs = []
    for _, a1, a2, b1, b2 in reversed(ops):
        if a2 > a1:
            reqs.append({'deleteContentRange': {'range': {'startIndex': at[a1], 'endIndex': at[a2]}}})
        if b2 > b1:
            text = ''.join(new[b1:b2])
            reqs += [{'insertText': {'location': {'index': at[a1]}, 'text': text}},
                     {'updateTextStyle': {'range': {'startIndex': at[a1], 'endIndex': at[a1] + u16(text)}, 'textStyle': {},
                                          'fields': STYLE_FIELDS}}]
    return reqs


def word_diff(old, new):
    """A paragraph's edits for the dry run: [-out-]{+in+}, long unchanged stretches cut short."""
    a, b, ops = word_edits(old, new)
    out, k = [], 0
    for _, a1, a2, b1, b2 in ops:
        same = ''.join(a[k:a1])
        out.append(same if len(same) <= 60 else (same[:25] + '…' + same[-25:] if k else '…' + same[-40:]))
        out.append((f'[-{"".join(a[a1:a2])}-]' if a2 > a1 else '') + (f'{{+{"".join(b[b1:b2])}+}}' if b2 > b1 else ''))
        k = a2
    tail = ''.join(a[k:])
    return ''.join(out) + (tail if len(tail) <= 40 else tail[:40] + '…')


def comment_fate(quote, gblocks, mblocks, todo):
    """'goes' when a push deletes all of a comment's anchored text, 'edited' when it changes some of it."""
    for step in todo:
        if step[0] == 'swap':
            if any(quote in gblocks[i]['text'] for i in range(step[1], step[2])):
                return 'goes'
            continue
        text = gblocks[step[1]]['text']
        p = text.find(quote)
        if p < 0:
            continue
        a, _, ops = word_edits(text, plain(mblocks[step[2]][1]))
        at = [0]
        for t in a:
            at.append(at[-1] + len(t))
        gone = sum(max(0, min(at[a2], p + len(quote)) - max(at[a1], p)) for _, a1, a2, _, _ in ops)
        if gone >= len(quote):
            return 'goes'
        if gone or any(p < at[a1] < p + len(quote) for _, a1, _, _, _ in ops):
            return 'edited'
    return None


def edited_since_push(repo, name, gblocks, mblocks):
    """Edits made in the Google Doc since the last push that a push would undo: text added there that
    the Markdown lacks, and text removed there that the Markdown still has. None before any push."""
    snap = repo.snapshot(name)
    if snap is None:
        return None
    now = [doc_key(b) for b in gblocks]
    want = {md_key(k, b) for k, b in mblocks}
    ops = difflib.SequenceMatcher(None, [tuple(x) for x in snap], now, autojunk=False).get_opcodes()
    added = [now[j][1] for op, _, _, j1, j2 in ops if op != 'equal' for j in range(j1, j2) if now[j] not in want]
    removed = [snap[i][1] for op, i1, i2, _, _ in ops if op != 'equal' for i in range(i1, i2) if tuple(snap[i]) in want]
    return added + ['(removed) ' + t for t in removed]


def push(g, repo, name, dry_run=False, force=False, target=None):
    doc_id = repo.map.get(name) or sys.exit(f'{name} has no Google Doc: create or map it first')
    md = (repo.root / name).read_text()
    mblocks, refs = md_blocks(md)
    doc = g.doc(doc_id)
    gblocks = doc_blocks(doc)
    if target:
        # Every change in the target was taken in on purpose, so edits made in the Google Doc that it
        # undoes need no --force.
        mblocks, styled = target_blocks(repo, name, gblocks, mblocks, target)
        edited = []
    else:
        styled, edited = None, edited_since_push(repo, name, gblocks, mblocks)
    if edited is None and gblocks and not force and not dry_run:
        sys.exit(f'{name}: no record of a previous push, so edits made in the Google Doc cannot be told apart.\n'
                 f'Compare it by hand, then push with --force; later pushes will check.')
    if edited and not force:
        print(f'{name}: the Google Doc was edited since the last push. Merge these into the Markdown, or --force:')
        for t in edited:
            print('  ·', t[:160])
        sys.exit(2)
    todo = steps(gblocks, mblocks, styled)
    swaps = [s for s in todo if s[0] == 'swap']
    comments = g.open_comments(doc_id)
    fates = [(comment_fate(c['quote'], gblocks, mblocks, todo), c['quote']) for c in comments if c['quote']]
    hit = [(f, q) for f, q in fates if f]
    print(f'{name}: {len(todo) - len(swaps)} paragraph(s) edited in place, {sum(s[2] - s[1] for s in swaps)} out, '
          f'{sum(s[4] - s[3] for s in swaps)} in; {len(hit)} of {len(comments)} open comment(s) anchored in text that changes')
    for f, q in hit:
        print('  comment loses its anchor:' if f == 'goes' else '  comment keeps an edited anchor:', q[:120])
    if dry_run:
        for s in todo:
            if s[0] == 'edit':
                print('  ~', word_diff(gblocks[s[1]]['text'], plain(mblocks[s[2]][1])))
                continue
            for i in range(s[1], s[2]):
                print('  -', gblocks[i]['text'][:140])
            for j in range(s[3], s[4]):
                print('  +', md_key(*mblocks[j])[1][:140])
        return
    # Bottom up, one change at a time, reading the document again after each.
    for _ in range(len(todo) + len(mblocks) + 5):
        doc = g.doc(doc_id)
        gblocks = doc_blocks(doc)
        todo = steps(gblocks, mblocks, styled)
        if not todo:
            break
        if todo[-1][0] == 'edit':
            _, i, j = todo[-1]
            g.batch(doc_id, edit_requests(gblocks[i], mblocks[j][1]), doc['revisionId'])
            continue
        _, i1, i2, j1, j2 = todo[-1]
        at = insertion_point(doc, gblocks, i1)
        reqs = [{'deleteContentRange': {'range': {'startIndex': gblocks[i1]['start'], 'endIndex': gblocks[i2 - 1]['end']}}}] if i2 > i1 else []
        new = mblocks[j1:j2]
        tables = [n for n, (k, _) in enumerate(new) if k == 'table']
        if tables and tables[0] == 0:
            g.batch(doc_id, reqs, doc['revisionId'])
            fill_table(g, doc_id, new[0][1], at)
            continue
        new = new[:tables[0]] if tables else new
        if new:
            reqs += text_requests(new, at)
            for (kind, body), a in zip(new, _starts(new, at)):
                reqs += style_requests(body, a)
        g.batch(doc_id, reqs, doc['revisionId'])
    else:
        sys.exit(f'{name}: the Google Doc did not converge on the Markdown; check it')
    restyle(g, repo, name, mblocks, refs, styled)
    repo.save_snapshot(name, doc_blocks(g.doc(doc_id)))
    print(f'{name}: pushed to https://docs.google.com/document/d/{doc_id}/edit')


def target_blocks(repo, name, gblocks, mblocks, target):
    """The blocks push --target brings the Google Doc to: the target's, each taken from the Markdown where
    it reads the same there, so it gets the Markdown's styles and links; and the indices of those. The
    target is made from the last export, so the Google Doc and the Markdown must read as they did then."""
    f = repo.state / 'exported' / f'{name}.json'
    then = json.loads(f.read_text()) if f.exists() else sys.exit(f'{name}: not exported yet; make the target from an export')
    if [list(doc_key(b)) for b in gblocks] != then['google'] or [list(md_key(*b)) for b in mblocks] != then['markdown']:
        sys.exit(f'{name}: the Google Doc or the Markdown changed since the export; export again and make the target from that')
    tblocks, _ = md_blocks(pathlib.Path(target).read_text())
    goal, styled = list(tblocks), set()
    keys = ([md_key(*b) for b in tblocks], [md_key(*b) for b in mblocks])
    for tag, i1, i2, j1, _ in difflib.SequenceMatcher(None, *keys, autojunk=False).get_opcodes():
        if tag == 'equal':
            goal[i1:i2] = mblocks[j1:j1 + i2 - i1]
            styled.update(range(i1, i2))
    return goal, styled


def insertion_point(doc, gblocks, i):
    """Where text for blocks that go before gblocks[i] is inserted: the start of that paragraph, or, before
    a table, the start of the empty paragraph that precedes it; at the end, the last paragraph's start."""
    content = doc['body']['content']
    if i == len(gblocks):
        return content[-1]['startIndex']
    if gblocks[i]['kind'] != 'table':
        return gblocks[i]['start']
    before = next((e for e in content if e['endIndex'] == gblocks[i]['start'] and 'paragraph' in e), None)
    if before is None or ''.join(r.get('textRun', {}).get('content', '') for r in before['paragraph']['elements']).strip():
        sys.exit('cannot insert text right before a table that has no empty paragraph above it; add one in the Google Doc')
    return before['startIndex']


def _starts(blocks, at):
    for kind, body in blocks:
        yield at
        at += u16(plain(body)) + 1


def restyle(g, repo, name, mblocks, refs, styled=None):
    """Reset and reapply inline styles and links on every paragraph (or those in styled); text and
    comments stay."""
    doc_id = repo.map[name]
    doc = g.doc(doc_id)
    gblocks = doc_blocks(doc)
    # A heading made in the Google Docs editor has no id until something links to it there; setting its
    # style again gives it one, so links can reach it. Text stays, so comments stay.
    bare = [b for b in gblocks if b['kind'] in HEADINGS and not b['heading']]
    if bare:
        g.batch(doc_id, [{'updateParagraphStyle': {'range': {'startIndex': b['start'], 'endIndex': b['end']},
                                                   'paragraphStyle': {'namedStyleType': s}, 'fields': 'namedStyleType'}}
                         for b in bare for s in ('NORMAL_TEXT', HEADINGS[b['kind']])], doc['revisionId'])
        doc = g.doc(doc_id)
        gblocks = doc_blocks(doc)
    if len(gblocks) != len(mblocks):
        sys.exit(f'{name}: {len(gblocks)} blocks in the Google Doc against {len(mblocks)} in the Markdown after the push')
    links = Links(g, repo, name, refs)
    links.headings(name, doc)
    reqs = []
    for j, (b, (kind, body)) in enumerate(zip(gblocks, mblocks)):
        if kind == 'table' or styled is not None and j not in styled:
            continue
        reqs.append({'updateTextStyle': {'range': {'startIndex': b['start'], 'endIndex': b['end'] - 1}, 'textStyle': {},
                                         'fields': STYLE_FIELDS}})
        reqs += [r for r in style_requests(body, b['start'], links)]
    g.batch(doc_id, reqs, doc['revisionId'])


def local_state(repo, name):
    """The Markdown file against git: committed, uncommitted (edited since the last commit) or new."""
    s = subprocess.run(['git', '-C', repo.root, 'status', '--porcelain', '--', name], capture_output=True, text=True).stdout
    return 'new' if s.startswith('??') else 'uncommitted' if s.strip() else 'committed'


def standing(repo, name, gblocks=None, mblocks=None):
    """Where a doc stands, as data and in words: its Markdown against git, its Google Doc against the
    Markdown (what a push would change), and whether the Google Doc was edited since the last push.
    Without blocks, the doc has no Google Doc."""
    local = local_state(repo, name)
    words = {'committed': 'Committed', 'uncommitted': 'Uncommitted edits', 'new': 'Not committed yet'}[local]
    if gblocks is None:
        return {'local': local, 'unpublished': None, 'edited': None, 'stand': words + '; no Google Doc'}
    ops, edited = plan(gblocks, mblocks), edited_since_push(repo, name, gblocks, mblocks)
    if not ops:
        words += ', and the Google Doc matches' if local == 'committed' else ', already in the Google Doc'
    else:
        words += f'; {len(ops)} change(s) not in the Google Doc yet'
    if edited is None:
        words += '; never pushed from here'
    elif edited:
        words += f'; {len(edited)} paragraph(s) edited in the Google Doc since the last push'
    return {'local': local, 'unpublished': len(ops), 'edited': None if edited is None else len(edited), 'stand': words}


def status(g, repo):
    if not repo.map:
        print(f'{repo.root.name}: no Markdown file is mapped to a Google Doc yet')
    for name, doc_id in sorted(repo.map.items()):
        if not (repo.root / name).exists():
            print(f'{name}: mapped, but the file is gone')
            continue
        mblocks, _ = md_blocks((repo.root / name).read_text())
        d = standing(repo, name, doc_blocks(g.doc(doc_id)), mblocks)
        print(f'{name}: {d["stand"]}; {len(g.open_comments(doc_id))} open comment(s)')
    unmapped = sorted(p.name for p in repo.root.glob('*.md') if p.name not in repo.map and p.name != 'README.md')
    if unmapped:
        print('not mapped:', ', '.join(unmapped))


def export(g, repo, names, where):
    """A git repo at `where` for any diff tool: tag `google` holds each Google Doc's text and the working
    tree its Markdown's, both as a push compares them; comments.json beside them holds each doc's open
    comments, with their quotes as they read there, and what a push does to their anchors; docs.json lists
    every Markdown file with its Google Doc and where the two stand, as status prints it. What each side
    read is kept in the state too, so push --target can tell that a target made from it is current."""
    where.mkdir(parents=True, exist_ok=True)
    run = lambda *a: subprocess.run(['git', '-C', where, '-c', 'user.name=gdocsync', '-c', 'user.email=gdocsync@localhost', *a],
                                    capture_output=True, check=True)
    if not (where / '.git').exists():
        run('init', '-q')
    sides, notes = {}, json.loads((where / 'comments.json').read_text()) if (where / 'comments.json').exists() else {}
    docs = {d['file']: d for d in json.loads((where / 'docs.json').read_text())
            if (repo.root / d['file']).exists()} if (where / 'docs.json').exists() else {}
    order, titles = readme_order(repo)
    for name in names:
        gblocks = doc_blocks(g.doc(repo.map[name]))
        mblocks, _ = md_blocks((repo.root / name).read_text())
        sides[name] = (as_markdown([(doc_key(b)[0], doc_key(b)[1], b.get('table') and table_rows(b['table'])) for b in gblocks]),
                       as_markdown([(*md_key(k, b), k == 'table' and [[plain(c) for c in row] for row in b]) for k, b in mblocks]))
        (repo.state / 'exported').mkdir(exist_ok=True)
        (repo.state / 'exported' / f'{name}.json').write_text(json.dumps(
            {'google': [doc_key(b) for b in gblocks], 'markdown': [md_key(*b) for b in mblocks]}, indent=0, ensure_ascii=False))
        todo, cs = steps(gblocks, mblocks), g.open_comments(repo.map[name])
        notes[name] = [{**c, 'quote': norm(c['quote']), 'fate': comment_fate(c['quote'], gblocks, mblocks, todo)}
                       for c in cs if c['quote']]
        was = docs.get(name, {})
        docs[name] = {'file': name, 'title': titles.get(name) or first_heading(repo.root / name) or name,
                      'url': f'https://docs.google.com/document/d/{repo.map[name]}/edit',
                      **standing(repo, name, gblocks, mblocks), 'comments': len(cs)}
        # browse's link to the doc's uncommitted edits holds while there are some.
        if docs[name]['local'] != 'committed' and was.get('local_diff'):
            docs[name]['local_diff'] = was['local_diff']
    # Markdown files with no Google Doc are listed too, so the list is the whole repo, but for README.md.
    for p in repo.root.glob('*.md'):
        if p.name not in repo.map and p.name != 'README.md':
            docs[p.name] = {'file': p.name, 'title': titles.get(p.name) or first_heading(p) or p.name, 'url': None,
                            **standing(repo, p.name), 'comments': 0}
    docs.pop('README.md', None)
    for name, (google, _) in sides.items():
        (where / name).write_text(google)
    run('add', '--', *sides)
    run('commit', '-q', '--allow-empty', '-m', 'Google Docs ' + time.strftime('%Y-%m-%d %H:%M'))
    run('tag', '-f', 'google')
    for name, (_, markdown) in sides.items():
        (where / name).write_text(markdown)
    (where / 'comments.json').write_text(json.dumps(notes, indent=1, ensure_ascii=False))
    (where / 'docs.json').write_text(json.dumps(sorted(docs.values(), key=lambda d: (order.get(d['file'], len(order)), d['file'])),
                                                indent=1, ensure_ascii=False))
    print(f'{where}: tag google is the Google Doc, the working tree the Markdown, for {len(sides)} file(s);'
          f' open comments in comments.json')


def readme_order(repo):
    """The docs README.md links to, in its order ({file: place}), and its words for each ({file: words}):
    the repo's own list of its documents."""
    f, order, titles = repo.root / 'README.md', {}, {}
    for words, path in re.findall(r'\[([^\]]+)\]\((?:\./)?([^)#\s/]+\.md)[^)]*\)', f.read_text() if f.exists() else ''):
        if path not in order:
            order[path], titles[path] = len(order), words
    return order, titles


def first_heading(path):
    m = re.search(r'^# (.+)$', path.read_text(), re.M)
    return m.group(1).strip() if m else None


def local_page(repo):
    """The URL of docdiff's page of the repo's uncommitted edits, started in the background (or already
    running), or None if it does not come up within a minute."""
    log = repo.state / 'local-docdiff.log'
    with open(log, 'w') as out:
        p = subprocess.Popen([sys.executable, '-I', str(DOCDIFF), '--repo', str(repo.root), '--base', 'HEAD', '--serve'],
                             stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(600):
        m = re.search(r'^URL: (\S+)', log.read_text(), re.M)
        if m:
            return m.group(1)
        if p.poll() is not None:
            return None
        time.sleep(0.1)
    return None


def browse(g, repo):
    """Exports every mapped doc, then hands over to docdiff on the export, with its docs list on top,
    every mapped doc named so one in step shows too, publishing through push --target, then export, and
    ↻ Google Docs through export.
    With uncommitted edits, a second docdiff page shows them against the last commit, and each doc that
    has some links to it there."""
    if not DOCDIFF.exists():
        sys.exit(f'{DOCDIFF}: docdiff not found')
    where = repo.state / f'{repo.root.name}-google'
    order, _ = readme_order(repo)
    names = sorted((n for n in repo.map if (repo.root / n).exists()), key=lambda n: (order.get(n, len(order)), n))
    export(g, repo, names, where)
    docs = json.loads((where / 'docs.json').read_text())
    if any(d['local'] != 'committed' for d in docs):
        url = local_page(repo)
        for d in docs:
            d.pop('local_diff', None)
            if url and d['local'] != 'committed':
                d['local_diff'] = f'{url}#file={urllib.parse.quote(d["file"])}&view=diff'
        (where / 'docs.json').write_text(json.dumps(docs, indent=1, ensure_ascii=False))
    me = ' '.join(map(shlex.quote, [sys.executable, '-I', str(pathlib.Path(__file__).resolve()), '--repo', str(repo.root)]))
    sys.stdout.flush()
    os.execv(sys.executable, [sys.executable, '-I', str(DOCDIFF), '--repo', str(where), '--base', 'google',
                              '--comments', str(where / 'comments.json'), '--docs', str(where / 'docs.json'),
                              '--new-label', f'Markdown of {repo.root.name}, as exported',
                              '--publish', f'{me} push {{file}} --target {{target}} && {me} export {{file}}',
                              '--refresh', f'{me} export', '--serve', *names])


def as_markdown(blocks):
    """Blocks [(kind, text, table rows or None)] as Markdown that renders each one as it reads."""
    esc = lambda s: re.sub(r'^(\d+)([.)])', r'\1\\\2', re.sub(r'([\\`*_\[\]<>#|~&+-])', r'\\\1', s))
    out, last = '', None
    for kind, text, rows in blocks:
        out += '' if last is None else '\n' if kind == last and kind in LIST_PRESET else '\n\n'
        if kind == 'table':
            cells = lambda row: '| ' + ' | '.join(esc(norm(c)) for c in row) + ' |'
            out += '\n'.join([cells(rows[0]), '|' + ' --- |' * len(rows[0]), *map(cells, rows[1:])])
        else:
            out += {'h1': '# ', 'h2': '## ', 'h3': '### ', 'ul': '- ', 'ol': '1. '}.get(kind, '') + esc(text)
        last = kind
    return out + '\n'


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--repo', default='.')
    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('status')
    s = sub.add_parser('push')
    s.add_argument('file')
    s.add_argument('--dry-run', action='store_true')
    s.add_argument('--force', action='store_true')
    s.add_argument('--target')
    s = sub.add_parser('create')
    s.add_argument('file')
    s.add_argument('--title')
    s = sub.add_parser('map')
    s.add_argument('file')
    s.add_argument('doc_id')
    s = sub.add_parser('export')
    s.add_argument('files', nargs='*')
    s.add_argument('--to')
    sub.add_parser('browse')
    a = p.parse_args()
    repo, g = Repo(a.repo), Google()
    name = getattr(a, 'file', None)
    if name and not (repo.root / name).exists():
        sys.exit(f'{name}: no such file in {repo.root}')
    if a.cmd == 'status':
        status(g, repo)
    elif a.cmd == 'browse':
        browse(g, repo)
    elif a.cmd == 'export':
        names = a.files or sorted(n for n in repo.map if (repo.root / n).exists())
        for n in names:
            if n not in repo.map:
                sys.exit(f'{n} has no Google Doc: create or map it first')
        export(g, repo, names, pathlib.Path(a.to) if a.to else repo.state / f'{repo.root.name}-google')
    elif a.cmd == 'map':
        repo.set_map(name, a.doc_id)
        print(f'{name} -> https://docs.google.com/document/d/{a.doc_id}/edit')
    elif a.cmd == 'create':
        if name in repo.map:
            sys.exit(f'{name} already has a Google Doc: {repo.map[name]}')
        repo.set_map(name, g.create(a.title or pathlib.Path(name).stem))
        push(g, repo, name, force=True)
    else:
        push(g, repo, name, a.dry_run, a.force, a.target)


if __name__ == '__main__':
    main()
