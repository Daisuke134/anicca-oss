"use strict";

const crypto = require("node:crypto");
const zlib = require("node:zlib");

const PRODUCT_ID = /^[a-z0-9]+(?:-[a-z0-9]+)*$/u;
const PNG_SIGNATURE = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);

function requireValue(condition, message) {
  if (!condition) throw new Error(message);
}

function crc32(buffer) {
  let crc = 0xffffffff;
  for (const byte of buffer) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit += 1) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0);
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function chunk(type, data) {
  const name = Buffer.from(type, "ascii");
  const length = Buffer.alloc(4);
  length.writeUInt32BE(data.length);
  const checksum = Buffer.alloc(4);
  checksum.writeUInt32BE(crc32(Buffer.concat([name, data])));
  return Buffer.concat([length, name, data, checksum]);
}

function png(width, height, pixel) {
  const raw = Buffer.alloc((width * 3 + 1) * height);
  for (let y = 0; y < height; y += 1) {
    const row = y * (width * 3 + 1);
    for (let x = 0; x < width; x += 1) {
      const [red, green, blue] = pixel(x, y);
      const offset = row + 1 + x * 3;
      raw[offset] = red;
      raw[offset + 1] = green;
      raw[offset + 2] = blue;
    }
  }
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width, 0);
  header.writeUInt32BE(height, 4);
  header.set([8, 2, 0, 0, 0], 8);
  return Buffer.concat([
    PNG_SIGNATURE,
    chunk("IHDR", header),
    chunk("IDAT", zlib.deflateSync(raw, { level: 9 })),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

function palette(productId) {
  const seed = crypto.createHash("sha256").update(productId).digest();
  return {
    base: [40 + seed[0] % 120, 35 + seed[1] % 120, 55 + seed[2] % 120],
    accent: [120 + seed[3] % 120, 120 + seed[4] % 120, 120 + seed[5] % 120],
    phase: seed[6] / 255,
  };
}

function iconBytes(productId) {
  const colors = palette(productId);
  return png(1024, 1024, (x, y) => {
    const gradient = Math.floor(45 * (x + y) / 2048);
    const dx = x - (360 + Math.floor(colors.phase * 300));
    const dy = y - 512;
    const inside = dx * dx + dy * dy < 235 * 235;
    const source = inside ? colors.accent : colors.base;
    return source.map((value) => Math.min(255, value + gradient));
  });
}

function screenshotBytes(productId) {
  const colors = palette(productId);
  return png(1290, 2796, (x, y) => {
    const margin = 105;
    const header = y > 210 && y < 880 && x > margin && x < 1290 - margin;
    const cardOne = y > 1040 && y < 1620 && x > margin && x < 1290 - margin;
    const cardTwo = y > 1750 && y < 2330 && x > margin && x < 1290 - margin;
    const stripe = Math.abs(x - (250 + Math.floor(colors.phase * 790))) < 42;
    if (header && stripe) return colors.accent;
    if (header || cardOne || cardTwo) return [242, 244, 248];
    const gradient = Math.floor(28 * y / 2796);
    return colors.base.map((value) => Math.min(225, value + gradient));
  });
}

function sha256(content) {
  return crypto.createHash("sha256").update(content).digest("hex");
}

function generateMobileProductAssets({ productId, displayName }) {
  requireValue(PRODUCT_ID.test(String(productId || "")), "productId invalid");
  requireValue(typeof displayName === "string" && displayName.trim().length > 0,
    "displayName invalid");
  requireValue(!/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]/u.test(displayName),
    "displayName contains unsupported control characters");
  const icon = iconBytes(productId);
  const screenshot = screenshotBytes(productId);
  const iconPath = "Resources/Assets.xcassets/AppIcon.appiconset/AppIcon-1024.png";
  const screenshotPath = "Marketing/Screenshots/iphone-6.7/01-overview.png";
  const contents = Buffer.from(`${JSON.stringify({
    images: [{ filename: "AppIcon-1024.png", idiom: "universal", platform: "ios", size: "1024x1024" }],
    info: { author: "life-manager", version: 1 },
  }, null, 2)}\n`);
  const metadata = Buffer.from(`${JSON.stringify({
    schema_version: "mobile.product-assets.v1",
    product_id: productId,
    display_name: displayName.trim(),
    generator: "mobile-product-assets.v1",
    assets: [
      { path: iconPath, width: 1024, height: 1024, sha256: sha256(icon) },
      { path: screenshotPath, width: 1290, height: 2796, sha256: sha256(screenshot) },
    ],
  }, null, 2)}\n`);
  return [
    { relative: iconPath, content: icon },
    { relative: "Resources/Assets.xcassets/AppIcon.appiconset/Contents.json", content: contents },
    { relative: screenshotPath, content: screenshot },
    { relative: "Marketing/product-assets.json", content: metadata },
  ];
}

module.exports = { generateMobileProductAssets };
