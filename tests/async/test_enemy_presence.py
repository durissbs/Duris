#!/usr/bin/env python3
"""Execute the who report of the opposing side's rolling-hour presence."""

from _paths import ROOT, SRC, extract_function, source
from pathlib import Path
import subprocess
import tempfile

actinf = source("actinf.c").read_text(encoding="utf-8")
start = actinf.index("// Opposing-side presence for who.")
presence = actinf[start:actinf.index("void do_who(", start)]
clock = extract_function("actinf.c", "static int64_t enemy_presence_clock(")
assert clock in presence
presence = presence.replace(clock, "static int64_t enemy_presence_clock() { return fake_now; }")
assert "sample_enemy_presence();" in source("comm.c").read_text(encoding="utf-8")
assert "append_enemy_presence(ch, who_output, MAX_STRING_LENGTH," in extract_function(
    "actinf.c", "void do_who(")

harness = r"""
#include "core/prototypes.h"
#include "core/structs.h"
#include "core/utils.h"
#include "economy/divine_boon_activity.h"
#include <cassert>
#include <string>

int64_t fake_now = 100000;
std::array<divine_boon_population, 2> fake_population = {};
std::array<divine_boon_population, 2> divine_boon_activity_population() { return fake_population; }

@PRESENCE@

static void set_side(int side, unsigned lowbies, unsigned counted)
{
    auto &levels = fake_population[side].active_levels;
    levels.clear();
    for (unsigned i = 0; i < lowbies; i++)
        levels.push_back(i % 2 ? 29 : 1);
    for (unsigned i = 0; i < counted; i++)
        levels.push_back(i % 2 ? 56 : 30);
}

static std::string report(P_char ch)
{
    char out[MAX_STRING_LENGTH] = "";
    append_enemy_presence(ch, out, sizeof(out), fake_now);
    return out;
}

int main()
{
    char_data good = {}, evil = {}, neutral = {}, staff = {};
    for (auto ch : { &good, &evil, &neutral, &staff })
        ch->player.level = 30;
    GET_RACEWAR(&good) = RACEWAR_GOOD;
    GET_RACEWAR(&evil) = RACEWAR_EVIL;
    GET_RACEWAR(&neutral) = RACEWAR_NEUTRAL;
    staff.player.level = MAXLVL;

    // The first sample waits five minutes after boot for activity to build up.
    set_side(1, 0, 6);
    sample_enemy_presence();
    fake_now += 299;
    sample_enemy_presence();
    assert(report(&good).empty() && report(&staff).empty());
    fake_now += 1;
    sample_enemy_presence();
    assert(report(&good) ==
           "&+rDark shapes moved through the wilds; evil was abroad.&n "
           "&+L(Scouts reported 0 minutes ago.)&n\n");
    assert(report(&evil) ==
           "&+wNo do-gooder dared show their face.&n &+L(Scouts reported 0 minutes ago.)&n\n");
    assert(report(&neutral).empty());
    assert(report(&staff) == "&+rPeak active mortals (30+) in the last hour: 6 &+Revil&n&+r, 0 "
                             "&+Ygood&n&+r (sampled 0 min ago).&n\n");

    // Characters under 30 are not counted.
    set_side(1, 9, 0);
    set_side(0, 20, 0);
    fake_now += 300;
    sample_enemy_presence();
    assert(report(&evil).find("No do-gooder") != std::string::npos);
    // The six evils sampled five minutes ago still set the peak.
    fake_now += 61;
    assert(report(&good).find("Dark shapes") != std::string::npos);
    assert(report(&good).find("(Scouts reported 1 minute ago.)") != std::string::npos);

    // Band edges.
    const std::pair<unsigned, const char *> bands[] = {
        { 1, "A faint taint" }, { 4, "A faint taint" }, { 5, "Dark shapes" },
        { 8, "Dark shapes" }, { 9, "An evil host" }, { 12, "An evil host" },
        { 13, "A great horde" }, { 40, "A great horde" },
    };
    for (const auto &[count, words] : bands) {
        for (auto &sample : enemy_presence_samples)
            sample = {};
        enemy_presence_samples[0] = { fake_now, 0, count };
        assert(report(&good).find(words) != std::string::npos);
    }
    enemy_presence_samples[0] = { fake_now, 13, 0 };
    assert(report(&evil).find("A shining host of the light") != std::string::npos);

    // Samples older than an hour drop out of the rolling window.
    for (auto &sample : enemy_presence_samples)
        sample = {};
    enemy_presence_samples[3] = { fake_now - 3600, 0, 20 };
    enemy_presence_samples[4] = { fake_now - 3599, 0, 2 };
    assert(report(&good).find("A faint taint") != std::string::npos);
    assert(report(&good).find("(Scouts reported 59 minutes ago.)") != std::string::npos);
    enemy_presence_samples[4] = {};
    assert(report(&good).empty());

    // Twelve five-minute samples fill the ring; the thirteenth replaces the oldest.
    for (auto &sample : enemy_presence_samples)
        sample = {};
    enemy_presence_next = 0;
    fake_now += 300;
    set_side(0, 0, 0);
    for (unsigned i = 0; i < 13; i++) {
        set_side(1, 0, i == 0 ? 30 : 1);
        sample_enemy_presence();
        fake_now += 300;
    }
    assert(enemy_presence_next == 1);
    fake_now -= 300;
    assert(report(&good).find("A faint taint") != std::string::npos);
    puts("enemy presence cases passed");
}
""".replace("@PRESENCE@", presence)

output = ROOT / "bin/tests"
output.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix="enemy-presence-", dir=output) as directory:
    directory = Path(directory)
    source_file = directory / "harness.cpp"
    binary = directory / "harness"
    source_file.write_text(harness)
    subprocess.run(
        ["g++", "-std=c++20", "-Wall", "-Wextra", "-Werror", f"-I{SRC}", str(source_file),
         str(SRC / "safe_format.c"), "-lbsd", "-o", str(binary)],
        check=True,
    )
    subprocess.run([str(binary)], check=True)
