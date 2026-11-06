import asyncio
import json
import logging

import pika

from app.core.config import settings

logger = logging.getLogger(__name__)


def publish(queue_name: str, payload: dict) -> None:
    """ RabbitMQ Publisher """
    # publish volume here is one message per @bot trigger

    # Open TCP Connection to RabbitMQ 
    # Pika is a sync client
    connection = pika.BlockingConnection(pika.URLParameters(settings.rabbitmq_url))
    try:
        channel = connection.channel()
        # Create the Queue if does not exist -> To survive restart
        channel.queue_declare(queue=queue_name, durable=True)
        channel.basic_publish(
            exchange="",
            routing_key=queue_name,
            body=json.dumps(payload).encode('utf-8'), # Pika requires raw byte
            # delivery_mode=2 -> make the msg persistent. RabbitMQ writes to disk immediately
            # content_type = json because the consumers need it.
            properties=pika.BasicProperties(delivery_mode=2, content_type="application/json")
        )
    finally:
        connection.close()


async def publish_async(queue_name: str, payload: dict) -> None:
    await asyncio.to_thread(publish, queue_name, payload)