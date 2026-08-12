import pytest

from app.services import login_throttle


@pytest.fixture(autouse=True)
def _clean():
    login_throttle.reset()
    yield
    login_throttle.reset()


def test_no_lockout_under_threshold():
    ip = "1.2.3.4"
    for _ in range(login_throttle.MAX_ATTEMPTS - 1):
        assert login_throttle.record_failure(ip) == 0
    assert login_throttle.retry_after(ip) == 0


def test_lockout_after_threshold():
    ip = "1.2.3.4"
    lockout = 0
    for _ in range(login_throttle.MAX_ATTEMPTS):
        lockout = login_throttle.record_failure(ip)
    assert lockout == int(login_throttle.BASE_LOCKOUT)
    assert login_throttle.retry_after(ip) > 0


def test_success_clears_state():
    ip = "1.2.3.4"
    for _ in range(login_throttle.MAX_ATTEMPTS):
        login_throttle.record_failure(ip)
    assert login_throttle.retry_after(ip) > 0
    login_throttle.record_success(ip)
    assert login_throttle.retry_after(ip) == 0


def test_lockout_escalates_on_repeat():
    ip = "9.9.9.9"
    first = 0
    for _ in range(login_throttle.MAX_ATTEMPTS):
        first = login_throttle.record_failure(ip)
    # After the lockout window resets the counter, a second round locks longer.
    second = 0
    for _ in range(login_throttle.MAX_ATTEMPTS):
        second = login_throttle.record_failure(ip)
    assert second == first * 2


def test_separate_ips_isolated():
    for _ in range(login_throttle.MAX_ATTEMPTS):
        login_throttle.record_failure("10.0.0.1")
    assert login_throttle.retry_after("10.0.0.1") > 0
    assert login_throttle.retry_after("10.0.0.2") == 0


@pytest.mark.anyio
async def test_login_locks_out_after_repeated_failures(client):
    for _ in range(login_throttle.MAX_ATTEMPTS):
        r = await client.post("/admin/login", data={"password": "nope"})
    assert r.status_code == 429
    assert "too many" in r.text.lower()
    # Even the correct password is refused while locked out, with a Retry-After.
    r2 = await client.post("/admin/login", data={"password": "admin"})
    assert r2.status_code == 429
    assert "retry-after" in r2.headers
    assert "admin_session" not in r2.cookies
