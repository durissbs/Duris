#!/usr/bin/env python3
"""Report files (ideas, bugs, typos) keep every entry and fail loudly."""

from pathlib import Path
import subprocess
import tempfile
from _paths import ROOT, extract_function, source

append = extract_function("db.c", "bool AppendToFile(")
tail = extract_function("specs.object.c", "static bool read_file_tail(")

# Every report command writes through the append-only helper and stops on failure.
actoth = source("actoth.c").read_text()
assert "InsertIntoFile" not in actoth
for command, constant in [("do_idea", "IDEA_FILE"), ("do_typo", "TYPO_FILE"), ("do_bug", "BUG_FILE")]:
    body = extract_function("actoth.c", f"void {command}(")
    assert f"if (!AppendToFile({constant}, buf))" in body, command
# Each report goes to Discord on its own channel, once, only after it is saved.
for command, channel in [("do_idea", "idea"), ("do_typo", "typo"), ("do_bug", "bug")]:
    body = extract_function("actoth.c", f"void {command}(")
    call = f'discord_report_export("{channel}", ch, '
    assert body.count(call) == 1, command
    assert body.index("AppendToFile(") < body.index(call), command
assert "read_file_tail(path, MAX_STRING_LENGTH, tail)" in extract_function(
    "specs.object.c", "int changelog(")

harness = r'''
#include <cassert>
#include <cerrno>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <string>
#include <unistd.h>
''' + append + "\n" + tail + r'''
static std::string slurp(const char *path)
{
	std::string text;
	assert(read_file_tail(path, 1 << 30, text));
	return text;
}

int main(int, char **argv)
{
	const std::string dir = argv[1], file = dir + "/ideas";
	errno = 0;
	std::string none;
	assert(!read_file_tail(file.c_str(), 100, none) && errno == ENOENT);

	// No cap: the first entry survives far more than 64 KiB of later ones.
	assert(AppendToFile(file.c_str(), "**Sizan: first\n"));
	const std::string spam = "**Griefer: " + std::string(1000, 'x') + "\n";
	for (int i = 0; i < 200; ++i)
		assert(AppendToFile(file.c_str(), spam.c_str()));
	const std::string all = slurp(file.c_str());
	assert(all.rfind("**Sizan: first\n", 0) == 0);
	assert(all.size() == strlen("**Sizan: first\n") + 200 * spam.size());

	// Failures are reported and leave earlier entries alone.
	assert(!AppendToFile("/dev/full", "**X: lost\n"));
	assert(!AppendToFile(dir.c_str(), "**X: lost\n"));
	assert(!AppendToFile((dir + "/missing/ideas").c_str(), "**X: lost\n"));
	assert(slurp(file.c_str()) == all);

	// The tail starts at a line boundary and ends with the newest entry.
	assert(AppendToFile(file.c_str(), "**Zoe: newest\n"));
	std::string last;
	assert(read_file_tail(file.c_str(), 1500, last));
	assert(last == spam + "**Zoe: newest\n");
	std::string small;
	assert(read_file_tail(file.c_str(), 20, small) && small == "**Zoe: newest\n");
	puts("report file harness passed");
}
'''

with tempfile.TemporaryDirectory(prefix="report-files-", dir=ROOT / "bin") as directory:
    temp = Path(directory)
    (temp / "harness.cpp").write_text(harness)
    subprocess.run(["g++", "-std=c++20", "-Wall", "-Wextra", "-Werror", "-Og", "-g",
                    str(temp / "harness.cpp"), "-o", str(temp / "harness")],
                   check=True, cwd=ROOT, timeout=120)
    subprocess.run([str(temp / "harness"), str(temp)], check=True, cwd=ROOT, timeout=60)

print("Report files are append-only, keep every entry and report write failures")
