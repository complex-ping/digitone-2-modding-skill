#!/bin/bash
# ColdFire V4e disassembly of your stock DN2 1.11 section 3 (MAIN OS, base 0x40000400), or of another section 3 file.
# usage: cfdis.sh START END [SECTION_3.bin]      e.g. cfdis.sh 0x40037b80 0x40037c50
# objdump: m68k-linux-gnu-objdump (or m68k-elf-objdump) on PATH or in $DN2_M68K_BIN. It is the only correct ColdFire V4e
# decoder we know of (-m m68k:cfv4e); other disassemblers mis-decode EMAC and some V4e forms.
WS=${DN2_WORKSPACE:-$HOME/dn2-workspace}
[ -n "$DN2_M68K_BIN" ] && PATH=$DN2_M68K_BIN:$PATH
[ -d "$WS/m68k/bin" ] && PATH=$WS/m68k/bin:$PATH
OBJDUMP=$(command -v m68k-linux-gnu-objdump || command -v m68k-elf-objdump) || { echo "no m68k objdump on PATH" >&2; exit 1; }
"$OBJDUMP" -D -b binary -m m68k:cfv4e --adjust-vma=0x40000400 \
    --start-address=$(($1)) --stop-address=$(($2)) "${3:-$WS/extracted/section_3.bin}" | tail -n +8
