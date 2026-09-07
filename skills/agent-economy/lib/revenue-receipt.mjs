// Provider-neutral, append-only revenue receipt contract.
//
// A receipt is evidence, not a claim: the proof is either a provider's immutable receipt id or
// an on-chain tuple (chain_id, tx_hash, log_index).  This module deliberately does not call a
// provider/RPC; callers must pass the read-back evidence they already verified.  The EVM-specific
// read-back verifier lives in skills/_shared/lib/verify-tx.mjs.

import { createHash } from "node:crypto";

export const REVENUE_RECEIPT_SCHEMA_VERSION = 2;
export const REVENUE_RECEIPT_KIND = "revenue_receipt";

const ADDRESS_RE = /^0x[0-9a-fA-F]{40}$/;
const TX_RE = /^0x[0-9a-fA-F]{64}$/;
const MAX_DECIMAL_PLACES = 18;
const MAX_SAFE_UNITS = BigInt(Number.MAX_SAFE_INTEGER) * (10n ** BigInt(MAX_DECIMAL_PLACES));

const TERMINAL_STATES = new Set([
  "settled", "paid", "received", "completed", "refunded", "charged_back", "chargeback",
  "reversed", "failed", "cancelled", "canceled", "voided", "expired", "rejected",
]);

/** Typed, non-secret validation failure for malformed receipt input. */
export class RevenueReceiptValidationError extends TypeError {
  constructor(code, message, field = undefined) {
    super(`revenue-receipt ${code}: ${message}`);
    this.name = "RevenueReceiptValidationError";
    this.code = code;
    if (field !== undefined) this.field = field;
  }
}

function fail(code, message, field) {
  throw new RevenueReceiptValidationError(code, message, field);
}

function valueOf(input, keys, fallback = undefined) {
  for (const key of keys) {
    if (input && input[key] !== undefined && input[key] !== null) return input[key];
  }
  return fallback;
}

function nonEmptyText(value, field, max = 512) {
  if (typeof value !== "string" && typeof value !== "number") fail("MISSING_FIELD", `${field} is required`, field);
  const text = String(value).trim();
  if (!text) fail("MISSING_FIELD", `${field} is required`, field);
  if (text.length > max) fail("INVALID_FIELD", `${field} is too long`, field);
  return text;
}

function normalizeIdentity(value, field) {
  const text = nonEmptyText(value, field);
  return ADDRESS_RE.test(text) ? text.toLowerCase() : text;
}

function decimal(value, field, { allowZero = true, allowNegative = false } = {}) {
  let raw;
  if (typeof value === "bigint") raw = value.toString();
  else if (typeof value === "number") {
    if (!Number.isFinite(value)) fail("INVALID_AMOUNT", `${field} must be finite`, field);
    raw = String(value);
  } else if (typeof value === "string") raw = value.trim();
  else fail("INVALID_AMOUNT", `${field} must be a decimal amount`, field);

  const sign = allowNegative && raw.startsWith("-") ? -1 : 1;
  const unsigned = sign < 0 ? raw.slice(1) : raw;
  if (!/^\d+(?:\.\d+)?$/.test(unsigned)) fail("INVALID_AMOUNT", `${field} must be a ${allowNegative ? "decimal" : "non-negative decimal"}`, field);
  const [whole, fraction = ""] = unsigned.split(".");
  if (fraction.length > MAX_DECIMAL_PLACES) fail("INVALID_AMOUNT", `${field} has too many decimal places`, field);
  const digits = `${whole}${fraction}`.replace(/^0+(?=\d)/, "") || "0";
  const units = BigInt(digits) * BigInt(sign);
  if (!allowZero && units === 0n) fail("INVALID_AMOUNT", `${field} must be greater than zero`, field);
  if ((units < 0n ? -units : units) > MAX_SAFE_UNITS) fail("INVALID_AMOUNT", `${field} is outside the supported range`, field);
  return { units, scale: fraction.length };
}

function alignDecimal(a, b) {
  const scale = Math.max(a.scale, b.scale);
  return { a: a.units * (10n ** BigInt(scale - a.scale)), b: b.units * (10n ** BigInt(scale - b.scale)), scale };
}

