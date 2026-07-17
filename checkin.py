#!/usr/bin/env python3
"""多站点每日登录奖励领取工具。"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, time as datetime_time, timedelta
from enum import Enum
from typing import Callable, Iterable, Mapping
from urllib.parse import urlencode, urljoin, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv


BASE_URL = "https://bbs.kfpromax.com/"
LOGIN_URL = urljoin(BASE_URL, "login.php")
INDEX_URL = urljoin(BASE_URL, "index.php")
YNGAL_BASE_URL = "https://www.yngal.com/"
YNGAL_LOGIN_URL = urljoin(YNGAL_BASE_URL, "sign")
YNGAL_REWARD_URL = urljoin(YNGAL_BASE_URL, "addJf")
DEFAULT_RETRY_DELAYS = (300, 900, 1800)
USER_AGENT = "KFCheckin/1.0 (+personal daily reward client)"
YNGAL_USER_AGENT = "MultiSiteCheckin/1.0 (+personal daily reward client)"


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
        checkin_time_text = os.getenv("KF_CHECKIN_TIME", "08:00").strip()

        if not username or not password:
            raise ValueError("KF_USERNAME 和 KF_PASSWORD 必须在 .env 或环境变量中设置")
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"未知时区：{timezone_name}") from exc
        try:
            checkin_time = datetime.strptime(checkin_time_text, "%H:%M").time()
        except ValueError as exc:
            raise ValueError("KF_CHECKIN_TIME 必须使用 HH:MM 格式，例如 08:00") from exc

        timeout_text = os.getenv("REQUEST_TIMEOUT", "20").strip()
        try:
            timeout = float(timeout_text)
        except ValueError as exc:
            raise ValueError("REQUEST_TIMEOUT 必须是秒数") from exc
        if timeout <= 0:
            raise ValueError("REQUEST_TIMEOUT 必须大于 0")

        return cls(username, password, timezone, checkin_time, timeout)


@dataclass(frozen=True)
class YngalConfig:
    email: str
    password: str
    timezone: ZoneInfo
    checkin_time: datetime_time
    timeout: float = 20.0


@dataclass(frozen=True)
class AppConfig:
    forum: Config | None
    yngal: YngalConfig | None
    timezone: ZoneInfo

    @classmethod
    def from_env(cls) -> "AppConfig":
        load_dotenv()
        return cls.from_mapping(os.environ)

    @classmethod
    def from_mapping(cls, env: Mapping[str, str]) -> "AppConfig":
        timezone_name = env.get("TZ", "Asia/Shanghai").strip()
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"未知时区：{timezone_name}") from exc

        timeout_text = env.get("REQUEST_TIMEOUT", "20").strip()
        try:
            timeout = float(timeout_text)
        except ValueError as exc:
            raise ValueError("REQUEST_TIMEOUT 必须是秒数") from exc
        if timeout <= 0:
            raise ValueError("REQUEST_TIMEOUT 必须大于 0")

        def parse_time(variable: str, default: str = "08:00") -> datetime_time:
            value = env.get(variable, default).strip()
            try:
                return datetime.strptime(value, "%H:%M").time()
            except ValueError as exc:
                raise ValueError(f"{variable} 必须使用 HH:MM 格式，例如 08:00") from exc

        def optional_pair(first_name: str, second_name: str) -> tuple[str, str] | None:
            first = env.get(first_name, "").strip()
            second = env.get(second_name, "")
            if bool(first) != bool(second):
                raise ValueError(f"{first_name} 和 {second_name} 必须同时设置或同时留空")
            return (first, second) if first else None

        forum_credentials = optional_pair("KF_USERNAME", "KF_PASSWORD")
        yngal_credentials = optional_pair("YNGAL_EMAIL", "YNGAL_PASSWORD")
        if forum_credentials is None and yngal_credentials is None:
            raise ValueError("至少需要配置一个站点的账号和密码")

        forum = None
        if forum_credentials is not None:
            forum = Config(
                forum_credentials[0],
                forum_credentials[1],
                timezone,
                parse_time("KF_CHECKIN_TIME"),
                timeout,
            )

        yngal = None
        if yngal_credentials is not None:
            yngal = YngalConfig(
                yngal_credentials[0],
                yngal_credentials[1],
                timezone,
                parse_time("YNGAL_CHECKIN_TIME"),
                timeout,
            )
        return cls(forum, yngal, timezone)


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


def is_safe_yngal_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "www.yngal.com"
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
        logger: logging.Logger | logging.LoggerAdapter | None = None,
    ) -> None:
        self.config = config
        self.session = session or requests.Session()
        self.session.headers.update(
            {"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"}
        )
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


@dataclass(frozen=True)
class YngalLogin:
    token: str
    reward_amount: int


class YngalClient:
    def __init__(
        self,
        config: YngalConfig,
        *,
        session: requests.Session | None = None,
        logger: logging.Logger | logging.LoggerAdapter | None = None,
    ) -> None:
        self.config = config
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": YNGAL_USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
        )
        self.logger = logger or logging.getLogger("multi_checkin")

    def _ensure_safe_url(self, url: str) -> None:
        if not is_safe_yngal_url(url):
            raise ForumError("yngal 请求地址不是 HTTPS 同源链接，已拒绝访问", retryable=False)

    @staticmethod
    def _json_object(response: requests.Response, action: str) -> dict[str, object]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise ForumError(f"yngal {action}返回了无法解析的数据") from exc
        if not isinstance(payload, dict):
            raise ForumError(f"yngal {action}返回格式不符合预期")
        return payload

    def login(self) -> YngalLogin:
        self._ensure_safe_url(YNGAL_LOGIN_URL)
        password_digest = hashlib.md5(
            self.config.password.encode("utf-8"), usedforsecurity=False
        ).hexdigest()
        response = self.session.post(
            YNGAL_LOGIN_URL,
            data={"email": self.config.email, "password": password_digest},
            timeout=self.config.timeout,
        )
        response.raise_for_status()
        payload = self._json_object(response, "登录接口")
        if payload.get("code") != 0:
            raise AuthenticationError("yngal 登录失败，请检查账号和密码", retryable=False)

        user = payload.get("obj")
        if not isinstance(user, dict):
            raise ForumError("yngal 登录成功响应缺少用户信息")
        token = user.get("token")
        if not isinstance(token, str) or not token:
            raise ForumError("yngal 登录成功响应缺少 token")

        reward_amount = 2 if user.get("vstatus") in (1, "1") else 1
        return YngalLogin(token, reward_amount)

    def checkin(self) -> CheckinResult:
        try:
            login = self.login()
            self._ensure_safe_url(YNGAL_REWARD_URL)
            response = self.session.get(
                YNGAL_REWARD_URL,
                headers={"X-Auth-Token": login.token},
                timeout=self.config.timeout,
            )
            response.raise_for_status()
            payload = self._json_object(response, "签到接口")
            code = payload.get("code")
            if code == 0:
                return CheckinResult(
                    CheckinStatus.CLAIMED,
                    "当天首次访问奖励领取成功",
                    reward_text=f"硬币 +{login.reward_amount}",
                )
            if code == 10:
                return CheckinResult(CheckinStatus.ALREADY_CLAIMED, "今天已经领取过硬币")
            if code == 119:
                raise AuthenticationError("yngal 签到时登录状态失效", retryable=True)
            raise ForumError("yngal 签到接口返回未知状态")
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
    logger: logging.Logger | logging.LoggerAdapter | None = None,
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


def log_result(
    result: CheckinResult, logger: logging.Logger | logging.LoggerAdapter
) -> None:
    if result.status is CheckinStatus.FAILED:
        logger.error("签到失败：%s", result.message)
    elif result.status is CheckinStatus.ALREADY_CLAIMED:
        logger.info("签到完成：%s", result.message)
    else:
        suffix = f"（{result.reward_text}）" if result.reward_text else ""
        logger.info("签到完成：%s%s", result.message, suffix)


class SiteLoggerAdapter(logging.LoggerAdapter):
    def __init__(self, logger: logging.Logger, site_name: str) -> None:
        super().__init__(logger, {})
        self.site_name = site_name

    def process(
        self, msg: object, kwargs: dict[str, object]
    ) -> tuple[str, dict[str, object]]:
        return f"[{self.site_name}] {msg}", kwargs


@dataclass(frozen=True)
class SiteJob:
    name: str
    timezone: ZoneInfo
    checkin_time: datetime_time
    operation: Callable[[logging.Logger | logging.LoggerAdapter], CheckinResult]


def build_site_jobs(config: AppConfig) -> list[SiteJob]:
    jobs: list[SiteJob] = []
    if config.forum is not None:
        forum_config = config.forum
        jobs.append(
            SiteJob(
                "绯月",
                forum_config.timezone,
                forum_config.checkin_time,
                lambda site_log, current=forum_config: ForumClient(
                    current, logger=site_log
                ).checkin(),
            )
        )
    if config.yngal is not None:
        yngal_config = config.yngal
        jobs.append(
            SiteJob(
                "yngal",
                yngal_config.timezone,
                yngal_config.checkin_time,
                lambda site_log, current=yngal_config: YngalClient(
                    current, logger=site_log
                ).checkin(),
            )
        )
    return jobs


def run_site_once(
    job: SiteJob,
    logger: logging.Logger,
    *,
    retry_delays: Iterable[int] = DEFAULT_RETRY_DELAYS,
    sleep: Callable[[float], None] = time.sleep,
) -> CheckinResult:
    site_log = SiteLoggerAdapter(logger, job.name)
    try:
        result = run_with_retries(
            lambda: job.operation(site_log),
            retry_delays=retry_delays,
            sleep=sleep,
            logger=site_log,
        )
    except Exception as exc:  # pragma: no cover - 防止常驻线程因意外异常退出
        site_log.exception("签到发生未处理异常：%s", exc.__class__.__name__)
        result = CheckinResult(CheckinStatus.FAILED, "签到发生未处理异常")
    log_result(result, site_log)
    return result


def run_all_once(jobs: list[SiteJob], logger: logging.Logger) -> dict[str, CheckinResult]:
    results: dict[str, CheckinResult] = {}
    with ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="checkin") as executor:
        futures = {executor.submit(run_site_once, job, logger): job.name for job in jobs}
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    return {job.name: results[job.name] for job in jobs}


def once_exit_code(results: Mapping[str, CheckinResult]) -> int:
    return 0 if all(
        result.status is not CheckinStatus.FAILED for result in results.values()
    ) else 1


def run_once(config: Config, logger: logging.Logger) -> CheckinResult:
    """保留原有单站点调用入口，便于现有使用方平滑升级。"""
    job = SiteJob(
        "绯月",
        config.timezone,
        config.checkin_time,
        lambda site_log: ForumClient(config, logger=site_log).checkin(),
    )
    return run_site_once(job, logger)


def _run_site_daemon(job: SiteJob, stop_event: threading.Event, logger: logging.Logger) -> None:
    site_log = SiteLoggerAdapter(logger, job.name)
    site_log.info(
        "任务启动；启动时立即检查，之后每天 %s %s 执行",
        job.timezone.key,
        job.checkin_time.strftime("%H:%M"),
    )
    run_site_once(job, logger)

    while not stop_event.is_set():
        now = datetime.now(job.timezone)
        next_run = next_scheduled_run(now, job.checkin_time)
        seconds = max(0.0, (next_run - now).total_seconds())
        site_log.info("下次签到时间：%s", next_run.isoformat(timespec="minutes"))
        if stop_event.wait(seconds):
            break
        run_site_once(job, logger)

    site_log.info("任务已停止")


def run_daemon(config: AppConfig, logger: logging.Logger) -> None:
    stop_event = threading.Event()

    def request_stop(signum: int, _frame: object) -> None:
        logger.info("收到信号 %s，准备退出", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    jobs = build_site_jobs(config)
    logger.info("多站点签到服务启动；已启用 %s", "、".join(job.name for job in jobs))
    threads = [
        threading.Thread(
            target=_run_site_daemon,
            args=(job, stop_event, logger),
            name=f"checkin-{job.name}",
        )
        for job in jobs
    ]
    for thread in threads:
        thread.start()
    while any(thread.is_alive() for thread in threads):
        for thread in threads:
            thread.join(timeout=0.5)
    logger.info("多站点签到服务已停止")


def configure_logging() -> logging.Logger:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    return logging.getLogger("multi_checkin")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="多站点每日登录奖励领取工具")
    parser.add_argument("mode", nargs="?", choices=("daemon", "once"), default="daemon")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    logger = configure_logging()
    try:
        config = AppConfig.from_env()
    except ValueError as exc:
        logger.error("配置错误：%s", exc)
        return 2

    if args.mode == "once":
        results = run_all_once(build_site_jobs(config), logger)
        return once_exit_code(results)

    run_daemon(config, logger)
    return 0


if __name__ == "__main__":
    sys.exit(main())
