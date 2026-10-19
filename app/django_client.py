from functools import lru_cache
from http.cookiejar import CookieJar, DefaultCookiePolicy
from uuid import UUID

import httpx2 as httpx

from app.config import get_settings


class DjangoError(Exception):
    """调用电商后端失败。message 是可以直接交给模型或用户看的中文说明。"""

    message = "电商服务出错了"


class DjangoUnauthorized(DjangoError):
    message = "登录已过期，请重新登录"


class DjangoNotFound(DjangoError):
    message = "找不到这张订单，或者它不属于当前用户"


class DjangoUnavailable(DjangoError):
    message = "电商服务暂时不可用"


class DjangoClient:
    """以「当前用户」的身份调用电商后端的只读接口。

    每个方法都要传 session_id：用户是谁由 Django 根据这个 cookie 判断，
    agent 自己不保存、也不猜任何用户身份。
    """

    def __init__(self, http: httpx.AsyncClient):
        self._http = http

    async def me(self, session_id: str) -> dict:
        return await self._get("/api/auth/me/", session_id)

    async def list_orders(self, session_id: str, page: int = 1) -> dict:
        # Django 返回 {count, next, previous, results}，每页 20 条，最新的在前
        return await self._get("/api/orders/", session_id, params={"page": page})

    async def get_order(self, session_id: str, public_id: UUID) -> dict:
        # public_id 的类型是 UUID，不是 str：拼进 URL 的东西必须先验证过
        return await self._get(f"/api/orders/{public_id}/", session_id)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, session_id: str, params: dict | None = None) -> dict:
        try:
            r = await self._http.get(path, params=params, headers={"Cookie": f"sessionid={session_id}"})
        except httpx.HTTPError as e:  # 连不上、超时
            raise DjangoUnavailable() from e
        # 这几个接口都是 IsAuthenticated + SessionAuthentication：没登录时 Django 回的是 403 而不是 401
        if r.status_code in (401, 403):
            raise DjangoUnauthorized()
        if r.status_code == 404:
            raise DjangoNotFound()
        if r.status_code != 200:
            raise DjangoUnavailable()
        try:
            return r.json()
        except ValueError as e:  # 比如 nginx 返回了一个 HTML 错误页
            raise DjangoUnavailable() from e


def make_django_client(base_url: str, timeout: float, transport: httpx.AsyncBaseTransport | None = None) -> DjangoClient:
    """共用客户端最怕「串号」。身份只靠每次请求显式带上的 Cookie 头；
    此外再给它一个什么都不存的 cookie jar。否则它会像浏览器一样把响应里的 Set-Cookie 存起来，
    以后哪个请求忘了带 Cookie 头，就会自动带上上一个用户的 sessionid。

    transport 只给测试用：换成 MockTransport，就能在不连网的情况下假装自己是 Django。
    """
    return DjangoClient(
        httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            headers={"Accept": "application/json"},
            cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=[])),
            transport=transport,
        )
    )


@lru_cache
def get_django() -> DjangoClient:
    """整个进程共用一个连接池，和 get_llm 一样。"""
    settings = get_settings()
    return make_django_client(settings.django_base_url, settings.django_timeout_seconds)
