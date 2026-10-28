"""simc.py -- sim.Sim with its run loop and memory in C (simc_core.c, built with cffi on first import).

CSim is a drop-in sim.Sim: Python keeps the registers between runs (everything outside run() sees the usual attributes),
memory lives in C pages (CMemory: mem.words is a live mapping, pickles as a dict). run() copies the registers into C, runs
there, and copies them back. Python still runs: hooked runs (on_insn / trace / mem.watch / mem.on_uninit / a patched
mem.r or mem.w), and single SLOW instructions (DO loops on first execution: hwrules; same-DAG rule breakers; IDLE; types
or access sizes the core does not take) through sim.Sim.step. simjit_check.py --c compares it with sim.Sim block by block.

    import simc; s = sim.load_state(path, image, cls=simc.CSim)
DN2_SIM=py makes dn2frame use the plain interpreter.
"""
import hashlib, os, sys, importlib.util
from collections.abc import MutableMapping
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sim, visa
from visa import B

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, 'simc_core.c')
M32 = 0xFFFFFFFF

TYPES = ['1a', '1b', '2a', '2b', '2c', '3a', '3b', '3c', '3d', '4a', '4b', '4d', '5a_move', '5b_move', '5a_swap', '5b_swap',
         '6a_nomem', '6a_mem', '7a', '7a_nw', '7b', '7d', '8a', '9a', '9b', '11a', '11c', '12a_imm', '12a_ureg', '13a',
         '14a', '14d', '15a', '15b', '16a', '16b', '17a', '17b', '18a', '19a', '20a', '21a/22a/26a', '21c/22c', '25a', '25c',
         '25a_rframe']
TID = {t: k for k, t in enumerate(TYPES)}
T_UNKNOWN = len(TYPES)
F_DB, F_RTI, F_SLOW = 1, 2, 8
R_OK, R_UNTIL, R_STOP, R_MAX, R_DECODE, R_SLOW, R_ERR = range(7)
SYSNAMES = [visa.ureg_name(0x60 | r) for r in range(16)] + [visa.ureg_name(0x70 | r) for r in range(16)]

CDEF = """
typedef struct Sim Sim;
typedef struct { int64_t start, end, count; int has_count, cond, is_push; } LoopE;
typedef struct { uint32_t ax, ay, m1; } StE;
typedef struct {
    uint32_t R[2][16]; uint32_t ast[2], sticky[2];
    uint64_t mr_lo[2][2]; int64_t mr_hi[2][2];
    uint64_t bff[2]; int64_t bffwrp[2]; int has_bff[2], has_bffwrp[2];
    uint32_t I[16], M[16], L[16], Bx[16];
    uint32_t sysv[32]; unsigned char sysp[32];
    uint32_t PX1, PX2;
    int64_t pc, icount, lcntr; int has_lcntr;
    int npc, nl, ns;
    int pend, pend_k; int64_t pend_t;
    uint64_t misaligned;
    int64_t until[8]; int nuntil; int64_t stopat[16]; int nstop;
} SyncRegs;
Sim *simc_new(void *handle);
void simc_free(Sim *s);
void simc_mem_reset(Sim *s);
void simc_set_uop(Sim *s, int64_t pc, int type, uint64_t x, int64_t target, int size, int flags, int asz, int asx, int alw);
void simc_clear_uops(Sim *s);
uint32_t simc_rw(Sim *s, uint32_t pa);
void simc_ww(Sim *s, uint32_t pa, uint32_t v);
int simc_written(Sim *s, uint32_t pa, uint32_t *out);
void simc_unwrite(Sim *s, uint32_t pa);
int64_t simc_dump(Sim *s, uint32_t *keys, uint32_t *vals);
void simc_load(Sim *s, const uint32_t *keys, const uint32_t *vals, int64_t n);
uint32_t simc_mem_r(Sim *s, uint32_t a, int size, int sx);
void simc_mem_w(Sim *s, uint32_t a, uint32_t v, int size);
void simc_get(Sim *s, SyncRegs *o);
void simc_put(Sim *s, const SyncRegs *o);
void simc_loops_alloc(Sim *s, int n);
int64_t *simc_pcstk(Sim *s);
LoopE *simc_loops(Sim *s);
StE *simc_ststk(Sim *s);
const char *simc_errmsg(Sim *s);
int simc_errcode(Sim *s);
int64_t simc_need_pc(Sim *s);
int64_t simc_n_done(Sim *s);
int simc_ntypes(void);
int simc_run(Sim *s, int64_t max_insns);
void simc_brk_unset(Sim *s, int64_t pc);
void simc_brk_set(Sim *s, int64_t pc);
void simc_brk_on(Sim *s, int on);
void simc_wlog(Sim *s, int on);
uint32_t *simc_wlog_buf(Sim *s, int64_t *n);
void simc_wlog_clear(Sim *s);
extern "Python+C" int simc_fill(void *h, uint32_t pagebase, uint32_t *out);
extern "Python+C" uint32_t simc_mmr_r(void *h, uint32_t a);
extern "Python+C" void simc_mmr_w(void *h, uint32_t a, uint32_t v);
"""


