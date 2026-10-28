"""P2 offline regression: account leases, cancellation, and safe retry boundaries.

Only mock Muse execution. No live browser, network, cookies, or credentials.
"""
import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

import app
from config import Config
from store import Store


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = Config(base_dir=str(tmp_path), profile_root=str(tmp_path / "profiles"))
    cfg.ensure_dirs()
    db = Store(cfg)
    for n in ("a", "b"):
        db.add_account({"hatch_sess": f"fixture-{n}", "hatch_vml": "fixture"},
                       label=n)
    pool = app.AccountPoolManager(db)
    monkeypatch.setattr(app, "store", db)
    monkeypatch.setattr(app, "ACCOUNT_POOL", pool)
    monkeypatch.setattr(app, "GEN_LOCK", app.ConcurrencyLock(limit=2))
    monkeypatch.setattr(app, "_sync_cookies", lambda *args: None)
    monkeypatch.setattr(app.engine, "start", lambda: None)
    monkeypatch.setattr(app.engine, "close_current_page", lambda: None)
    return db, pool


def ids(env):
    return [a["id"] for a in env[0].accounts]


def test_pool_atomically_reserves_two_distinct_accounts(env):
    _, pool = env
    barrier = threading.Barrier(3)  # 两个 worker + 主测试线程
    released = threading.Event()
    results = []

    def worker():
        acc = pool.acquire(timeout=2)
        results.append(acc["id"])
        barrier.wait(timeout=2)
        released.wait(timeout=2)
        pool.release(acc["id"])

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1, f2 = executor.submit(worker), executor.submit(worker)
        barrier.wait(timeout=2)
        assert len(set(results)) == 2
        assert len(pool.busy_accounts) == 2
        released.set()
        f1.result(timeout=2)
        f2.result(timeout=2)
    assert not pool.busy_accounts


def test_pool_never_assigns_busy_or_excluded_account(env):
    _, pool = env
    a = pool.acquire(timeout=1)
    b = pool.acquire(exclude_ids={a["id"]}, timeout=1)
    try:
        assert b["id"] != a["id"]
        with pytest.raises(app.MuseGenerationError):
            pool.acquire(timeout=0.05)
    finally:
        pool.release(a["id"])
        pool.release(b["id"])
    with pytest.raises(app.MuseGenerationError, match="没有其他账号"):
        pool.acquire(exclude_ids=set(ids(env)), timeout=1)
    assert not pool.busy_accounts


def test_chat_request_routes_do_not_preselect_account(env, monkeypatch):
    assert all(a["use_count"] == 0 for a in env[0].accounts)
    def fake_stream(cookies, prompt, expires, timeout, account_id):
        assert cookies is None and expires is None and account_id is None
        yield "fixture"
    monkeypatch.setattr(app, "safe_chat_stream", fake_stream)
    async def run():
        chat = await app.chat_completions(app.ChatRequest(messages=[
            app.ChatMessage(role="user", content="hello")]))
        response = await app.responses_api(app.ResponsesRequest(input="hello"))
        assert chat["choices"][0]["message"]["content"] == "fixture"
        assert response["status"] == "completed"
    asyncio.run(run())
    assert all(a["use_count"] == 0 for a in env[0].accounts)


def test_chat_does_not_retry_after_generator_has_started(env, monkeypatch):
    seen = []
    monkeypatch.setattr(app, "_renew_and_persist", lambda ident, **kwargs: env[0].get_account(ident))
    def fake_chat(cookies, prompt, expires, timeout, account_id, stop_event):
        seen.append(account_id)
        raise app.MuseGenerationError("提交状态未知")
        yield "unreachable"
    monkeypatch.setattr(app.engine, "chat_stream", fake_chat)
    with pytest.raises(app.MuseGenerationError, match="提交状态未知"):
        list(app.safe_chat_stream(None, "hello", None, 2, None))
    assert len(seen) == 1
    assert not env[1].busy_accounts


def test_chat_preflight_failure_can_use_second_account(env, monkeypatch):
    account_a, account_b = ids(env)
    preflight = []
    sent = []
    def fake_renew(account, **kwargs):
        preflight.append(account)
        if account == account_a:
            raise app.MuseAuthError("cookie expired")
        return env[0].get_account(account)
    def fake_chat(cookies, prompt, expires, timeout, account_id, stop_event):
        sent.append(account_id)
        yield "ok"
    monkeypatch.setattr(app, "_renew_and_persist", fake_renew)
    monkeypatch.setattr(app.engine, "chat_stream", fake_chat)
    assert list(app.safe_chat_stream(None, "hello", None, 3, None)) == ["ok"]
    assert preflight == [account_a, account_b]
    assert sent == [account_b]
    assert not env[1].busy_accounts


def test_chat_with_specified_account_never_switches(env, monkeypatch):
    first = ids(env)[0]
    def fake_renew(account, **kwargs):
        raise app.MuseAuthError("expired")
    monkeypatch.setattr(app, "_renew_and_persist", fake_renew)
    with pytest.raises(app.MuseAuthError):
        list(app.safe_chat_stream(None, "hello", None, 2, first))
    assert env[0].accounts[1]["use_count"] == 0
    assert not env[1].busy_accounts


