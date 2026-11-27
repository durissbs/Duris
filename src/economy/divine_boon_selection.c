#include "economy/divine_boon_selection.h"
#include "economy/divine_boon_runtime.h"

#include "combat/justice.h"
#include "core/prototypes.h"
#include "core/utils.h"
#include "world/db.h"
#include "world/map.h"
#include "world/epic.h"
#include "world/specs.prototypes.h"
#include "item/objmisc.h"
#include "sql/sql.h"
#include "ships/ships.h"
#include <map>
#include "world/world_quest_policy.h"

#include <algorithm>
#include <cmath>
#include <deque>
#include <new>
#include <optional>
#include <set>

extern P_room world;
extern zone_data *zone_table;
extern int top_of_world;
extern P_index obj_index;
extern P_obj object_list;
extern int top_of_objt;
extern int top_of_zone_table;

namespace
{
struct encounter
{
	int room;
	int level;
	int prototype;
};
struct candidate
{
	int number;
	int index;
	std::vector<encounter> encounters;
	bool experience = false;
	// Epic difficulty: the 75th-percentile level of the zone's reset mobs.
	int level = 0;
};
// Epic zones fit an online player of level L when rated L-5 to L+17.
constexpr int epic_levels_below = 5;
constexpr int epic_levels_above = 17;
std::vector<candidate> catalogue;
std::optional<divine_boon_state> prepared_hour;
// Ring of the last eight durably published hourly decisions (current boot only).
constexpr size_t diagnostic_limit = 8;
std::deque<divine_boon_selection_diagnostic> diagnostic_history;
std::optional<divine_boon_selection_diagnostic> prepared_diagnostic;
std::optional<divine_boon_selection_diagnostic> submitted_diagnostic;

void confirm_diagnostic(const divine_boon_state &state, bool pending)
{
	if (!submitted_diagnostic || pending)
		return;
	if (state.revision == submitted_diagnostic->committed_revision &&
	    state.last_opportunity == submitted_diagnostic->hour)
	{
		if (diagnostic_history.size() == diagnostic_limit)
			diagnostic_history.pop_front();
		try
		{
			diagnostic_history.push_back(*submitted_diagnostic);
		}
		catch (const std::bad_alloc &)
		{
			return; // Retry recording this confirmed diagnostic on the next tick.
		}
	}
	// Never report a refused, superseded or failed write as committed.
	if (state.revision >= submitted_diagnostic->committed_revision)
		submitted_diagnostic.reset();
}
std::array<std::array<int32_t, DIVINE_BOON_SLOTS>, 2> reward_zones = {};
uint64_t reward_revision = 0;
int64_t reward_checked_at = -1;

bool live_zone(const candidate &zone)
{
	if (!zone_table || zone.index < 0 || zone.index > top_of_zone_table)
		return false;
	const auto &current = zone_table[zone.index];
	return current.number == zone.number && current.reset_mode > 0 &&
	       !IS_SET(current.flags,
		       ZONE_TOWN | ZONE_SAFE | ZONE_MAP | ZONE_CLOSED | ZONE_PRIVATE) &&
	       current.hometown == 0;
}

// Zones need no proved walking route from a hometown: portals, boats, keys and
// secret doors are ordinary ways in. The zone's own fighting rooms decide.
double suitable_level(const candidate &zone)
{
	if (!live_zone(zone))
		return 0;
	std::set<int> rooms;
	std::set<int> prototypes;
	double total = 0;
	unsigned count = 0;
	for (const auto &mob : zone.encounters)
		if (mob.room >= 0 && mob.room <= top_of_world &&
		    world[mob.room].zone == zone.index &&
		    !IS_SET(world[mob.room].room_flags, ROOM_SAFE | ROOM_NO_MOB) &&
		    !IS_SURFACE_MAP(mob.room) && !IS_UD_MAP(mob.room))
		{
			rooms.insert(mob.room);
			prototypes.insert(mob.prototype);
			total += mob.level;
			++count;
		}
	return rooms.size() >= 3 && prototypes.size() >= 2 && count >= 5 ? total / count : 0;
}

bool payable_epic_stone(P_obj obj)
{
	return obj && obj_index && obj->R_num >= 0 && obj->R_num <= top_of_objt &&
	       obj_index[obj->R_num].func.obj == epic_stone && obj->obj_uid && OBJ_MAGIC(obj) &&
	       obj->value[0] > 0 && obj->value[1] > 0;
}

using epic_stone_locations = std::set<std::pair<int, int>>;

bool epic_target(const candidate &zone, std::optional<epic_stone_locations> *locations = nullptr)
{
	zone_info metadata;
	if (!live_zone(zone) || !get_zone_info(zone.number, &metadata) ||
	    metadata.epic_payout <= 0 || metadata.suggested_group_size <= 0 ||
	    metadata.stonecount <= 0 || metadata.epic_type < EPIC_ZONE_TYPE_SMALL ||
	    metadata.epic_type > EPIC_ZONE_TYPE_MONOLITH || epic_zone_done_now(zone.number) ||
	    zone.level <= 0)
		return false;
	// A defeated carrier leaves the same stone in a corpse, then the player
	// takes it. Follow the existing container/carrier location; do not withdraw
	// the bonus precisely when the group completes that legitimate stone.
	if (locations)
	{
		// Only catalogue-wide selection/status passes share this snapshot. Build
		// it lazily after a candidate zone is found, and discard it each pass.
		if (!*locations)
		{
			locations->emplace();
			for (P_obj obj = object_list; obj; obj = obj->next)
				if (payable_epic_stone(obj))
					(*locations)->emplace(obj->value[2], obj_zone_id(obj));
		}
		return (*locations)->contains({ zone.number, zone.index });
	}
	for (P_obj obj = object_list; obj; obj = obj->next)
		if (payable_epic_stone(obj) && obj->value[2] == zone.number &&
		    obj_zone_id(obj) == zone.index)
			return true;
	return false;
}

std::string category_list(unsigned categories)
{
	constexpr const char *names[] = { "none", "experience", "epic", "cargo", "pvp" };
	std::string text;
	for (unsigned category = 1; category < std::size(names); ++category)
		if (categories & (1u << category))
			text += (text.empty() ? "" : "+") + std::string(names[category]);
	return text.empty() ? "none" : text;
}

// Either side's recent use makes the shared hotspot less desirable.
bool repeated(const divine_boon_state &state, int zone, int64_t now)
{
	return std::any_of(
		state.history.begin(), state.history.end(), [&](const auto &old)
		{ return old.zone == zone && now < old.expired_at + DIVINE_BOON_REPEAT_WINDOW; });
}
}

