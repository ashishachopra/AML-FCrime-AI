import asyncio
import json
import logging
from typing import Any, Callable, Dict

import aio_pika

logger = logging.getLogger(__name__)


async def consume_events(
    channel: aio_pika.Channel,
    exchange: aio_pika.Exchange,
    event_handler: Callable[[Dict[str, Any]], None],
):
    """
    Consume events from RabbitMQ exchange

    Args:
        channel: RabbitMQ channel
        exchange: Exchange to consume from
        event_handler: Function to handle received events
    """

    dead_letter_exchange = await channel.declare_exchange(
        "aml.events.dlx", aio_pika.ExchangeType.DIRECT, durable=True
    )
    dead_letter_queue = await channel.declare_queue(
        "alert-manager-queue.dlq", durable=True, auto_delete=False
    )
    await dead_letter_queue.bind(dead_letter_exchange, routing_key="alert-manager-queue")

    queue = await channel.declare_queue(
        "alert-manager-queue",
        durable=True,
        auto_delete=False,
        arguments={
            "x-dead-letter-exchange": "aml.events.dlx",
            "x-dead-letter-routing-key": "alert-manager-queue",
        },
    )

    # Bind queue to exchange
    await queue.bind(exchange)

    async def process_message(message: aio_pika.IncomingMessage):
        try:
            event_data = json.loads(message.body.decode("utf-8"))
            event_type = event_data.get("type")
            if event_type == "Scored":
                await event_handler(event_data)
                logger.info("Processed %s event", event_type)
            await message.ack()
        except Exception:
            logger.exception("Rejecting invalid or failed event %s", message.message_id)
            await message.reject(requeue=False)

    # Start consuming
    await queue.consume(process_message)
    logger.info("Started consuming events from aml.events exchange")
    await asyncio.Future()
