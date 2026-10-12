"""The migrations form one chain, so ``alembic upgrade head`` has exactly one target.

Two pull requests that each add the next-numbered migration both merge cleanly and leave
two heads, and the upgrade then refuses to run on the mine's box. Naming the head here
makes the second such pull request conflict on this line instead, so it is re-chained
before it merges.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

HEAD = "0030_source_status"


def test_migrations_have_one_head() -> None:
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    assert ScriptDirectory.from_config(config).get_heads() == [HEAD]
