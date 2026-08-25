from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
)

Identifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
CountryCode = Annotated[
    str,
    BeforeValidator(lambda value: value.strip().upper() if isinstance(value, str) else value),
    StringConstraints(pattern=r"^[A-Z]{2}$"),
]
CurrencyCode = Annotated[
    str,
    BeforeValidator(lambda value: value.strip().upper() if isinstance(value, str) else value),
    StringConstraints(pattern=r"^[A-Z]{3}$"),
]
SourceText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
SourceAnnotation = dict[str, JsonValue]


def _require_aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a UTC offset")
    return value.astimezone(timezone.utc)


def _require_plausible_dob(value: date) -> date:
    if value >= date.today():
        raise ValueError("date of birth must be in the past")
    if value.year < 1900:
        raise ValueError("date of birth is outside the supported range")
    return value


AwareDateTime = Annotated[datetime, AfterValidator(_require_aware_datetime)]
DateOfBirth = Annotated[date, AfterValidator(_require_plausible_dob)]


class StrictRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Account(StrictRecord):
    account_id: Identifier = Field(description="Unique account identifier")
    customer_id: Identifier = Field(description="Associated customer identifier")
    country: CountryCode = Field(description="ISO 3166-1 alpha-2 account country")
    opened_at: AwareDateTime = Field(description="Timezone-aware account opening timestamp")
    account_type: Literal[
        "current",
        "savings",
        "business",
        "offshore",
        "private_banking",
        "corporate",
        "checking",
        "trust",
    ] = Field(description="Type of account")
    balance: Decimal | None = Field(default=None, ge=0, max_digits=20, decimal_places=4)
    currency: CurrencyCode | None = None
    status: Literal["active", "frozen", "monitored", "restricted"] | None = None
    risk_rating: Literal["low", "medium", "high", "critical"] | None = Field(
        default=None,
        description="Upstream account risk classification; retained as source evidence",
    )


class Customer(StrictRecord):
    customer_id: Identifier = Field(description="Unique customer identifier")
    full_name: str = Field(min_length=1, max_length=200, description="Customer full name")
    dob: DateOfBirth = Field(description="Date of birth")
    kyc_level: Literal["basic", "standard", "enhanced"] = Field(
        description="KYC verification level"
    )
    pep_flag: bool = Field(description="Politically exposed person flag")
    nationality: CountryCode | None = None
    occupation: SourceText | None = None
    income_source: SourceText | None = None
    risk_category: Literal["low", "medium", "high", "critical"] | None = None
    pep_details: SourceAnnotation | None = None
    sanctions_check: SourceAnnotation | None = Field(
        default=None,
        description="Unverified upstream screening annotation; not itself a normalized match",
    )
    adverse_media: SourceAnnotation | None = None
    corporate_structure: SourceAnnotation | None = None
    behavioral_flags: SourceAnnotation | None = None
    geographic_risk: SourceAnnotation | None = None
    technology_exposure: SourceAnnotation | None = None
    scandal_involvement: SourceAnnotation | None = None
    charity_flags: SourceAnnotation | None = None
    export_controls: SourceAnnotation | None = None
    weapons_program: SourceAnnotation | None = None
    terrorist_financing: SourceAnnotation | None = None


class Transaction(StrictRecord):
    txn_id: Identifier = Field(description="Unique transaction identifier")
    account_id: Identifier = Field(description="Associated account identifier")
    timestamp: AwareDateTime = Field(description="Timezone-aware transaction timestamp")
    amount: Decimal = Field(
        gt=0,
        max_digits=20,
        decimal_places=4,
        description="Transaction amount in the stated currency",
    )
    currency: CurrencyCode = Field(description="ISO 4217 currency code")
    counterparty_country: CountryCode = Field(description="ISO 3166-1 alpha-2 counterparty country")
    counterparty_name: SourceText | None = None
    purpose: SourceText | None = None
    transaction_type: Literal["wire_transfer", "cash_deposit"] | None = None
    risk_flags: list[SourceText] | None = Field(
        default=None,
        max_length=50,
        description="Unverified source-system annotations retained as evidence",
    )
