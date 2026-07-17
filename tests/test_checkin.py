from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest
import requests

from checkin import (
    AuthenticationError,
    CheckinResult,
    CheckinStatus,
    Config,
    ForumClient,
    ForumError,
    RewardState,
    decode_html,
    find_account_url,
    is_login_page,
    is_safe_forum_url,
    next_scheduled_run,
    parse_reward_page,
    run_with_retries,
)


class DummyResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content


class FakeSession:
    def __init__(self, responses: list[requests.Response]) -> None:
        self.responses = iter(responses)
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.post_kwargs: dict[str, object] | None = None

    def get(self, url: str, **_kwargs: object) -> requests.Response:
        self.calls.append(("GET", url))
        return next(self.responses)

    def post(self, url: str, **kwargs: object) -> requests.Response:
        self.calls.append(("POST", url))
        self.post_kwargs = kwargs
        return next(self.responses)


def make_response(html: str, url: str = "https://bbs.kfpromax.com/index.php") -> requests.Response:
    response = requests.Response()
    response.status_code = 200
    response.url = url
    response._content = html.encode("gb18030")
    return response


def make_config() -> Config:
    return Config("test-user", "test-password", ZoneInfo("Asia/Shanghai"), time(8, 0))


def test_decode_gbk_page() -> None:
    response = DummyResponse("你可以领取 90KFB".encode("gbk"))
    assert decode_html(response) == "你可以领取 90KFB"


def test_login_page_detection() -> None:
    assert is_login_page('<form><input name="pwpwd" type="password"></form>')
    assert not is_login_page("<html><p>欢迎回来</p></html>")


def test_login_success_finds_account_page() -> None:
    index = '<div>test-user</div><a href="kf_growup.php">100KFB | 0贡献</a>'
    session = FakeSession(
        [
            make_response('<input name="pwpwd">', "https://bbs.kfpromax.com/login.php"),
            make_response(index),
            make_response(index),
        ]
    )
    _html, account_url = ForumClient(make_config(), session=session).login()
    assert account_url == "https://bbs.kfpromax.com/kf_growup.php"
    assert [method for method, _url in session.calls] == ["GET", "POST", "GET"]


@pytest.mark.parametrize(
    ("username", "encoded_username"),
    [
        ("13189262189", "13189262189"),
        ("User_r", "User_r"),
        ("月r", "%D4%C2r"),
    ],
)
def test_login_form_supports_numeric_english_and_chinese_usernames(
    username: str, encoded_username: str
) -> None:
    index = '<a href="kf_growup.php">100KFB | 0贡献</a>'
    session = FakeSession(
        [
            make_response('<input name="pwpwd">', "https://bbs.kfpromax.com/login.php"),
            make_response(index),
            make_response(index),
        ]
    )
    config = Config(username, "test-password", ZoneInfo("Asia/Shanghai"), time(8, 0))
    ForumClient(config, session=session).login()
    assert session.post_kwargs is not None
    assert f"pwuser={encoded_username}" in str(session.post_kwargs["data"])
    if username == "月r":
        assert "%E6%9C%88" not in str(session.post_kwargs["data"])


def test_wrong_password_is_non_retryable() -> None:
    login = '<form><input name="pwpwd" type="password"></form>'
    session = FakeSession([make_response(login), make_response(login), make_response(login)])
    with pytest.raises(AuthenticationError) as error:
        ForumClient(make_config(), session=session).login()
    assert error.value.retryable is False


def test_unknown_post_login_structure_is_non_retryable() -> None:
    unknown = "<html><div>页面维护中</div></html>"
    session = FakeSession([make_response(unknown), make_response(unknown), make_response(unknown)])
    with pytest.raises(AuthenticationError) as error:
        ForumClient(make_config(), session=session).login()
    assert error.value.retryable is False


