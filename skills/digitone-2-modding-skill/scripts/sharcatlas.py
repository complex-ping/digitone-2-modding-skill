#!/usr/bin/env python3
"""SHARC atlas: section 7 as a queryable database. Section 7 is byte-identical on OS 1.11 and 1.12. No RTTI here: names come from
reading, calls, strings and behaviour checks (sharc_checks.py).

    python3 sharcatlas.py build               # $DN2_WORKSPACE/atlas/sharc.sqlite from extracted/section_7.bin
    python3 sharcatlas.py stats | fn ADDR|NAME | unknown [N] | dis ADDR|NAME [N]
    python3 sharcatlas.py reread [CHARS]      # next unread functions as compact assembly: calls and returns folded, labels only where jumped to
    python3 sharcatlas.py confirm < NOTES     # lines `ADDR [name]|role` after reading: marks them read
    python3 sharcatlas.py name ADDR NAME [conf] [why...]

Addresses are short words, as in references/sharc.md. Findings live in atlas/names_sharc.tsv and survive rebuilds.
The database is made from your image: keep it private.
"""
import bisect, collections, os, re, sqlite3, struct, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dn2paths as P
P.use_dnfw()
import visa
OUT = P.WS / 'atlas'
DB, NAMES = OUT / 'sharc.sqlite', OUT / 'names_sharc.tsv'
CODE = [(0x20000000, 0x1e880), (0x2001e888, 0x699b4), (0x28380000, 0x25c0), (0x283825c4, 0x19a38)]
HEX = re.compile(r'0x[0-9a-f]+')


def read_names():
    out = {}
    if NAMES.exists():
        for l in open(NAMES):
            if l.strip() and not l.startswith('#'):
                a, n, c, w = (l.rstrip('\n').split('\t') + ['', '', ''])[:4]
                out[int(a, 16)] = (n, c, w)
    return out


def write_names(d):
    with open(NAMES, 'w') as f:
        f.write('# addr\tname\tconfidence (guess, read, verified)\twhy\n')
        for a in sorted(d):
            f.write(f'{a:#x}\t' + '\t'.join(d[a]) + '\n')


def decode_all(img):
    ins = []
    for base, n in CODE:
        ins += list(img.linear(visa.Image.load_to_sw(base), end=visa.Image.load_to_sw(base + n), stop_invalid=False))
    return ins


def build():
    OUT.mkdir(parents=True, exist_ok=True)
    img = visa.Image()
    ins = decode_all(img)
    pcs = {i.sw for i in ins}
    calls, jumps, ptrs = [], [], set()
    for i in ins:
        t = i.text
        if re.search(r'\b(CALL|CJUMP|JUMP)\b', t):
            for h in HEX.findall(t):
                v = int(h, 16)
                if v in pcs:
                    (calls if re.search(r'\b(CALL|CJUMP)\b', t) else jumps).append((i.sw, v))
        else:
            for h in re.findall(r'I\d+ = (0x[0-9a-f]+)', t):       # a code pointer in an index register: C function pointers (17a I12 = ...)
                v = int(h, 16)
                if v in pcs:
                    ptrs.add(v)
    # table pointers: a 32-bit data word that is an instruction start and follows a return (or starts code)
    rets = {i.sw + i.size for i in ins if re.match(r'(IF \S+ )?(RTS|RFRAME|RTI)', i.text) or 'RFRAME' in i.text}
    data = set()
    for base, d in img.regions:
        for o in range(0, len(d) - 3, 4):
            v = struct.unpack_from('<I', d, o)[0]
            if v in pcs and v in rets:
                data.add(v)
    starts = {v for _, v in calls} | data | (ptrs & rets) | {visa.Image.load_to_sw(b) for b, _ in CODE}
    order = sorted(pcs)
    st = sorted(starts)
    c = sqlite3.connect(str(DB))
    c.executescript('drop table if exists fn; drop table if exists call; create table fn(addr integer primary key,end integer,ninsn integer,name text,conf text,why text,seed text,callers integer);create table call(src integer,dst integer,fn integer);')
    names = read_names()
    cnt = collections.Counter()
    for i in ins:
        cnt[bisect.bisect_right(st, i.sw) - 1] += 1
    callers = collections.Counter(v for _, v in calls)
    rows = []
    for k, a in enumerate(st):
        end = st[k + 1] if k + 1 < len(st) else order[-1] + 1
        n = names.get(a, (None, None, None))
        seed = 'call' if callers[a] else ('table' if a in data else 'region')
        rows.append((a, end, cnt[k], n[0], n[1], n[2], seed, callers[a]))
    c.executemany('insert into fn values(?,?,?,?,?,?,?,?)', rows)
    c.executemany('insert into call values(?,?,?)', [(s, d, st[bisect.bisect_right(st, s) - 1]) for s, d in calls])
    c.commit()
    print(f'{len(ins):,} instructions, {len(rows):,} functions ({len(calls):,} calls, {len(data):,} from data tables), {len(names):,} named')


def db():
    return sqlite3.connect(str(DB))


def addr_of(c, x):
    if re.match(r'^(0x)?[0-9a-fA-F]+$', x):
        return int(x, 16)
    r = c.execute('select addr from fn where name=?', (x,)).fetchone()
    return r[0] if r else sys.exit('no ' + x)


def stats():
    c = db()
    tot, n = c.execute('select sum(ninsn),count(*) from fn').fetchone()
    print(f'functions {n:,}, {tot:,} instructions')
    for conf, k, s in c.execute("select coalesce(conf,'none'),count(*),sum(ninsn) from fn group by 1 order by 3 desc"):
        print(f'  {conf:9} {k:6,} functions  {100 * s / tot:5.1f} % of instructions')


