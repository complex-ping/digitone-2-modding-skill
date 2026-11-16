/* Optimal-parse packer for Elektron's aPLib variant (the stream dnfw codec/aplib.depack reads), after the idea in
 * mischa85/elektron-firmware-tool (cost-optimal forward DP). See references/setup.md "Packer headroom".
 * Forward DP over positions; state per position = (cost, last offset of that path).
 * usage: optpack IN OUT [chain] */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>

#define BIAS 767u
#define FAR 3328u
#define MINM 2u
#define MAXM 2048u
#define MAXOFF 1044480u
#define HS (1 << 16)
#define INF 0xFFFFFFFFu

static int gc(uint32_t v) { int n = 0; for (; v; v >>= 1) n++; return 2 * (n - 1); }
static int mcost(uint32_t off, uint32_t L, uint32_t lo) {
    int b = 1 + (off == lo ? 2 : gc((off + BIAS) >> 8) + 8);
    uint32_t base = L - 1 - (off > FAR);
    return b + (base <= 3 ? 2 : 2 + gc(base - 2));
}

static uint8_t *out; static size_t on, tagp; static int tagb;
static void bit(int v) { if (!tagb) { tagp = on; out[on++] = 0; tagb = 8; } if (v) out[tagp] |= 1 << (tagb - 1); tagb--; }
static void byte(uint8_t v) { out[on++] = v; }
static void gam(uint32_t v) { int w = 0; for (uint32_t t = v; t; t >>= 1) w++; for (int i = w - 2; i >= 0; i--) { bit((v >> i) & 1); bit(i == 0); } }

int main(int argc, char **argv) {
    FILE *f = fopen(argv[1], "rb"); fseek(f, 0, 2); size_t n = ftell(f); rewind(f);
    uint8_t *d = malloc(n + 8); fread(d, 1, n, f); fclose(f);
    int chain = argc > 3 ? atoi(argv[3]) : 256;
    uint32_t *cost = malloc((n + 1) * 4), *lo = malloc((n + 1) * 4), *toff = malloc((n + 1) * 4), *tlen = malloc((n + 1) * 4);
    int32_t *from = malloc((n + 1) * 4), *prev = malloc(n * 4), *head = malloc(HS * 4);
    for (size_t i = 0; i <= n; i++) cost[i] = INF;
    for (int i = 0; i < HS; i++) head[i] = -1;
    cost[0] = 0; lo[0] = 1;
#define RELAX(p, c, l, fr, o, ln) do { uint32_t c_ = (c); if (c_ < cost[p]) { cost[p] = c_; lo[p] = (l); from[p] = (fr); toff[p] = (o); tlen[p] = (ln); } } while (0)
    for (size_t i = 0; i < n; i++) {
        uint32_t bi = cost[i], loi = lo[i], cap = n - i > MAXM ? MAXM : (uint32_t)(n - i);
        RELAX(i + 1, bi + 9, loi, (int32_t)i, 0, 0);
        if (i + 1 < n) {
            if (loi <= i && loi <= MAXOFF) {
                uint32_t r = 0; while (r < cap && d[i - loi + r] == d[i + r]) r++;
                for (uint32_t L = (loi > FAR ? MINM + 1 : MINM); L <= r; L++) RELAX(i + L, bi + mcost(loi, L, loi), loi, (int32_t)i, loi, L);
            }
            int32_t j = head[(d[i] << 8) | d[i + 1]]; int c = chain; uint32_t pm = 1;
            while (j >= 0 && c--) {
                uint32_t off = (uint32_t)(i - j);
                if (off > MAXOFF || pm >= cap) break;
                if (d[j + pm] == d[i + pm]) {
                    uint32_t l = 0; while (l < cap && d[j + l] == d[i + l]) l++;
                    if (l > pm) {
                        uint32_t mn = off > FAR ? MINM + 1 : MINM, s = pm + 1 > mn ? pm + 1 : mn;
                        for (uint32_t L = s; L <= l; L++) RELAX(i + L, bi + mcost(off, L, loi), off, (int32_t)i, off, L);
                        pm = l;
                    }
                }
                j = prev[j];
            }
            uint32_t h = (d[i] << 8) | d[i + 1]; prev[i] = head[h]; head[h] = (int32_t)i;
        }
    }
    size_t np = 0; for (int32_t j = n; j > 0; j = from[j]) np++;
    uint32_t *path = malloc(np * 4); size_t k = np; for (int32_t j = n; j > 0; j = from[j]) path[--k] = j;
    out = malloc(n * 2 + 64);
    uint32_t last = 1;
    for (k = 0; k < np; k++) {
        uint32_t p = path[k]; int32_t s = from[p];
        if (!tlen[p]) { bit(1); byte(d[s]); continue; }
        uint32_t off = toff[p], L = tlen[p];
        bit(0);
        if (off == last) gam(2); else { uint32_t raw = off + BIAS; gam(raw >> 8); byte(raw & 0xff); last = off; }
        uint32_t base = L - 1 - (off > FAR);
        if (base <= 3) { bit(base >> 1); bit(base & 1); } else { bit(0); bit(0); gam(base - 2); }
    }
    bit(0); gam(0x1000002); byte(0xff);
    f = fopen(argv[2], "wb"); fwrite(out, 1, on, f); fclose(f);
    printf("%s: %zu -> %zu (chain %d)\n", argv[1], n, on, chain);
    return 0;
}
