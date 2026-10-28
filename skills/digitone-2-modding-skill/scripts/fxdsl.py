"""fxdsl: write a DSP routine ONCE; get the SHARC code and a bit-exact float32 model of it from the same source.

WHY: an effect written twice -- hand-allocated SHARC and a hand-written float32 mirror to test it against -- spends most
of its debugging on the two disagreeing (a table index x3 vs x6, a clobbered register, a name collision). Here a routine is a Python function over a Kernel; the Kernel records a small IR and has two
backends that share it:
  * emit(a):  register allocation (R0-R15, I0-I4) + sisa packing -> sasm.Asm (48-bit loop bodies, hwrules-safe shapes);
  * run(mem, inputs): an interpreter with the SHARC simulator's own float32 semantics (sim.f_of / u_of: denormals
    flushed, TRUNC toward zero, flags as the sim sets them).
Check a build by running each routine in the SHARC emulator and, at each call, run() on the same inputs and memory:
all words written must match bit for bit (no per-effect mirror to write). run() is also a 48 kHz model of the effect
for listening / comparing offline.

Writing a routine (examples/track_trim/build.py is a minimal one):
    k = Kernel('ch_track', inputs={'par': 'I0', 'ts': 'I1'})
    par, ts = k['par'], k['ts']
    d = k.float(k.ld(par, -8, 'i')) * (7.75 * R / 256) + 16 * R      # Python numbers become constants
    k.st(ts, 16, d)
    with k.loop(32):  ...                    # hardware loop: straight-line body (no if, shift, abs load or pointer set)
    with k.if_(a < b) as br: ...             # comparisons of two floats or two ints
    with br.else_(): ...
    k.set(v, expr)                           # assign to an existing variable (loop-carried state)
Values are virtual registers ('f' float, 'i' int, 'p' pointer = byte address in an I register). Memory offsets and
indices are in words (DM(k, I), DM(M4, I), post-modify M6 = +1 word). Constants used inside a loop are loaded before it.
The routine may clobber R0-R15, I0-I4, M4, the flags; I5-I7 are read-only inputs at most.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import dn2paths as P; P.use_dnfw()
from contextlib import contextmanager
import sasm, sisa  # noqa: E402
from sasm import W, NOP48, jrel, ld15, st15, ld_abs, st_abs  # noqa: E402
from sisa import C, M3, MV  # noqa: E402
from sim import f_of, u_of, s32  # noqa: E402

M32 = 0xFFFFFFFF
sisa.ALU.setdefault('FABS', (0, 0xB0, 'F{n} = ABS F{x}', 'x'))
ALU = {'fadd': 'FADD', 'fsub': 'FSUB', 'fmul': 'FMUL', 'fabs': 'FABS', 'fmin': 'FMIN', 'fmax': 'FMAX',
       'float': 'FLOAT', 'trunc': 'TRUNC', 'add': 'ADD', 'sub': 'SUB', 'and': 'AND', 'or': 'OR', 'inc': 'INC',
       'pass': 'PASS', 'fclip': 'FCLIP'}
RESULT = {'fadd': 'f', 'fsub': 'f', 'fmul': 'f', 'fabs': 'f', 'fmin': 'f', 'fmax': 'f', 'float': 'f', 'trunc': 'i',
          'add': 'i', 'sub': 'i', 'and': 'i', 'or': 'i', 'inc': 'i', 'fclip': 'f'}
COND = {'eq': 0, 'lt': 1, 'le': 2, 'ne': 16, 'ge': 17, 'gt': 18}
NEG = {'eq': 'ne', 'ne': 'eq', 'lt': 'ge', 'ge': 'lt', 'le': 'gt', 'gt': 'le'}
R_REGS = tuple(range(16))
I_REGS = (0, 1, 2, 3, 4)                                   # allocatable pointers (I5 = the caller's buffer, read-only)
TRUE = 31


class DSLError(Exception):
    pass


class Var:
    __slots__ = ('k', 't', 'id', 'name', 'fixed', 'uses', 'temp')

    def __init__(self, k, t, name=None, fixed=None, temp=True):
        self.k, self.t, self.id, self.name, self.fixed, self.uses, self.temp = k, t, len(k.vars), name, fixed, 0, temp
        k.vars.append(self)

    def __repr__(self):
        return f'{self.name or "v"}{self.id}:{self.t}'

    # arithmetic
    def _b(self, op, o, rev=False):
        return self.k._arith(op, o, self, rev)

    def __add__(self, o): return self._b('+', o)
    def __radd__(self, o): return self._b('+', o, True)
    def __sub__(self, o): return self._b('-', o)
    def __rsub__(self, o): return self._b('-', o, True)
    def __mul__(self, o): return self._b('*', o)
    def __rmul__(self, o): return self._b('*', o, True)
    def __and__(self, o): return self._b('&', o)
    def __rand__(self, o): return self._b('&', o, True)
    def __or__(self, o): return self._b('|', o)
    def __ror__(self, o): return self._b('|', o, True)
    def __neg__(self): return self.k._arith('-', self, 0.0 if self.t == 'f' else 0, True)
    # comparisons -> Cond
    def __lt__(self, o): return Cond('lt', self, o)
    def __le__(self, o): return Cond('le', self, o)
    def __gt__(self, o): return Cond('gt', self, o)
    def __ge__(self, o): return Cond('ge', self, o)
    def __eq__(self, o): return Cond('eq', self, o)
    def __ne__(self, o): return Cond('ne', self, o)
    __hash__ = object.__hash__

    def __bool__(self):
        raise DSLError('a DSL value has no Python truth value (use k.if_)')


class Cond:
    def __init__(self, op, a, b):
        self.op, self.a, self.b = op, a, b

    def __bool__(self):
        raise DSLError('use k.if_(cond), not a Python if')


class Ins:
    __slots__ = ('op', 'dst', 'src', 'imm')

    def __init__(self, op, dst=None, src=(), imm=None):
        self.op, self.dst, self.src, self.imm = op, dst, list(src), imm

    def defs(self):
        d = [self.dst] if self.dst is not None else []
        if self.op in ('ldpost', 'stpost'):
            d.append(self.pointer())                       # the pointer advances
        return d

    def pointer(self):
        return self.src[0] if self.op == 'ldpost' else self.src[1]

    def __repr__(self):
        return f'{self.op} {self.dst} {self.src} {self.imm if self.imm is not None else ""}'


class Kernel:
    def __init__(self, name, inputs=None, readonly=('I5', 'I6', 'I7')):
        self.name, self.vars, self.code, self.inputs = name, [], [], {}
        self.readonly = set(readonly)
        self.loop_at = None                                # index of the open loop's 'loop' Ins
        self.loop_consts = {}
        self.nlabel = 0
        self.consts_ok = True
        for nm, reg in (inputs or {}).items():
            t = 'p' if reg[0] == 'I' else ('f' if reg[0] == 'F' else 'i')
            reg = 'R' + reg[1:] if reg[0] == 'F' else reg
            v = Var(self, t, nm, fixed=reg, temp=False)
            self.inputs[nm] = v

    def __getitem__(self, nm):
        return self.inputs[nm]

    # ---------------------------------------------------------------- building
    def _new(self, t, name=None):
        return Var(self, t, name)

    def _use(self, *vs):
        for v in vs:
            if not isinstance(v, Var) or v.k is not self:
                raise DSLError(f'not a value of this kernel: {v!r}')
            v.uses += 1

    def _add(self, ins):
        self._use(*ins.src)
        if self.loop_at is not None and ins.op in ('const', 'fext', 'lsh', 'ldabs', 'stabs', 'toptr', 'fromptr',
                                                    'cmp', 'br', 'jmp', 'label', 'loop'):
            raise DSLError(f'{ins.op} inside a hardware loop (hoist it before k.loop)')
        self.code.append(ins)
        return ins.dst

    def _const(self, value, t):
        if t == 'f':
            u = u_of(float(value))
        elif t == 'i':
            u = int(value) & M32
        else:
            raise DSLError(f'constant for a {t} value')
        if self.loop_at is not None:                       # hoisted before the loop, one register per value
            key = (t, u)
            if key not in self.loop_consts:
                v = self._new(t, 'c')
                self.code.insert(self.loop_at, Ins('const', v, (), u))
                self.loop_at += 1
                self.loop_consts[key] = v
            return self.loop_consts[key]
        v = self._new(t, 'c')
        self.code.append(Ins('const', v, (), u))
        return v

    def val(self, x, t):
        """A Var for x (a Var of type t, or a Python number)."""
        if isinstance(x, Var):
            if x.t != t:
                raise DSLError(f'{x!r} is not {t}')
            return x
        if isinstance(x, (int, float)):
            return self._const(x, t)
        raise DSLError(f'bad operand {x!r}')

    def f(self, x): return self._const(x, 'f')
    def i(self, x): return self._const(x, 'i')

    def op(self, op, *srcs, t=None):
        dst = self._new(t or RESULT[op])
        return self._add(Ins(op, dst, srcs))

    def _arith(self, sym, o, v, rev):
        t = v.t
        if t == 'p':
            raise DSLError('pointer arithmetic: use k.int_of / k.ptr')
        o = self.val(o, t)
        a, b = (o, v) if rev else (v, o)
        if t == 'f':
            op = {'+': 'fadd', '-': 'fsub', '*': 'fmul'}.get(sym)
        else:
            op = {'+': 'add', '-': 'sub', '&': 'and', '|': 'or'}.get(sym)
        if op is None:
            raise DSLError(f'{sym} on {t} values')
        return self.op(op, a, b)

    # conversions / unary / binary helpers
    def float(self, x): return self.op('float', self.val(x, 'i'))
    def trunc(self, x): return self.op('trunc', self.val(x, 'f'))
    def abs(self, x): return self.op('fabs', self.val(x, 'f'))
    def min(self, a, b): return self.op('fmin', self.val(a, 'f'), self.val(b, 'f'))
    def max(self, a, b): return self.op('fmax', self.val(a, 'f'), self.val(b, 'f'))
    def clip(self, a, lim): return self.op('fclip', self.val(a, 'f'), self.val(lim, 'f'))
    def inc(self, x): return self.op('inc', self.val(x, 'i'))

    def fext(self, x, pos, ln):                            # R = FEXT R BY pos:len (unsigned bit field)
        return self._add(Ins('fext', self._new('i'), (self.val(x, 'i'),), (pos, ln)))

    def lsh(self, x, sh):                                  # R = LSHIFT R BY sh (left, 0..31)
        assert 0 <= sh < 32
        return self._add(Ins('lsh', self._new('i'), (self.val(x, 'i'),), sh))

    def var(self, t, init=None, name=None):
        """A named variable (assign with k.set); init: a Var or a number."""
        v = self._new(t, name)
        v.temp = False
        if init is not None:
            self.set(v, init)
        return v

    def set(self, dst, src):
        src = self.val(src, dst.t) if dst.t != 'p' else src
        if dst is src:
            return
        last = self.code[-1] if self.code else None
        if src.temp and last is not None and last.dst is src and src.uses == 0 and last.op != 'const':
            last.dst = dst                                 # retarget the op that made src
            src.temp = False
            return
        if dst.t == 'p':
            self._add(Ins('toptr', dst, (self.fromptr(src) if src.t == 'p' else src,)))
            return
        self._add(Ins('pass', dst, (src,)))

    # memory
    def ld(self, p, k=0, t='f'):
        return self._add(Ins('ld', self._new(t), (self._ptr(p),), k))

    def st(self, p, k, v, t=None):
        v = self.val(v, t or (v.t if isinstance(v, Var) else 'f'))
        self._add(Ins('st', None, (v, self._ptr(p)), k))

    def ld_post(self, p, t='f', step=1):                   # DM(I, M6): read, then the pointer += step words
        self._owned(p)
        return self._add(Ins('ldpost', self._new(t), self._step((p,), step), step))

    def st_post(self, p, v, t=None, step=1):
        self._owned(p)
        v = self.val(v, t or (v.t if isinstance(v, Var) else 'f'))
        self._add(Ins('stpost', None, self._step((v, p), step), step))

    def _step(self, srcs, step):
        """step 1: M6. Any other step goes through M4 (a register holding it is the access's last source)."""
        return srcs if step == 1 else srcs + (self.i(step),)

    def ldx(self, p, idx, t='f'):                          # DM(M4 = idx, I): idx in words, the pointer unchanged
        return self._add(Ins('ldx', self._new(t), (self._ptr(p), self.val(idx, 'i'))))

    def stx(self, p, idx, v, t=None):
        v = self.val(v, t or (v.t if isinstance(v, Var) else 'f'))
        self._add(Ins('stx', None, (v, self._ptr(p), self.val(idx, 'i'))))

    def ld_abs(self, addr, t='f'):
        return self._add(Ins('ldabs', self._new(t), (), addr))

    def st_abs(self, addr, v, t=None):
        v = self.val(v, t or (v.t if isinstance(v, Var) else 'f'))
        self._add(Ins('stabs', None, (v,), addr))

    def ptr(self, x):
        """A new pointer from an int value or a constant byte address."""
        if isinstance(x, Var) and x.t == 'p':
            x = self.fromptr(x)
        return self._add(Ins('toptr', self._new('p'), (self.val(x, 'i'),)))

    def fromptr(self, p):
        return self._add(Ins('fromptr', self._new('i'), (self._ptr(p),)))

    int_of = fromptr

    def _ptr(self, p):
        if not isinstance(p, Var) or p.t != 'p':
            raise DSLError(f'{p!r} is not a pointer')
        return p

    def _owned(self, p):
        self._ptr(p)
        if p.fixed in self.readonly:
            raise DSLError(f'{p!r} is a read-only input (copy it: k.ptr(k.int_of(p)))')

    # control flow
    def label(self):
        self.nlabel += 1
        return f'{self.name}_L{self.nlabel}'

    @contextmanager
    def loop(self, count=32):
        if self.loop_at is not None:
            raise DSLError('nested hardware loops')
        end = self.label()
        self._add(Ins('loop', None, (), (count, end)))
        self.loop_at, self.loop_consts = len(self.code) - 1, {}
        body0 = len(self.code)
        yield
        if len(self.code) == body0:
            raise DSLError('empty loop')
        self.loop_at = None
        self.code.append(Ins('endloop', None, (), end))

    def _cmp(self, cond):
        if not isinstance(cond, Cond):
            raise DSLError('k.if_ takes a comparison of two values')
        t = cond.a.t if isinstance(cond.a, Var) else cond.b.t
        a, b = self.val(cond.a, t), self.val(cond.b, t)
        tmp = self._new('f') if t == 'f' else None
        self._add(Ins('cmp', tmp, (a, b), t))
        return cond.op

    @contextmanager
    def if_(self, cond):
        op = self._cmp(cond)
        skip, end = self.label(), self.label()
        self._add(Ins('br', None, (), (NEG[op], skip)))
        br = _Branch(self, skip, end)
        yield br
        if not br.has_else:
            self._add(Ins('label', None, (), skip))

    def ret(self):
        self._add(Ins('ret'))

    # ---------------------------------------------------------------- analysis
    def _succ(self):
        lab = {ins.imm: n for n, ins in enumerate(self.code) if ins.op == 'label'}
        starts = {}
        for n, ins in enumerate(self.code):
            if ins.op == 'loop':
                starts[ins.imm[1]] = n + 1
        out = []
        for n, ins in enumerate(self.code):
            nxt = [n + 1] if n + 1 < len(self.code) else []
            if ins.op == 'jmp':
                nxt = [lab[ins.imm]]
            elif ins.op == 'br':
                nxt = nxt + [lab[ins.imm[1]]]
            elif ins.op == 'endloop':
                nxt = nxt + [starts[ins.imm]]
            elif ins.op == 'ret':
                nxt = []
            out.append(nxt)
        return out

    def liveness(self):
        succ = self._succ()
        n = len(self.code)
        live_in = [set() for _ in range(n)]
        live_out = [set() for _ in range(n)]
        changed = True
        while changed:
            changed = False
            for i in range(n - 1, -1, -1):
                ins = self.code[i]
                lo = set()
                for s in succ[i]:
                    lo |= live_in[s]
                li = (lo - set(ins.defs())) | set(ins.src)
                if ins.op in ('ldpost', 'stpost'):
                    li.add(ins.pointer())
                if lo != live_out[i] or li != live_in[i]:
                    live_out[i], live_in[i], changed = lo, li, True
        bad = [v for v in live_in[0] if v.fixed is None]
        if bad:
            raise DSLError(f'{self.name}: used before set: {bad}')
        return live_in, live_out

    def allocate(self):
        """Graph colouring: R0-R15 for f / i values, I0-I4 for pointers; inputs keep their registers."""
        live_in, live_out = self.liveness()
        adj = {v: set() for v in self.vars}
        entry = set(live_in[0])
        for a in entry:                                    # the inputs are all live at once at the entry
            adj[a] |= entry - {a}
        for i, ins in enumerate(self.code):
            ds = ins.defs()
            for d in ds:
                others = live_out[i] - {d}
                if ins.op == 'pass' and ins.src[0] in others:
                    others = others - {ins.src[0]}          # a move: source and destination may share a register
                for o in others | set(ds) - {d}:
                    adj[d].add(o); adj[o].add(d)
        color = {}
        for v in self.vars:
            if v.fixed:
                color[v] = v.fixed
        # a written read-only input would clobber the caller's register
        for ins in self.code:
            for d in ins.defs():
                if d.fixed in self.readonly:
                    raise DSLError(f'{self.name}: writes the read-only input {d!r}')
        # live range ends (for the choice among free registers: the one idle longest, so the packer meets fewer false
        # dependencies)
        last = {}
        for i, ins in enumerate(self.code):
            for v in set(ins.src) | set(ins.defs()) | live_out[i]:
                last[v] = i
        busy = {}                                          # register -> the last index any of its values is live
        for v, r in color.items():
            busy[r] = max(busy.get(r, -1), last.get(v, -1))
        defined = set()
        order = []
        for ins in self.code:
            for d in ins.defs():
                if d not in defined:
                    defined.add(d); order.append(d)
        for v in order:
            if v in color:
                continue
            taken = {color[o] for o in adj[v] if o in color}
            regs = [f'I{r}' for r in I_REGS] if v.t == 'p' else [f'R{r}' for r in R_REGS]
            free = sorted((r for r in regs if r not in taken and r not in self.readonly), key=lambda r: busy.get(r, -1))
            if not free:
                worst = max(range(len(self.code)), key=lambda i: len(live_out[i]))
                raise DSLError(f'{self.name}: out of {v.t} registers at {self.code[worst]!r}; live there: '
                               f'{sorted(live_out[worst], key=lambda x: x.id)}')
            color[v] = free[0]
            busy[free[0]] = max(busy.get(free[0], -1), last.get(v, -1))
        self.color = color
        return color

    # ---------------------------------------------------------------- SHARC backend
    def emit(self, a):
        """Append the routine to sasm.Asm a (label = the kernel's name). Returns the register map."""
        if not self.code or self.code[-1].op != 'ret':
            self.ret()
        col = self.allocate()
        rn = lambda v: int(col[v][1:])
        a.label(self.name)
        group = []
        state = {'flow': False}

        def raw(item, flow=False):
            flush()
            if flow and state['flow']:
                a((NOP48, 'NOP'))                          # branches >= 2 sw apart
            if callable(item):
                a(fix=item)
            else:
                a(item)
            state['flow'] = flow

        def flush():
            if group:
                for cop, mop in sisa.schedule(group):
                    if cop is None and _alone_4a(mop):         # stock never has one: its VISA 15b form (stock's)
                        f = mop.f
                        a((st15 if f['d'] else ld15)(f'R{f["r"]}', f['k'], f['i']))
                    else:
                        a(sisa.encode(cop, mop))
                group.clear()
                state['flow'] = False

        def access(ins):
            """sisa ops for a memory instruction (None: needs a raw VISA form)."""
            if ins.op == 'ld':
                if -32 <= ins.imm <= 31:
                    return [sisa.M4(rn(ins.dst), ins.imm, rn(ins.src[0]), 0)]
            elif ins.op == 'st':
                if -32 <= ins.imm <= 31:
                    return [sisa.M4(rn(ins.src[0]), ins.imm, rn(ins.src[1]), 1)]
            elif ins.op == 'ldpost':
                if ins.imm == 1:
                    return [M3(f'R{rn(ins.dst)}', rn(ins.src[0]), 6, 0, 1)]
                return ([] if hoisted_m4 else [MV('M4', f'R{rn(ins.src[1])}')]) + \
                    [M3(f'R{rn(ins.dst)}', rn(ins.src[0]), 4, 0, 1)]
            elif ins.op == 'stpost':
                if ins.imm == 1:
                    return [M3(f'R{rn(ins.src[0])}', rn(ins.src[1]), 6, 1, 1)]
                return ([] if hoisted_m4 else [MV('M4', f'R{rn(ins.src[2])}')]) + \
                    [M3(f'R{rn(ins.src[0])}', rn(ins.src[1]), 4, 1, 1)]
            elif ins.op == 'ldx':
                return [MV('M4', f'R{rn(ins.src[1])}'), M3(f'R{rn(ins.dst)}', rn(ins.src[0]), 4, 0, 0)]
            elif ins.op == 'stx':
                return [MV('M4', f'R{rn(ins.src[2])}'), M3(f'R{rn(ins.src[0])}', rn(ins.src[1]), 4, 1, 0)]
            return None

        def compute(ins):
            n = rn(ins.dst)
            x = rn(ins.src[0])
            y = rn(ins.src[1]) if len(ins.src) > 1 else 0
            return C(ALU[ins.op], n, x, y)

        hoisted_m4 = False
        i = 0
        code = self.code
        while i < len(code):
            ins = code[i]
            op = ins.op
            if op == 'loop':
                flush()
                count, end = ins.imm
                j = i + 1
                while code[j].op != 'endloop':
                    j += 1
                seg = code[i + 1:j]
                steps = {b.src[-1] for b in seg if b.op in ('ldpost', 'stpost') and b.imm != 1}
                if steps:                                  # one strided register for the loop: M4 set before it
                    if len(steps) > 1 or any(b.op in ('ldx', 'stx') for b in seg):
                        raise DSLError(f'{self.name}: a loop mixes M4 uses (several steps, or steps and ldx/stx)')
                    raw(sisa.encode(None, MV('M4', f'R{rn(steps.pop())}')))
                hoisted_m4 = bool(steps) or any(b.op in ('ldpost', 'stpost') and b.imm != 1 for b in seg)
                j = i + 1
                body = []
                while code[j].op != 'endloop':
                    b = code[j]
                    if b.op in ALU:
                        body.append(compute(b))
                    else:
                        acc = access(b)
                        if acc is None:
                            raise DSLError(f'{self.name}: {b!r} in a loop (offset beyond -32..31?)')
                        body += acc
                    j += 1
                packed = sisa.schedule(body)
                while len(packed) < 3:
                    body.append(C('PASS', 0, 0))
                    packed = sisa.schedule(body)
                packed = [(cop, _as_3a(mop) if cop is None and _alone_4a(mop) else mop) for cop, mop in packed]
                raw(sasm.loop(count, end))
                for k_, (cop, mop) in enumerate(packed):
                    if k_ == len(packed) - 1:
                        a.label(end)
                    a(sisa.encode(cop, mop))
                state['flow'] = False
                hoisted_m4 = False
                i = j + 1
                continue
            if op in ALU:
                group.append(compute(ins))
            elif op == 'const':
                raw(sasm.const(f'R{rn(ins.dst)}', ins.imm))
            elif op in ('ld', 'st', 'ldpost', 'stpost', 'ldx', 'stx'):
                acc = access(ins)
                if acc is not None:
                    group.extend(acc)
                elif op == 'ld':
                    raw(ld15(f'R{rn(ins.dst)}', ins.imm, rn(ins.src[0])))
                else:
                    raw(st15(f'R{rn(ins.src[0])}', ins.imm, rn(ins.src[1])))
            elif op == 'ldabs':
                raw(ld_abs(rn(ins.dst), ins.imm))
            elif op == 'stabs':
                raw(st_abs(rn(ins.src[0]), ins.imm))
            elif op == 'fext':
                raw(_fext(rn(ins.dst), rn(ins.src[0]), *ins.imm))
            elif op == 'lsh':
                raw(_lsh(rn(ins.dst), rn(ins.src[0]), ins.imm))
            elif op == 'toptr':
                raw(mv5b(0x10 | rn(ins.dst), rn(ins.src[0])))
            elif op == 'fromptr':
                raw(mv5b(rn(ins.dst), 0x10 | rn(ins.src[0])))
            elif op == 'cmp':
                if ins.imm == 'f':
                    c = C('FSUB', rn(ins.dst), rn(ins.src[0]), rn(ins.src[1]))
                else:
                    c = C('COMP', 0, rn(ins.src[0]), rn(ins.src[1]))
                raw(sisa.encode(c, None))
            elif op == 'br':
                raw(jrel(COND[ins.imm[0]], ins.imm[1]), flow=True)
            elif op == 'jmp':
                raw(jrel(TRUE, ins.imm), flow=True)
            elif op == 'label':
                flush()
                a.label(ins.imm)
                # a label is a branch target: the next branch may sit right after the previous one's fall-through
                state['flow'] = state['flow']
            elif op == 'ret':
                raw((W(0x0ABE), 'RTS'), flow=True)
            else:
                raise DSLError(f'unknown op {op}')
            i += 1
        flush()
        return col

    # ---------------------------------------------------------------- the interpreter
    def run(self, mem, inputs, count=None):
        """Execute with the simulator's semantics. mem: .r(addr) -> u32, .w(addr, u32) (byte addresses, words at
        p + 4 k). inputs: {name: u32} (floats as their bits). Returns the values of every variable."""
        val = {}
        for nm, v in self.inputs.items():
            if nm in inputs:
                val[v] = inputs[nm] & M32
        lab = {ins.imm: n for n, ins in enumerate(self.code) if ins.op == 'label'}
        start = {}
        for n, ins in enumerate(self.code):
            if ins.op == 'loop':
                start[ins.imm[1]] = n
        flags = {'az': 0, 'an': 0}
        lcnt = []
        pc = 0
        code = self.code
        steps = 0
        while pc < len(code):
            ins = code[pc]
            op = ins.op
            steps += 1
            s = [val[x] for x in ins.src] if op not in ('ldpost', 'stpost') else None
            if op in RESULT or op == 'pass':
                val[ins.dst] = _alu(op, s)
            elif op == 'const':
                val[ins.dst] = ins.imm
            elif op == 'ld':
                val[ins.dst] = mem.r((s[0] + 4 * ins.imm) & M32)
            elif op == 'st':
                mem.w((s[1] + 4 * ins.imm) & M32, s[0])
            elif op == 'ldpost':
                p = ins.src[0]
                val[ins.dst] = mem.r(val[p]); val[p] = (val[p] + 4 * ins.imm) & M32
            elif op == 'stpost':
                p = ins.src[1]
                mem.w(val[p], val[ins.src[0]]); val[p] = (val[p] + 4 * ins.imm) & M32
            elif op == 'ldx':
                val[ins.dst] = mem.r((s[0] + 4 * s32(s[1])) & M32)
            elif op == 'stx':
                mem.w((s[1] + 4 * s32(s[2])) & M32, s[0])
            elif op == 'ldabs':
                val[ins.dst] = mem.r(ins.imm)
            elif op == 'stabs':
                mem.w(ins.imm, s[0])
            elif op == 'fext':
                pos, ln = ins.imm
                val[ins.dst] = (s[0] >> pos) & ((1 << ln) - 1)
            elif op == 'lsh':
                val[ins.dst] = (s[0] << ins.imm) & M32
            elif op in ('toptr', 'fromptr'):
                val[ins.dst] = s[0]
            elif op == 'cmp':
                if ins.imm == 'f':
                    r = u_of(f_of(s[0]) - f_of(s[1]))
                    val[ins.dst] = r
                    ai = r == M32
                    flags = {'az': (r & 0x7FFFFFFF) == 0, 'an': bool(r & 0x80000000) and not ai}
                else:
                    a_, b_ = s32(s[0]), s32(s[1])
                    flags = {'az': a_ == b_, 'an': a_ < b_}
            elif op == 'br':
                c = ins.imm[0]
                v = {'eq': flags['az'], 'lt': flags['an'] and not flags['az'], 'le': flags['an'] or flags['az']}
                taken = v[c] if c in v else not v[NEG[c]]
                if taken:
                    pc = lab[ins.imm[1]]
                    continue
            elif op == 'jmp':
                pc = lab[ins.imm]
                continue
            elif op == 'loop':
                lcnt.append(ins.imm[0])
            elif op == 'endloop':
                lcnt[-1] -= 1
                if lcnt[-1] > 0:
                    pc = start[ins.imm] + 1
                    continue
                lcnt.pop()
            elif op == 'ret':
                break
            elif op == 'label':
                pass
            else:
                raise DSLError(f'unknown op {op}')
            pc += 1
        if count is not None:
            count[0] += steps
        return val


class _Branch:
    def __init__(self, k, skip, end):
        self.k, self.skip, self.end, self.has_else = k, skip, end, False

    @contextmanager
    def else_(self):
        k = self.k
        self.has_else = True
        k._add(Ins('jmp', None, (), self.end))
        k._add(Ins('label', None, (), self.skip))
        yield
        k._add(Ins('label', None, (), self.end))


def _alone_4a(mop):
    """A type-4a immediate-offset access (DM(k, I), no update): stock uses ~1700, never without a compute."""
    return mop is not None and mop.f.get('t3') == '4a' and not mop.f['u']


def _as_3a(mop):
    """In a loop (48-bit only): DM(0, I) alone -> DM(M5, I), M5 = 0 (C runtime), the stock 3a pre-modify form."""
    f = mop.f
    if f['k'] != 0:
        raise DSLError(f'a lone DM({f["k"]}, I{f["i"]}) in a loop: stock has no such shape (use offset 0 or ld_post)')
    return M3(f'R{f["r"]}', f['i'], 5, f['d'], 0)


def _alu(op, s):
    if op == 'fadd': return u_of(f_of(s[0]) + f_of(s[1]))
    if op == 'fsub': return u_of(f_of(s[0]) - f_of(s[1]))
    if op == 'fmul': return u_of(f_of(s[0]) * f_of(s[1]))
    if op == 'fabs': return u_of(abs(f_of(s[0])))
    if op in ('fmin', 'fmax', 'fclip') and any(f_of(x) != f_of(x) for x in s[:2]):
        return 0xFFFFFFFF                                # (SHARC: a NaN passes MIN / MAX / CLIP -- seen on HW)
    if op == 'fmin': return u_of(min(f_of(s[0]), f_of(s[1])))
    if op == 'fmax': return u_of(max(f_of(s[0]), f_of(s[1])))
    if op == 'fclip':
        y = abs(f_of(s[1])); return u_of(max(-y, min(y, f_of(s[0]))))
    if op == 'float': return u_of(float(s32(s[0])))
    if op == 'trunc':
        x = f_of(s[0])
        if x != x: return M32
        if abs(x) == float('inf'): return 0x7FFFFFFF if x > 0 else 0x80000000
        return max(-0x80000000, min(0x7FFFFFFF, int(x))) & M32
    if op == 'add': return (s[0] + s[1]) & M32
    if op == 'sub': return (s[0] - s[1]) & M32
    if op == 'and': return s[0] & s[1]
    if op == 'or': return s[0] | s[1]
    if op == 'inc': return (s[0] + 1) & M32
    if op == 'pass': return s[0]
    raise DSLError(op)


# ---------------------------------------------------------------- encodings (verified against visa when assembled)
def _fext(n, x, pos, ln):                                  # 6a shift-imm: Rn = FEXT Rx BY pos:len
    data = ln << 6 | pos
    return W(0x023E, 0x0010 | (data >> 8) << 11, (data & 0xFF) << 8 | n << 4 | x), f'R{n} = FEXT R{x} BY {pos}:{ln}'


def _lsh(n, x, sh):                                        # 6a shift-imm: Rn = LSHIFT Rx BY sh
    return W(0x023E, 0x0000, sh << 8 | n << 4 | x), f'R{n} = LSHIFT R{x} BY {sh}'


def mv5b(d, s):
    """5b `ureg = ureg` (stock '703f 0a3f' I4 = R2, '713e 013f' R2 = I0): d, s = ureg numbers (R 0x0n, I 0x1n)."""
    name = lambda u: ('R' if u < 0x10 else 'I') + str(u & 0xF)
    return (W(0x7000 | (s >> 2) << 6 | 0x3E | (s >> 1 & 1), (s & 1) << 15 | d << 7 | 0x3F),
            f'{name(d)} = {name(s)}')


class DictMem:
    """Flat memory for running kernels outside the simulator (floats stored as bits)."""
    def __init__(self):
        self.words = {}

    def r(self, a): return self.words.get(a, 0)
    def w(self, a, v): self.words[a] = v & M32
    def rf(self, a): return f_of(self.r(a))
    def wf(self, a, x): self.w(a, u_of(x))


class Overlay:
    """Copy-on-write view of another memory (the simulator's at a call): reads fall through, writes stay here."""
    def __init__(self, base):
        self.base, self.words = base, {}

    def r(self, a):
        return self.words[a] if a in self.words else self.base.r(a)

    def w(self, a, v):
        self.words[a] = v & M32
