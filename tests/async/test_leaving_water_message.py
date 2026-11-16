#!/usr/bin/env python3
"""Coming up for air says so, once and only then (#1062 N7).

event_underwatersector() cleared the holding-breath and drowning flags without a word
when a character left the water. Compiles the production event and checks that a
character who was holding their breath or drowning is told when they reach air, and
that no one else is: a dry character, or one who gained water breathing below.
"""

import os
import subprocess
import tempfile
from pathlib import Path

from _paths import HARNESS_STUBS, ROOT, extract_function, source

HARNESS = r'''
// INCLUDES
#include <cassert>
#include <cstdlib>
#include <cstdio>
#include <string>

P_room world;
static std::string told;
static int events;
// Neither the drowning damage nor a death is reached by these cases.
void StartRegen(P_char, regen_resource) { std::abort(); }
void die(P_char, P_char) { std::abort(); }
void send_to_char(const char *text, P_char) { told += text; }
nevent_schedule_result add_event(event_func, int, P_char, P_char, P_obj, int, const void *, int)
{
	++events;
	return {};
}

// FUNCTIONS

static bool surfaced(int room, unsigned long breath, unsigned long affects = 0)
{
	char_data ch{};
	pc_only_data pc{};
	ch.only.pc = &pc;
	ch.in_room = room;
	ch.specials.affected_by = affects;
	ch.specials.affected_by2 = breath;
	told.clear();
	event_underwatersector(&ch, nullptr, nullptr, nullptr);
	assert(!(ch.specials.affected_by2 & (AFF2_HOLDING_BREATH | AFF2_IS_DROWNING)));
	return told.find("fresh air") != std::string::npos;
}

int main()
{
	world = new room_data[2]{};
	world[0].sector_type = SECT_UNDERWATER;
	world[1].sector_type = SECT_FIELD;
	assert(surfaced(1, AFF2_HOLDING_BREATH));
	assert(surfaced(1, AFF2_IS_DROWNING));
	assert(!surfaced(1, 0));					     // never under
	assert(!surfaced(0, AFF2_HOLDING_BREATH, AFF_WATERBREATH)); // still under
	assert(events == 0);
	delete[] world;
	std::puts("reaching air is announced only for a real transition");
}
'''

specials = "mob/specials.c"
includes = "\n".join(line for line in source(specials).read_text().splitlines()
                     if line.startswith("#include"))
(ROOT / "bin/tests").mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix="leave-water-", dir=ROOT / "bin/tests") as tmp:
    harness = Path(tmp) / "water.cpp"
    harness.write_text(HARNESS.replace("// INCLUDES", includes).replace(
        "// FUNCTIONS", extract_function(specials, "void event_underwatersector(")))
    binary = harness.with_suffix("")
    subprocess.run(["g++", "-std=c++20", "-g", "-O1", "-fsanitize=address,undefined",
                    "-fno-sanitize-recover=undefined", "-fno-omit-frame-pointer",
                    "-ffunction-sections", "-fdata-sections", "-Wl,--gc-sections",
                    "-Isrc", "-I/usr/include/mysql", "-I/usr/include/libxml2", str(harness),
                    str(HARNESS_STUBS), "-o", str(binary)], cwd=ROOT, check=True)
    subprocess.run([str(binary)], check=True, env=dict(os.environ,
        ASAN_OPTIONS="detect_leaks=1:halt_on_error=1", UBSAN_OPTIONS="halt_on_error=1"))
