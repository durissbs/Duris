#!/usr/bin/env python3
"""Regression test: file_to_string() must not split lines longer than its read buffer.

file_to_string() reads with fgets(tmp, 255, ...), so a line longer than 254
bytes arrives in several chunks.  It used to append '\\r' after every chunk,
which injected a carriage return into the middle of long lines.  The ANSI login
screens have dozens of such lines, so the stray '\\r' redrew lines from column 0
and split UTF-8 characters and colour escapes.

This compiles the real function from db.c with small stubs and checks the
output byte for byte.
"""
from _paths import SRC
from pathlib import Path
import subprocess
import tempfile

db = (SRC / "db.c").read_text()
start = db.index("char *file_to_string(const char *name)")
function = db[start:db.index("\n}\n", start) + 3]

harness = r"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#define MAX_STRING_LENGTH 65536
#define LOG_FILE 0
#define MEM_TAG_STRING 0
#define CREATE(result, type, number, tag) ((result) = (type *)calloc((number), sizeof(type)))
static void logit(int, const char *, ...) {}
""" + function + r"""
int main(int argc, char **argv)
{
	char *text = file_to_string(argv[1]);
	if (!text)
		return 2;
	fputs(text, stdout);
	return 0;
}
"""

long_line = "\x1b[1;36m" + "█" * 120 + "\x1b[0m"  # about 370 bytes of UTF-8 and ANSI
content = "short\n" + long_line + "\n" + "x" * 254 + "\n" + "last\n"
expected = content.replace("\n", "\n\r").encode()

with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    (tmp / "harness.cpp").write_text(harness)
    subprocess.run(["g++", "-std=c++20", "-o", str(tmp / "harness"), str(tmp / "harness.cpp")],
                   check=True)
    (tmp / "screen.txt").write_bytes(content.encode())
    result = subprocess.run([str(tmp / "harness"), str(tmp / "screen.txt")],
                            capture_output=True, check=True)

assert len(long_line.encode()) > 254, "test line must exceed the fgets buffer"
actual = result.stdout
if actual != expected:
    stray = actual.replace(b"\n\r", b"").count(b"\r")
    raise SystemExit("file_to_string changed the file: %d stray carriage return(s)" % stray)
print("file_to_string long-line contract passed")
