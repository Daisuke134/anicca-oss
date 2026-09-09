"""Typed, provider-independent access to the marketplace JSON contracts.

The schemas in the sibling ``schemas`` directory are the wire contract.  This
module deliberately keeps provider/model code out of that contract: callers
load a schema, route by its record type, validate a copy of the input, and get
one of the immutable record models back.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from functools import lru_cache
import json
from pathlib import Path
import re
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple, Type, Union

from jsonschema import Draft202012Validator, FormatChecker, validators


SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"

SCHEMA_FILES = {
    "opportunity": "opportunity.schema.json",
    "payment_receipt": "payment.schema.json",
    "event": "event.schema.json",
}

_SCHEMA_ALIASES = {
    "opportunity": "opportunity.schema.json",
    "opportunity.schema.json": "opportunity.schema.json",
    "payment": "payment.schema.json",
    "payment_receipt": "payment.schema.json",
    "payment.schema.json": "payment.schema.json",
    "event": "event.schema.json",
    "event.schema.json": "event.schema.json",
}

_RECORD_TYPE_TO_SCHEMA = {
    "opportunity": "opportunity.schema.json",
    "payment_receipt": "payment.schema.json",
    "application_intent": "event.schema.json",
    "application_receipt": "event.schema.json",
    "contract_receipt": "event.schema.json",
    "authorization_receipt": "event.schema.json",
    "qa_receipt": "event.schema.json",
    "payout_match_receipt": "event.schema.json",
    "work_event": "event.schema.json",
    "delivery_intent": "event.schema.json",
    "delivery_receipt": "event.schema.json",
}


@dataclass(frozen=True)
class Opportunity:
    schema_version: int
    record_type: str
    platform: str
    external_id: str
    title: str
    description: str
    url: Optional[str]
    category: str
    budget_type: str
    budget_min_minor: Optional[int]
    budget_max_minor: Optional[int]
    currency: Optional[str]
    buyer_external_id: Optional[str]
    observed_at: str


@dataclass(frozen=True)
class PaymentReceipt:
    schema_version: int
    record_type: str
    platform: str
    work_external_id: str
    payment_external_id: str
    receipt_id: str
    gross_amount_minor: int
    fee_amount_minor: int
    cost_amount_minor: int
    net_amount_minor: int
    currency: str
    status: str
    occurred_at: str
    observed_at: str


@dataclass(frozen=True)
class ApplicationIntent:
    schema_version: int
    record_type: str
    platform: str
    opportunity_external_id: str
    proposal_text: str
    proposed_amount_minor: int
    currency: str
    content_sha256: str
    idempotency_key: str
    created_at: str


@dataclass(frozen=True)
class ApplicationReceipt:
    schema_version: int
    record_type: str
    platform: str
    opportunity_external_id: str
    application_external_id: str
    status: str
    content_sha256: str
    idempotency_key: str
    observed_at: str
    # Optional so every existing lane's receipts stay valid unchanged. Without them a reader can
    # only report a bare id, which is why CrowdWorks reports read "案件: 案件 13422653".
    opportunity_title: str | None = None
    proposed_amount_minor: int | None = None
    pricing_mode: str | None = None
    proposed_hourly_rate_minor: int | None = None
    weekly_limit_hours: int | None = None


@dataclass(frozen=True)
class ContractReceipt:
    schema_version: int
    record_type: str
    platform: str
    application_external_id: str
    work_external_id: str
    contract_external_id: str
    status: str
    terms_sha256: str
    observed_at: str


@dataclass(frozen=True)
class AuthorizationReceipt:
    schema_version: int
    record_type: str
    platform: str
    contract_external_id: str
    authorization_external_id: str
    status: str
    scope_sha256: str
    observed_at: str


@dataclass(frozen=True)
class QAReceipt:
    schema_version: int
    record_type: str
    platform: str
    work_external_id: str
    qa_external_id: str
    status: str
    artifact_sha256: str
    report_sha256: str
    observed_at: str


@dataclass(frozen=True)
class PayoutMatchReceipt:
    schema_version: int
    record_type: str
    platform: str
    payment_external_id: str
    payout_external_id: str
    bank_transaction_external_id: str
    status: str
    amount_minor: int
    currency: str
    observed_at: str


@dataclass(frozen=True)
class WorkEvent:
    schema_version: int
    record_type: str
    platform: str
    event_type: str
    external_id: str
    parent_external_id: Optional[str]
    content_sha256: str
    occurred_at: str
    observed_at: str


@dataclass(frozen=True)
class DeliveryIntent:
    schema_version: int
    record_type: str
    platform: str
    work_external_id: str
    artifact_path: str
    artifact_sha256: str
    message: str
    content_sha256: str
    idempotency_key: str
    created_at: str


@dataclass(frozen=True)
class DeliveryReceipt:
    schema_version: int
    record_type: str
    platform: str
    work_external_id: str
    delivery_external_id: str
    qa_external_id: str
    status: str
    artifact_sha256: str
    idempotency_key: str
    observed_at: str


Contract = Union[
    Opportunity,
    PaymentReceipt,
    ApplicationIntent,
    ApplicationReceipt,
    ContractReceipt,
    AuthorizationReceipt,
    QAReceipt,
    PayoutMatchReceipt,
    WorkEvent,
    DeliveryIntent,
    DeliveryReceipt,
]
ContractRecord = Contract


class ContractValidationError(ValueError):
    """A stable, path-aware validation failure.

    ``errors`` is sorted and immutable so logging, retries, and tests observe
    the same message order regardless of jsonschema's traversal details.
    """

    def __init__(self, errors: Sequence[str]):
        normalized = tuple(sorted(str(error) for error in errors))
        self.errors = normalized
        super().__init__("\n".join(normalized))


_RFC3339_RE = re.compile(
    r"^[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])[Tt]"
    r"(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](?:\.[0-9]+)?"
    r"(?:[Zz]|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])$"
)


def _is_rfc3339(value: object) -> bool:
    """Return whether *value* is a timezone-bearing RFC3339 timestamp."""

    if not isinstance(value, str) or _RFC3339_RE.fullmatch(value) is None:
        return False
    # Normalize the two RFC3339 spellings explicitly before handing the value
    # to Python's ISO parser. Python 3.9 does not accept a lowercase separator
    # consistently across all supported patch releases.
    normalized = value[:10] + "T" + value[11:]
    if normalized[-1] in "Zz":
        normalized = normalized[:-1] + "+00:00"
    try:
        datetime.fromisoformat(normalized)
    except (TypeError, ValueError, OverflowError):
        return False
    return True


_FORMAT_CHECKER = FormatChecker()
_FORMAT_CHECKER.checks("date-time")(_is_rfc3339)


def _is_strict_integer(checker: Any, instance: object) -> bool:
    del checker
    return isinstance(instance, int) and not isinstance(instance, bool)


_STRICT_TYPE_CHECKER = Draft202012Validator.TYPE_CHECKER.redefine(
    "integer", _is_strict_integer
)
_StrictDraft202012Validator = validators.extend(
    Draft202012Validator, type_checker=_STRICT_TYPE_CHECKER
)


def _with_runtime_integer_constraints(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Add strict integer typing to the const-only schema_version definition."""

    properties = schema.get("properties")
    if isinstance(properties, dict) and "schema_version" in properties:
        properties["schema_version"] = deepcopy(properties["schema_version"])
        properties["schema_version"]["type"] = "integer"

    definitions = schema.get("$defs")
    if isinstance(definitions, dict) and "schema_version" in definitions:
        definitions["schema_version"] = deepcopy(definitions["schema_version"])
        definitions["schema_version"]["type"] = "integer"
    return schema


