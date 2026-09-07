"""Small, typed evidence boundary shared by HTTP and event processing.

Source annotations and personal names deliberately do not enter the feature store.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


def aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a UTC offset")
    return value.astimezone(timezone.utc)


Timestamp = Annotated[datetime, AfterValidator(aware)]
Identifier = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
]
Money = Annotated[Decimal, Field(gt=0, max_digits=20, decimal_places=4)]


class FeatureTransaction(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False, str_strip_whitespace=True)

    txn_id: Identifier
    account_id: Identifier
    timestamp: Timestamp
    amount: Money
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    counterparty_country: str = Field(pattern=r"^[A-Z]{2}$")
    base_currency_amount: Money | None = None
    base_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    counterparty_account_id: Identifier | None = None
    direction: Literal["inbound", "outbound"] | None = None

    @model_validator(mode="after")
    def conversion_pair(self):
        if (self.base_currency is None) != (self.base_currency_amount is None):
            raise ValueError("base_currency and base_currency_amount must be supplied together")
        if self.base_currency == self.currency and self.base_currency_amount != self.amount:
            raise ValueError("same-currency converted amount must equal amount")
        return self

    def canonical(self) -> dict:
        value = self.model_dump(mode="json", exclude_none=True)
        # Decimal equality is also replay equality ("100.00" == "100").
        for key in ("amount", "base_currency_amount"):
            if key in value:
                value[key] = format(Decimal(value[key]).normalize(), "f")
        return value


class ComputeFeaturesRequest(FeatureTransaction):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)


class FeatureCustomer(BaseModel):
    customer_id: Identifier
    kyc_level: Literal["basic", "standard", "enhanced"]
    pep_flag: bool


class FeatureAccount(BaseModel):
    account_id: Identifier
    customer_id: Identifier
    opened_at: Timestamp
