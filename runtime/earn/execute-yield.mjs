// execute-yield.mjs — earn_yield as a self-managing TREASURY: keep a liquid compute buffer so the
// loop's survival tier stays funded (= frontier model), deploy the surplus into the best safe stable
// yield, and REFILL the buffer from yield when frontier inference has burned it down. No human, no
// per-instance babysitting: every Anicca on this repo manages its own liquid-vs-yield balance.
//
// Why a buffer: compute is paid in USDC via x402 from LIQUID balance, and the loop picks its model
// by liquid tier (>$1 funded → frontier). If we deploy ALL liquid to yield, the agent starves its
// own brain down to the cheap tier. So the treasury keeps COMPUTE_RESERVE_USDC liquid (sized above
// the funded threshold), parks the rest in yield, and withdraws from yield to top the buffer back up
// when it runs low. Result: frontier while the agent is solvent, automatic step-down to cheaper
// models only when total wealth is genuinely gone.
//
// Honest: own-capital accrual (external:false, kind:"yield"); every action is a real on-chain tx.
import { createPublicClient, createWalletClient, http, fallback } from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { loadEvmKey } from "./lib/resolve-identity.mjs";
import { base } from "viem/chains";
import fs from "fs";
import { recordDeposit, recordWithdraw } from "./lib/cost-basis.mjs";
import { depositLanded } from "./lib/deposit-guard.mjs";
// depositKind → telemetry netWorth() venue key (so per-source P&L can pair value ↔ cost basis)
const VENUE_KEY = { beefy: "beefy", erc4626: "fluid", aave: "aave" };

// EVERY public Base RPC flakes intermittently (mainnet.base.org timeouts, llamarpc 521, drpc 500) —
// that silently dropped deposits (the loop logged "deploy" but no tx landed). Use a viem fallback
// transport across several RPCs so a single one being down never breaks an earn. BASE_RPC_URL (if set)
// is tried FIRST, then the public pool.
const RPC_LIST = [
  ...(process.env.BASE_RPC_URL ? [process.env.BASE_RPC_URL] : []),
  "https://mainnet.base.org",
  "https://base-rpc.publicnode.com",
  "https://base.drpc.org",
  "https://base.llamarpc.com",
  "https://1rpc.io/base",
  "https://base.meowrpc.com",
];
const TRANSPORT = fallback(RPC_LIST.map((u) => http(u, { timeout: 12_000, retryCount: 2 })));
const RPC = RPC_LIST[0]; // kept for any log/back-compat references
const USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
const AAVE_POOL = "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5";
const AAVE_APY = 0.032;
// Liquid USDC to keep for compute (x402 inference). Sized well above the funded tier ($1) so the
// loop runs a frontier model and has runway between refills.
const RESERVE = Math.round(parseFloat(process.env.COMPUTE_RESERVE_USDC || "5") * 1e6);
const MIN_DEPLOY = Math.round(parseFloat(process.env.YIELD_MIN_DEPLOY_USDC || "1") * 1e6);
const REFILL_AT = Math.round(RESERVE * 0.6); // refill the buffer once liquid drops below 60% of it

function out(o) { process.stdout.write(JSON.stringify(o) + "\n"); }
// #28: env-first (PKVAR indirection / BLOCKRUN_WALLET_KEY — the loop always passes PKVAR) then GATED
// per-instance file resolution. Never read the shared $HOME wallet directly (a foreign spawn would
// then sign with another instance's key). null → caller aborts.
function loadKey() { return loadEvmKey(); }
const erc20 = [
  { name: "approve", type: "function", stateMutability: "nonpayable", inputs: [{ type: "address" }, { type: "uint256" }], outputs: [{ type: "bool" }] },
  { name: "allowance", type: "function", stateMutability: "view", inputs: [{ type: "address" }, { type: "address" }], outputs: [{ type: "uint256" }] },
  { name: "balanceOf", type: "function", stateMutability: "view", inputs: [{ type: "address" }], outputs: [{ type: "uint256" }] },
];
const beefy = [
  { name: "deposit", type: "function", stateMutability: "nonpayable", inputs: [{ type: "uint256" }], outputs: [] },
  { name: "withdraw", type: "function", stateMutability: "nonpayable", inputs: [{ type: "uint256" }], outputs: [] },
  { name: "withdrawAll", type: "function", stateMutability: "nonpayable", inputs: [], outputs: [] },
  { name: "getPricePerFullShare", type: "function", stateMutability: "view", inputs: [], outputs: [{ type: "uint256" }] },
  { name: "balanceOf", type: "function", stateMutability: "view", inputs: [{ type: "address" }], outputs: [{ type: "uint256" }] },
];
const aave = [{ name: "supply", type: "function", stateMutability: "nonpayable", inputs: [{ type: "address" }, { type: "uint256" }, { type: "address" }, { type: "uint16" }], outputs: [] }];
// Fluid fUSDC, ERC4626 (deposit(assets,receiver)), 5.36% — addr/iface from docs/earn-verification-2026-06-18.md:233.
const FLUID = "0xf42f5795D9ac7e9D757dB633D693cD548Cfd9169";
const FLUID_APY = 0.0536;
const erc4626 = [{ name: "deposit", type: "function", stateMutability: "nonpayable", inputs: [{ type: "uint256" }, { type: "address" }], outputs: [{ type: "uint256" }] }];

