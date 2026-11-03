#!/usr/bin/env python3
"""What keeps this repo honest without any firmware: skill frontmatter and size, routing, named tools exist, no Elektron data, every script compiles."""
import pathlib, py_compile, re, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKILL = ROOT / 'skills/digitone-2-modding-skill'
bad = []
text = (SKILL / 'SKILL.md').read_text()
m = re.match(r'---\nname: (\S+)\ndescription: (.*?)\n---\n', text, re.S)
if not m or m.group(1) != SKILL.name:
    bad.append('SKILL.md: frontmatter name must match its folder')
elif len(m.group(2)) > 1024:
    bad.append('SKILL.md: description over 1024 characters')
if len(text.encode()) > 12000:
    bad.append('SKILL.md: over 12 KB, move detail to a reference')
for f in sorted((SKILL / 'references').glob('*.md')):
    if f'references/{f.name}' not in text:
        bad.append(f'{f.name}: not routed from SKILL.md')
docs = text + ''.join(f.read_text() for f in (SKILL / 'references').glob('*.md'))
for tool in sorted(set(re.findall(r'`scripts/([\w-]+\.(?:py|sh|c))\b', docs))):
    if not (SKILL / 'scripts' / tool).exists():
        bad.append(f'scripts/{tool}: named in the docs, not shipped')
tracked = subprocess.run(['git', 'ls-files'], cwd=ROOT, capture_output=True, text=True).stdout.split()
for t in tracked:
    if re.search(r'\.(syx|bin|snap|snap\.gz|dis|sqlite|hex)$', t):
        bad.append(f'{t}: firmware-derived file tracked')
    p = ROOT / t
    if p.is_file() and p.stat().st_size > 400_000:
        bad.append(f'{t}: {p.stat().st_size:,} B, check this is not firmware data')
    if p.suffix in ('.md', '.py', '.sh') and p.name != 'skill_check.py' and re.search(r'/Users/[a-z]|/home/[a-z]', p.read_text(errors='replace')):
        bad.append(f'{t}: personal path')
with tempfile.TemporaryDirectory() as d:
    for f in sorted(SKILL.rglob('*.py')) + sorted((ROOT / 'tests').glob('*.py')):
        try:
            py_compile.compile(str(f), cfile=f'{d}/x.pyc', doraise=True)
        except py_compile.PyCompileError as e:
            bad.append(f'{f.name}: {e}')
for b in bad:
    print('FAIL', b)
print(f'{len(tracked)} files checked, {len(bad)} problems')
sys.exit(1 if bad else 0)
