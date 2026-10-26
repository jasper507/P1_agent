import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError, WatchError

from app.config import get_settings

# 一轮：用户的一句话，加上它引出的所有工具调用、查询结果和最终回复，按发给模型的格式原样保存
Round = list[dict]


class ConversationError(Exception):
    """对话存储出错。status_code 和 message 直接用作接口的返回。"""

    status_code = 500
    message = "对话服务出错了"


class ConversationNotFound(ConversationError):
    # 别人的对话也返回这个：不承认它存在，和商城对别人的订单回 404 一样
    status_code = 404
    message = "对话不存在或已过期"


class ConversationBusy(ConversationError):
    status_code = 409
    message = "上一条还在处理，请稍后再发"


class ConversationUnavailable(ConversationError):
    status_code = 503
    message = "对话服务暂时不可用，请稍后再试"


class ConversationStore:
    """对话历史存在 Redis 里，一段对话一个 key，值是 {"user_id": ..., "rounds": [...]}。

    读写都在对话锁里进行，同一时间只有一个请求在改它，所以整体读出、整体写回就够了。
    """

    def __init__(self, redis: Redis, *, ttl_seconds: int, max_rounds: int, lock_seconds: int):
        self._redis = redis
        self._ttl = ttl_seconds
        self._max_rounds = max_rounds
        self._lock_seconds = lock_seconds

    async def load(self, conversation_id: UUID, user_id: int) -> list[Round]:
        """不存在、已过期、不属于这个用户，一律当作不存在。"""
        raw = await self._call(self._redis.get(self._key(conversation_id)))
        if raw is None:
            raise ConversationNotFound()
        data = json.loads(raw)
        if data["user_id"] != user_id:
            raise ConversationNotFound()
        return data["rounds"]

    async def save_round(self, conversation_id: UUID, user_id: int, history: list[Round], new_round: Round) -> None:
        """一轮完整跑完才调用。只留最近 max_rounds 轮，整轮丢，并重新开始计算过期时间。"""
        await self._write(conversation_id, user_id, (history + [new_round])[-self._max_rounds:])

    async def touch(self, conversation_id: UUID, user_id: int, history: list[Round]) -> None:
        """这一轮停下来等用户确认：历史不变，但重新开始计时，也记下对话属于谁。

        第一轮就停下来的对话，Redis 里还没有它，也要靠这一步才存在，用户点确认时才认得出是谁的对话。
        """
        await self._write(conversation_id, user_id, history)

    async def _write(self, conversation_id: UUID, user_id: int, rounds: list[Round]) -> None:
        value = json.dumps({"user_id": user_id, "rounds": rounds}, ensure_ascii=False)
        await self._call(self._redis.set(self._key(conversation_id), value, ex=self._ttl))

    @asynccontextmanager
    async def lock(self, conversation_id: UUID) -> AsyncIterator[None]:
        """同一段对话同时只处理一条消息。

        锁带过期时间：进程半路崩了，锁也会自己消失，不会把对话永远锁住。
        token 用来认出「这是我加的锁」，释放时只删自己的。
        """
        key = self._key(conversation_id) + ":lock"
        token = uuid.uuid4().hex
        if not await self._call(self._redis.set(key, token, nx=True, ex=self._lock_seconds)):
            raise ConversationBusy()
        try:
            yield
        finally:
            await self._release(key, token)

    async def aclose(self) -> None:
        await self._redis.aclose()

    async def _release(self, key: str, token: str) -> None:
        # 一轮跑得比锁还久时，锁已经过期、可能被下一条消息拿走了，这时不能删。
        # 「比较再删除」必须是原子的：WATCH 之后 key 被别人改过，事务就不会执行。
        try:
            async with self._redis.pipeline() as pipe:
                await pipe.watch(key)
                if await pipe.get(key) == token:
                    pipe.multi()
                    pipe.delete(key)
                    await pipe.execute()
        except (WatchError, RedisError):
            pass  # 锁已经不是自己的，或者 Redis 暂时连不上：反正它会自己过期

    @staticmethod
    def _key(conversation_id: UUID) -> str:
        return f"conversation:{conversation_id}"

    @staticmethod
    async def _call(awaitable):
        try:
            return await awaitable
        except RedisError as e:  # 连不上、超时
            raise ConversationUnavailable() from e


@lru_cache
def get_conversations() -> ConversationStore:
    """整个进程共用一个 Redis 连接池，和 get_django、get_llm 一样。"""
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=2, socket_timeout=2)
    return ConversationStore(
        redis,
        ttl_seconds=settings.conversation_ttl_seconds,
        max_rounds=settings.conversation_max_rounds,
        lock_seconds=settings.conversation_lock_seconds,
    )
