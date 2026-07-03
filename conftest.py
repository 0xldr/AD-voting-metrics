"""Pytest configuration.

Loads .env at session startup so integration tests can read env vars without
the developer having to export them in their shell.

CI environments inject env vars directly (no .env file), so load_dotenv() is
a no-op there — it returns False without raising. Production CLI entry
(cli.py:main) also calls load_dotenv() independently; this conftest only
affects test runs.
"""

from dotenv import load_dotenv

load_dotenv()
