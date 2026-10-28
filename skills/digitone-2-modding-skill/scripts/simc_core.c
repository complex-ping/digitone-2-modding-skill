/* simc_core.c -- C execution core for sim.py (SHARC+ interpreter), driven by simc.py through cffi.

A line-by-line port of sim.Sim.step / exec and sim.Memory: the same arithmetic (floats in double, rounded to float32 once,
as Python's struct.pack does), the same flags, DAG, SIMD, loop / PC / status stacks and delayed branches. Instructions
arrive pre-decoded from Python (type id + raw 48-bit word + target + static flags). Anything Python must see (the
hardware-rule checks on first execution, IDLE, unknown types) is flagged SLOW: the core stops before it and Python runs that
one instruction with sim.Sim.step. Memory pages live here; MMR accesses and image page fills call back into Python.
simjit_check.py compares the whole machine state with sim.Sim after every audio block.
*/
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <stdarg.h>
#include <math.h>

typedef uint32_t u32; typedef int64_t i64; typedef uint64_t u64; typedef __int128 i128;
#define M32 0xFFFFFFFFu

#define AZ 0
#define AV 1
#define AN 2
#define AC 3
#define AS 4
#define AI 5
#define MN 6
#define MV 7
#define MU 8
#define MI 9
#define AF 10
#define SV 11
#define SZ 12
#define SS 13
#define BTF 18
#define PCFL 21
#define PCEM 22
#define SSOV 23
#define SSEM 24
#define LSOV 25
#define LSEM 26
#define PEYEN (1u << 21)
#define BDCST1 (1u << 23)
#define BDCST9 (1u << 22)
#define CBUFEN (1u << 24)
#define ALUSAT (1u << 13)
#define TRUNCB (1u << 15)

enum { T_1a, T_1b, T_2a, T_2b, T_2c, T_3a, T_3b, T_3c, T_3d, T_4a, T_4b, T_4d, T_5a_move, T_5b_move, T_5a_swap, T_5b_swap,
       T_6a_nomem, T_6a_mem, T_7a, T_7a_nw, T_7b, T_7d, T_8a, T_9a, T_9b, T_11a, T_11c, T_12a_imm, T_12a_ureg, T_13a,
       T_14a, T_14d, T_15a, T_15b, T_16a, T_16b, T_17a, T_17b, T_18a, T_19a, T_20a, T_21a, T_21c, T_25a, T_25c,
       T_25a_rframe, T_UNKNOWN };

enum { RC_OK = 0, RC_UNTIL = 1, RC_STOP = 2, RC_MAX = 3, RC_DECODE = 4, RC_SLOW = 5, RC_ERR = 6 };
enum { E_NOTIMPL = 1, E_OVERFLOW = 2, E_STOP = 3, E_RUNTIME = 4, E_ATTR = 5, E_CB = 6 };
enum { F_DB = 1, F_RTI = 2, F_SLOW = 8 };

typedef struct { u32 R[16]; u32 ast, sticky; i128 MRF, MRB; u64 bff; i64 bffwrp; int has_bff, has_bffwrp; } PE;
typedef struct { i64 start, end, count; int has_count, cond, is_push; } LoopE;   /* cond -1 = None (counter loop) */
typedef struct { u32 ax, ay, m1; } StE;
typedef struct { u64 x; i64 target; unsigned short type; unsigned char size, flags, asz, asx, alw, pad; } Uop;
typedef struct { u32 d[1024]; u32 wr[32]; } Page;

typedef struct Sim {
    PE px, py;
    u32 I[16], M[16], L[16], Bx[16];
    u32 sysv[32]; unsigned char sysp[32];          /* ureg group 6 -> 0..15, group 7 -> 16..31; sysp = key in sim.sysr */
    u32 PX1, PX2;
    i64 pc, icount, lcntr; int has_lcntr;
    i64 *pcstk; int npc, cpc;
    LoopE *lst; int nl, cl;
    StE *sst; int ns, cs;
    int pend, pend_k; i64 pend_t;
    int ea_i;
    u64 misaligned;
    Page **pt[1024];
    u32 **it[1024];
    Uop *code[4096];
    i64 until[8]; int nuntil; i64 stopat[16]; int nstop;
    void *handle;
    int err; char errmsg[200]; i64 need_pc;
    i64 n_done;
    unsigned char *brk; int has_brk;                /* hook PCs (2^24-bit map): stop before them (RC_SLOW) */
    u32 *wlog; i64 nw, cw; int wlog_on;             /* write log for a write-only Python watch: (a, v, size) */
} Sim;

/* Python callbacks (cffi extern "Python") */
extern int simc_fill(void *h, u32 pagebase, u32 *out);
extern u32 simc_mmr_r(void *h, u32 a);
extern void simc_mmr_w(void *h, u32 a, u32 v);

static void fail(Sim *s, int code, const char *fmt, ...) {
    if (s->err) return;
    s->err = code;
    va_list ap; va_start(ap, fmt); vsnprintf(s->errmsg, sizeof s->errmsg, fmt, ap); va_end(ap);
}

static inline i64 S32(u32 v) { return (i64)(int32_t)v; }
static inline u64 B(u64 x, int hi, int lo) { return (x >> lo) & ((1ULL << (hi - lo + 1)) - 1); }
static inline i64 sext(u64 v, int bits) { return (v & (1ULL << (bits - 1))) ? (i64)v - (i64)(1ULL << bits) : (i64)v; }
static inline int simd(Sim *s) { return (s->sysv[18] & PEYEN) != 0; }
#define MODE1 (s->sysv[18])

/* ------------------------------------------------------------------ memory */
static inline int is_mmr(u32 a) { return a < 0x80000 || (a >= 0x30000000u && a < 0x40000000u); }
static inline u32 phys(u32 a) {
    if (a >= 0x80000 && a < 0xF0000) return a * 4;
    if (a >= 0x120000 && a < 0x1E0000) return a * 2;
    return a;
}

static u32 *img_page(Sim *s, u32 pa) {
    u32 hi = pa >> 22, mid = (pa >> 12) & 1023;
    if (!s->it[hi]) s->it[hi] = calloc(1024, sizeof(u32 *));
    u32 *p = s->it[hi][mid];
    if (!p) {
        p = calloc(1024, 4);
        if (simc_fill(s->handle, pa & ~0xFFFu, p)) fail(s, E_CB, "page fill %#x", pa);
        s->it[hi][mid] = p;
    }
    return p;
}

static Page *pg_slow(Sim *s, u32 pa) {
    u32 hi = pa >> 22, mid = (pa >> 12) & 1023;
    if (!s->pt[hi]) s->pt[hi] = calloc(1024, sizeof(Page *));
    Page *p = s->pt[hi][mid];
    if (!p) {
        p = malloc(sizeof(Page));
        memcpy(p->d, img_page(s, pa), 4096);
        memset(p->wr, 0, sizeof p->wr);
        s->pt[hi][mid] = p;
    }
    return p;
}

static inline Page *pg(Sim *s, u32 pa) {
    Page **l2 = s->pt[pa >> 22];
    if (l2) { Page *p = l2[(pa >> 12) & 1023]; if (p) return p; }
    return pg_slow(s, pa);
}
static inline u32 rw(Sim *s, u32 pa) { return pg(s, pa)->d[(pa >> 2) & 1023]; }
static inline void ww(Sim *s, u32 pa, u32 v) {
    Page *p = pg(s, pa); u32 k = (pa >> 2) & 1023;
    p->d[k] = v; p->wr[k >> 5] |= 1u << (k & 31);
}

static u32 mem_r(Sim *s, u32 a, int size, int sx) {
    if (is_mmr(a)) return simc_mmr_r(s->handle, a);
    u32 pa = phys(a), v;
    if (size == 4 && (pa & 3) && simd(s)) pa &= ~3u;
    if (size == 4) {
        if (pa & 3) {
            v = 0;
            for (int k = 0; k < 4; k++) { u32 q = pa + k; v |= ((rw(s, q & ~3u) >> (8 * (q & 3))) & 0xFF) << (8 * k); }
            s->misaligned++;
        } else v = rw(s, pa);
    } else if (size == 2) {
        if ((pa & 3) == 3) v = ((rw(s, pa & ~3u) >> 24) & 0xFF) | ((rw(s, pa + 1) & 0xFF) << 8);
        else v = (rw(s, pa & ~3u) >> (8 * (pa & 3))) & 0xFFFF;
        if (sx && (v & 0x8000)) v |= 0xFFFF0000u;
    } else {
        v = (rw(s, pa & ~3u) >> (8 * (pa & 3))) & 0xFF;
        if (sx && (v & 0x80)) v |= 0xFFFFFF00u;
    }
    return v;
}

static void mem_w(Sim *s, u32 a, u32 v, int size) {
    if (is_mmr(a)) { simc_mmr_w(s->handle, a, v); return; }
    if (s->wlog_on) {
        if (s->nw + 3 > s->cw) { s->cw = s->cw * 2 + 3072; s->wlog = realloc(s->wlog, s->cw * sizeof(u32)); }
        s->wlog[s->nw++] = a; s->wlog[s->nw++] = v; s->wlog[s->nw++] = size;
    }
    u32 pa = phys(a);
    if (size == 4 && (pa & 3) && simd(s)) pa &= ~3u;
    if (size == 4) {
        if (pa & 3) {
            for (int k = 0; k < 4; k++) {
                u32 q = pa + k, base = q & ~3u, sh = 8 * (q & 3);
                ww(s, base, (rw(s, base) & ~(0xFFu << sh)) | (((v >> (8 * k)) & 0xFF) << sh));
            }
            s->misaligned++;
        } else ww(s, pa, v);
    } else {
        u32 base = pa & ~3u, sh = 8 * (pa & 3);
        u64 mask = (u64)(size == 2 ? 0xFFFF : 0xFF) << sh;
        u64 old = rw(s, base);
        ww(s, base, (u32)((old & ~mask) | (((u64)v << sh) & mask)));
    }
}

