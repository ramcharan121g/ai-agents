#!/usr/bin/env python3
"""AI task reminder agent that gathers tasks from Gmail and Slack."""

from __future__ import annotations

import datetime as dt
import hashlib
import imaplib
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email import message_from_bytes
from email.header import decode_header
from email.message import Message
from pathlib import Path
from typing import Any


STATE_FILE = Path(".task_agent_state.json")


@dataclass
class Config:
    openai_api_key: str
    openai_model: str
    openai_base_url: str
    gmail_address: str
    gmail_app_password: str
    gmail_imap_host: str
    slack_bot_token: str
    slack_channel_ids: list[str]
    slack_reminder_channel: str | None
    poll_interval_minutes: int
    lookback_hours: int
    reminder_lookahead_hours: int

    @classmethod
    def from_env(cls) -> "Config":
        missing = [
            key
            for key in (
                "OPENAI_API_KEY",
                "GMAIL_ADDRESS",
                "GMAIL_APP_PASSWORD",
                "SLACK_BOT_TOKEN",
                "SLACK_CHANNEL_IDS",
            )
            if not os.getenv(key)
        ]
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

        return cls(
            openai_api_key=os.environ["OPENAI_API_KEY"],
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            gmail_address=os.environ["GMAIL_ADDRESS"],
            gmail_app_password=os.environ["GMAIL_APP_PASSWORD"],
            gmail_imap_host=os.getenv("GMAIL_IMAP_HOST", "imap.gmail.com"),
            slack_bot_token=os.environ["SLACK_BOT_TOKEN"],
            slack_channel_ids=[channel.strip() for channel in os.environ["SLACK_CHANNEL_IDS"].split(",") if channel.strip()],
            slack_reminder_channel=os.getenv("SLACK_REMINDER_CHANNEL"),
            poll_interval_minutes=int(os.getenv("POLL_INTERVAL_MINUTES", "15")),
            lookback_hours=int(os.getenv("LOOKBACK_HOURS", "24")),
            reminder_lookahead_hours=int(os.getenv("REMINDER_LOOKAHEAD_HOURS", "24")),
        )


def decode_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return value.decode("utf-8", errors="replace")


def decode_mime_header(header_value: str | None) -> str:
    if not header_value:
        return ""
    decoded_parts: list[str] = []
    for part, encoding in decode_header(header_value):
        if isinstance(part, bytes):
            decoded_parts.append(part.decode(encoding or "utf-8", errors="replace"))
        else:
            decoded_parts.append(part)
    return "".join(decoded_parts)


def extract_plain_text(message: Message) -> str:
    if message.is_multipart():
        for part in message.walk():
            content_type = part.get_content_type()
            content_disposition = part.get("Content-Disposition", "")
            if content_type == "text/plain" and "attachment" not in content_disposition.lower():
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                if payload is None:
                    return ""
                return payload.decode(charset, errors="replace")
    payload = message.get_payload(decode=True)
    if payload is None:
        return ""
    charset = message.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")


def fetch_gmail_messages(config: Config) -> list[dict[str, Any]]:
    context = ssl.create_default_context()
    lookback_date = (dt.datetime.utcnow() - dt.timedelta(hours=config.lookback_hours)).strftime("%d-%b-%Y")
    messages: list[dict[str, Any]] = []

    with imaplib.IMAP4_SSL(config.gmail_imap_host, ssl_context=context) as imap:
        imap.login(config.gmail_address, config.gmail_app_password)
        imap.select("INBOX")
        status, message_ids = imap.search(None, f'(SINCE "{lookback_date}")')
        if status != "OK":
            return messages

        for msg_id in message_ids[0].split()[-30:]:
            msg_status, msg_data = imap.fetch(msg_id, "(RFC822)")
            if msg_status != "OK" or not msg_data or not msg_data[0]:
                continue
            raw_email = msg_data[0][1]
            parsed = message_from_bytes(raw_email)
            messages.append(
                {
                    "source": "gmail",
                    "id": decode_text(msg_id),
                    "from": decode_mime_header(parsed.get("From")),
                    "subject": decode_mime_header(parsed.get("Subject")),
                    "date": decode_mime_header(parsed.get("Date")),
                    "body": extract_plain_text(parsed)[:1200],
                }
            )
    return messages


