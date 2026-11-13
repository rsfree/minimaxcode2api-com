# 部署文档（服务器 / Docker）

面向「在一台 Linux 服务器上长期跑」的场景。生产形态是**单个容器 + 一个可写目录 `/data`**。

- 镜像：`ghcr.io/rsfree/minimaxcode2api-com:latest`
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

镜像发布在仓库右侧 **Packages** 页面，**公开可拉，不需要登录**：

```bash
docker pull ghcr.io/rsfree/minimaxcode2api-com:latest
```

> 若你把包改成了私有，才需要先登录：
> `echo "$GHCR_TOKEN" | docker login ghcr.io -u <你的GitHub用户名> --password-stdin`
> （`GHCR_TOKEN` 需要一个带 `read:packages` 的 PAT）

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
  ghcr.io/rsfree/minimaxcode2api-com:latest \
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

### 3.4 邮箱 + 密码批量开户（多账号池，一份额度不够时用这个）

一台机器挂多个账号，请求按轮询摊到各账号的额度上。**不需要浏览器、不需要验证码、
不需要进邮箱**——密码登录协议由 `app/account_api.py` 直接实现（RSA 加密密码 +
签名头），设备授权那一段也一并走完。

准备一个账号文件，一行一个，用 Tab 或空格分隔：

```text
# accounts.txt
aaa@mail.example.com   Pa55word1
bbb@mail.example.com   Pa55word1
```

逐条开户，token 落进 `/data/tokens/<label>.json`：

```bash
docker run --rm -v "$PWD/data:/data" \
  ghcr.io/rsfree/minimaxcode2api-com:latest \
  python -m app.provision --file /data/accounts.txt --out /data/tokens
```

```
aaa@mail.example.com  OK   expires_in=43200  groups=1
bbb@mail.example.com  OK   expires_in=43200  groups=1
2/2 provisioned
```

单条也可以：

```bash
python -m app.provision --email aaa@mail.example.com --password 'Pa55word1' --out /data/tokens
```

已有的 token 续期（refresh token 会轮换，就地覆盖原文件）：

```bash
python -m app.provision --refresh --out /data/tokens
```

然后把 `.env` 里的凭证来源换成池子：

```dotenv
TOKEN_POOL=/data/tokens
```

不带 `TOKEN_POOL` 时仍走 `TOKEN_STORE` 单 token，两者不冲突，池子优先。

#### 扩容：加号不用重启

网关每 10 秒扫一次池目录（`TOKEN_POOL_WATCH`，秒，`0` 关闭）。新账号文件落进去就自动进轮询，
删掉文件就自动退池，**不用 `docker compose restart`**，正在跑的请求也不受影响。

最省事的加号方式是控制台：`http://<服务器>:39600/ui` → **账号池 → ＋ 添加账号** → 粘贴
`邮箱 密码`（一行一个，空格 / Tab / 逗号都行）→ 开始导入。逐行回显「已加入 / 已更新 / 失败原因」，
写完当场进池。密码只在网关内存里过一遍换 token，不落盘。

命令行等价写法（没有浏览器时）：

```bash
# 在宿主机的 /opt/minimax-code2api 下：用一次性容器写同一个卷
docker compose run --rm --entrypoint python minimax-code2api \
  -m app.provision --email new@mail.example.com --password 'Pa55word1' --out /data/tokens

# 或者走和网页同一个接口（后台任务，可轮询进度）
curl -s -H "X-API-Key: $KEY" -H 'content-type: application/json' \
  -d '{"text":"a@mail.example.com pw1\nb@mail.example.com pw2"}' \
  http://127.0.0.1:39600/admin/accounts/import

# 等 10 秒，看池子变大（不要 restart）
curl -s -H "X-API-Key: $KEY" http://127.0.0.1:39600/healthz |
  python3 -c "import json,sys;d=json.load(sys.stdin);print(d['pool_size'],[a['label'] for a in d['accounts']])"

docker compose logs --tail 20 | grep pool
# minimax.auth pool: +1 account new@mail.example.com
# minimax.auth pool reloaded: 9 account(s) (+1/-0)
```

规则：

- **只增删、不覆盖** —— 内存里已轮换过的 token 不会被磁盘旧值顶掉；
- 一次最多贴 200 个号，默认 3 并发（`PROVISION_CONCURRENCY` 可调）；登录接口会限流，
  别把并发开大，也别在两个地方同时给同一个号开户；