/* ------------------------------------------------------------------ floats (Python float = double) */
static inline double f_of(u32 u) {
    if ((u & 0x7F800000u) == 0 && (u & 0x7FFFFF)) u &= 0x80000000u;
    float f; memcpy(&f, &u, 4); return (double)f;
}
static inline u32 u_of(double d) {
    if (d != d) return 0xFFFFFFFFu;
    float f = (float)d; u32 u; memcpy(&u, &f, 4);
    if ((u & 0x7F800000u) == 0) u &= 0x80000000u;
    return u;
}
static double pow2(Sim *s, i64 n) {                 /* Python 2.0 ** n */
    if (n >= 1024) { fail(s, E_OVERFLOW, "2.0 ** %lld", (long long)n); return 0; }
    if (n < -1100) return 0.0;
    return ldexp(1.0, (int)n);
}
static inline double pymin(double a, double b) { return b < a ? b : a; }
static inline double pymax(double a, double b) { return b > a ? b : a; }

/* ------------------------------------------------------------------ registers */
static inline u32 stky(Sim *s, PE *pe) {
    u32 v = pe->sticky;
    if (!s->npc) v |= 1u << PCEM;
    if (!s->ns) v |= 1u << SSEM;
    if (!s->nl) v |= 1u << LSEM;
    return v;
}

static u32 get_ureg(Sim *s, int u, PE *pe) {
    int g = (u >> 4) & 7, r = u & 15;
    if (!pe) pe = &s->px;
    switch (g) {
    case 0: return pe->R[r];
    case 1: return s->I[r];
    case 2: return s->M[r];
    case 3: return s->L[r];
    case 4: return s->Bx[r];
    case 5: return s->py.R[r];
    }
    if (g == 7) {
        if (r == 6) return s->px.ast;
        if (r == 7) return s->py.ast;
        if (r == 8) return stky(s, &s->px);
        if (r == 9) return s->py.sticky;
    }
    int k = g == 6 ? r : 16 + r;
    if (s->sysp[k]) return s->sysv[k];
    if (g == 6) switch (r) {
        case 12: return s->PX1;
        case 13: return s->PX2;
        case 11: return s->PX2;
        case 4: return s->npc ? (u32)s->pcstk[s->npc - 1] : 0;
        case 5: return s->npc;
        case 8: return s->has_lcntr ? (u32)s->lcntr : 0;
        case 7: return s->nl ? (s->lst[s->nl - 1].has_count ? (u32)s->lst[s->nl - 1].count : 0) : 0;
        case 6: return s->nl ? (u32)s->lst[s->nl - 1].end : 0;
        case 9: return (u32)(s->icount & M32);
        case 10: return (u32)(s->icount >> 32);
        case 14: case 15: case 0: case 1: return 0;
    }
    fail(s, E_NOTIMPL, "read ureg %#x @ %#llx", u, (long long)s->pc);
    return 0;
}

static void set_ureg(Sim *s, int u, u32 v, PE *pe) {
    int g = (u >> 4) & 7, r = u & 15;
    if (!pe) pe = &s->px;
    switch (g) {
    case 0: pe->R[r] = v; return;
    case 1: s->I[r] = v; return;
    case 2: s->M[r] = v; return;
    case 3: s->L[r] = v; return;
    case 4: s->Bx[r] = v; s->I[r] = v; return;
    case 5: s->py.R[r] = v; return;
    }
    if (g == 7) {
        if (r == 6) { s->px.ast = v; return; }
        if (r == 7) { s->py.ast = v; return; }
        if (r == 8) { s->px.sticky = v & ~((1u << PCFL) | (1u << PCEM) | (1u << SSOV) | (1u << SSEM) | (1u << LSOV) | (1u << LSEM)); return; }
        if (r == 9) { s->py.sticky = v; return; }
    }
    int k = g == 6 ? r : 16 + r;
    if (s->sysp[k]) { s->sysv[k] = v; return; }
    if (g == 6) switch (r) {
        case 12: s->PX1 = v; return;
        case 13: s->PX2 = v; return;
        case 11: s->PX2 = v; s->PX1 = 0; return;
        case 4:
            if (s->npc) s->pcstk[s->npc - 1] = v;
            else { if (s->npc == s->cpc) { s->cpc = s->cpc * 2 + 16; s->pcstk = realloc(s->pcstk, s->cpc * sizeof(i64)); } s->pcstk[s->npc++] = v; }
            return;
        case 8: s->lcntr = v; s->has_lcntr = 1; return;
        case 7: if (s->nl) { s->lst[s->nl - 1].count = v; s->lst[s->nl - 1].has_count = 1; } return;
        case 14: case 15: case 0: case 1: case 9: case 10: s->sysv[k] = v; s->sysp[k] = 1; return;
    }
    fail(s, E_NOTIMPL, "write ureg %#x @ %#llx", u, (long long)s->pc);
}

static void pc_push(Sim *s, i64 v) {
    if (s->npc == s->cpc) { s->cpc = s->cpc * 2 + 16; s->pcstk = realloc(s->pcstk, s->cpc * sizeof(i64)); }
    s->pcstk[s->npc++] = v;
}
static LoopE *loop_push(Sim *s) {
    if (s->nl == s->cl) { s->cl = s->cl * 2 + 8; s->lst = realloc(s->lst, s->cl * sizeof(LoopE)); }
    return &s->lst[s->nl++];
}

/* ------------------------------------------------------------------ conditions */
static int cond(Sim *s, int c, PE *pe) {
    if (c == 31) return 1;
    if (!pe) pe = &s->px;
    u32 a = pe->ast;
    if (c == 15) return s->nl && (s->lst[s->nl - 1].has_count ? s->lst[s->nl - 1].count : 0) != 1;
    int neg = c >= 16, k = neg ? c - 16 : c, v;
    switch (k) {
    case 0: v = a >> AZ & 1; break;
    case 1: v = (a >> AN & 1) && !(a >> AZ & 1); break;
    case 2: v = (a >> AN & 1) || (a >> AZ & 1); break;
    case 3: v = a >> AC & 1; break;
    case 4: v = a >> AV & 1; break;
    case 5: v = a >> MV & 1; break;
    case 6: v = a >> MN & 1; break;
    case 7: v = a >> SV & 1; break;
    case 8: v = a >> SZ & 1; break;
    case 9: case 10: case 11: case 12: v = 0; break;
    case 13: v = a >> BTF & 1; break;
    case 14: v = 0; break;
    default: fail(s, E_NOTIMPL, "cond %d", c); return 0;
    }
    return neg ? !v : v;
}

static int cond_both(Sim *s, int c) {
    if (!simd(s) || c == 31 || c == 15) return cond(s, c, NULL);
    int cx = cond(s, c, &s->px), cy = cond(s, c, &s->py);
    return cx && cy;
}

/* ------------------------------------------------------------------ flags */
static inline void fixflags(PE *pe, i64 v, int av, int ac) {
    u32 w = (u32)v;
    u32 a = pe->ast & ~((1u << AZ) | (1u << AN) | (1u << AV) | (1u << AC) | (1u << AI) | (1u << AF));
    if (w == 0) a |= 1u << AZ;
    if (w & 0x80000000u) a |= 1u << AN;
    if (av) a |= 1u << AV;
    if (ac) a |= 1u << AC;
    pe->ast = a;
}
static inline void fltflags(PE *pe, u32 u, int ai, int av) {
    u32 a = pe->ast & ~((1u << AZ) | (1u << AN) | (1u << AV) | (1u << AC) | (1u << AI));
    a |= 1u << AF;
    if ((u & 0x7FFFFFFF) == 0) a |= 1u << AZ;
    if ((u & 0x80000000u) && !ai) a |= 1u << AN;
    if (ai) a |= 1u << AI;
    if (av) a |= 1u << AV;
    pe->ast = a;
}
static inline void shf(PE *pe, i64 v, int sv, int ss) {
    u32 a = pe->ast & ~((1u << SZ) | (1u << SV) | (1u << SS));
    if ((u32)v == 0) a |= 1u << SZ;
    if (sv) a |= 1u << SV;
    if (ss) a |= 1u << SS;
    pe->ast = a;
}
static inline void setbit(PE *pe, int b, int on) { pe->ast = on ? (pe->ast | (1u << b)) : (pe->ast & ~(1u << b)); }

/* ------------------------------------------------------------------ compute */
static u32 addsub(Sim *s, PE *pe, u32 X, u32 Y, int sub, i64 ci) {
    i64 sx = S32(X), sy = S32(Y);
    i64 r = sub ? sx - sy + ci : sx + sy + ci;
    u64 ux = X, uy = Y;
    int carry = sub ? ((i64)(ux + ((~uy) & M32) + 1) + ci > (i64)M32) : ((i64)(ux + uy) + ci > (i64)M32);
    int ov = r > 0x7FFFFFFFLL || r < -0x80000000LL;
    if (ov && (MODE1 & ALUSAT)) r = r > 0 ? 0x7FFFFFFFLL : -0x80000000LL;
    fixflags(pe, r, ov, carry);
    return (u32)r;
}

static u32 do_fix(Sim *s, PE *pe, double v, int mode /* -1 None, 0 nearest, 1 floor, 2 zero */) {
    u32 r;
    if (v != v) r = 0xFFFFFFFFu;
    else {
        if (mode == -1) mode = (MODE1 & TRUNCB) ? 1 : 0;
        i64 iv;
        if (isinf(v)) iv = v > 0 ? 0x7FFFFFFFLL : -0x80000000LL;
        else {
            double d = mode == 2 ? trunc(v) : mode == 1 ? floor(v) : nearbyint(v);
            if (d > 2147483647.0) iv = 0x7FFFFFFFLL;
            else if (d < -2147483648.0) iv = -0x80000000LL;
            else iv = (i64)d;
        }
        r = (u32)iv;
    }
    fixflags(pe, r, 0, 0);
    return r;
}

