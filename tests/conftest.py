from __future__ import annotations

import pytest

from slowquerytop import demo
from slowquerytop.repository import store


@pytest.fixture()
def engine(tmp_path):
    eng = store.get_engine(f"sqlite:///{tmp_path / 'repo.db'}")
    store.init_db(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def demo_engine(engine):
    demo.generate(engine, days=3)
    return engine
