import os

import pytest

from governed_agents.store import Store


@pytest.fixture
def store_url(tmp_path):
    """SQLite by default. Set TEST_DATABASE_URL (CI does) to run the same tests on Postgres."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        return f"sqlite:///{tmp_path / 'test.db'}"
    Store(url)._exec("TRUNCATE runs, events, actions, outbox")
    return url