static inline u32 mr_result(i128 acc, int frac, int rnd) {
    if (frac) { if (rnd) acc += (i128)1 << 31; return (u32)((acc >> 32) & M32); }
    return (u32)(acc & M32);
}
static inline u32 fmul_fixed(u32 X, u32 Y) {           /* _fmul_fixed(pe, X, Y, 1, 1, 1, 1) */
    i128 p = (i128)S32(X) * S32(Y);
    p <<= 1;
    return mr_result(p, 1, 1);
}
static inline i128 s80(i128 v) {
    i128 m = ((i128)1 << 80) - 1;
    v &= m;
    if (v & ((i128)1 << 79)) v -= (i128)1 << 80;
    return v;
}

static void falu(Sim *s, PE *pe, int op, int n, int x, int y);

static void alu(Sim *s, PE *pe, int op, int n, int x, int y) {
    u32 *R = pe->R;
    u32 X = R[x], Y = R[y];
    i64 sx = S32(X), sy = S32(Y);
    i64 ci = pe->ast >> AC & 1;
    switch (op) {
    case 0x01: R[n] = addsub(s, pe, X, Y, 0, 0); return;
    case 0x02: R[n] = addsub(s, pe, X, Y, 1, 0); return;
    case 0x05: R[n] = addsub(s, pe, X, Y, 0, ci); return;
    case 0x06: R[n] = addsub(s, pe, X, Y, 1, ci - 1); return;
    case 0x09: R[n] = (u32)((sx + sy) >> 1); fixflags(pe, R[n], 0, 0); return;
    case 0x0a:
        fixflags(pe, sx == sy ? 0 : (sx > sy ? 1 : -1), 0, 0);
        pe->ast = (pe->ast & 0x00FFFFFF) | (((pe->ast >> 25) & 0x7F) << 24) | (sx > sy ? 0x80000000u : 0);
        return;
    case 0x0b:
        fixflags(pe, X == Y ? 0 : (X > Y ? 1 : -1), 0, 0);
        pe->ast = (pe->ast & 0x00FFFFFF) | (((pe->ast >> 25) & 0x7F) << 24) | (X > Y ? 0x80000000u : 0);
        return;
    case 0x25: R[n] = addsub(s, pe, X, 0, 0, ci); return;
    case 0x26: R[n] = addsub(s, pe, X, 0, 0, ci - 1); return;
    case 0x29: R[n] = addsub(s, pe, X, 1, 0, 0); return;
    case 0x2a: R[n] = addsub(s, pe, X, 1, 1, 0); return;
    case 0x22: R[n] = addsub(s, pe, 0, X, 1, 0); return;
    case 0x30: {
        i64 v = sx < 0 ? -sx : sx; int ov = v > 0x7FFFFFFFLL;
        if (ov && (MODE1 & ALUSAT)) v = 0x7FFFFFFFLL;
        R[n] = (u32)v; fixflags(pe, R[n], ov, 0); setbit(pe, AS, sx < 0);
        return;
    }
    case 0x21: R[n] = X; fixflags(pe, R[n], 0, 0); return;
    case 0x40: R[n] = X & Y; fixflags(pe, R[n], 0, 0); return;
    case 0x41: R[n] = X | Y; fixflags(pe, R[n], 0, 0); return;
    case 0x42: R[n] = X ^ Y; fixflags(pe, R[n], 0, 0); return;
    case 0x43: R[n] = ~X; fixflags(pe, R[n], 0, 0); return;
    case 0x61: R[n] = (u32)(sx < sy ? sx : sy); fixflags(pe, R[n], 0, 0); return;
    case 0x62: R[n] = (u32)(sx > sy ? sx : sy); fixflags(pe, R[n], 0, 0); return;
    case 0x63: {
        i64 lim = sy < 0 ? -sy : sy, m = lim < sx ? lim : sx;
        R[n] = (u32)(-lim > m ? -lim : m); fixflags(pe, R[n], 0, 0);
        return;
    }
    }
    falu(s, pe, op, n, x, y);
}

static void falu(Sim *s, PE *pe, int op, int n, int x, int y) {
    u32 *R = pe->R;
    u32 X = R[x], Y = R[y];
    double fx = f_of(X), fy = f_of(Y);
    i64 sy = S32(Y);
    u32 r;
    switch (op) {
    case 0x81: r = u_of(fx + fy); break;
    case 0x82: r = u_of(fx - fy); break;
    case 0x91: r = u_of(fabs(fx + fy)); break;
    case 0x92: r = u_of(fabs(fx - fy)); break;
    case 0x89: r = u_of((fx + fy) / 2); break;
    case 0x8a: {
        int az = fx == fy, an = fx < fy;
        u32 a = pe->ast & ~((1u << AZ) | (1u << AN) | (1u << AI));
        if (az) a |= 1u << AZ;
        if (an) a |= 1u << AN;
        if (fx != fx || fy != fy) a |= 1u << AI;
        pe->ast = (a & 0x00FFFFFF) | (((a >> 25) & 0x7F) << 24) | (fx > fy ? 0x80000000u : 0);
        return;
    }
    case 0xa2: r = u_of(-fx); break;
    case 0xb0: r = u_of(fabs(fx)); break;
    case 0xa1: case 0xa5: r = u_of(fx); break;
    case 0xbd:
        if (fx == fx && !isinf(fx)) { i64 e = sy < -300 ? -300 : sy > 300 ? 300 : sy; r = u_of(ldexp(fx, (int)e)); }
        else r = u_of(fx);
        break;
    case 0xad: {
        if (fx == 0 || fx != fx || isinf(fx)) { R[n] = 0; fixflags(pe, 0, 0, 0); return; }
        int e; double mnt = frexp(fabs(fx), &e);
        R[n] = (u32)((u64)(mnt * 2 * 2147483648.0) & M32); fixflags(pe, R[n], 0, 0);
        return;
    }
    case 0xc1: {
        if (fx == 0) { R[n] = 0xFF800000u; fixflags(pe, R[n], 0, 0); return; }
        int e = 0;
        if (fx == fx && !isinf(fx)) frexp(fabs(fx), &e);
        R[n] = (u32)(e - 1); fixflags(pe, R[n], 0, 0);
        return;
    }
    case 0xc9: case 0xd9: case 0xcd: case 0xdd: {
        double sc = 1.0;
        if (op == 0xd9 || op == 0xdd) { sc = pow2(s, sy); if (s->err) return; }
        R[n] = do_fix(s, pe, fx * sc, (op == 0xcd || op == 0xdd) ? 2 : -1);
        return;
    }
    case 0xca: case 0xda: {
        double sc = 1.0;
        if (op == 0xda) { sc = pow2(s, sy); if (s->err) return; }
        r = u_of((double)S32(X) * sc);
        break;
    }
    case 0xc4: r = fx != 0 ? u_of(1.0 / fx) : (0x7F800000u | (X & 0x80000000u)); break;
    case 0xc5: r = fx > 0 ? u_of(pow(fx, -0.5)) : (fx == 0 ? 0x7F800000u : 0xFFFFFFFFu); break;
    case 0xe0: r = u_of(copysign(fabs(fx), fy)); break;
    case 0xe1: case 0xe2: case 0xe3:
        if (fx != fx || fy != fy) r = 0xFFFFFFFFu;
        else if (op == 0xe1) r = u_of(pymin(fx, fy));
        else if (op == 0xe2) r = u_of(pymax(fx, fy));
        else r = u_of(pymax(-fabs(fy), pymin(fabs(fy), fx)));
        break;
    default:
        fail(s, E_NOTIMPL, "alu %#x @ %#llx", op, (long long)s->pc);
        return;
    }
    R[n] = r;
    fltflags(pe, r, r == 0xFFFFFFFFu, (r & 0x7FFFFFFF) == 0x7F800000u);
}

static void mul(Sim *s, PE *pe, int op, int n, int x, int y) {
    u32 *R = pe->R;
    if (op == 0x30) {
        u32 r = u_of(f_of(R[x]) * f_of(R[y])); R[n] = r;
        u32 a = pe->ast & ~((1u << MN) | (1u << MV) | (1u << MU) | (1u << MI));
        if (r & 0x80000000u) a |= 1u << MN;
        pe->ast = a;
        return;
    }
    int hi = (op >> 6) & 3;
    if (hi) {
        int ys = op >> 5 & 1, xs = op >> 4 & 1, frac = op >> 3 & 1, dst = op >> 1 & 3, rnd = op & 1;
        i128 xv = xs ? S32(R[x]) : (i64)R[x];
        i128 yv = ys ? S32(R[y]) : (i64)R[y];
        i128 p = xv * yv;
        if (frac) p <<= 1;
        i128 *acc = (dst == 1 || dst == 3) ? &pe->MRB : &pe->MRF;
        i128 res = hi == 1 ? p : hi == 2 ? *acc + p : *acc - p;
        res = s80(res);
        if (dst == 2 || dst == 3) *acc = res;
        else R[n] = mr_result(res, frac, rnd);
        return;
    }
    if (op == 0x14 || op == 0x16) { if (op == 0x14) pe->MRF = 0; else pe->MRB = 0; return; }
    if ((op >> 4) == 0 || ((op >> 4) == 1 && (op & 8))) {
        int rnd = (op >> 4) == 1;
        int frac = (op & 8) || rnd;
        int dest = (op >> 1) & 3, sgn = op & 1;
        i128 *accp = (dest == 1 || dest == 3) ? &pe->MRB : &pe->MRF;
        i128 acc = *accp, v;
        if (rnd) v = (acc + ((i128)1 << 31)) & ~(((i128)1 << 32) - 1);
        else if (frac) {
            i128 lo = sgn ? -((i128)1 << 63) : 0, hi_ = sgn ? ((i128)1 << 63) - 1 : ((i128)1 << 64) - 1;
            v = acc < lo ? lo : acc > hi_ ? hi_ : acc;
        } else {
            i128 lo = sgn ? -((i128)1 << 31) : 0, hi_ = sgn ? ((i128)1 << 31) - 1 : ((i128)1 << 32) - 1;
            v = acc < lo ? lo : acc > hi_ ? hi_ : acc;
        }
        if (dest == 2 || dest == 3) *accp = v;
        else R[n] = (u32)((frac ? (v >> 32) : v) & M32);
        return;
    }
    fail(s, E_NOTIMPL, "mul op %#x @ %#llx", op, (long long)s->pc);
}

