from __future__ import annotations

import hashlib
import logging
from datetime import time
from zoneinfo import ZoneInfo

import pytest
import requests

import checkin


class FakeResponse:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def json(self) -> object:
        return self.payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(
        self,
        login_payload: object,
        get_payloads: list[object],
    ) -> None:
        self.headers: dict[str, str] = {}
        self.login_response = FakeResponse(login_payload)
        self.get_responses = [FakeResponse(payload) for payload in get_payloads]
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append(("POST", url, kwargs))
        return self.login_response

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append(("GET", url, kwargs))
        if not self.get_responses:
            raise AssertionError(f"unexpected GET {url}")
        return self.get_responses.pop(0)


def make_config(*, hunt_enabled: bool = True) -> checkin.YngalConfig:
    return checkin.YngalConfig(
        email="person@example.com",
        password="test-password",
        timezone=ZoneInfo("Asia/Shanghai"),
        checkin_time=time(8, 0),
        timeout=12.0,
        hunt_enabled=hunt_enabled,
    )


def login_payload(*, token: str = "test-token", vstatus: int = 0) -> dict[str, object]:
    return {"code": 0, "obj": {"token": token, "vstatus": vstatus}}


def make_client(
    get_payloads: list[object],
    *,
    hunt_enabled: bool = True,
    token: str = "test-token",
) -> tuple[checkin.YngalClient, FakeSession]:
    session = FakeSession(login_payload(token=token), get_payloads)
    return checkin.YngalClient(make_config(hunt_enabled=hunt_enabled), session=session), session


def base_env(**overrides: str) -> dict[str, str]:
    env = {
        "YNGAL_EMAIL": "person@example.com",
        "YNGAL_PASSWORD": "test-password",
    }
    env.update(overrides)
    return env


def test_hunt_enabled_defaults_to_true() -> None:
    config = checkin.AppConfig.from_mapping(base_env())

    assert config.yngal is not None
    assert config.yngal.hunt_enabled is True


@pytest.mark.parametrize("value", ["true", "1", "yes", "on", "TRUE", " On "])
def test_hunt_enabled_accepts_true_values(value: str) -> None:
    config = checkin.AppConfig.from_mapping(base_env(YNGAL_HUNT_ENABLED=value))

    assert config.yngal is not None
    assert config.yngal.hunt_enabled is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "FALSE", " Off "])
def test_hunt_enabled_accepts_false_values(value: str) -> None:
    config = checkin.AppConfig.from_mapping(base_env(YNGAL_HUNT_ENABLED=value))

    assert config.yngal is not None
    assert config.yngal.hunt_enabled is False


def test_hunt_enabled_rejects_unknown_value() -> None:
    with pytest.raises(ValueError, match="YNGAL_HUNT_ENABLED"):
        checkin.AppConfig.from_mapping(base_env(YNGAL_HUNT_ENABLED="sometimes"))


def test_checkin_posts_md5_then_uses_token_for_both_rewards() -> None:
    client, session = make_client(
        [
            {"code": 0},
            {"code": 0, "obj": ["找到积分"], "wrap": 3, "config": 2},
        ],
        token="private-test-token",
    )

    result = client.checkin()

    assert result == checkin.CheckinResult(
        checkin.CheckinStatus.CLAIMED,
        "当天首次访问奖励领取成功；寻宝完成",
        reward_text="硬币 +1；积分 +3",
    )
    assert [call[:2] for call in session.calls] == [
        ("POST", checkin.YNGAL_LOGIN_URL),
        ("GET", checkin.YNGAL_REWARD_URL),
        ("GET", checkin.YNGAL_HUNT_URL),
    ]
    login_form = session.calls[0][2]["data"]
    assert login_form == {
        "email": "person@example.com",
        "password": hashlib.md5(
            b"test-password", usedforsecurity=False
        ).hexdigest(),
    }
    for _, _, kwargs in session.calls[1:]:
        assert kwargs["headers"] == {"X-Auth-Token": "private-test-token"}
        assert kwargs["timeout"] == 12.0


