#!/usr/bin/env python3
"""Exercise production water-drop placement, timers, cancellation and relocation."""

from pathlib import Path
import subprocess
import tempfile

from _paths import ROOT, HARNESS_STUBS, extract_function
from _innate_numbers import with_innate_numbers

source = (ROOT / "src/world/handler.c").read_text(encoding="utf-8")
start = source.index("namespace\n{\nstruct water_drop_event")
end = source.index("/*\n * Take an object from a room", start)
production = source[start:end] + "\n" + extract_function(
    "world/handler.c", "void obj_from_room(P_obj object)"
)
harness = r'''
#include "core/prototypes.h"
#include "core/utils.h"
#include "net/comm.h"
#include "redis/redis_floor_runtime.h"
#include "world/vnum.obj.h"
#include <algorithm>
#include <cassert>
#include <cstddef>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

static room_data rooms[3]{};
P_room world = rooms;
extern const int top_of_world = 2;
static index_data indices[1]{};
P_index obj_index = indices;
static char_data viewer{};
static bool visible = true;
static int fail_delay = -1;
static int floor_room = -1, removed = 0;
static std::vector<std::string> messages;

struct queued_event {
    event_func callback;
    P_obj object;
    int delay;
    alignas(std::max_align_t) unsigned char payload[32];
};
static std::vector<queued_event> events;

nevent_schedule_result add_event(event_func callback, int delay, P_char, P_char,
                                  P_obj object, int, const void *data, int size) {
    if (delay == fail_delay)
        return {nevent_schedule_status::sequence_exhausted, {}};
    queued_event event{callback, object, delay, {}};
    assert(size > 0 && size <= int(sizeof(event.payload)));
    memcpy(event.payload, data, size);
    events.push_back(event);
    return {nevent_schedule_status::scheduled, {}};
}
void disarm_obj_nevents(P_obj object, event_func_type callback) {
    std::erase_if(events, [=](const auto &event) {
        return event.object == object && (!callback || event.callback == callback);
    });
}
static void dispatch(int delay) {
    for (;;) {
        auto found = std::find_if(events.begin(), events.end(), [=](const auto &event) {
            return event.delay == delay;
        });
        if (found == events.end()) return;
        auto event = *found;
        events.erase(found);
        event.callback(nullptr, nullptr, event.object, event.payload);
    }
}
int real_room(int vnum) { assert(vnum == 31724); return 1; }
int number(int, int high) { return high; }
int distance_from_shore(int) { return 7; }
bool ac_can_see_obj(P_char, P_obj, int) { return visible; }
void act(const char *message, int, P_char, P_obj, void *, int) {
    messages.emplace_back(message);
}
void artifact_update_location_sql(P_obj) {}
P_obj find_live_object(P_obj object, uint64_t uid) {
    return object->obj_uid == uid ? object : nullptr;
}
void redis_remove_floor_drop(unsigned long uid) { assert(uid); ++removed; }
void redis_log_floor_drop(P_obj object, int room) {
    assert(OBJ_ROOM(object) && world[object->loc.room].number == room);
    floor_room = room;
}
void logit(const char *, const char *, ...) {}
void wizlog(int, const char *, ...) { std::abort(); }
int panic_corruption_int(const char *, const char *, ...) { std::abort(); }
void set_obj_affected(P_obj, int, sh_int, sh_int) { std::abort(); }
void add_coins(P_obj, int, int, int, int) { std::abort(); }
void extract_obj(P_obj, int) { std::abort(); }
int room_light(int, int) { return 0; }
void writeCorpse(P_obj) { std::abort(); }
void PurgeCorpseFile(P_obj) { std::abort(); }
void PurgeSavedItemFile(P_obj) { std::abort(); }
bool falling_obj(P_obj, int, bool) { std::abort(); }

// PRODUCTION

static obj_data item(int type = ITEM_WEAPON) {
    obj_data obj{};
    obj.type = type;
    obj.loc_p = LOC_NOWHERE;
    obj.obj_uid = 42;
    return obj;
}

int main() {
    rooms[0].sector_type = SECT_OCEAN;
    rooms[0].number = 100;
    rooms[0].people = &viewer;
    rooms[1].sector_type = SECT_UNDERWATER_GR;
    rooms[1].number = 31724;
    rooms[2].sector_type = SECT_CITY;
    rooms[2].number = 200;
    indices[0].virtual_number = 1000;

    // Normal drop stays at the surface until the absolute final event.
    auto weapon = item();
    obj_to_room_dropped(&weapon, 0);
    assert(OBJ_IN_ROOM(&weapon, 0) && events.size() == 2);
    assert(messages.size() == 1 && messages.back().find("bobs") != std::string::npos);
    dispatch(239);
    assert(OBJ_IN_ROOM(&weapon, 0) && messages.size() == 1);
    dispatch(240);
    assert(OBJ_IN_ROOM(&weapon, 0) && messages.size() == 2);
    assert(messages.back().find("slowly sink") != std::string::npos);
    dispatch(359);
    assert(OBJ_IN_ROOM(&weapon, 0));
    dispatch(360);
    assert(OBJ_IN_ROOM(&weapon, 1) && !rooms[0].contents && events.empty());
    assert(messages.size() == 3 && removed == 0 && floor_room == 31724);
    obj_from_room(&weapon);

    // Pickup cancels both events, including a pickup after the warning.
    for (bool warned : {false, true}) {
        obj_to_room_dropped(&weapon, 0);
        if (warned) dispatch(240);
        obj_from_room(&weapon);
        assert(events.empty() && OBJ_NOWHERE(&weapon));
        obj_to_room_dropped(&weapon, 2);
        dispatch(360);
        assert(OBJ_IN_ROOM(&weapon, 2) && events.empty());
        obj_from_room(&weapon);
    }

    // Two independent equipment drops cancel independently.
    auto other = item(ITEM_ARMOR);
    other.obj_uid = 43;
    obj_to_room_dropped(&weapon, 0);
    obj_to_room_dropped(&other, 0);
    obj_from_room(&weapon);
    assert(events.size() == 2);
    dispatch(240);
    dispatch(360);
    assert(OBJ_IN_ROOM(&other, 1) && OBJ_NOWHERE(&weapon));
    obj_from_room(&other);

    // Empty and filled mutable containers must not expose stale floor trees
    // to the new surface recovery window.
    for (int type : {ITEM_CONTAINER, ITEM_QUIVER, ITEM_SPELLBOOK}) {
        auto bag = item(type);
        auto a = item(), b = item();
        a.obj_uid = 44; b.obj_uid = 45;
        for (bool filled : {false, true}) {
            bag.contains = filled ? &a : nullptr;
            a.next_content = &b;
            obj_to_room_dropped(&bag, 0);
            assert(OBJ_IN_ROOM(&bag, 1) && events.empty());
            assert(bag.contains == (filled ? &a : nullptr));
            obj_from_room(&bag);
        }
    }

    // Legacy paths and exceptions are unchanged.
    obj_to_room(&weapon, 0);
    assert(OBJ_IN_ROOM(&weapon, 1) && events.empty());
    obj_from_room(&weapon);
    SET_BIT(weapon.extra_flags, ITEM_FLOAT);
    obj_to_room_dropped(&weapon, 0);
    assert(OBJ_IN_ROOM(&weapon, 0) && events.empty());
    obj_from_room(&weapon);
    REMOVE_BIT(weapon.extra_flags, ITEM_FLOAT);
    SET_BIT(weapon.extra_flags, ITEM_ARTIFACT);
    obj_to_room_dropped(&weapon, 0);
    assert(OBJ_IN_ROOM(&weapon, 0) && weapon.z_cord == -7 && events.empty());
    obj_from_room(&weapon);
    REMOVE_BIT(weapon.extra_flags, ITEM_ARTIFACT);
    weapon.z_cord = 0;

    // A failed final event must fall back to immediate legacy sinking.
    fail_delay = 360;
    obj_to_room_dropped(&weapon, 0);
    assert(OBJ_IN_ROOM(&weapon, 1) && events.empty());
    obj_from_room(&weapon);

    // A failed warning must not strand the object.
    fail_delay = 240;
    obj_to_room_dropped(&weapon, 0);
    assert(OBJ_IN_ROOM(&weapon, 0) && events.size() == 1);
    dispatch(360);
    assert(OBJ_IN_ROOM(&weapon, 1) && events.empty());
    obj_from_room(&weapon);

    // Hidden items do not reveal themselves in any message.
    fail_delay = -1;
    visible = false;
    messages.clear();
    obj_to_room_dropped(&weapon, 0);
    dispatch(240);
    dispatch(360);
    assert(messages.empty() && OBJ_IN_ROOM(&weapon, 1));
    obj_from_room(&weapon);
}
'''.replace("// PRODUCTION", production)

build = ROOT / "bin/tests"
build.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix="water-drop-", dir=build) as temporary:
    source = Path(temporary) / "harness.cpp"
    binary = Path(temporary) / "harness"
    source.write_text(with_innate_numbers(harness), encoding="utf-8")
    subprocess.run([
        "g++", "-std=c++20", "-g", "-fsanitize=address,undefined",
        "-fno-omit-frame-pointer", "-fno-pie", "-no-pie",
        "-ffunction-sections", "-fdata-sections", "-I", str(ROOT / "src"),
        str(source), str(HARNESS_STUBS), "-Wl,--gc-sections", "-o", str(binary),
    ], check=True)
    subprocess.run([str(binary)], check=True, timeout=30)
