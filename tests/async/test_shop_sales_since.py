#!/usr/bin/env python3
"""Sell-price sale counts restart on every reboot or crash and survive a copyover."""
from pathlib import Path
import os
import resource
import signal
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
PROGRAM = r'''
#include "economy/shop_sales_since.h"
#include <cassert>
#include <cstdio>
#include <string>
int main(int argc, char **argv) {
    assert(argc == 4);
    std::string error;
    const time_t now = atoll(argv[3]);
    const time_t since = shop_sales_since(argv[1], std::string(argv[2]) == "copyover", now, &error);
    printf("%lld %s\n", static_cast<long long>(since), error.empty() ? "ok" : "fallback");
}
'''

output = ROOT / "bin/tests"
output.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix="shop-sales-since-", dir=output) as temporary:
    directory = Path(temporary)
    source = directory / "test.cpp"
    binary = directory / "test"
    source.write_text(PROGRAM)
    subprocess.run([
        "g++", "-std=c++20", "-g", "-Wall", "-Wextra", "-Werror",
        "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-fno-pie", "-no-pie",
        "-I", str(ROOT / "src"), str(source),
        str(ROOT / "src/economy/shop_sales_since.c"),
        str(ROOT / "src/flatfile/flatfile_store.c"), "-o", str(binary),
    ], check=True)
    state = directory / "shop-sales"

    def no_file_writes():
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))

    def run(mode, now, failing_writes=False):
        return subprocess.check_output([str(binary), str(state), mode, str(now)], cwd=directory,
                                       text=True, timeout=15, env={
            **os.environ, "ASAN_OPTIONS": "detect_leaks=1:halt_on_error=1",
        }, preexec_fn=no_file_writes if failing_writes else None).split()

    # A copyover before any recorded cold start counts from now.
    assert run("copyover", 1000) == ["1000", "fallback"]
    # A cold start (clean reboot or crash) counts from now and records it.
    assert run("cold", 2000) == ["2000", "ok"]
    # Copyovers keep counting from that cold start.
    assert run("copyover", 2500) == ["2000", "ok"]
    assert run("copyover", 3000) == ["2000", "ok"]
    # The next cold start resets the count again.
    assert run("cold", 4000) == ["4000", "ok"]
    assert run("copyover", 4100) == ["4000", "ok"]
    # A saved time in the future is not trusted.
    assert run("copyover", 3500) == ["3500", "fallback"]
    # A cold start that cannot record its time (a full disk, say) must not leave the
    # previous cold start's record for a later copyover to count from.
    assert run("cold", 6000) == ["6000", "ok"]
    assert run("cold", 7000, failing_writes=True) == ["7000", "fallback"]
    assert run("copyover", 7100) == ["7100", "fallback"]
    # A damaged record falls back to now.
    (state / "since").write_bytes(b"bad")
    assert run("copyover", 5000) == ["5000", "fallback"]

print("shop sales since: ok")
