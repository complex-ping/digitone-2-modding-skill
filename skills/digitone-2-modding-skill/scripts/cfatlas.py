#!/usr/bin/env python3
"""Firmware atlas: stock MAIN OS 1.11 as a queryable database. The ColdFire OS kept its C++ RTTI, so most classes, vtables and many
method names can be recovered from your own image.

    python3 cfatlas.py build              # $DN2_WORKSPACE/atlas/atlas.sqlite from extracted/section_3.{bin,dis}, in seconds
    python3 cfatlas.py stats              # functions, how much is named
    python3 cfatlas.py fn ADDR|NAME       # size, name, callers, callees, strings, table slots
    python3 cfatlas.py class PATTERN      # classes: base, subclasses, vtable slots (one to three hits)
    python3 cfatlas.py str PATTERN        # strings matching a regex, and who uses them
    python3 cfatlas.py unknown [N]        # the most-called functions nobody has named: where naming pays most
    python3 cfatlas.py name ADDR NAME [conf] [why...]   # record a finding in atlas/names.tsv; it survives rebuilds

The database is made from your image: keep it private. Function slots are named Class::vN until someone reads the code.
"""
import bisect, os, pathlib, re, shutil, sqlite3, struct, subprocess, sys

BASE, TEXT_END, FAST, CODE_END = 0x40000400, 0x401DC000, 0x402EF000, 0x40400000    # OS 1.11: code, data tables, fast code
LINE = re.compile(r'^([0-9a-f]{8}):\t(?:[0-9a-f]{4} ?)+\s*\t?(.*)$')
CALL = re.compile(r'^(jsr|bsr[wlb]?) 0x([0-9a-f]{8})$')
ABS = re.compile(r'0x(4[0-7][0-9a-f]{6}|8000[0-9a-f]{4})\b')
DEC = re.compile(r'(?<![\w$])(10[7-9]\d{7}|11\d{8}|120\d{7})\b')   # objdump prints move / cmp immediates in decimal
MANGLED = re.compile(rb'\*?(?:Z?N?S?t?\d+[A-Za-z_]|Z?N\d|St\d)[\x21-\x7e]*\x00')


def paths():
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import dn2paths as P
    out = P.WS / 'atlas'
    return P, out, out / 'atlas.sqlite', out / 'names.tsv'


def operands(t):
    """Every absolute address an instruction names, hex or decimal."""
    return [int(m.group(1), 16) for m in ABS.finditer(t)] + [x for x in map(int, DEC.findall(t)) if 0x40000000 <= x < 0x48000000]


def in_code(x):
    return BASE <= x < TEXT_END or FAST <= x < CODE_END


def demangle(raw):
    """Typeinfo name -> C++ name; the raw name when no c++filt is around."""
    tool = next((t for t in [pathlib.Path(os.environ.get('DN2_M68K_BIN', '.')) / 'm68k-linux-gnu-c++filt', shutil.which('m68k-linux-gnu-c++filt'),
                             shutil.which('c++filt')] if t and pathlib.Path(t).exists()), None)
    if not tool:
        return list(raw)
    r = subprocess.run([str(tool)], input='\n'.join('_ZTS' + x for x in raw), capture_output=True, text=True).stdout.split('\n')
    return [v.replace('typeinfo name for ', '', 1) if v.startswith('typeinfo name for ') else x for x, v in zip(raw, r)]


