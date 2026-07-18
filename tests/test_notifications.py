from __future__ import annotations

import json
import logging
from datetime import datetime, time
from typing import Callable
from zoneinfo import ZoneInfo

import pytest
import requests

from checkin import (
    AppConfig,
    CheckinResult,
    CheckinStatus,
    NotificationMode,
    PushLiteConfig,
    PushLiteNotifier,
    SiteJob,
    once_exit_code,
    run_all_once,
    run_site_once,
)


TIMEZONE = ZoneInfo("Asia/Shanghai")
FIXED_NOW = datetime(2026, 7, 18, 8, 30, tzinfo=TIMEZONE)


def base_env() -> dict[str, str]:
    return {
        "KF_USERNAME": "forum-user",
        "KF_PASSWORD": "forum-password",
        "TZ": "Asia/Shanghai",
    }


def push_env(**overrides: str) -> dict[str, str]:
    env = {
        **base_env(),
        "PUSH_LITE_URL": "http://astrbot:9966/send",
        "PUSH_LITE_TOKEN": "push-secret",
        "PUSH_LITE_UMO": "umo:test-session",
    }
    env.update(overrides)
    return env


def make_response(status_code: int, payload: object | None = None) -> requests.Response:
    response = requests.Response()
    response.status_code = status_code
    if payload is not None:
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps(payload).encode("utf-8")
    else:
        response._content = b""
    return response


def make_notifier(
    post: Callable[..., requests.Response],
    *,
    mode: NotificationMode = NotificationMode.FAILURE,
    sleep: Callable[[float], None] = lambda _delay: None,
) -> PushLiteNotifier:
    config = PushLiteConfig(
        "http://astrbot:9966/send",
        "push-secret",
        "umo:test-session",
        mode,
        7.5,
    )
    return PushLiteNotifier(
        config,
        post=post,
        sleep=sleep,
        now=lambda _timezone: FIXED_NOW,
    )


def test_push_lite_is_disabled_when_endpoint_settings_are_empty() -> None:
    config = AppConfig.from_mapping(base_env())
    assert config.push_lite is None


def test_complete_push_lite_config_uses_defaults_and_overrides() -> None:
    default_config = AppConfig.from_mapping(push_env()).push_lite
    assert default_config == PushLiteConfig(
        "http://astrbot:9966/send",
        "push-secret",
        "umo:test-session",
        NotificationMode.FAILURE,
        10.0,
    )

    all_config = AppConfig.from_mapping(
        push_env(PUSH_LITE_NOTIFY_MODE="all", PUSH_LITE_TIMEOUT="3.5")
    ).push_lite
    assert all_config is not None
    assert all_config.mode is NotificationMode.ALL
    assert all_config.timeout == 3.5


@pytest.mark.parametrize(
    "settings",
    [
        {"PUSH_LITE_URL": "http://astrbot:9966/send"},
        {"PUSH_LITE_TOKEN": "token"},
        {"PUSH_LITE_UMO": "umo"},
        {
            "PUSH_LITE_URL": "http://astrbot:9966/send",
            "PUSH_LITE_TOKEN": "token",
        },
    ],
)
def test_partial_push_lite_config_is_rejected(settings: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="必须同时设置"):
        AppConfig.from_mapping({**base_env(), **settings})


