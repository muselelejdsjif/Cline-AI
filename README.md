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
python3 tools/telegram.py send --chat-id <chat_id> --text '部署完成 ✅'

# 内容较长或含引号时,从文件读取正文(推荐)
python3 tools/telegram.py send --chat-id <chat_id> \
    --parse-mode HTML --text-file /tmp/digest.html

# 发文件(日志、报告、截图)
python3 tools/telegram.py send-file --chat-id <chat_id> \
    --file build/report.txt --caption 'nightly build'

# 发图片
python3 tools/telegram.py send-file --chat-id <chat_id> \
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

---

## 24/7 新闻推送(GitHub Actions)

`scripts/news_push.py` + GitHub Actions 工作流组成一个**不依赖任何个人电脑**的推送任务。

> 本仓库的自动化提交账号没有 `workflows` 权限,无法直接推送 `.github/workflows/` 下的文件,
> 因此工作流内容放在 `docs/news-workflow.yml`,需要你在 GitHub 网页上手动建一次(见下方步骤)。

工作流行为:

- 每 10 分钟由 GitHub 云端触发(runs-on `ubuntu-latest`);
- 从 Google News RSS(四类关键词)和 BBC / 36氪 RSS 抓取,按 `AI / 外贸 / 经济 / 国际` 分类,每类最多 1 条;
- 与 `bot-state` 分支里的 `.news_seen.json` 去重,无新内容不发送,连续 6 次空跑发一次心跳;
- 用仓库 Secret `TELEGRAM_BOT_TOKEN` 调 Bot API 推送;
- 状态回写到 `bot-state` 分支,不污染主分支历史。

### 启用步骤

1. 仓库 → Settings → Secrets and variables → Actions → 新建 secret:
   名称 `TELEGRAM_BOT_TOKEN`,值为 bot token;
2. (可选)新建 repository variable `TELEGRAM_CHAT_ID`,不设时默认 `8611407819`;
3. 打开 `docs/news-workflow.yml`,点右上角 **Raw** 复制全部内容;
4. 仓库首页 → **Add file → Create new file**,文件名填 `.github/workflows/news.yml`,
   粘贴内容并提交(这次提交需要由你本人完成,自动化账号无 workflow 权限);
5. 在 Actions 页面手动 **Run workflow** 验证一次;
6. 验证通过后,记得暂停平台自带的定时任务,避免重复推送。

### 注意

- GitHub 的 schedule 在高负载时可能延迟几分钟,不是秒级精确;
- 公开仓库的定时工作流若 60 天无提交活动会被自动禁用,提交一次即可恢复;
- 本地手动跑:

```bash
export TELEGRAM_BOT_TOKEN='...'
python3 scripts/news_push.py --chat-id 8611407819 --dry-run   # 只打印
python3 scripts/news_push.py --chat-id 8611407819             # 真发
```