def read_rtti(mem, starts, base=BASE, lo=None, hi=None):
    """Every typeinfo {vptr, name, [base]} and every vtable {offset, typeinfo, slots...} in the data tables of an image at `base`."""
    lo = TEXT_END - base if lo is None else lo
    hi = min(FAST - base, len(mem)) if hi is None else hi
    word = lambda off: struct.unpack_from('>I', mem, off)[0]
    cands, names = [], {}
    for off in range(lo, hi - 8, 2):
        n = word(off + 4)
        if base + lo <= n < base + hi and base + lo <= word(off) < base + hi and MANGLED.match(mem, n - base):
            cands.append((base + off, word(off), n))
            names[n] = mem[n - base:mem.index(b'\0', n - base)].decode('ascii').lstrip('*')
    keys = list(names)
    dem = dict(zip(keys, demangle([names[k] for k in keys])))
    infos = {t: {'vptr': v, 'name': dem[n], 'raw': names[n], 'base': None} for t, v, n in cands}
    for t, i in infos.items():                                # __si_class_type_info: one base after the name; __vmi: flags, count, bases
        off = t - base
        if word(off + 8) in infos:
            i['base'] = word(off + 8)
        elif off + 20 <= len(mem) and word(off + 12) in range(1, 16) and word(off + 16) in infos:
            i['base'] = word(off + 16)
    vtables = []
    for off in range(lo, hi - 8, 2):
        t, top = word(off + 4), word(off)
        if t in infos and (top < 0x100000 or top >= 0xfff00000):
            slots, o = [], off + 8
            while o + 4 <= hi and word(o) in starts:
                slots.append(word(o))
                o += 4
            if slots:
                vtables.append({'addr': base + off + 8, 'info': t, 'top': top, 'slots': slots})
    return {'infos': infos, 'vtables': vtables}


def name_from_rtti(rtti, refs):
    """{function: (name, why)} from vtable slots, vtable loads (constructors) and std::function lambdas. refs: {address: functions naming it}."""
    infos, auto, holders = rtti['infos'], {}, {}
    def ancestors(t):
        out = []
        while infos[t]['base'] in infos and infos[t]['base'] not in out:
            t = infos[t]['base']
            out.append(t)
        return out
    for v in rtti['vtables']:
        for k, x in enumerate(v['slots']):
            holders.setdefault(x, []).append((v['info'], k))
    for x, hs in holders.items():                             # the base-most class that holds the slot
        ts = {t for t, _ in hs}
        roots = [h for h in hs if not any(a in ts for a in ancestors(h[0]))]
        t, k = roots[0]
        auto[x] = (f"{infos[t]['name']}::v{k}" + ('' if len(roots) == 1 else f'+{len(roots) - 1}'), f'vtable slot, in {len(ts)} classes')
    for v in rtti['vtables']:
        for f in refs.get(v['addr'], ()):
            auto.setdefault(f, (infos[v['info']]['name'] + '::ctor_dtor', 'loads the vtable'))
    for t, i in infos.items():                                # std::function's manager names the typeinfo; the method making it names the manager
        m = re.match(r'^(.*?)::\{lambda\(.*$', i['name'])
        if m:
            for mgr in refs.get(t, ()):
                auto.setdefault(mgr, (i['name'] + '::manager', 'std::function manager'))
                for host in refs.get(mgr, ()):
                    auto.setdefault(host, (m.group(1), 'makes ' + i['name']))
    return auto


def read_names(path):
    out = {}
    if path.exists():
        for l in path.read_text().splitlines():
            if l.strip() and not l.startswith('#'):
                p = l.split('\t')
                out[int(p[0], 16)] = (p[1], p[2] if len(p) > 2 else 'guess', p[3] if len(p) > 3 else '')
    return out


