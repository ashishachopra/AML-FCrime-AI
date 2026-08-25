import json
import logging
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict

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


async def publish_event(
    exchange: aio_pika.Exchange, event_type: str, data: Dict[str, Any], batch_id: str = None
):
    """
    Publish a CloudEvent-style message to RabbitMQ

    Args:
        exchange: RabbitMQ exchange to publish to
        event_type: Type of event (e.g., "IngestedTransaction")
        data: Event payload data
        batch_id: Optional batch identifier
    """

    event = {
        "specversion": "1.0",
        "type": event_type,
        "source": "aml.ingestion",
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
            headers={"event_type": event_type, "source": "aml.ingestion"},
        )

        await exchange.publish(message, routing_key="", mandatory=True)
        logger.info(f"Published {event_type} event with ID {event['id']}")

    except Exception as e:
        logger.error(f"Failed to publish event {event_type}: {e}")
        raise