static inline u32 lsh(u32 X, i64 sh) {
    if (sh >= 32 || sh <= -32) return 0;
    return sh >= 0 ? (u32)((u64)X << sh) : X >> -sh;
}
static inline u32 ash(u32 X, i64 sh) {
    i64 v = S32(X);
    if (sh >= 32) return 0;
    if (sh <= -32) return v < 0 ? M32 : 0;
    return sh >= 0 ? (u32)((u64)v << sh) : (u32)(v >> -sh);
}
static inline u32 rot(u32 X, i64 d8) {
    i64 k = ((d8 % 32) + 32) % 32;
    return k ? (u32)(((u64)X << k) | (X >> (32 - k))) : X;
}
static inline u32 expo(u32 X) {
    i64 v = S32(X); int lz;
    if (v >= 0) lz = v == 0 ? 32 : __builtin_clz((u32)v);
    else { u32 nv = ~(u32)v; lz = nv == 0 ? 32 : __builtin_clz(nv); }
    return lz ? (u32)(-(lz - 1)) : 0;
}
static inline int clz32(u32 X) { return X == 0 ? 32 : __builtin_clz(X); }
static inline u64 shl64(u64 v, int pos) { return pos >= 64 ? 0 : v << pos; }

static void field(Sim *s, PE *pe, int op, int n, u32 X, int pos, int ln) {
    u32 *R = pe->R;
    u64 mask = (1ULL << ln) - 1;                     /* ln <= 63 */
    if (op == 0x40 || op == 0x48) {
        u64 v = pos < 32 ? ((u64)X >> pos) & mask : 0;
        if (op == 0x48 && ln && (v & (1ULL << (ln - 1)))) v |= ~mask;
        R[n] = (u32)v;
    } else {
        u64 v = X & mask;
        if ((op == 0x4c || op == 0x6c) && ln && (v & (1ULL << (ln - 1)))) v |= ~mask & 0xFFFFFFFFFFFFULL;
        u32 w = (u32)shl64(v, pos);
        if (op == 0x64 || op == 0x6c) {
            u32 keep = op == 0x64 ? (R[n] & ~(u32)shl64(mask, pos)) : R[n];
            w = keep | w;
        }
        R[n] = w;
    }
    shf(pe, R[n], 0, 0);
}

static void bitdep(PE *pe, u32 X, i64 ln) {
    i64 wrp = pe->has_bffwrp ? pe->bffwrp : 0; u64 bff = pe->has_bff ? pe->bff : 0;
    if (ln > 32) ln = 32;
    i64 pos = 64 - (wrp + ln);
    if (ln && pos >= 0) bff |= shl64(X & ((1ULL << ln) - 1), (int)pos);
    pe->bff = bff; pe->has_bff = 1; pe->bffwrp = wrp + ln; pe->has_bffwrp = 1;
    shf(pe, 1, 0, 0);
}

static void shift_reg(Sim *s, PE *pe, int op, int n, int x, int y) {
    u32 *R = pe->R;
    u32 X = R[x];
    i64 sy8 = sext(R[y] & 0xFF, 8);
    switch (op) {
    case 0x00: case 0x20: case 0x04: case 0x24: case 0x08: {
        u32 v = (op == 0x00 || op == 0x20) ? lsh(X, sy8) : (op == 0x04 || op == 0x24) ? ash(X, sy8) : rot(X, sy8);
        if (op == 0x20 || op == 0x24) v |= R[n];
        R[n] = v; shf(pe, R[n], 0, 0); return;
    }
    case 0xc0: case 0xc4: case 0xc8: case 0xcc: {
        int bit = R[y] & 31;
        if (op == 0xc0) R[n] = X | (1u << bit);
        else if (op == 0xc4) R[n] = X & ~(1u << bit);
        else if (op == 0xc8) R[n] = X ^ (1u << bit);
        else { shf(pe, X & (1u << bit), 0, 0); return; }
        shf(pe, R[n], 0, 0); return;
    }
    case 0x40: case 0x48: case 0x44: case 0x64: case 0x4c: case 0x6c:
        field(s, pe, op, n, X, R[y] & 0x3F, (R[y] >> 6) & 0x3F); return;
    case 0x80: case 0x84:
        R[n] = expo(X); shf(pe, 1, 0, (X & 0x80000000u) != 0); return;
    case 0x88: { int v = clz32(X); R[n] = v; shf(pe, v, v == 32, 0); return; }
    case 0x8c: { int v = clz32(~X); R[n] = v; shf(pe, v, v == 32, 0); return; }
    case 0x7c: pe->bffwrp = X & 0x7F; pe->has_bffwrp = 1; return;
    case 0x70: R[n] = pe->has_bffwrp ? (u32)pe->bffwrp : 0; return;
    }
    fail(s, E_NOTIMPL, "shift %#x @ %#llx", op, (long long)s->pc);
}

static void shift_imm1(Sim *s, PE *pe, u32 comp, u32 dataex) {
    int op = (comp >> 16) & 0x3F;
    u32 data = (dataex << 8) | ((comp >> 8) & 0xFF);
    int n = (comp >> 4) & 0xF, x = comp & 0xF;
    u32 *R = pe->R;
    u32 X = R[x], v;
    i64 d8 = sext(data & 0xFF, 8);
    switch (op) {
    case 0x00: case 0x08: v = lsh(X, d8) | (op == 0x08 ? R[n] : 0); break;
    case 0x01: case 0x09: v = ash(X, d8) | (op == 0x09 ? R[n] : 0); break;
    case 0x02: v = rot(X, d8); break;
    case 0x30: case 0x31: case 0x32: case 0x33: {
        int bit = data & 31;
        if (op == 0x30) v = X | (1u << bit);
        else if (op == 0x31) v = X & ~(1u << bit);
        else if (op == 0x32) v = X ^ (1u << bit);
        else { shf(pe, X & (1u << bit), 0, 0); return; }
        break;
    }
    case 0x10: case 0x12: case 0x11: case 0x19: case 0x13: case 0x1b: {
        int fop = op == 0x10 ? 0x40 : op == 0x12 ? 0x48 : op == 0x11 ? 0x44 : op == 0x19 ? 0x64 : op == 0x13 ? 0x4c : 0x6c;
        field(s, pe, fop, n, X, data & 0x3F, (data >> 6) & 0x3F); return;
    }
    case 0x20: case 0x21: R[n] = expo(X); shf(pe, 1, 0, (X & 0x80000000u) != 0); return;
    case 0x22: { int c = clz32(X); R[n] = c; shf(pe, c, c == 32, 0); return; }
    case 0x23: { int c = clz32(~X); R[n] = c; shf(pe, c, c == 32, 0); return; }
    case 0x1f: pe->bffwrp = data & 0x7F; pe->has_bffwrp = 1; return;
    case 0x1d: bitdep(pe, X, data & 0xFFF); return;
    default: fail(s, E_NOTIMPL, "shift imm %#x @ %#llx", op, (long long)s->pc); return;
    }
    R[n] = v;
    shf(pe, R[n], 0, 0);
}

static void shift_imm(Sim *s, u32 comp, u32 dataex) {
    shift_imm1(s, &s->px, comp, dataex);
    if (simd(s)) shift_imm1(s, &s->py, comp, dataex);
}