def build():
    P, out, db_path, names_path = paths()
    dis, bin_ = P.EXTRACTED / 'section_3.dis', P.SEC3_STOCK
    if not dis.exists():
        sys.exit(f'{dis} missing: run cfx.sh once (e.g. cfx.sh fn 0x40000400: it makes the disassembly)')
    out.mkdir(exist_ok=True)
    mem = bin_.read_bytes()
    insns = []
    for l in open(dis, errors='replace'):
        m = LINE.match(l)
        if m and in_code(int(m.group(1), 16)):
            insns.append((int(m.group(1), 16), m.group(2).strip()))
    insns.sort()
    starts = {a for a, _ in insns}
    after_end = {b[0]: a[1].split(' ')[0] in ('rts', 'rte', 'unlk', 'nop') or a[1].startswith('jmp') for a, b in zip(insns, insns[1:])}
    seeds, calls = {}, []
    for a, t in insns:
        m = CALL.match(t)
        if m and int(m.group(2), 16) in starts:
            seeds.setdefault(int(m.group(2), 16), 'call')
            calls.append((a, int(m.group(2), 16)))
        elif t.startswith('link'):
            seeds.setdefault(a, 'link')
        elif not t.startswith(('b', 'j')):
            for x in operands(t):
                if x in starts and after_end.get(x):          # a code address taken right after a function's end: a callback
                    seeds.setdefault(x, 'ptr')
    rtti = read_rtti(mem, starts)
    for v in rtti['vtables']:
        for x in v['slots']:
            seeds.setdefault(x, 'vtable')
    entries = sorted(seeds)
    owner = lambda a: entries[max(bisect.bisect_right(entries, a) - 1, 0)]
    strs = {BASE + TEXT_END - BASE + m.start(): m.group()[:-1].decode('ascii')
            for m in re.finditer(rb'[\x20-\x7e\t\n]{3,}\x00', mem[TEXT_END - BASE:min(FAST - BASE, len(mem))])}
    refs, struse, ninsn, refmap = [], [], {}, {}
    for a, t in insns:
        o = owner(a)
        ninsn[o] = ninsn.get(o, 0) + 1
        if not CALL.match(t):
            for x in operands(t):
                (struse if x in strs else refs).append((o, a, x))
                refmap.setdefault(x, set()).add(o)
    auto, names = name_from_rtti(rtti, refmap), read_names(names_path)
    db_path.unlink(missing_ok=True)
    c = sqlite3.connect(db_path)
    c.executescript("""create table fn(addr integer primary key, end integer, seed text, ninsn integer, name text, conf text, why text);
        create table call(src integer, site integer, dst integer); create table ref(fn integer, site integer, target integer);
        create table str(addr integer primary key, text text); create table struse(fn integer, site integer, addr integer);
        create table cls(info integer primary key, name text, raw text, base integer);
        create table vslot(vt integer, cls integer, top integer, slot integer, target integer);""")
    rows = []
    for i, e in enumerate(entries):
        nm, conf, why = names.get(e) or ((auto[e][0], 'rtti', auto[e][1]) if e in auto else (None, None, None))
        rows.append((e, entries[i + 1] if i + 1 < len(entries) else TEXT_END, seeds[e], ninsn.get(e, 0), nm, conf, why))
    c.executemany('insert into fn values(?,?,?,?,?,?,?)', rows)
    c.executemany('insert into call values(?,?,?)', [(owner(a), a, x) for a, x in calls])
    c.executemany('insert into ref values(?,?,?)', refs)
    c.executemany('insert into str values(?,?)', strs.items())
    c.executemany('insert into struse values(?,?,?)', struse)
    c.executemany('insert into cls values(?,?,?,?)', [(t, i['name'], i['raw'], i['base']) for t, i in rtti['infos'].items()])
    c.executemany('insert into vslot values(?,?,?,?,?)', [(v['addr'], v['info'], v['top'], k, x) for v in rtti['vtables'] for k, x in enumerate(v['slots'])])
    for n, idx in enumerate(('call(dst)', 'call(src)', 'ref(target)', 'struse(fn)', 'struse(addr)', 'vslot(target)', 'vslot(cls)')):
        c.execute(f'create index i{n} on {idx}')
    c.commit()
    print(f'{len(entries):,} functions, {len(calls):,} direct calls, {len(strs):,} strings, {len(rtti["infos"]):,} classes -> {db_path}')


def db():
    p = paths()[2]
    if not p.exists():
        sys.exit('no atlas yet: python3 cfatlas.py build')
    return sqlite3.connect(p)


def fname(c, a):
    r = c.execute('select name from fn where addr=?', (a,)).fetchone()
    return f'{a:#010x}' + (f' {r[0]}' if r and r[0] else '')


def stats():
    c = db()
    q = lambda s: c.execute(s).fetchone()
    n, ins = q('select count(*), sum(ninsn) from fn')
    nm, ni = q('select count(*), coalesce(sum(ninsn),0) from fn where name is not null')
    print(f'functions {n:,} ({ins:,} instructions); named {nm:,} ({100 * ni / ins:.1f} % of instructions)')
    print(f'classes {q("select count(*) from cls")[0]:,}, with a vtable {q("select count(distinct cls) from vslot")[0]:,}')


