"""The runtime built with -DGS_HOSTED, inside a C host: every failure comes
back to goose_host_call with the data stacks rewound, printing and polling go
through the host's hooks, and nothing ends the process."""
import subprocess
import sys
from pathlib import Path

import toolchain as tc

# What R's package checks reject in compiled code (tools:::so_symbol_names_table),
# besides the C++ and Fortran names: a hosted runtime must reference none.
FORBIDDEN = {"abort", "exit", "_exit", "_Exit", "quick_exit", "printf", "puts", "putchar",
             "stdout", "stderr", "__stdoutp", "__stderrp", "sprintf", "vprintf", "vsprintf",
             "rand", "srand", "random", "srandom", "__assert_fail", "__assert_rtn"}

LIB = '''
extern fn goose_host_poll() = "goose_host_poll";

export fn add(a: i64, b: i64) -> i64 { return a + b; }

export fn bad_index(i: i64) -> i64 {
    var a: i64[3] = [10, 20, 30];
    return a[i];
}

// Grows one data stack by n values: past 1 MB it overflows, as the host
// builds it with a 1 MB reservation.
export fn grow(n: i64) -> i64 {
    var v: i64[>..] = [];
    for i in 0..n { v.push(i); }
    return v.len;
}

export fn say(n: i64) -> i64 {
    print("hello ", n);
    return n;
}

// A long loop that lets the host stop it every 1024 steps.
export fn spin(n: i64) -> i64 {
    var s: i64 = 0;
    for i in 0..n {
        if (i & 1023) == 0 { goose_host_poll(); }
        s += i & 7;
    }
    return s;
}
'''

HOST = r'''
#include "lib.h"
#include <signal.h>
#include <stdio.h>
#include <string.h>

static char outbuf[256];
static size_t outlen;
static void out(int stream, const uint8_t *p, int64_t len) {
    if (stream == 1 && outlen + (size_t)len < sizeof outbuf) {
        memcpy(outbuf + outlen, p, (size_t)len);
        outlen += (size_t)len;
    }
}
static int polls, stop_after;
static int poll(void) { return stop_after && ++polls >= stop_after; }

typedef struct { int64_t a, b, v; int which; } call_t;
static void init_body(void *p) { (void)p; static char n[] = "host"; char *argv[] = {n, NULL}; goose_init(1, argv); }
static void body(void *p) {
    call_t *c = p;
    switch (c->which) {
    case 0: c->v = add(c->a, c->b); break;
    case 1: c->v = bad_index(c->a); break;
    case 2: c->v = grow(c->a); break;
    case 3: c->v = say(c->a); break;
    case 4: c->v = spin(c->a); break;
    case 5: *(volatile uint8_t *)goose_host_test_gap() = 1; break;
    }
}
static int nested_rc;
static const uint8_t *nested_msg;
static void nested_body(void *p) {
    (void)p;
    call_t c = {1, 2, 0, 0};
    int64_t len;
    nested_rc = goose_host_call(body, &c, &nested_msg, &len);
}

static int failures;
#define CHECK(cond, what) do { if (!(cond)) { fprintf(stderr, "FAIL: %s\n", what); failures++; } } while (0)

static int run(int which, int64_t a, int64_t b, int64_t *v, const char **msg) {
    call_t c = {a, b, 0, which};
    const uint8_t *m = NULL;
    int64_t len = 0;
    int rc = goose_host_call(body, &c, &m, &len);
    *v = c.v;
    *msg = rc ? (const char *)m : "";
    return rc;
}

int main(void) {
    goose_host h = {out, poll};
    goose_host_set(&h);
    struct sigaction before, after;
    sigaction(SIGSEGV, NULL, &before);
    const uint8_t *m;
    int64_t len, v;
    const char *msg;
    CHECK(goose_host_call(init_body, NULL, &m, &len) == 0, "goose_init in a call");
    int64_t base = goose_host_stack_used();

    CHECK(run(0, 40, 2, &v, &msg) == 0 && v == 42, "add");
    CHECK(run(1, 5, 0, &v, &msg) == 1 && strstr(msg, "out of bounds"), "a bounds failure");
    CHECK(goose_host_stack_used() == base, "stacks rewound after a bounds failure");
    CHECK(run(2, 1000, 0, &v, &msg) == 0 && v == 1000, "grow within the reservation");
    CHECK(run(2, 10000000, 0, &v, &msg) == 1 && strstr(msg, "data stack overflow"),
          "a data stack overflow");
    CHECK(goose_host_stack_used() == base, "stacks rewound after an overflow");
    CHECK(run(5, 0, 0, &v, &msg) == 1 && strstr(msg, "data stack overflow"), "the guard gap");
    CHECK(run(3, 7, 0, &v, &msg) == 0 && outlen == 8 && !memcmp(outbuf, "hello 7\n", 8),
          "print goes to the out hook");
    stop_after = 3;
    CHECK(run(4, 100000000, 0, &v, &msg) == 2 && !strcmp(msg, "interrupted"), "poll stops a call");
    stop_after = 0;
    CHECK(goose_host_stack_used() == base, "stacks rewound after an interrupt");
    CHECK(goose_host_call(nested_body, NULL, &m, &len) == 0 && nested_rc == 1 &&
          strstr((const char *)nested_msg, "re-entered"), "a nested call is refused");
    CHECK(run(0, 1, 1, &v, &msg) == 0 && v == 2, "usable after every failure");
    sigaction(SIGSEGV, NULL, &after);
    CHECK(after.sa_handler == before.sa_handler && after.sa_sigaction == before.sa_sigaction,
          "the fault handler is the host's again");
    if (failures) return 1;
    puts("ok");
    return 0;
}
'''


