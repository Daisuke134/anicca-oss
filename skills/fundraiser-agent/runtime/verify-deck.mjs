#!/usr/bin/env node

import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { contextDigest } from "../../../scripts/startup-context/lib.mjs";

export async function verifyDeckArtifact({ contextPath, assetsPath, receiptPath, pdfPath }) {
  const [context, assets, receipt, pdf] = await Promise.all([
    readFile(contextPath, "utf8").then(JSON.parse),
    readFile(assetsPath, "utf8").then(JSON.parse),
    readFile(receiptPath, "utf8").then(JSON.parse),
    readFile(pdfPath),
  ]);
  const digest = contextDigest(context);
  const sha256 = createHash("sha256").update(pdf).digest("hex");
  const pdfText = pdf.toString("latin1");
  const errors = [];

  if (assets.context_version !== context.context_version) errors.push("assets context version is stale");
  if (receipt.context_version !== context.context_version) errors.push("receipt context version is stale");
  if (assets.context_digest !== digest) errors.push("assets context digest is stale");
  if (receipt.context_digest !== digest) errors.push("receipt context digest is stale");
  if (receipt.pdf_sha256 !== sha256) errors.push("deck PDF SHA-256 mismatch");
  if (!pdfText.startsWith("%PDF-1.4")) errors.push("deck is not a supported PDF");
  if (!/\/Type \/Pages \/Kids \[[^\]]+\] \/Count 10 >>/.test(pdfText)) {
    errors.push("deck must contain exactly 10 pages");
  }
  if (!pdfText.endsWith("%%EOF\n")) errors.push("deck PDF is truncated");
  if (errors.length > 0) throw new Error(errors.join("; "));

  return { context_version: context.context_version, context_digest: digest, pdf_sha256: sha256, pages: 10 };
}

async function main() {
  const [contextPath, assetsPath, receiptPath, pdfPath] = process.argv.slice(2);
  if (![contextPath, assetsPath, receiptPath, pdfPath].every(Boolean)) {
    throw new Error("usage: verify-deck.mjs <context.json> <assets.json> <receipt.json> <deck.pdf>");
  }
  process.stdout.write(`${JSON.stringify(await verifyDeckArtifact({
    contextPath: resolve(contextPath),
    assetsPath: resolve(assetsPath),
    receiptPath: resolve(receiptPath),
    pdfPath: resolve(pdfPath),
  }))}\n`);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  await main();
}
