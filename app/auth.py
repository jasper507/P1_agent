import re

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.django_client import DjangoClient, DjangoError, DjangoUnauthorized, get_django

# Django 的 session key 是 32 位小写字母和数字。格式不对就不必去问 Django，
# 也挡住了带换行、分号的值被拼进我们发给 Django 的 Cookie 头
SESSION_ID_RE = re.compile(r"[a-z0-9]{1,64}")


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
