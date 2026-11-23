#!/usr/bin/env python3
"""Quest rewards claim artifact identity, so the reward survives the next login."""
import os
from pathlib import Path
import subprocess
import tempfile

from _paths import ROOT, HARNESS_STUBS, extract_function

fixture = r'''
#include "core/prototypes.h"
#include "core/utils.h"
#include "core/utility.h"
#include "world/db.h"
#include "net/comm.h"
#include "item/item_movement_transaction.h"
#include "magic/spells.h"
#include "sql/sql.h"
#include <cassert>
#include <cerrno>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
Skill skills[MAX_SKILLS]{};
long new_exp_table[MAXLVL + 2]{};
static index_data objects[1]{}, mobiles[1]{};
P_index obj_index=objects, mob_index=mobiles;
// Other reward kinds are not exercised here.
int gain_exp(P_char,P_char,int,int) { std::abort(); }
void ADD_MONEY(P_char,int) { std::abort(); }
char *coin_stringv(int,int) { std::abort(); }
ClassSkillInfo SKILL_DATA_ALL(P_char,int) { std::abort(); }
static obj_data reward{};
static int creation_error, created, granted, extracted, extracted_flag=-1;
static bool grant_accepts=true;
static std::string told;
P_obj read_object(int,int) { std::fputs("quest reward bypassed artifact identity\n",stderr); std::abort(); }
P_obj read_object_for_creation(int vnum,int type) {
 assert(vnum==28973 && type==VIRTUAL);
 ++created;
 if (creation_error) { errno=creation_error; return nullptr; }
 return &reward;
}
bool item_creation_grant_submit_to_player(P_char,P_obj object,P_char,P_obj) {
 assert(object==&reward); ++granted; return grant_accepts;
}
bool item_creation_grant_submit_to_room(P_char,P_obj,int) { std::abort(); }
void extract_obj(P_obj object,int flag) { assert(object==&reward); ++extracted; extracted_flag=flag; }
void send_to_char_f(P_char,const char *fmt,...) {
 char buf[512]; va_list args; va_start(args,fmt); std::vsnprintf(buf,sizeof(buf),fmt,args); va_end(args);
 told+=buf;
}
void send_to_char(const char *text,P_char,int) { told+=text; }
int total_carried_weight(P_char) { return 0; }
int CAN_CARRY_N_stub=10;
#undef CAN_CARRY_N
#define CAN_CARRY_N(ch) CAN_CARRY_N_stub
#undef CAN_CARRY_W
#define CAN_CARRY_W(ch) 1000
// FUNCTIONS
static void reset(int error,bool accepts) {
 creation_error=error; grant_accepts=accepts; created=granted=extracted=0; extracted_flag=-1; told.clear();
}
int main() {
 objects[0].virtual_number=28973;
 reward.R_num=0; reward.short_description=(char *)"Rage";
 char_data mob{}, player{};
 char name[]="Ras", mob_name[]="Thrund";
 player.player.name=name; mob.player.short_descr=mob_name;
 npc_only_data npc{}; mob.only.npc=&npc; mob.specials.act=ACT_ISNPC;
 goal_data goal{}; goal.goal_type=QUEST_GOAL_ITEM; goal.number=28973;
 quest_complete_data completion{}; completion.receive=&goal;

 reset(0,true);
 give_reward(&completion,&mob,&player);
 assert(created==1 && granted==1 && !extracted && told.empty());

 reset(EEXIST,true);
 give_reward(&completion,&mob,&player);
 assert(created==1 && !granted && !extracted);
 assert(told.find("already present or reserved")!=std::string::npos);

 reset(ENODATA,true);
 give_reward(&completion,&mob,&player);
 assert(!granted && told.find("identity unresolved")!=std::string::npos);

 reset(0,false);
 give_reward(&completion,&mob,&player);
 assert(granted==1 && extracted==1 && extracted_flag==FALSE);
 puts("quest artifact rewards: created with identity, duplicates refused and explained, failed grants roll back");
}
'''
functions = extract_function(
    "quest.c", "void give_reward(struct quest_complete_data *qcp, P_char mob, P_char pl)")
with tempfile.TemporaryDirectory(prefix="quest-artifact-") as temp:
    test = Path(temp) / "quest.cpp"
    binary = test.with_suffix("")
    test.write_text(fixture.replace("// FUNCTIONS", functions))
    subprocess.run(["g++", "-std=c++20", "-g", "-O1", "-fsanitize=address,undefined",
                    "-ffunction-sections", "-fdata-sections", "-Isrc", "-I/usr/include/mysql",
                    str(test), str(HARNESS_STUBS), "-Wl,--gc-sections", "-o", str(binary)],
                   cwd=ROOT, check=True)
    subprocess.run([str(binary)], check=True, env=dict(os.environ,
                   ASAN_OPTIONS="detect_leaks=1:halt_on_error=1", UBSAN_OPTIONS="halt_on_error=1"))
