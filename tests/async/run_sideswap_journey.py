#!/usr/bin/env python3
"""Move a whole logged-out account from good to evil with sideswap on isolated authority."""
import argparse
from pathlib import Path
import signal
import subprocess
import tempfile
import time

import test_mariadb_combat_journey as journey
from test_flight_network_journey import authority, command

ROOT = journey.ROOT
ACCOUNT, FIRST, SECOND = "Swapacct", "Swapfirst", "Swapsecond"
GOD, GOD_ACCOUNT = "Swapwarden", "Swapstaff"
ANATOMY = 1201  # an epic skill warriors can learn and sorcerers cannot
SORCERER = 1 << 9


def run(binary):
    (ROOT / "bin/tests").mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sideswap-journey-", dir=ROOT / "bin/tests") as temporary:
        runtime = Path(temporary)
        journey.make_fixture(runtime)
        journey.generate_certificate(runtime)
        (runtime / "logs/log").mkdir(parents=True)
        critical = runtime / "journals/critical"
        critical.mkdir(parents=True, mode=0o700)
        plain, tls, websocket = journey.available_ports()
        process = None
        clients = []
        output_path = runtime / "server.out"
        with authority("mariadb", runtime) as (env, sql), output_path.open("w") as output:
            env.update(CRITICAL_COMMAND_JOURNAL_DIR=str(critical), DURIS_TLS_PORT=str(tls),
                       DURIS_WEBSOCKET_PORT=str(websocket),
                       MAINTENANCE_STATE_FILE=str(runtime / "maintenance.state"))

            def boot():
                start = output_path.stat().st_size
                proc = subprocess.Popen([str(binary), "--minimal", "-d", str(runtime), str(plain)],
                                        cwd=runtime, env=env, stdout=output,
                                        stderr=subprocess.STDOUT)
                deadline = time.monotonic() + 120
                while b"Entering game loop." not in output_path.read_bytes()[start:]:
                    assert proc.poll() is None and time.monotonic() < deadline, "boot failed"
                    time.sleep(.1)
                return proc

            def stop():
                for client in clients:
                    client.close()
                clients.clear()
                process.send_signal(signal.SIGTERM)
                process.wait(timeout=60)
                assert process.returncode == 0

            def login(name, account):
                client = journey.reconnect_character(plain, expected_room=None, account=account,
                                                     character=name)
                clients.append(client)
                client.expect_any(("Pos: standing >", "<>"), timeout=30)
                command(client, "toggle paging no")
                return client

            def until(query, expected, timeout=30):
                deadline = time.monotonic() + timeout
                while True:
                    value = sql(query)
                    if value == expected:
                        return
                    assert time.monotonic() < deadline, (query, value, expected)
                    time.sleep(.2)

            try:
                process = boot()
                for name, account, existing in ((GOD, GOD_ACCOUNT, False), (FIRST, ACCOUNT, False),
                                                (SECOND, ACCOUNT, True)):
                    client = journey.MudClient(plain)
                    clients.append(client)
                    journey.create_character(client, expected_room=None, account=account,
                                             character=name, existing_account=existing,
                                             email=name.lower() + "@example.invalid",
                                             class_name="w")
                    client.send("save")
                    client.expect("Saving " + name + ".", timeout=30)
                    client.send("quit")
                    clients.remove(client)
                    client.close()
                stop()
                journey.make_overlord(sql, GOD)
                pids = sql(f"SELECT GROUP_CONCAT(pid ORDER BY name) FROM player_data "
                           f"WHERE name IN ('{FIRST}','{SECOND}')")
                # A good-side bank, a good-side account locker with something in it, and
                # some coins on hand and a few frags.
                sql(f"INSERT INTO account_banks(account_name,racewar,bank_platinum) "
                    f"VALUES('{ACCOUNT}',1,25)")
                sql("INSERT IGNORE INTO currency_bank_baseline(bank_id,opening_copper,"
                    "opening_silver,opening_gold,opening_platinum,opening_revision) SELECT id,"
                    "bank_copper,bank_silver,bank_gold,bank_platinum,bank_revision FROM account_banks")
                sql(f"INSERT INTO lockers(locker_name,racewar) VALUES('account.{ACCOUNT.lower()}.1.locker',1)")
                sql(f"UPDATE player_data SET platinum=7,frags=3 WHERE name='{FIRST}'")
                items = int(sql(f"SELECT COUNT(*) FROM player_items pi JOIN player_data pd "
                                f"ON pd.pid=pi.pid WHERE pd.name='{SECOND}'"))
                assert items > 0, "the starter kit should have given the character items"

                process = boot()
                god = login(GOD, GOD_ACCOUNT)

                god.send(f"sideswap {ACCOUNT} {FIRST}=drow")
                god.expect(f"{SECOND} is on the account but not listed", timeout=30)
                god.send(f"sideswap {ACCOUNT} {FIRST}=human {SECOND}=drow")
                god.expect("Human is not an evil race.", timeout=30)

                # Good to evil, taking money, locker and equipment along.
                swap = f"sideswap {ACCOUNT} {FIRST}=drow {SECOND}=orc +money +locker +eq"
                god.send(swap)
                preview = god.expect("Nothing was changed. Add 'confirm' to do this.", timeout=30)
                assert "good to evil" in preview, preview
                assert "equipment kept, moved to inventory" in preview, preview
                assert "Account bank: moves to the new side." in preview, preview
                assert "Account locker: moves to the new side." in preview, preview
                until(f"SELECT GROUP_CONCAT(racewar) FROM player_data WHERE name IN "
                      f"('{FIRST}','{SECOND}')", "1,1")

                god.send(swap + " confirm")
                god.expect("Done: 2 of 2 character(s) saved on the evil side.", timeout=30)
                until(f"SELECT GROUP_CONCAT(racewar ORDER BY name) FROM player_data WHERE name IN "
                      f"('{FIRST}','{SECOND}')", "2,2")
                until(f"SELECT GROUP_CONCAT(racewar ORDER BY pid) FROM account_characters "
                      f"WHERE pid IN ({pids})", "2,2")
                until(f"SELECT CONCAT(racewar,':',bank_platinum) FROM account_banks WHERE "
                      f"account_name='{ACCOUNT}' ORDER BY racewar", "1:0\n2:25")
                until(f"SELECT racewar FROM lockers WHERE locker_name="
                      f"'account.{ACCOUNT.lower()}.2.locker'", "2")
                until(f"SELECT CONCAT(platinum,':',frags) FROM player_data WHERE "
                      f"name='{FIRST}'", "7:0")
                assert sql(f"SELECT COUNT(*) FROM player_items pi JOIN player_data pd ON "
                           f"pd.pid=pi.pid WHERE pd.name='{SECOND}'") == str(items)
                assert sql(f"SELECT alignment < 0 FROM player_data WHERE name='{FIRST}'") == "1"

                # Back to good, changing one class: an epic skill the new class cannot use
                # is refunded, one the class keeps stays; equipment is left behind.
                for name in (FIRST, SECOND):
                    sql(f"INSERT INTO player_skills(pid,skill_id,learned,taught) SELECT pid,"
                        f"{ANATOMY},20,20 FROM player_data WHERE name='{name}' ON DUPLICATE KEY "
                        f"UPDATE learned=20,taught=20")
                sql(f"UPDATE player_data SET frags=5 WHERE name='{SECOND}'")
                swap = f"sideswap {ACCOUNT} {FIRST}=human:sorcerer {SECOND}=human +epics +frags"
                god.send(swap + " confirm")
                done = god.expect("Done: 2 of 2 character(s) saved on the good side.", timeout=30)
                assert "refunds anatomy: 150 epic points, 1000 platinum to the bank" in done, done
                assert "item(s) destroyed" in done, done
                until(f"SELECT GROUP_CONCAT(racewar ORDER BY name) FROM player_data WHERE name IN "
                      f"('{FIRST}','{SECOND}')", "1,1")
                until(f"SELECT CONCAT(epics,':',m_class) FROM player_data WHERE name='{FIRST}'",
                      f"150:{SORCERER}")
                until(f"SELECT COALESCE(MAX(ps.learned),0) FROM player_skills ps JOIN player_data pd "
                      f"ON pd.pid=ps.pid WHERE pd.name='{FIRST}' AND ps.skill_id={ANATOMY}", "0")
                assert sql(f"SELECT ps.learned FROM player_skills ps JOIN player_data pd ON "
                           f"pd.pid=ps.pid WHERE pd.name='{SECOND}' AND ps.skill_id={ANATOMY}") == "20"
                assert sql(f"SELECT frags FROM player_data WHERE name='{SECOND}'") == "5"
                until(f"SELECT COUNT(*) FROM player_items pi JOIN player_data pd ON pd.pid=pi.pid "
                      f"WHERE pd.name='{SECOND}'", "0")
                # Without +money the coins on hand join the bank left on the old side.
                until(f"SELECT CONCAT(racewar,':',bank_platinum) FROM account_banks WHERE "
                      f"account_name='{ACCOUNT}' ORDER BY racewar", "1:1000\n2:32")

                # The swapped character plays on the new side straight away: no cooldown.
                god.send("quit")
                god.expect("Please select an option", timeout=30)
                clients.remove(god)
                god.close()
                # The account menu lists Swapsecond first; the login takes the first.
                player = login(SECOND, ACCOUNT)
                score = command(player, "score")
                assert "Race: Human" in score, score
                assert "Coins in bank: 1000 platinum" in score, score
                stop()
                print("PASS: sideswap preview, refusals, whole-account move, bank, locker, "
                      "items and login on the new side", flush=True)
            except BaseException:
                print(output_path.read_text(errors="replace")[-5000:])
                print(journey.runtime_logs(runtime)[-5000:])
                for client in clients:
                    print(bytes(client.transcript[-5000:]).decode(errors="replace"))
                raise
            finally:
                for client in clients:
                    client.close()
                if process and process.poll() is None:
                    process.send_signal(signal.SIGTERM)
                    process.wait(timeout=60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", type=Path, required=True)
    run(parser.parse_args().server.resolve())
