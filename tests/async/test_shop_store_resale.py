#!/usr/bin/env python3
"""Shops refuse to buy back goods bought in a shop, before pricing them.

Discounts (epic shop bonus, barter, charisma) can take a shop's price below what
another shop pays, so buying in one and selling in another would make money. The
production shop_refuses_resale() is compiled against stub objects; guild-store gear,
priced to resell at a tenth, stays sellable.
"""
import subprocess
import tempfile
from pathlib import Path

from _paths import extract_function

for name in ("void shopping_sell(", "void shopping_value("):
    body = extract_function("economy/shop.c", name)
    assert body.index("shop_refuses_resale(temp1)") < body.index("cost_factor ="), name
print("[PASS] sell and value refuse store goods before pricing them")

PROGRAM = r'''
#include <cassert>
#include <cstdio>
#define BIT_13 0x2000
#define ITEM2_STOREITEM BIT_13
#define IS_SET(flag, bit) ((flag) & (bit))
#define IS_OBJ_STAT2(obj, stat) (IS_SET((obj)->extra2_flags, stat))
struct obj_data { unsigned extra2_flags = 0; bool guild_store = false; };
typedef obj_data *P_obj;
bool kingdom_store_piece(const obj_data *obj) { return obj->guild_store; }
''' + extract_function("economy/shop.c", "static bool shop_refuses_resale(") + r'''
int main() {
    obj_data looted, bought, guild;
    bought.extra2_flags = ITEM2_STOREITEM;
    guild.extra2_flags = ITEM2_STOREITEM;
    guild.guild_store = true;
    assert(!shop_refuses_resale(&looted));
    assert(shop_refuses_resale(&bought));
    assert(!shop_refuses_resale(&guild));
    puts("[PASS] store goods are refused; looted items and guild-store gear sell");
}
'''

with tempfile.TemporaryDirectory(prefix="shop-store-resale-") as temporary:
    cpp, binary = Path(temporary) / "test.cpp", Path(temporary) / "test"
    cpp.write_text(PROGRAM)
    subprocess.run(["g++", "-std=c++20", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-g", str(cpp), "-o", str(binary)], check=True)
    print(subprocess.check_output([str(binary)], text=True).strip())
