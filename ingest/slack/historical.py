"""Slack historical ingestion — full cursor pagination + per-page schema validation.

Every Slack list/read endpoint paginates with response_metadata.next_cursor. The
slack_sdk SlackResponse is iterable and follows that cursor automatically, yielding
one SlackResponse per underlying API call — so this is exactly the production
pattern, and it also gives us per-page access to validate each response and record
the cursor chain for the fidelity report.
"""
from __future__ import annotations

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from ..fidelity import FidelityReport
from ..schemas import SpecValidator

_PAGE = 200


def _cursor_of(page) -> str | None:
    meta = (page.data or {}).get("response_metadata") or {}
    return meta.get("next_cursor") or None


def auth_test(client: WebClient, sv: SpecValidator, report: FidelityReport) -> dict:
    resp = client.auth_test()
    sv.validate_response(resp.data, "/auth.test", report, label="auth.test")
    report.auth.update({
        "team": resp.get("team"), "team_id": resp.get("team_id"),
        "user": resp.get("user"), "user_id": resp.get("user_id"),
        "url": resp.get("url"),
    })
    return resp.data


def list_users(client: WebClient, sv: SpecValidator, report: FidelityReport) -> int:
    total = 0
    for page in client.users_list(limit=_PAGE):
        report.record_page("users.list", _cursor_of(page))
        sv.validate_response(page.data, "/users.list", report, label="users.list")
        members = page.get("members") or []
        total += len(members)
    report.count("user", total)
    return total


def list_conversations(client: WebClient, sv: SpecValidator,
                       report: FidelityReport) -> list[dict]:
    channels: list[dict] = []
    for page in client.conversations_list(
        limit=_PAGE,
        types="public_channel,private_channel,mpim,im",
    ):
        report.record_page("conversations.list", _cursor_of(page))
        sv.validate_response(page.data, "/conversations.list", report,
                             label="conversations.list")
        channels.extend(page.get("channels") or [])
    report.count("channel", len(channels))
    return channels


def fetch_history(client: WebClient, channel_id: str, sv: SpecValidator,
                  report: FidelityReport) -> list[str]:
    """Pull a channel's full message history; return thread-parent ts values."""
    thread_parents: list[str] = []
    msg_count = 0
    try:
        for page in client.conversations_history(channel=channel_id, limit=_PAGE):
            report.record_page("conversations.history", _cursor_of(page))
            sv.validate_response(page.data, "/conversations.history", report,
                                 label="conversations.history")
            for m in page.get("messages") or []:
                msg_count += 1
                if m.get("thread_ts") and m.get("reply_count"):
                    thread_parents.append(m["thread_ts"])
    except SlackApiError as e:
        # e.g. not_in_channel for some publics; record and continue (real client behavior)
        report.note(f"conversations.history({channel_id}): {e.response.get('error')}")
    report.count("message", msg_count)
    return thread_parents


def fetch_replies(client: WebClient, channel_id: str, thread_ts: str,
                  sv: SpecValidator, report: FidelityReport) -> int:
    count = 0
    try:
        for page in client.conversations_replies(channel=channel_id, ts=thread_ts, limit=_PAGE):
            report.record_page("conversations.replies", _cursor_of(page))
            sv.validate_response(page.data, "/conversations.replies", report,
                                 label="conversations.replies")
            # the parent message is echoed in each replies page; count replies only
            msgs = page.get("messages") or []
            count += max(len(msgs) - 1, 0)
    except SlackApiError as e:
        report.note(f"conversations.replies({channel_id},{thread_ts}): {e.response.get('error')}")
    report.count("reply", count)
    return count


def run_historical(client: WebClient, report: FidelityReport,
                   max_channels: int | None = None) -> None:
    sv = SpecValidator("slack")
    auth_test(client, sv, report)
    list_users(client, sv, report)
    channels = list_conversations(client, sv, report)
    if max_channels is not None:
        channels = channels[:max_channels]
    for ch in channels:
        cid = ch.get("id")
        if not cid:
            continue
        for thread_ts in fetch_history(client, cid, sv, report):
            fetch_replies(client, cid, thread_ts, sv, report)
