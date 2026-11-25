#!/usr/bin/env python3
"""Password-only registration durability and disabled email recovery on disposable MariaDB.

Run through with_disposable_mariadb.sh. All accounts, mail and faults belong to
this loopback fixture; no checkout .env or external SMTP relay is used.
"""
import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

import test_account_recovery_journey as journey
from test_character_stat_choice_journey import WebSocketClient


def check_registration_guards(server, sql):
    """A delayed Telnet confirmation cannot overwrite a committed web signup."""
    account = "Claimrace"
    stale = journey.MudClient(server.plain_port)
    claimant = WebSocketClient(server.websocket_port)
    try:
        journey.enter_account_details(stale, journey.NEW_PASSWORD, name=account)
        auth = claimant.command("register", {"account": account, "password": journey.OLD_PASSWORD,
                                             "email": "claim@example.invalid"}, "auth")
        assert auth["status"] == "success", auth
        deadline = time.monotonic() + 10
        original = ""
        while not original:
            original = sql("SELECT password FROM accounts WHERE account_name='Claimrace'")
            assert time.monotonic() < deadline, "the first signup never landed"
            if not original:
                time.sleep(0.05)
        # A normal account save refreshes same-name caches, including the stale
        # form. Even a copied winning hash cannot prove its first INSERT applied.
        result = claimant.command("change_password", {"currentPassword": journey.OLD_PASSWORD,
                                  "newPassword": "WinnerOnlyFixturePassword"}, "account")
        assert result["action"] == "password_changed", result
        deadline = time.monotonic() + 10
        while True:
            rotated = sql("SELECT password FROM accounts WHERE account_name='Claimrace'")
            if rotated != original:
                break
            assert time.monotonic() < deadline, "winner's password update did not land"
            time.sleep(0.05)
        original = rotated
        issues = []
        if sql("SELECT COALESCE(email,'') FROM accounts WHERE account_name='Claimrace'") != "":
            issues.append("signup saved a legacy recovery address")
        stale.send("y")
        answer, _ = stale.expect_any(("Account could not be created", "Account created successfully"))
        if answer == "Account created successfully":
            stale.expect("PRESS RETURN")
            stale.send("")
            stale.expect("Please select an option")
            issues.append("stale Telnet signup entered the winner's account menu without its password")
        if sql("SELECT password FROM accounts WHERE account_name='Claimrace'") != original:
            issues.append("stale Telnet signup overwrote the committed password")
        assert not issues, "; ".join(issues)
    finally:
        claimant.close()
        stale.close()


