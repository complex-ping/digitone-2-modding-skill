"""Fixes for digikit's ColdFire emulation (Unicorn 2.1.4 + digikit patches) needed for faithful DN2 1.11 frames.

EMAC fractional mode: the patched Unicorn omits the x2 product alignment of fractional MAC (MACSR.F/I = 0x20) for
word AND long operands (unit test: sharcdec/cf/emactest.py). The OS smooths every sound/FX parameter each frame with
the EMAC one-pole at 0x400db160 (s = 0x03d7/2^15 * target + 0x7c29/2^15 * s, state Q31 at 0x8000de60, output
rounded s>>16 into the mirror 0x800068e4, which is memcpy'd into the SHARC frame); under the bug every value settles
at 2.91 % of its target. install_emac_smoother() runs that loop natively (bit-exact ColdFire EMAC fractional
arithmetic, truncating) and resumes at 0x400db188.
"""
import struct
from unicorn import UC_HOOK_CODE
from unicorn.m68k_const import (UC_M68K_REG_A0, UC_M68K_REG_A1, UC_M68K_REG_A2, UC_M68K_REG_D1, UC_M68K_REG_D3,
                                UC_M68K_REG_PC)

LOOP, AFTER = 0x400DB160, 0x400DB188
STATE, MIRROR = 0x8000DE60, 0x800068E4
A_COEF, B_COEF = 0x03D7, 0x7C290000                     # d6.u (Q15) and d5 (Q31)


def _s32(v):
    return v - (1 << 32) if v & 0x80000000 else v


def _s16(v):
    return v - 0x10000 if v & 0x8000 else v


def install_emac_smoother(uc):
    def hook(uc_, addr, size, user):
        a0 = uc.reg_read(UC_M68K_REG_A0)                # points one long past the first target pair (d0 preloaded)
        a1 = uc.reg_read(UC_M68K_REG_A1)
        n = uc.reg_read(UC_M68K_REG_D3) // 2            # pairs (d3 = 1734 values)
        tg = bytes(uc.mem_read(a0 - 4, 4 * n))
        st = bytes(uc.mem_read(a1, 8 * n))
        new_st, out = bytearray(8 * n), bytearray(4 * n)
        for k in range(n):
            xh, xl = struct.unpack_from('>HH', tg, 4 * k)
            s0, s1 = struct.unpack_from('>ii', st, 8 * k)
            r = []
            for x, s in ((_s16(xh), s0), (_s16(xl), s1)):
                v = ((2 * A_COEF * x) << 0) + ((B_COEF * s) >> 31)      # frac: word product <<1, long product >>31
                v = _s32(v & 0xFFFFFFFF)
                r.append(v)
            struct.pack_into('>ii', new_st, 8 * k, r[0], r[1])
            hi = ((r[0] + 0x8000) >> 16) & 0xFFFF
            lo = ((r[1] + 0x8000) >> 16) & 0xFFFF
            struct.pack_into('>HH', out, 4 * k, hi, lo)
        uc.mem_write(a1, bytes(new_st))
        uc.mem_write(MIRROR, bytes(out[:4 * (n - 1)]))  # the last pair is stored by the instruction at AFTER
        uc.reg_write(UC_M68K_REG_D1, struct.unpack_from('>I', out, 4 * (n - 1))[0])
        uc.reg_write(UC_M68K_REG_A2, MIRROR + 4 * (n - 1))
        uc.reg_write(UC_M68K_REG_A0, a0 + 4 * n)
        uc.reg_write(UC_M68K_REG_A1, a1 + 8 * n)
        uc.reg_write(UC_M68K_REG_D3, 0)
        uc.reg_write(UC_M68K_REG_PC, AFTER)
    return uc.hook_add(UC_HOOK_CODE, hook, begin=LOOP, end=LOOP)
