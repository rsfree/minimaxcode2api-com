# 部署文档（服务器 / Docker）

面向「在一台 Linux 服务器上长期跑」的场景。生产形态是**单个容器 + 一个可写目录 `/data`**。

- 镜像：`ghcr.io/jushenzhidao/minimax-code2api:latest`
- 端口：容器内 `8080`
- 持久化：`/data`（存 `token.json`，会自动刷新）

---

## 0. 先读这一节：出口网络决定能不能用

这是**唯一一个必然踩到的坑**，先确认再往下做。

```
POST https://agent.minimax.io/mavis/api/v1/llm/v1/messages
→ HTTP/1.1 307 Temporary Redirect
   Location: https://agent.minimaxi.com/mavis/api/v1/llm/v1/messages
```

中国大陆 IP 访问 `agent.minimax.io` 会被边缘节点（Akamai，响应头 `X-From: ak`）**地理重定向**到旧的
`agent.minimaxi.com`。而 HTTP 规范要求**跨域重定向丢弃 `Authorization` 头**，于是旧站收到一个没有
身份信息的请求，返回：

```json
{"error":"invalid access token"}
```

**这跟 token 是否有效无关**：同一个 token 换个出口就是 200。

所以先测一下你的服务器属于哪种：

```bash
# 返回 200/401 之外的东西（尤其是 307）就说明出口不行
curl -s -o /dev/null -D - -X POST \
  https://agent.minimax.io/mavis/api/v1/llm/v1/messages \
  -H 'content-type: application/json' -d '{}' | head -5
```

| 结果 | 含义 | 动作 |
|---|---|---|
| `HTTP/1.1 307` + `Location: ...agent.minimaxi.com` | 出口在国内，会被重定向 | 必须配 `UPSTREAM_PROXY` |
| `HTTP/2 401` / `400` | 出口直连正常 | 跳过，继续第 3 节 |
| 连不上 / 超时 | 出口不通 | 配 `UPSTREAM_PROXY` 或换机器 |

`UPSTREAM_PROXY` 填一个能直达 `agent.minimax.io` 的 HTTP/SOCKS5 代理，例如：

```dotenv
UPSTREAM_PROXY=http://10.0.0.5:7890
# 或
UPSTREAM_PROXY=socks5://10.0.0.5:1080
```

> 这台机器上的实测证据：走本地 `57890` 出口时返回 `200 OK` 并带真实补全；走另一个国内出口时稳定
> `307 → 401`。同一个 token、同一个请求体，差别只在出口。

---

## 1. 环境要求

| 项 | 要求 |
|---|---|
| Docker | 24+（compose v2） |
| CPU / 内存 | 1 核 / 256 MB 足够（只做转发，不做推理） |
| 磁盘 | 100 MB |
| 出网 | 能访问 `agent.minimax.io` 和 `account.minimax.io`（或经 `UPSTREAM_PROXY`） |
| 账号 | 一个登录过 MiniMax Code 的账号（Coding Plan / Credits） |

---

## 2. 获取镜像

**方式 A：直接拉 GHCR（推荐）**

镜像发布在仓库右侧 **Packages** 页面。

```bash
echo "$GHCR_TOKEN" | docker login ghcr.io -u <你的GitHub用户名> --password-stdin
docker pull ghcr.io/jushenzhidao/minimax-code2api:latest
```

> 包是私有的，`GHCR_TOKEN` 需要一个带 `read:packages` 的 PAT。

**方式 B：离线包**（CI 每次构建都会产出，保留 7 天）

```bash
# 从 Actions 页面下载 minimax-code2api-<version>.tar.gz
gunzip -c minimax-code2api-*.tar.gz | docker load
```

**方式 C：自己构建**

```bash
git clone <this-repo> && cd minimax-code2api
docker build -t minimax-code2api:local .
```

---

## 3. 拿到凭证

网关需要一个 MiniMax Code 的 OAuth token。三条路，选一条。

### 3.1 设备码登录（服务器首选，无需桌面端）

```bash
docker run --rm -it -v "$PWD/data:/data" \
  ghcr.io/jushenzhidao/minimax-code2api:latest \
  python -m app.login --out /data/token.json
```

终端会打印一个链接和验证码：

```
  1. open this page : https://account.minimax.io/oauth-authorize
  2. enter the code : Z4B3-X2U7
     or open directly: https://account.minimax.io/oauth-authorize?user_code=Z4B3-X2U7
     (code expires in 5 min)

waiting for authorisation...
OK  access token : mmoat_xxxxxxx...
    refresh token: yes
    written to   : /data/token.json
```

浏览器里确认后，`/data/token.json` 就写好了。**之后网关会自己用 refresh token 续期并就地更新这个文件**，
不用再管。

