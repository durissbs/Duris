#!/usr/bin/env python3
"""Post the newest player news section to a Discord channel.

Reads the newest dated section of lib/information/news, removes the game's
colour codes and posts it as plain Discord messages through the Duris Discord
bot. The bot token comes from DISCORD_TOKEN in the environment, or else from the
bridge's env file (~/discord-mud-chat/.env). It is never printed.

A fingerprint of the last posted section is kept in the state file, so running
the script again for the same news posts nothing.

    scripts/post_news_to_discord.py --channel 1572409179069489298 --dry-run
    scripts/post_news_to_discord.py --channel 1572409179069489298
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DISCORD_LIMIT = 2000
COLOUR = re.compile(r"&[+-]?[A-Za-z]")
DATE_HEADER = re.compile(r"^&\+R\d\d/\d\d/\d\d")
MARKDOWN = re.compile(r"([\\*_~`|])")  # > and # only matter at a line start


def newest_section(news: str) -> list[str]:
    """Return the raw lines of the first dated section, header included."""
    lines = news.splitlines()
    start = next((i for i, line in enumerate(lines) if DATE_HEADER.match(line)), None)
    if start is None:
        raise SystemExit("no dated news section found")
    end = next((i for i in range(start + 1, len(lines)) if DATE_HEADER.match(lines[i])),
               len(lines))
    return lines[start:end]


def plain(line: str) -> str:
    return COLOUR.sub("", line).strip()


def escape(text: str) -> str:
    """Escape Discord formatting characters, leaving links intact."""
    parts = re.split(r"(https?://\S+)", text)
    return "".join(part if part.startswith(("http://", "https://")) else
                   MARKDOWN.sub(r"\\\1", part) for part in parts)


def to_discord(section: list[str]) -> str:
    """Turn a news section into Discord text: bold title and headings, '-' bullets."""
    title = plain(section[0])
    paragraphs: list[str] = [f"**Duris News: {escape(title)}**"]
    for raw in section[1:]:
        text = plain(raw)
        if not text or set(text) == {"-"}:
            continue
        if raw.startswith("  ") and paragraphs[-1].startswith("- "):
            paragraphs[-1] += " " + escape(text)  # wrapped continuation of a bullet
        elif text.startswith("- "):
            paragraphs.append("- " + escape(text[2:]))
        elif raw.startswith("&+C"):
            paragraphs.append("")
            paragraphs.append(f"**{escape(text)}**")
        else:
            text = escape(text)
            paragraphs.append("\\" + text if text.startswith((">", "#")) else text)
    return "\n".join(paragraphs).strip()


def split_messages(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """Split on line boundaries into messages of at most `limit` characters."""
    messages: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:  # a single line longer than a message
            if current:
                messages.append(current)
                current = ""
            messages.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            messages.append(current)
            current = line
        else:
            current = candidate
    if current.strip():
        messages.append(current)
    return [m.strip("\n") for m in messages if m.strip()]


def read_token(env_file: Path) -> str:
    if os.environ.get("DISCORD_TOKEN"):
        return os.environ["DISCORD_TOKEN"]
    try:
        for line in env_file.read_text().splitlines():
            match = re.match(r"^\s*(?:export\s+)?DISCORD_TOKEN\s*=\s*(.*?)\s*$", line)
            if match:
                return match[1].strip("'\"")
    except OSError as error:
        raise SystemExit(f"cannot read {env_file}: {error.strerror}")
    raise SystemExit(f"DISCORD_TOKEN is not set in the environment or {env_file}")


def post(channel: str, token: str, content: str) -> None:
    body = json.dumps({"content": content, "allowed_mentions": {"parse": []}}).encode()
    request = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{channel}/messages", data=body, method="POST",
        headers={"Authorization": f"Bot {token}", "Content-Type": "application/json",
                 "User-Agent": "DiscordBot (https://github.com/durissbs/Duris, 1)"})
    for _ in range(5):
        try:
            with urllib.request.urlopen(request, timeout=20):
                return
        except urllib.error.HTTPError as error:
            if error.code != 429:
                detail = error.read().decode(errors="replace")[:300]
                raise SystemExit(f"Discord refused the post: HTTP {error.code} {detail}")
            time.sleep(float(json.loads(error.read() or b"{}").get("retry_after", 1)))
    raise SystemExit("Discord kept rate-limiting the post; try again later")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--channel", required=True, help="Discord channel ID")
    parser.add_argument("--news", type=Path, default=Path("lib/information/news"))
    parser.add_argument("--env-file", type=Path,
                        default=Path.home() / "discord-mud-chat" / ".env")
    parser.add_argument("--state", type=Path,
                        default=Path.home() / ".local/state/duris-news-discord/last-posted")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be posted; post nothing")
    args = parser.parse_args(argv)

    section = newest_section(args.news.read_text(encoding="utf-8"))
    fingerprint = hashlib.sha256("\n".join(section).encode()).hexdigest()
    messages = split_messages(to_discord(section))

    already_posted = args.state.exists() and args.state.read_text().strip() == fingerprint
    if args.dry_run:
        if already_posted:
            print("(This section was already posted; a real run would post nothing.)")
        for number, message in enumerate(messages, 1):
            print(f"--- message {number}/{len(messages)} ({len(message)} characters)")
            print(message)
        return 0
    if already_posted:
        print("This news section was already posted; nothing to do.")
        return 0

    token = read_token(args.env_file)
    for message in messages:
        post(args.channel, token, message)
    args.state.parent.mkdir(parents=True, exist_ok=True)
    args.state.write_text(fingerprint + "\n")
    print(f"Posted the newest news section in {len(messages)} message(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
