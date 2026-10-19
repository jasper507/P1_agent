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

## Wisdom (Communities)

- 还没选。用户没有表态要不要加入社区，先不主动推荐。

## Gaps

- Python 文档没写 `allowed_domains=[]`（空列表）的行为。项目里靠测试验证：两个用户请求过后 jar 仍然为空。
- DeepSeek 文档里没找到讲工具调用消息流程的专门页面（之前的链接打开是通用页面）。目前按 OpenAI 兼容的 Chat Completions 格式理解。
