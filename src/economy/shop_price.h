#ifndef DURIS_SHOP_PRICE_H
#define DURIS_SHOP_PRICE_H

// No shop pays more than this share of an item's cost, nor charges less than its full
// cost before discounts (boot_the_shops clamps both).
constexpr float shop_max_buy_percent = 0.8f;
constexpr float shop_min_sell_percent = 1.0f;

// Charisma improves shop prices by at most this share. Below 1/9, the best price a
// shop pays (0.8 x 1.1) stays under the cheapest it charges (1.0 x 0.9), so charisma
// alone never lets buying in one shop and selling in another make money.
constexpr float shop_max_charisma_bonus = 0.10f;

// The share charisma improves a price by; negative makes it worse. Another race gets
// half the bonus; its penalty is halved when selling and doubled when buying.
float shop_charisma_bonus(int cha_modifier, bool same_race, bool selling);

// What a shop pays for an item, and charges for one before other discounts, as a share
// of the item's cost.
float shop_sell_factor(float buy_percent, float charisma_bonus);
float shop_buy_factor(float sell_percent, float charisma_bonus);

#endif