- 单号下线 `rm data/tokens/<label>.json` 即可，别在网关运行时用别处副本覆盖同一个文件
  （refresh token 一经轮换，旧副本就是废的）。

> **一个账号没有某个模型的权限是正常的**（例如个别号没有 M3.1）。网关收到
> `400 unknown model` 会自动换下一个账号重试，不会把错误抛给客户端。

### 3.5 每日签到（免费回血）

签到是唯一不花钱的加额度方式：**每天 400 积分，第 4/7 天各 1000，7 天一轮共 4000，
领到手 30 天有效，仅限 MiniMax Code 使用**。

不用配置任何东西 —— 只要有 `TOKEN_POOL`，网关**每次启动都会替全池签到**，之后每 6 小时补一次：

```bash
docker compose logs -f | grep check-in
# daily check-in: 7/8 accounts, +2800 credits
```

手动补签或查看进度：

```bash
# 直接签
docker compose exec minimax-code2api python -m app.signin --dir /data/tokens

# 只看状态不领
docker compose exec minimax-code2api python -m app.signin --dir /data/tokens --status
```

```
bbb@mail.example.com            day 1 claimable cycle=4000 pts
ccc@mail.example.com            day 1 claimed   cycle=4000 pts

8 account(s), 2800 credits still claimable this cycle
```

`SIGNIN_TIMEZONE` 必须是 IANA 名字（默认 `Asia/Shanghai`），写 `480` 这种偏移会被服务端拒：
`{"status_code":1406010011,"status_msg":"invalid timezone_id"}`。

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
TOKEN_POOL=/data/tokens           # 多账号池目录，设了就优先轮询它
UPSTREAM_TRANSPORT=auto           # auto / httpx / curl，出口代理重置 Python TLS 时用 curl
SIGNIN_AUTO=1                     # 有池子时自动签到领积分
SIGNIN_TIMEZONE=Asia/Shanghai     # 必须是 IANA 名字，写 480 会被拒
TZ=Asia/Shanghai                  # 容器时区，只影响 docker compose logs（控制台按 SIGNIN_TIMEZONE）
METRICS_DB=                       # 请求历史落盘路径，空 = /data/metrics.db；off = 只在内存
METRICS_RETENTION_DAYS=3          # 请求历史保留天数，重启不清
PROBE_AUTO=1                      # 后台探针（控制台的余额/签到状态来源），不花积分
PROBE_INTERVAL=300                # 探针间隔秒数，最小 60；0 或 PROBE_AUTO=0 = 只手动刷新
DEFAULT_MAX_TOKENS=8192
RETRY_ATTEMPTS=3                  # 一次调用最多重试几次（0 = 不重试），面板可在线改
MEDIA_MIN_CREDITS=100             # 派生图/视频前要求的最低积分（一张图 × 张数），0 = 不预筛
GATEWAY_PORT=8080
```

### 哪些配置能在线改、哪些必须重建

面板「配置 → 运行配置」可以**不改 `.env`、不重建容器**直接改这 12 项：
`RETRY_ATTEMPTS`、`MEDIA_MIN_CREDITS`、`UPSTREAM_TIMEOUT`、`DEFAULT_MAX_TOKENS`、`UPSTREAM_TRANSPORT`、
`TOKEN_POOL_WATCH`、`PROBE_AUTO`/`PROBE_INTERVAL`、`SIGNIN_AUTO`/`SIGNIN_INTERVAL_HOURS`、
`METRICS_RETENTION_DAYS`、`EXPOSE_RETIRED_MODELS`。

改动写在 `/data/runtime.json`（容器内的卷，备份与迁移记得带上它），优先级高于 `.env`；
重建容器、换镜像都不丢。面板点「重置」= 删掉该条覆盖，回到 `.env` 的值。

剩下这些烘进了客户端或路由，改了必须重建：`MINIMAX_REGION`、`MINIMAX_BASE_URL`、
`UPSTREAM_PROXY`、`GATEWAY_API_KEY`、`GATEWAY_PORT`、`REQUEST_BODY_LIMIT`、`TOKEN_POOL`。

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
curl -s -H "X-API-Key: $KEY" $BASE/healthz
# 单 token：
# {"status":"ok","region":"en","source":"auth_file","cached":true,"expires_at_ms":...,"base_url":"...","proxy":true}
# 账号池：
# {"status":"ok","region":"en","pool_size":8,"accounts":[{"label":"aaa@mail.example.com","usable":true,...}]}
```

