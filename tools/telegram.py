#!/usr/bin/env python3
"""Minimal Telegram Bot API client built on the Python standard library.

Token resolution order:
    1. --token argument
    2. TELEGRAM_BOT_TOKEN environment variable
    3. file given by --token-file (default: .telegram_token, git-ignored)

Examples:
    export TELEGRAM_BOT_TOKEN=123456:ABC...
    python3 tools/telegram.py me
    python3 tools/telegram.py updates
    python3 tools/telegram.py chats
    python3 tools/telegram.py send --chat-id 123456789 --text "hello"
    python3 tools/telegram.py send-file --chat-id 123456789 --file build/report.txt
    python3 tools/telegram.py listen --timeout 30 --count 5
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple

API_ROOT = "https://api.telegram.org/bot{token}/{method}"
DEFAULT_TOKEN_FILE = ".telegram_token"
HTTP_TIMEOUT = 40


class TelegramError(RuntimeError):
    """Raised when the Bot API answers with ok=false or the request fails."""


def resolve_token(args: argparse.Namespace) -> str:
    token = getattr(args, "token", None) or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        token_file = getattr(args, "token_file", DEFAULT_TOKEN_FILE)
        if token_file and os.path.isfile(token_file):
            with open(token_file, "r", encoding="utf-8") as handle:
                token = handle.read().strip()
    if not token:
        raise TelegramError(
            "No bot token found. Pass --token, set TELEGRAM_BOT_TOKEN, "
            f"or write the token to {DEFAULT_TOKEN_FILE}."
        )
    return token


def _encode_multipart(
    fields: Dict[str, Any], files: Dict[str, Tuple[str, bytes]]
) -> Tuple[bytes, str]:
    boundary = f"----ClineBoundary{uuid.uuid4().hex}"
    chunks: List[bytes] = []
    for name, value in fields.items():
        if value is None:
            continue
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        chunks.append(str(value).encode())
        chunks.append(b"\r\n")
    for name, (filename, payload) in files.items():
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            (
                f'Content-Disposition: form-data; name="{name}"; '
                f'filename="{filename}"\r\n'
            ).encode()
        )
        chunks.append(f"Content-Type: {content_type}\r\n\r\n".encode())
        chunks.append(payload)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), boundary



def call(
    token: str,
    method: str,
    fields: Optional[Dict[str, Any]] = None,
    files: Optional[Dict[str, Tuple[str, bytes]]] = None,
    timeout: int = HTTP_TIMEOUT,
) -> Any:
    """Call a Bot API method and return its `result` payload."""
    url = API_ROOT.format(token=token, method=method)
    fields = fields or {}

    if files:
        body, boundary = _encode_multipart(fields, files)
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
    else:
        encoded = urllib.parse.urlencode(
            {k: v for k, v in fields.items() if v is not None}
        ).encode()
        request = urllib.request.Request(url, data=encoded, method="POST")

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # API errors still carry JSON bodies
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            raise TelegramError(f"HTTP {exc.code}: {raw[:300]}") from exc
    except urllib.error.URLError as exc:
        raise TelegramError(f"Network error calling {method}: {exc.reason}") from exc

    if not payload.get("ok"):
        raise TelegramError(
            f"{method} failed ({payload.get('error_code')}): "
            f"{payload.get('description')}"
        )
    return payload.get("result")


def describe_update(update: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten one update into the fields that matter for scripting."""
    message = (
        update.get("message")
        or update.get("edited_message")
        or update.get("channel_post")
        or update.get("edited_channel_post")
        or {}
    )
    chat = message.get("chat", {})
    sender = message.get("from", {})
    return {
        "update_id": update.get("update_id"),
        "chat_id": chat.get("id"),
        "chat_type": chat.get("type"),
        "chat_title": chat.get("title") or chat.get("username") or chat.get("first_name"),
        "from": sender.get("username") or sender.get("first_name"),
        "date": message.get("date"),
        "text": message.get("text") or message.get("caption"),
    }


def iter_updates(
    token: str, offset: Optional[int], poll_timeout: int, limit: int
) -> Iterable[Dict[str, Any]]:
    updates = call(
        token,
        "getUpdates",
        {"offset": offset, "timeout": poll_timeout, "limit": limit},
        timeout=poll_timeout + 15,
    )
    for update in updates:
        yield update


def parse_message_id(result: Dict[str, Any]) -> str:
    return str(result.get("message_id", "?"))


