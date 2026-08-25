import json
import logging
from collections import Counter
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)


class DataProcessor:
    """Parse a batch and enforce cross-record integrity before publication."""

    def __init__(self, max_batch_records: int = 10_000):
        if max_batch_records < 1:
            raise ValueError("max_batch_records must be positive")
        self.max_batch_records = max_batch_records

    async def process_batch_files(
        self,
        accounts_content: bytes,
        customers_content: bytes,
        transactions_content: bytes,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
        accounts = self._parse_array(accounts_content, "accounts")
        customers = self._parse_array(customers_content, "customers")
        transactions = self._parse_array(transactions_content, "transactions")

        total = len(accounts) + len(customers) + len(transactions)
        if total > self.max_batch_records:
            raise ValueError(f"batch contains {total} records; maximum is {self.max_batch_records}")

        self._require_unique(accounts, "account_id", "accounts")
        self._require_unique(customers, "customer_id", "customers")
        self._require_unique(transactions, "txn_id", "transactions")
        self._validate_relationships(accounts, customers, transactions)

        logger.info(
            "Validated batch with %d accounts, %d customers, and %d transactions",
            len(accounts),
            len(customers),
            len(transactions),
        )
        return accounts, customers, transactions

    @staticmethod
    def _parse_array(content: bytes, label: str) -> List[Dict[str, Any]]:
        try:
            value = json.loads(content.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise ValueError(f"{label} must be UTF-8 encoded") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"{label} contains invalid JSON: {exc.msg}") from exc

        if not isinstance(value, list):
            raise ValueError(f"{label} must be a JSON array")
        if not all(isinstance(record, dict) for record in value):
            raise ValueError(f"every {label} item must be a JSON object")
        return value

    @staticmethod
    def _require_unique(records: List[Dict[str, Any]], key: str, label: str) -> None:
        values = [record.get(key) for record in records]
        missing = [index for index, value in enumerate(values) if not value]
        if missing:
            raise ValueError(f"{label} records missing {key} at indexes {missing[:10]}")

        duplicates = sorted(value for value, count in Counter(values).items() if count > 1)
        if duplicates:
            raise ValueError(f"duplicate {key} values in {label}: {duplicates[:10]}")

    @staticmethod
    def _validate_relationships(
        accounts: List[Dict[str, Any]],
        customers: List[Dict[str, Any]],
        transactions: List[Dict[str, Any]],
    ) -> None:
        customer_ids = {customer.get("customer_id") for customer in customers}
        account_ids = {account.get("account_id") for account in accounts}

        orphan_accounts = sorted(
            account.get("account_id")
            for account in accounts
            if account.get("customer_id") not in customer_ids
        )
        orphan_transactions = sorted(
            transaction.get("txn_id")
            for transaction in transactions
            if transaction.get("account_id") not in account_ids
        )
        errors = []
        if orphan_accounts:
            errors.append(f"accounts with unknown customers: {orphan_accounts[:10]}")
        if orphan_transactions:
            errors.append(f"transactions with unknown accounts: {orphan_transactions[:10]}")
        if errors:
            raise ValueError("; ".join(errors))
