#!/usr/bin/env python3
"""绯月论坛每日登录奖励领取工具。"""

from __future__ import annotations

import argparse
import logging
import os
import re
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, time as datetime_time, timedelta
from enum import Enum
from typing import Callable, Iterable
from urllib.parse import urlencode, urljoin, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv


BASE_URL = "https://bbs.kfpromax.com/"
LOGIN_URL = urljoin(BASE_URL, "login.php")
INDEX_URL = urljoin(BASE_URL, "index.php")
DEFAULT_RETRY_DELAYS = (300, 900, 1800)
USER_AGENT = "KFCheckin/1.0 (+personal daily reward client)"


class CheckinStatus(str, Enum):
    CLAIMED = "claimed"
    ALREADY_CLAIMED = "already_claimed"
    FAILED = "failed"


class RewardState(str, Enum):
    AVAILABLE = "available"
    CLAIMED = "claimed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Config:
    username: str
    password: str
    timezone: ZoneInfo
    checkin_time: datetime_time
    timeout: float = 20.0

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        username = os.getenv("KF_USERNAME", "").strip()
        password = os.getenv("KF_PASSWORD", "")
        timezone_name = os.getenv("TZ", "Asia/Shanghai").strip()
        checkin_time_text = os.getenv("CHECKIN_TIME", "08:00").strip()

        if not username or not password:
            raise ValueError("KF_USERNAME 和 KF_PASSWORD 必须在 .env 或环境变量中设置")
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"未知时区：{timezone_name}") from exc
        try:
            checkin_time = datetime.strptime(checkin_time_text, "%H:%M").time()
        except ValueError as exc:
            raise ValueError("CHECKIN_TIME 必须使用 HH:MM 格式，例如 08:00") from exc

        timeout_text = os.getenv("REQUEST_TIMEOUT", "20").strip()
        try:
            timeout = float(timeout_text)
        except ValueError as exc:
            raise ValueError("REQUEST_TIMEOUT 必须是秒数") from exc
        if timeout <= 0:
            raise ValueError("REQUEST_TIMEOUT 必须大于 0")

        return cls(username, password, timezone, checkin_time, timeout)


@dataclass(frozen=True)
class RewardPage:
    state: RewardState
    claim_url: str | None = None
    reward_text: str | None = None


@dataclass(frozen=True)
class CheckinResult:
    status: CheckinStatus
    message: str
    reward_text: str | None = None
    retryable: bool = False


class ForumError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class AuthenticationError(ForumError):
    pass


def decode_html(response: requests.Response) -> str:
    """该站声明 GBK；GB18030 是其兼容超集。"""
    return response.content.decode("gb18030", errors="replace")


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def is_safe_forum_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "bbs.kfpromax.com"
        and parsed.port in (None, 443)
        and parsed.username is None
        and parsed.password is None
    )


