import asyncio
from uuid import UUID

import httpx2 as httpx
import pytest

from app.auth import CsrfTokens
from app.django_client import (
    DjangoCsrfFailed,
    DjangoNotFound,
    DjangoOutcomeUnknown,
    DjangoRefused,
    DjangoUnauthorized,
    DjangoUnavailable,
    make_django_client,
)
from tests.fakes import SESSION_U1, SESSION_U2, uuid_for

CSRF = CsrfTokens(cookie="c" * 32, header="h" * 64)


def client_returning(handler):
    """用真正的 make_django_client 造客户端，只把网络换成 handler。"""
    return make_django_client("http://django.test", timeout=5, transport=httpx.MockTransport(handler))


def test_does_not_remember_cookies_between_users():
    seen = []

    def handler(request):
        seen.append(request.headers.get("cookie"))
        # Django 有时会在响应里下发 cookie。普通的 httpx 客户端会把它存进自己的 cookie jar
        return httpx.Response(200, json={}, headers={"Set-Cookie": f"sessionid={SESSION_U1}; Path=/"})

    django = client_returning(handler)

    async def scenario():
        await django.me(SESSION_U1)
        await django.me(SESSION_U2)

    asyncio.run(scenario())
    # 每个请求只带自己的 sessionid
    assert seen == [f"sessionid={SESSION_U1}", f"sessionid={SESSION_U2}"]
    # 显式的 Cookie 头本来就优先于 cookie jar，所以光有上面那条断言，jar 存了小明的 cookie 也照样通过。
    # 真正要保证的是 jar 里什么都没存：以后哪个请求忘了带 Cookie 头，结果是「没登录」，而不是「变成小明」
    assert len(django._http.cookies) == 0


@pytest.mark.parametrize(
    ("status", "error"),
    [(403, DjangoUnauthorized), (401, DjangoUnauthorized), (404, DjangoNotFound), (500, DjangoUnavailable)],
)
def test_maps_status_codes_to_errors(status, error):
    django = client_returning(lambda request: httpx.Response(status, json={"detail": "x"}))
    with pytest.raises(error):
        asyncio.run(django.me(SESSION_U1))


@pytest.mark.parametrize("exc", [httpx.ConnectError, httpx.ReadTimeout])
def test_network_problems_become_unavailable(exc):
    def handler(request):
        raise exc("boom", request=request)

    with pytest.raises(DjangoUnavailable):
        asyncio.run(client_returning(handler).me(SESSION_U1))


def test_html_error_page_becomes_unavailable():
    django = client_returning(lambda request: httpx.Response(200, text="<html>502 Bad Gateway</html>"))
    with pytest.raises(DjangoUnavailable):
        asyncio.run(django.me(SESSION_U1))


def cancel(handler):
    return asyncio.run(client_returning(handler).cancel_order(SESSION_U1, CSRF, UUID(uuid_for(101))))


def test_cancel_sends_both_csrf_tokens():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"status": "CANCELED"})

    assert cancel(handler) == {"status": "CANCELED"}
    [request] = seen
    assert request.method == "POST" and request.url.path == f"/api/orders/{uuid_for(101)}/cancel/"
    assert request.headers["cookie"] == f"sessionid={SESSION_U1}; csrftoken={'c' * 32}"
    assert request.headers["x-csrftoken"] == "h" * 64


def test_cancel_refusal_keeps_shop_reason():
    body = {"code": "order_not_cancelable", "detail": "订单已经发货，不能在此取消。"}
    with pytest.raises(DjangoRefused) as e:
        cancel(lambda request: httpx.Response(409, json=body))
    assert e.value.code == "order_not_cancelable" and e.value.message == "订单已经发货，不能在此取消。"


@pytest.mark.parametrize(
    ("detail", "error"),
    [("CSRF Failed: CSRF token missing.", DjangoCsrfFailed), ("Authentication credentials were not provided.", DjangoUnauthorized)],
)
def test_cancel_tells_csrf_failure_from_logged_out(detail, error):
    with pytest.raises(error):
        cancel(lambda request: httpx.Response(403, json={"detail": detail}))


@pytest.mark.parametrize(
    ("exc", "error"),
    [(httpx.ConnectError, DjangoUnavailable), (httpx.ConnectTimeout, DjangoUnavailable), (httpx.ReadTimeout, DjangoOutcomeUnknown)],
)
def test_cancel_knows_whether_request_reached_shop(exc, error):
    # 连不上：请求肯定没到商城，订单没动。读超时：请求可能已经到了，不知道取消了没有
    def handler(request):
        raise exc("boom", request=request)

    with pytest.raises(error):
        cancel(handler)