bool divine_boon_selection_bootstrap()
try
{
	catalogue.clear();
	prepared_hour.reset();
	prepared_diagnostic.reset();
	submitted_diagnostic.reset();
	diagnostic_history.clear();
	reward_revision = 0;
	reward_checked_at = -1;
	if (!world || !zone_table || top_of_world < 0 || top_of_zone_table < 0)
		return false;
	for (int index = 0; index <= top_of_zone_table; ++index)
	{
		// Exp uses the bartender-quest vetting (normal zone, not a town, not
		// explicitly denied, valid average level). Epics need their live stone.
		const int number = zone_table[index].number;
		candidate zone{ number, index, {}, !world_quest_policy_zone_is_invalid(number) };
		if (!live_zone(zone) || !zone_table[index].cmd)
			continue;
		for (auto *reset = zone_table[index].cmd; reset->command != 'S'; ++reset)
		{
			if (reset->command != 'M' || reset->arg2 <= 0 || reset->arg4 < 50)
				continue;
			const int level = world_quest_policy_combat_level(reset->arg1);
			if (level < 5)
				continue;
			zone.encounters.push_back({ reset->arg3, level, reset->arg1 });
		}
		if (zone.encounters.size() < 5)
			continue;
		std::vector<int> levels;
		for (const auto &mob : zone.encounters)
			levels.push_back(mob.level);
		std::ranges::sort(levels);
		zone.level = levels[levels.size() * 3 / 4];
		catalogue.push_back(std::move(zone));
	}
	return true;
}
catch (const std::bad_alloc &)
{
	catalogue.clear();
	return false;
}