> 设备码用的是 S256 PKCE（`code_challenge` + `code_verifier`），不是裸 `client_id`，
> 少了会被账号服务直接拒：`{"error":"invalid_request","error_description":"valid S256 PKCE is required"}`。
>
> 若登录页打不开，加 `--proxy`：`python -m app.login --proxy http://10.0.0.5:7890`。

### 3.2 从已登录的桌面端复制

```bash
# 桌面端所在机器
cat ~/.minimax/auth/prod/en/mcode-public/auth.json
```

把里面的 `accessToken`（`mmoat_` 开头）填到 `.env` 的 `MINIMAX_ACCESS_TOKEN`。
**缺点**：12 小时后失效，需要重新复制（因为没有 refresh 能力时网关无法续期）。同时填
`MINIMAX_REFRESH_TOKEN` 可自动续期。

### 3.3 挂载桌面端的 auth.json

仅适合网关和桌面端在同一台机器：

```dotenv
MINIMAX_AUTH_FILE=/host-minimax/auth.json
```

`docker-compose.yml` 里加一条只读挂载：

```yaml
    volumes:
      - ./data:/data
      - /home/user/.minimax/auth/prod/en/mcode-public/auth.json:/host-minimax/auth.json:ro
```

网关每次请求都会重读该文件，桌面端刷新 token 后自动生效。

---

## 4. 配置 `.env`

```bash
cp .env.example .env
```

必须改的只有两项：

```dotenv
GATEWAY_API_KEY=换成你自己的随机串        # openssl rand -hex 24
UPSTREAM_PROXY=http://10.0.0.5:7890      # 第 0 节判定需要时才填
```

其余按需：

```dotenv
MINIMAX_REGION=en                 # 账号注册在国际站就填 en
TOKEN_STORE=/data/token.json
DEFAULT_MAX_TOKENS=8192
GATEWAY_PORT=8080
```

`.env` 权限收紧，且**永远不要提交进 git**：

```bash
chmod 600 .env
```

---

## 5. 启动

```bash
mkdir -p data && chmod 700 data
docker compose up -d
docker compose logs -f --tail=50
```

正常日志：

```
upstream ready base_url=https://agent.minimax.io/mavis/api/v1/llm/v1 proxy=http://10.0.0.5:7890 region=en
INFO:     Uvicorn running on http://0.0.0.0:8080
```

> 容器以 uid `10001` 运行，`./data` 需要可写。宿主机目录属主不对时，在 compose 里加
> `user: "<PUID>:<PGID>"` 覆盖，**不要改镜像**。

---

## 6. 验证清单

逐条跑，全绿才算部署成功。

```bash
BASE=http://127.0.0.1:8080
KEY=你的GATEWAY_API_KEY
```

**① 健康检查：有凭证**

```bash
curl -s $BASE/healthz
# {"status":"ok","region":"en","source":"auth_file","cached":true,"expires_at_ms":...,"base_url":"...","proxy":true}
```

**② 鉴权生效：无 key 必须 401**

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST $BASE/v1/chat/completions \
  -H 'content-type: application/json' -d '{"messages":[{"role":"user","content":"hi"}]}'
# 401
```

**③ 模型列表：应看到 6 个**

```bash
curl -s $BASE/v1/models -H "Authorization: Bearer $KEY" | python3 -m json.tool
```

**④ 真实补全（非流式）**

```bash
curl -s $BASE/v1/chat/completions -H "Authorization: Bearer $KEY" \
  -H 'content-type: application/json' \
  -d '{"model":"MiniMax-M3.1-Flash","messages":[{"role":"user","content":"答 OK 两个字"}]}'
```

期望 `choices[0].message.content` 有内容、`usage.prompt_tokens > 0`。

**⑤ 流式**

```bash
curl -N $BASE/v1/chat/completions -H "Authorization: Bearer $KEY" \
  -H 'content-type: application/json' \
  -d '{"model":"MiniMax-M3.1-Flash","stream":true,"messages":[{"role":"user","content":"数到3"}]}' | head -5
```

期望连续 `data: {...chat.completion.chunk...}`，最后 `data: [DONE]`。

**⑥ 工具调用**

```bash
curl -s $BASE/v1/chat/completions -H "Authorization: Bearer $KEY" \
  -H 'content-type: application/json' \
  -d '{"model":"MiniMax-M3","messages":[{"role":"user","content":"深圳天气"}],
       "tools":[{"type":"function","function":{"name":"get_weather","parameters":{"type":"object","properties":{"city":{"type":"string"}}}}}]}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['choices'][0]['message'].get('tool_calls'))"
```

期望非 `None`，`finish_reason` 为 `tool_calls`。

**⑦ Anthropic 协议**

```bash
curl -s $BASE/v1/messages -H "Authorization: Bearer $KEY" \
  -H 'content-type: application/json' \
  -d '{"model":"MiniMax-M2.7","max_tokens":32,"messages":[{"role":"user","content":"答 OK"}]}'