def slack_api_request(token: str, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(
        f"https://slack.com/api/{endpoint}",
        data=data,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read().decode("utf-8")
        parsed = json.loads(body)
        if not parsed.get("ok"):
            raise RuntimeError(f"Slack API error ({endpoint}): {parsed.get('error', 'unknown')}")
        return parsed


def fetch_slack_messages(config: Config) -> list[dict[str, Any]]:
    oldest = str(time.time() - (config.lookback_hours * 3600))
    items: list[dict[str, Any]] = []
    for channel_id in config.slack_channel_ids:
        response = slack_api_request(
            config.slack_bot_token,
            "conversations.history",
            {"channel": channel_id, "limit": "50", "oldest": oldest},
        )
        for message in response.get("messages", []):
            items.append(
                {
                    "source": "slack",
                    "id": message.get("client_msg_id") or message.get("ts"),
                    "channel": channel_id,
                    "timestamp": message.get("ts"),
                    "text": message.get("text", ""),
                    "user": message.get("user", ""),
                }
            )
    return items


def extract_tasks_with_ai(config: Config, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not records:
        return []

    prompt = (
        "You are a task extraction assistant. "
        "From the provided Gmail and Slack records, extract action items assigned to me. "
        "Return strict JSON array only. "
        "Each object: title (string), due_at (ISO 8601 string or null), priority (low|medium|high), "
        "source (gmail|slack), source_id (string), reason (short string)."
    )
    payload = {
        "model": config.openai_model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(records)},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(
        f"{config.openai_base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + config.openai_api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        body = response.read().decode("utf-8")
        parsed = json.loads(body)
        content = parsed["choices"][0]["message"]["content"]
        structured = json.loads(content)
        tasks = structured.get("tasks", structured)
        if not isinstance(tasks, list):
            return []
        return [task for task in tasks if isinstance(task, dict) and task.get("title")]


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"notified_task_hashes": []}
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def task_hash(task: dict[str, Any]) -> str:
    key = "|".join(
        [
            str(task.get("title", "")).strip().lower(),
            str(task.get("source", "")).strip().lower(),
            str(task.get("source_id", "")).strip().lower(),
        ]
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def due_soon(due_at: str | None, lookahead_hours: int) -> bool:
    if not due_at:
        return True
    try:
        due = dt.datetime.fromisoformat(due_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    now = dt.datetime.now(tz=due.tzinfo) if due.tzinfo else dt.datetime.now()
    return due <= now + dt.timedelta(hours=lookahead_hours)


def build_reminder_lines(tasks: list[dict[str, Any]], lookahead_hours: int) -> list[str]:
    lines: list[str] = []
    for task in tasks:
        if not due_soon(task.get("due_at"), lookahead_hours):
            continue
        due = task.get("due_at") or "No due date found"
        lines.append(
            f"- [{task.get('priority', 'medium').upper()}] {task['title']} (due: {due}, source: {task.get('source')})"
        )
    return lines


def send_reminder_to_slack(config: Config, lines: list[str]) -> None:
    if not config.slack_reminder_channel or not lines:
        return
    text = "*Task Reminder*\n" + "\n".join(lines)
    slack_api_request(
        config.slack_bot_token,
        "chat.postMessage",
        {"channel": config.slack_reminder_channel, "text": text},
    )


def run_once(config: Config) -> int:
    records = fetch_gmail_messages(config) + fetch_slack_messages(config)
    tasks = extract_tasks_with_ai(config, records)

    state = load_state()
    seen = set(state.get("notified_task_hashes", []))
    new_tasks = [task for task in tasks if task_hash(task) not in seen]

    reminder_lines = build_reminder_lines(new_tasks, config.reminder_lookahead_hours)
    if reminder_lines:
        print("Task reminders:")
        for line in reminder_lines:
            print(line)
        send_reminder_to_slack(config, reminder_lines)

    for task in new_tasks:
        seen.add(task_hash(task))
    state["notified_task_hashes"] = sorted(seen)
    save_state(state)
    return len(reminder_lines)


def main() -> None:
    config = Config.from_env()
    print("Task reminder agent started.")
    while True:
        try:
            reminder_count = run_once(config)
            print(f"[{dt.datetime.utcnow().isoformat()}] cycle complete, reminders sent: {reminder_count}")
        except (RuntimeError, urllib.error.URLError, TimeoutError, imaplib.IMAP4.error, ValueError) as exc:
            print(f"Agent cycle failed: {exc}")
        time.sleep(config.poll_interval_minutes * 60)


if __name__ == "__main__":
    main()
