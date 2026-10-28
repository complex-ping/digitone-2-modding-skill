/* jsr dt_stub replaces `move.l 0x402a0dec,%d0` (6 bytes) in the audio ISR at 0x40027ac0.
 * a0 ("now", loaded just before) and d1 are live there: keep them (and a1) across the C call; d0 = the result. */
    .text
    .globl  dt_stub
dt_stub:
    lea     -12(%sp),%sp
    movem.l %d1/%a0-%a1,(%sp)            /* ColdFire movem: (An) / d16(An) only, no -(sp) */
    jsr     dt_step
    movem.l (%sp),%d1/%a0-%a1
    lea     12(%sp),%sp
    rts
