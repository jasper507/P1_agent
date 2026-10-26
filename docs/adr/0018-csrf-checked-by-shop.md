# CSRF 交给商城检查，agent 原样转发

取消订单是写操作，商城的 Django 要做 CSRF 检查：请求里要有 csrftoken cookie，还要有内容对得上的 X-CSRFToken 头。浏览器里，商城自己的页面就是从 cookie 里读出 token 放进请求头。

我们决定：确认接口要求浏览器带上 csrftoken cookie 和 X-CSRFToken 头，agent 只检查格式（只有字母和数字，32 到 64 位），然后原样转给商城的取消接口，对不对由商城判断。缺了任何一个，确认接口直接返回 403，订单不动，卡片还在，刷新页面再点就行。

这和 ADR 0001 是同一个思路：身份、权限、CSRF 都只在商城一处判断。

## Considered Options

- **agent 自己去商城拿一个 token**：带着用户的 sessionid 调 `/api/auth/csrf/` 就能拿到。但这样任何能让浏览器带着 cookie 访问 agent 的请求，都能借 agent 之手通过商城的检查，agent 就成了绕过 CSRF 的后门。
- **请商城给 agent 开一个免 CSRF 的接口**：要改电商项目，也削弱了商城自己的防护。

## Consequences

- agent 自己的接口还靠两件事挡跨站请求：请求体是 JSON，别的网站要发 JSON 得先过浏览器的跨域预检；商城的 cookie 是 SameSite=Lax（Django 的默认值），别的网站发起的 POST 不会带上它。
- 现在本地是 HTTP。换成 HTTPS 以后，Django 还会检查请求的 Origin 或 Referer 是不是自己的网站，而 agent 转发的请求两个都没有，会被拒。部署时（第 6 步）要在转发时带上商城的地址，或者把 agent 的地址加进商城的 `CSRF_TRUSTED_ORIGINS`。
- 在 `/docs` 里手动试的时候，要从浏览器开发者工具里复制 csrftoken cookie 的值，填进 X-CSRFToken。