int32_t divine_boon_shared_target_zone(const divine_boon_slot &slot)
{
	if (slot.category != divine_boon_category::experience &&
	    slot.category != divine_boon_category::epic)
		return -1;
	// Normal shared slots and old one-sided slots need no world lookup.
	if (slot.zones[0] == slot.zones[1] || slot.zones[0] < 0 || slot.zones[1] < 0)
		return divine_boon_target_zone(slot);
	// Older state may contain different Good/Evil zones. Prefer a target that
	// is actually eligible/payable for BOTH sides; otherwise advertise one
	// consistent target to both rather than silently hiding the active boon.
	auto eligible = [&](int32_t zone, unsigned side)
	{
		return zone >= 0 && (slot.category == divine_boon_category::epic ?
					     divine_boon_epic_zone_eligible(zone, side) :
					     divine_boon_zone_eligible(zone, side));
	};
	for (int32_t zone : slot.zones)
		if (eligible(zone, 0) && eligible(zone, 1))
			return zone;
	for (int32_t zone : slot.zones)
		if (eligible(zone, 0) || eligible(zone, 1))
			return zone;
	return divine_boon_target_zone(slot);
}

bool divine_boon_zone_eligible(int zone_number, unsigned side)
{
	for (const auto &zone : catalogue)
		if (zone.number == zone_number)
			return side < 2 && zone.experience && suitable_level(zone) > 0;
	return false;
}

bool divine_boon_epic_zone_eligible(int zone_number, unsigned side)
{
	for (const auto &zone : catalogue)
		if (zone.number == zone_number)
			return side < 2 && live_zone(zone) && epic_target(zone);
	return false;
}

bool divine_boon_reward_zone_eligible(int zone_number, unsigned side)
{
	const auto *state = divine_boon_runtime_state();
	return state && state->revision == reward_revision && side < reward_zones.size() &&
	       zone_number >= 0 &&
	       std::find(reward_zones[side].begin(), reward_zones[side].end(), zone_number) !=
		       reward_zones[side].end();
}

bool divine_boon_cargo_eligible(const divine_boon_slot &slot)
{
	if (slot.category != divine_boon_category::cargo || slot.source_port < 0 ||
	    slot.source_port >= NUM_PORTS || slot.destination_port < 0 ||
	    slot.destination_port >= NUM_PORTS || slot.source_port == slot.destination_port ||
	    slot.cargo_commodity != slot.source_port ||
	    slot.contraband_commodity != slot.source_port)
		return false;
	// Any port pair qualifies, whichever faction lives nearby. Only require that
	// both ports exist in the loaded world with their ship shops.
	if (!world)
		return false;
	const int source = real_room(ports[slot.source_port].loc_room),
		  destination = real_room(ports[slot.destination_port].loc_room);
	return source >= 0 && source <= top_of_world && destination >= 0 &&
	       destination <= top_of_world && world[source].funct == ship_shop_proc &&
	       world[destination].funct == ship_shop_proc;
}

bool divine_boon_select(const divine_boon_state &published, int64_t now,
			const std::array<divine_boon_population, 2> &population,
			std::vector<divine_boon_slot> *choices,
			divine_boon_selection_diagnostic *diagnostic, divine_boon_category only)