function decimalNumber(value, scale) {
  const number = Number(value) / (10 ** scale);
  if (!Number.isFinite(number)) fail("INVALID_AMOUNT", "amount is outside the supported range");
  return number;
}

function decimalTextFromUnits(value, scale) {
  const negative = value < 0n;
  const digits = (negative ? -value : value).toString().padStart(scale + 1, "0");
  if (scale === 0) return `${negative ? "-" : ""}${digits}`;
  const split = digits.length - scale;
  const fraction = digits.slice(split).replace(/0+$/, "");
  return `${negative ? "-" : ""}${digits.slice(0, split)}${fraction ? `.${fraction}` : ""}`;
}

function normalizeChainId(value) {
  if (typeof value === "number") {
    if (!Number.isInteger(value) || value <= 0) fail("INVALID_PROOF", "chain_id must be a positive integer", "proof.chain_id");
    return value;
  }
  if (typeof value === "bigint") {
    if (value <= 0n || value > BigInt(Number.MAX_SAFE_INTEGER)) fail("INVALID_PROOF", "chain_id is outside the supported range", "proof.chain_id");
    return Number(value);
  }
  const text = nonEmptyText(value, "proof.chain_id", 64);
  if (["base", "base-mainnet", "base_mainnet"].includes(text.toLowerCase())) return 8453;
  if (["base-sepolia", "base_sepolia"].includes(text.toLowerCase())) return 84532;
  try {
    const parsed = /^0x/i.test(text) ? BigInt(text) : BigInt(text);
    if (parsed <= 0n || parsed > BigInt(Number.MAX_SAFE_INTEGER)) throw new Error("range");
    return Number(parsed);
  } catch {
    fail("INVALID_PROOF", "chain_id must be a positive integer", "proof.chain_id");
  }
}

function normalizeProof(input) {
  const candidate = valueOf(input, ["proof", "chain_provider_proof", "chainProviderProof", "provider_proof", "providerProof"]);
  const providerReceiptId = valueOf(input, ["provider_receipt_id", "providerReceiptId", "receipt_id", "receiptId"])
    ?? (candidate && typeof candidate === "object"
      ? valueOf(candidate, ["provider_receipt_id", "providerReceiptId", "receipt_id", "receiptId"])
      : typeof candidate === "string" ? candidate : undefined);

  if (providerReceiptId !== undefined && providerReceiptId !== null) {
    if (!candidate || typeof candidate !== "object" || Array.isArray(candidate) || candidate.verified !== true) {
      fail("UNVERIFIED_PROOF", "provider receipt proof requires explicit verified:true", "proof.verified");
    }
    const id = nonEmptyText(providerReceiptId, "proof.provider_receipt_id", 512);
    return { provider_receipt_id: id, verified: true };
  }

  if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) {
    fail("MISSING_PROOF", "proof must identify a provider receipt or chain transfer", "proof");
  }
  const chainId = valueOf(candidate, ["chain_id", "chainId"]);
  const txHash = valueOf(candidate, ["tx_hash", "txHash", "transaction_hash", "transactionHash"]);
  const logIndex = valueOf(candidate, ["log_index", "logIndex"]);
  if (chainId === undefined || txHash === undefined || logIndex === undefined) {
    fail("MISSING_PROOF", "chain proof requires chain_id, tx_hash, and log_index", "proof");
  }
  if (candidate.verified !== true) fail("UNVERIFIED_PROOF", "chain proof requires explicit verified:true", "proof.verified");
  const normalizedHash = nonEmptyText(txHash, "proof.tx_hash", 128).toLowerCase();
  if (!TX_RE.test(normalizedHash)) fail("INVALID_PROOF", "tx_hash must be a 32-byte EVM hash", "proof.tx_hash");
  let normalizedIndex;
  if (typeof logIndex === "number") normalizedIndex = logIndex;
  else {
    const text = nonEmptyText(logIndex, "proof.log_index", 64);
    try { normalizedIndex = /^0x/i.test(text) ? Number(BigInt(text)) : Number(BigInt(text)); } catch { normalizedIndex = NaN; }
  }
  if (!Number.isSafeInteger(normalizedIndex) || normalizedIndex < 0) {
    fail("INVALID_PROOF", "log_index must be a non-negative integer", "proof.log_index");
  }
  return { chain_id: normalizeChainId(chainId), tx_hash: normalizedHash, log_index: normalizedIndex, verified: true };
}

