from fastapi import HTTPException, Request
from pydantic import BaseModel


class CurrentUser(BaseModel):
    id: str
    username: str


# 第 1 步先用假会话。第 2 步换成：带着浏览器的 cookie 去问 Django 的 /api/auth/me/
FAKE_SESSIONS = {
    "demo-u1": CurrentUser(id="u1", username="小明"),
    "demo-u2": CurrentUser(id="u2", username="小红"),
}


def get_current_user(request: Request) -> CurrentUser:
    user = FAKE_SESSIONS.get(request.cookies.get("sessionid", ""))
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    return user
