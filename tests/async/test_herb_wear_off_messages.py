#!/usr/bin/env python3
"""Smoking effects say when they wear off, once (#1062 N18).

The herb tags were created without wear-off text, so Medicus and the other smoking
effects ended in silence. Compiles the production create_tags() and wear_off_message()
and checks that each herb has its own message, and that a multi-part effect such as
Blue Haze speaks only when its last part ends.
"""

import os
import subprocess
import tempfile
from pathlib import Path

from _paths import HARNESS_STUBS, ROOT, extract_function

HARNESS = r'''
#include "core/prototypes.h"
#include "core/structs.h"
#include "core/utils.h"
#include "magic/spells.h"
#include "net/comm.h"
#include <cassert>
#include <cstdio>
#include <string>

Skill skills[MAX_AFFECT_TYPES + 1]{};
int numSkills;
int SortedSkills[MAX_AFFECT_TYPES + 1];
static std::string told;
void send_to_char(const char *text, P_char) { told += text; }
void act(const char *, int, P_char, P_obj, void *, int) { assert(false); }

// FUNCTIONS

#define TAG_CREATE(Name, Index) SKILL_CREATE(Name, Index, 0)
#define TAG_CREATE_WITH_MESSAGES(Name, Index, wear_off, wear_off_room) \
	SKILL_CREATE_WITH_MESSAGES(Name, Index, 0, wear_off, wear_off_room)

// TAGS

int main()
{
	create_tags();
	char_data smoker{};
	const struct { int type; const char *text; } herbs[] = {
		{ HERB_SMOKED, "recovered from the" },
		{ HERB_OCULARIUS, "paranoia fades" },
		{ HERB_BLUE_HAZE, "lifts from your body" },
		{ HERB_MEDICUS, "stop helping your body mend itself" },
		{ HERB_BLACK_KUSH, "wears off" },
		{ HERB_GOOTWIET, "clears from your head" },
	};
	for (const auto &herb : herbs)
	{
		affected_type ending{};
		ending.type = herb.type;
		smoker.affected = &ending;
		told.clear();
		wear_off_message(&smoker, &ending);
		assert(told.find(herb.text) != std::string::npos);
		assert(!skills[herb.type].wear_off_room[0]);
	}

	// Blue Haze is several parts; only the last one to end announces it.
	affected_type last{}, first{};
	first.type = last.type = HERB_BLUE_HAZE;
	first.next = &last;
	smoker.affected = &first;
	told.clear();
	wear_off_message(&smoker, &first);
	assert(told.empty());
	smoker.affected = &last;
	wear_off_message(&smoker, &last);
	assert(told.find("lifts from your body") != std::string::npos);
	std::puts("each smoking effect says once that it has worn off");
}
'''

(ROOT / "bin/tests").mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix="herb-wear-off-", dir=ROOT / "bin/tests") as tmp:
    harness = Path(tmp) / "herbs.cpp"
    functions = "\n".join(extract_function("skills.c", signature) for signature in (
        "void SKILL_CREATE(const char *Name", "void SKILL_CREATE_WITH_MESSAGES("))
    harness.write_text(HARNESS
                       .replace("// FUNCTIONS", functions + "\n" +
                                extract_function("affects.c", "void wear_off_message("))
                       .replace("// TAGS", extract_function("skills.c", "void create_tags()\n{")))
    binary = harness.with_suffix("")
    subprocess.run(["g++", "-std=c++20", "-g", "-O1", "-fsanitize=address,undefined",
                    "-fno-sanitize-recover=undefined", "-fno-omit-frame-pointer",
                    "-ffunction-sections", "-fdata-sections", "-Wl,--gc-sections",
                    "-Isrc", "-I/usr/include/mysql", "-I/usr/include/libxml2", str(harness),
                    str(HARNESS_STUBS), "-o", str(binary)], cwd=ROOT, check=True)
    subprocess.run([str(binary)], check=True, env=dict(os.environ,
        ASAN_OPTIONS="detect_leaks=1:halt_on_error=1", UBSAN_OPTIONS="halt_on_error=1"))