def _build():
    """Compile (once per source version, per interpreter) into build_simc/; returns the cffi module."""
    tag = hashlib.sha256(open(SRC, 'rb').read() + CDEF.encode()).hexdigest()[:12]
    name = f'_simc_{tag}'
    out = os.path.join(HERE, 'build_simc')
    os.makedirs(out, exist_ok=True)
    import sysconfig
    so = os.path.join(out, name + (sysconfig.get_config_var('EXT_SUFFIX') or '.so'))
    if not os.path.exists(so):
        import cffi, tempfile, shutil, fcntl
        with open(os.path.join(out, '.lock'), 'w') as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            if not os.path.exists(so):
                ffi = cffi.FFI()
                ffi.cdef(CDEF)
                ffi.set_source(name, f'#include "{SRC}"', extra_compile_args=['-O2', '-ffp-contract=off', '-fno-fast-math', '-w'])
                tmp = tempfile.mkdtemp(dir=out)
                import warnings, contextlib, io
                with warnings.catch_warnings(), contextlib.redirect_stdout(io.StringIO()):
                    warnings.simplefilter('ignore')
                    built = ffi.compile(tmpdir=tmp)
                os.replace(built, so)
                shutil.rmtree(tmp, ignore_errors=True)
    spec = importlib.util.spec_from_file_location(name, so)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _build()
ffi, lib = _mod.ffi, _mod.lib
assert lib.simc_ntypes() == T_UNKNOWN, 'simc.TYPES and the C enum differ'

_PAGES = {}                                     # (regions id, page) -> 1024 words from the image (shared by all sims)


def _image_page(mem, pagebase):
    key = (id(mem.image), pagebase)
    words = _PAGES.get(key)
    if words is not None:
        return words
    la0 = pagebase + 0x28000000 if 0x240000 <= pagebase < 0x3C0000 else pagebase
    hit = [(b, d) for b, d in mem.regions if b < la0 + 4096 and la0 - 3 < b + len(d)]
    import struct
    if len(hit) == 1 and hit[0][0] <= la0 and la0 + 4096 <= hit[0][0] + len(hit[0][1]):
        b, d = hit[0]
        words = list(struct.unpack_from('<1024I', d, la0 - b))
    else:
        words = []
        for k in range(1024):
            la = la0 + 4 * k
            v = None
            for b, d in hit:
                if b <= la < b + len(d) - 3:
                    v = struct.unpack_from('<I', d, la - b)[0]; break
                if b <= la < b + len(d):
                    v = struct.unpack_from('<I', d[la - b:] + b'\0\0\0', 0)[0]; break
            if v is None:
                v = mem.on_uninit(pagebase + 4 * k) & M32 if mem.on_uninit else 0
            words.append(v)
    if not mem.on_uninit:
        _PAGES[key] = words
    return words


@ffi.def_extern()
def simc_fill(h, pagebase, out):
    try:
        s = ffi.from_handle(h)
        for k, v in enumerate(_image_page(s.mem, pagebase)):
            out[k] = v
        return 0
    except BaseException as e:                 # (reported by the core as an error; re-raised by run)
        ffi.from_handle(h)._cb_exc = e
        return 1


@ffi.def_extern()
def simc_mmr_r(h, a):
    s = ffi.from_handle(h)
    hook = s.mem.read_hooks.get(a)
    if hook is None:
        return s.mem.mmr.get(a, 0) & M32
    s._pull()
    v = hook(s) & M32
    s._push()
    return v


@ffi.def_extern()
def simc_mmr_w(h, a, v):
    s = ffi.from_handle(h)
    s.mem.mmr[a] = v
    hook = s.mem.write_hooks.get(a)
    if hook:
        s._pull(); hook(s, v); s._push()