try
{
	if (!choices || published.mode != divine_boon_mode::divine || published.paused ||
	    !divine_boon_state_valid(published) || top_of_world < 0)
		return false;
	choices->clear();
	auto state = published;
	divine_boon_reconcile(
		&state, now,
		divine_boon_shared_target_zone); // Newly expired objectives join repeat history.
	if (diagnostic)
		diagnostic->population = population;
	unsigned experience_boons = 0, epic_boons = 0;
	bool has_cargo = false, has_pvp = false;
	for (const auto &slot : state.slots)
		if (divine_boon_active(slot, now))
		{
			has_cargo |= slot.category == divine_boon_category::cargo;
			has_pvp |= slot.category == divine_boon_category::pvp;
			experience_boons += slot.category == divine_boon_category::experience;
			epic_boons += slot.category == divine_boon_category::epic;
		}

	// Each type has three reserved allocations, independent of the other type.
	// Activity on either side influences the shared objective level.
	std::vector<uint16_t> levels;
	for (const auto &side : population)
		levels.insert(levels.end(), side.levels.begin(), side.levels.end());
	const bool online = !levels.empty();
	if (experience_boons < DIVINE_BOON_EXPERIENCE_SLOTS || epic_boons < DIVINE_BOON_EPIC_SLOTS)
	{
		std::vector<double> fit(catalogue.size());
		for (size_t index = 0; index < catalogue.size(); ++index)
			fit[index] = suitable_level(catalogue[index]);
		std::set<int32_t> occupied_targets;
		for (const auto &slot : state.slots)
			if (divine_boon_active(slot, now) &&
			    (slot.category == divine_boon_category::experience ||
			     slot.category == divine_boon_category::epic))
				occupied_targets.insert(divine_boon_shared_target_zone(slot));
		int new_experience_zone = -1;
		auto open = [&](size_t index, bool relaxed)
		{
			const int zone = catalogue[index].number;
			return zone != new_experience_zone && !occupied_targets.contains(zone) &&
			       (relaxed || !repeated(state, zone, now));
		};

		auto shuffle = [](std::vector<uint16_t> &draws)
		{
			for (size_t next = 0; next < draws.size(); ++next)
				std::swap(draws[next],
					  draws[number(static_cast<int>(next),
						       static_cast<int>(draws.size()) - 1)]);
		};

		if ((only == divine_boon_category::none ||
		     only == divine_boon_category::experience) &&
		    experience_boons < DIVINE_BOON_EXPERIENCE_SLOTS && online)
		{
			// Draw eligible online players' levels from either side without
			// replacement, so one level without a fitting zone does not waste
			// the hour. Keep the existing -5/+3 range, widening at most ten levels.
			// A recently used zone is only taken when no level has a fresh one.
			shuffle(levels);
			for (unsigned relaxed = 0; relaxed < 2 && new_experience_zone < 0;
			     ++relaxed)
				for (size_t next = 0;
				     next < levels.size() && new_experience_zone < 0; ++next)
					for (int spread = 0;
					     spread <= 10 && new_experience_zone < 0; spread += 5)
					{
						const int level = levels[next];
						unsigned count = 0;
						for (size_t index = 0; index < catalogue.size();
						     ++index)
						{
							// Match the chosen player's side-independent zone level
							// without requiring opposing players to be online.
							const double typical = fit[index];
							if (catalogue[index].experience &&
							    typical > 0 && open(index, relaxed) &&
							    typical >= level - 5 - spread &&
							    typical <= level + 3 + spread &&
							    number(1, ++count) == 1)
								new_experience_zone =
									catalogue[index].number;
						}
						if (diagnostic && count)
							for (unsigned side = 0; side < 2; ++side)
							{
								diagnostic->experience_candidates
									[side] = count;
								diagnostic
									->experience_relaxed[side] =
									relaxed;
							}
					}
			if (new_experience_zone >= 0)
			{
				divine_boon_slot exp;
				exp.category = divine_boon_category::experience;
				exp.zones = { new_experience_zone, new_experience_zone };
				choices->push_back(exp);
			}
		}

		// With nobody online, accounts active in the last 15 minutes choose
		// the level, and at most one Epic objective waits for them. With
		// neither, no Epic objective is made, so none waits at a random level.
		std::vector<uint16_t> epic_levels = levels;
		if (!online)
			for (const auto &side : population)
				epic_levels.insert(epic_levels.end(), side.recent_levels.begin(),
						   side.recent_levels.end());
		if ((only == divine_boon_category::none || only == divine_boon_category::epic) &&
		    epic_boons < DIVINE_BOON_EPIC_SLOTS && !epic_levels.empty() &&
		    (online || epic_boons == 0))
		{
			std::optional<epic_stone_locations> stones;
			std::vector<size_t> live;
			for (size_t index = 0; index < catalogue.size(); ++index)
				if (epic_target(catalogue[index], &stones))
					live.push_back(index);
			// Like Experience, draw levels without replacement. A zone rated L-5
			// to L+17 fits level L, so groups also see zones above their level.
			// A recently used zone is only taken when no level has a fresh one.
			shuffle(epic_levels);
			int selected = -1;
			for (unsigned relaxed = 0; relaxed < 2 && selected < 0; ++relaxed)
				for (size_t next = 0; next < epic_levels.size() && selected < 0;
				     ++next)
				{
					const int low = epic_levels[next] - epic_levels_below;
					const int high = epic_levels[next] + epic_levels_above;
					unsigned count = 0;
					for (size_t index : live)
						if (catalogue[index].level >= low &&
						    catalogue[index].level <= high &&
						    open(index, relaxed) && number(1, ++count) == 1)
							selected = catalogue[index].number;
					if (diagnostic && count)
						for (unsigned side = 0; side < 2; ++side)
						{
							diagnostic->epic_candidates[side] = count;
							diagnostic->epic_relaxed[side] = relaxed;
						}
				}
			if (selected >= 0)
			{
				divine_boon_slot epic;
				epic.category = divine_boon_category::epic;
				epic.zones = { selected, selected };
				choices->push_back(epic);
			}
		}
	}

	const bool pvp =
		(only == divine_boon_category::none || only == divine_boon_category::pvp) &&
		population[0].pvp_accounts && population[1].pvp_accounts && !has_pvp;
	if (diagnostic)
		diagnostic->pvp_eligible = pvp;
	if (pvp)
	{
		choices->emplace_back();
		choices->back().category = divine_boon_category::pvp;
	}

	if ((only == divine_boon_category::none || only == divine_boon_category::cargo) &&
	    !has_cargo)
	{
		// Uniformly random over every source/destination pair in the port list.
		divine_boon_slot route;
		unsigned count = 0;
		for (int source = 0; source < NUM_PORTS; ++source)
			for (int destination = 0; destination < NUM_PORTS; ++destination)
			{
				divine_boon_slot candidate;
				candidate.category = divine_boon_category::cargo;
				candidate.source_port = candidate.cargo_commodity =
					candidate.contraband_commodity = source;
				candidate.destination_port = destination;
				if (divine_boon_cargo_eligible(candidate) &&
				    number(1, ++count) == 1)
					route = candidate;
			}
		if (diagnostic)
			diagnostic->cargo_routes = count;
		if (count)
			choices->push_back(route);
	}

	if (diagnostic)
		diagnostic->sampled = true;
	return !choices->empty();
}
catch (const std::bad_alloc &)
{
	return false;
}

