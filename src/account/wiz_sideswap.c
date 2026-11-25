/****************************************************************************
 *
 *  File: wiz_sideswap.c                                   Part of Duris
 *  Usage: wizard command to move a whole account to the other racewar side
 *
 *  Copyright 1990, 1991 - see LICENSE for complete information
 *
 *  Additions since 2025 by the Duris maintainers and since 2026 by
 *    LuminariMUD maintainers are public domain (Unlicense, see LICENSE)
 *
 *  Created by: Duris maintainers                          Date: 2026-11-25
 *
 ****************************************************************************/

/*
 * sideswap <account> <char>=<race>[:<class>] ... [+money] [+eq] [+epics] [+artis]
 *          [+locker] [+frags] [confirm]
 *
 * Moves every character of an offline account between good and evil. Each character
 * keeps its name and pid, so everything it owns stays linked to it, and is rebuilt
 * for its new race and class the way creation builds one: alignment, hometown,
 * height, weight and size, and skills (kept where the new class has them, otherwise
 * what a new character of that level has). Without the options, money goes to the
 * old side's bank, equipment is destroyed, artifacts return to the world, epics and
 * frags are reset and the account locker stays on the old side. Without confirm the
 * command only shows what it would do.
 */

#include "core/prototypes.h"
#include "core/structs.h"
#include "core/utils.h"
#include "net/comm.h"
#include "account/account.h"
#include "account/creation_availability_config.h"
#include "classes/epic_skills.h"
#include "core/files.h"
#include "core/utility.h"
#include "guild/assocs.h"
#include "guild/artifact_admission.h"
#include "item/item_removal_audit.h"
#include "magic/spells.h"
#include "player/player_load_items.h"
#include "player/player_load_offline.h"
#include "player/player_load_pipeline.h"
#include "player/player_revision_state.h"
#include "sql/sql.h"
#include "sql/sql_async.h"
#include "sql/sql_player.h"
#include "world/epic.h"
#include "world/epic_command.h"
#include "world/epic_transaction.h"

#include <cerrno>
#include <cstdlib>
#include <memory>
#include <set>
#include <string>
#include <utility>
#include <vector>

extern struct race_names race_names_table[];
extern struct class_names class_names_table[];
extern epic_reward epic_rewards[];
extern bool updateArtis;
extern Skill skills[];
extern P_desc descriptor_list;

