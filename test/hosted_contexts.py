"""Contexts (--contexts) in a hosted program: each context has its own
globals and data stacks, a call nested in another runs once the host swaps
in a different context, and a failed context can be dropped for a new one."""
import subprocess
from pathlib import Path

import toolchain as tc

LIB = '''
extern fn goose_host_poll() = "goose_host_poll";

var count: i64 = 100;

// A global only this context sees.
export fn bump() -> i64 {
    count += 1;
    return count;
}

// Polls the host while it holds stack and global state of its own.
export fn spin(n: i64) -> i64 {
    var v: i64[>..] = [];
    for i in 0..n {
        if (i & 1023) == 0 { goose_host_poll(); }
        v.push(i & 7);
    }
    count += 1;
    var s: i64 = 0;
    for x in v { s += x; }
    return s;
}

export fn bad_index(i: i64) -> i64 {
    count += 1000;
    var a: i64[3] = [10, 20, 30];
    return a[i];
}
'''

HOST = r'''
#include "lib.h"
#include <signal.h>
#include <stdio.h>
#include <string.h>

static int failures;
#define CHECK(cond, what) do { if (!(cond)) { fprintf(stderr, "FAIL: %s\n", what); failures++; } } while (0)

typedef struct { int which; int64_t a, v; } call_t;
static void init_body(void *p) { (void)p; static char n[] = "host"; char *argv[] = {n, NULL}; goose_init(1, argv); }
static void body(void *p) {
    call_t *c = p;
    if (c->which == 0) c->v = bump();
    else if (c->which == 1) c->v = spin(c->a);
    else c->v = bad_index(c->a);
}
static int call(int which, int64_t a, int64_t *v, const char **msg) {
    call_t c = {which, a, 0};
    const uint8_t *m = NULL;
    int64_t len;
    int rc = goose_host_call(body, &c, &m, &len);
    *v = c.v;
    *msg = rc ? (const char *)m : "";
    return rc;
}

/* A fresh context, started inside a host call while it is current. */
static goose_ctx *ctx_new(void) {
    goose_ctx *c = goose_ctx_new();
    if (!c) return NULL;
    goose_ctx *prev = goose_ctx_swap(c);
    const uint8_t *m;
    int64_t len;
    int rc = goose_host_call(goose_ctx_start, NULL, &m, &len);
    goose_ctx_swap(prev);
    if (rc) { goose_ctx_free(c); return NULL; }
    return c;
}

/* The poll hook: on the third poll, a nested call in another context, and
   once more without swapping, which must be refused. */
static goose_ctx *inner;
static int polls, nested_ok, nested_refused;
static int64_t nested_value;
static int poll(void) {
    if (++polls != 3) return 0;
    int64_t v;
    const char *msg;
    nested_refused = call(0, 0, &v, &msg) == 1 && strstr(msg, "re-entered");
    goose_ctx *prev = goose_ctx_swap(inner);
    nested_ok = call(1, 5000, &nested_value, &msg) == 0;
    goose_ctx_swap(prev);
    return 0;
}

int main(void) {
    struct sigaction before, after;
    sigaction(SIGSEGV, NULL, &before);
    goose_host h = {NULL, poll};
    goose_host_set(&h);
    const uint8_t *m;
    int64_t len, v;
    const char *msg;
    CHECK(goose_host_call(init_body, NULL, &m, &len) == 0, "goose_init");

    /* Separate globals. */
    CHECK(call(0, 0, &v, &msg) == 0 && v == 101, "main's global");
    goose_ctx *a = ctx_new();
    CHECK(a != NULL, "a new context");
    goose_ctx *prev = goose_ctx_swap(a);
    CHECK(call(0, 0, &v, &msg) == 0 && v == 101, "a new context's globals start fresh");
    CHECK(call(0, 0, &v, &msg) == 0 && v == 102, "and are its own");
    goose_ctx_swap(prev);
    CHECK(call(0, 0, &v, &msg) == 0 && v == 102, "main's global is untouched");

    /* A nested call from inside a running one. */
    inner = ctx_new();
    CHECK(inner != NULL, "the inner context");
    int64_t base = goose_host_stack_used();
    CHECK(call(1, 100000, &v, &msg) == 0, "the outer call");
    CHECK(nested_refused, "a nested call in the same context is refused");
    CHECK(nested_ok && nested_value == 17500, "a nested call in another context runs");
    CHECK(v == 350000, "the outer call's result is intact");
    CHECK(goose_host_stack_used() == base, "main's stacks are back where they were");

    /* A failed context is dropped and replaced. */
    goose_ctx *b = ctx_new();
    prev = goose_ctx_swap(b);
    CHECK(call(2, 9, &v, &msg) == 1 && strstr(msg, "out of bounds"), "a failure in a context");
    goose_ctx_swap(prev);
    goose_ctx_free(b);
    b = ctx_new();
    prev = goose_ctx_swap(b);
    CHECK(call(0, 0, &v, &msg) == 0 && v == 101, "its replacement starts fresh");
    goose_ctx_swap(prev);
    goose_ctx_free(b);
    goose_ctx_free(a);
    goose_ctx_free(inner);
    CHECK(call(0, 0, &v, &msg) == 0 && v == 104, "main after all of it");

    sigaction(SIGSEGV, NULL, &after);
    CHECK(after.sa_handler == before.sa_handler, "the fault handler is the host's again");
    if (failures) return 1;
    puts("ok");
    return 0;
}
'''


def check_hosted_contexts(exe, cc, base, *, runtime, extra=()):
    """Raises RuntimeError on a failure. POSIX hosts only for now."""
    base = (Path(base) / "hosted-contexts").resolve()
    base.mkdir(parents=True, exist_ok=True)
    tc.write_text(base / "lib.goose", LIB)
    tc.write_text(base / "host.c", HOST)
    gen = subprocess.run([str(exe), "-o", "lib.c", "--header", "lib.h", "--contexts", "lib.goose"],
                         cwd=base, capture_output=True, text=True, timeout=120)
    if gen.returncode:
        raise RuntimeError(f"goose --contexts failed:\n{gen.stdout}{gen.stderr}")
    binary = base / ("host" + tc.EXE_SUFFIX)
    ok, log = cc.compile([base / "host.c", base / "lib.c"], binary, opt=1, extra=extra,
                         defines=("GS_HOSTED=1",), runtime=runtime)
    if not ok:
        raise RuntimeError(f"the program with contexts does not build:\n{log}")
    run = subprocess.run([str(binary)], cwd=base, capture_output=True, text=True, timeout=120)
    if run.returncode or run.stdout.strip() != "ok":
        raise RuntimeError(f"exit {run.returncode}:\n{run.stdout}{run.stderr}")