bool divine_boon_regenerate_selection(divine_boon_state *state, size_t slot, int64_t now)
try
{
	return divine_boon_regenerate(
		state, slot, now,
		[](const divine_boon_state &candidate, int64_t at, divine_boon_category only,
		   std::vector<divine_boon_slot> *choices)
		{
			return divine_boon_select(candidate, at, divine_boon_activity_population(),
						  choices, nullptr, only);
		},
		divine_boon_shared_target_zone);
}
catch (const std::bad_alloc &)
{
	return false;
}

void divine_boon_selection_status(P_char ch)
{
	if (!ch || IS_NPC(ch) || !IS_TRUSTED(ch))
		return;
	std::optional<epic_stone_locations> stones;
	unsigned exp = 0, epic = 0;
	for (const auto &zone : catalogue)
	{
		if (zone.experience && suitable_level(zone) > 0 && ++exp <= 2)
			send_to_char_f(ch, "divine_exp zone=%d\r\n", zone.number);
		if (epic_target(zone, &stones))
		{
			++epic;
			send_to_char_f(ch, "divine_target zone=%d level=%d\r\n", zone.number,
				       zone.level);
		}
	}
	send_to_char_f(ch, "divine_targets experience=%u epic=%u\r\n", exp, epic);
	unsigned routes = 0;
	for (int source = 0; source < NUM_PORTS; ++source)
		for (int destination = 0; destination < NUM_PORTS; ++destination)
		{
			divine_boon_slot route;
			route.category = divine_boon_category::cargo;
			route.source_port = route.cargo_commodity = route.contraband_commodity =
				source;
			route.destination_port = destination;
			routes += divine_boon_cargo_eligible(route);
		}
	send_to_char_f(ch, "divine_routes count=%u of %d\r\n", routes, NUM_PORTS * (NUM_PORTS - 1));

	const auto *published = divine_boon_runtime_state();
	if (published)
		confirm_diagnostic(*published, divine_boon_runtime_pending());
	if (diagnostic_history.empty())
		send_to_char_f(ch, "divine_diag history=empty scope=current-boot\r\n");
	else
	{
		const auto &latest = diagnostic_history.back();
		send_to_char_f(
			ch, "divine_diag last_hour=%lld outcome=%s categories=%s revision=%llu\r\n",
			static_cast<long long>(latest.hour), latest.reason,
			category_list(latest.categories).c_str(),
			static_cast<unsigned long long>(latest.committed_revision));
		for (unsigned side = 0; side < latest.population.size(); ++side)
		{
			if (!latest.sampled)
			{
				send_to_char_f(
					ch,
					"divine_diag side=%s active=N/A online=N/A levels=N/A "
					"pvp=N/A exp_candidates=N/A epic_candidates=N/A\r\n",
					side ? "evil" : "good");
				continue;
			}
			const auto &p = latest.population[side];
			send_to_char_f(ch,
				       "divine_diag side=%s active=%u online=%zu "
				       "levels=1-20:%u,21-35:%u,36-50:%u,51+:%u\r\n",
				       side ? "evil" : "good", p.active_accounts, p.levels.size(),
				       p.brackets[0], p.brackets[1], p.brackets[2], p.brackets[3]);
			send_to_char_f(
				ch,
				"divine_diag side=%s pvp=%u exp_candidates=%u epic_candidates=%u "
				"exp_history=%s epic_history=%s\r\n",
				side ? "evil" : "good", p.pvp_accounts,
				latest.experience_candidates[side], latest.epic_candidates[side],
				latest.experience_relaxed[side] ? "relaxed" : "strict",
				latest.epic_relaxed[side] ? "relaxed" : "strict");
		}
		if (latest.sampled)
			send_to_char_f(
				ch, "divine_diag sampled=yes pvp_eligible=%s cargo_routes=%u\r\n",
				latest.pvp_eligible ? "yes" : "no", latest.cargo_routes);
		else
			send_to_char_f(
				ch, "divine_diag sampled=no pvp_eligible=N/A cargo_routes=N/A\r\n");
		for (const auto &item : diagnostic_history)
			send_to_char_f(ch,
				       "divine_diag recent hour=%lld outcome=%s categories=%s\r\n",
				       static_cast<long long>(item.hour), item.reason,
				       category_list(item.categories).c_str());
	}
	if (submitted_diagnostic)
		send_to_char_f(ch, "divine_diag hour=%lld status=pending-durable-write\r\n",
			       static_cast<long long>(submitted_diagnostic->hour));
	else if (prepared_diagnostic)
		send_to_char_f(ch, "divine_diag hour=%lld status=queue-refused\r\n",
			       static_cast<long long>(prepared_diagnostic->hour));
}