namespace
{
struct sideswap_target
{
	std::string name;
	int pid = 0;
	player_revision_t loaded_revision = 0;
	player_revision_t saved_revision = 0;
	int race = 0;
	unsigned int m_class = 0; // 0 keeps the current class
	P_char ch = nullptr;
	// Worked out when the plan is checked.
	unsigned int new_class = 0;
	int64_t wallet = 0;
	int refund_points = 0;
	int refund_coins = 0;
	std::vector<int> refund_skills;
	int destroyed_items = 0;
	int artifacts = 0;
};

struct sideswap_request
{
	uint64_t staff = 0;
	std::string staff_name;
	std::string account;
	bool money = false, eq = false, epics = false, artis = false, locker = false, frags = false,
	     confirm = false;
	std::vector<sideswap_target> targets;
	int from_side = RACEWAR_NONE;
	int to_side = RACEWAR_NONE;
	size_t pending = 0;
	bool load_failed = false;
	// Each side's account locker: whether it exists and holds anything.
	bool locker_exists[RACEWAR_NEUTRAL + 1] = {};
	bool locker_used[RACEWAR_NEUTRAL + 1] = {};
};

std::set<std::string> accounts_in_progress;

std::string lower(std::string text)
{
	for (char &c : text)
		c = LOWER(c);
	return text;
}

std::string locker_name(const std::string &account, int side)
{
	return "account." + lower(account) + "." + std::to_string(side) + ".locker";
}

const char *side_name(int side)
{
	return side == RACEWAR_GOOD ? "good" : side == RACEWAR_EVIL ? "evil" : "neither side";
}

// A playable race by id, name, or the start of one name ("drow" for Drow Elf).
int find_race(const char *name)
{
	if (isdigit(*name))
		return atoi(name);
	int found = 0;
	for (int i = 0; playable_races[i].race_id != -1; ++i)
	{
		const int race = playable_races[i].race_id;
		if (!strcasecmp(name, race_names_table[race].normal) ||
		    !strcasecmp(name, race_names_table[race].no_spaces))
			return race;
		if (is_abbrev(name, race_names_table[race].normal) ||
		    is_abbrev(name, race_names_table[race].no_spaces))
			found = found ? -1 : race;
	}
	return found;
}

unsigned int find_class(const char *name)
{
	int index = 0;
	if (isdigit(*name))
		index = atoi(name);
	else
		for (int i = 1; i <= CLASS_COUNT && !index; ++i)
			if (class_names_table[i].normal &&
			    !strcasecmp(name, class_names_table[i].normal))
				index = i;
	return index >= 1 && index <= CLASS_COUNT ? 1U << (index - 1) : 0;
}

// The side a playable race can be on: RACEWAR_NONE when it is not playable, or both
// (RACEWAR_NEUTRAL) for the races that choose.
int race_side(int race)
{
	for (int i = 0; playable_races[i].race_id != -1; ++i)
		if (playable_races[i].race_id == race)
			return !strcmp(playable_races[i].faction, "good") ? RACEWAR_GOOD :
			       !strcmp(playable_races[i].faction, "evil") ? RACEWAR_EVIL :
									    RACEWAR_NEUTRAL;
	return RACEWAR_NONE;
}

bool class_allowed(int race, unsigned int m_class)
{
	return !m_class || creation_class_normally_available(race, flag2idx(m_class));
}

void tell(const sideswap_request &request, const std::string &text)
{
	if (P_char staff = find_character_by_runtime_id(request.staff))
		send_to_char(text.c_str(), staff);
}

void collect_artifacts(P_obj object, std::vector<P_obj> *found)
{
	for (; object; object = object->next_content)
	{
		collect_artifacts(object->contains, found);
		if (IS_ARTIFACT(object))
			found->push_back(object);
	}
}

// Offline loads contain the player's saved pets, not transient group followers.
std::vector<P_char> item_holders(P_char ch)
{
	std::vector<P_char> holders{ ch };
	for (auto *link = ch->followers; link; link = link->next)
		if (IS_NPC(link->follower))
			holders.push_back(link->follower);
	return holders;
}

// Post-order: a nested artifact is retired before its container.
std::vector<P_obj> artifacts_of(P_char ch)
{
	std::vector<P_obj> found;
	for (P_char holder : item_holders(ch))
	{
		for (int slot = 0; slot < MAX_WEAR; ++slot)
			if (holder->equipment[slot])
			{
				collect_artifacts(holder->equipment[slot]->contains, &found);
				if (IS_ARTIFACT(holder->equipment[slot]))
					found.push_back(holder->equipment[slot]);
			}
		collect_artifacts(holder->carrying, &found);
	}
	return found;
}

P_char artifact_holder(P_obj object)
{
	while (OBJ_INSIDE(object) && object->loc.inside)
		object = object->loc.inside;
	return OBJ_CARRIED(object) ? object->loc.carrying :
	       OBJ_WORN(object) ? object->loc.wearing : nullptr;
}

int count_objects(P_obj object)
{
	int count = 0;
	for (; object; object = object->next_content)
		count += 1 + count_objects(object->contains);
	return count;
}

// An epic skill the character could not have learned as it will be.
bool epic_skill_lost(P_char ch, int skill, int race, unsigned int m_class)
{
	for (int i = 0; epic_rewards[i].type; ++i)
		if (epic_rewards[i].type == EPIC_REWARD_SKILL && epic_rewards[i].value == skill)
			return (epic_rewards[i].classes &&
				!IS_SET(epic_rewards[i].classes, m_class) &&
				!IS_SET(epic_rewards[i].classes, ch->player.secondary_class)) ||
			       (race == RACE_THRIKREEN && skill == SKILL_DEVASTATING_CRITICAL);
	return false;
}

// Lets go of a character loaded off the loop: its items and pets leave with it.
void release_loaded(P_char ch)
{
	while (ch->followers)
	{
		P_char pet = ch->followers->follower;
		player_load_items_discard(pet);
		extract_char(pet);
	}
	player_load_items_discard(ch);
	free_char(ch);
}

void finish(const std::shared_ptr<sideswap_request> &request, bool unlock = true)
{
	for (sideswap_target &target : request->targets)
		if (target.ch)
		{
			release_loaded(target.ch);
			target.ch = nullptr;
		}
	if (unlock)
		accounts_in_progress.erase(lower(request->account));
}

bool account_in_game(const std::string &account)
{
	for (P_desc d = descriptor_list; d; d = d->next)
		if (d->account && d->account->acct_name &&
		    !strcasecmp(d->account->acct_name, account.c_str()))
			return true;
	return false;
}

bool any_in_game(const sideswap_request &request)
{
	if (account_in_game(request.account))
		return true;
	for (const sideswap_target &target : request.targets)
		if (is_pid_online(target.pid, TRUE) || player_load_pipeline_pid_pending(target.pid))
			return true;
	return false;
}

// Checks the loaded characters and works out what each one gains and loses. Returns
// the problems; empty when the swap can go ahead.
std::string check_plan(sideswap_request &request)
{
	std::string problems;
	request.from_side = request.targets[0].ch ? GET_RACEWAR(request.targets[0].ch) : 0;
	request.to_side = request.from_side == RACEWAR_GOOD ? RACEWAR_EVIL : RACEWAR_GOOD;
	if (request.from_side != RACEWAR_GOOD && request.from_side != RACEWAR_EVIL)
		return "Only good and evil characters can swap sides.\r\n";

	for (sideswap_target &target : request.targets)
	{
		P_char ch = target.ch;
		const std::string who = std::string(GET_NAME(ch)) + ": ";
		if (GET_RACEWAR(ch) != request.from_side)
			problems += who + "is not on the same side as the rest of the account.\r\n";
		if (GET_LEVEL(ch) >= MINLVLIMMORTAL)
			problems += who + "is an immortal.\r\n";
		const int side = race_side(target.race);
		if (side != request.to_side && side != RACEWAR_NEUTRAL)
			problems += who + race_names_table[target.race].normal + " is not " +
				    (request.to_side == RACEWAR_EVIL ? "an evil" : "a good") +
				    " race.\r\n";
		target.new_class = target.m_class ? target.m_class : ch->player.m_class;
		if (!class_allowed(target.race, target.new_class))
			problems += who + "that race cannot be a " +
				    class_names_table[flag2idx(target.new_class)].normal + ".\r\n";
		if (!class_allowed(target.race, ch->player.secondary_class))
			problems += who + "that race cannot have its secondary class.\r\n";

		if (Guild *guild = GET_ASSOC(ch))
		{
			unsigned int leaving = 0;
			for (const sideswap_target &other : request.targets)
				leaving += GET_ASSOC(other.ch) == guild;
			if (guild->get_num_members() <= leaving)
				problems += who +
					    "the account's characters are all that is left of " +
					    "its guild; delete the guild first.\r\n";
		}

		const auto artifacts = artifacts_of(ch);
		target.artifacts = static_cast<int>(artifacts.size());
		for (P_obj artifact : artifacts)
			if (!artifact_is_canonical(artifact) || !artifact_holder(artifact))
				problems += who + "has an artifact with invalid identity or custody.\r\n";
		if (target.artifacts && !request.artis && !updateArtis)
			problems += who +
				    "artifact updates are off, so its artifacts cannot return to "
				    "the world; use +artis or turn them on.\r\n";

		target.wallet = GET_MONEY(ch);
		if (!request.eq)
		{
			target.destroyed_items = 0;
			for (P_char holder : item_holders(ch))
			{
				target.destroyed_items += count_objects(holder->carrying);
				for (int slot = 0; slot < MAX_WEAR; ++slot)
					if (holder->equipment[slot])
						target.destroyed_items +=
							1 + count_objects(holder->equipment[slot]->contains);
			}
			target.destroyed_items -= target.artifacts;
		}

		target.refund_points = target.refund_coins = 0;
		target.refund_skills.clear();
		if (request.epics)
			for (int skill = FIRST_SKILL; skill <= LAST_SKILL; ++skill)
			{
				int points, coins;
				if (epic_skill_lost(ch, skill, target.race, target.new_class) &&
				    epic_skill_refund_value(ch, skill, &points, &coins))
				{
					target.refund_points += points;
					target.refund_coins += coins;
					target.refund_skills.push_back(skill);
				}
			}
	}

	if (request.locker)
	{
		if (account_locker_in_use(request.account.c_str(), request.from_side))
			problems += "The account locker is in use.\r\n";
		if (request.locker_used[request.to_side])
			problems += std::string("The account already has a ") +
				    side_name(request.to_side) +
				    " locker with items or private chests in it; empty it or leave "
				    "out +locker.\r\n";
	}
	return problems;
}

std::string describe_plan(const sideswap_request &request)
{
	char buf[MAX_STRING_LENGTH];
	std::string text;
	snprintf(buf, sizeof(buf), "&+WSideswap of account %s: %s to %s&n\r\n",
		 request.account.c_str(), side_name(request.from_side), side_name(request.to_side));
	text += buf;
	for (const sideswap_target &target : request.targets)
	{
		P_char ch = target.ch;
		snprintf(buf, sizeof(buf), "  %s: %s %s -> %s %s\r\n", GET_NAME(ch),
			 race_names_table[GET_RACE(ch)].normal,
			 class_names_table[flag2idx(ch->player.m_class)].normal,
			 race_names_table[target.race].normal,
			 class_names_table[flag2idx(target.new_class)].normal);
		text += buf;
		if (GET_ASSOC(ch))
			text += "    leaves its guild\r\n";
		snprintf(buf, sizeof(buf), "    money on hand: %s%s\r\n",
			 target.wallet ? coin_stringv(target.wallet) : "none",
			 request.money ? " (kept)" : " (to the old side's bank)");
		text += buf;
		if (!request.eq)
		{
			snprintf(
				buf, sizeof(buf),
				"    &+R%d item(s) destroyed&n (including pet gear and container contents)\r\n",
				target.destroyed_items);
			text += buf;
		}
		else
			text += "    equipment kept, moved to inventory to be worn again\r\n";
		if (target.artifacts)
		{
			snprintf(buf, sizeof(buf), "    %d artifact(s) %s\r\n", target.artifacts,
				 request.artis ? "kept" : "returned to the world");
			text += buf;
		}
		if (!request.epics)
		{
			snprintf(buf, sizeof(buf),
				 "    &+R%ld epic points and all epic skills reset&n\r\n",
				 ch->only.pc->epics);
			text += buf;
		}
		else if (!target.refund_skills.empty())
		{
			std::string names;
			for (int skill : target.refund_skills)
				names +=
					std::string(names.empty() ? "" : ", ") + skills[skill].name;
			snprintf(buf, sizeof(buf),
				 "    refunds %s: %d epic points, %s to the bank\r\n",
				 names.c_str(), target.refund_points,
				 coin_stringv(target.refund_coins));
			text += buf;
		}
		if (!request.frags)
		{
			snprintf(buf, sizeof(buf), "    frags reset (%ld)\r\n", ch->only.pc->frags);
			text += buf;
		}
	}
	text += request.money ? "  Account bank: moves to the new side.\r\n" :
				"  Account bank: stays on the old side.\r\n";
	text += !request.locker ? "  Account locker: stays on the old side.\r\n" :
		request.locker_exists[request.from_side] ?
				  "  Account locker: moves to the new side.\r\n" :
				  "  Account locker: there is none to move.\r\n";
	return text;
}

void add_to_bank(std::vector<std::string> *statements, const std::string &account, int side,
		 int64_t copper)
{
	if (copper <= 0)
		return;
	const int64_t platinum = copper / 1000, gold = copper % 1000 / 100,
		      silver = copper % 100 / 10, coins = copper % 10;
	statements->push_back(sql_format(
		"INSERT INTO account_banks (account_name,racewar,bank_copper,bank_silver,bank_gold,"
		"bank_platinum) VALUES ('%s',%d,%lld,%lld,%lld,%lld) ON DUPLICATE KEY UPDATE "
		"bank_revision=bank_revision+1,bank_copper=bank_copper+VALUES(bank_copper),"
		"bank_silver=bank_silver+VALUES(bank_silver),bank_gold=bank_gold+VALUES(bank_gold),"
		"bank_platinum=bank_platinum+VALUES(bank_platinum)",
		account.c_str(), side, static_cast<long long>(coins),
		static_cast<long long>(silver), static_cast<long long>(gold),
		static_cast<long long>(platinum)));
}

// The account's own rows, after its characters' saves: the side its characters are
// admitted by, no side-switch cooldown, and the bank and locker it takes along.
std::vector<std::string> account_statements(const sideswap_request &request)
{
	const std::string account = escape_str(request.account.c_str());
	const int from = request.from_side, to = request.to_side;
	std::vector<std::string> statements;
	std::string pids;
	for (const sideswap_target &target : request.targets)
		pids += (pids.empty() ? "" : ",") + std::to_string(target.pid);
	statements.push_back(sql_format(
		"UPDATE account_characters SET racewar=%d WHERE pid IN (%s) AND deleted_at IS NULL",
		account_admission_racewar(to, false), pids.c_str()));
	statements.push_back(sql_format("UPDATE accounts SET %s=NULL WHERE account_name='%s'",
					from == RACEWAR_GOOD ? "last_good_char" : "last_evil_char",
					account.c_str()));

	if (request.money)
	{
		statements.push_back(sql_format(
			"INSERT INTO account_banks (account_name,racewar,bank_copper,bank_silver,"
			"bank_gold,bank_platinum) SELECT source.account_name,%d,source.bank_copper,"
			"source.bank_silver,source.bank_gold,source.bank_platinum FROM account_banks "
			"AS source WHERE source.account_name='%s' AND source.racewar=%d "
			"ON DUPLICATE KEY UPDATE bank_revision=account_banks.bank_revision+1,"
			"bank_copper=account_banks.bank_copper+VALUES(bank_copper),"
			"bank_silver=account_banks.bank_silver+VALUES(bank_silver),"
			"bank_gold=account_banks.bank_gold+VALUES(bank_gold),"
			"bank_platinum=account_banks.bank_platinum+VALUES(bank_platinum)",
			to, account.c_str(), from));
		statements.push_back(sql_format(
			"UPDATE account_banks SET bank_copper=0,bank_silver=0,bank_gold=0,"
			"bank_platinum=0,bank_revision=bank_revision+1 WHERE account_name='%s' AND "
			"racewar=%d",
			account.c_str(), from));
	}
	for (const sideswap_target &target : request.targets)
	{
		if (!request.money)
			add_to_bank(&statements, account, from, target.wallet);
		add_to_bank(&statements, account, to, target.refund_coins);
	}
	// Boot refuses a bank without an opening baseline; a new row gets one.
	statements.push_back(sql_format(
		"INSERT IGNORE INTO currency_bank_baseline(bank_id,opening_copper,opening_silver,"
		"opening_gold,opening_platinum,opening_revision) SELECT id,bank_copper,bank_silver,"
		"bank_gold,bank_platinum,bank_revision FROM account_banks WHERE account_name='%s'",
		account.c_str()));

	if (request.locker && request.locker_exists[from])
	{
		const std::string old_name = escape_str(locker_name(request.account, from).c_str());
		const std::string new_name = escape_str(locker_name(request.account, to).c_str());
		// An empty new-side locker trades places with the old one.
		const std::string parked = old_name + ".sideswap";
		if (request.locker_exists[to])
			statements.push_back(
				sql_format("UPDATE lockers SET locker_name='%s',racewar=%d WHERE "
					   "locker_name='%s'",
					   parked.c_str(), from, new_name.c_str()));
		statements.push_back(sql_format(
			"UPDATE lockers SET locker_name='%s',racewar=%d WHERE locker_name='%s'",
			new_name.c_str(), to, old_name.c_str()));
		if (request.locker_exists[to])
			statements.push_back(sql_format(
				"UPDATE lockers SET locker_name='%s' WHERE locker_name='%s'",
				old_name.c_str(), parked.c_str()));
	}
	return statements;
}

// Rebuilds one loaded character on its new side and saves it.
bool swap_character(const sideswap_request &request, sideswap_target &target)
{
	P_char ch = target.ch;

	// Separate nested artifacts before retiring their containers. Use the
	// existing non-crumbling attachment path for offline inventory.
	for (P_obj artifact : artifacts_of(ch))
	{
		P_char holder = artifact_holder(artifact);
		if (!holder || !artifact_is_canonical(artifact))
			return false;
		if (OBJ_INSIDE(artifact))
		{
			obj_from_obj(artifact);
			obj_to_char_at_end(artifact, holder);
		}
		if (!request.artis)
		{
			// Gear retained by +eq must not vanish with an artifact container.
			while (request.eq && artifact->contains)
			{
				P_obj child = artifact->contains;
				obj_from_obj(child);
				obj_to_char_at_end(child, holder);
			}
			const uint64_t uid = artifact->obj_uid;
			poof_artifact(artifact);
			if (find_live_object(artifact, uid))
				return false;
		}
	}
	// Never purge a container when an artifact failed to retire.
	if (!request.artis && !artifacts_of(ch).empty())
		return false;

	for (P_char holder : item_holders(ch))
	{
		if (holder == ch || !request.eq)
			for (int slot = 0; slot < MAX_WEAR; ++slot)
				if (holder->equipment[slot])
					obj_to_char_at_end(unequip_char(holder, slot), holder);
		if (!request.eq)
			for (P_obj object = holder->carrying, next; object; object = next)
			{
				next = object->next_content;
				if (IS_ARTIFACT(object))
				{
					while (object->contains)
					{
						P_obj child = object->contains;
						audit_item_removal("sideswap", capture_item_removal(child));
						extract_obj(child, TRUE);
					}
					continue;
				}
				audit_item_removal("sideswap", capture_item_removal(object));
				extract_obj(object, TRUE);
			}
	}

	if (GET_ASSOC(ch))
		GET_ASSOC(ch)->secede(ch);

	if (!request.money)
	{
		CLEAR_MONEY(ch);
	}
	if (!request.frags)
		ch->only.pc->frags = 0;
	if (!request.epics)
	{
		if (ch->only.pc->epics)
			epic_transaction_submit(ch, -ch->only.pc->epics,
						epic_reason_type::admin_adjustment, 0, 0,
						critical_source_site::operator_repair,
						critical_deadline_class::terminal, nullptr, nullptr,
						0);
		ch->only.pc->epic_skill_points = 0;
		for (int skill = FIRST_SKILL; skill <= LAST_SKILL; ++skill)
			if (IS_EPIC_SKILL(skill) && !isname(skills[skill].name, "forge mine craft"))
				ch->only.pc->skills[skill].learned =
					ch->only.pc->skills[skill].taught = 0;
	}
	else if (target.refund_points)
	{
		for (int skill : target.refund_skills)
			ch->only.pc->skills[skill].learned = ch->only.pc->skills[skill].taught = 0;
		epic_transaction_submit(ch, target.refund_points,
					epic_reason_type::epic_skill_refund, 0, 0,
					critical_source_site::operator_repair,
					critical_deadline_class::terminal, nullptr, nullptr, 0);
	}

	// What creation sets from race and class.
	GET_RACE(ch) = target.race;
	GET_RACEWAR(ch) = request.to_side;
	if (target.new_class != ch->player.m_class)
	{
		ch->player.m_class = target.new_class;
		ch->player.spec = 0;
		forget_spells(ch, -1);
	}
	GET_ALIGNMENT(ch) = find_starting_alignment(GET_RACE(ch), ch->player.m_class);
	set_char_height_weight(ch);
	set_char_size(ch);
	find_starting_location(ch, find_hometown(GET_RACE(ch), true, GET_RACEWAR(ch)));
	GET_BIRTHPLACE(ch) = GET_ORIG_BIRTHPLACE(ch) = GET_HOME(ch);
	update_skills(ch);

	return writeCharacter(ch, RENT_CRASH2, real_room(GET_HOME(ch)));
}

// Keep the account fenced on any partial/ambiguous write. Staff must reconcile
// the affected characters before a restart releases the in-memory fence.
void apply_failed(const std::shared_ptr<sideswap_request> &request, const std::string &reason)
{
	std::string detail = reason + " Account bank, locker and side were not confirmed. "
			     "Account remains locked; reconcile the character saves before restarting.\r\n";
	for (const sideswap_target &target : request->targets)
		detail += target.name + (target.saved_revision ? ": save queued at revision " +
							     std::to_string(target.saved_revision) : ": no final save queued") +
			  "\r\n";
	tell(*request, detail);
	logit(LOG_WIZ, "sideswap: account %s needs reconciliation: %s",
	      request->account.c_str(), detail.c_str());
	finish(request, false);
}

std::string revision_query(const sideswap_request &request)
{
	std::string pids;
	for (const sideswap_target &target : request.targets)
		pids += (pids.empty() ? "" : ",") + std::to_string(target.pid);
	return "SELECT pid,save_revision,racewar FROM player_data WHERE pid IN (" + pids + ")";
}

bool revisions_match(const sql_rows &rows,
		     const std::vector<std::pair<int, player_revision_t>> &expected, int side)
{
	if (rows.size() != expected.size())
		return false;
	for (const auto &entry : expected)
	{
		bool found = false;
		for (const sql_row &row : rows)
			if (row[0] && row[1] && row[2] && atoi(row[0]) == entry.first &&
			    strtoull(row[1], nullptr, 10) == entry.second && atoi(row[2]) == side)
				found = true;
		if (!found)
			return false;
	}
	return true;
}

void apply(const std::shared_ptr<sideswap_request> &request)
{
	std::vector<std::pair<int, player_revision_t>> expected;
	for (sideswap_target &target : request->targets)
	{
		player_revision_snapshot revision = {};
		if (!swap_character(*request, target) ||
		    !player_revision_snapshot_copy(target.pid, &revision) ||
		    revision.acknowledged_revision <= target.loaded_revision)
		{
			apply_failed(request, "Could not queue the final save of " + target.name + ".");
			return;
		}
		target.saved_revision = revision.acknowledged_revision;
		expected.emplace_back(target.pid, target.saved_revision);
	}

	// The ordered writer runs this after the character saves. Verify their exact
	// durable revisions in the same transaction as the account updates.
	const auto statements = account_statements(*request);
	const auto query = revision_query(*request) + " FOR UPDATE";
	const int side = request->to_side;
	if (!sql_read_work(
		    [query, statements, expected, side](MYSQL *connection, sql_rows *rows) -> unsigned int
		    {
			    if (const auto error = sql_select(connection, query, rows))
				    return error;
			    if (!revisions_match(*rows, expected, side))
				    return EINVAL;
			    for (const auto &statement : statements)
				    if (const auto error = sql_execute(connection, statement))
					    return error;
			    return 0;
		    },
		    [request](bool ok, const sql_rows &)
		    {
			    if (!ok)
			    {
				    apply_failed(request, "Final account transaction failed or was uncertain.");
				    return;
			    }
			    char buf[MAX_STRING_LENGTH];
			    snprintf(buf, sizeof(buf),
				     "sideswap: %s moved account %s (%zu character(s)) to %s "
				     "[money:%d eq:%d epics:%d artis:%d locker:%d frags:%d]",
				     request->staff_name.c_str(), request->account.c_str(),
				     request->targets.size(), side_name(request->to_side),
				     request->money, request->eq, request->epics, request->artis,
				     request->locker, request->frags);
			    logit(LOG_WIZ, "%s", buf);
			    wizlog(GREATER_G, "%s", buf);
			    snprintf(buf, sizeof(buf), "Done: %zu of %zu character(s) saved on the %s side.\r\n",
				     request->targets.size(), request->targets.size(), side_name(request->to_side));
			    tell(*request, buf);
			    finish(request);
		    }))
		apply_failed(request, "Could not queue final account transaction.");
}

// Every character and the locker state are in: check, then show or apply the plan.
void all_loaded(const std::shared_ptr<sideswap_request> &request)
{
	if (request->load_failed)
	{
		tell(*request, "Could not load every character of the account (one may be logging "
			       "in). Nothing was changed.\r\n");
		finish(request);
		return;
	}
	const std::string problems = check_plan(*request);
	if (!problems.empty())
	{
		tell(*request, "Sideswap refused:\r\n" + problems);
		finish(request);
		return;
	}
	tell(*request, describe_plan(*request));
	if (!request->confirm)
	{
		tell(*request, "Nothing was changed. Add 'confirm' to do this.\r\n");
		finish(request);
		return;
	}
	std::vector<std::pair<int, player_revision_t>> expected;
	for (const sideswap_target &target : request->targets)
		expected.emplace_back(target.pid, target.loaded_revision);
	if (!sql_read(revision_query(*request),
		      [request, expected](bool ok, const sql_rows &rows)
		      {
			      if (!ok || any_in_game(*request) ||
				  !revisions_match(rows, expected, request->from_side))
			      {
				      tell(*request, "Account changed while loading. Nothing was changed.\r\n");
				      finish(request);
				      return;
			      }
			      for (const sideswap_target &target : request->targets)
			      {
				      player_revision_snapshot revision = {};
				      if (!player_revision_snapshot_copy(target.pid, &revision) ||
					  revision.written_revision != target.loaded_revision)
				      {
					      tell(*request, "Character save changed while loading. "
					     "Nothing was changed.\r\n");
					      finish(request);
					      return;
				      }
			      }
			      apply(request);
		      }))
	{
		tell(*request, "The database is busy. Nothing was changed.\r\n");
		finish(request);
	}
}

void one_loaded(const std::shared_ptr<sideswap_request> &request)
{
	if (--request->pending == 0)
		all_loaded(request);
}

void account_loaded(const std::shared_ptr<sideswap_request> &request, bool ok, P_acct account)
{
	std::string problems;
	if (!ok || !account)
		problems = "No such account.\r\n";
	else
	{
		request->account = account->acct_name;
		if (account_in_game(request->account))
			problems += "Someone is logged in to the account.\r\n";
		for (sideswap_target &target : request->targets)
			for (acct_chars *entry = account->acct_character_list; entry;
			     entry = entry->next)
				if (!strcasecmp(entry->charname, target.name.c_str()))
					target.pid = entry->pid;
		std::set<int> listed_pids;
		for (const sideswap_target &target : request->targets)
			if (!target.pid)
				problems += target.name + " is not on that account.\r\n";
			else if (!listed_pids.insert(target.pid).second)
				problems += target.name + " is listed more than once.\r\n";
		size_t character_count = 0;
		for (acct_chars *entry = account->acct_character_list; entry; entry = entry->next)
		{
			++character_count;
			bool listed = false;
			for (const sideswap_target &target : request->targets)
				listed |= target.pid == entry->pid;
			if (!listed)
				problems +=
					std::string(entry->charname) +
					" is on the account but not listed; list every character.\r\n";
			else if (is_pid_online(entry->pid, TRUE) ||
				 player_load_pipeline_pid_pending(entry->pid))
				problems += std::string(entry->charname) + " is in the game.\r\n";
		}
		if (request->targets.size() != character_count)
			problems += "List every account character exactly once.\r\n";
	}
	if (account)
		free_account(account);
	if (!problems.empty())
	{
		tell(*request, "Sideswap refused:\r\n" + problems);
		finish(request);
		return;
	}

	// One read for the two account lockers, then one load per character.
	request->pending = request->targets.size() + 1;
	std::string names;
	for (int side : { RACEWAR_GOOD, RACEWAR_EVIL })
		names += (names.empty() ? "'" : ",'") +
			 escape_str(locker_name(request->account, side).c_str()) + "'";
	const bool read = sql_read(
		sql_format("SELECT LOWER(l.locker_name),(SELECT COUNT(*) FROM locker_items i "
			   "WHERE i.locker_id=l.id)+(SELECT COUNT(*) FROM private_chests c WHERE "
			   "c.locker_id=l.id AND c.is_public=0) FROM lockers l WHERE "
			   "LOWER(l.locker_name) IN (%s)",
			   names.c_str()),
		[request](bool read_ok, const sql_rows &rows)
		{
			request->load_failed |= !read_ok;
			for (const sql_row &row : rows)
				for (int side : { RACEWAR_GOOD, RACEWAR_EVIL })
					if (row[0] && locker_name(request->account, side) == row[0])
					{
						request->locker_exists[side] = true;
						request->locker_used[side] = row[1] &&
									     atoi(row[1]) > 0;
					}
			one_loaded(request);
		});
	if (!read)
	{
		tell(*request, "The database is busy. Nothing was changed.\r\n");
		finish(request);
		return;
	}
	for (size_t index = 0; index < request->targets.size(); ++index)
		if (!player_load_offline(request->targets[index].pid,
					 [request, index](P_char loaded)
					 {
						 request->targets[index].ch = loaded;
						 player_revision_snapshot revision = {};
						 const bool valid = loaded && player_revision_snapshot_copy(
								 request->targets[index].pid, &revision);
						 request->load_failed |= !valid;
						 request->targets[index].loaded_revision = revision.written_revision;
						 one_loaded(request);
					 }))
		{
			request->load_failed = true;
			one_loaded(request);
		}
}
} // namespace