static void compute1(Sim *s, u32 c, PE *pe) {
    u32 *R = pe->R;
    if (c & 0x400000) {
        int op6 = (c >> 16) & 0x3F;
        int m = (c >> 12) & 0xF, a = (c >> 8) & 0xF;
        int xm = (c >> 6) & 3, ym = ((c >> 4) & 3) + 4, xa = ((c >> 2) & 3) + 8, ya = (c & 3) + 12;
        if (op6 == 0 || op6 == 1) {
            int sel = (c >> 12) & 7;
            i128 *acc = sel < 4 ? &pe->MRF : &pe->MRB;
            int part = sel & 3;
            if (part == 3) { fail(s, E_NOTIMPL, "MR part 3"); return; }
            if (op6 == 0) {
                i128 mr = *acc;
                R[a] = (u32)((mr >> (32 * part)) & M32);
                if (part == 2) R[a] = (u32)((mr >> 64) & 0xFFFF);
            } else {
                i128 mr = *acc & (((i128)1 << 80) - 1);
                int sh = 32 * part, width = part < 2 ? 32 : 16;
                i128 wm = ((i128)1 << width) - 1;
                mr = (mr & ~(wm << sh)) | (((i128)R[a] & wm) << sh);
                *acc = mr;
            }
            return;
        }
        u32 X = R[xm], Y = R[ym], XA = R[xa], YA = R[ya];
        if (op6 >= 0x20) {
            int sr = op6 & 0xF;
            if (op6 & 0x10) {
                double p = f_of(X) * f_of(Y), fx = f_of(XA), fy = f_of(YA);
                R[m] = u_of(p); R[a] = u_of(fx + fy); R[sr] = u_of(fx - fy);
                fltflags(pe, R[sr], 0, 0);
            } else {
                R[m] = fmul_fixed(X, Y);
                R[a] = addsub(s, pe, XA, YA, 0, 0);
                R[sr] = addsub(s, pe, XA, YA, 1, 0);
            }
            return;
        }
        if (op6 >= 0x18) {
            double p = f_of(X) * f_of(Y);
            R[m] = u_of(p);
            double fx = f_of(XA), fy = f_of(YA);
            u32 r;
            if (op6 == 0x18) r = u_of(fx + fy);
            else if (op6 == 0x19) r = u_of(fx - fy);
            else if (op6 == 0x1a) { double sc = pow2(s, S32(YA)); if (s->err) return; r = u_of((double)S32(XA) * sc); }
            else if (op6 == 0x1b) { double sc = pow2(s, S32(YA)); if (s->err) return; r = do_fix(s, pe, fx * sc, -1); R[a] = r; return; }
            else if (op6 == 0x1c) r = u_of((fx + fy) / 2);
            else if (op6 == 0x1d) r = u_of(fabs(fx));
            else if (fx != fx || fy != fy) r = 0xFFFFFFFFu;
            else if (op6 == 0x1e) r = u_of(pymax(fx, fy));
            else r = u_of(pymin(fx, fy));
            R[a] = r; fltflags(pe, r, 0, 0);
            return;
        }
        /* fixed multiply (SSFR / MR acc) + fixed ALU */
        int mk, ak;                                          /* mk 0 R, 1 MRF+, 2 RMRF+, 3 MRF-, 4 RMRF-; ak 0 +, 1 -, 2 avg */
        switch (op6) {
        case 0x04: mk = 0; ak = 0; break; case 0x05: mk = 0; ak = 1; break; case 0x06: mk = 0; ak = 2; break;
        case 0x08: mk = 1; ak = 0; break; case 0x09: mk = 1; ak = 1; break; case 0x0a: mk = 1; ak = 2; break;
        case 0x0c: mk = 2; ak = 0; break; case 0x0d: mk = 2; ak = 1; break; case 0x0e: mk = 2; ak = 2; break;
        case 0x10: mk = 3; ak = 0; break; case 0x11: mk = 3; ak = 1; break; case 0x12: mk = 3; ak = 2; break;
        case 0x14: mk = 4; ak = 0; break; case 0x15: mk = 4; ak = 1; break; case 0x16: mk = 4; ak = 2; break;
        default: fail(s, E_NOTIMPL, "multifunc %#x @ %#llx", op6, (long long)s->pc); return;
        }
        i128 prod = ((i128)S32(X) * S32(Y)) << 1;
        if (mk == 0) R[m] = fmul_fixed(X, Y);
        else if (mk == 1) pe->MRF = pe->MRF + prod;
        else if (mk == 3) pe->MRF = pe->MRF - prod;
        else { i128 acc = mk == 2 ? pe->MRF + prod : pe->MRF - prod; R[m] = mr_result(acc, 1, 1); }
        if (ak == 0) R[a] = addsub(s, pe, XA, YA, 0, 0);
        else if (ak == 1) R[a] = addsub(s, pe, XA, YA, 1, 0);
        else { R[a] = (u32)((S32(XA) + S32(YA)) >> 1); fixflags(pe, R[a], 0, 0); }
        return;
    }
    int cu = (c >> 20) & 3;
    int op = (c >> 12) & 0xFF;
    int n = (c >> 8) & 0xF, x = (c >> 4) & 0xF, y = c & 0xF;
    if (cu == 0) {
        if (op >> 4 == 0x7) {
            u32 X = R[x], Y = R[y];
            R[n] = addsub(s, pe, X, Y, 0, 0); R[op & 0xF] = addsub(s, pe, X, Y, 1, 0); return;
        }
        if (op >> 4 == 0xF) {
            double fx = f_of(R[x]), fy = f_of(R[y]);
            R[n] = u_of(fx + fy); R[op & 0xF] = u_of(fx - fy); fltflags(pe, R[op & 0xF], 0, 0); return;
        }
        alu(s, pe, op, n, x, y); return;
    }
    if (cu == 1) { mul(s, pe, op, n, x, y); return; }
    if (cu == 2) { shift_reg(s, pe, op, n, x, y); return; }
    fail(s, E_NOTIMPL, "compute cu3 %#x @ %#llx", c, (long long)s->pc);
}

static void compute(Sim *s, u32 c) {
    if (!c) return;
    compute1(s, c, &s->px);
    if (simd(s)) compute1(s, c, &s->py);
}

static void branch_compute(Sim *s, int cnd, u32 comp, int els) {
    int rx = (cnd == 31 || cond(s, cnd, &s->px)) != (els != 0);
    int ry = simd(s) ? ((cnd == 31 || cond(s, cnd, &s->py)) != (els != 0)) : 0;
    if (rx) compute1(s, comp, &s->px);
    if (ry) compute1(s, comp, &s->py);
}

static void short_compute(Sim *s, int op, int n, int r) {
    for (int k = 0; k < (simd(s) ? 2 : 1); k++) {
        PE *pe = k ? &s->py : &s->px;
        u32 *R = pe->R;
        switch (op) {
        case 0: R[n] = addsub(s, pe, R[n], R[r], 0, 0); break;
        case 1: R[n] = addsub(s, pe, R[n], R[r], 1, 0); break;
        case 2: R[n] = R[r]; fixflags(pe, R[n], 0, 0); break;
        case 3: alu(s, pe, 0x0a, 0, n, r); break;
        case 4: R[n] = ~R[r]; fixflags(pe, R[n], 0, 0); break;
        case 5: R[n] = addsub(s, pe, R[r], 1, 0, 0); break;
        case 6: R[n] = addsub(s, pe, R[r], 1, 1, 0); break;
        case 7: R[n] = (u32)(S32(R[n]) * S32(R[r])); break;
        case 8: falu(s, pe, 0x81, n, n, r); break;
        case 9: falu(s, pe, 0x82, n, n, r); break;
        case 10: falu(s, pe, 0xca, n, r, 0); break;
        case 11: falu(s, pe, 0x8a, 0, n, r); break;
        case 12: R[n] = R[n] & R[r]; fixflags(pe, R[n], 0, 0); break;
        case 13: R[n] = R[n] | R[r]; fixflags(pe, R[n], 0, 0); break;
        case 14: R[n] = R[n] ^ R[r]; fixflags(pe, R[n], 0, 0); break;
        case 15: mul(s, pe, 0x30, n, n, r); break;
        }
    }
}

/* ------------------------------------------------------------------ DAG */
static inline int sc(u32 a) {
    if ((a >= 0x80000 && a < 0xF0000) || a < 0x80000) return 1;
    if (a >= 0x120000 && a < 0x1E0000) return 1;
    return 4;
}
static inline void update_i(Sim *s, int i, u32 base, i64 mv) {
    u32 L = s->L[i];
    i64 nv = (i64)base + mv;
    if (L && (MODE1 & CBUFEN)) {
        i64 Bx = s->Bx[i], Lb = (i64)L * sc(s->Bx[i]);
        if (nv >= Bx + Lb) nv -= Lb;
        else if (nv < Bx) nv += Lb;
    }
    s->I[i] = (u32)nv;
}
static inline u32 dag_ea(Sim *s, int i, int m, int pre, int width) {
    u32 base = s->I[i];
    s->ea_i = i;
    i64 mv = S32(s->M[m]);
    if (sc(base) == 4) mv *= width < 4 ? width : 4;
    if (pre) return (u32)(base + mv);
    update_i(s, i, base, mv);
    return base;
}
static inline u32 ystep(u32 addr, int size) {
    if (addr >= 0x80000 && addr < 0xF0000) return 1;
    if (addr >= 0x120000 && addr < 0x1E0000) return 2;
    return size;
}
static inline int bcast(Sim *s, int i) { return (i == 1 && (MODE1 & BDCST1)) || (i == 9 && (MODE1 & BDCST9)); }

static void px48(Sim *s, u32 a, int write) {
    if (!(a >= 0x80000 && a < 0xF0000)) { fail(s, E_NOTIMPL, "48-bit access outside NW space %#x @ %#llx", a, (long long)s->pc); return; }
    u32 base = a & ~0xFFFFu, sw = 2 * base + 3 * (a - base), pa = 2 * sw;
    if (write) {
        u32 w[3] = {(s->PX2 >> 16) & 0xFFFF, s->PX2 & 0xFFFF, (s->PX1 >> 16) & 0xFFFF};
        for (int k = 0; k < 3; k++) mem_w(s, pa + 2 * k, w[k], 2);
    } else {
        u32 w0 = mem_r(s, pa, 2, 0), w1 = mem_r(s, pa + 2, 2, 0), w2 = mem_r(s, pa + 4, 2, 0);
        s->PX2 = (w0 << 16) | w1;
        s->PX1 = w2 << 16;
    }
}

static void dreg_access(Sim *s, u32 addr, int reg, int write, int size, int sx, int lw, int ureg) {
    int is_data = ureg < 0 || (ureg >> 4) == 0;
    int u = ureg >= 0 ? ureg : reg;
    if (u == 0x6B) { px48(s, addr, write); return; }
    u32 step = (addr >= 0x240000 || addr >= 0x20000000u) ? 4 : 1;
    if (lw) {
        addr &= step == 4 ? ~7u : ~1u;
        int r0 = u & ~1;
        if (write) {
            u32 v0 = get_ureg(s, r0, NULL); mem_w(s, addr, v0, 4);
            u32 v1 = get_ureg(s, r0 | 1, NULL); mem_w(s, addr + step, v1, 4);
        } else {
            set_ureg(s, r0, mem_r(s, addr, 4, 0), NULL);
            set_ureg(s, r0 | 1, mem_r(s, addr + step, 4, 0), NULL);
        }
        return;
    }
    if (size != 4) step = ystep(addr, size);
    if (write) {
        mem_w(s, addr, get_ureg(s, u, NULL), size);
        if (simd(s)) {
            u32 yv = is_data ? s->py.R[u & 0xF] : u == 0x76 ? s->py.ast : u == 0x78 ? s->py.sticky : get_ureg(s, u, NULL);
            mem_w(s, addr + step, yv, size);
        }
    } else {
        u32 v = mem_r(s, addr, size, sx);
        if (is_data && simd(s)) {
            u32 vy = mem_r(s, addr + (bcast(s, s->ea_i) ? 0 : step), size, sx);
            set_ureg(s, u, v, NULL); s->py.R[u & 0xF] = vy;
        } else set_ureg(s, u, v, NULL);
    }
}