池子里 `usable:true` 的条数就是还能**对话**的账号数；`cooling_for` 是对话通道的剩余冷却秒数，
`media_cooling_for` / `usable_media` 是图片 / 视频通道的同一组数字（两条通道分开记，
「不能画图」不代表「不能聊天」）；`last_error` 是上次被摘下来的原因。

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

**⑧ 多模态能力清单与音色（不耗额度）**

```bash
curl -s $BASE/v1/media/models -H "Authorization: Bearer $KEY" | python3 -m json.tool
curl -s $BASE/v1/audio/voices -H "Authorization: Bearer $KEY" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('voices:', d['count'])"
```

期望：能力清单 4 项（image / video / speech / music）；音色数约为 **332**。

**⑨ 文生图（Ark / Seedream 协议，会扣额度，约 20 秒）**

```bash
curl -s $BASE/v1/images/generations -H "Authorization: Bearer $KEY" \
  -H 'content-type: application/json' \
  -d '{"model":"doubao-seedream-5-5-261028","prompt":"a red apple on a white table","size":"1K"}'
```

期望 `data[0].url` 是一个 `agent-cdn.minimax.io` 链接，能直接下载，旁边带 `size` / `output_format`
（网关读图片字节得出的真实值）。返回 `402` 说明账号额度用完了，不是部署问题 —— 看下面 `402` 那行。

**⑩ 文生视频（异步，最省的一档）**

```bash
curl -s $BASE/v1/videos/generations -H "Authorization: Bearer $KEY" \
  -H 'content-type: application/json' \
  -d '{"model":"MiniMax-Hailuo-2.3","prompt":"a cat walking on grass","duration":6,"resolution":"768P"}'
# 202 + task_id

curl -s "$BASE/v1/videos/generations/<task_id>?model=MiniMax-Hailuo-2.3" \
  -H "Authorization: Bearer $KEY"
```

期望轮询到 `status: succeeded` 并带 `video_url`。若看到
`503 + status: succeeded + retryable: true`，是上游交付环节没备好文件，继续轮询同一 `task_id`。

**⑪ 签到（有 `TOKEN_POOL` 才适用）**

```bash
docker compose exec minimax-code2api \
  python -m app.signin --dir /data/tokens --status
```

期望每个账号一行，`cycle=4000 pts`，说明 7 天一轮的奖励全都在账上。全部 `claimed`
也没问题——明天会重新变成 `claimable`。

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

