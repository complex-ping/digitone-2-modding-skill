"""Content-keyed cache for dnfw's aPLib section compression (pure Python: ~20-50 s per section in CPython, ~3 s in
PyPy): a build whose section's bytes did not change reuses the compressed section ($DN2_WORKSPACE/build/.zcache/); misses
compress in parallel PyPy processes (CPython in-process if PyPy is missing).

Flash space is the binding limit (the OS upgrade buffer is 2 MiB): dnfw's packer is greedy with a one-step lazy look-ahead
over a 32-deep match chain; a 192-deep chain finds better matches within the same format and window (limits.PACK_MAX_OFFSET):
MAIN OS -0.8 %, section 7 -0.5 % (depth 1024 packs worse: the greedy parse). Same format, proven on hardware. Every section
round-trips through dnfw's depack (checked here on every miss)."""
import hashlib, inspect, os, pickle, shutil, subprocess, sys
from concurrent.futures import ProcessPoolExecutor
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import dn2paths as P; P.use_dnfw()
from dnfw.container.section import compress
from dnfw.codec import aplibpack

DIR = str(P.BUILD / '.zcache')
SRC = os.path.dirname(os.path.dirname(os.path.dirname(inspect.getfile(aplibpack))))     # the dnfw package's parent
CHAIN = 192                                           # aplibpack.pack's chain_depth (dnfw's default: 32)
_SRC = hashlib.sha256(inspect.getsource(aplibpack).encode()).hexdigest() + f'/chain{CHAIN}' 


def _key(sid, dest, data):
    return hashlib.sha256(repr((sid, dest, _SRC)).encode() + bytes(data)).hexdigest()[:32]


def _deep():                                          # (the in-process fallback: the same depth)
    from dnfw.codec import aplib
    if getattr(aplibpack.pack, '_deep', False):
        return
    p = aplibpack.pack

    def pack(d, **kw):
        z = p(d, chain_depth=CHAIN)
        assert aplib.depack(z) == bytes(d)
        return z
    pack._deep = True
    aplibpack.pack = pack


def compress_all(jobs):
    """jobs: {name: (section id, dest, bytes)} -> {name: compressed section} (dnfw's compress, cached)."""
    os.makedirs(DIR, exist_ok=True)
    out, todo = {}, {}
    for name, (sid, dest, data) in jobs.items():
        path = os.path.join(DIR, _key(sid, dest, data) + '.pkl')
        if os.path.exists(path):
            out[name] = pickle.load(open(path, 'rb'))
        else:
            todo[name] = (sid, dest, bytes(data), path)
    pypy = next((shutil.which(n) for n in ('pypy3', 'pypy3.11', 'pypy3.10') if shutil.which(n)), None)
    if todo and pypy:
        code = ('import pickle, sys, functools; from dnfw.codec import aplibpack, aplib; '
                f'_p = aplibpack.pack; aplibpack.pack = lambda d_: _chk(d_, _p(d_, chain_depth={CHAIN})); '
                'exec("def _chk(d_, z_):\\n    assert aplib.depack(z_) == d_\\n    return z_"); '
                'from dnfw.container.section import compress; '
                'd = open(sys.argv[3], "rb").read(); z = compress(int(sys.argv[1]), int(sys.argv[2]), d); '
                'pickle.dump(z, open(sys.argv[4] + ".tmp", "wb"), protocol=4)')
        procs = []
        for n, (sid, dest, data, path) in todo.items():
            open(path + '.in', 'wb').write(data)
            procs.append((n, path, subprocess.Popen([pypy, '-c', code, str(sid), str(dest), path + '.in', path],
                                                    env=dict(os.environ, PYTHONPATH=SRC))))
        for n, path, pr in procs:
            if pr.wait() == 0:
                os.replace(path + '.tmp', path)
                out[n] = pickle.load(open(path, 'rb'))
                del todo[n]
            os.remove(path + '.in')
    if todo:
        _deep()
        with ProcessPoolExecutor(len(todo)) as ex:
            futs = {n: ex.submit(compress, sid, dest, data) for n, (sid, dest, data, _) in todo.items()}
            for n, f in futs.items():
                out[n] = f.result()
                path = todo[n][3]
                pickle.dump(out[n], open(path + '.tmp', 'wb')); os.replace(path + '.tmp', path)
    return out
