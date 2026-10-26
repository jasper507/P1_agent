# P1 客服 agent 面试准备 Resources

## Knowledge

- [OWASP Top 10 for LLM Applications 2025: LLM06 Excessive Agency](https://genai.owasp.org/llmrisk/llm06/)
  LLM 应用「过度授权」风险和防护：以用户身份执行、最小权限、授权放在下游系统、高风险操作要人确认。Use for: 身份透传、工具权限设计，以及后面「取消订单要二次确认」的依据。
- [OWASP Insecure Direct Object Reference Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Insecure_Direct_Object_Reference_Prevention_Cheat_Sheet.html)
  越权访问对象（IDOR）的防护：每次访问都做权限检查，难猜的 ID 只是加一层防护。Use for: 「订单号是 UUID 是否就安全」这类追问。
- [RFC 6265 §8.5 Weak Confidentiality](https://www.rfc-editor.org/rfc/rfc6265#section-8.5)
  cookie 不按端口、不按协议隔离。Use for: 解释 8080 登录、8000 也能用；部署时的同域要求。
- [Django REST framework: Authentication](https://www.django-rest-framework.org/api-guide/authentication/#sessionauthentication)
  会话认证未登录返回 403、401 必须带 WWW-Authenticate 头、会话认证下的 CSRF 规则。Use for: 403 和 401 的处理；后面做写操作时的 CSRF。
- [Python 文档: http.cookiejar.DefaultCookiePolicy](https://docs.python.org/3/library/http.cookiejar.html#http.cookiejar.DefaultCookiePolicy)
  allowed_domains 白名单。Use for: 「共用客户端但不存 cookie」的实现依据。
- [HTTPX 文档: Clients](https://www.python-httpx.org/advanced/clients/)
  客户端复用 TCP 连接、在请求之间保存 cookie。项目用的 [httpx2](https://pypi.org/project/httpx2/) 是 Pydantic 接手维护的后继版本，接口相同。Use for: 连接池和串号风险。
- [FastAPI 文档: Lifespan Events](https://fastapi.tiangolo.com/advanced/events/)
  启动和关闭时执行的代码。Use for: 关闭连接池。
- [Redis 文档: SET](https://redis.io/docs/latest/commands/set/)
  `NX`、`EX` 的含义；成功的 SET 会丢掉原来的过期时间；「Patterns」一节的单实例锁（随机 token、脚本释放），并注明更推荐 Redlock。Use for: 对话锁、滑动过期。
- [Redis 文档: Distributed Locks with Redis](https://redis.io/docs/latest/develop/clients/patterns/distributed-locks/)
  为什么锁的值必须随机、锁的有效期、锁续期、fencing token 的建议，Redis 8.4 起可用 `DELEX key IFEQ token` 释放。Use for: 「一轮跑得比锁还久」「为什么不用 Redlock」这类追问。
- [Redis 文档: Transactions](https://redis.io/docs/latest/develop/using-commands/transactions/)
  WATCH 的乐观锁语义，被 Redis 过期删除也算修改；脚本通常比事务更简单更快。Use for: 释放锁为什么用 WATCH、和 Lua 的取舍。
- [Redis 文档: redis-py Pipelines and transactions](https://redis.io/docs/latest/develop/clients/redis-py/transpipe/)
  `pipe.watch()` 之后命令立即执行、`pipe.multi()` 开始事务、key 被改过时抛 `WatchError`。Use for: 释放锁的代码怎么写。
- [fakeredis](https://pypi.org/project/fakeredis/)
  纯 Python 实现的 Redis 协议，支持 asyncio，多个客户端共用一个 FakeServer；Lua 脚本要装额外的 lua 依赖。Use for: 测试为什么不用自己写的内存版。
- [Cormack 等: Reciprocal Rank Fusion outperforms Condorcet and individual Rank Learning Methods (SIGIR 2009)](https://cormack.uwaterloo.ca/cormacksigir09-rrf.pdf)
  RRF 的原始论文：公式 1/(k+r)，k=60 在试验中定下后没再改。Use for: 「两路结果怎么合并」「k 为什么是 60」「为什么合并反而丢分」（两路都出现就加分）。
- [BAAI bge-small-zh 模型卡](https://huggingface.co/BAAI/bge-small-zh)
  推荐用 v1.5；相似度分数集中在一个区间，要看相对顺序，阈值按自己数据的分布来选；small-zh 是 512 维。Use for: 拒答阈值怎么定、为什么选这个模型。
- [Qdrant 文档: FastEmbed](https://qdrant.tech/documentation/fastembed/)
  轻量的 embedding 库，用 ONNX Runtime 推理，依赖比 PyTorch 少。Use for: 为什么不装 PyTorch。
- [qdrant-client](https://pypi.org/project/qdrant-client/)
  本地模式 `QdrantClient(":memory:")`、`QdrantClient(path=...)`，不用起服务。Use for: 向量库选型、开发和测试怎么跑。
- [jieba](https://github.com/fxsjy/jieba)
  搜索引擎模式粒度更细、适合建倒排索引；`add_word`、`suggest_freq` 调整词典和词频。Use for: 中文 BM25 的分词问题。
- [OWASP LLM08:2025 Vector and Embedding Weaknesses](https://genai.owasp.org/llmrisk/llm082025-vector-and-embedding-weaknesses/)
  向量库要做细粒度权限控制、数据集严格分区。Use for: 「RAG 有什么安全问题」「为什么订单不放进向量库」。
- [onnxruntime 源码: onnxruntime_pybind_state.cc](https://github.com/microsoft/onnxruntime/blob/main/onnxruntime/python/onnxruntime_pybind_state.cc)
  `InferenceSession.run` 执行前释放 GIL，让多个线程能并行推理。Use for: 检索为什么放进 `asyncio.to_thread`。

## Wisdom (Communities)

- 还没选。用户没有表态要不要加入社区，先不主动推荐。

## Gaps

- Python 文档没写 `allowed_domains=[]`（空列表）的行为。项目里靠测试验证：两个用户请求过后 jar 仍然为空。
- DeepSeek 文档里没找到讲工具调用消息流程的专门页面（之前的链接打开是通用页面）。目前按 OpenAI 兼容的 Chat Completions 格式理解。
- 没找到 DeepSeek 或 OpenAI 文档里「每条工具结果前面必须有对应的工具调用」的原文（ADR 0006 和第 3 步 Q10 依赖这一条）。第 3 步联调时用真实接口实测：只发工具结果、不发调用，看是否报错。
- 第 4 步的真模型评测只能在用户电脑上跑：云端会话连不上 huggingface.co。用户电脑直连官方站下载模型约 5 秒，没用镜像。
