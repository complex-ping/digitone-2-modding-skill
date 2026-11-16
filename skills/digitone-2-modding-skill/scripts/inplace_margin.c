/* worst in-place margin of an Elektron aPLib stream: min over tokens of gap + read_pos - write_end. usage: margin FILE SKIP GAP */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
static const uint8_t *d; static size_t n, pos; static uint32_t tag;
static int byte(void) { return pos < n ? d[pos++] : 0; }
static int bit(void) { tag <<= 1; if (!(tag & 0xff)) { int v = byte(); tag = (v << 1) | 1; return (v >> 7) & 1; } return (tag >> 8) & 1; }
static uint32_t gam(void) { uint32_t v = 1; do { v = (v << 1) + bit(); } while (!bit() && v <= 0x2000000); return v; }
int main(int c, char **a) {
    FILE *f = fopen(a[1], "rb"); fseek(f, 0, 2); size_t len = ftell(f); rewind(f);
    uint8_t *buf = malloc(len); fread(buf, 1, len, f); fclose(f);
    size_t skip = atoi(a[2]); long gap = atol(a[3]); d = buf + skip; n = len - skip;
    long produced = 0, worst = 1L << 40, wat = 0; uint32_t last = 1;
    for (;;) {
        long cnt;
        if (bit()) { pos++; cnt = 1; }
        else {
            uint32_t g = gam(), off;
            if (g == 2) off = last;
            else { off = (g << 8) + byte(); if (off == 767) break; off -= 767; last = off; }
            int s = 2 * bit(); s += bit();
            uint32_t ln = s ? s : gam() + 2; if (off > 3328) ln++;
            cnt = ln + 1;
        }
        long m = gap + (long)pos - (produced + cnt);
        if (m < worst) { worst = m; wat = produced; }
        produced += cnt;
        if (pos > n) { puts("overrun"); return 1; }
    }
    printf("out %ld in %zu worst %ld at out %ld\n", produced, pos, worst, wat);
}
