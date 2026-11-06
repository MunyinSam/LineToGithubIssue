# For PVSD Bot (Not ON right now)

import json
import logging

import pika
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    PushMessageRequest,
    TextMessage,
)

from app.core.config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

LINE_TEXT_MAX_LENGTH = 5000
LINE_MAX_MESSAGES_PER_PUSH = 5
CHUNK_LABEL_MARGIN = 20  # room reserved for a "(1/12)\n" prefix on multi-chunk messages


# Split chunk for AI ctx
def chunk_text(text: str, max_length: int = LINE_TEXT_MAX_LENGTH - CHUNK_LABEL_MARGIN) -> list:
    lines = text.split("\n")
    chunks = []
    current = ""
    for line in lines:
        cumulated_line = f"{current}\n{line}" if current else line
        if len(cumulated_line) > max_length:
            if current:
                chunks.append(current)
            current = line
        else:
            current = cumulated_line
    if current:
        chunks.append(current)
    
    if len(chunks) > 1:
        chunks = [f"({i}/{len(chunks)})\n{chunk}" for i, chunk in enumerate(chunks, start=1)]
    return chunks


def send_line_push(group_id: str, text: str) -> None:
    configuration = Configuration(access_token=settings.line_channel_access_token)
    chunks = chunk_text(text)
    with ApiClient(configuration) as api_client:
        messaging_api = MessagingApi(api_client)
        for i in range(0, len(chunks), LINE_MAX_MESSAGES_PER_PUSH):
            batch = chunks[i:i + LINE_MAX_MESSAGES_PER_PUSH] # It will take 5 chunks becuase LINE api limits
            messaging_api.push_message(
                PushMessageRequest(to=group_id, messages=[TextMessage(text=c) for c in batch])
            )


def on_message(channel, method, properties, body):
    try:
        payload = json.loads(body)
        send_line_push(payload["group_id"], payload["text"])
        logger.info("Sent LINE message to %s", payload["group_id"])
    except Exception:
        logger.exception("Failed to process message: %r", body)
    finally:
        channel.basic_ack(delivery_tag=method.delivery_tag)


def main():
    connection = pika.BlockingConnection(pika.URLParameters(settings.rabbitmq_url))
    channel = connection.channel()
    channel.queue_declare(queue=settings.line_queue_name, durable=True)
    channel.basic_qos(prefetch_count=1)
    channel.basic_consume(queue=settings.line_queue_name, on_message_callback=on_message)

    logger.info("Wating for emssages on queue '%s'...", settings.line_queue_name)
    try:
        channel.start_consuming()
    except KeyboardInterrupt:
        channel.stop_consuming()
    finally:
        connection.close()


if __name__ == "__main__":
    main()