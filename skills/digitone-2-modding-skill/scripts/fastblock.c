/* SPDX-License-Identifier: GPL-2.0  (native re-implementation of parts of digikit, github.com/m-dwyer/digikit) */
/* digikit's _FastStepper._on_block, in C: bound a Unicorn run by counting basic-block entries.
 *
 * Same semantics as emu/longrun.py's Python hook (checked before counting: the block that exhausts the budget runs
 * and is counted, the one after it is neither), without a Python callback per block -- that callback was ~5.4M
 * calls per 21M instructions and most of the wall time (sharcdec/cf/fastemu.py has the measurements).
 * Built by fastemu.py with the system clang; registered straight through uc_hook_add.
 *
 * `sp_above` (fastemu.run_isr): an interrupt handler run ends at the first block entered with the stack pointer back
 * at the interrupted context's -- its RTE, or a task switch out of it. Before, harnesses ran a raised vector "until
 * PC == the interrupted PC", which a task switch or a mid-block PC never reaches: every frame then spun the idle
 * loop (a Python hook per spin) to its instruction limit -- 95 % of a sequencer test's wall time.
 */
#include <stdint.h>

typedef int (*stop_fn)(void *uc);
typedef int (*reg_read_fn)(void *uc, int regid, void *value);

typedef struct {
    int64_t left;          /* block entries still allowed in this step */
    uint64_t blocks;       /* entries counted in this step */
    uint64_t total;        /* entries since creation */
    stop_fn stop;          /* uc_emu_stop, from the same libunicorn */
    reg_read_fn reg_read;  /* uc_reg_read */
    int32_t sp_reg;        /* UC_M68K_REG_A7 */
    uint32_t sp_above;     /* != 0: stop at the first block entered with SP >= this (an interrupt has returned) */
    uint64_t sp_stops;     /* how often that ended a run */
    uint32_t idle_hit;     /* the run stopped at an idle `bra *` */
    uint32_t pad;
    uint64_t idle_stops;
} fast_state;

/* On each idle spin (`bra *`, digikit dspboot.find_idle_spins): the CPU waits for an interrupt and does nothing else,
 * so the run stops here and fastemu counts the rest of the step as idle time (the timers then fire on schedule).
 * Replaces digikit's Python do_halt, called once per spin -- 1.3M Python calls per second of guest idle. */
void fast_idle(void *uc, uint64_t address, uint32_t size, void *user)
{
    fast_state *s = (fast_state *)user;

    (void)address;
    (void)size;
    s->idle_hit = 1;
    s->idle_stops++;
    s->stop(uc);
}

void fast_block(void *uc, uint64_t address, uint32_t size, void *user)
{
    fast_state *s = (fast_state *)user;

    (void)address;
    (void)size;
    if (s->left <= 0) {
        s->stop(uc);
        return;
    }
    if (s->sp_above) {
        uint32_t sp = 0;

        s->reg_read(uc, s->sp_reg, &sp);
        if (sp >= s->sp_above) {
            s->sp_above = 0;
            s->sp_stops++;
            s->stop(uc);
            return;
        }
    }
    s->blocks++;
    s->total++;
    s->left--;
}

/* digikit emu/hle.py's Bitmap::setPixel / getPixel HLE in C (same semantics, see its docstring): 2.6M Python calls
 * on the way to the main screen, and one per pixel of every redraw after it. Hooked at the routines' entries; each
 * does the pixel, then returns for the routine (SP += 4, PC = the return address). */
typedef int (*reg_write_fn)(void *uc, int regid, const void *value);
typedef int (*mem_read_fn)(void *uc, uint64_t address, void *bytes, uint64_t size);
typedef int (*mem_write_fn)(void *uc, uint64_t address, const void *bytes, uint64_t size);

typedef struct {
    reg_read_fn reg_read;
    reg_write_fn reg_write;
    mem_read_fn mem_read;
    mem_write_fn mem_write;
    int32_t r_a7, r_d0, r_pc, pad;
    uint64_t sets, gets;
} pixel_state;

static uint32_t be32(const uint8_t *p)
{
    return (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 | (uint32_t)p[2] << 8 | p[3];
}

static void put_be32(uint8_t *p, uint32_t v)
{
    p[0] = v >> 24; p[1] = v >> 16; p[2] = v >> 8; p[3] = v;
}

/* -> the word's address for (x, y) in bmp, or 0 when outside (signed bounds, as the routines) */
static uint32_t pixel_at(pixel_state *s, void *uc, uint32_t bmp, int32_t x, int32_t y)
{
    uint8_t h[16];
    int32_t w, hh, stride;

    s->mem_read(uc, bmp + 4, h, 16);
    w = (int32_t)be32(h); hh = (int32_t)be32(h + 4); stride = (int32_t)be32(h + 8);
    if (x < 0 || y < 0 || x >= w || y >= hh)
        return 0;
    return be32(h + 12) + (uint32_t)((x * stride) + (y >> 5)) * 4u;
}

static void ret_from(pixel_state *s, void *uc, uint32_t sp, uint32_t ret)
{
    uint32_t v = sp + 4;

    s->reg_write(uc, s->r_a7, &v);
    s->reg_write(uc, s->r_pc, &ret);
}

void fast_set_pixel(void *uc, uint64_t address, uint32_t size, void *user)
{
    pixel_state *s = (pixel_state *)user;
    uint32_t sp = 0, off;
    uint8_t a[20], wbuf[4];

    (void)address; (void)size;
    s->reg_read(uc, s->r_a7, &sp);
    s->mem_read(uc, sp, a, 20);                 /* ret, bmp, x, y, val */
    off = pixel_at(s, uc, be32(a + 4), (int32_t)be32(a + 8), (int32_t)be32(a + 12));
    if (off) {
        uint32_t word, mask = 0x80000000u >> ((int32_t)be32(a + 12) & 31);

        s->mem_read(uc, off, wbuf, 4);
        word = be32(wbuf);
        word = (be32(a + 16) & 1) ? (word | mask) : (word & ~mask);
        put_be32(wbuf, word);
        s->mem_write(uc, off, wbuf, 4);
    }
    s->sets++;
    ret_from(s, uc, sp, be32(a));
}

void fast_get_pixel(void *uc, uint64_t address, uint32_t size, void *user)
{
    pixel_state *s = (pixel_state *)user;
    uint32_t sp = 0, off, out = 0;
    uint8_t a[16], wbuf[4];

    (void)address; (void)size;
    s->reg_read(uc, s->r_a7, &sp);
    s->mem_read(uc, sp, a, 16);                 /* ret, bmp, x, y */
    off = pixel_at(s, uc, be32(a + 4), (int32_t)be32(a + 8), (int32_t)be32(a + 12));
    if (off) {
        s->mem_read(uc, off, wbuf, 4);
        out = be32(wbuf) & (0x80000000u >> ((int32_t)be32(a + 12) & 31));
    }
    s->reg_write(uc, s->r_d0, &out);
    s->gets++;
    ret_from(s, uc, sp, be32(a));
}
