# 多站点自动签到

通过 HTTP 自动领取绯月论坛和 yngal 的每日登录奖励。两个站点可分别启用和设置执行时间；程序启动时会立即检查所有已启用站点，之后默认在北京时间 08:00 执行。

- 绯月：登录 `bbs.kfpromax.com` 并领取账户页面中的奖励。
- yngal：登录 `www.yngal.com`，领取当天首次访问奖励，并可自动完成每日寻宝。

## Docker Compose 部署

1. 复制配置模板并填写账号密码：

   ```bash
   cp .env.example .env
   ```

2. 启动常驻服务：

   ```bash
   docker compose up -d --build
   ```

3. 查看运行日志：

   ```bash
   docker compose logs -f checkin
   ```

4. 停止服务：

   ```bash
   docker compose down
   ```

基础镜像同时支持常见的 AMD64 和 ARM64 Linux 服务器。Compose 设置了 `restart: unless-stopped`，服务器或 Docker 重启后会自动恢复。

## 手动执行一次

使用 Docker：

```bash
docker compose run --rm checkin once
```

或使用本机 Python 3.11 及以上版本：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python checkin.py once
```

`once` 会并行执行所有已启用站点。常驻模式为每个站点使用独立任务，某个站点的失败或重试不会延误另一个站点。临时网络故障时会依次等待 5、15、30 分钟重试；密码错误等不可重试问题会立即失败。

启用 yngal 后，程序会在同一次登录中先领取访问奖励，再执行一次寻宝。寻宝需要先在网页中设置守护灵出战位；如果返回“未设置守护灵出战位”，程序会记录提示，但不会把已经成功的登录奖励判定为失败。`YNGAL_HUNT_ENABLED=false` 可关闭寻宝请求。

## 配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `KF_USERNAME` | 无 | 绯月论坛账号，与 `KF_PASSWORD` 同时设置后启用 |
| `KF_PASSWORD` | 无 | 绯月论坛密码 |
| `YNGAL_EMAIL` | 无 | yngal 邮箱，与 `YNGAL_PASSWORD` 同时设置后启用 |
| `YNGAL_PASSWORD` | 无 | yngal 密码 |
| `TZ` | `Asia/Shanghai` | 调度时区 |
| `KF_CHECKIN_TIME` | `08:00` | 绯月每日执行时间，格式为 `HH:MM` |
| `YNGAL_CHECKIN_TIME` | `08:00` | yngal 每日执行时间，格式为 `HH:MM` |
| `YNGAL_HUNT_ENABLED` | `true` | 是否执行 yngal 每日寻宝；支持 `true/false`、`1/0`、`yes/no`、`on/off` |
| `REQUEST_TIMEOUT` | `20` | 单次 HTTP 请求超时秒数 |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `PUSH_LITE_URL` | 无 | Push Lite 完整 `/send` 地址；与 Token、UMO 同时设置后启用通知 |
| `PUSH_LITE_TOKEN` | 无 | Push Lite API 的 Bearer Token |
| `PUSH_LITE_UMO` | 无 | AstrBot 目标会话标识 |
| `PUSH_LITE_NOTIFY_MODE` | `failure` | `failure` 仅通知失败；`all` 通知成功、已领取和失败 |
| `PUSH_LITE_TIMEOUT` | `10` | 单次通知请求超时秒数 |

两个站点都是可选的，但至少需要完整配置其中一组账号密码。如果某组只设置了账号或只设置了密码，程序会拒绝启动并报告配置错误。

`.env` 已被 Git 和 Docker 构建上下文排除。不要把它上传到代码仓库，也不要在问题反馈中粘贴其内容。yngal 官网登录协议要求客户端对密码计算 MD5；程序只在内存中临时计算，不会记录原密码、MD5 或 token。寻宝请求使用登录响应中的 `X-Auth-Token`，仅访问 `https://www.yngal.com` 的 `/addJf` 和 `/hunt`。

## AstrBot Push Lite 通知

本项目兼容 [astrbot_plugin_push_lite](https://github.com/Raven95676/astrbot_plugin_push_lite) 的 `POST /send` 接口。先在 AstrBot 中安装并启用插件，通过 `/sid` 获取目标会话的 SID（即配置所需的 UMO），然后在 `.env` 中填写：

```dotenv
PUSH_LITE_URL=http://astrbot:9966/send
PUSH_LITE_TOKEN=replace_with_push_lite_token
PUSH_LITE_UMO=replace_with_sid
PUSH_LITE_NOTIFY_MODE=failure
PUSH_LITE_TIMEOUT=10
```

`PUSH_LITE_URL`、`PUSH_LITE_TOKEN` 和 `PUSH_LITE_UMO` 必须同时填写；全部留空则关闭通知。默认的 `failure` 模式只在某个站点完成全部签到重试后仍然失败时告警。改为 `all` 后，首次领取成功、今日已领取和最终失败都会逐站即时通知。服务启动时的立即检查也遵循相同规则。

当本程序运行在 Docker 中时，URL 里的 `127.0.0.1` 指向签到容器自身。若 AstrBot 与本程序位于同一个 Docker 网络，应使用 AstrBot 的服务名，例如 `http://astrbot:9966/send`；其他部署方式应填写签到容器实际能够访问的地址。

通知连接失败、超时、HTTP 429 或服务端错误会在 2 秒、5 秒后重试，最多请求 3 次。通知最终失败只写入日志，不会改变签到结果或 `once` 的退出码。Push Lite 返回 `queued` 只表示消息已经进入插件队列，不表示聊天平台已经最终投递。

## 安全与状态判断

- 绯月只允许访问 `https://bbs.kfpromax.com` 下的奖励链接。
- yngal 只允许访问 `https://www.yngal.com` 的同源登录、签到和寻宝接口。
- 页面状态不明确时不会猜测接口或点击链接。
- 每次执行都以站点返回状态为准，因此容器当天重复启动也能正确识别“已领取”和“已寻宝”。寻宝返回 `wrap=10` 时记录硬币 `+5`，其他非负奖励数值记录为积分增加。
- 日志不会输出密码、MD5、Push Lite Token、登录 token、会话 Cookie 或完整请求数据。
- Push Lite Token 的持有者可以让 AstrBot 发送消息，请勿提交、粘贴或公开该 Token。

## 测试

```bash
pip install -r requirements-dev.txt
pytest -q
```