def _canonical_schema_name(name: str) -> str:
    if not isinstance(name, str):
        raise ContractValidationError(("$: schema_name_must_be_string",))
    try:
        return _SCHEMA_ALIASES[name]
    except KeyError:
        raise ContractValidationError(("$: unknown_schema_name",)) from None


@lru_cache(maxsize=None)
def _load_schema_cached(name: str) -> Dict[str, Any]:
    path = SCHEMA_DIR / name
    try:
        with path.open("r", encoding="utf-8") as handle:
            schema = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ContractValidationError(("$: schema_unreadable",)) from exc
    if not isinstance(schema, dict):
        raise ContractValidationError(("$: schema_must_be_object",))
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:  # jsonschema exposes several schema-error classes.
        raise ContractValidationError(("$: schema_invalid",)) from exc
    return schema


def load_schema(name: str) -> Dict[str, Any]:
    """Load a known schema once and return a caller-owned deep copy."""

    canonical = _canonical_schema_name(name)
    return deepcopy(_load_schema_cached(canonical))


# Keep cache introspection available on the public API without exposing the
# mutable object held by the cache.
load_schema.cache_info = _load_schema_cached.cache_info  # type: ignore[attr-defined]
load_schema.cache_clear = _load_schema_cached.cache_clear  # type: ignore[attr-defined]


def schema_name_for_record_type(record_type: object) -> str:
    if not isinstance(record_type, str):
        raise ContractValidationError(("$.record_type: record_type_must_be_string",))
    try:
        return _RECORD_TYPE_TO_SCHEMA[record_type]
    except KeyError:
        raise ContractValidationError(("$.record_type: unknown_record_type",)) from None