static void pairmove(Sim *s, u32 a, int r, int d, const u32 *prex, const u32 *prey, int i) {
    u32 step = ystep(a, 4);
    if (d) {
        mem_w(s, a, prex[r], 4);
        if (simd(s)) mem_w(s, a + step, prey[r], 4);
    } else {
        s->px.R[r] = mem_r(s, a, 4, 0);
        if (simd(s)) s->py.R[r] = mem_r(s, a + (bcast(s, i) ? 0 : step), 4, 0);
    }
}

/* ------------------------------------------------------------------ execute */
static inline Uop *uop_at(Sim *s, i64 pc) {
    if ((u64)pc >= (1u << 24)) return NULL;
    Uop *t = s->code[pc >> 12];
    if (!t) return NULL;
    Uop *u = &t[pc & 4095];
    return u->size ? u : NULL;
}

#define TGT_NONE 0
#define TGT_NOW 1
#define TGT_DELAYED 2

static int branch(Sim *s, Uop *ins, i64 tgt, int call, i64 nxt, int la, i64 *out) {
    int db = ins->flags & F_DB;
    if (call) {
        if (db) {
            Uop *a = uop_at(s, nxt); Uop *b = a ? uop_at(s, nxt + a->size) : NULL;
            if (!a || !b) { fail(s, E_RUNTIME, "DB call without decoded slots @ %#llx", (long long)s->pc); return TGT_NONE; }
            pc_push(s, nxt + a->size + b->size);
        } else pc_push(s, nxt);
    }
    if (la && s->nl) s->nl--;
    *out = tgt;
    return db ? TGT_DELAYED : TGT_NOW;
}

static int exec_insn(Sim *s, Uop *ins, i64 nxt, i64 *tgt) {
    u64 x = ins->x;
    s->ea_i = -1;
    u32 comp = (u32)B(x, 22, 0);
    switch (ins->type) {
    case T_3a: case T_3b: case T_3d: {
        int cnd = B(x, 37, 33);
        int u = B(x, 44, 44), i = B(x, 43, 41), m = B(x, 40, 38), g = B(x, 32, 32), d = B(x, 31, 31), ur = B(x, 29, 23);
        int size = ins->asz, sx = ins->asx, lw = ins->alw;
        if (!cond_both(s, cnd)) return TGT_NONE;
        u32 prex[16], prey[16]; int pre = ins->type == T_3a && comp && d && (ur >> 4) == 0;
        if (pre) { memcpy(prex, s->px.R, 64); memcpy(prey, s->py.R, 64); }
        if (d) get_ureg(s, ur, NULL);
        if (ins->type == T_3a && comp) compute(s, comp);
        int ii = i + (g ? 8 : 0), mm = m + (g ? 8 : 0);
        u32 a = dag_ea(s, ii, mm, !u, lw ? 8 : size);
        if (pre) {
            mem_w(s, a, prex[ur & 0xF], size);
            if (simd(s) && !lw) mem_w(s, a + ystep(a, size), prey[ur & 0xF], size);
            if (lw) { mem_w(s, a + 4, prex[(ur & 0xF) | 1], 4); mem_w(s, a, prex[ur & 0xE], 4); }
            return TGT_NONE;
        }
        dreg_access(s, a, -1, d, size, sx, lw, ur);
        return TGT_NONE;
    }
    case T_2a: {
        int c = B(x, 37, 33);
        if (simd(s)) {
            if (cond(s, c, &s->px)) compute1(s, comp, &s->px);
            if (cond(s, c, &s->py)) compute1(s, comp, &s->py);
        } else if (cond_both(s, c)) compute(s, comp);
        return TGT_NONE;
    }
    case T_4a: case T_4b: case T_4d: {
        int cnd = B(x, 37, 33);
        int i = B(x, 43, 41), g = B(x, 40, 40), d = B(x, 39, 39), u = B(x, 38, 38), r = B(x, 26, 23);
        int size = ins->asz, sx = ins->asx;
        if (!cond_both(s, cnd)) return TGT_NONE;
        u32 prex[16], prey[16];
        memcpy(prex, s->px.R, 64); memcpy(prey, s->py.R, 64);
        if (ins->type == T_4a && comp) compute(s, comp);
        i64 data = sext(B(x, 32, 27), 6);
        int ii = i + (g ? 8 : 0);
        u32 base = s->I[ii], a;
        data *= sc(base) == 4 ? size : 1;
        if (u) { a = base; update_i(s, ii, base, data); }
        else a = (u32)(base + data);
        if (d) {
            mem_w(s, a, prex[r], size);
            if (simd(s)) mem_w(s, a + ystep(a, size), prey[r], size);
        } else {
            s->px.R[r] = mem_r(s, a, size, sx);
            if (simd(s)) s->py.R[r] = mem_r(s, a + (bcast(s, ii) ? 0 : ystep(a, size)), size, sx);
        }
        return TGT_NONE;
    }
    case T_2b:
        compute(s, (u32)B(x, 38, 16)); return TGT_NONE;
    case T_5a_move: case T_5b_move: {
        int cnd = B(x, 37, 33);
        int src = (int)((B(x, 42, 38) << 2) | B(x, 32, 31)), dst = B(x, 29, 23);
        int ok = cond_both(s, cnd);
        u32 v = get_ureg(s, src, NULL), vy;
        if ((src >> 4) == 0) vy = s->py.R[src & 0xF];
        else if ((src >> 4) == 5) vy = s->px.R[src & 0xF];
        else if (src == 0x76) vy = s->py.ast;
        else if (src == 0x78) vy = s->py.sticky;
        else vy = v;
        if (ins->type == T_5a_move && comp && ok) compute(s, comp);
        if (ok) {
            set_ureg(s, dst, v, NULL);
            if (simd(s) && (dst >> 4) == 0) s->py.R[dst & 0xF] = vy;
        }
        return TGT_NONE;
    }
    case T_6a_nomem:
        if (cond_both(s, B(x, 37, 33))) shift_imm(s, comp, (u32)B(x, 30, 27));
        return TGT_NONE;
    case T_15a: case T_15b: {
        int g, i, d, l, ur; i64 data;
        if (ins->type == T_15b) { g = B(x, 37, 37); i = B(x, 43, 41); d = B(x, 40, 40); l = B(x, 39, 39); ur = B(x, 29, 23); data = sext(B(x, 22, 16), 7); }
        else { g = B(x, 44, 44); i = B(x, 43, 41); d = B(x, 40, 40); l = B(x, 39, 39); ur = B(x, 38, 32); data = sext(B(x, 31, 0), 32); }
        int ii = i + (g ? 8 : 0);
        u32 base = s->I[ii];
        s->ea_i = ii;
        u32 a = (u32)(base + data * (sc(base) == 4 ? 4 : 1));
        dreg_access(s, a, -1, d, 4, 0, l, ur);
        return TGT_NONE;
    }
    case T_2c:
        short_compute(s, B(x, 43, 40), B(x, 39, 36), B(x, 35, 32)); return TGT_NONE;
    case T_3c: {
        int i = B(x, 43, 41), m = B(x, 40, 38), d = B(x, 37, 37), r = B(x, 35, 32);
        u32 a = dag_ea(s, i, m, 0, 4);
        dreg_access(s, a, r, d, 4, 0, 0, -1);
        return TGT_NONE;
    }
    case T_17a: case T_17b: {
        int ur = B(x, 38, 32);
        u32 data = ins->type == T_17a ? (u32)B(x, 31, 0) : (u32)sext(B(x, 31, 16), 16);
        set_ureg(s, ur, data, NULL);
        if (simd(s) && (ur >> 4) == 0) s->py.R[ur & 0xF] = data;
        return TGT_NONE;
    }
    case T_8a: {
        int bl = B(x, 39, 39), la = B(x, 38, 38), cnd = B(x, 37, 33);
        if (cond_both(s, cnd)) return branch(s, ins, ins->target, bl, nxt, la, tgt);
        return TGT_NONE;
    }
    case T_7a: case T_7a_nw: case T_7b: case T_7d: {
        int cnd = B(x, 37, 33);
        int ok = cond_both(s, cnd);
        if (ins->type != T_7b && comp && ok) compute(s, comp);
        if (!ok) return TGT_NONE;
        int g = B(x, 38, 38), i = B(x, 32, 30), m = B(x, 29, 27), idis = B(x, 26, 24);
        int off = g ? 8 : 0;
        if (ins->type == T_7d) {
            int breg = B(x, 28, 28), toby = B(x, 27, 27);
            u32 *p = breg ? &s->Bx[i + off] : &s->I[i + off];
            *p = toby ? (*p) * 4 : (*p) / 4;
            return TGT_NONE;
        }
        u32 base = s->I[i + off];
        i64 mv = S32(s->M[m + off]);
        if (ins->type == T_7a_nw) mv *= sc(base);
        else if (ins->type == T_7a && B(x, 23, 23)) mv *= sc(base) == 4 ? 2 : 1;
        if (idis) {
            u32 old = base;
            update_i(s, i + off, base, mv);
            s->I[(i ^ idis) + off] = s->I[i + off]; s->I[i + off] = old;
        } else update_i(s, i + off, base, mv);
        return TGT_NONE;
    }
    case T_19a: {
        int scl = B(x, 41, 40);
        int g = B(x, 38, 38), i = B(x, 34, 32), idis = B(x, 37, 35);
        i64 data = sext(B(x, 31, 0), 32);
        if (scl == 2 && B(x, 39, 39)) { fail(s, E_NOTIMPL, "BITREV"); return TGT_NONE; }
        int off = g ? 8 : 0;
        if (scl == 1) data *= B(x, 39, 39) ? 4 : 2;
        u32 base = s->I[i + off];
        if (idis) s->I[(i ^ idis) + off] = (u32)(base + data);
        else update_i(s, i + off, base, data);
        return TGT_NONE;
    }
    case T_16a: case T_16b: {
        int i = B(x, 43, 41), m = B(x, 40, 38), g = B(x, 37, 37);
        u32 data = ins->type == T_16a ? (u32)B(x, 31, 0) : (u32)sext(B(x, 31, 16), 16);
        u32 a = dag_ea(s, i + (g ? 8 : 0), m + (g ? 8 : 0), 0, 4);
        mem_w(s, a, data, 4);
        if (simd(s)) mem_w(s, a + 4, data, 4);
        return TGT_NONE;
    }
    case T_9a: case T_9b: {
        int rel = B(x, 40, 40), bl = B(x, 39, 39), la = B(x, 38, 38), cnd = B(x, 37, 33);
        int e = ins->type == T_9a ? B(x, 25, 25) : 0;
        int take = cond_both(s, cnd);
        if (ins->type == T_9a && comp) branch_compute(s, cnd, comp, e);
        if (take) {
            i64 t;
            if (rel) t = ins->target;
            else { int ii = B(x, 32, 30) + 8, mm = B(x, 29, 27) + 8; t = ((i64)s->I[ii] + S32(s->M[mm])) & 0xFFFFFF; }
            return branch(s, ins, t, bl, nxt, la, tgt);
        }
        return TGT_NONE;
    }
    case T_25a:
        { u32 i6 = s->I[6]; s->px.R[2] = i6; s->I[6] = s->I[7]; }
        *tgt = ins->target; return TGT_DELAYED;
    case T_25c: case T_25a_rframe:
        s->I[7] = s->I[6];
        s->I[6] = mem_r(s, s->I[6], 4, 0);
        return TGT_NONE;
    case T_18a: {
        int bop = B(x, 39, 37), sreg = B(x, 35, 32);
        u32 data = (u32)B(x, 31, 0);
        int u = 0x70 | sreg;
        u32 cur = get_ureg(s, u, NULL);
        if (bop == 0) set_ureg(s, u, cur | data, NULL);
        else if (bop == 1) set_ureg(s, u, cur & ~data, NULL);
        else if (bop == 2) set_ureg(s, u, cur ^ data, NULL);
        else if (bop == 4 || bop == 5) {
            int hit = bop == 4 ? (cur & data) == data : cur == data;
            setbit(&s->px, BTF, hit);
            if (simd(s)) setbit(&s->py, BTF, hit);
        } else fail(s, E_NOTIMPL, "18a bop %d", bop);
        return TGT_NONE;
    }
    case T_12a_imm: case T_12a_ureg: case T_13a: {
        i64 t = ins->target, cnt; int has = 1;
        if (ins->type == T_12a_imm) cnt = B(x, 39, 24);
        else if (ins->type == T_12a_ureg) cnt = get_ureg(s, B(x, 38, 32), NULL);
        else if (B(x, 37, 33) == 15) {
            if (!s->has_lcntr) { fail(s, E_ATTR, "'Sim' object has no attribute 'lcntr_next'"); return TGT_NONE; }
            cnt = s->lcntr;
        } else { has = 0; cnt = 0; }
        if (s->err) return TGT_NONE;
        LoopE e;
        if (has) {
            if (cnt == 0) { fail(s, E_NOTIMPL, "LCNTR = 0 @ %#llx", (long long)s->pc); return TGT_NONE; }
            e = (LoopE){nxt, t, cnt, 1, -1, 0};
        } else e = (LoopE){nxt, t, 0, 0, B(x, 37, 33), 0};
        *loop_push(s) = e;
        return TGT_NONE;
    }
    case T_21a: case T_21c:
        return TGT_NONE;                                 /* (IDLE is SLOW: Python raises Stop) */
    case T_6a_mem:
        if (cond_both(s, B(x, 37, 33))) {
            int i = B(x, 43, 41), m = B(x, 40, 38), g = B(x, 32, 32), d = B(x, 31, 31), dx = B(x, 30, 27), r = B(x, 26, 23);
            u32 prex[16], prey[16];
            memcpy(prex, s->px.R, 64); memcpy(prey, s->py.R, 64);
            shift_imm(s, comp, dx);
            int ii = i + (g ? 8 : 0), mm = m + (g ? 8 : 0);
            u32 a = dag_ea(s, ii, mm, 0, 4);
            pairmove(s, a, r, d, prex, prey, ii);
        }
        return TGT_NONE;
    case T_14a: {
        int d = B(x, 40, 40), l = B(x, 39, 39), ur = B(x, 38, 32);
        u32 addr = (u32)B(x, 31, 0);
        dreg_access(s, addr, -1, d, 4, 0, l, ur);
        return TGT_NONE;
    }
    case T_14d: {
        int d = B(x, 40, 40), l = B(x, 38, 38), w = B(x, 37, 37), xx = B(x, 36, 36), r = B(x, 35, 32);
        u32 addr = (u32)B(x, 31, 0);
        if (w) { dreg_access(s, addr, r, d, 4, 0, l, -1); return TGT_NONE; }
        dreg_access(s, addr, r, d, l ? 2 : 1, xx && !d, 0, -1);
        return TGT_NONE;
    }
    case T_1a: case T_1b: {
        u32 prex[16], prey[16]; const u32 *px_, *py_;
        int cp = ins->type == T_1a && comp;
        if (cp) { memcpy(prex, s->px.R, 64); memcpy(prey, s->py.R, 64); px_ = prex; py_ = prey; }
        else { px_ = s->px.R; py_ = s->py.R; }
        int dmd = B(x, 44, 44), dmi = B(x, 43, 41), dmm = B(x, 40, 38), pmd = B(x, 37, 37), dmr = B(x, 36, 33);
        int pmi = B(x, 32, 30), pmm = B(x, 29, 27), pmr = B(x, 26, 23);
        if (cp) compute(s, comp);
        u32 a1 = dag_ea(s, dmi, dmm, 0, 4);
        u32 a2 = dag_ea(s, pmi + 8, pmm + 8, 0, 4);
        pairmove(s, a1, dmr, dmd, px_, py_, dmi);
        pairmove(s, a2, pmr, pmd, px_, py_, pmi + 8);
        return TGT_NONE;
    }
    case T_5a_swap: case T_5b_swap: {
        int ok = cond_both(s, B(x, 37, 33));
        if (ins->type == T_5a_swap && comp && ok) compute(s, comp);
        if (ok) {
            int r = B(x, 26, 23), q = B(x, 41, 38);
            u32 t = s->px.R[r]; s->px.R[r] = s->py.R[q]; s->py.R[q] = t;
        }
        return TGT_NONE;
    }
    case T_20a:
        if (B(x, 39, 39)) *loop_push(s) = (LoopE){0, 0, 0, 1, -1, 1};
        if (B(x, 38, 38)) { if (s->nl) s->nl--; }
        if (B(x, 37, 37)) {
            if (s->ns == s->cs) { s->cs = s->cs * 2 + 8; s->sst = realloc(s->sst, s->cs * sizeof(StE)); }
            s->sst[s->ns++] = (StE){s->px.ast, s->py.ast, MODE1};
        }
        if (B(x, 36, 36)) {
            if (s->ns) { StE e = s->sst[--s->ns]; s->px.ast = e.ax; s->py.ast = e.ay; MODE1 = e.m1; }
        }
        if (B(x, 35, 35)) pc_push(s, 0);
        if (B(x, 34, 34)) { if (s->npc) s->npc--; }
        return TGT_NONE;
    case T_11a: case T_11c: {
        int cnd = B(x, 37, 33);
        int e = ins->type == T_11a ? B(x, 25, 25) : 0;
        int take = cond_both(s, cnd);
        if (ins->type == T_11a && comp) branch_compute(s, cnd, comp, e);
        if (take) {
            if (!s->npc) { fail(s, E_STOP, "RTS with empty PC stack @ %#llx", (long long)s->pc); return TGT_NONE; }
            *tgt = s->pcstk[--s->npc];
            return (ins->flags & F_DB) ? TGT_DELAYED : TGT_NOW;
        }
        return TGT_NONE;
    }
    }
    fail(s, E_NOTIMPL, "exec type %d @ %#llx", ins->type, (long long)s->pc);
    return TGT_NONE;
}