def run(binary):
    database = "recovery_test_" + uuid.uuid4().hex[:12]
    host = os.environ["TEST_DB_HOST"]
    assert host == "127.0.0.1", "use the disposable loopback database runner"
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "ENVIRONMENT": "local",
        "DB_HOST": host, "DB_PORT": os.environ["TEST_DB_PORT"], "DB_NAME": database,
        "DB_USER": os.environ["TEST_DB_USER"], "DB_PASSWD": os.environ["TEST_DB_PASSWORD"],
        "DB_ALLOWED_TARGETS": host + "/" + database, "MYSQL_PWD": os.environ["TEST_DB_PASSWORD"],
        "DB_TLS": "FALSE", "DB_SOCKET": "", "PERSISTENCE_MODE": "mariadb-primary",
        "DURIS_WEBSOCKET": "TRUE",
    }
    mysql = ["mysql", "--protocol=tcp", "--skip-ssl", "-h", host,
             "-P", environment["DB_PORT"], "-u", environment["DB_USER"], "-N", "-B"]

    def sql(text, selected=True):
        return subprocess.check_output(mysql + ([database] if selected else []), input=text,
                                       text=True, env=environment, timeout=120).strip()

    @contextmanager
    def boot(extra_environment=None, *, mail_enabled=True):
        def prepare(runtime):
            # Match the other gameplay journeys' short, isolated camping timer.
            properties = runtime / "lib/duris.properties"
            text = properties.read_text()
            assert "camp.timer=9.000" in text
            properties.write_text(text.replace("camp.timer=9.000", "camp.timer=2.000"))

        smtp = journey.FakeSmtpServer()
        try:
            with journey.IsolatedServer(binary, dict(environment, MAIL_ENABLED="TRUE" if mail_enabled else "FALSE",
                    MAIL_HOST=host, MAIL_PORT=str(smtp.port), MAIL_TLS="FALSE",
                    MAIL_FROM=journey.MAIL_FROM, **(extra_environment or {})),
                    prepare_run_root=prepare) as server:
                try:
                    yield server, smtp
                    server.shutdown()
                except Exception:
                    # Logs contain only synthetic fixture data; do not print client input.
                    print(server.server_output()[-6000:])
                    print(server.runtime_logs()[-6000:])
                    raise
        finally:
            smtp.close()

    sql("CREATE DATABASE " + database + " CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci", False)
    try:
        sql((journey.ROOT / "migrations/bootstrap_multithread_safe.sql").read_text())
        for args in (("adopt", "--kind", "fresh_bootstrap"), ("run",)):
            subprocess.run(["python3", "scripts/migration_runner.py", *args], cwd=journey.ROOT,
                           env=environment, check=True, capture_output=True)

        with boot(mail_enabled=False) as (server, _):
            check_registration_guards(server, sql)
        print("PASS: first signup wins and legacy registration email is ignored", flush=True)

        with boot() as (server, smtp):
            client = WebSocketClient(server.websocket_port)
            try:
                auth = client.command("register", {"account": "Noemail", "password": journey.OLD_PASSWORD}, "auth")
                assert auth["status"] == "success", auth
                assert sql("SELECT COALESCE(email,'') FROM accounts WHERE account_name='Noemail'") == ""
                before = sql("SELECT password FROM accounts WHERE account_name='Noemail'")
                for command, data in (
                    ("change_email", {"newEmail": "owner@example.invalid"}),
                    ("confirm_email", {"code": "wrong"}),
                    ("request_reset", {"account": "Noemail"}),
                    ("complete_reset", {"account": "Noemail", "code": "wrong", "newPassword": journey.NEW_PASSWORD}),
                ):
                    response = client.command(command, data, "account")
                    assert response["action"] == "error", response
                assert sql("SELECT COALESCE(email,'') FROM accounts WHERE account_name='Noemail'") == ""
                assert sql("SELECT password FROM accounts WHERE account_name='Noemail'") == before
                journey.require_no_new_connection(smtp, expected=0)
            finally:
                client.close()
        print("PASS: WebSocket signup without email and all retired commands rejected", flush=True)

        build = journey.ROOT / "bin/tests"
        build.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="recovery-commit-", dir=build) as tmp:
            runtime = Path(tmp)
            preload = runtime / "commit-fault.so"
            subprocess.run(["cc", "-shared", "-fPIC", "-Wall", "-Wextra", "-Werror",
                            "tests/async/account_recovery_commit_fault.c", "-ldl", "-o", str(preload)],
                           cwd=journey.ROOT, check=True)
            arm = runtime / "commit-fault"
            reached = Path(str(arm) + ".reached")
            for mode, name in (("before", "Ackbefore"), ("after", "Ackafter")):
                with boot({"LD_PRELOAD": str(preload),
                           "DURIS_TEST_RECOVERY_COMMIT_FAULT": str(arm)},
                          mail_enabled=False) as (server, _):
                    client = journey.MudClient(server.plain_port)
                    try:
                        journey.enter_account_details(client, journey.OLD_PASSWORD, name=name)
                        arm.write_text(mode)
                        client.send("y")
                        deadline = time.monotonic() + 20
                        while not reached.exists():
                            assert time.monotonic() < deadline, "registration COMMIT fault was not reached"
                            time.sleep(0.05)
                        password_query = f"SELECT password FROM accounts WHERE account_name='{name}'"
                        committed = sql(password_query)
                        assert bool(committed) == (mode == "after")
                        client._receive()
                        assert b"Account created successfully" not in client.transcript
                        arm.unlink()
                        client.expect("Account created successfully")
                        client.expect("PRESS RETURN")
                        client.send("")
                        client.expect("Please select an option")
                        final = sql(password_query)
                        assert final and (not committed or final == committed)
                        assert sql(f"SELECT COALESCE(email,'') FROM accounts WHERE account_name='{name}'") == ""
                    finally:
                        arm.unlink(missing_ok=True)
                        reached.unlink(missing_ok=True)
                        client.close()
            print("PASS: registration reconciles lost COMMIT replies before and after application", flush=True)
    finally:
        sql("DROP DATABASE " + database, False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", type=Path, required=True)
    run(parser.parse_args().server.resolve())
