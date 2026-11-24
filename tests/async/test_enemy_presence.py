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

morph = extract_function("utility.c", "int IS_MORPH(")

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

@MORPH@
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

    // Samples every five minutes from five minutes after boot; the first
    // report is published by the twelfth sample, an hour after boot.
    auto run_hour = [&](unsigned good_count, unsigned evil_count) {
        for (unsigned i = 0; i < 12; i++) {
            set_side(0, 0, good_count);
            set_side(1, 0, evil_count);
            fake_now += 300;
            sample_enemy_presence();
        }
    };
    set_side(1, 0, 6);
    sample_enemy_presence(); // Schedules the first sample.
    fake_now += 299;
    sample_enemy_presence();
    assert(enemy_presence_samples == 0);
    fake_now += 1;
    for (unsigned i = 0; i < 11; i++) {
        sample_enemy_presence();
        assert(report(&good).empty() && report(&staff).empty());
        fake_now += 300;
    }
    sample_enemy_presence();
    assert(report(&good) ==
           "&+rShadows gathered at the edges of the realm; evil was on the move.&n "
           "&+L(Scouts reported 0 minutes ago.)&n\n");
    assert(report(&evil) ==
           "&+wNo do-gooder dared show their face.&n &+L(Scouts reported 0 minutes ago.)&n\n");
    assert(report(&neutral).empty());
    assert(report(&staff) == "&+rPeak active mortals (30+) in the reported hour: 6 &+Revil&n&+r, "
                             "0 &+Ygood&n&+r (reported 0 min ago).&n\n");

    // A morphed player types who through a mob body with no side.
    npc_only_data morph_data = {};
    char_data beast = {};
    beast.specials.act = ACT_ISNPC;
    beast.only.npc = &morph_data;
    morph_data.orig_char = &good;
    assert(report(&beast) == report(&good));
    morph_data.orig_char = &staff;
    assert(report(&beast) == report(&staff));
    // So does a switched player.
    descriptor_data link = {};
    char_data mob = {};
    mob.specials.act = ACT_ISNPC;
    mob.only.npc = &morph_data;
    morph_data.orig_char = nullptr;
    mob.desc = &link;
    link.original = &evil;
    assert(report(&mob) == report(&evil));
    mob.desc = nullptr;
    assert(report(&mob).empty());

    // The report stays the same between hourly reports while its age counts up
    // to 59 minutes, even as new samples arrive. Under-30s are never counted.
    for (unsigned i = 0; i < 11; i++) {
        set_side(0, 20, 2);
        set_side(1, 9, 0);
        fake_now += 300;
        sample_enemy_presence();
        assert(report(&good).find("Shadows gathered") != std::string::npos);
        assert(report(&evil).find("No do-gooder") != std::string::npos);
    }
    fake_now += 61;
    assert(report(&good).find("(Scouts reported 56 minutes ago.)") != std::string::npos);
    fake_now += 238;
    assert(report(&good).find("(Scouts reported 59 minutes ago.)") != std::string::npos);
    sample_enemy_presence(); // Not due for another second.
    assert(report(&good).find("Shadows gathered") != std::string::npos);
    // The twelfth sample publishes the new hour: its peak, then 0 minutes.
    fake_now += 1;
    sample_enemy_presence();
    assert(report(&good).find("The land lay quiet") != std::string::npos);
    assert(report(&good).find("(Scouts reported 0 minutes ago.)") != std::string::npos);
    assert(report(&evil).find("A few self-righteous fools") != std::string::npos);
    fake_now += 60;
    assert(report(&good).find("(Scouts reported 1 minute ago.)") != std::string::npos);

    // A single busy sample sets the hour's peak.
    fake_now -= 60;
    for (unsigned i = 0; i < 12; i++) {
        set_side(1, 0, i == 3 ? 17 : 1);
        fake_now += 300;
        sample_enemy_presence();
    }
    assert(report(&good).find("A horde of evil") != std::string::npos);

    // Band edges.
    const std::pair<unsigned, const char *> bands[] = {
        { 0, "The land lay quiet" }, { 1, "A faint stench" }, { 4, "A faint stench" },
        { 5, "Shadows gathered" }, { 8, "Shadows gathered" }, { 9, "War drums" },
        { 12, "War drums" }, { 13, "A dark host" }, { 16, "A dark host" },
        { 17, "A horde of evil" }, { 40, "A horde of evil" },
    };
    for (const auto &[count, words] : bands) {
        run_hour(0, count);
        assert(report(&good).find(words) != std::string::npos);
    }
    const std::pair<unsigned, const char *> good_bands[] = {
        { 0, "No do-gooder" }, { 1, "A few self-righteous fools" }, { 4, "A few self-righteous fools" },
        { 5, "Bands of meddling" }, { 8, "Bands of meddling" }, { 9, "Banners of the light" },
        { 12, "Banners of the light" }, { 13, "An army of the light" },
        { 16, "An army of the light" }, { 17, "A shining host" }, { 40, "A shining host" },
    };
    for (const auto &[count, words] : good_bands) {
        run_hour(count, 0);
        assert(report(&evil).find(words) != std::string::npos);
    }
    puts("enemy presence cases passed");
}
""".replace("@PRESENCE@", presence).replace("@MORPH@", morph)

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
