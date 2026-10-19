# 全进程共用一个 HTTP 客户端，但禁止它存 cookie

调用商城的 HTTP 客户端全进程只有一个，用来复用连接池。客户端默认会像浏览器一样保存响应里的 `Set-Cookie`，共用时只要某个请求忘了带 Cookie 头，就会自动带上上一个用户的会话，造成串号。我们决定：用户身份只靠每个请求显式带上的 Cookie 头，同时给客户端一个拒绝所有域名的 cookie jar（`DefaultCookiePolicy(allowed_domains=[])`），让它什么都不存。漏写 Cookie 头的结果是「按未登录处理」，而不是「用别人的身份」。

## Considered Options

- **每个请求新建一个客户端**：天然不会串号，但每次都要重新建立连接。
- **共用客户端，用默认的 cookie jar**：快，但有串号风险。
- **共用客户端，cookie jar 什么都不存**（选择这个）。

## Consequences

- 以后如果需要商城下发的 cookie（比如做写操作时的 CSRF token），要显式读响应头自己处理，不能指望客户端自动保存。
- 测试必须断言「请求过后 jar 仍然为空」。只检查发出去的 Cookie 头不够，因为显式的 Cookie 头本来就会覆盖 jar 里的值，删掉这项配置测试也不会变红。
