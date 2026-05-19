"""Shared fixtures to prevent test pollution between modules."""
import os
import threading
import pytest


@pytest.fixture(autouse=True, scope="module")
def _restore_store_state():
    """Ensure store state is clean after each test module."""
    try:
        from telechat_pkg import store
        orig_db = store.DB_PATH
    except Exception:
        yield
        return
    yield
    # Restore after module — reset connection and writer thread
    store.DB_PATH = orig_db
    store._local = threading.local()
    store._writer_thread = None
    store._write_queue = None
    store.init_db()