`proxy_read_timeout` 别低于 **900s**：音乐生成上游超时是 600s，生图最长 300s，
生视频查询虽然快，但提交在余额不足时会等上游串行排队。

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
MINIMAX_IMAGE=ghcr.io/rsfree/minimaxcode2api-com:sha-1a2b3c4 docker compose up -d

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
| 生图 / TTS / 音乐返回 `402` | 账号 credits 耗尽（原文是 `plan balance has run out`，网关会换成中性文案） | 桌面端充值；该号只停图片通道 15 分钟，对话照常。全池都不够时才会把 402 返给你 |
| 对话 `503 no usable account: every account is out of quota until the daily reset at 00:10` | 全池都撞了 1008，免费额度按天重置 | 等次日重置（网关会自动签到并解除冷却），或加号 / 充值 |
| 号还有积分但生图一直 402 | 余额够聊天、不够一次生成：1 张 1K 图约 115 credits | 看面板积分列；低于 `MEDIA_MIN_CREDITS`（默认 100）的号会被自动跳过，调低门槛没用，得充值/签到 |
| 对话 `503 no usable account: every account is out of quota until the daily reset at 00:10` | 全池都撞了 1008，免费额度按天重置 | 等次日重置（网关会自动签到并解除冷却），或加号 / 充值 |
| 视频提交 `402` + `this task costs N credits` | 余额不够这一单（H3 4s = 320 credits） | 充值，或改用 `MiniMax-Hailuo-2.3` 6s |
| 视频提交 `503 temporarily unavailable` | **该账号没有视频权限**（按账号给，不是按 key） | 池里只要有一个号能提交就没事，网关自动换号；日志会打印 `cannot use this tool (503)` |
| 视频查询 `404 does not belong to the current caller` | 提交/查询不是同一个账号，或网关重启丢了 `task_id→账号` 映射 | 同一实例内轮询；重启后旧任务查不了 |
| 视频查询 `503` + `status: succeeded` | 上游渲染完成但未能交付文件 | 隔几分钟轮询同一 `task_id`，**别重新提交** |
| 多模态全部 `502`，日志里有 `307` | 出口不行（和聊天是同一个根因） | 配 `UPSTREAM_PROXY`，MCP 走同一个出口 |
| `reference_images` 没效果 | 传的是本地路径，上游只认公网 URL | 先上传图床/OSS 再传链接 |
| SDK 报 `404` + `{"detail":"Not Found"}`，curl 却正常 | 环境里有 `HTTP_PROXY`，httpx 对 localhost 也走代理，请求行变成绝对 URL，Starlette 把它当路径 | 已由内置中间件兼容；1.0.0 之前的版本请给客户端设 `NO_PROXY=127.0.0.1,localhost` |
| `pool_size` 有值但总是不用某个号 | 该号缺对应模型权限，`400 unknown model` 后被自动跳过 | 正常行为，看 `/healthz` 的 `last_error` |
| 签到报 `1406010011 invalid timezone_id` | `SIGNIN_TIMEZONE` 写成了偏移量 | 改成 IANA 名字，如 `Asia/Shanghai` |
| 日志 `daily check-in: nothing to claim` | 今天已经签过了（幂等，不是故障） | 正常；次日 0 点后自动再签 |
| 签到成功但仍 `402` | 签到积分 30 天有效、**仅限 MiniMax Code** 消耗，且一天才 400 | 多账号分摊，或充值；单号靠签到撑不住高并发 |
| 控制台日志时间比本地慢 8 小时 | 容器在 UTC，旧版本用 `time.localtime` 格式化 | 升级即可（按 `SIGNIN_TIMEZONE` 格式化，带日期）；`docker compose logs` 的时间由 `TZ` 控制 |
| 面板某页报 `HTTP 502`，**响应体为空** | 网关回的每个错误都带 JSON 体，空体不是它产的 | 点页面右上「诊断」：4 个接口若也全断，就是浏览器到服务器之间（代理 / VPN / 端口转发）；请求日志里没有 `面板` 行同样说明请求没到 |
| 请求日志里有 `面板` 行、状态 `0` | 连上了但网关没来得及回响应（重启中、客户端中断） | 看上表；`kind=console` 的行只在 4xx/5xx 或 >1.5s 时才写 |
| 想知道「最近到底哪句报错最多」 | 逐行读日志看不出形状 | 请求日志顶部「近 24h 失败聚合」，或 `GET /admin/errors?hours=24` |
| 要给上游 / 工单贴证据 | 容器日志要 ssh，页面只有状态码 | 请求日志点开那一行「复制这一行 JSON」；或页面「诊断 → 复制诊断报告」（含版本、号池、容器 WARNING、失败聚合） |
| 升级后日志还是空的 | 历史上一直存在内存里，第一个落盘版本之前的数据补不回来 | 这是预期：新版本从部署那一刻开始记，之后重启不清 |
| 控制台「积分余额」一直是 `—` / 探针数据不动 | `PROBE_AUTO=0`、`PROBE_INTERVAL=0`，或首轮还没跑完（启动后 8 秒开始，30 个号约 8 秒） | 点「刷新探针」，或 `curl -X POST -H "X-API-Key: $KEY" .../admin/probes/refresh`；**探针不花积分**，可以随便刷 |
| 某个号探针报 `401` 但业务请求正常 | 探针不主动续期 token；业务请求触发续期后即恢复 | 不用处理，下一轮自动好；持续 401 说明 refresh token 也失效了 |
| 日志 `ConnectError [WinError 10054]` / `SSL: UNEXPECTED_EOF` | 出口代理只放行 Windows schannel 指纹，拒 Python/OpenSSL 握手 | 留着 `UPSTREAM_TRANSPORT=auto` 让它自动退回 curl（镜像已装 curl）；退回只维持 5 分钟，之后自动再试 httpx |
| 只有**长对话**报 500，错误是 `OSError: [Errno 7] Argument list too long: 'curl'` | 旧版本把请求体当命令行参数交给 curl，Linux 单个参数上限 128 KiB（`MAX_ARG_STRLEN`），超了就 exec 失败 | 升级到 2026-11-13 之后的镜像（请求体改走 stdin，无上限）；临时可 `UPSTREAM_TRANSPORT=httpx` 绕过 |

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
- [ ] `/ui`、`/healthz` 都要 key：`/ui` 走登录页种 cookie，`/healthz` 只有容器本机 127.0.0.1 免鉴权
      （外部探活记得带 `-H "X-API-Key: $KEY"`，否则拿到 401）
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
