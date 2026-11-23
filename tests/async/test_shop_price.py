#!/usr/bin/env python3
"""Charisma makes shop prices better, up to 10%, and never opens a buy-low/sell-high loop."""
from pathlib import Path
import os
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
PROGRAM = r'''
#include "economy/shop_price.h"
#include <cassert>
#include <cmath>
#include <cstdio>
#include <initializer_list>
static bool near(float a, float b) { return std::fabs(a - b) < 1e-5f; }
int main() {
    // Every modifier in cha_app, and some beyond it.
    for (bool same_race : { true, false })
    {
        float last_sell = -1, last_buy = 100;
        for (int mod = -60; mod <= 80; ++mod)
        {
            const float sell = shop_sell_factor(shop_max_buy_percent,
                                                shop_charisma_bonus(mod, same_race, true));
            const float buy = shop_buy_factor(shop_min_sell_percent,
                                              shop_charisma_bonus(mod, same_race, false));
            // More charisma never makes a price worse.
            assert(sell >= last_sell && buy <= last_buy);
            last_sell = sell;
            last_buy = buy;
            // The best-paying shop never pays more than the cheapest one charges.
            assert(sell < buy);
            assert(shop_charisma_bonus(mod, same_race, true) <= shop_max_charisma_bonus);
            assert(shop_charisma_bonus(mod, same_race, false) <= shop_max_charisma_bonus);
        }
    }
    // Average charisma pays and charges the shop's rates.
    assert(near(shop_sell_factor(0.5f, shop_charisma_bonus(0, true, true)), 0.5f));
    assert(near(shop_buy_factor(1.5f, shop_charisma_bonus(0, true, false)), 1.5f));
    // High charisma (CHA 92-100, modifier 15) gets the full 10%.
    assert(near(shop_sell_factor(0.5f, shop_charisma_bonus(15, true, true)), 0.55f));
    assert(near(shop_buy_factor(1.5f, shop_charisma_bonus(15, true, false)), 1.35f));
    // Another race gets half the bonus.
    assert(near(shop_charisma_bonus(15, false, true), 0.075f));
    assert(near(shop_charisma_bonus(10, false, false), 0.05f));
    // Low-charisma penalties are unchanged: halved selling, doubled buying for another race.
    assert(near(shop_charisma_bonus(-10, true, true), -0.10f));
    assert(near(shop_charisma_bonus(-10, false, true), -0.05f));
    assert(near(shop_charisma_bonus(-10, false, false), -0.20f));
    puts("shop price: ok");
}
'''

output = ROOT / "bin/tests"
output.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix="shop-price-", dir=output) as temporary:
    directory = Path(temporary)
    source = directory / "test.cpp"
    binary = directory / "test"
    source.write_text(PROGRAM)
    subprocess.run([
        "g++", "-std=c++20", "-g", "-Wall", "-Wextra", "-Werror",
        "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
        "-I", str(ROOT / "src"), str(source), str(ROOT / "src/economy/shop_price.c"),
        "-o", str(binary),
    ], check=True)
    print(subprocess.check_output([str(binary)], text=True, timeout=15, env={
        **os.environ, "ASAN_OPTIONS": "detect_leaks=1:halt_on_error=1",
    }).strip())
