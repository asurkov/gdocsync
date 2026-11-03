#!/usr/bin/env python3
"""Keep Google Docs in step with a repo's Markdown docs.

Usage: gdocsync.py [--repo PATH] status
       gdocsync.py [--repo PATH] push FILE [--dry-run] [--force]
       gdocsync.py [--repo PATH] create FILE [--title TITLE]
       gdocsync.py [--repo PATH] map FILE DOC_ID

Markdown is the source; the Google Doc follows it. A push rewrites only the paragraphs whose text
changed, bottom up, so comments on the rest keep their anchors; then it restyles every paragraph and
sets every link. A link to another mapped Markdown file goes to that file's Google Doc, at the heading
its anchor names; any other relative link goes to the file on GitHub.

Before a push, the Google Doc is compared with what the last push left in it. If someone edited it
since, the push lists those paragraphs and stops: merge them into the Markdown first, or pass --force.

State lives in ~/.gdocsync/<repo name>/: map.json (file -> document id) and snapshots/ (each Google
Doc's paragraphs as the last push left them). Credentials are gdocs-mcp's, in ~/.gdocs-mcp.
"""
import argparse, difflib, json, os, pathlib, re, subprocess, sys, time, urllib.error, urllib.parse
import urllib.request

AUTH = pathlib.Path('~/.gdocs-mcp').expanduser()
STATE = pathlib.Path('~/.gdocsync').expanduser()
DOCS = 'https://docs.googleapis.com/v1/documents'
DRIVE = 'https://www.googleapis.com/drive/v3/files'
LIST_PRESET = {'ul': 'BULLET_DISC_CIRCLE_SQUARE', 'ol': 'NUMBERED_DECIMAL_ALPHA_ROMAN'}
HEADINGS = {'h1': 'HEADING_1', 'h2': 'HEADING_2', 'h3': 'HEADING_3'}
STYLE_FIELDS = 'bold,italic,underline,link,weightedFontFamily'


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
        q = urllib.parse.urlencode({'fields': 'comments(resolved,quotedFileContent/value)', 'pageSize': 100})
        return [c.get('quotedFileContent', {}).get('value', '') for c in self.call(f'{DRIVE}/{doc_id}/comments?{q}').get('comments', [])
                if not c.get('resolved')]


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
                    rows.append([c.strip() for c in lines[i].strip().strip('|').split('|')])
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


def norm(s):
    return ' '.join(s.split())


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
            cells = [''.join(r.get('textRun', {}).get('content', '') for c in cell['content'] if 'paragraph' in c
                             for r in c['paragraph']['elements']).strip()
                     for row in e['table']['tableRows'] for cell in row['tableCells']]
            out.append({'kind': 'table', 'text': ' | '.join(cells), 'start': e['startIndex'], 'end': e['endIndex'], 'table': e['table']})
    return out


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
            ts['link'] = {'url': links.url(st['link'])}
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


def push(g, repo, name, dry_run=False, force=False):
    doc_id = repo.map.get(name) or sys.exit(f'{name} has no Google Doc: create or map it first')
    md = (repo.root / name).read_text()
    mblocks, refs = md_blocks(md)
    doc = g.doc(doc_id)
    gblocks = doc_blocks(doc)
    edited = edited_since_push(repo, name, gblocks, mblocks)
    if edited is None and gblocks and not force:
        sys.exit(f'{name}: no record of a previous push, so edits made in the Google Doc cannot be told apart.\n'
                 f'Compare it by hand, then push with --force; later pushes will check.')
    if edited and not force:
        print(f'{name}: the Google Doc was edited since the last push. Merge these into the Markdown, or --force:')
        for t in edited:
            print('  ·', t[:160])
        sys.exit(2)
    ops = plan(gblocks, mblocks)
    comments = g.open_comments(doc_id)
    hit = [q for q in comments if q and any(q in gblocks[i]['text'] for op in ops for i in range(op[1], op[2]))]
    print(f'{name}: {len(ops)} change(s): {sum(o[2] - o[1] for o in ops)} paragraph(s) out, '
          f'{sum(o[4] - o[3] for o in ops)} in; {len(hit)} of {len(comments)} open comment(s) anchored in text that goes')
    for q in hit:
        print('  comment on:', q[:120])
    if dry_run:
        for op, i1, i2, j1, j2 in ops:
            for i in range(i1, i2):
                print('  -', gblocks[i]['text'][:140])
            for j in range(j1, j2):
                print('  +', md_key(*mblocks[j])[1][:140])
        return
    # Bottom up, one change at a time, reading the document again after each.
    for _ in range(len(ops) + len(mblocks) + 5):
        doc = g.doc(doc_id)
        gblocks = doc_blocks(doc)
        ops = plan(gblocks, mblocks)
        if not ops:
            break
        op, i1, i2, j1, j2 = ops[-1]
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
    restyle(g, repo, name, mblocks, refs)
    repo.save_snapshot(name, doc_blocks(g.doc(doc_id)))
    print(f'{name}: pushed to https://docs.google.com/document/d/{doc_id}/edit')


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


