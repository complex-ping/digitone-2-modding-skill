/* double_time: the sequencer runs at twice the tempo shown (a minimal ColdFire C mod).
 *
 * The audio ISR advances the sequencer clock once per DSP block (~1500 / s): at 0x40027ac0 it loads the tempo word
 * (BPM x 120 = half-units per block) into d0, adds 2 x d0 to "now" (0x80005398) and takes it off the tick countdown
 * (0x800053a0). hooks.S puts `jsr dt_stub` over that load; the stub calls dt_step(), whose result is the new d0.
 * Everything on the sequencer clock follows: steps, micro timing, retrigs, note lengths, arpeggiator ticks.
 */
typedef unsigned long u32;

#define TEMPO_WORD (*(volatile u32 *)0x402a0dec)

u32 dt_blocks;                        /* how often the hook ran (read it in the emulator: proves the hook is live) */

void dt_init(void)                    /* called once at boot by dnfw's loader, after the code is copied */
{
    dt_blocks = 0;
}

u32 dt_step(void)
{
    u32 d = TEMPO_WORD * 2;
    dt_blocks++;
    if (d > 112000)                   /* stock fires at most one 96 ppqn subtick (225,000 units) per block: stay under */
        d = 112000;
    return d;
}