def schema_name_for_record(record: Mapping[str, Any]) -> str:
    if not isinstance(record, Mapping):
        raise ContractValidationError(("$: expected_object",))
    return schema_name_for_record_type(record.get("record_type"))


_EVENT_DEFINITION_BY_RECORD_TYPE = {
    "application_intent": "ApplicationIntent",
    "application_receipt": "ApplicationReceipt",
    "contract_receipt": "ContractReceipt",
    "authorization_receipt": "AuthorizationReceipt",
    "qa_receipt": "QAReceipt",
    "payout_match_receipt": "PayoutMatchReceipt",
    "work_event": "WorkEvent",
    "delivery_intent": "DeliveryIntent",
    "delivery_receipt": "DeliveryReceipt",
}


def schema_for_record(record: Mapping[str, Any]) -> Dict[str, Any]:
    """Return the routed schema as a defensive copy."""

    schema_name = schema_name_for_record(record)
    schema = load_schema(schema_name)
    if schema_name == "event.schema.json":
        definition = _EVENT_DEFINITION_BY_RECORD_TYPE[record["record_type"]]
        schema.pop("oneOf", None)
        schema["$ref"] = "#/$defs/{}".format(definition)
    return schema


def validator_for_schema(name: str) -> Draft202012Validator:
    schema = _with_runtime_integer_constraints(load_schema(name))
    return _StrictDraft202012Validator(schema, format_checker=_FORMAT_CHECKER)


def validator_for_record(record: Mapping[str, Any]) -> Draft202012Validator:
    schema = _with_runtime_integer_constraints(schema_for_record(record))
    return _StrictDraft202012Validator(schema, format_checker=_FORMAT_CHECKER)


def _path_for_error(error: Any) -> str:
    path = "$"
    for component in error.absolute_path:
        if isinstance(component, int):
            path += "[{}]".format(component)
        elif isinstance(component, str) and component.isidentifier():
            path += "." + component
        else:
            path += "[{}]".format(repr(component))
    return path


def _required_error_path(error: Any) -> Optional[str]:
    if error.validator != "required":
        return None
    base = _path_for_error(error)
    required = error.validator_value
    instance = error.instance
    if not isinstance(required, (list, tuple)) or not isinstance(instance, Mapping):
        return None
    for field in required:
        if not isinstance(field, str) or field in instance:
            continue
        expected_message = "{!r} is a required property".format(field)
        if error.message == expected_message:
            return base + "." + field
    return None


_VALIDATOR_CODES = {
    "additionalProperties": "additional_property",
    "anyOf": "any_of",
    "const": "const_mismatch",
    "enum": "enum_mismatch",
    "format": "invalid_format",
    "maxItems": "above_max_items",
    "maxLength": "above_max_length",
    "maximum": "above_maximum",
    "minItems": "below_min_items",
    "minLength": "below_min_length",
    "minimum": "below_minimum",
    "oneOf": "one_of",
    "pattern": "pattern_mismatch",
    "required": "required",
    "type": "invalid_type",
}


def _validator_code(error: Any) -> str:
    if error.validator in ("anyOf", "oneOf") and error.context:
        for child in error.context:
            if child.validator != "type":
                return _validator_code(child)
        if all(child.validator == "type" for child in error.context):
            return "invalid_type"
    return _VALIDATOR_CODES.get(str(error.validator), "schema_validation")


def _schema_errors(record: Mapping[str, Any]) -> Tuple[str, ...]:
    validator = validator_for_record(record)
    messages = []
    errors = sorted(
        validator.iter_errors(record),
        key=lambda error: (
            tuple(str(component) for component in error.absolute_path),
            str(error.validator),
            tuple(str(component) for component in error.absolute_schema_path),
        ),
    )
    for error in errors:
        path = _required_error_path(error) or _path_for_error(error)
        messages.append("{}: {}".format(path, _validator_code(error)))
    return tuple(messages)


def _opportunity_semantic_errors(record: Mapping[str, Any]) -> Tuple[str, ...]:
    budget_min = record.get("budget_min_minor")
    budget_max = record.get("budget_max_minor")
    currency = record.get("currency")
    errors = []

    min_is_int = isinstance(budget_min, int) and not isinstance(budget_min, bool)
    max_is_int = isinstance(budget_max, int) and not isinstance(budget_max, bool)
    if min_is_int and max_is_int and budget_min > budget_max:
        errors.append(
            "$.budget_min_minor: budget_min_minor_must_not_exceed_budget_max_minor"
        )
    if (min_is_int or max_is_int) and currency is None:
        errors.append("$.currency: currency_required_when_budget_bound_present")
    return tuple(errors)


