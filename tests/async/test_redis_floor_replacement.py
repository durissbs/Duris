#!/usr/bin/env python3
"""Exercise the production floor journal's replace/remove ordering under backpressure."""

from pathlib import Path
import subprocess
import tempfile

from _paths import ROOT

source = (ROOT / "src/redis/redis_floor_runtime.c").read_text(encoding="utf-8")
production = source[source.index("namespace\n{"):]
harness = r'''
#include <cassert>
#include <cstring>
#include <ctime>
#include <new>
#include <unordered_map>
#include <vector>

struct obj_data {
    unsigned long obj_uid;
    int value[7];
};
using P_obj = obj_data *;
struct world_recovery_object_record { int room; };
struct world_recovery_item_snapshot { int item; };
constexpr size_t WORLD_RECOVERY_MAX_ITEM_TREE = 2;
constexpr const char *REDIS_FLOOR_DROPS_SUFFIX = "floor";
constexpr const char *REDIS_FLOOR_DROP_INDEX_SUFFIX = "index";
constexpr const char *LOG_SYS = "sys";
#define OBJ_VNUM(obj) 1000
int _pwipe = 0;
struct redis_floor_mutation {
    unsigned long uid;
    const unsigned char *value;
    size_t value_size;
    bool remove;
    bool encode_world_object = false;
};
static bool reject_serialization = false, reject_submit = false;
static std::unordered_map<unsigned long, int> durable;
static int deletes = 0, submits = 0;
bool redis_flush_floor_drops();
void logit(const char *, const char *, ...) {}
bool redis_namespace_season_key(const char *, uint64_t, const char *suffix,
                                char *out, size_t capacity) {
    assert(strlen(suffix) < capacity);
    strcpy(out, suffix);
    return true;
}
struct health { bool initialized; };
health redis_floor_store_health_copy() { return {true}; }
int world_recovery_write_object_to_buffer(P_obj, int room, char *out, size_t capacity) {
    if (reject_serialization) return -1;
    assert(capacity >= sizeof(room));
    memcpy(out, &room, sizeof(room));
    return sizeof(room);
}
bool redis_floor_store_submit(const char *, const char *,
                              const redis_floor_mutation *mutations, size_t count) {
    ++submits;
    if (reject_submit) return false;
    for (size_t i = 0; i < count; ++i) {
        const auto &mutation = mutations[i];
        if (mutation.remove) {
            ++deletes;
            durable.erase(mutation.uid);
        } else {
            int room = 0;
            assert(mutation.value_size == sizeof(room));
            memcpy(&room, mutation.value, sizeof(room));
            durable[mutation.uid] = room;
        }
    }
    return true;
}

// PRODUCTION

int main() {
    redis_floor_runtime_reset();
    assert(redis_floor_runtime_configure("test", 1));
    redis_floor_runtime_set_enabled(true);
    obj_data item{42, {}};

    // A relocation of an already durable drop must never delete its record.
    redis_log_floor_drop(&item, 100);
    assert(redis_flush_floor_drops() && durable.at(42) == 100);
    for (unsigned long uid = 1000; uid < 1000 + max_floor_drop_batch; ++uid) {
        obj_data other{uid, {}};
        redis_log_floor_drop(&other, 100);
    }
    reject_submit = true;
    redis_log_floor_drop(&item, 31724);
    assert(durable.at(42) == 100 && deletes == 0);
    reject_submit = false;
    redis_log_floor_drop(&item, 31724);
    assert(durable.at(42) == 100 && deletes == 0);
    assert(redis_flush_floor_drops() && durable.at(42) == 31724 && deletes == 0);

    // An unflushed earlier record survives a failed replacement.
    redis_log_floor_drop(&item, 200);
    reject_serialization = true;
    redis_log_floor_drop(&item, 31724);
    reject_serialization = false;
    assert(redis_flush_floor_drops() && durable.at(42) == 200);
    redis_log_floor_drop(&item, 100);
    redis_log_floor_drop(&item, 31724);
    assert(floor_drop_batch_count == 1);
    assert(redis_flush_floor_drops() && durable.at(42) == 31724 && deletes == 0);

    // Picking up after a pending relocation must also remove the old durable row.
    redis_log_floor_drop(&item, 100);
    redis_remove_floor_drop(42);
    assert(redis_flush_floor_drops() && !durable.count(42) && deletes == 1);

    // A successful redrop supersedes a pending pickup, without a delete window.
    redis_log_floor_drop(&item, 100);
    assert(redis_flush_floor_drops());
    redis_remove_floor_drop(42);
    redis_log_floor_drop(&item, 31724);
    assert(redis_flush_floor_drops() && durable.at(42) == 31724 && deletes == 1);
}
'''.replace("// PRODUCTION", production)

with tempfile.TemporaryDirectory(prefix="duris-floor-replacement-") as directory:
    cpp = Path(directory) / "floor.cpp"
    binary = Path(directory) / "floor"
    cpp.write_text("#include <cstdint>\n" + harness, encoding="utf-8")
    subprocess.run(["g++", "-std=c++20", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-pie", "-no-pie",
                    str(cpp), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=10)
