# 绯月论坛自动签到

通过 HTTP 登录 `bbs.kfpromax.com`，每天领取账户页面中的登录奖励。程序启动时会立即检查一次，之后默认在北京时间 08:00 执行。

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

`once` 和常驻模式都会在临时网络故障时依次等待 5、15、30 分钟重试。密码错误等不可重试问题会立即失败。

## 配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `KF_USERNAME` | 无 | 论坛账号，必填 |
| `KF_PASSWORD` | 无 | 论坛密码，必填 |
| `TZ` | `Asia/Shanghai` | 调度时区 |
| `CHECKIN_TIME` | `08:00` | 每日执行时间，格式为 `HH:MM` |
| `REQUEST_TIMEOUT` | `20` | 单次 HTTP 请求超时秒数 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

`.env` 已被 Git 和 Docker 构建上下文排除。不要把它上传到代码仓库，也不要在问题反馈中粘贴其内容。

## 安全与状态判断

- 只允许访问 `https://bbs.kfpromax.com` 下的奖励链接。
- 页面状态不明确时不会猜测接口或点击链接。
- 每次执行都会以论坛页面为准判断是否已经领取，因此容器当天重复启动也不会重复领取。
- 日志不会输出密码、会话 Cookie 或完整表单数据。

## 测试

```bash
pip install -r requirements-dev.txt
pytest -q
```