def test_hunt_wrap_ten_means_five_coins() -> None:
    client, _ = make_client(
        [{"code": 10}, {"code": 200, "obj": [], "wrap": "10"}]
    )

    result = client.checkin()

    assert result.status is checkin.CheckinStatus.CLAIMED
    assert result.reward_text == "硬币 +5"
    assert result.message == "今天已经领取过硬币；寻宝完成"


def test_both_rewards_already_completed() -> None:
    client, _ = make_client([{"code": 10}, {"code": 688}])

    result = client.checkin()

    assert result.status is checkin.CheckinStatus.ALREADY_CLAIMED
    assert result.message == "今天已经领取过硬币；今天已经完成寻宝"
    assert result.reward_text is None


def test_hunt_can_be_disabled_without_requesting_endpoint() -> None:
    client, session = make_client([{"code": 0}], hunt_enabled=False)

    result = client.checkin()

    assert result.status is checkin.CheckinStatus.CLAIMED
    assert [call[1] for call in session.calls] == [
        checkin.YNGAL_LOGIN_URL,
        checkin.YNGAL_REWARD_URL,
    ]


def test_missing_guardian_is_informational_and_keeps_exit_code_zero() -> None:
    client, _ = make_client([{"code": 10}, {"code": 602}])

    result = client.checkin()

    assert result.status is checkin.CheckinStatus.ALREADY_CLAIMED
    assert result.message == "今天已经领取过硬币；寻宝未完成：未设置守护灵出战位"
    assert result.retryable is False
    assert checkin.once_exit_code({"yngal": result}) == 0


def test_hunt_authentication_expiry_is_retryable() -> None:
    client, _ = make_client([{"code": 10}, {"code": 601}])

    result = client.checkin()

    assert result.status is checkin.CheckinStatus.FAILED
    assert result.retryable is True
    assert result.message == "yngal 寻宝时登录状态失效"


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"code": 777}, "返回未知状态"),
        ({"code": 0, "wrap": 2}, "缺少奖励报告"),
        ({"code": 0, "obj": [], "wrap": -1}, "奖励数值无效"),
        ({"code": 0, "obj": [], "wrap": True}, "奖励数值无效"),
    ],
)
def test_hunt_rejects_unknown_or_malformed_success(
    payload: dict[str, object], message: str
) -> None:
    client, _ = make_client([{"code": 10}, payload])

    result = client.checkin()

    assert result.status is checkin.CheckinStatus.FAILED
    assert result.retryable is False
    assert message in result.message


def test_hunt_rejects_non_same_origin_url(monkeypatch: pytest.MonkeyPatch) -> None:
    client, session = make_client([])
    monkeypatch.setattr(checkin, "YNGAL_HUNT_URL", "https://example.com/hunt")

    with pytest.raises(checkin.ForumError, match="HTTPS 同源") as exc_info:
        client.hunt("test-token")

    assert exc_info.value.retryable is False
    assert session.calls == []


def test_retry_repeats_login_and_hunt_after_authentication_expiry() -> None:
    first, _ = make_client([{"code": 10}, {"code": 601}])
    second, _ = make_client([{"code": 10}, {"code": 688}])
    clients = iter([first, second])
    sleeps: list[float] = []

    result = checkin.run_with_retries(
        lambda: next(clients).checkin(),
        retry_delays=(5,),
        sleep=sleeps.append,
    )

    assert result.status is checkin.CheckinStatus.ALREADY_CLAIMED
    assert sleeps == [5]


def test_result_logging_does_not_expose_yngal_secrets(caplog: pytest.LogCaptureFixture) -> None:
    secret_password = "test-password"
    secret_digest = hashlib.md5(
        secret_password.encode(), usedforsecurity=False
    ).hexdigest()
    secret_token = "private-test-token"
    client, _ = make_client(
        [{"code": 0}, {"code": 0, "obj": [], "wrap": 4}],
        token=secret_token,
    )
    result = client.checkin()

    with caplog.at_level(logging.INFO):
        checkin.log_result(result, logging.getLogger("test-yngal"))

    logs = caplog.text
    assert secret_password not in logs
    assert secret_digest not in logs
    assert secret_token not in logs
    assert "积分 +4" in logs
