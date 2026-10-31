"""The header `--header` writes compiles on its own in a C host's file."""
import subprocess
from pathlib import Path

import toolchain as tc


def check_export_header(exe, cc, base, *, runtime, extra=()):
    """Raises RuntimeError on a failure."""
    base = (Path(base) / "export-header").resolve()
    base.mkdir(parents=True, exist_ok=True)
    # Scalars of every width the alignment typedefs cover, a scalar slice,
    # and a slice result, whose length comes back through a pointer.
    tc.write_text(base / "lib.goose", '''
export fn add_i64(a: i64, b: i64) -> i64 { return a + b; }
export fn add_i32(a: i32, b: i32) -> i32 { return a + b; }
export fn half(x: f64) -> f64 { return x / 2.0; }
export fn total(xs: const i64[:]) -> i64 {
    var s: i64 = 0;
    for x in xs { s += x; }
    return s;
}
var out: u8[>..] = [];
export fn greet(n: i32) -> const u8[:] {
    out.clear();
    for i in 0..n { out.push((65 + i) as u8); }
    return out;
}
''')
    # The host includes nothing but the header, as an embedder's own file
    # does: every type the prototypes name must be declared there.
    tc.write_text(base / "host.c", '''
#include "lib.h"
#include <stdio.h>
int main(int argc, char **argv) {
    goose_init(argc, argv);
    int64_t xs[3] = {1, 2, 3};
    sl_i64 s = {xs, 3};
    int64_t len = 0;
    const uint8_t *g = greet(3, &len);
    printf("%lld %d %g %lld %.*s\\n", (long long)add_i64(40, 2), (int)add_i32(1, 2),
           half(5.0), (long long)total(s), (int)len, (const char *)g);
    return 0;
}
''')
    gen = subprocess.run([str(exe), "-o", "lib.c", "--header", "lib.h", "lib.goose"],
                         cwd=base, capture_output=True, text=True, timeout=120)
    if gen.returncode:
        raise RuntimeError(f"goose -o --header failed:\n{gen.stdout}{gen.stderr}")
    # Hosts that check their compiler's output (R's package checks build with
    # -pedantic) need the generated C free of ISO C warnings: 128-bit integers
    # go through an __extension__ typedef.
    if cc.style != "msvc":
        ped = subprocess.run([str(cc.cc), "-std=gnu11", "-pedantic", "-Werror", "-fsyntax-only",
                              "lib.c"], cwd=base, capture_output=True, text=True, timeout=120)
        if ped.returncode:
            raise RuntimeError(f"the generated C has -pedantic warnings:\n{ped.stderr[:2000]}")
    binary = base / ("host" + tc.EXE_SUFFIX)
    ok, log = cc.compile([base / "host.c", base / "lib.c"], binary, opt=1, extra=extra,
                         runtime=runtime)
    if not ok:
        raise RuntimeError(f"a host file that includes only the header does not build:\n{log}")
    run = subprocess.run([str(binary)], cwd=base, capture_output=True, text=True, timeout=60)
    want = "42 3 2.5 6 ABC"
    if run.returncode or run.stdout.strip() != want:
        raise RuntimeError(f"expected {want!r}, got {run.stdout!r} (exit {run.returncode})"
                           f"\n{run.stderr}")
