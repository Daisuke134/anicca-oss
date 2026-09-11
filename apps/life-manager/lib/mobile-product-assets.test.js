"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");

const { generateMobileProductAssets } = require("./mobile-product-assets.js");

const signature = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);
const digest = (value) => crypto.createHash("sha256").update(value).digest("hex");

function dimensions(buffer) {
  assert.deepEqual(buffer.subarray(0, 8), signature);
  assert.equal(buffer.subarray(12, 16).toString("ascii"), "IHDR");
  assert.equal(buffer[25], 2, "App Store PNG must be RGB without an alpha channel");
  return [buffer.readUInt32BE(16), buffer.readUInt32BE(20)];
}

test("generates deterministic product-specific App Store assets with verified metadata", () => {
  const first = generateMobileProductAssets({ productId: "focus-bloom", displayName: "Focus Bloom" });
  const replay = generateMobileProductAssets({ productId: "focus-bloom", displayName: "Focus Bloom" });
  assert.deepEqual(first, replay);
  assert.deepEqual(first.map((item) => item.relative), [
    "Resources/Assets.xcassets/AppIcon.appiconset/AppIcon-1024.png",
    "Resources/Assets.xcassets/AppIcon.appiconset/Contents.json",
    "Marketing/Screenshots/iphone-6.7/01-overview.png",
    "Marketing/product-assets.json",
  ]);
  assert.deepEqual(dimensions(first[0].content), [1024, 1024]);
  assert.deepEqual(dimensions(first[2].content), [1290, 2796]);
  const metadata = JSON.parse(first[3].content.toString("utf8"));
  assert.equal(metadata.schema_version, "mobile.product-assets.v1");
  assert.equal(metadata.product_id, "focus-bloom");
  assert.equal(metadata.assets[0].sha256, digest(first[0].content));
  assert.equal(metadata.assets[1].sha256, digest(first[2].content));
  assert.notDeepEqual(
    first[0].content,
    generateMobileProductAssets({ productId: "quiet-mind", displayName: "Quiet Mind" })[0].content,
  );
});

test("rejects invalid product identity before generating assets", () => {
  assert.throws(() => generateMobileProductAssets({ productId: "../escape", displayName: "Escape" }),
    /productId invalid/);
  assert.throws(() => generateMobileProductAssets({ productId: "safe-app", displayName: "Bad\u0001Name" }),
    /control characters/);
});