function normalizeAsset(value) {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    value = valueOf(value, ["symbol", "code", "asset", "name"]);
  }
  const asset = nonEmptyText(value, "asset", 128);
  return asset.toUpperCase();
}

function normalizeOccurredAt(value) {
  if (typeof value === "number") {
    if (!Number.isFinite(value)) fail("INVALID_FIELD", "occurred_at must be a timestamp", "occurred_at");
    const ms = value > 1e12 ? value : value * 1e3;
    const date = new Date(ms);
    if (!Number.isFinite(date.getTime())) fail("INVALID_FIELD", "occurred_at must be a timestamp", "occurred_at");
    return date.toISOString();
  }
  const text = nonEmptyText(value, "occurred_at", 128);
  const date = new Date(text);
  if (!Number.isFinite(date.getTime())) fail("INVALID_FIELD", "occurred_at must be a timestamp", "occurred_at");
  return date.toISOString();
}

function normalizeTerminalState(value) {
  let state = nonEmptyText(value, "terminal_state", 64).toLowerCase().replace(/[ -]+/g, "_");
  if (state === "0x1" || state === "success" || state === "succeeded") state = "settled";
  if (!TERMINAL_STATES.has(state)) fail("NON_TERMINAL", `terminal_state ${JSON.stringify(state)} is not terminal`, "terminal_state");
  return state;
}

function canonicalValue(value) {
  if (Array.isArray(value)) return value.map(canonicalValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonicalValue(value[key])]));
  }
  return value;
}

/**
 * Derive the one stable key used by the append-only journal.  Amount changes cannot turn one chain
 * proof into two positive rows; a refund must use its own provider/chain proof and is therefore a
 * distinct correction receipt.
 */
export function canonicalRevenueReceiptKey(receipt) {
  if (!receipt || typeof receipt !== "object") fail("INVALID_RECEIPT", "receipt must be an object");
  if (receipt.schema_version !== undefined && Number(receipt.schema_version) !== REVENUE_RECEIPT_SCHEMA_VERSION) {
    fail("UNSUPPORTED_VERSION", `schema_version ${JSON.stringify(receipt.schema_version)} is not supported`, "schema_version");
  }
  const proof = normalizeProof(receipt);
  // Idempotency is the provider/chain proof identity only.  Metadata changes cannot mint a second
  // row, while a genuinely distinct transfer log or provider receipt remains distinct.
  const identity = proof.provider_receipt_id !== undefined
    ? {
      provider: nonEmptyText(valueOf(receipt, ["provider"]), "provider").toLowerCase(),
      provider_receipt_id: proof.provider_receipt_id,
    }
    : { chain_id: proof.chain_id, tx_hash: proof.tx_hash, log_index: proof.log_index };
  const material = JSON.stringify(canonicalValue(identity));
  return `revenue:v2:${createHash("sha256").update(material, "utf8").digest("hex")}`;
}

function deepFreeze(value) {
  if (!value || typeof value !== "object" || Object.isFrozen(value)) return value;
  for (const nested of Object.values(value)) deepFreeze(nested);
  return Object.freeze(value);
}

