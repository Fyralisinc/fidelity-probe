"""Slack historical ingestion — the two-token model, with full cursor pagination
and per-page schema validation.

Slack data arrives via two tokens because of a hard Slack constraint: a **bot
token cannot read human-human DMs**. So:

  * the **bot** token (xoxb) lists + reads public/private CHANNELS;
  * the **user** token (xoxp) lists + reads the consenting human's 1:1 DMs (im)
    and group DMs (mpim).

This slice exercises both paths and — critically — verifies the constraint that
makes them separate: it confirms the bot token is *refused* when it tries to read
a DM. A target that lets the bot read DMs would be a false green (real Slack
forbids it), so that refusal is asserted, not assumed.

Every list/read endpoint paginates with response_metadata.next_cursor; the
slack_sdk SlackResponse follows that cursor automatically, giving us per-page
access to validate each response and record the cursor chain.
"""
from __future__ import annotations

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from ..fidelity import FidelityReport
from ..schemas import SpecValidator

_PAGE = 200

# Errors a faithful Slack returns when the WRONG token type / a non-member tries
# to read a DM. Any of these proves the bot is correctly refused.
_DM_REFUSAL_ERRORS = {
    "not_allowed_token_type", "missing_scope", "not_in_channel",
    "channel_not_found", "no_permission",
}


def _cursor_of(page) -> str | None:
    meta = (page.data or {}).get("response_metadata") or {}
    return meta.get("next_cursor") or None


def auth_test(client: WebClient, sv: SpecValidator, report: FidelityReport,
              *, label_prefix: str = "") -> dict:
    resp = client.auth_test()
    sv.validate_response(resp.data, "/auth.test", report,
                         label=f"auth.test{label_prefix}")
    return resp.data


def list_users(client: WebClient, sv: SpecValidator, report: FidelityReport) -> int:
    total = 0
    for page in client.users_list(limit=_PAGE):
        report.record_page("users.list", _cursor_of(page))
        sv.validate_response(page.data, "/users.list", report, label="users.list")
        total += len(page.get("members") or [])
    report.count("user", total)
    return total


# --------------------------------------------------------------------------- channels (bot)

def list_channels(client: WebClient, sv: SpecValidator,
                  report: FidelityReport) -> list[dict]:
    """Bot-token channel listing. Public + private only — a bot token does not
    surface human DMs, so we do NOT request im/mpim here."""
    channels: list[dict] = []
    for page in client.conversations_list(limit=_PAGE,
                                          types="public_channel,private_channel"):
        report.record_page("conversations.list", _cursor_of(page))
        sv.validate_response(page.data, "/conversations.list", report,
                             label="conversations.list")
        channels.extend(page.get("channels") or [])
    report.count("channel", len(channels))
    return channels


def fetch_history(client: WebClient, channel_id: str, sv: SpecValidator,
                  report: FidelityReport, *, op_label: str = "conversations.history") -> list[str]:
    """Pull a conversation's full message history; return thread-parent ts values."""
    thread_parents: list[str] = []
    msg_count = 0
    try:
        for page in client.conversations_history(channel=channel_id, limit=_PAGE):
            report.record_page(op_label, _cursor_of(page))
            sv.validate_response(page.data, "/conversations.history", report,
                                 label=op_label)
            for m in page.get("messages") or []:
                msg_count += 1
                if m.get("thread_ts") and m.get("reply_count"):
                    thread_parents.append(m["thread_ts"])
    except SlackApiError as e:
        report.note(f"{op_label}({channel_id}): {e.response.get('error')}")
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
            count += max(len(page.get("messages") or []) - 1, 0)
    except SlackApiError as e:
        report.note(f"conversations.replies({channel_id},{thread_ts}): {e.response.get('error')}")
    report.count("reply", count)
    return count


# --------------------------------------------------------------------------- DMs (user token)

def list_dms(user_client: WebClient, sv: SpecValidator,
             report: FidelityReport) -> list[dict]:
    """User-token DM listing: the consenting human's 1:1 DMs (im) + group DMs (mpim)."""
    dms: list[dict] = []
    for page in user_client.conversations_list(limit=_PAGE, types="im,mpim"):
        report.record_page("conversations.list.dm", _cursor_of(page))
        sv.validate_response(page.data, "/conversations.list", report,
                             label="conversations.list (im,mpim)")
        dms.extend(page.get("channels") or [])
    ims = [d for d in dms if d.get("is_im")]
    mpims = [d for d in dms if d.get("is_mpim")]
    report.count("im", len(ims))
    report.count("mpim", len(mpims))
    _check_dm_object_shapes(ims, mpims, report)
    return dms


