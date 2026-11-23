#include "economy/shop_price.h"

float shop_charisma_bonus(int cha_modifier, bool same_race, bool selling)
{
	float bonus = cha_modifier / 100.f;
	if (!same_race)
		bonus = (bonus > 0 || selling) ? bonus / 2 : bonus * 2;
	return bonus < shop_max_charisma_bonus ? bonus : shop_max_charisma_bonus;
}

float shop_sell_factor(float buy_percent, float charisma_bonus)
{
	return buy_percent * (1 + charisma_bonus);
}

float shop_buy_factor(float sell_percent, float charisma_bonus)
{
	return sell_percent * (1 - charisma_bonus);
}