def _payment_semantic_errors(record: Mapping[str, Any]) -> Tuple[str, ...]:
    values = [
        record.get("gross_amount_minor"),
        record.get("fee_amount_minor"),
        record.get("cost_amount_minor"),
        record.get("net_amount_minor"),
    ]
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in values):
        return ()
    gross, fee, cost, net = values
    if net != gross - fee - cost:
        return ("$.net_amount_minor: must_equal_gross_minus_fee_and_cost",)
    return ()


def _raise_validation(errors: Sequence[str]) -> None:
    if errors:
        raise ContractValidationError(errors)


def validate_contract(value: Mapping[str, object]) -> None:
    """Validate a record against its routed schema and semantic invariants."""

    if not isinstance(value, Mapping):
        raise ContractValidationError(("$: expected_object",))
    copied = deepcopy(dict(value))
    schema_name_for_record(copied)
    errors = list(_schema_errors(copied))
    if copied.get("record_type") == "opportunity":
        errors.extend(_opportunity_semantic_errors(copied))
    elif copied.get("record_type") == "payment_receipt":
        errors.extend(_payment_semantic_errors(copied))
    _raise_validation(errors)


_MODEL_BY_RECORD_TYPE: Dict[str, Type[Contract]] = {
    "opportunity": Opportunity,
    "payment_receipt": PaymentReceipt,
    "application_intent": ApplicationIntent,
    "application_receipt": ApplicationReceipt,
    "contract_receipt": ContractReceipt,
    "authorization_receipt": AuthorizationReceipt,
    "qa_receipt": QAReceipt,
    "payout_match_receipt": PayoutMatchReceipt,
    "work_event": WorkEvent,
    "delivery_intent": DeliveryIntent,
    "delivery_receipt": DeliveryReceipt,
}


def parse_contract(value: Mapping[str, object]) -> Contract:
    """Validate and parse a wire record into its immutable model."""

    copied = deepcopy(dict(value)) if isinstance(value, Mapping) else value
    validate_contract(copied)
    model = _MODEL_BY_RECORD_TYPE[copied["record_type"]]
    return model(**copied)


def record_to_dict(record: Contract) -> Dict[str, Any]:
    """Serialize a model into an independent wire dictionary."""

    if type(record) not in tuple(_MODEL_BY_RECORD_TYPE.values()) or not is_dataclass(record):
        raise TypeError("record_must_be_marketplace_dataclass")
    return deepcopy(asdict(record))


def parse_opportunity(record: Mapping[str, Any]) -> Opportunity:
    parsed = parse_contract(record)
    if not isinstance(parsed, Opportunity):
        raise ContractValidationError(("$.record_type: expected opportunity",))
    return parsed


def parse_payment_receipt(record: Mapping[str, Any]) -> PaymentReceipt:
    parsed = parse_contract(record)
    if not isinstance(parsed, PaymentReceipt):
        raise ContractValidationError(("$.record_type: expected payment_receipt",))
    return parsed


def parse_application_intent(record: Mapping[str, Any]) -> ApplicationIntent:
    parsed = parse_contract(record)
    if not isinstance(parsed, ApplicationIntent):
        raise ContractValidationError(("$.record_type: expected application_intent",))
    return parsed


def parse_application_receipt(record: Mapping[str, Any]) -> ApplicationReceipt:
    parsed = parse_contract(record)
    if not isinstance(parsed, ApplicationReceipt):
        raise ContractValidationError(("$.record_type: expected application_receipt",))
    return parsed


def parse_contract_receipt(record: Mapping[str, Any]) -> ContractReceipt:
    parsed = parse_contract(record)
    if not isinstance(parsed, ContractReceipt):
        raise ContractValidationError(("$.record_type: expected contract_receipt",))
    return parsed


def parse_authorization_receipt(record: Mapping[str, Any]) -> AuthorizationReceipt:
    parsed = parse_contract(record)
    if not isinstance(parsed, AuthorizationReceipt):
        raise ContractValidationError(("$.record_type: expected authorization_receipt",))
    return parsed


def parse_qa_receipt(record: Mapping[str, Any]) -> QAReceipt:
    parsed = parse_contract(record)
    if not isinstance(parsed, QAReceipt):
        raise ContractValidationError(("$.record_type: expected qa_receipt",))
    return parsed


def parse_payout_match_receipt(record: Mapping[str, Any]) -> PayoutMatchReceipt:
    parsed = parse_contract(record)
    if not isinstance(parsed, PayoutMatchReceipt):
        raise ContractValidationError(("$.record_type: expected payout_match_receipt",))
    return parsed