def _check_dm_object_shapes(ims: list[dict], mpims: list[dict],
                            report: FidelityReport) -> None:
    """Assert the documented im/mpim object shapes (the spec is closed/incomplete,
    so these intrinsic fields are checked explicitly rather than via the schema)."""
    for im in ims:
        # A real `im` object carries the counterpart `user` and has no `name`.
        report.record_protocol(
            "im.object.has_user", bool(im.get("user")),
            f"im {im.get('id')} has no `user` (counterpart) field" if not im.get("user")
            else "", diverge_category="schema")
        report.record_protocol(
            "im.object.no_name", "name" not in im,
            f"im {im.get('id')} carries a `name` (real im objects have none)"
            if "name" in im else "", diverge_category="schema")
    for mp in mpims:
        report.record_protocol(
            "mpim.is_mpim", mp.get("is_mpim") is True,
            f"mpim {mp.get('id')} missing is_mpim=true" if mp.get("is_mpim") is not True
            else "", diverge_category="schema")


def check_bot_cannot_read_dms(bot_client: WebClient, dms: list[dict],
                              report: FidelityReport) -> None:
    """The two-token invariant: a bot token must be REFUSED when reading a human DM.

    If the bot token can read a DM's history, the target is more permissive than
    real Slack — a false green that would break against the real API. So a success
    here is a protocol divergence.
    """
    if not dms:
        report.note("bot-cannot-read-DM check skipped: no DMs were listed.")
        return
    sample = dms[0]
    dm_id = sample.get("id")
    try:
        resp = bot_client.conversations_history(channel=dm_id, limit=1)
        # A faithful Slack does NOT reach here for a human DM under a bot token.
        ok = not resp.get("ok", True)
        report.record_protocol(
            "two_token.bot_refused_on_dm", ok,
            f"bot token READ DM {dm_id} (real Slack forbids bot tokens from reading "
            f"human DMs — this is a false green)",
        )
    except SlackApiError as e:
        err = e.response.get("error")
        refused = err in _DM_REFUSAL_ERRORS
        report.record_protocol(
            "two_token.bot_refused_on_dm", refused,
            f"bot read of DM {dm_id} failed with `{err}` (expected one of "
            f"{sorted(_DM_REFUSAL_ERRORS)})" if not refused
            else f"bot correctly refused on DM with `{err}`",
        )


def run_historical(bot_client: WebClient, user_client: WebClient | None,
                   report: FidelityReport, max_channels: int | None = None) -> None:
    sv = SpecValidator("slack")

    # --- channel path (bot token) ---
    bot_identity = auth_test(bot_client, sv, report)
    report.auth.update({
        "bot_user": bot_identity.get("user"), "bot_user_id": bot_identity.get("user_id"),
        "bot_id": bot_identity.get("bot_id"), "team_id": bot_identity.get("team_id"),
        "url": bot_identity.get("url"),
    })
    list_users(bot_client, sv, report)
    channels = list_channels(bot_client, sv, report)
    scanned = channels if max_channels is None else channels[:max_channels]
    for ch in scanned:
        cid = ch.get("id")
        if not cid:
            continue
        for thread_ts in fetch_history(bot_client, cid, sv, report):
            fetch_replies(bot_client, cid, thread_ts, sv, report)

    # --- DM path (user token) ---
    if user_client is None:
        report.note("No user (xoxp) token available — the DM (im/mpim) ingestion "
                    "path was NOT exercised. The two-token model is only partially "
                    "verified.")
        return
    user_identity = auth_test(user_client, sv, report, label_prefix=" (user)")
    report.auth["acting_user_id"] = user_identity.get("user_id")
    # auth.test on a user token returns the human user_id and NO bot_id.
    report.record_protocol(
        "user_token.no_bot_id", "bot_id" not in user_identity,
        "auth.test on a user token returned bot_id (real Slack omits it for user "
        "tokens)" if "bot_id" in user_identity else "",
    )
    dms = list_dms(user_client, sv, report)
    dm_scan = dms if max_channels is None else dms[: max_channels]
    for dm in dm_scan:
        did = dm.get("id")
        if did:
            fetch_history(user_client, did, sv, report, op_label="conversations.history.dm")

    # The invariant that makes the two tokens necessary.
    check_bot_cannot_read_dms(bot_client, dms, report)