def test_chat_cancellation_unblocks_full_output_queue(env, monkeypatch):
    monkeypatch.setattr(app, "_renew_and_persist", lambda ident, **kwargs: env[0].get_account(ident))
    active = threading.Event()
    completed = threading.Event()
    def flooding_chat(cookies, prompt, expires, timeout, account_id, stop_event):
        active.set()
        try:
            for _ in range(10000):
                if stop_event.is_set():
                    break
                yield "X"
        finally:
            completed.set()
    monkeypatch.setattr(app.engine, "chat_stream", flooding_chat)
    stream = app.safe_chat_stream(None, "hello", None, 2, None)
    assert next(stream) == "X"
    assert active.is_set()
    stream.close()
    assert completed.wait(timeout=3), "worker did not exit after client closed stream"
    deadline = time.monotonic() + 3
    while env[1].busy_accounts and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not env[1].busy_accounts


def test_media_does_not_duplicate_after_unknown_submission(env, monkeypatch):
    calls = []
    monkeypatch.setattr(app, "_renew_and_persist", lambda ident, **kwargs: env[0].get_account(ident))
    def unknown_media(cookies, prompt, **kwargs):
        calls.append(kwargs["account_id"])
        raise app.MuseGenerationError("已经提交但取回失败")
    monkeypatch.setattr(app.engine, "generate", unknown_media)
    with pytest.raises(app.MuseGenerationError, match="已经提交"):
        app._run_generation_locked("image", "image", 3)
    assert len(calls) == 1
    assert not env[1].busy_accounts


def test_media_retries_only_during_preflight(env, monkeypatch):
    first, second = ids(env)
    preflights, submissions = [], []
    def fake_renew(account, **kwargs):
        preflights.append(account)
        if account == first:
            raise app.MuseAuthError("invalid preflight")
        return env[0].get_account(account)
    def fake_media(cookies, prompt, **kwargs):
        submissions.append(kwargs["account_id"])
        return {"kind": "image", "filename": "fixture.png"}
    monkeypatch.setattr(app, "_renew_and_persist", fake_renew)
    monkeypatch.setattr(app.engine, "generate", fake_media)
    result, account = app._run_generation_locked("image", "image", 3)
    assert result["filename"] == "fixture.png"
    assert account == second
    assert preflights == [first, second]
    assert submissions == [second]
    assert not env[1].busy_accounts


def test_media_unknown_exception_no_secret_leak_or_retry(env, monkeypatch):
    calls = []
    monkeypatch.setattr(app, "_renew_and_persist", lambda ident, **kwargs: env[0].get_account(ident))
    def failed_media(cookies, prompt, **kwargs):
        calls.append(kwargs["account_id"])
        raise RuntimeError("socks5://user:SECRET@proxy.test")
    monkeypatch.setattr(app.engine, "generate", failed_media)
    with pytest.raises(app.MuseGenerationError) as err:
        app._run_generation_locked("video", "video", 3)
    assert len(calls) == 1
    assert "SECRET" not in str(err.value)
    assert not env[1].busy_accounts


def test_chat_cancel_before_dispatch_never_sends(tmp_path):
    from engine import MuseEngine
    cfg = Config(base_dir=str(tmp_path), profile_root=str(tmp_path / "profiles"))
    cfg.ensure_dirs()
    engine = MuseEngine(cfg)
    stop = threading.Event()
    with patch.object(engine, "ensure_page") as opened:
        stop.set()
        assert list(engine.chat_stream({"hatch_sess": "fixture"}, "hello",
                                       account_id="a", stop_event=stop)) == []
        opened.assert_not_called()

    stop.clear()
    def cancel_during_setup(*args, **kwargs):
        stop.set()
    with (patch.object(engine, "ensure_page", side_effect=cancel_during_setup),
          patch.object(engine, "_send") as sent):
        assert list(engine.chat_stream({"hatch_sess": "fixture"}, "hello",
                                       account_id="a", stop_event=stop)) == []
        sent.assert_not_called()


def test_media_cancel_before_send_never_submits(tmp_path):
    from engine import MuseEngine
    cfg = Config(base_dir=str(tmp_path), profile_root=str(tmp_path / "profiles"))
    cfg.ensure_dirs()
    engine = MuseEngine(cfg)
    stop = threading.Event()
    stop.set()
    with (patch.object(engine, "ensure_page"),
          patch.object(engine, "reset_thread"),
          patch.object(engine, "_scroll_bottom"),
          patch.object(engine, "_clear_attachments"),
          patch.object(engine, "attachments", return_value=[]),
          patch.object(engine, "_agent_count", return_value=0),
          patch.object(engine, "_send") as sent):
        with pytest.raises(app.MuseGenerationError, match="未提交生成请求"):
            engine.generate({"hatch_sess": "fixture"}, "test", stop_event=stop)
        sent.assert_not_called()


def test_chat_acquires_global_generation_lock_before_account_lease(env, monkeypatch):
    """No lock inversion: media uses GEN_LOCK -> ACCOUNT_POOL; chat must match."""
    pool = env[1]
    original_acquire = pool.acquire
    checked = []
    def check_lock_order(*args, **kwargs):
        checked.append(app.GEN_LOCK.locked())
        return original_acquire(*args, **kwargs)
    monkeypatch.setattr(pool, "acquire", check_lock_order)
    monkeypatch.setattr(app, "_renew_and_persist", lambda ident, **kwargs: env[0].get_account(ident))
    def fake_chat(cookies, prompt, expires, timeout, account_id, stop_event):
        yield "done"
    monkeypatch.setattr(app.engine, "chat_stream", fake_chat)
    assert list(app.safe_chat_stream(None, "hello", None, 3, None)) == ["done"]
    assert checked == [True]
    assert not pool.busy_accounts
