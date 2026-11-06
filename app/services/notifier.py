import asyncio
import logging
import uuid

from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    PushMessageRequest,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.messaging.exceptions import ApiException

from app.core.config import settings

logger = logging.getLogger(__name__)

# Fixed arbitrary namespace so uuid5(namespace, dedupe_key) is stable across
# process restarts — that's what makes x_line_retry_key actually dedupe a
# re-sent push instead of just being a random UUID each time.
_RETRY_NAMESPACE = uuid.UUID("6f7d3c1a-2b8e-4a5f-9c3d-1e2f3a4b5c6d")


def _get_client() -> MessagingApi:
    configuration = Configuration(access_token=settings.line_channel_access_token)
    return MessagingApi(ApiClient(configuration))


# LINE will send reply token for every msg
# We just use this to reply to that msg
# ** Reply Token Expires in 1 MIN
def _reply_sync(reply_token: str, text: str) -> None:
    api = _get_client()
    try:
        api.reply_message(ReplyMessageRequest(reply_token=reply_token, messages=[TextMessage(text=text)]))
    except ApiException:
        logger.exception("Failed to send reply ack (reply_token may have expired)")


# Send message by targeting conversation directly
# Using group_id / room_id / user_id
def _push_sync(conversation_id: str, text: str, retry_key: str) -> None:
    api = _get_client()
    try:
        api.push_message(
            PushMessageRequest(to=conversation_id, messages=[TextMessage(text=text)]),
            x_line_retry_key=retry_key,
        )
    except ApiException as e:
        # 409 "The retry key is already accepted" is the dedupe working, not a
        # failure: LINE already delivered this exact push. Raising here is what
        # turned a successfully published issue into a 'failed' request plus a
        # bogus "couldn't build that issue" message in the group.
        if e.status == 409:
            logger.info("Push with retry key %s was already delivered — treating as sent", retry_key)
            return
        raise


# Which endpoint serves a display name depends on where the message came from:
# a user must have added the bot as a friend for get_profile to work, but the
# group/room member endpoints work for any member.
def _profile_sync(conversation_type: str, conversation_id: str, user_id: str) -> str | None:
    api = _get_client()
    try:
        if conversation_type == "group":
            return api.get_group_member_profile(conversation_id, user_id).display_name
        if conversation_type == "room":
            return api.get_room_member_profile(conversation_id, user_id).display_name
        return api.get_profile(user_id).display_name
    except ApiException:
        # Non-fatal: the summarizer falls back to the raw user_id.
        logger.warning("Could not resolve display name for %s in %s", user_id, conversation_id)
        return None


async def fetch_display_name(conversation_type: str, conversation_id: str, user_id: str) -> str | None:
    return await asyncio.to_thread(_profile_sync, conversation_type, conversation_id, user_id)


async def reply_ack(reply_token: str, text: str) -> None:
    await asyncio.to_thread(_reply_sync, reply_token, text)


async def push_result(conversation_id: str, text: str, dedupe_key: str) -> None:
    retry_key = str(uuid.uuid5(_RETRY_NAMESPACE, dedupe_key))
    await asyncio.to_thread(_push_sync, conversation_id, text, retry_key)