bool account_sideswap_in_progress(const char *name)
{
	return name && accounts_in_progress.count(lower(name));
}

void do_sideswap(P_char ch, char *argument, int /*cmd*/)
{
	char word[MAX_INPUT_LENGTH];
	const char *usage =
		"usage: sideswap <account> <char>=<race>[:<class>] ... [+money] [+eq] [+epics]\r\n"
		"                [+artis] [+locker] [+frags] [confirm]\r\n"
		"Moves every character of an offline account between good and evil. List each\r\n"
		"character with its new race (and class, to change it). Without the options,\r\n"
		"money goes to the old side's bank, equipment is destroyed, artifacts return to\r\n"
		"the world, epics and frags are reset and the account locker stays behind.\r\n"
		"Without confirm, it only shows what it would do. See help sideswap.\r\n";

	if (IS_NPC(ch))
		return;

	auto request = std::make_shared<sideswap_request>();
	request->staff = ch->runtime_id;
	request->staff_name = GET_NAME(ch);

	argument = one_argument(argument, word);
	if (!*word)
	{
		send_to_char(usage, ch);
		return;
	}
	request->account = word;
	for (argument = one_argument(argument, word); *word;
	     argument = one_argument(argument, word))
	{
		if (!str_cmp(word, "confirm"))
			request->confirm = true;
		else if (*word == '+')
		{
			bool *option = !str_cmp(word, "+money")	 ? &request->money :
				       !str_cmp(word, "+eq")	 ? &request->eq :
				       !str_cmp(word, "+epics")	 ? &request->epics :
				       !str_cmp(word, "+artis")	 ? &request->artis :
				       !str_cmp(word, "+locker") ? &request->locker :
				       !str_cmp(word, "+frags")	 ? &request->frags :
								   nullptr;
			if (!option)
			{
				send_to_char_f(ch, "Unknown option %s.\r\n%s", word, usage);
				return;
			}
			*option = true;
		}
		else
		{
			char *race = strchr(word, '=');
			char *cls = race ? strchr(race, ':') : nullptr;
			if (!race)
			{
				send_to_char_f(ch, "'%s' needs a new race: %s=<race>.\r\n", word,
					       word);
				return;
			}
			*race++ = '\0';
			if (cls)
				*cls++ = '\0';
			sideswap_target target;
			target.name = word;
			for (const sideswap_target &other : request->targets)
				if (!strcasecmp(other.name.c_str(), target.name.c_str()))
				{
					send_to_char_f(ch, "%s is listed more than once.\r\n", word);
					return;
				}
			target.race = find_race(race);
			if (target.race <= 0 || target.race > LAST_RACE ||
			    race_side(target.race) == RACEWAR_NONE || !creation_race_enabled(target.race))
			{
				send_to_char_f(ch, "%s is not one enabled playable race.\r\n", race);
				return;
			}
			if (cls && !(target.m_class = find_class(cls)))
			{
				send_to_char_f(ch, "%s is not a class.\r\n", cls);
				return;
			}
			request->targets.push_back(target);
		}
	}
	if (request->targets.empty())
	{
		send_to_char(usage, ch);
		return;
	}
	if (!accounts_in_progress.insert(lower(request->account)).second)
	{
		send_to_char("A sideswap of that account is already running.\r\n", ch);
		return;
	}
	if (!sql_load_account(request->account.c_str(), [request](bool ok, P_acct account)
			      { account_loaded(request, ok, account); }))
	{
		send_to_char("The database is busy. Nothing was changed.\r\n", ch);
		accounts_in_progress.erase(lower(request->account));
		return;
	}
	send_to_char("Loading the account...\r\n", ch);
}