/** Normalize and validate one provider/chain receipt into the immutable v1 contract. */
export function normalizeRevenueReceipt(input, options = {}) {
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    fail("INVALID_RECEIPT", "receipt must be an object");
  }
  if (input.schema_version !== undefined && Number(input.schema_version) !== REVENUE_RECEIPT_SCHEMA_VERSION) {
    fail("UNSUPPORTED_VERSION", `schema_version ${JSON.stringify(input.schema_version)} is not supported`, "schema_version");
  }
  const provider = normalizeIdentity(valueOf(input, ["provider"]), "provider").toLowerCase();
  const payer = normalizeIdentity(valueOf(input, ["payer", "external_payer", "externalPayer"]), "payer");
  const recipient = normalizeIdentity(valueOf(input, ["recipient"]), "recipient");
  const configuredSelfPayers = options.selfPayers ?? options.selfWallets ?? options.ownWallets;
  const selfPayerList = configuredSelfPayers == null
    ? [] : Array.isArray(configuredSelfPayers) ? configuredSelfPayers : [configuredSelfPayers];
  if (selfPayerList.some((value) => String(value).trim().toLowerCase() === payer.toLowerCase())) {
    fail("SELF_PAYMENT", "payer is an instance-controlled wallet", "payer");
  }
  const gross = decimal(valueOf(input, ["gross_decimal", "gross", "gross_amount", "grossAmount"]), "gross");
  const fee = decimal(valueOf(input, ["fee_decimal", "fee", "fees", "fee_amount", "feeAmount"], 0), "fee");
  const refund = decimal(valueOf(input, ["refund_decimal", "refund", "refunds", "refund_amount", "refundAmount"], 0), "refund");
  const grossFee = alignDecimal(gross, fee);
  const grossRefund = alignDecimal({ units: grossFee.a, scale: grossFee.scale }, refund);
  const feeAtScale = grossFee.b * (10n ** BigInt(grossRefund.scale - grossFee.scale));
  const signedUnits = grossRefund.a - feeAtScale - grossRefund.b;
  if (gross.units === 0n && fee.units === 0n && refund.units === 0n) {
    fail("EMPTY_AMOUNT", "at least one amount must be non-zero");
  }
  const signedNet = decimalNumber(signedUnits, grossRefund.scale);
  const providedNet = valueOf(input, ["signed_net_decimal", "signed_net", "signedNet", "net", "net_usdc"]);
  if (providedNet !== undefined && providedNet !== null) {
    const parsedProvided = decimal(providedNet, "signed_net", { allowNegative: true });
    const expected = { units: signedUnits, scale: grossRefund.scale };
    const aligned = alignDecimal(parsedProvided, expected);
    if (aligned.a !== aligned.b) fail("ARITHMETIC_MISMATCH", "signed_net must equal gross - fee - refund", "signed_net");
  }
  const asset = normalizeAsset(valueOf(input, ["asset"]));
  const proof = normalizeProof(input);
  const terminalState = normalizeTerminalState(valueOf(input, ["terminal_state", "terminalState", "settlement_state", "settlementState", "status"]));
  const occurredAt = normalizeOccurredAt(valueOf(input, ["occurred_at", "occurredAt", "timestamp", "ts"]));
  const receipt = {
    schema_version: REVENUE_RECEIPT_SCHEMA_VERSION,
    kind: REVENUE_RECEIPT_KIND,
    provider,
    payer,
    recipient,
    gross: decimalNumber(gross.units, gross.scale),
    fee: decimalNumber(fee.units, fee.scale),
    refund: decimalNumber(refund.units, refund.scale),
    signed_net: signedNet,
    gross_decimal: decimalTextFromUnits(gross.units, gross.scale),
    fee_decimal: decimalTextFromUnits(fee.units, fee.scale),
    refund_decimal: decimalTextFromUnits(refund.units, refund.scale),
    signed_net_decimal: decimalTextFromUnits(signedUnits, grossRefund.scale),
    asset,
    proof,
    terminal_state: terminalState,
    occurred_at: occurredAt,
    idempotency_key: "",
  };
  receipt.idempotency_key = canonicalRevenueReceiptKey(receipt);
  const suppliedKey = valueOf(input, ["idempotency_key", "idempotencyKey"]);
  if (suppliedKey !== undefined && suppliedKey !== null && String(suppliedKey).trim() !== receipt.idempotency_key) {
    fail("INVALID_IDEMPOTENCY_KEY", "idempotency_key does not match the canonical receipt identity", "idempotency_key");
  }

  if (options && options.freeze === false) return receipt;
  return deepFreeze(receipt);
}

export function isTerminalRevenueState(value) {
  return typeof value === "string" && TERMINAL_STATES.has(value.toLowerCase().replace(/[ -]+/g, "_"));
}

export function isNormalizedRevenueReceipt(value) {
  if (!value || typeof value !== "object" || value.schema_version !== REVENUE_RECEIPT_SCHEMA_VERSION || value.kind !== REVENUE_RECEIPT_KIND) return false;
  try {
    const normalized = normalizeRevenueReceipt(value);
    return value.idempotency_key === normalized.idempotency_key
      && value.signed_net === normalized.signed_net
      && isTerminalRevenueState(value.terminal_state);
  } catch {
    return false;
  }
}
