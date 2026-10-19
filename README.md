# Cline-AI 沙箱工作区

本仓库用于存放 Cline 智能体在沙箱中的产出与工具脚本。

## tools/telegram.py

纯标准库实现的 Telegram Bot API 客户端,用于从沙箱向 Telegram 推送消息、文件和读取更新。

### 认证

按以下顺序解析 token:

1. `--token` 参数
2. 环境变量 `TELEGRAM_BOT_TOKEN`
3. `.telegram_token` 文件(已在 `.gitignore` 中忽略)

```bash
export TELEGRAM_BOT_TOKEN='123456:ABC...'
# 或者
echo '123456:ABC...' > .telegram_token && chmod 600 .telegram_token
```

### 常用命令

```bash
# 机器人身份
python3 tools/telegram.py me

# 查看收到过消息的会话(拿 chat_id)
python3 tools/telegram.py chats

# 原始更新
python3 tools/telegram.py updates --limit 20

# 发文本(Markdown / MarkdownV2 / HTML)
python3 tools/telegram.py send --chat-id 8611407819 --text '部署完成 ✅'

# 发文件(日志、报告、截图)
python3 tools/telegram.py send-file --chat-id 8611407819 \
    --file build/report.txt --caption 'nightly build'

# 发图片
python3 tools/telegram.py send-file --chat-id 8611407819 \
    --file screenshot.png --as-photo

# 轮询接收消息(最多 5 条,长轮询 25 秒)
python3 tools/telegram.py listen --count 5 --timeout 25
```

### 说明与限制

- 沙箱没有公网入口,无法注册 webhook,因此只能主动发送 + 长轮询接收;会话结束后轮询即停止。
- Bot 在群组中默认只接收 `/命令` 和回复它的消息;需要读取全部群消息时,通过 BotFather 的
  `/setprivacy` 关闭隐私模式。
- `listen` 不带持久化 offset,重复启动可能重复输出同一条更新;需要精确去重时可自行记录
  `update_id`。