def test_cookie_expiry_on_reward_page_is_reported() -> None:
    index = '<a href="kf_growup.php">100KFB | 0贡献</a>'
    login = '<form><input name="pwpwd" type="password"></form>'
    session = FakeSession(
        [
            make_response(login),
            make_response(index),
            make_response(index),
            make_response(login, "https://bbs.kfpromax.com/login.php"),
        ]
    )
    result = ForumClient(make_config(), session=session).checkin()
    assert result.status is CheckinStatus.FAILED
    assert result.retryable is True
    assert "登录状态已失效" in result.message


def test_find_account_balance_link() -> None:
    html = '<a href="u.php?action=show&amp;uid=123">79777KFB | 0贡献</a>'
    assert find_account_url(html) == "https://bbs.kfpromax.com/u.php?action=show&uid=123"


def test_reward_available() -> None:
    html = """
      <div>你可以领取 90KFB + 180经验 + 0贡献
        <a href="userpay.php?action=reward">请点击这里</a>
      </div>
    """
    reward = parse_reward_page(html, "https://bbs.kfpromax.com/u.php?uid=123")
    assert reward.state is RewardState.AVAILABLE
    assert reward.reward_text == "90KFB + 180经验 + 0贡献"
    assert reward.claim_url == "https://bbs.kfpromax.com/userpay.php?action=reward"


@pytest.mark.parametrize(
    "text",
    [
        "今日奖励已经领取",
        "今天已领取登录奖励",
        "已领取今日奖励",
        "今日奖励领取完毕",
        "今天的每日奖励已经领过了，请明天继续。",
    ],
)
def test_reward_already_claimed(text: str) -> None:
    reward = parse_reward_page(f"<div>{text}</div>", "https://bbs.kfpromax.com/u.php")
    assert reward.state is RewardState.CLAIMED


def test_reward_unknown_is_safe_failure() -> None:
    reward = parse_reward_page("<div>登录奖励说明</div>", "https://bbs.kfpromax.com/u.php")
    assert reward.state is RewardState.UNKNOWN


def test_external_claim_link_is_rejected() -> None:
    html = '<div>你可以领取 90KFB <a href="https://evil.example/claim">请点击这里</a></div>'
    with pytest.raises(ForumError, match="同源"):
        parse_reward_page(html, "https://bbs.kfpromax.com/u.php")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://bbs.kfpromax.com/userpay.php?a=1", True),
        ("http://bbs.kfpromax.com/userpay.php?a=1", False),
        ("https://bbs.kfpromax.com.evil.example/a", False),
        ("https://user:pass@bbs.kfpromax.com/a", False),
    ],
)
def test_safe_forum_url(url: str, expected: bool) -> None:
    assert is_safe_forum_url(url) is expected


def test_retry_stops_after_success() -> None:
    results = iter(
        [
            CheckinResult(CheckinStatus.FAILED, "temporary", retryable=True),
            CheckinResult(CheckinStatus.CLAIMED, "ok"),
        ]
    )
    sleeps: list[float] = []
    result = run_with_retries(lambda: next(results), retry_delays=(5, 15), sleep=sleeps.append)
    assert result.status is CheckinStatus.CLAIMED
    assert sleeps == [5]


def test_non_retryable_failure_does_not_sleep() -> None:
    sleeps: list[float] = []
    failed = CheckinResult(CheckinStatus.FAILED, "bad password", retryable=False)
    result = run_with_retries(lambda: failed, sleep=sleeps.append)
    assert result is failed
    assert sleeps == []


def test_next_run_today_or_tomorrow() -> None:
    tz = ZoneInfo("Asia/Shanghai")
    before = datetime(2026, 7, 17, 7, 30, tzinfo=tz)
    after = datetime(2026, 7, 17, 8, 30, tzinfo=tz)
    assert next_scheduled_run(before, time(8, 0)) == datetime(2026, 7, 17, 8, 0, tzinfo=tz)
    assert next_scheduled_run(after, time(8, 0)) == datetime(2026, 7, 18, 8, 0, tzinfo=tz)
