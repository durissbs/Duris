# Account access and password recovery

## Players

New accounts require only an account name and a password. No email address is
requested, and email verification and password resets are no longer supported.
If you can log in, change your password through account-menu option **6** on
telnet or the **Password** tab in the browser client.

If you forget your password, contact game staff for assistance. Staff can reset
a password, but cannot retrieve the old one.

## Staff

Use the established manual account-ownership checks before restoring access.
Do not treat knowledge of an email address or a claimed character name as proof.
Do not request a player's mailbox password or disclose password hashes. Once
ownership is established, use the authorized password administration procedure
(`scripts/change_password.sh`) and have the player choose a unique password.

## Rollout

Deploy the matching game and browser changes together and perform a full game
restart to discard any old in-memory recovery state. Check registration and
password login on both clients and verify that the account email columns contain
no addresses. Take a verified backup after the scrub. Restore qualification
rejects a recovered database that still contains account email addresses. Use a
verified post-scrub generation for recovery. The account-recovery
SMTP settings no longer enable player email; operational backup alert settings
are separate and must not be removed merely because account recovery is retired.
