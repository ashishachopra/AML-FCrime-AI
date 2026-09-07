import asyncio
import json
import logging
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Dict

import aio_pika

logger = logging.getLogger(__name__)


class DateTimeEncoder(json.JSONEncoder):
    """Custom JSON encoder for datetime objects"""

    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        elif isinstance(obj, date):
            return obj.isoformat()
        elif isinstance(obj, Decimal):
            return str(obj)
        return super().default(obj)


async def publish_envelope(exchange: aio_pika.Exchange, event: dict):
    """Publish an already persisted envelope without changing its identity."""
    message = aio_pika.Message(
        json.dumps(event, cls=DateTimeEncoder, allow_nan=False).encode(),
        content_type="application/json",
        content_encoding="utf-8",
        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        message_id=event["id"],
        correlation_id=event.get("batchid"),
        timestamp=datetime.fromisoformat(event["time"].replace("Z", "+00:00")),
        headers={"event_type": event["type"], "source": event["source"]},
    )
    await exchange.publish(message, routing_key="", mandatory=True, timeout=5.0)


async def publish_event(
    exchange: aio_pika.Exchange, event_type: str, data: Dict[str, Any], batch_id: str = None
):
    """
    Publish a CloudEvent-style message to RabbitMQ

    Args:
        exchange: RabbitMQ exchange to publish to
        event_type: Type of event (e.g., "FeaturesReady")
        data: Event payload data
        batch_id: Optional batch identifier
    """

    event = {
        "specversion": "1.0",
        "type": event_type,
        "source": "aml.feature-engine",
        "id": str(uuid.uuid4()),
        "time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "datacontenttype": "application/json",
        "data": data,
    }

    if batch_id:
        event["batchid"] = batch_id

    try:
        message = aio_pika.Message(
            json.dumps(event, cls=DateTimeEncoder).encode(),
            content_type="application/json",
            content_encoding="utf-8",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id=event["id"],
            correlation_id=batch_id,
            timestamp=datetime.now(timezone.utc),
            headers={"event_type": event_type, "source": "aml.feature-engine"},
        )

        await exchange.publish(message, routing_key="", mandatory=True)
        logger.info(f"Published {event_type} event with ID {event['id']}")

    except Exception as e:
        logger.error(f"Failed to publish event {event_type}: {e}")
        raise


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
        "feature-engine-queue.dlq", durable=True, auto_delete=False
    )
    await dead_letter_queue.bind(dead_letter_exchange, routing_key="feature-engine-queue")

    queue = await channel.declare_queue(
        "feature-engine-queue",
        durable=True,
        auto_delete=False,
        arguments={
            "x-dead-letter-exchange": "aml.events.dlx",
            "x-dead-letter-routing-key": "feature-engine-queue",
        },
    )

    # Bind queue to exchange
    await queue.bind(exchange)

    # Preserve entity-before-transaction order within this queue. The handler
    # commits to the local outbox before ack and performs no broker I/O.
    handler_lock = asyncio.Lock()

    async def process_message(message: aio_pika.IncomingMessage):
        try:
            event_data = json.loads(message.body.decode("utf-8"))
            event_type = event_data.get("type")
            if event_type in ["IngestedTransaction", "IngestedCustomer", "IngestedAccount"]:
                async with handler_lock:
                    await event_handler(event_data)
                logger.info("Processed %s event", event_type)
            await message.ack()
        except (ValueError, KeyError, TypeError):
            logger.exception("Rejecting invalid or failed event %s", message.message_id)
            await message.reject(requeue=False)
        except Exception:
            logger.exception(
                "Transient feature processing failure; requeueing %s", message.message_id
            )
            await asyncio.sleep(0.25)
            await message.reject(requeue=True)

    # Start consuming
    await queue.consume(process_message)
    logger.info("Started consuming events from aml.events exchange")
    await asyncio.Future()