class Words(MutableMapping):
    """mem.words over the C pages: the words the program (or Python) wrote."""
    def __init__(self, c): self.c = c; self._out = ffi.new('uint32_t *')
    def __getitem__(self, k):
        if lib.simc_written(self.c, k, self._out): return self._out[0]
        raise KeyError(k)
    def get(self, k, default=None):
        return self._out[0] if lib.simc_written(self.c, k, self._out) else default
    def __contains__(self, k): return bool(lib.simc_written(self.c, k, self._out))
    def __setitem__(self, k, v): lib.simc_ww(self.c, k, v & M32)
    def __delitem__(self, k):
        if k not in self: raise KeyError(k)
        lib.simc_unwrite(self.c, k)
    def _arrays(self):
        n = lib.simc_dump(self.c, ffi.NULL, ffi.NULL)
        keys, vals = ffi.new('uint32_t[]', max(n, 1)), ffi.new('uint32_t[]', max(n, 1))
        lib.simc_dump(self.c, keys, vals)
        return n, keys, vals
    def items(self):
        n, keys, vals = self._arrays()
        return [(keys[i], vals[i]) for i in range(n)]
    def __iter__(self):
        n, keys, _ = self._arrays()
        return iter([keys[i] for i in range(n)])
    def __len__(self): return lib.simc_dump(self.c, ffi.NULL, ffi.NULL)
    def copy(self): return dict(self.items())
    def __reduce__(self): return (dict, (self.items(),))
    def __eq__(self, o): return dict(self.items()) == (dict(o.items()) if hasattr(o, 'items') else o)


class CMemory(sim.Memory):
    def __init__(self, image, c):
        self.c = c
        self._words = Words(c)
        super().__init__(image)

    @property
    def words(self): return self._words

    @words.setter
    def words(self, d):
        lib.simc_mem_reset(self.c)
        items = list(d.items())
        n = len(items)
        if n:
            keys = ffi.new('uint32_t[]', [k for k, _ in items]); vals = ffi.new('uint32_t[]', [v & M32 for _, v in items])
            lib.simc_load(self.c, keys, vals, n)

    def rw(self, pa):
        return lib.simc_rw(self.c, pa)


