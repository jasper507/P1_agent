# P1_agent

电商客服 / 导购 Agent（FastAPI + DeepSeek）。

- `step0\`：最小的 agent 循环，一个脚本，用来理解原理
- `app\`：真正的服务，从第 1 步开始，后面每一步都在它上面改
- `tests\`：`app\` 的离线测试，用假的 LLM，不连网、不花钱

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

## 第 1 步：启动服务

```powershell
.venv\Scripts\python -m uvicorn app.main:app --reload
```

浏览器打开 http://127.0.0.1:8000/docs ：

1. 先调 `POST /dev/login/{session_id}`，填 `demo-u1`（小明）或 `demo-u2`（小红），浏览器会记住登录 cookie
2. 再调 `POST /api/chat`，请求体填 `{"message": "我有哪些订单？"}`
