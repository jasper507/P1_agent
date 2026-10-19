# P1_agent

电商客服 / 导购 Agent（FastAPI + DeepSeek）。

- `step0\`：最小的 agent 循环，一个脚本，用来理解原理
- `app\`：真正的服务，从第 1 步开始，后面每一步都在它上面改
- `tests\`：`app\` 的离线测试，用假的 LLM 和假的电商后端，不连网、不花钱

## 准备（Windows PowerShell，只做一次）

这台电脑上直接敲 `python` 用的是 MSYS2 的版本，装不了依赖。所以用 `py -3` 建虚拟环境，之后统一用 `.venv\Scripts\python`。

```powershell
cd C:\P1_agent
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

密钥放在 `C:\P1_agent\.env`，内容一行：`DEEPSEEK_API_KEY=sk-...`。这个文件已在 `.gitignore` 里，不会被提交。

## 跑测试

```powershell
.venv\Scripts\python -m pytest -q
```

## 第 0 步：最小 Agent

```powershell
.venv\Scripts\python step0\agent.py
```

## 第 2 步：和电商网站共用登录

agent 自己不管账号。它读浏览器带来的 `sessionid` cookie，拿去问电商项目的 Django「这是谁」，查订单也用这个身份，所以只能查到自己的订单。

1. 启动电商项目（在 `C:\project-one-ecommerce` 里执行，需要先打开 Docker Desktop）：
   ```powershell
   docker compose --env-file docker/deployment/.env -f compose.deployment-local.yaml -p project-one-deployment-local up -d --wait db
   docker compose --env-file docker/deployment/.env -f compose.deployment-local.yaml -p project-one-deployment-local up -d --wait --force-recreate web nginx
   ```
2. 浏览器打开 http://127.0.0.1:8080 登录电商网站（没有账号就先注册）
3. 回到 `C:\P1_agent` 启动 agent：
   ```powershell
   .venv\Scripts\python -m uvicorn app.main:app --reload
   ```
4. 用同一个浏览器打开 http://127.0.0.1:8000/docs ，直接调 `POST /api/chat`，请求体填 `{"message": "我有哪些订单？"}`

第 4 步不用再登录：浏览器的 cookie 只按主机名区分，不按端口区分，在 127.0.0.1:8080 拿到的 `sessionid` 也会带给 127.0.0.1:8000。所以两边要么都用 `127.0.0.1`，要么都用 `localhost`，混用就带不过去。

电商后端不在 8080 的话，在 `.env` 里加一行 `DJANGO_BASE_URL=http://...`。

停掉电商项目（数据会保留）：

```powershell
docker compose --env-file docker/deployment/.env -f compose.deployment-local.yaml -p project-one-deployment-local down
```

## 第 3 步：多轮对话

对话历史存在服务端的 Redis 里，前端只带对话编号。第一次提问不传 `conversation_id`，回复里会带一个；之后每次带上它，agent 就能接着上一轮聊。30 分钟没有新消息，对话就过期。

1. 在 `C:\P1_agent` 里起 Redis（需要 Docker Desktop）：
   ```powershell
   docker compose -p p1-agent up -d --wait redis
   ```
2. 按第 2 步启动电商项目、登录、启动 agent
3. 在 http://127.0.0.1:8000/docs 调 `POST /api/chat`：
   - 先发 `{"message": "我有哪些订单？"}`，记下回复里的 `conversation_id`
   - 再发 `{"message": "第一张是什么时候下的？", "conversation_id": "上一步的编号"}`

可能的错误：编号不存在、已过期或不是你的，返回 404；同一段对话上一条还没处理完就发下一条，返回 409；一轮处理超过 90 秒，返回 504；Redis 没起，返回 503。

Redis 不在本机 6379 的话，在 `.env` 里加一行 `REDIS_URL=redis://...`。

停掉 Redis：

```powershell
docker compose -p p1-agent down
```