def show_fn(arg):
    c = db()
    cols = 'addr,end,seed,ninsn,name,conf,why'
    r = c.execute(f'select {cols} from fn where name=?', (arg,)).fetchone() or \
        c.execute(f'select {cols} from fn where addr<=? order by addr desc limit 1', (int(arg, 16),)).fetchone()
    if not r:
        sys.exit(f'{arg}: not in a function')
    a = r[0]
    print(f'{a:#010x}..{r[1]:#010x} {r[3]} insns name={r[4]} conf={r[5]} {r[6] or ""}')
    cs = [s for s, in c.execute('select distinct src from call where dst=?', (a,))]
    ds = [d for d, in c.execute('select distinct dst from call where src=?', (a,))]
    print(f'callers {len(cs)}: ' + ', '.join(fname(c, s) for s in cs[:12]) + (' ...' if len(cs) > 12 else ''))
    print(f'callees {len(ds)}: ' + ', '.join(fname(c, d) for d in ds[:16]) + (' ...' if len(ds) > 16 else ''))
    for vt, k in c.execute('select vt,slot from vslot where target=? limit 6', (a,)):
        print(f'vtable {vt:#010x} +{4 * k}')
    for t, in c.execute('select distinct s.text from struse u join str s on s.addr=u.addr where u.fn=? limit 10', (a,)):
        print(f'string {t!r}')


def show_class(pat):
    c = db()
    rx = re.compile(pat, re.I)
    hits = [r for r in c.execute('select info,name,base from cls') if rx.search(r[1])]
    for info, nm, base in hits[:40]:
        b = c.execute('select name from cls where info=?', (base,)).fetchone()
        print(f'{info:#010x} {nm}' + (f' : {b[0]}' if b else ''))
        if len(hits) <= 3:
            for vt, top in c.execute('select distinct vt,top from vslot where cls=?', (info,)).fetchall():
                print(f'  vtable {vt:#010x} (top {top})')
                for k, x in c.execute('select slot,target from vslot where vt=? order by slot', (vt,)):
                    print(f'    +{4 * k:<4} {fname(c, x)}')
    print(f'{len(hits)} classes')


def strings(pat):
    c = db()
    rx, n = re.compile(pat, re.I), 0
    for a, t in c.execute('select addr,text from str'):
        if rx.search(t):
            n += 1
            if n <= 40:
                fs = [fname(c, f) for f, in c.execute('select distinct fn from struse where addr=? limit 4', (a,))]
                print(f'{a:#010x} {t!r:50.50} {", ".join(fs)}')
    print(f'{n} strings')


def unknown(n=30):
    c = db()
    for a, k, i in c.execute('select f.addr,count(distinct l.src),f.ninsn from fn f join call l on l.dst=f.addr where f.name is null '
                             'group by f.addr order by 2 desc limit ?', (int(n),)).fetchall():
        print(f'{a:#010x} {k:5} callers {i:6} insns')


def name(addr, nm, conf='guess', *why):
    _, out, db_path, names_path = paths()
    if not re.match(r'^[A-Za-z_][\w:]*$', nm):
        sys.exit(f'bad name {nm!r}')
    a, cur = int(addr, 16), read_names(names_path)
    cur[a] = (nm, conf, ' '.join(why))
    out.mkdir(exist_ok=True)
    names_path.write_text('# address, name, confidence (verified / read / guess), why\n' +
                          ''.join(f'{k:#010x}\t{v[0]}\t{v[1]}\t{v[2]}\n' for k, v in sorted(cur.items())))
    if db_path.exists():
        c = db()
        c.execute('update fn set name=?,conf=?,why=? where addr=?', (nm, conf, ' '.join(why), a))
        c.commit()
    print(f'{a:#010x} = {nm} ({conf})')


if __name__ == '__main__':
    args = sys.argv[1:] or ['help']
    cmds = {'build': build, 'stats': stats, 'fn': show_fn, 'class': show_class, 'str': strings, 'unknown': unknown, 'name': name}
    if args[0] not in cmds:
        sys.exit(__doc__)
    cmds[args[0]](*args[1:])
