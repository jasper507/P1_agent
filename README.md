# P1_agent

电商客服 / 导购 Agent（FastAPI + DeepSeek）。

- `step0\`：最小的 agent 循环，一个脚本，用来理解原理
- `app\`：真正的服务，从第 1 步开始，后面每一步都在它上面改
- `tests\`：`app\` 的离线测试，用假的 LLM、假的电商后端和假的向量模型，不连网、不花钱
- `policies\`：售后政策（示例商城的虚构政策），第 4 步用
- `evals\`：评测。检索评测第 4 步用，端到端评测第 7 步用

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

## 第 4 步：售后政策问答

售后政策放在 `policies\`，是一份示例商城的虚构政策。服务启动时把政策按条款切段，用本机的向量模型 bge-small-zh-v1.5 算向量、放进内存里的 Qdrant。模型遇到售后问题会调用 `search_policy`，交给模型最相关的 4 条；最高相似度低于阈值（0.55），就告诉模型政策里没有相关规定。

关键词检索（jieba 分词加 BM25）和按名次合并（RRF）也写好了，但评测里合并反而丢分，所以默认关着。想打开就在 `.env` 里加一行 `POLICY_HYBRID=true`。

第一次启动会下载向量模型（约 90 MB）到 `.cache\fastembed\`，之后不再下载。

试一下：按第 3 步启动 Redis、电商项目和 agent，在 http://127.0.0.1:8000/docs 调 `POST /api/chat`：

- `{"message": "耳机坏了能保修吗？"}`：回答里带出处；响应里的 `sources` 是这一轮真正查到的条款
- `{"message": "可以分期付款吗？"}`：政策里没有，客服会建议联系人工客服

检索评测，只跑检索、不调大模型、不花钱，说明和最近一次的结果见 `evals\README.md`：

```powershell
.venv\Scripts\python -m evals.eval_retrieval
```

## 第 5 步：取消订单（要用户确认）

用户说「帮我把那张没付款的订单取消了」，agent 不会马上取消：它先查订单，返回一张确认卡片（`pending_action`），用户点了「确认取消」才去商城取消。agent 的循环从这一步起用 LangGraph 写成一张图，停下来等确认时的状态存在 `.data\checkpoints.sqlite`，服务重启了也接得上。

试一下：按第 3 步启动 Redis、电商项目和 agent，先在电商网站上下一单但不付款。然后在 http://127.0.0.1:8000/docs 里：

1. 调 `POST /api/chat`：`{"message": "帮我把那张没付款的订单取消了"}`。回复里的 `pending_action` 就是确认卡片，记下 `conversation_id` 和 `pending_action.action_id`
2. 在浏览器开发者工具里找到 `127.0.0.1` 下名为 `csrftoken` 的 cookie，复制它的值。没有的话，先打开一次 http://127.0.0.1:8080/api/auth/csrf/
3. 调 `POST /api/chat/confirm`：请求头 `X-CSRFToken` 填刚才复制的值，请求体填 `{"conversation_id": "...", "action_id": "...", "decision": "confirm"}`；`decision` 填 `reject` 就是不取消

可能的错误：卡片没处理完就发新消息，返回 409；操作编号对不上或已经处理过，返回 404；缺 `X-CSRFToken`，返回 403。每次取消在服务的输出里各有一行 `cancel_proposed` 和 `cancel_decided` 的审计日志。

## 第 7 步：端到端评测（真模型）

第 6 步（演示模式、Docker、MCP）跳过了，直接做精简版的第 7 步：用真的 DeepSeek 跑 26 段固定的对话（查订单、查政策、取消、防注入），每一轮用规则判对错，并记下耗时、token 和花费。商城和 Redis 用假的，不用起 Docker。

```powershell
.venv\Scripts\python -m evals.eval_agent
```

最近一次 26 道各跑 3 遍，75/78 通过，没点确认就取消、泄露别人订单都是 0 次。评测还找出一个问题：用户冒充「系统消息」说订单已取消，模型会照着说。系统提示加了一条规则后，又跑的 8 遍里没再出现。说明和完整结果见 `evals\README.md`。

## MCP 服务：让别的 AI 应用也能用这些工具

把查订单、查订单详情、查售后政策三个工具做成 MCP 服务，售后政策原文做成资源（`policy://returns` 这样的地址）。工具实现和 agent 共用 `app/tools.py`，所以通过 MCP 也只能查到自己的订单。取消订单不对外，因为它必须在 agent 的页面上点确认，见 ADR 0020。

服务由 MCP 客户端拉起（stdio）。需要两个环境变量：`P1_SHOP_SESSIONID` 填浏览器里商城 `127.0.0.1` 下名为 `sessionid` 的 cookie 值（先在商城登录）；`PYTHONPATH` 填 `C:\P1_agent`，这样从哪个目录拉起都找得到 `app`。商城不在 8080 的话再加 `DJANGO_BASE_URL`。

接到 Claude Code（在 `C:\P1_agent` 里执行一次，`<sessionid>` 换成真的值）：

```powershell
claude mcp add p1-shop --env P1_SHOP_SESSIONID=<sessionid> --env PYTHONPATH=C:\P1_agent -- C:\P1_agent\.venv\Scripts\python.exe -m app.mcp_server
```

接到 Claude Desktop：在它的配置文件 `claude_desktop_config.json` 里加上（JSON 里的反斜杠要写两个）：

```json
{
  "mcpServers": {
    "p1-shop": {
      "command": "C:\\P1_agent\\.venv\\Scripts\\python.exe",
      "args": ["-m", "app.mcp_server"],
      "env": {"P1_SHOP_SESSIONID": "<sessionid>", "PYTHONPATH": "C:\\P1_agent"}
    }
  }
}
```

离线测试 `tests\test_mcp_server.py` 用 MCP 官方客户端在进程内连上服务，走一遍列工具、调工具、读资源。