/* one sim.Sim.step(); RC_OK, or RC_DECODE / RC_SLOW before executing, or RC_ERR after */
static inline int step(Sim *s) {
    i64 pc = s->pc;
    Uop *ins = uop_at(s, pc);
    if (!ins) { s->need_pc = pc; return RC_DECODE; }
    if (ins->flags & F_SLOW) return RC_SLOW;
    if (s->has_brk && (s->brk[pc >> 3] >> (pc & 7) & 1)) return RC_SLOW;
    i64 nxt = pc + ins->size, t = 0;
    s->icount++;
    int k = exec_insn(s, ins, nxt, &t);
    if (s->err) return RC_ERR;
    if (s->pend) {
        if (--s->pend_k == 0) { s->pend = 0; s->pc = s->pend_t; }
        else s->pc = nxt;
        return RC_OK;
    }
    if (k == TGT_DELAYED) { s->pend = 1; s->pend_t = t; s->pend_k = 2; s->pc = nxt; return RC_OK; }
    if (k == TGT_NOW) { s->pc = t; return RC_OK; }
    while (s->nl && pc == s->lst[s->nl - 1].end) {
        LoopE *top = &s->lst[s->nl - 1];
        if (top->cond < 0) {
            top->count -= 1;
            if (top->count > 0) { s->pc = top->start; return RC_OK; }
            s->nl--;
        } else {
            if (top->cond == 31) { s->pc = top->start; return RC_OK; }
            int c = cond(s, top->cond, NULL);
            if (s->err) return RC_ERR;
            if (c) s->nl--;
            else { s->pc = top->start; return RC_OK; }
        }
    }
    s->pc = nxt;
    return RC_OK;
}

/* ------------------------------------------------------------------ API */
typedef struct {
    u32 R[2][16]; u32 ast[2], sticky[2];
    u64 mr_lo[2][2]; i64 mr_hi[2][2];               /* [pe][MRF, MRB] */
    u64 bff[2]; i64 bffwrp[2]; int has_bff[2], has_bffwrp[2];
    u32 I[16], M[16], L[16], Bx[16];
    u32 sysv[32]; unsigned char sysp[32];
    u32 PX1, PX2;
    i64 pc, icount, lcntr; int has_lcntr;
    int npc, nl, ns;
    int pend, pend_k; i64 pend_t;
    u64 misaligned;
    i64 until[8]; int nuntil; i64 stopat[16]; int nstop;
} SyncRegs;

