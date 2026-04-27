import pytest

from app.locking import RunLock


def test_run_lock_allows_only_one_holder(tmp_path):
    lock_path = tmp_path / "scraper.lock"
    first = RunLock(lock_path)
    second = RunLock(lock_path)

    assert first.acquire()
    assert not second.acquire()

    first.release()
    assert second.acquire()
    second.release()


def test_run_lock_context_manager(tmp_path):
    lock_path = tmp_path / "scraper.lock"
    with RunLock(lock_path):
        assert lock_path.exists()
    assert not lock_path.exists()


def test_run_lock_context_manager_raises_when_held(tmp_path):
    lock_path = tmp_path / "scraper.lock"
    holder = RunLock(lock_path)
    holder.acquire()
    try:
        with pytest.raises(RuntimeError):
            with RunLock(lock_path):
                pass
    finally:
        holder.release()
