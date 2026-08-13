# 部署文档（Ubuntu + Nginx + systemd）

本文档记录迷你 Agent 骨架在 Ubuntu 云服务器上的部署与更新方式。默认假设：

- 操作系统：Ubuntu 22.04 / 24.04
- 反向代理：Nginx（如已有 Caddy，二选一，不要同时占用 80/443）
- 进程守护：systemd
- 运行用户：`minimalagent`（非 root）
- 项目目录：`/opt/minimal_agent`

## 端口约定

| 层 | 端口 | 说明 |
|---|---|---|
| 对外 | 443（HTTPS），80 自动跳转 443 | 用户和 OpenAI 兼容客户端访问 |
| 对内 | 8000，只绑定 `127.0.0.1` | Nginx 反向代理访问 |

防火墙只放行 22、80、443，**不要开放 8000**。

## 首次部署

### 1. 安装基础依赖

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git nginx
```

如果 `python3 -m venv` 报 `ensurepip is not available`，按提示补装对应版本：

```bash
sudo apt install -y python3.12-venv
```

### 2. 创建运行用户与目录

```bash
sudo useradd --system --shell /usr/sbin/nologin minimalagent
sudo mkdir -p /opt/minimal_agent
```

### 3. 拉取代码并安装依赖

先用普通有 sudo 权限的用户操作，装完再交还给运行用户：

```bash
sudo chown -R "$USER" /opt/minimal_agent
git clone <你的仓库地址> /opt/minimal_agent
cd /opt/minimal_agent

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

### 4. 配置生产 .env

```bash
cp .env.example .env
nano .env
```

至少配置：

- `DEEPSEEK_API_KEY`
- `SERVER_AUTH_TOKEN`
- `DASHBOARD_USERNAME`
- `DASHBOARD_PASSWORD_HASH`
- `DASHBOARD_AUTH_SECRET`
- `DASHBOARD_COOKIE_SECURE=true`

密码哈希在本地生成后粘贴：

```bash
python dashboard_auth.py hash-password '你的密码'
```

收权限：

```bash
sudo chown -R minimalagent:minimalagent /opt/minimal_agent
sudo chmod 600 /opt/minimal_agent/.env
```

### 5. systemd 服务

写入 `/etc/systemd/system/minimal-agent.service`：

```ini
[Unit]
Description=Minimal Agent
After=network.target

[Service]
Type=simple
User=minimalagent
Group=minimalagent
WorkingDirectory=/opt/minimal_agent
EnvironmentFile=/opt/minimal_agent/.env
ExecStart=/opt/minimal_agent/.venv/bin/python /opt/minimal_agent/server.py 127.0.0.1 8000
Restart=on-failure
RestartSec=5

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=/opt/minimal_agent

[Install]
WantedBy=multi-user.target
```

启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now minimal-agent
systemctl status minimal-agent
```

### 6. Nginx 反向代理

新建站点配置 `/etc/nginx/sites-available/minimal-agent`：

```nginx
server {
    listen 80;
    server_name your.domain.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE 流式响应不能缓冲
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }
}
```

启用并重载：

```bash
sudo ln -s /etc/nginx/sites-available/minimal-agent /etc/nginx/sites-enabled/minimal-agent
sudo nginx -t
sudo systemctl reload nginx
```

### 7. 申请 HTTPS 证书

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d your.domain.com
```

域名 DNS 必须先解析到服务器公网 IP，否则证书签发失败。

### 8. 防火墙

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

### 9. 验证

```bash
curl -f http://127.0.0.1:8000/health
journalctl -u minimal-agent -f
```

浏览器访问 `https://your.domain.com`，能登录和流式对话即完成。

## 日常更新

更新本质上是“拉代码 → 依赖有变化就重装 → 重启 Python 服务”。**不需要重启 Nginx**，除非改了 Nginx 配置或后端端口。

```bash
cd /opt/minimal_agent

# 1. 先看本地是否有未提交改动，避免 git pull 冲突
git status

# 2. 拉取最新代码（推荐 --ff-only，保持线性历史）
git pull --ff-only

# 3. 如果 requirements.txt 有变化，重装依赖
.venv/bin/pip install -r requirements.txt

# 4. 交还文件属主，并恢复 .env 权限
chown -R minimalagent:minimalagent /opt/minimal_agent
chmod 600 /opt/minimal_agent/.env

# 5. 重启 Python 服务
systemctl restart minimal-agent

# 6. 验证
curl -f http://127.0.0.1:8000/health
systemctl status minimal-agent
```

注意：

- `.env` 已 gitignore，`git pull` 不会覆盖它。
- 如果 `web/` 里的 `app.js`、`style.css` 更新了，浏览器可能用旧缓存；当前文件名不带 hash，发版后可让用户强制刷新，或反向代理层对静态资源设短缓存。
- 本项目目前没有数据库迁移框架；如果版本升级涉及 `sessions.db` 表结构变化，先备份数据库，再按版本说明处理。

### 回滚

确认工作区无未提交改动后，切回上一个可用提交：

```bash
cd /opt/minimal_agent
git log --oneline -10
git checkout <上一个稳定提交>
.venv/bin/pip install -r requirements.txt
chown -R minimalagent:minimalagent /opt/minimal_agent
systemctl restart minimal-agent
```

## 备份

需要备份的运行时数据：

- `sessions.db`
- `MEMORY.md`、`USER.md`
- `providers/`
- `logs/`

示例 cron，每天 03:00 打包到 `/var/backups`：

```cron
0 3 * * * tar -czf /var/backups/minimal_agent-$(date +\%F).tar.gz -C /opt minimal_agent
```

## 常见问题

- **`python3 -m venv` 失败**：缺少 `python3-venv` / `python3.12-venv`，见首次部署第 1 步。
- **修改 `.env` 后要不要重启 Nginx**：不用，重启 `minimal-agent` 即可。
- **Caddy 和 Nginx 冲突**：二选一，已经装了 Nginx 就不要装 Caddy。
- **SSE 卡住/一次性吐完**：确认 Nginx `proxy_buffering off` 和 `proxy_http_version 1.1`。