class CSim(sim.Sim):
    def __init__(self, image=None):
        self._h = ffi.new_handle(self)
        self.c = ffi.gc(lib.simc_new(self._h), lib.simc_free)
        self._sync = ffi.new('SyncRegs *')
        self._cb_exc = None
        super().__init__(image)
        self.mem = CMemory(self.img, self.c); self.mem.sim = self
        self.c_insns = 0                     # instructions run in C / in Python (SLOW and hooked) -- for profiles
        self.py_insns = 0
        self._installed = set()
        self._brk = frozenset()

    # ---------------------------------------------------------------- register sync (Python <-> C)
    def _push(self, until=(), stop=()):
        o = self._sync
        for y, pe in enumerate((self.px, self.py)):
            R = o.R[y]
            for k in range(16): R[k] = pe.R[k] & M32
            o.ast[y] = pe.ast & M32; o.sticky[y] = pe.sticky & M32
            for w, v in enumerate((pe.MRF, pe.MRB)):
                o.mr_lo[y][w] = v & 0xFFFFFFFFFFFFFFFF; o.mr_hi[y][w] = v >> 64
            d = pe.__dict__
            o.has_bff[y] = 'bff' in d; o.bff[y] = d.get('bff', 0)
            o.has_bffwrp[y] = 'bffwrp' in d; o.bffwrp[y] = d.get('bffwrp', 0)
        for k in range(16):
            o.I[k] = self.I[k] & M32; o.M[k] = self.M[k] & M32; o.L[k] = self.L[k] & M32; o.Bx[k] = self.Bx[k] & M32
        sysr = self.sysr
        for k, nm in enumerate(SYSNAMES):
            if nm in sysr: o.sysp[k] = 1; o.sysv[k] = sysr[nm] & M32
            else: o.sysp[k] = 0; o.sysv[k] = 0
        o.PX1 = self.PX1 & M32; o.PX2 = self.PX2 & M32
        o.pc = self.pc; o.icount = self.icount
        o.has_lcntr = hasattr(self, 'lcntr_next'); o.lcntr = getattr(self, 'lcntr_next', 0)
        n = max(len(self.pcstk), len(self.loopstk), len(self.ststk))
        lib.simc_loops_alloc(self.c, n)
        o.npc, o.nl, o.ns = len(self.pcstk), len(self.loopstk), len(self.ststk)
        p = lib.simc_pcstk(self.c)
        for k, v in enumerate(self.pcstk): p[k] = v
        lp = lib.simc_loops(self.c)
        for k, (st, end, cnt, cnd) in enumerate(self.loopstk):
            e = lp[k]
            e.is_push = st == 'push'; e.start = 0 if st == 'push' else st; e.end = end
            e.has_count = cnt is not None; e.count = cnt or 0
            e.cond = -1 if cnd is None else cnd
        sp = lib.simc_ststk(self.c)
        for k, (ax, ay, m1) in enumerate(self.ststk):
            sp[k].ax = ax; sp[k].ay = ay; sp[k].m1 = m1
        if self.pending is None: o.pend = 0
        else: o.pend = 1; o.pend_t, o.pend_k = self.pending
        o.misaligned = self.mem.misaligned
        o.nuntil = len(until)
        for k, v in enumerate(until): o.until[k] = v
        o.nstop = len(stop)
        for k, v in enumerate(stop): o.stopat[k] = v
        lib.simc_put(self.c, o)

    def _pull(self):
        o = self._sync
        lib.simc_get(self.c, o)
        for y, pe in enumerate((self.px, self.py)):
            R = o.R[y]
            pe.R[:] = [R[k] for k in range(16)]
            pe.ast = o.ast[y]; pe.sticky = o.sticky[y]
            pe.MRF = (o.mr_hi[y][0] << 64) | o.mr_lo[y][0]
            pe.MRB = (o.mr_hi[y][1] << 64) | o.mr_lo[y][1]
            if o.has_bff[y]: pe.bff = o.bff[y]
            if o.has_bffwrp[y]: pe.bffwrp = o.bffwrp[y]
        self.I[:] = [o.I[k] for k in range(16)]; self.M[:] = [o.M[k] for k in range(16)]
        self.L[:] = [o.L[k] for k in range(16)]; self.Bx[:] = [o.Bx[k] for k in range(16)]
        sysr = self.sysr
        for k, nm in enumerate(SYSNAMES):
            if o.sysp[k]: sysr[nm] = o.sysv[k]
        self.PX1 = o.PX1; self.PX2 = o.PX2
        self.pc = o.pc; self.icount = o.icount
        if o.has_lcntr: self.lcntr_next = o.lcntr
        p = lib.simc_pcstk(self.c)
        self.pcstk = [p[k] for k in range(o.npc)]
        lp = lib.simc_loops(self.c)
        self.loopstk = [['push' if e.is_push else e.start, e.end, e.count if e.has_count else None,
                         None if e.cond < 0 else e.cond] for e in (lp[k] for k in range(o.nl))]
        sp = lib.simc_ststk(self.c)
        self.ststk = [(sp[k].ax, sp[k].ay, sp[k].m1) for k in range(o.ns)]
        self.pending = (o.pend_t, o.pend_k) if o.pend else None
        self.mem.misaligned = o.misaligned

    # ---------------------------------------------------------------- decode
    def _install(self, pc):
        ins = self.insn(pc)
        t = ins.type
        tid = TID.get(t, T_UNKNOWN)
        x = ins.x
        flags = (F_DB if '(DB)' in ins.text else 0) | (F_RTI if 'RTI' in ins.text else 0)
        asz, asx, alw = 4, 0, 0
        slow = tid == T_UNKNOWN
        if t in ('3a', '3b', '3d'):
            try:
                asz, asx, alw = self._size3(x, t)
            except NotImplementedError:
                slow = True
            ur, g, d, u = B(x, 29, 23), B(x, 32, 32), B(x, 31, 31), B(x, 44, 44)
            slow |= self._same_dag(ur, g, d or u)
        elif t in ('4b', '4d'):
            try:
                asz, asx, alw = self._sizes(visa.SIZE_4B.get((B(x, 18, 18), B(x, 16, 16), B(x, 17, 17))))
            except NotImplementedError:
                slow = True
        elif t == '15b':
            slow |= self._same_dag(B(x, 29, 23), B(x, 37, 37), B(x, 40, 40))
        elif t == '15a':
            slow |= self._same_dag(B(x, 38, 32), B(x, 44, 44), B(x, 40, 40))
        elif t in ('12a_imm', '12a_ureg', '13a'):
            slow |= pc not in self.loops_checked
        elif t in ('21a/22a/26a', '21c/22c'):
            slow |= 'IDLE' in ins.text
        if flags & F_DB and t in ('8a', '9a', '9b') and B(x, 39, 39):     # delayed call: its two slots' sizes
            nxt = pc + ins.size
            for a in (nxt, nxt + self.insn(nxt).size):
                if a not in self._installed: self._install(a)
        tgt = ins.target if isinstance(ins.target, int) else 0
        lib.simc_set_uop(self.c, pc, tid, x, tgt, ins.size, flags | (F_SLOW if slow else 0), asz, asx, alw)
        self._installed.add(pc)

    @staticmethod
    def _same_dag(ur, g, store_or_post):
        grp, rn = ur >> 4, ur & 0xF
        return bool(1 <= grp <= 4 and (rn >= 8) == bool(g) and store_or_post)

    # ---------------------------------------------------------------- run
    def _c_ok(self, until):
        """Can the next instructions run in C? Hooks need their PCs declared (on_insn_pcs), a watch must be write-only."""
        m = self.mem
        w = m.watch
        return not ((self.on_insn is not None and self.on_insn_pcs is None) or self.trace is not None
                    or (w is not None and not getattr(w, 'writes_only', False)) or m.on_uninit is not None
                    or 'r' in m.__dict__ or 'w' in m.__dict__ or len(until) > 8 or len(self.stop_at) > 16)

    def _set_brk(self):
        pcs = self.on_insn_pcs
        if self.on_insn is None or pcs is None:
            new = frozenset()
        else:
            new = frozenset(pcs() if callable(pcs) else pcs)
        if new != self._brk:
            for pc in self._brk - new: lib.simc_brk_unset(self.c, pc)
            for pc in new - self._brk: lib.simc_brk_set(self.c, pc)
            self._brk = new
        lib.simc_brk_on(self.c, bool(new))

    def _drain_wlog(self):
        n = ffi.new('int64_t *')
        buf = lib.simc_wlog_buf(self.c, n)
        w = self.mem.watch
        if n[0] and w is not None:
            for k in range(0, n[0], 3):
                w(buf[k], buf[k + 2], buf[k + 1], True)
        lib.simc_wlog_clear(self.c)

    def run(self, max_insns=10_000_000, until=None):
        until = tuple(until or ())
        untils = set(until)
        stop = tuple(self.stop_at)
        n = 0
        while True:
            if not self._c_ok(until):                       # (one instruction as sim.Sim.run does it)
                if self.pending is None and self.pc in untils: return 'until'
                if self.pc in self.stop_at and self.pending is None: return 'stop'
                sim.Sim.step(self)
                self.py_insns += 1; n += 1
                if n >= max_insns: return 'max'
                continue
            self._set_brk()
            lib.simc_wlog(self.c, self.mem.watch is not None)
            self._push(until, stop)
            while True:
                r = lib.simc_run(self.c, max_insns - n)
                done = lib.simc_n_done(self.c)
                n += done; self.c_insns += done
                if r != R_DECODE:
                    break
                self._install(lib.simc_need_pc(self.c))
            self._drain_wlog()
            lib.simc_wlog(self.c, 0)
            self._pull()
            if r == R_SLOW:                                 # (a SLOW instruction or a hook PC: Python runs it)
                pc = self.pc
                sim.Sim.step(self)
                self.py_insns += 1; n += 1
                if self.insn(pc).type in ('12a_imm', '12a_ureg', '13a') and pc in self._installed:
                    self._install(pc)
                if n >= max_insns: return 'max'
                continue
            if r == R_UNTIL: return 'until'
            if r == R_STOP: return 'stop'
            if r == R_MAX: return 'max'
            if r == R_ERR:
                code, msg = lib.simc_errcode(self.c), ffi.string(lib.simc_errmsg(self.c)).decode()
                if code == 6 and self._cb_exc is not None:
                    e, self._cb_exc = self._cb_exc, None
                    raise e
                raise {1: NotImplementedError, 2: OverflowError, 3: sim.Stop, 4: RuntimeError, 5: AttributeError}.get(
                    code, RuntimeError)(msg)
            raise RuntimeError(f'simc_run -> {r}')

    def invalidate_code(self):
        """After patching instruction memory: drop every decoded instruction (Python's cache and the core's)."""
        self.code.clear()
        lib.simc_clear_uops(self.c)
        self._installed = set()
