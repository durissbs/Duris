#!/usr/bin/env python3
"""Offline checks for scripts/post_news_to_discord.py; nothing is sent to Discord."""
import importlib.util
import io
import os
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

os.environ.pop("DISCORD_TOKEN", None)  # never let the test reach Discord
ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "post_news_to_discord", ROOT / "scripts/post_news_to_discord.py")
news = importlib.util.module_from_spec(spec)
spec.loader.exec_module(news)

SAMPLE = """&+W======================================================================&n
&+Y                           DURIS PLAYER NEWS&n
&+WUpdated: November 22, 2026&n

Welcome back! Intro text that must not be posted.

&+R11/22/26&n &+L-&n &+YLuck of the draw&n
&+W----------------------------------------------------------------------&n

&+CEnhancement&n
&+Y-&n Results are random among items of the same value, using
  &+C'enhance <item>'&n as before; *stars* and _marks_ stay literal.
&+Y-&n Join us: &+Chttps://discord.gg/e3XE4Yvpz5&n

&+R11/15/26 - 11/17/26&n &+L-&n &+YOlder news&n
&+W----------------------------------------------------------------------&n
&+Y-&n This older section must not be posted.
"""

text = news.to_discord(news.newest_section(SAMPLE))
assert text.startswith("**Duris News: 11/22/26 - Luck of the draw**"), text
assert "**Enhancement**" in text
assert ("- Results are random among items of the same value, using 'enhance <item>' "
        "as before; \\*stars\\* and \\_marks\\_ stay literal.") in text, text
assert "https://discord.gg/e3XE4Yvpz5" in text and "\\_" not in text.split("Join us: ")[1]
assert "&" not in text and "Intro text" not in text and "older section" not in text
assert "-----" not in text

# Long news splits on line boundaries, every message within Discord's limit.
lines = [f"- bullet {n} " + "x" * 150 for n in range(40)]
parts = news.split_messages("\n".join(lines))
assert len(parts) > 1 and all(len(p) <= news.DISCORD_LIMIT for p in parts)
assert "\n".join(parts) == "\n".join(lines)
assert all(len(p) <= 2000 for p in news.split_messages("y" * 4500))

with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    (tmp / "news").write_text(SAMPLE)
    state = tmp / "state"
    common = ["--channel", "1", "--news", str(tmp / "news"), "--state", str(state),
              "--env-file", str(tmp / "missing.env")]

    out = io.StringIO()
    with redirect_stdout(out):
        assert news.main(common + ["--dry-run"]) == 0
    assert "--- message 1/1" in out.getvalue() and not state.exists()

    # An already-posted section exits before reading the token or contacting Discord.
    section = "\n".join(news.newest_section(SAMPLE))
    state.write_text(news.hashlib.sha256(section.encode()).hexdigest() + "\n")
    out = io.StringIO()
    with redirect_stdout(out):
        assert news.main(common) == 0
    assert "already posted" in out.getvalue()

    # A new section with no token available fails clearly without posting.
    state.write_text("old\n")
    try:
        news.main(common)
    except SystemExit as stop:
        assert "missing.env" in str(stop)
    else:
        raise AssertionError("expected a missing-token error")
    assert state.read_text() == "old\n"

print("post_news_to_discord contract passed")
