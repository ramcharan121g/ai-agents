# AI Task Reminder Agent

This repository contains a lightweight AI agent that:
- reads recent task-related messages from **Gmail** and **Slack**
- uses an LLM to extract actionable tasks
- prints reminders and can optionally post reminders back to Slack

## File

- `/home/runner/work/ai-agents/ai-agents/task_reminder_agent.py` – main agent
- `/home/runner/work/ai-agents/ai-agents/test_task_reminder_agent.py` – unit tests

## Required environment variables

Set these before running:

- `OPENAI_API_KEY`
- `GMAIL_ADDRESS`
- `GMAIL_APP_PASSWORD` (Gmail app password)
- `SLACK_BOT_TOKEN`
- `SLACK_CHANNEL_IDS` (comma-separated channel IDs to scan, e.g. `C123,C456`)

Optional:

- `OPENAI_MODEL` (default: `gpt-4o-mini`)
- `OPENAI_BASE_URL` (default: `https://api.openai.com/v1`)
- `GMAIL_IMAP_HOST` (default: `imap.gmail.com`)
- `SLACK_REMINDER_CHANNEL` (channel to post reminders)
- `POLL_INTERVAL_MINUTES` (default: `15`)
- `LOOKBACK_HOURS` (default: `24`)
- `REMINDER_LOOKAHEAD_HOURS` (default: `24`)

## Run

```bash
python3 task_reminder_agent.py
```

The agent runs in a loop, polls Gmail and Slack, extracts tasks, and sends reminders for new or due-soon items.

## Test

```bash
python3 -m unittest -v
```