#!/bin/bash
# Stock ColdFire section 3 lookups from the cached full disassembly ($DN2_WORKSPACE/extracted/section_3.dis; made once).
#   cfx.sh refs ADDR          every instruction that names ADDR (callers, jumps, lea/pea, data refs)
#   cfx.sh at ADDR [B] [A]    B instructions before and A after ADDR (default 30 / 40)
#   cfx.sh fn ADDR            the instructions from ADDR to its first rts
WS=${DN2_WORKSPACE:-$HOME/dn2-workspace}
D=$WS/extracted/section_3.dis
[ -s "$D" ] || "$(dirname "$0")/cfdis.sh" 0x40000400 0x402e0000 > "$D"
a=$(printf '%08x' $(( $2 )))
case "$1" in
  refs) grep -n "0x$a\b\|${a:0:4} ${a:4:4}" "$D" | grep -v ":$a:" ;;
  at)   L=$(grep -n "^$a:" "$D" | cut -d: -f1); [ -z "$L" ] && { echo "no instruction starts at 0x$a"; exit 1; }
        sed -n "$((L - ${3:-30})),$((L + ${4:-40}))p" "$D" ;;
  fn)   L=$(grep -n "^$a:" "$D" | cut -d: -f1); [ -z "$L" ] && { echo "no instruction starts at 0x$a"; exit 1; }
        tail -n +$L "$D" | awk '{print} /\trts/{exit}' ;;
  *)    sed -n 2,5p "$0"; exit 1 ;;
esac
