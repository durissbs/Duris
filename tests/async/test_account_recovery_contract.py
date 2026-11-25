#!/usr/bin/env python3
"""Source contracts for removal of account email from the live game paths."""

from _paths import extract_function, source

name = extract_function("account.c", "void verify_account_name(")
register = extract_function("account.c", "void account_create(")
web_register = extract_function("ws_handlers.c", "void ws_cmd_register(")
account_info = extract_function("ws_handlers.c", "void ws_cmd_account_info(")
save = source("sql_player.c").read_text()
comm = source("comm.c").read_text()

assert "CON_GET_NEW_ACCT_PASSWD" in name
assert "get_new_account_email" not in name
assert "INSERT INTO accounts(account_name,password,confirmed)" in register
assert '"email"' not in web_register
assert '"email"' not in account_info
assert "insert into accounts (account_name, password, confirmation_code" in save
assert "select account_name, NULL, password, confirmation_code" in save
assert "email='%s'" not in save
assert "account_recovery_init()" not in comm

for command in ("change_email", "confirm_email", "request_reset", "complete_reset"):
    body = extract_function("ws_handlers.c", f"void ws_cmd_{command}(")
    assert 'ws_send_account_message(d, "error"' in body, command
    assert "account_recovery_" not in body, command
    assert "account_email_" not in body, command

print("account email retirement source contracts passed")
