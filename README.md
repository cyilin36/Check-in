# 多站点自动签到

通过 HTTP 自动领取绯月论坛和 yngal 的每日登录奖励。两个站点可分别启用和设置执行时间；程序启动时会立即检查所有已启用站点，之后默认在北京时间 08:00 执行。

- 绯月：登录 `bbs.kfpromax.com` 并领取账户页面中的奖励。
- yngal：登录 `www.yngal.com` 并调用当天首次访问奖励接口领取硬币。

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
   docker compose logs -f kf-checkin
   ```

4. 停止服务：

   ```bash
   docker compose down
   ```

基础镜像同时支持常见的 AMD64 和 ARM64 Linux 服务器。Compose 设置了 `restart: unless-stopped`，服务器或 Docker 重启后会自动恢复。

## 手动执行一次

使用 Docker：

```bash
docker compose run --rm kf-checkin once
```

或使用本机 Python 3.11 及以上版本：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python checkin.py once
```

`once` 会并行执行所有已启用站点。常驻模式为每个站点使用独立任务，某个站点的失败或重试不会延误另一个站点。临时网络故障时会依次等待 5、15、30 分钟重试；密码错误等不可重试问题会立即失败。

## 配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `KF_USERNAME` | 无 | 绯月论坛账号，与 `KF_PASSWORD` 同时设置后启用 |
| `KF_PASSWORD` | 无 | 绯月论坛密码 |
| `YNGAL_EMAIL` | 无 | yngal 邮箱，与 `YNGAL_PASSWORD` 同时设置后启用 |
| `YNGAL_PASSWORD` | 无 | yngal 密码 |
| `TZ` | `Asia/Shanghai` | 调度时区 |
| `CHECKIN_TIME` | `08:00` | 绯月每日执行时间，格式为 `HH:MM` |
| `YNGAL_CHECKIN_TIME` | `08:00` | yngal 每日执行时间，格式为 `HH:MM` |
| `REQUEST_TIMEOUT` | `20` | 单次 HTTP 请求超时秒数 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

两个站点都是可选的，但至少需要完整配置其中一组账号密码。如果某组只设置了账号或只设置了密码，程序会拒绝启动并报告配置错误。

`.env` 已被 Git 和 Docker 构建上下文排除。不要把它上传到代码仓库，也不要在问题反馈中粘贴其内容。yngal 官网登录协议要求客户端对密码计算 MD5；程序只在内存中临时计算，不会记录原密码、MD5 或 token。

## 安全与状态判断

- 绯月只允许访问 `https://bbs.kfpromax.com` 下的奖励链接。
- yngal 只允许访问 `https://www.yngal.com` 的同源登录和签到接口。
- 页面状态不明确时不会猜测接口或点击链接。
- 每次执行都以站点返回状态为准，因此容器当天重复启动也能正确识别“已领取”。
- 日志不会输出密码、MD5、token、会话 Cookie 或完整表单数据。

## 测试

```bash
pip install -r requirements-dev.txt
pytest -q
```