void simc_get(Sim *s, SyncRegs *o) {
    for (int y = 0; y < 2; y++) {
        PE *pe = y ? &s->py : &s->px;
        memcpy(o->R[y], pe->R, 64); o->ast[y] = pe->ast; o->sticky[y] = pe->sticky;
        o->mr_lo[y][0] = (u64)pe->MRF; o->mr_hi[y][0] = (i64)(pe->MRF >> 64);
        o->mr_lo[y][1] = (u64)pe->MRB; o->mr_hi[y][1] = (i64)(pe->MRB >> 64);
        o->bff[y] = pe->bff; o->bffwrp[y] = pe->bffwrp; o->has_bff[y] = pe->has_bff; o->has_bffwrp[y] = pe->has_bffwrp;
    }
    memcpy(o->I, s->I, 64); memcpy(o->M, s->M, 64); memcpy(o->L, s->L, 64); memcpy(o->Bx, s->Bx, 64);
    memcpy(o->sysv, s->sysv, sizeof s->sysv); memcpy(o->sysp, s->sysp, sizeof s->sysp);
    o->PX1 = s->PX1; o->PX2 = s->PX2; o->pc = s->pc; o->icount = s->icount; o->lcntr = s->lcntr; o->has_lcntr = s->has_lcntr;
    o->npc = s->npc; o->nl = s->nl; o->ns = s->ns; o->pend = s->pend; o->pend_k = s->pend_k; o->pend_t = s->pend_t;
    o->misaligned = s->misaligned;
}

void simc_put(Sim *s, const SyncRegs *o) {
    for (int y = 0; y < 2; y++) {
        PE *pe = y ? &s->py : &s->px;
        memcpy(pe->R, o->R[y], 64); pe->ast = o->ast[y]; pe->sticky = o->sticky[y];
        pe->MRF = ((i128)o->mr_hi[y][0] << 64) | (i128)o->mr_lo[y][0];
        pe->MRB = ((i128)o->mr_hi[y][1] << 64) | (i128)o->mr_lo[y][1];
        pe->bff = o->bff[y]; pe->bffwrp = o->bffwrp[y]; pe->has_bff = o->has_bff[y]; pe->has_bffwrp = o->has_bffwrp[y];
    }
    memcpy(s->I, o->I, 64); memcpy(s->M, o->M, 64); memcpy(s->L, o->L, 64); memcpy(s->Bx, o->Bx, 64);
    memcpy(s->sysv, o->sysv, sizeof s->sysv); memcpy(s->sysp, o->sysp, sizeof s->sysp);
    s->PX1 = o->PX1; s->PX2 = o->PX2; s->pc = o->pc; s->icount = o->icount; s->lcntr = o->lcntr; s->has_lcntr = o->has_lcntr;
    s->npc = o->npc; s->nl = o->nl; s->ns = o->ns; s->pend = o->pend; s->pend_k = o->pend_k; s->pend_t = o->pend_t;
    s->misaligned = o->misaligned;
    memcpy(s->until, o->until, sizeof s->until); s->nuntil = o->nuntil;
    memcpy(s->stopat, o->stopat, sizeof s->stopat); s->nstop = o->nstop;
}
i64 *simc_pcstk(Sim *s) { return s->pcstk; }
LoopE *simc_loops(Sim *s) { return s->lst; }
StE *simc_ststk(Sim *s) { return s->sst; }
const char *simc_errmsg(Sim *s) { return s->errmsg; }
int simc_errcode(Sim *s) { return s->err; }
i64 simc_need_pc(Sim *s) { return s->need_pc; }
i64 simc_n_done(Sim *s) { return s->n_done; }
int simc_ntypes(void) { return T_UNKNOWN; }
void simc_brk_unset(Sim *s, i64 pc) { if (s->brk && (u64)pc < (1u << 24)) s->brk[pc >> 3] &= ~(1 << (pc & 7)); }
void simc_brk_on(Sim *s, int on) { s->has_brk = on && s->brk; }
void simc_brk_set(Sim *s, i64 pc) {
    if ((u64)pc >= (1u << 24)) return;
    if (!s->brk) s->brk = calloc(1 << 21, 1);
    s->brk[pc >> 3] |= 1 << (pc & 7); s->has_brk = 1;
}
void simc_wlog(Sim *s, int on) { s->wlog_on = on; s->nw = 0; }
u32 *simc_wlog_buf(Sim *s, i64 *n) { *n = s->nw; return s->wlog; }
void simc_wlog_clear(Sim *s) { s->nw = 0; }

Sim *simc_new(void *handle) { Sim *s = calloc(1, sizeof(Sim)); s->handle = handle; s->ea_i = -1; return s; }

void simc_mem_reset(Sim *s) {
    for (int h = 0; h < 1024; h++) if (s->pt[h]) {
        for (int m = 0; m < 1024; m++) free(s->pt[h][m]);
        free(s->pt[h]); s->pt[h] = NULL;
    }
}

void simc_free(Sim *s) {
    simc_mem_reset(s);
    for (int h = 0; h < 1024; h++) if (s->it[h]) { for (int m = 0; m < 1024; m++) free(s->it[h][m]); free(s->it[h]); }
    for (int k = 0; k < 4096; k++) free(s->code[k]);
    free(s->pcstk); free(s->lst); free(s->sst); free(s->brk); free(s->wlog); free(s);
}

void simc_set_uop(Sim *s, i64 pc, int type, u64 x, i64 target, int size, int flags, int asz, int asx, int alw) {
    if (!s->code[pc >> 12]) s->code[pc >> 12] = calloc(4096, sizeof(Uop));
    Uop *u = &s->code[pc >> 12][pc & 4095];
    u->x = x; u->target = target; u->type = type; u->size = size; u->flags = flags; u->asz = asz; u->asx = asx; u->alw = alw;
}
void simc_clear_uops(Sim *s) { for (int k = 0; k < 4096; k++) { free(s->code[k]); s->code[k] = NULL; } }

/* raw physical words (4-aligned pa) for Python's Memory */
u32 simc_rw(Sim *s, u32 pa) { return rw(s, pa); }
void simc_ww(Sim *s, u32 pa, u32 v) { ww(s, pa, v); }
int simc_written(Sim *s, u32 pa, u32 *out) {
    Page **l2 = s->pt[pa >> 22]; if (!l2) return 0;
    Page *p = l2[(pa >> 12) & 1023]; if (!p) return 0;
    u32 k = (pa >> 2) & 1023;
    if (!(p->wr[k >> 5] >> (k & 31) & 1)) return 0;
    *out = p->d[k]; return 1;
}
void simc_unwrite(Sim *s, u32 pa) {                 /* del words[pa]: back to the image value */
    Page **l2 = s->pt[pa >> 22]; if (!l2) return;
    Page *p = l2[(pa >> 12) & 1023]; if (!p) return;
    u32 k = (pa >> 2) & 1023;
    p->wr[k >> 5] &= ~(1u << (k & 31));
    p->d[k] = img_page(s, pa)[k];
}
/* written words: count (keys == NULL) or fill keys / vals in address order */
i64 simc_dump(Sim *s, u32 *keys, u32 *vals) {
    i64 n = 0;
    for (u32 h = 0; h < 1024; h++) if (s->pt[h]) for (u32 m = 0; m < 1024; m++) {
        Page *p = s->pt[h][m]; if (!p) continue;
        for (u32 w = 0; w < 32; w++) { u32 bits = p->wr[w]; while (bits) {
            int b = __builtin_ctz(bits); bits &= bits - 1;
            u32 k = w * 32 + b;
            if (keys) { keys[n] = (h << 22) | (m << 12) | (k << 2); vals[n] = p->d[k]; }
            n++;
        } }
    }
    return n;
}
void simc_load(Sim *s, const u32 *keys, const u32 *vals, i64 n) { for (i64 k = 0; k < n; k++) ww(s, keys[k], vals[k]); }

u32 simc_mem_r(Sim *s, u32 a, int size, int sx) { return mem_r(s, a, size, sx); }
void simc_mem_w(Sim *s, u32 a, u32 v, int size) { mem_w(s, a, v, size); }

/* register file access for the Python side (sync) */
PE *simc_pe(Sim *s, int y) { return y ? &s->py : &s->px; }
void simc_pe_mr(Sim *s, int y, int which, u64 *lo, i64 *hi) {
    i128 v = which ? simc_pe(s, y)->MRB : simc_pe(s, y)->MRF;
    *lo = (u64)v; *hi = (i64)(v >> 64);
}
void simc_pe_set_mr(Sim *s, int y, int which, u64 lo, i64 hi) {
    i128 v = ((i128)hi << 64) | (i128)lo;
    if (which) simc_pe(s, y)->MRB = v; else simc_pe(s, y)->MRF = v;
}
void simc_loops_alloc(Sim *s, int n) {
    if (n > s->cl) { s->cl = n + 8; s->lst = realloc(s->lst, s->cl * sizeof(LoopE)); }
    if (n > s->cpc) { s->cpc = n + 16; s->pcstk = realloc(s->pcstk, s->cpc * sizeof(i64)); }
    if (n > s->cs) { s->cs = n + 8; s->sst = realloc(s->sst, s->cs * sizeof(StE)); }
}

int simc_run(Sim *s, i64 max_insns) {
    i64 n = 0;
    s->err = 0;
    for (;;) {
        if (!s->pend) {
            i64 pc = s->pc;
            for (int k = 0; k < s->nuntil; k++) if (s->until[k] == pc) { s->n_done = n; return RC_UNTIL; }
            for (int k = 0; k < s->nstop; k++) if (s->stopat[k] == pc) { s->n_done = n; return RC_STOP; }
        }
        int r = step(s);
        if (r != RC_OK) { if (r == RC_ERR) n++; s->n_done = n; return r; }
        if (++n >= max_insns) { s->n_done = n; return RC_MAX; }
    }
}
