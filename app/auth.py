import re

from fastapi import Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app.django_client import DjangoClient, DjangoError, DjangoUnauthorized, get_django

# Django 的 session key 是 32 位小写字母和数字。格式不对就不必去问 Django，
# 也挡住了带换行、分号的值被拼进我们发给 Django 的 Cookie 头
SESSION_ID_RE = re.compile(r"[a-z0-9]{1,64}")
# Django 的 CSRF token：cookie 里是 32 位，请求头里通常是加了掩码的 64 位，都只有字母和数字
CSRF_TOKEN_RE = re.compile(r"[A-Za-z0-9]{32,64}")


class CurrentUser(BaseModel):
    id: int
    username: str
    # repr=False：哪天有人 logger.info(user)，session id 也不会进日志
    session_id: str = Field(repr=False)


async def get_current_user(request: Request, django: DjangoClient = Depends(get_django)) -> CurrentUser:
    """和电商网站共用登录：浏览器带来的 sessionid，原样拿去问 Django「这是谁」。"""
    session_id = request.cookies.get("sessionid", "")
    if not SESSION_ID_RE.fullmatch(session_id):
        raise HTTPException(status_code=401, detail="未登录")
    try:
        me = await django.me(session_id)
    except DjangoUnauthorized as e:
        raise HTTPException(status_code=401, detail="未登录或登录已过期") from e
    except DjangoError as e:
        raise HTTPException(status_code=502, detail="电商服务暂时不可用，请稍后再试") from e
    return CurrentUser(id=me["id"], username=me["username"], session_id=session_id)


class CsrfTokens(BaseModel):
    """浏览器带来的两份 CSRF token，原样转给商城比对。"""

    cookie: str = Field(repr=False)
    header: str = Field(repr=False)


def get_csrf_tokens(request: Request, x_csrftoken: str | None = Header(default=None, alias="X-CSRFToken")) -> CsrfTokens:
    """确认取消要过商城的 CSRF 检查。agent 自己不判断 token 对不对，只检查格式，免得把怪字符拼进转给商城的请求头。

    前端从 csrftoken cookie 里读出 token，放进 X-CSRFToken 头，和商城自己的页面提交表单时一样。
    """
    cookie = request.cookies.get("csrftoken", "")
    header = x_csrftoken or ""
    if not (CSRF_TOKEN_RE.fullmatch(cookie) and CSRF_TOKEN_RE.fullmatch(header)):
        raise HTTPException(status_code=403, detail="缺少页面校验信息，请刷新页面后再试")
    return CsrfTokens(cookie=cookie, header=header)