def fn(x):
    c = db()
    a = addr_of(c, x)
    r = c.execute('select * from fn where addr<=? order by addr desc limit 1', (a,)).fetchone()
    print(f'{r[0]:#x}..{r[1]:#x} {r[2]} insns name={r[3]} conf={r[4]} {r[5] or ""} seed={r[6]}')
    print('callers:', ' '.join(f'{s:#x}' for (s,) in c.execute('select src from call where dst=? limit 12', (r[0],))))
    print('calls:', ' '.join(f'{d:#x}' for (d,) in c.execute('select distinct dst from call where fn=? limit 30', (r[0],))))


def unknown(n=30):
    for a, e, k, cl in db().execute('select addr,end,ninsn,callers from fn where name is null order by callers desc,ninsn desc limit ?', (int(n),)):
        print(f'{a:#x} {k:5} insns {cl:4} callers')


def dis(x, n=60):
    img = visa.Image()
    a = addr_of(db(), x)
    for i in img.linear(a, count=int(n), stop_invalid=False):
        print(f'{i.sw:#x}  {i.text}')


SAVE = re.compile(r'^(DM\(-\d+, I6\) = [RI]\d+|[RI]\d+ = DM\(-\d+, I6\)|R2 = I\d+|MODIFY\(I7, -\d+\) \(NW\))$')


def _compact(ins, names, targets):
    """One function as short lines: labels only where jumped to, calls and returns folded, float constants as numbers."""
    out = []
    skip_ret = False
    for i in ins:
        t = i.text.strip()
        if t.startswith('.word') or t == 'NOP':
            continue
        if i.sw in targets:
            out.append(f'{i.sw & 0xfff:03x}:')
        m = re.match(r'^(.*?)\s*;\s*=\s*(\S+)$', t)
        if m:
            t = re.sub(r'0x[0-9a-f]+', m.group(2), m.group(1))
        if re.match(r'^DM\(I7, M7\) = 0x00[0-9a-f]{6}$', t):
            continue
        if t in ('RFRAME', 'I12 = DM(M7, I6)') or SAVE.match(t):
            continue
        t = re.sub(r', I12 = DM\(M7, I6\)$', '', t)
        t = re.sub(r'^I12 = DM\(M7, I6\), ', '', t)
        if t.startswith('JUMP (M14, I12)'):
            t = 'ret' + (t[t.index(',') + 1:].replace('I12)', '').strip().rjust(1) if t.count(',') > 1 else '')
        def nm(mm):
            a = int(mm.group(2), 16)
            return (mm.group(1) + ' ' + names[a]) if a in names and names[a] else mm.group(0)
        t = re.sub(r'\b(CALL|CJUMP|JUMP)\s+(0x[0-9a-f]+)', nm, t)
        t = re.sub(r'\b(JUMP|CALL|CJUMP) 0x[0-9a-f]*([0-9a-f]{3})\b', lambda mm: f'{mm.group(1)} {mm.group(2)}', t)
        t = t.replace('CJUMP', 'call').replace(' (DB)', '').replace('DM(I7, M7) = ', 'push ')
        t = t.replace('MODIFY(I7, -2) (NW)', '').replace('(SSI)', '').strip()
        if t:
            out.append(t)
    return out


def reread(chars=24000):
    """The next unread functions, by address, as compact assembly."""
    img = visa.Image()
    c = db()
    names = {a: n for a, n in c.execute('select addr,name from fn')}
    rows = c.execute("select addr,end,ninsn,name,callers from fn where conf is null or conf not in ('read','verified') order by addr").fetchall()
    size = shown = 0
    for a, e, ni, nm, cl_ in rows:
        ins = [i for i in img.linear(a, end=e, stop_invalid=False)]
        targets = set()
        for i in ins:
            for m in re.finditer(r'\b(?:JUMP|LCNTR.*UNTIL\s+LCE|UNTIL)\s*(0x[0-9a-f]+)', i.text):
                targets.add(int(m.group(1), 16))
        lines = _compact(ins, names, targets)
        text = f'={a:x} {nm or ""} {ni}i c{cl_}\n' + '\n'.join(lines)
        if size + len(text) > int(chars) and shown:
            break
        print(text[:int(chars)])
        size += len(text) + 1
        shown += 1
    print(f'## {shown} functions, {size:,} chars; {len(rows)} left, {sum(r[2] for r in rows):,} instructions')


def confirm():
    """Lines: ADDR [name]|role after reading; marks read."""
    d = read_names()
    c = db()
    n = 0
    for l in sys.stdin:
        l = l.strip()
        if not l or l.startswith('#'):
            continue
        a, rest = l.split(' ', 1)
        a = int(a, 16)
        nm, _, role = rest.partition('|')
        old = c.execute('select name from fn where addr=?', (a,)).fetchone()
        if old is None:
            sys.exit(f'no function at {a:#x}')
        nm = nm.strip() or old[0]
        d[a] = (nm, 'read', role.strip())
        c.execute("update fn set name=?,conf='read',why=? where addr=?", (nm, role.strip(), a))
        n += 1
    write_names(d)
    c.commit()
    print(n, 'confirmed')


def name(a, nm, conf='read', *why):
    d = read_names()
    d[int(a, 16)] = (nm, conf, ' '.join(why))
    write_names(d)
    c = db()
    c.execute('update fn set name=?,conf=?,why=? where addr=?', (nm, conf, ' '.join(why), int(a, 16)))
    c.commit()


if __name__ == '__main__':
    cmds = dict(build=build, stats=stats, fn=fn, unknown=unknown, dis=dis, reread=reread, confirm=confirm, name=name)
    cmds[sys.argv[1] if len(sys.argv) > 1 else 'stats'](*sys.argv[2:])