def is_login_page(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    return soup.find("input", attrs={"name": "pwpwd"}) is not None


def find_account_url(html: str, page_url: str = INDEX_URL) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.find_all("a", href=True):
        text = normalize_text(anchor.get_text(" ", strip=True))
        if re.search(r"\d[\d,.]*\s*KFB\b", text, flags=re.IGNORECASE):
            candidate = urljoin(page_url, anchor["href"])
            if is_safe_forum_url(candidate):
                return candidate
    return None


def parse_reward_page(html: str, page_url: str) -> RewardPage:
    soup = BeautifulSoup(html, "html.parser")
    full_text = normalize_text(soup.get_text(" ", strip=True))

    already_patterns = (
        r"(?:今日|今天).{0,12}(?:已经|已).{0,4}领(?:取|过)",
        r"(?:已经|已).{0,4}领(?:取|过).{0,12}(?:今日|今天)",
        r"(?:今日|今天).{0,10}领取完毕",
    )
    if any(re.search(pattern, full_text) for pattern in already_patterns):
        return RewardPage(RewardState.CLAIMED)

    for anchor in soup.find_all("a", href=True):
        anchor_text = normalize_text(anchor.get_text(" ", strip=True))
        container = anchor.find_parent(["td", "div", "p", "li"]) or anchor.parent
        context = normalize_text(container.get_text(" ", strip=True)) if container else anchor_text
        if "可以领取" not in context:
            continue
        if "点击这里" not in anchor_text and "领取" not in anchor_text:
            continue

        claim_url = urljoin(page_url, anchor["href"])
        if not is_safe_forum_url(claim_url):
            raise ForumError("奖励领取链接不是论坛 HTTPS 同源链接，已拒绝访问", retryable=False)

        reward_match = re.search(r"可以领取\s*(.*?)\s*请点击这里", context)
        reward_text = normalize_text(reward_match.group(1)) if reward_match else context[:160]
        return RewardPage(RewardState.AVAILABLE, claim_url, reward_text)

    return RewardPage(RewardState.UNKNOWN)


class ForumClient:
    def __init__(
        self,
        config: Config,
        *,
        session: requests.Session | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"})
        self.logger = logger or logging.getLogger("kf_checkin")

    def _get(self, url: str) -> requests.Response:
        response = self.session.get(url, timeout=self.config.timeout)
        response.raise_for_status()
        return response

    def login(self) -> tuple[str, str]:
        self._get(LOGIN_URL)
        payload = {
            "forward": "",
            "jumpurl": INDEX_URL,
            "step": "2",
            "lgt": "1",
            "hideid": "0",
            "cktime": "31536000",
            "pwuser": self.config.username,
            "pwpwd": self.config.password,
            "submit": "登录",
        }
        # 论坛运行在传统 GBK/PHP 环境中。requests 对字典默认使用 UTF-8，
        # 会导致中文用户名在服务端变成乱码，因此在这里显式生成 GBK 表单体。
        encoded_payload = urlencode(payload, encoding="gb18030", errors="strict")
        response = self.session.post(
            LOGIN_URL,
            data=encoded_payload,
            headers={
                "Referer": LOGIN_URL,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=self.config.timeout,
        )
        response.raise_for_status()

        # 无论登录 POST 最终落在哪一页，都重新读取首页作为权威认证状态。
        index_response = self._get(INDEX_URL)
        index_html = decode_html(index_response)
        account_url = find_account_url(index_html, index_response.url)
        if is_login_page(index_html) or account_url is None:
            raise AuthenticationError("登录失败，或登录后的账户入口无法识别", retryable=False)
        return index_html, account_url

    def checkin(self) -> CheckinResult:
        try:
            _, account_url = self.login()
            account_response = self._get(account_url)
            account_html = decode_html(account_response)
            if is_login_page(account_html):
                raise AuthenticationError("读取奖励页面时登录状态已失效")

            reward = parse_reward_page(account_html, account_response.url)
            if reward.state is RewardState.CLAIMED:
                return CheckinResult(CheckinStatus.ALREADY_CLAIMED, "今天已经领取过奖励")
            if reward.state is RewardState.UNKNOWN or reward.claim_url is None:
                raise ForumError("无法在账户页面识别领取状态；为避免误操作，本次未点击任何链接")

            self.logger.info("检测到可领取奖励：%s", reward.reward_text or "金额未解析")
            claim_response = self._get(reward.claim_url)
            if is_login_page(decode_html(claim_response)):
                raise AuthenticationError("领取过程中登录状态已失效")

            verify_response = self._get(account_url)
            verified = parse_reward_page(decode_html(verify_response), verify_response.url)
            if verified.state is RewardState.CLAIMED:
                return CheckinResult(
                    CheckinStatus.CLAIMED,
                    "奖励领取成功",
                    reward_text=reward.reward_text,
                )
            if verified.state is RewardState.AVAILABLE:
                raise ForumError("领取请求完成，但页面仍显示可以领取")
            raise ForumError("领取后无法从账户页面确认结果")
        except AuthenticationError as exc:
            return CheckinResult(CheckinStatus.FAILED, str(exc), retryable=exc.retryable)
        except ForumError as exc:
            return CheckinResult(CheckinStatus.FAILED, str(exc), retryable=exc.retryable)
        except requests.RequestException as exc:
            return CheckinResult(
                CheckinStatus.FAILED,
                f"网络请求失败：{exc.__class__.__name__}",
                retryable=True,
            )


def run_with_retries(
    operation: Callable[[], CheckinResult],
    *,
    retry_delays: Iterable[int] = DEFAULT_RETRY_DELAYS,
    sleep: Callable[[float], None] = time.sleep,
    logger: logging.Logger | None = None,
) -> CheckinResult:
    log = logger or logging.getLogger("kf_checkin")
    result = operation()
    for delay in retry_delays:
        if result.status is not CheckinStatus.FAILED or not result.retryable:
            return result
        log.warning("签到失败，%s 秒后重试：%s", delay, result.message)
        sleep(delay)
        result = operation()
    return result


def next_scheduled_run(now: datetime, target: datetime_time) -> datetime:
    candidate = datetime.combine(now.date(), target, tzinfo=now.tzinfo)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def log_result(result: CheckinResult, logger: logging.Logger) -> None:
    if result.status is CheckinStatus.FAILED:
        logger.error("签到失败：%s", result.message)
    elif result.status is CheckinStatus.ALREADY_CLAIMED:
        logger.info("签到完成：%s", result.message)
    else:
        suffix = f"（{result.reward_text}）" if result.reward_text else ""
        logger.info("签到完成：%s%s", result.message, suffix)


def run_once(config: Config, logger: logging.Logger) -> CheckinResult:
    client = ForumClient(config, logger=logger)
    result = run_with_retries(client.checkin, logger=logger)
    log_result(result, logger)
    return result


def run_daemon(config: Config, logger: logging.Logger) -> None:
    stop_event = threading.Event()

    def request_stop(signum: int, _frame: object) -> None:
        logger.info("收到信号 %s，准备退出", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    logger.info(
        "签到服务启动；启动时立即检查，之后每天 %s %s 执行",
        config.timezone.key,
        config.checkin_time.strftime("%H:%M"),
    )
    run_once(config, logger)

    while not stop_event.is_set():
        now = datetime.now(config.timezone)
        next_run = next_scheduled_run(now, config.checkin_time)
        seconds = max(0.0, (next_run - now).total_seconds())
        logger.info("下次签到时间：%s", next_run.isoformat(timespec="minutes"))
        if stop_event.wait(seconds):
            break
        run_once(config, logger)

    logger.info("签到服务已停止")


def configure_logging() -> logging.Logger:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    return logging.getLogger("kf_checkin")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="绯月论坛每日登录奖励领取工具")
    parser.add_argument("mode", nargs="?", choices=("daemon", "once"), default="daemon")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    logger = configure_logging()
    try:
        config = Config.from_env()
    except ValueError as exc:
        logger.error("配置错误：%s", exc)
        return 2

    if args.mode == "once":
        result = run_once(config, logger)
        return 0 if result.status is not CheckinStatus.FAILED else 1

    run_daemon(config, logger)
    return 0


if __name__ == "__main__":
    sys.exit(main())