// Best ACTIVE single-asset USDC Beefy Base vault (want==USDC → direct deposit/withdraw). null on fail.
async function bestBeefy() {
  try {
    const [vaults, apys] = await Promise.all([
      fetch("https://api.beefy.finance/vaults").then((r) => r.json()),
      fetch("https://api.beefy.finance/apy").then((r) => r.json()),
    ]);
    const cands = vaults
      .filter((v) => v.chain === "base" && v.status === "active" && (v.tokenAddress || "").toLowerCase() === USDC.toLowerCase())
      .map((v) => ({ id: v.id, vault: v.earnedTokenAddress, apy: apys[v.id] || 0 }))
      .filter((v) => v.vault && v.apy > 0)
      .sort((a, b) => b.apy - a.apy);
    return cands[0] || null;
  } catch { return null; }
}

async function main() {
  const pk = loadKey();
  if (!pk) return out({ abort: "no wallet key" });
  const acct = privateKeyToAccount(pk);
  const pub = createPublicClient({ chain: base, transport: TRANSPORT });
  const w = createWalletClient({ account: acct, chain: base, transport: TRANSPORT });
  if ((await pub.getBalance({ address: acct.address })) === 0n) return out({ abort: "no ETH for gas", wallet: acct.address });

  const liquid = await pub.readContract({ address: USDC, abi: erc20, functionName: "balanceOf", args: [acct.address] });
  const bf = await bestBeefy();
  const vault = bf?.vault;

  // ---- DEPLOY: liquid above the buffer goes to the best yield ----
  const surplus = liquid - BigInt(RESERVE);
  if (surplus >= BigInt(MIN_DEPLOY)) {
    // AUTO highest-APY, NO opt-in (money trees used by default — Dais 2026-06-21). Beefy ~6.1% #1
    // (proven earner: $3.82→$3.85 withdraw, tx 0x55c71f84) → Fluid 5.36% #2 (clean ERC4626) → Aave 3.2%
    // #3. Selection shape from AEA strategy.py:413/485 (enumerate→gate→best), collapsed to a code-side
    // best-APY pick. The old code defaulted to Aave with Beefy behind an opt-in that was never set.
    let venue, protocol, apy, depositKind;
    if (bf && bf.apy >= FLUID_APY) { venue = vault; protocol = `beefy:${bf.id}`; apy = bf.apy; depositKind = "beefy"; }
    else if (FLUID_APY >= AAVE_APY) { venue = FLUID; protocol = "fluid-fusdc"; apy = FLUID_APY; depositKind = "erc4626"; }
    else { venue = AAVE_POOL; protocol = "aave-v3-base"; apy = AAVE_APY; depositKind = "aave"; }
    // Approve the MAX (uint256) — the universal DeFi pattern (Uniswap/GOAT/every integration). The old
    // approve(surplus) re-approved the EXACT amount every deposit; with auto-nonce + a fallback RPC the
    // approve didn't reliably confirm before the deposit, so allowance < surplus → transferFrom REVERTED
    // (status 0x0, gas burned in a retry storm). A one-time max approve means allowance is always
    // sufficient → no re-approve race → deposits stop reverting.
    const MAX_UINT = (1n << 256n) - 1n;
    let alw = await pub.readContract({ address: USDC, abi: erc20, functionName: "allowance", args: [acct.address, venue] });
    if (alw < surplus) {
      const ah = await w.writeContract({ address: USDC, abi: erc20, functionName: "approve", args: [venue, MAX_UINT] });
      await pub.waitForTransactionReceipt({ hash: ah, confirmations: 1 });
    }
    // Beefy's nested Morpho-Gauntlet deposit uses ~1.37M gas (verified tx 0x0a133483); viem's auto
    // estimate under-provisioned it → out-of-gas → revert (status 0x0). Pin an explicit 2.5M gas so the
    // monster tx never runs out. This is THE Beefy fix — confirmed: with gas:2.5M it lands (shares > 0).
    let tx = depositKind === "beefy"
      ? await w.writeContract({ address: venue, abi: beefy, functionName: "deposit", args: [surplus], gas: 2_500_000n })
      : depositKind === "erc4626"
      ? await w.writeContract({ address: venue, abi: erc4626, functionName: "deposit", args: [surplus, acct.address] })
      : await w.writeContract({ address: venue, abi: aave, functionName: "supply", args: [USDC, surplus, acct.address, 0] });
    let r = await pub.waitForTransactionReceipt({ hash: tx });
    // ANY venue that reverts (Beefy gauntlet OOG, OR Aave v3 supply revert observed 2026-06-22 — S1) falls
    // back to the reliable Fluid ERC4626 (5.36%, verified real deposit tx 0x901047bf): a reverting deposit
    // earns $0, a working Fluid one earns. Fluid itself has no fallback (it IS the reliable floor).
    if (r.status !== "success" && venue !== FLUID) {
      venue = FLUID; protocol = "fluid-fusdc"; apy = FLUID_APY; depositKind = "erc4626";
      const alwF = await pub.readContract({ address: USDC, abi: erc20, functionName: "allowance", args: [acct.address, FLUID] });
      if (alwF < surplus) {
        const ah2 = await w.writeContract({ address: USDC, abi: erc20, functionName: "approve", args: [FLUID, (1n << 256n) - 1n] });
        await pub.waitForTransactionReceipt({ hash: ah2, confirmations: 1 });
      }
      tx = await w.writeContract({ address: FLUID, abi: erc4626, functionName: "deposit", args: [surplus, acct.address] });
      r = await pub.waitForTransactionReceipt({ hash: tx });
    }
    // READ-AFTER-WRITE: "status === success" is NOT proof money moved (a tx can succeed yet deposit
    // nothing → the phantom `morpho $1` in cost-basis.json while on-chain morpho was 0). The ONLY honest
    // proof is that LIQUID USDC strictly dropped by ~the amount we deposited. Record basis ONLY then.
    const liqAfter = await pub.readContract({ address: USDC, abi: erc20, functionName: "balanceOf", args: [acct.address] });
    const landed = depositLanded({ statusOk: r.status === "success", liqBefore: liquid, liqAfter, surplus });
    if (landed) recordDeposit(VENUE_KEY[depositKind], Number(liquid - liqAfter) / 1e6);
    return out({ kind: "yield", action: "deploy", protocol, apy_pct: +(apy * 100).toFixed(2), tx, status: r.status === "success" ? "0x1" : "0x0", landed, phantom: r.status === "success" && !landed, moved_usdc: Number(liquid - liqAfter) / 1e6, deposited_usdc: Number(surplus) / 1e6, reserve_usdc: RESERVE / 1e6, wallet: acct.address });
  }

  // ---- REFILL: liquid below the trigger → pull the position back to liquid to top up the buffer ----
  // Uses Beefy's withdrawAll() (no args) — NOT a computed partial withdraw(shares). The old partial
  // path mixed 6-dec USDC `need` with 18-dec mooShares, producing a wrong share amount that REVERTED
  // (status 0x0) every wake = a recurring gas bleed. withdrawAll exits the whole position in one robust
  // call; the next wake redeploys the surplus above RESERVE, so the buffer is restored cleanly.
  if (liquid < BigInt(REFILL_AT) && vault) {
    const shares = await pub.readContract({ address: vault, abi: beefy, functionName: "balanceOf", args: [acct.address] });
    if (shares > 0n) {
      const tx = await w.writeContract({ address: vault, abi: beefy, functionName: "withdrawAll", args: [] });
      const r = await pub.waitForTransactionReceipt({ hash: tx });
      const liq2 = await pub.readContract({ address: USDC, abi: erc20, functionName: "balanceOf", args: [acct.address] });
      // Withdrawing back to liquid is principal-OUT: reduce the beefy basis by the amount pulled, so the
      // remaining position's value − reduced basis still reflects true unrealised P&L.
      if (r.status === "success") recordWithdraw("beefy", Number(liq2 - liquid) / 1e6);
      return out({ kind: "yield", action: "refill", protocol: `beefy:${bf.id}`, tx, status: r.status === "success" ? "0x1" : "0x0", refilled_usdc: Number(liq2 - liquid) / 1e6, reserve_usdc: RESERVE / 1e6, wallet: acct.address });
    }
  }

  // ---- HOLD: buffer is healthy, surplus too small to deploy. Positions keep accruing. ----
  out({ kind: "yield_hold", action: "hold", liquid_usdc: Number(liquid) / 1e6, reserve_usdc: RESERVE / 1e6, note: "buffer healthy" });
}
main().catch((e) => out({ error: String(e?.message || e) }));