void divine_boon_selection_pulse(int64_t now)
try
{
	const auto *state = divine_boon_runtime_state();
	if (!state || state->mode != divine_boon_mode::divine || now < 0)
	{
		reward_revision = 0;
		return;
	}
	// Rewards are recomputed at most once per second (and after publication),
	// never from a per-hit reward calculation. Epic stones pay through
	// divine_boon_epic_zone_eligible instead of this cache.
	if (reward_revision != state->revision || reward_checked_at != now)
	{
		for (auto &side : reward_zones)
			side.fill(-1);
		for (size_t index = 0; index < state->slots.size(); ++index)
		{
			const auto &slot = state->slots[index];
			if (slot.category != divine_boon_category::experience ||
			    !divine_boon_active(slot, now))
				continue;
			const int zone = divine_boon_shared_target_zone(slot);
			if (zone >= 0 && divine_boon_zone_eligible(zone, 0))
				for (auto &side : reward_zones)
					side[index] = zone;
		}
		reward_revision = state->revision;
		reward_checked_at = now;
	}
	confirm_diagnostic(*state, divine_boon_runtime_pending());
	if (divine_boon_runtime_pending())
		return;
	const int64_t hour = now - now % DIVINE_BOON_HOUR;
	if (prepared_hour && prepared_hour->revision != state->revision + 1)
	{
		prepared_hour.reset(); // A staff mutation superseded this unsubmitted choice.
		prepared_diagnostic.reset();
	}
	if (hour <= state->last_opportunity)
		return;
	if (!prepared_hour || hour > prepared_hour->last_opportunity)
	{
		auto next = *state;
		std::vector<divine_boon_slot> choices;
		divine_boon_selection_diagnostic report;
		report.hour = hour;
		// A refused queue retains the exact choice for this hour. Once the hour
		// has passed, skip it and consume the present opportunity without RNG.
		if (prepared_hour || hour - state->last_opportunity != DIVINE_BOON_HOUR)
			report.reason = "missed-hour";
		else if (state->paused)
			report.reason = "paused";
		else
		{
			auto candidates = *state;
			divine_boon_reconcile(
				&candidates, now,
				divine_boon_shared_target_zone); // Include just-expired repeat history.
			if (std::any_of(candidates.slots.begin(), candidates.slots.end(),
					[](const auto &slot) { return !slot.id; }))
			{
				const bool selected = divine_boon_select(
					candidates, now, divine_boon_activity_population(),
					&choices, &report);
				report.reason = selected       ? "generated" :
						report.sampled ? "no-eligible-category" :
								 "selector-unavailable";
			}
			else
				report.reason = "slots-full";
		}
		if (!divine_boon_rotate(&next, now, choices.data(), choices.size(),
					divine_boon_shared_target_zone))
			return;
		for (const auto &choice : choices)
			report.categories |= 1u << static_cast<unsigned>(choice.category);
		prepared_diagnostic = report;
		prepared_hour = std::move(next);
	}
	// New objectives carry IDs from the published next_id onward. Drop any whose
	// target became invalid before admission, keeping the rest; never reroll.
	std::vector<divine_boon_slot> valid;
	bool invalidated = false;
	for (const auto &slot : prepared_hour->slots)
	{
		if (!slot.id || slot.id < state->next_id)
			continue;
		bool targets_valid = slot.category != divine_boon_category::cargo ||
				     divine_boon_cargo_eligible(slot);
		if (slot.category == divine_boon_category::experience ||
		    slot.category == divine_boon_category::epic)
		{
			const int zone = divine_boon_target_zone(slot);
			// Reject a half-shared or inaccessible candidate before publication.
			targets_valid = zone >= 0 && slot.zones[0] == slot.zones[1];
			for (unsigned side = 0; side < 2 && targets_valid; ++side)
				targets_valid = slot.category == divine_boon_category::epic ?
							divine_boon_epic_zone_eligible(zone, side) :
							divine_boon_zone_eligible(zone, side);
		}
		if (!targets_valid)
		{
			invalidated = true;
			continue;
		}
		auto choice = slot;
		choice.id = 0;
		choice.starts_at = choice.expires_at = 0;
		valid.push_back(choice);
	}
	if (invalidated)
	{
		auto kept = *state;
		if (!divine_boon_rotate(&kept, now, valid.data(), valid.size(),
					divine_boon_shared_target_zone))
			return;
		prepared_hour = std::move(kept);
		if (prepared_diagnostic)
		{
			prepared_diagnostic->reason = "target-invalidated";
			prepared_diagnostic->categories = 0;
			for (const auto &choice : valid)
				prepared_diagnostic->categories |=
					1u << static_cast<unsigned>(choice.category);
		}
	}
	if (divine_boon_runtime_queue(*prepared_hour))
	{
		if (prepared_diagnostic)
		{
			prepared_diagnostic->committed_revision = prepared_hour->revision;
			submitted_diagnostic = *prepared_diagnostic;
		}
		prepared_diagnostic.reset();
		prepared_hour.reset();
	}
}
catch (const std::bad_alloc &)
{
	// No durable admission happened; published state remains the authority.
	logit(LOG_STATUS, "Divine Boon selection deferred: insufficient memory.");
}
