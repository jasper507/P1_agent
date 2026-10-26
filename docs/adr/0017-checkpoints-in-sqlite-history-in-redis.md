# 断点存在 SQLite，对话历史还在 Redis

LangGraph 停下来的时候要把状态存下来（checkpoint，这里叫断点），用户点了确认再读出来接着跑。它提供几种存法：内存、SQLite、Postgres，以及社区维护的 Redis 版。

我们决定：

- **断点只管还没跑完的那一轮**。用对话编号当 LangGraph 的 thread_id。一轮跑完，消息照旧整轮存进 Redis 的对话历史，断点马上删掉；新的一轮开始前也先删一次，免得上一轮出错留下的半截混进来。
- **断点存在本机的 SQLite 文件** `.data/checkpoints.sqlite`。服务启动时打开、建表，重启以后照样能接着确认。
- **停下来时也写一次对话历史**：历史不变，但重新开始 30 分钟计时，也记下这段对话属于谁。第一轮就停下来的对话，Redis 里本来还没有它，用户点确认时就认不出是谁的。

## Considered Options

- **Redis 版的 checkpointer**：和对话历史放在一起，还支持过期时间，断点到时间自己消失。但它要 RedisJSON 和 RediSearch 两个模块，Redis 8 才自带，我们 compose 里是 Redis 7.4，要换镜像；测试用的 fakeredis 也没有这两个模块。上线时值得换。
- **内存版（InMemorySaver）**：最简单，但服务一重启，等着确认的操作就全没了，开多个进程也互相看不到。
- **Postgres 版**：官方维护，生产最稳。现在为它多起一个数据库不值得。
- **整个对话历史都放进断点，不用 Redis 了**：LangGraph 推荐这么用。但第 3 步的归属检查、按轮截断、30 分钟过期都已经写好、测好了，断点还会把每一步的状态都存一份，比历史大得多。

## Consequences

- 用户一直不点、对话过期了，断点会一直留在 SQLite 里，只占一点空间。Redis 版的过期时间正好解决这个问题，换的时候一起解决。
- SQLite 只适合一台机器。开多台机器时，要换成 Postgres 或 Redis 版。代码里只改创建 checkpointer 的那几行。
- `.data/` 加进了 `.gitignore`。