def check_hosted_runtime(exe, cc, base, *, runtime, extra=()):
    """Raises RuntimeError on a failure. POSIX hosts only for now."""
    base = (Path(base) / "hosted-runtime").resolve()
    base.mkdir(parents=True, exist_ok=True)
    tc.write_text(base / "lib.goose", LIB)
    tc.write_text(base / "host.c", HOST)
    gen = subprocess.run([str(exe), "-o", "lib.c", "--header", "lib.h", "lib.goose"],
                         cwd=base, capture_output=True, text=True, timeout=120)
    if gen.returncode:
        raise RuntimeError(f"goose -o --header failed:\n{gen.stdout}{gen.stderr}")
    binary = base / ("host" + tc.EXE_SUFFIX)
    ok, log = cc.compile([base / "host.c", base / "lib.c"], binary, opt=1, extra=extra,
                         defines=("GS_HOSTED=1", "GS_STACK_RESERVE=1048576"), runtime=runtime)
    if not ok:
        raise RuntimeError(f"the hosted program does not build:\n{log}")
    run = subprocess.run([str(binary)], cwd=base, capture_output=True, text=True, timeout=120)
    if run.returncode or run.stdout.strip() != "ok":
        raise RuntimeError(f"exit {run.returncode}:\n{run.stdout}{run.stderr}")
    # The runtime object alone, as a host links it.
    rt = subprocess.run([str(exe), "--emit-runtime", "rt.c"], cwd=base, capture_output=True,
                        text=True, timeout=120)
    if rt.returncode:
        raise RuntimeError(f"goose --emit-runtime failed:\n{rt.stderr}")
    obj = subprocess.run([str(cc.cc), "-O2", "-Wall", "-Werror", "-DGS_HOSTED", "-c", "rt.c",
                          "-o", "rt.o"], cwd=base, capture_output=True, text=True, timeout=300)
    if obj.returncode:
        raise RuntimeError(f"the hosted runtime object has warnings:\n{obj.stderr[:3000]}")
    nm = subprocess.run(["nm", "-u", "rt.o"], cwd=base, capture_output=True, text=True)
    names = {line.split()[-1] for line in nm.stdout.splitlines() if line.strip()}
    if sys.platform == "darwin":  # Mach-O prefixes every C name with one underscore.
        names = {n[1:] if n.startswith("_") else n for n in names}
    bad = sorted(names & FORBIDDEN)
    if bad:
        raise RuntimeError(f"the hosted runtime references {', '.join(bad)}")