```

期望 `{"type":"message",...,"stop_reason":"end_turn"}`。

### 关于 `/healthz` 返回 503

含义是「服务活着，但没有可用凭证」。容器不会退出（便于你修凭证而不是陷入重启循环），
但 `docker ps` 会显示 `unhealthy`。修 `token.json` 或环境变量后自动恢复。

---

## 7. 反向代理

### Nginx

流式必须关缓冲，否则客户端会等到最后一次性吐出：

```nginx
server {
    listen 443 ssl http2;
    server_name ai.example.com;

    ssl_certificate     /etc/letsencrypt/live/ai.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/ai.example.com/privkey.pem;

    client_max_body_size 64m;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection        "";

        # SSE 关键三项
        proxy_buffering     off;
        proxy_cache         off;
        proxy_read_timeout  900s;
        chunked_transfer_encoding on;
    }
}
```

### Caddy

```caddyfile
ai.example.com {
    encode gzip
    reverse_proxy 127.0.0.1:8080 {
        flush_interval -1
        transport http {
            read_timeout 15m
        }
    }
}
```

> 不要在反代上加统一的 `Authorization` 校验后透传——网关自己做鉴权，重复校验容易把
> `x-api-key` 和 `Authorization` 弄混。

---

## 8. 升级与回滚

```bash
# 升级
docker compose pull && docker compose up -d

# 回滚到指定版本（每次构建都有 sha-<7位> 和 <分支名> 标签）
MINIMAX_IMAGE=ghcr.io/jushenzhidao/minimax-code2api:sha-1a2b3c4 docker compose up -d

# 看当前版本
docker inspect --format '{{ index .Config.Labels "org.opencontainers.image.version" }}' \
  $(docker compose ps -q)
```

`/data/token.json` 在宿主机上，升级回滚都不会丢凭证。

---

## 9. 排障表

| 现象 | 根因 | 处理 |
|---|---|---|
| `307` → `401 invalid access token` | 出口被地理重定向，`Authorization` 被丢弃 | 配 `UPSTREAM_PROXY`（第 0 节） |
| `401` + `{"code":401,"message":"token is required"}` | 请求缺 `x-api-key: sk-xxx` | 用本网关，不要手搓上游请求 |
| `401 invalid gateway api key` | 调用方没带 / 带错 `GATEWAY_API_KEY` | 检查客户端 `api_key` |
| `/healthz` 503，`error: no credential available` | 三条凭证路径都没配 | 跑 `python -m app.login` |
| `/healthz` 503，`refresh failed: HTTP 400 invalid_grant` | refresh token 已被别处用掉（轮换） | 重新登录一次，生成新 token.json |
| `404 ... no longer available (2069)` | 调了已下线的 M2 / M2.1 | 换 `MiniMax-M2.5` 及以上 |
| 流式卡住 / 最后一次性返回 | 反代开了缓冲 | 见第 7 节，`proxy_buffering off` |
| 首字很慢（10s+） | M3.1-Flash 强制思考，先出 `thinking` | 正常；或换 `MiniMax-M2.5-highspeed` |
| `429` / 限流 | 账号额度或并发限制 | 降并发、错峰，或换账号 |
| `502 upstream error` + 具体 message | 上游返回的业务错误，原文透传 | 按 message 处理 |

看实时日志定位：

```bash
docker compose logs -f --tail=100 | grep -E 'kind=|WARNING|ERROR'
# ok kind=chat model=MiniMax-M3.1-Flash in=212 out=26 3.41s
```

---

## 10. 安全清单

- [ ] `GATEWAY_API_KEY` 已设，且不是示例值（`openssl rand -hex 24`）
- [ ] `.env` 权限 `600`，未提交进 git
- [ ] 容器不以 root 运行（镜像默认 uid `10001`）
- [ ] 只监听内网或已上反代 + TLS，不要裸 `0.0.0.0:8080` 暴露公网
- [ ] `data/token.json` 权限 `600`，不进备份的公开位置
- [ ] 出问题先想「谁在用这个 token」——它等价于账号登录态
- [ ] 定期看 `/healthz` 的 `expires_at_ms`，确认续期正常

## 附：不用 Docker 直接跑

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
set -a && . ./.env && set +a
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080 --proxy-headers
```

systemd 单元：

```ini
[Unit]
Description=minimax-code2api
After=network-online.target

[Service]
WorkingDirectory=/opt/minimax-code2api
EnvironmentFile=/opt/minimax-code2api/.env
ExecStart=/opt/minimax-code2api/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8080 --proxy-headers
Restart=always
RestartSec=3
User=minimax
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```