def command_me(args: argparse.Namespace) -> int:
    bot = call(resolve_token(args), "getMe")
    print(
        json.dumps(
            {
                "id": bot.get("id"),
                "username": bot.get("username"),
                "name": bot.get("first_name"),
                "can_join_groups": bot.get("can_join_groups"),
                "can_read_all_group_messages": bot.get("can_read_all_group_messages"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def command_updates(args: argparse.Namespace) -> int:
    token = resolve_token(args)
    updates = list(iter_updates(token, args.offset, args.timeout, args.limit))
    print(
        json.dumps(
            [describe_update(u) for u in updates], ensure_ascii=False, indent=2
        )
    )
    return 0


def command_chats(args: argparse.Namespace) -> int:
    token = resolve_token(args)
    seen: Dict[int, Dict[str, Any]] = {}
    for update in iter_updates(token, args.offset, args.timeout, args.limit):
        info = describe_update(update)
        if info["chat_id"] is not None:
            seen[info["chat_id"]] = {
                "chat_id": info["chat_id"],
                "chat_type": info["chat_type"],
                "chat_title": info["chat_title"],
            }
    print(json.dumps(list(seen.values()), ensure_ascii=False, indent=2))
    return 0


def command_send(args: argparse.Namespace) -> int:
    token = resolve_token(args)
    text = args.text
    if args.text_file:
        with open(args.text_file, "r", encoding="utf-8") as handle:
            text = handle.read()
    if not text:
        raise TelegramError("Message text is empty (use --text or --text-file)")
    result = call(
        token,
        "sendMessage",
        {
            "chat_id": args.chat_id,
            "text": text,
            "parse_mode": args.parse_mode,
            "disable_web_page_preview": "true" if args.no_preview else None,
            "disable_notification": "true" if args.silent else None,
            "reply_to_message_id": args.reply_to,
            "message_thread_id": args.thread_id,
        },
    )
    print(f"sent message_id={parse_message_id(result)} chat_id={args.chat_id}")
    return 0


def command_send_file(args: argparse.Namespace) -> int:
    token = resolve_token(args)
    path = args.file
    if not os.path.isfile(path):
        raise TelegramError(f"File not found: {path}")
    with open(path, "rb") as handle:
        payload = handle.read()
    method = "sendPhoto" if args.as_photo else "sendDocument"
    field = "photo" if args.as_photo else "document"
    result = call(
        token,
        method,
        {
            "chat_id": args.chat_id,
            "caption": args.caption,
            "parse_mode": args.parse_mode,
            "message_thread_id": args.thread_id,
        },
        files={field: (os.path.basename(path), payload)},
    )
    print(f"sent message_id={parse_message_id(result)} chat_id={args.chat_id}")
    return 0


def command_listen(args: argparse.Namespace) -> int:
    token = resolve_token(args)
    offset = args.offset
    received = 0
    print("# listening; press Ctrl+C to stop", flush=True)
    while received < args.count:
        updates = list(iter_updates(token, offset, args.timeout, 100))
        for update in updates:
            offset = update["update_id"] + 1
            info = describe_update(update)
            print(json.dumps(info, ensure_ascii=False), flush=True)
            received += 1
            if received >= args.count:
                break
        if not updates:
            if args.once:
                break
            time.sleep(1)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--token", help="Bot token (or use TELEGRAM_BOT_TOKEN)")
    parser.add_argument(
        "--token-file", default=DEFAULT_TOKEN_FILE, help="File holding the bot token"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    me = subparsers.add_parser("me", help="Show bot identity")
    me.set_defaults(func=command_me)

    updates = subparsers.add_parser("updates", help="Fetch recent updates")
    updates.add_argument("--offset", type=int)
    updates.add_argument("--timeout", type=int, default=0)
    updates.add_argument("--limit", type=int, default=20)
    updates.set_defaults(func=command_updates)

    chats = subparsers.add_parser("chats", help="List chats seen in updates")
    chats.add_argument("--offset", type=int)
    chats.add_argument("--timeout", type=int, default=0)
    chats.add_argument("--limit", type=int, default=100)
    chats.set_defaults(func=command_chats)

    send = subparsers.add_parser("send", help="Send a text message")
    send.add_argument("--chat-id", required=True)
    send.add_argument("--text")
    send.add_argument(
        "--text-file", help="Read message body from a UTF-8 file instead of --text"
    )
    send.add_argument(
        "--parse-mode", choices=["Markdown", "MarkdownV2", "HTML"], default=None
    )
    send.add_argument("--no-preview", action="store_true")
    send.add_argument("--silent", action="store_true")
    send.add_argument("--reply-to", type=int)
    send.add_argument("--thread-id", type=int)
    send.set_defaults(func=command_send)

    send_file = subparsers.add_parser("send-file", help="Send a document or photo")
    send_file.add_argument("--chat-id", required=True)
    send_file.add_argument("--file", required=True)
    send_file.add_argument("--caption")
    send_file.add_argument(
        "--parse-mode", choices=["Markdown", "MarkdownV2", "HTML"], default=None
    )
    send_file.add_argument("--as-photo", action="store_true")
    send_file.add_argument("--thread-id", type=int)
    send_file.set_defaults(func=command_send_file)

    listen = subparsers.add_parser("listen", help="Poll and print incoming messages")
    listen.add_argument("--offset", type=int)
    listen.add_argument("--timeout", type=int, default=25, help="Long-poll seconds")
    listen.add_argument("--count", type=int, default=10)
    listen.add_argument("--once", action="store_true")
    listen.set_defaults(func=command_listen)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except TelegramError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())

