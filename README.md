# P1_agent

电商客服 / 导购 Agent（FastAPI + DeepSeek）。按步骤推进，每一步一个文件夹。

## 准备（Windows PowerShell，只做一次）

```powershell
cd C:\P1_agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

密钥放在 `C:\P1_agent\.env`，内容一行：`DEEPSEEK_API_KEY=sk-...`。这个文件已在 `.gitignore` 里，不会被提交。

## 第 0 步：最小 Agent

```powershell
pytest step0 -q          # 离线测试，不连网、不花钱
python step0\agent.py    # 真的调用 DeepSeek，跑 4 个问题
```