def restyle(g, repo, name, mblocks, refs):
    """Reset and reapply inline styles and links on every paragraph; text and comments stay."""
    doc_id = repo.map[name]
    doc = g.doc(doc_id)
    gblocks = doc_blocks(doc)
    if len(gblocks) != len(mblocks):
        sys.exit(f'{name}: {len(gblocks)} blocks in the Google Doc against {len(mblocks)} in the Markdown after the push')
    links = Links(g, repo, name, refs)
    links.headings(name, doc)
    reqs = []
    for b, (kind, body) in zip(gblocks, mblocks):
        if kind == 'table':
            continue
        reqs.append({'updateTextStyle': {'range': {'startIndex': b['start'], 'endIndex': b['end'] - 1}, 'textStyle': {},
                                         'fields': STYLE_FIELDS}})
        reqs += [r for r in style_requests(body, b['start'], links)]
    g.batch(doc_id, reqs, doc['revisionId'])


def status(g, repo):
    if not repo.map:
        print(f'{repo.root.name}: no Markdown file is mapped to a Google Doc yet')
    for name, doc_id in sorted(repo.map.items()):
        if not (repo.root / name).exists():
            print(f'{name}: mapped, but the file is gone')
            continue
        mblocks, _ = md_blocks((repo.root / name).read_text())
        gblocks = doc_blocks(g.doc(doc_id))
        ops = plan(gblocks, mblocks)
        edited = edited_since_push(repo, name, gblocks, mblocks)
        comments = len(g.open_comments(doc_id))
        state = 'in step' if not ops else f'{len(ops)} change(s) to push'
        since = 'never pushed by gdocsync' if edited is None else (f'{len(edited)} paragraph(s) edited in the Google Doc' if edited else 'not edited since')
        print(f'{name}: {state}; {since}; {comments} open comment(s)')
    unmapped = sorted(p.name for p in repo.root.glob('*.md') if p.name not in repo.map and p.name != 'README.md')
    if unmapped:
        print('not mapped:', ', '.join(unmapped))


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--repo', default='.')
    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('status')
    s = sub.add_parser('push')
    s.add_argument('file')
    s.add_argument('--dry-run', action='store_true')
    s.add_argument('--force', action='store_true')
    s = sub.add_parser('create')
    s.add_argument('file')
    s.add_argument('--title')
    s = sub.add_parser('map')
    s.add_argument('file')
    s.add_argument('doc_id')
    a = p.parse_args()
    repo, g = Repo(a.repo), Google()
    name = getattr(a, 'file', None)
    if name and not (repo.root / name).exists():
        sys.exit(f'{name}: no such file in {repo.root}')
    if a.cmd == 'status':
        status(g, repo)
    elif a.cmd == 'map':
        repo.set_map(name, a.doc_id)
        print(f'{name} -> https://docs.google.com/document/d/{a.doc_id}/edit')
    elif a.cmd == 'create':
        if name in repo.map:
            sys.exit(f'{name} already has a Google Doc: {repo.map[name]}')
        repo.set_map(name, g.create(a.title or pathlib.Path(name).stem))
        push(g, repo, name, force=True)
    else:
        push(g, repo, name, a.dry_run, a.force)


if __name__ == '__main__':
    main()