@pytest.mark.parametrize(
    ("variable", "value", "message"),
    [
        ("PUSH_LITE_NOTIFY_MODE", "success", "failure 或 all"),
        ("PUSH_LITE_TIMEOUT", "zero", "必须是秒数"),
        ("PUSH_LITE_TIMEOUT", "0", "必须大于 0"),
    ],
)
def test_invalid_notification_options_are_rejected(
    variable: str, value: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        AppConfig.from_mapping(push_env(**{variable: value}))


@pytest.mark.parametrize(
    "url",
    [
        "ftp://astrbot/send",
        "http:///send",
        "http://astrbot:9966",
        "http://astrbot:9966/health",
        "http://user:password@astrbot:9966/send",
        "http://astrbot:70000/send",
        "http://astrbot:9966/send#token",
    ],
)
def test_unsafe_or_invalid_push_lite_urls_are_rejected(url: str) -> None:
    with pytest.raises(ValueError, match="PUSH_LITE_URL"):
        AppConfig.from_mapping(push_env(PUSH_LITE_URL=url))


def test_failure_notification_matches_push_lite_protocol() -> None:
    calls: list[dict[str, object]] = []

    def post(url: str, **kwargs: object) -> requests.Response:
        calls.append({"url": url, **kwargs})
        return make_response(200, {"status": "queued", "message_id": "message-1"})

    notifier = make_notifier(post)
    result = CheckinResult(CheckinStatus.FAILED, "登录状态失效")

    assert notifier.notify("绯月", result, TIMEZONE, logging.getLogger("test"))
    assert len(calls) == 1
    assert calls[0]["url"] == "http://astrbot:9966/send"
    assert calls[0]["headers"] == {"Authorization": "Bearer push-secret"}
    assert calls[0]["timeout"] == 7.5
    assert calls[0]["json"] == {
        "content": (
            "【签到失败告警】绯月\n"
            "时间：2026-07-18T08:30:00+08:00\n"
            "结果：登录状态失效"
        ),
        "umo": "umo:test-session",
        "message_type": "text",
    }


def test_failure_mode_skips_success_and_already_claimed() -> None:
    calls: list[object] = []
    notifier = make_notifier(
        lambda *_args, **_kwargs: calls.append(object()) or make_response(200)
    )

    for status in (CheckinStatus.CLAIMED, CheckinStatus.ALREADY_CLAIMED):
        result = CheckinResult(status, "签到完成")
        assert notifier.notify("yngal", result, TIMEZONE, logging.getLogger("test"))

    assert calls == []


@pytest.mark.parametrize(
    ("result", "expected_text"),
    [
        (
            CheckinResult(CheckinStatus.CLAIMED, "领取成功", reward_text="硬币 +2"),
            "【签到成功】yngal",
        ),
        (
            CheckinResult(CheckinStatus.ALREADY_CLAIMED, "今天已经领取过硬币"),
            "【签到成功】yngal",
        ),
        (
            CheckinResult(CheckinStatus.FAILED, "网络请求失败"),
            "【签到失败告警】yngal",
        ),
    ],
)
def test_all_mode_sends_every_final_status(
    result: CheckinResult, expected_text: str
) -> None:
    contents: list[str] = []

    def post(_url: str, **kwargs: object) -> requests.Response:
        payload = kwargs["json"]
        assert isinstance(payload, dict)
        contents.append(str(payload["content"]))
        return make_response(200, {"status": "queued"})

    notifier = make_notifier(post, mode=NotificationMode.ALL)
    assert notifier.notify("yngal", result, TIMEZONE, logging.getLogger("test"))
    assert expected_text in contents[0]
    if result.reward_text:
        assert "奖励：硬币 +2" in contents[0]


@pytest.mark.parametrize(
    "first_outcome",
    [
        requests.ConnectionError("connection details must not be logged"),
        requests.Timeout("timeout details must not be logged"),
        make_response(429),
        make_response(500),
    ],
)
def test_transient_notification_failures_retry_with_short_delays(
    first_outcome: requests.Response | Exception,
) -> None:
    outcomes = [first_outcome, make_response(200, {"status": "queued"})]
    sleeps: list[float] = []
    calls = 0

    def post(_url: str, **_kwargs: object) -> requests.Response:
        nonlocal calls
        calls += 1
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    notifier = make_notifier(post, sleep=sleeps.append)
    result = CheckinResult(CheckinStatus.FAILED, "temporary")

    assert notifier.notify("绯月", result, TIMEZONE, logging.getLogger("test"))
    assert calls == 2
    assert sleeps == [2]


@pytest.mark.parametrize(
    "response",
    [
        make_response(400),
        make_response(403),
        make_response(200),
        make_response(200, {"status": "sent"}),
    ],
)
def test_deterministic_notification_errors_do_not_retry(
    response: requests.Response,
) -> None:
    calls = 0
    sleeps: list[float] = []

    def post(_url: str, **_kwargs: object) -> requests.Response:
        nonlocal calls
        calls += 1
        return response

    notifier = make_notifier(post, sleep=sleeps.append)
    result = CheckinResult(CheckinStatus.FAILED, "failed")

    assert not notifier.notify("绯月", result, TIMEZONE, logging.getLogger("test"))
    assert calls == 1
    assert sleeps == []


def test_only_final_checkin_result_is_notified_after_retries() -> None:
    results = iter(
        [
            CheckinResult(CheckinStatus.FAILED, "temporary", retryable=True),
            CheckinResult(CheckinStatus.FAILED, "final failure"),
        ]
    )
    contents: list[str] = []

    def post(_url: str, **kwargs: object) -> requests.Response:
        payload = kwargs["json"]
        assert isinstance(payload, dict)
        contents.append(str(payload["content"]))
        return make_response(200, {"status": "queued"})

    notifier = make_notifier(post)
    job = SiteJob("绯月", TIMEZONE, time(8), lambda _logger: next(results))
    result = run_site_once(
        job,
        logging.getLogger("test-final-result"),
        retry_delays=(0,),
        sleep=lambda _delay: None,
        notifier=notifier,
    )

    assert result.message == "final failure"
    assert len(contents) == 1
    assert "final failure" in contents[0]
    assert "temporary" not in contents[0]


def test_notification_failure_does_not_change_results_or_exit_code() -> None:
    calls = 0

    def post(_url: str, **_kwargs: object) -> requests.Response:
        nonlocal calls
        calls += 1
        return make_response(503)

    notifier = make_notifier(post)
    success = CheckinResult(CheckinStatus.CLAIMED, "ok")
    failure = CheckinResult(CheckinStatus.FAILED, "checkin failed")
    jobs = [
        SiteJob("success", TIMEZONE, time(8), lambda _logger: success),
        SiteJob("failure", TIMEZONE, time(8), lambda _logger: failure),
    ]

    results = run_all_once(jobs, logging.getLogger("test-exit-code"), notifier)

    assert results == {"success": success, "failure": failure}
    assert once_exit_code(results) == 1
    assert calls == 3