def validate_receipt_chain(values: Sequence[Mapping[str, object]]) -> Tuple[Contract, ...]:
    """Validate one complete provider-observed application-to-bank chain."""

    records = tuple(parse_contract(value) for value in values)
    expected = (
        ApplicationReceipt,
        ContractReceipt,
        AuthorizationReceipt,
        QAReceipt,
        DeliveryReceipt,
        PaymentReceipt,
        PayoutMatchReceipt,
    )
    if len(records) != len(expected) or any(
        not isinstance(record, record_type)
        for record, record_type in zip(records, expected)
    ):
        raise ContractValidationError(("$: incomplete_or_out_of_order_receipt_chain",))

    application, contract, authorization, qa, delivery, payment, payout = records
    if len({record.platform for record in records}) != 1:
        raise ContractValidationError(("$: receipt platforms do not match",))
    if application.status != "verified" or contract.status != "accepted":
        raise ContractValidationError(("$: application or contract is not accepted",))
    if authorization.status != "authorized" or qa.status != "passed":
        raise ContractValidationError(("$: work is not authorized and QA-passed",))
    if delivery.status != "verified" or payment.status != "settled" or payout.status != "matched":
        raise ContractValidationError(("$: delivery payment or payout is not final",))
    if application.application_external_id != contract.application_external_id:
        raise ContractValidationError(("$: application IDs do not match",))
    if contract.contract_external_id != authorization.contract_external_id:
        raise ContractValidationError(("$: contract IDs do not match",))
    if len({contract.work_external_id, qa.work_external_id, delivery.work_external_id, payment.work_external_id}) != 1:
        raise ContractValidationError(("$: work IDs do not match",))
    if qa.qa_external_id != delivery.qa_external_id or qa.artifact_sha256 != delivery.artifact_sha256:
        raise ContractValidationError(("$: QA and delivery evidence do not match",))
    if payment.payment_external_id != payout.payment_external_id:
        raise ContractValidationError(("$: payment IDs do not match",))
    if payment.net_amount_minor != payout.amount_minor or payment.currency != payout.currency:
        raise ContractValidationError(("$: payout amount or currency does not match net payment",))
    observed = [record.observed_at for record in records]
    if observed != sorted(observed):
        raise ContractValidationError(("$: receipt observation times are out of order",))
    return records


def parse_work_event(record: Mapping[str, Any]) -> WorkEvent:
    parsed = parse_contract(record)
    if not isinstance(parsed, WorkEvent):
        raise ContractValidationError(("$.record_type: expected work_event",))
    return parsed


def parse_delivery_intent(record: Mapping[str, Any]) -> DeliveryIntent:
    parsed = parse_contract(record)
    if not isinstance(parsed, DeliveryIntent):
        raise ContractValidationError(("$.record_type: expected delivery_intent",))
    return parsed


def parse_delivery_receipt(record: Mapping[str, Any]) -> DeliveryReceipt:
    parsed = parse_contract(record)
    if not isinstance(parsed, DeliveryReceipt):
        raise ContractValidationError(("$.record_type: expected delivery_receipt",))
    return parsed


# Small compatibility spellings for callers that describe the same operation
# as ``to_dict``/``parse``. They point at the required implementation above and
# do not add another model or provider-specific contract.
parse = parse_contract
parse_record = parse_contract
validate = validate_contract
validate_record = validate_contract
to_dict = record_to_dict


__all__ = [
    "ApplicationIntent",
    "ApplicationReceipt",
    "AuthorizationReceipt",
    "Contract",
    "ContractRecord",
    "ContractReceipt",
    "ContractValidationError",
    "DeliveryIntent",
    "DeliveryReceipt",
    "Opportunity",
    "PaymentReceipt",
    "PayoutMatchReceipt",
    "QAReceipt",
    "SCHEMA_DIR",
    "SCHEMA_FILES",
    "WorkEvent",
    "load_schema",
    "parse",
    "parse_application_intent",
    "parse_application_receipt",
    "parse_authorization_receipt",
    "parse_delivery_intent",
    "parse_delivery_receipt",
    "parse_opportunity",
    "parse_payment_receipt",
    "parse_payout_match_receipt",
    "parse_qa_receipt",
    "parse_contract",
    "parse_contract_receipt",
    "parse_record",
    "record_to_dict",
    "schema_for_record",
    "schema_name_for_record",
    "schema_name_for_record_type",
    "to_dict",
    "validate",
    "validate_contract",
    "validate_record",
    "validate_receipt_chain",
    "validator_for_record",
    "validator_for_schema",
]
