#!/usr/bin/env node
"use strict";

const path = require("node:path");
const http = require("node:http");
const os = require("node:os");
const dns = require("node:dns");

function requiredEnv(env, name) {
  const value = String(env[name] || "").trim();
  if (!value) throw new Error(`${name} is required`);
  return value;
}

function createHealthServer(port, state) {
  const server = http.createServer((request, response) => {
    if (request.url !== "/health") {
      response.writeHead(404).end("not found");
      return;
    }
    const ok = state.ready === true && state.error == null;
    response.writeHead(ok ? 200 : 503, { "content-type": "application/json" });
    response.end(JSON.stringify({
      ok,
      role: state.role,
      worker_id: state.workerId,
      capabilities: state.capabilities,
      last_poll_at: state.lastPollAt,
    }));
  });
  server.listen(port, "0.0.0.0");
  return server;
}

function createScopedEnvironmentSecretProvider(env = process.env) {
  const { createSecretProvider } = require("../lib/secret-provider.js");
  const tenantId = requiredEnv(env, "LM_RUNTIME_TENANT_ID");
  const bindings = new Map([
    ["secret://telegram/bot-token", "LM_TELEGRAM_BOT_TOKEN"],
    ["secret://postiz/api-key", "LM_POSTIZ_API_KEY"],
  ]);
  return createSecretProvider({
    mode: "local",
    keychain: {
      async get(requestTenantId, ref) {
        if (requestTenantId !== tenantId) {
          throw new Error("environment secret tenant scope mismatch");
        }
        const envName = bindings.get(ref);
        if (!envName) throw new Error("environment secret reference is not declared");
        return requiredEnv(env, envName);
      },
      async health() {
        return { ok: true };
      },
    },
  });
}

async function executeCapabilityJob(job, services) {
  const {
    workerId,
    handlers = {},
    heartbeatJob,
    completeJob,
    completeJobAndEnqueue,
    failJob,
    storeOptions,
    leaseSeconds = 300,
    setIntervalFn,
    clearIntervalFn,
  } = services;
  const identity = {
    tenantId: job.tenant_id,
    jobId: job.job_id,
    attempt: job.attempt,
    workerId,
  };
  const handler = handlers[job.capability];
  if (typeof handler !== "function") {
    if (job.effect_class === "none") {
      await completeJob({
        ...identity,
        receipt: {
          kind: "runtime_noop",
          worker_id: workerId,
          completed_at: new Date().toISOString(),
        },
      }, storeOptions);
      return;
    }
    await failJob({
      ...identity,
      errorCode: "CAPABILITY_ADAPTER_UNAVAILABLE",
      unknownEffect: true,
    }, storeOptions);
    return;
  }
  let leaseHeartbeat;
  if (typeof heartbeatJob === "function") {
    const {
      startRuntimeLeaseHeartbeat,
    } = require("../lib/runtime-lease-heartbeat.js");
    leaseHeartbeat = startRuntimeLeaseHeartbeat({
      ...identity,
      leaseSeconds,
    }, {
      heartbeatJob,
      storeOptions,
      setIntervalFn,
      clearIntervalFn,
    });
  }

  let execution;
  let executionError;
  let heartbeatFailed = false;
  let unknownEffect = false;
  try {
    execution = await handler(job);
    if (!execution || !execution.receipt) {
      throw new Error("capability adapter returned no receipt");
    }
    if (job.capability === "outbound.event.apply") {
      const {
        assertVerifiedOutboundReceipt,
      } = require("../lib/outbound-success.js");
      assertVerifiedOutboundReceipt(execution.receipt, job);
    }
  } catch (error) {
    executionError = error;
    unknownEffect = error && error.unknownEffect === true;
  }
  if (leaseHeartbeat) {
    try {
      await leaseHeartbeat.stop();
    } catch (error) {
      executionError = executionError || error;
      heartbeatFailed = true;
      unknownEffect = unknownEffect || job.effect_class !== "none";
    }
  }
  if (executionError) {
    const connectorCoverageCode = job.capability === "connector.coverage.refresh"
      && /^CONNECTOR_COVERAGE_[A-Z_]+$/.test(String(executionError.code || ""))
      ? executionError.code
      : null;
    const outboundLumaCode = job.capability === "outbound.event.apply"
      && new Set([
        "LUMA_LOGIN_REQUIRED",
        "LUMA_RSVP_UNAVAILABLE",
        "LUMA_EFFECT_UNKNOWN",
      ]).has(String(executionError.code || ""))
      ? executionError.code
      : null;
    await failJob({
      ...identity,
      errorCode: heartbeatFailed
        ? "CAPABILITY_HEARTBEAT_FAILED"
        : connectorCoverageCode || outboundLumaCode || "CAPABILITY_EXECUTION_FAILED",
      unknownEffect,
    }, storeOptions);
    return;
  }
  if (execution.receipt.status === "blocked") return;
  if (execution.continuation) {
    if (typeof completeJobAndEnqueue !== "function") throw new Error("runtime continuation store unavailable");
    await completeJobAndEnqueue({ ...identity, receipt: execution.receipt,
      nextJob: execution.continuation.job, availableAt: execution.continuation.availableAt }, storeOptions);
  } else {
    await completeJob({ ...identity, receipt: execution.receipt }, storeOptions);
  }
}

function createWorkerHandlers(env, capabilities, dependencies = {}) {
  const handlers = {};
  const servicesByAdapter = {};
  if (capabilities.includes("agent-economy.start")) {
    if (typeof dependencies.query !== "function") {
      throw new Error("Agent Economy Cloud citizen store unavailable");
    }
    const { createCloudCitizenStore } = require("../lib/cloud-citizen-store.js");
    const createWakeRunner = dependencies.createAgentEconomyCloudWakeRunner
      || require("../lib/agent-economy-cloud-wake.js").createAgentEconomyCloudWakeRunner;
    const citizenStore = createCloudCitizenStore({
      query: dependencies.query,
      encryptionKey: requiredEnv(env, "LM_CLOUD_CITIZEN_ENCRYPTION_KEY"),
    });
    const { createAgentEconomyControlStore } = require("../lib/agent-economy-control.js");
    const economyControl = createAgentEconomyControlStore({ query: dependencies.query });
    const { createPostgresFinancialRecordStore } = require("../lib/financial-record-store.js");
    servicesByAdapter["agent-economy-cloud"] = {
      citizenStore,
      isPaused: (tenantId) => economyControl.isPaused(tenantId),
      runSharedWake: createWakeRunner({
        citizenStore,
        dataDir: requiredEnv(env, "LM_DATA_DIR"),
        repoRoot: String(env.LM_REPO_ROOT || "").trim() || path.resolve(__dirname, "../../.."),
        financialStore: createPostgresFinancialRecordStore({ query: dependencies.query }),
        now: dependencies.now,
      }),
      now: dependencies.now,
    };
  }
  if (capabilities.includes("general-agent.work")) {
    const {
      createMoneyPrinterRuntimeStore,
    } = require("../lib/money-printer-runtime-store.js");
    const {
      createMoneyPrinterSpecialist,
    } = require("../lib/money-printer-specialist.js");
    let specialist = dependencies.moneyPrinterSpecialist || dependencies.runBoundedSpecialist;
    if (!specialist) {
      const runAgentRunner = typeof dependencies.runAgentRunner === "function"
        ? dependencies.runAgentRunner
        : typeof dependencies.runLocalAgentRunner === "function"
          ? dependencies.runLocalAgentRunner
          : null;
      const geminiKey = String(env.GEMINI_API_KEY || "").trim();
      if (!geminiKey && !runAgentRunner) throw new Error("GEMINI_API_KEY is required");
      const runtimeStore = typeof dependencies.query === "function"
        ? createMoneyPrinterRuntimeStore({ query: dependencies.query })
        : null;
      const readOpportunity = dependencies.readOpportunity || (runtimeStore && runtimeStore.readOpportunity);
      const updateOpportunity = dependencies.updateOpportunity || (runtimeStore && runtimeStore.updateOpportunity);
      if (typeof readOpportunity !== "function" || typeof updateOpportunity !== "function") {
        throw new Error("money printer runtime opportunity store unavailable");
      }
      const configuredRepoRoot = String(env.LM_REPO_ROOT || "").trim();
      specialist = (dependencies.createMoneyPrinterSpecialist || createMoneyPrinterSpecialist)({
        dataDir: path.resolve(requiredEnv(env, "LM_DATA_DIR")),
        repoRoot: configuredRepoRoot
          ? path.resolve(configuredRepoRoot)
          : dependencies.repoRoot || path.resolve(__dirname, "../../.."),
        fetchImpl: dependencies.fetchImpl || globalThis.fetch,
        geminiKey,
        runAgentRunner,
        readOpportunity,
        updateOpportunity,
        humanTaskStore: runtimeStore,
      });
    }
    if (typeof specialist !== "function") {
      throw new Error("money printer specialist unavailable");
    }
    servicesByAdapter["general-agent-work"] = {
      runBoundedSpecialist: specialist,
    };
  }
  if (capabilities.includes("money-printer.scout")) {
    const {
      createMoneyPrinterRuntimeStore,
    } = require("../lib/money-printer-runtime-store.js");
    const {
      createMoneyPrinterScout,
    } = require("../lib/money-printer-scout.js");
    const geminiKey = requiredEnv(env, "GEMINI_API_KEY");
    const tenantId = requiredEnv(env, "LM_MONEY_SCOUT_TENANT_ID");
    if (typeof dependencies.query !== "function") {
      throw new Error("money printer scout runtime store unavailable");
    }
    const runtimeStore = createMoneyPrinterRuntimeStore({ query: dependencies.query });
    const configuredRepoRoot = String(env.LM_REPO_ROOT || "").trim();
    const scout = (dependencies.createMoneyPrinterScout || createMoneyPrinterScout)({
      apiKey: geminiKey,
      tenantId,
      dataDir: path.resolve(requiredEnv(env, "LM_DATA_DIR")),
      repoRoot: configuredRepoRoot
        ? path.resolve(configuredRepoRoot)
        : dependencies.repoRoot || path.resolve(__dirname, "../../.."),
      fetchImpl: dependencies.fetchImpl || globalThis.fetch,
      readOpportunityBySource: runtimeStore.readOpportunityBySource,
      createOpportunity: runtimeStore.createOpportunity,
    });
    if (typeof scout !== "function") throw new Error("money printer scout unavailable");
    servicesByAdapter["money-printer-scout"] = { runScout: scout };
  }
  if (capabilities.includes("connector.coverage.refresh")) {
    const factory = dependencies.createConnectorCoverageRuntimeServices || (
      require("../lib/connector-coverage-runtime-services.js")
        .createConnectorCoverageRuntimeServices
    );
    const services = dependencies.connectorCoverageServices || factory(env, {
      query: dependencies.query,
      connect: dependencies.connect,
    });
    if (!services || typeof services !== "object" || Array.isArray(services)) {
      throw new Error("Connector coverage refresh services are unavailable");
    }
    servicesByAdapter["connector-coverage-refresh"] = services;
  }
  if (capabilities.includes("report.financial.telegram")) {
    const secretProvider = createScopedEnvironmentSecretProvider(env);
    const readBalance = dependencies.readBalance || ((walletAddress) => {
      const { readUsdcBalance } = require("../lib/base-usdc-balance.js");
      return readUsdcBalance(walletAddress, {
        rpcUrl: String(env.BASE_RPC_URL || "https://mainnet.base.org"),
        fetchImpl: globalThis.fetch,
      });
    });
    servicesByAdapter["financial-report-telegram"] = {
      secretProvider,
      supaUrl: requiredEnv(env, "SUPABASE_URL"),
      supaKey: requiredEnv(env, "SUPABASE_SERVICE_ROLE_KEY"),
      fetchImpl: globalThis.fetch,
      query: dependencies.query,
      readBalance,
    };
  }
  if (capabilities.includes("marketing.life-manager.daily.publish")) {
    const secretProvider = createScopedEnvironmentSecretProvider(env);
    const tenantId = requiredEnv(env, "LM_RUNTIME_TENANT_ID");
    const dataDir = path.resolve(requiredEnv(env, "LM_DATA_DIR"));
    const runtimeOwnedPath = (name) => {
      const resolved = path.resolve(requiredEnv(env, name));
      if (resolved !== dataDir && !resolved.startsWith(`${dataDir}${path.sep}`)) {
        throw new Error(`${name} must be beneath LM_DATA_DIR`);
      }
      return resolved;
    };
    servicesByAdapter["marketing-life-manager-daily"] = {
      secretProvider,
      profileProvider: {
        async get(requestTenantId, ref) {
          if (requestTenantId !== tenantId || ref !== "profile://instagram/life-manager") {
            throw new Error("Instagram profile tenant scope mismatch");
          }
          return {
            handle: requiredEnv(env, "LM_INSTAGRAM_HANDLE"),
            accountsPath: runtimeOwnedPath("LM_INSTAGRAM_ACCOUNTS_PATH"),
            settingsPath: runtimeOwnedPath("LM_INSTAGRAM_SETTINGS_PATH"),
            credentialsPath: runtimeOwnedPath("LM_INSTAGRAM_CREDENTIALS_PATH"),
            stateDir: runtimeOwnedPath("LM_INSTAGRAM_PROFILE_STATE_DIR"),
          };
        },
      },
      integrationProvider: {
        async get(requestTenantId, ref) {
          if (
            requestTenantId !== tenantId
            || ref !== "integration://postiz/tiktok/life-manager"
          ) {
            throw new Error("TikTok integration tenant scope mismatch");
          }
          return requiredEnv(env, "LM_TIKTOK_INTEGRATION");
        },
      },
    };
  }
  if (capabilities.includes("marketing.life-manager.daily.generate")) {
    servicesByAdapter["marketing-life-manager-daily-generation"] = {
      dataDir: path.resolve(requiredEnv(env, "LM_DATA_DIR")),
      pythonBin: String(env.PYTHON_BIN || "python3"),
    };
  }
  if (capabilities.includes("marketing.video.generate")) {
    const tenantId = requiredEnv(env, "LM_RUNTIME_TENANT_ID");
    const query = dependencies.query;
    if (typeof query !== "function") {
      throw new Error("marketing video generation receipt store is unavailable");
    }
    servicesByAdapter["marketing-video-generation"] = {
      dataDir: path.resolve(requiredEnv(env, "LM_DATA_DIR")),
      historyProvider: {
        async list(request) {
          if (
            !request
            || request.tenantId !== tenantId
            || !request.productId
            || !request.formatId
            || !request.locale
          ) {
            throw new Error("marketing video generation history scope mismatch");
          }
          const result = await query(`
            SELECT r.receipt
            FROM public.lm_runtime_job_receipts r
            JOIN public.lm_runtime_jobs j
              ON j.job_id = r.job_id
              AND j.tenant_id = r.tenant_id
            WHERE r.tenant_id = $1
              AND j.capability = 'marketing.video.generate'
              AND r.outcome = 'completed'
              AND r.receipt->>'kind' = 'marketing_video_artifact'
              AND r.receipt->>'product_id' = $2
              AND r.receipt->>'format_id' = $3
              AND r.receipt->>'locale' = $4
            ORDER BY r.created_at ASC
            LIMIT 500
          `, [
            tenantId,
            request.productId,
            request.formatId,
            request.locale,
          ]);
          return result.rows.map((row) => row.receipt);
        },
      },
      now: dependencies.now || (() => new Date().toISOString()),
    };
  }
  if (capabilities.includes("marketing.video.publish")) {
    const tenantId = requiredEnv(env, "LM_RUNTIME_TENANT_ID");
    const dataDir = path.resolve(requiredEnv(env, "LM_DATA_DIR"));
    const runtimeOwnedPath = (name) => {
      const resolved = path.resolve(requiredEnv(env, name));
      if (resolved !== dataDir && !resolved.startsWith(`${dataDir}${path.sep}`)) {
        throw new Error(`${name} must be beneath LM_DATA_DIR`);
      }
      return resolved;
    };
    const profileRef = requiredEnv(env, "LM_HONNE_EN_INSTAGRAM_PROFILE_REF");
    const integrationRef = requiredEnv(env, "LM_HONNE_EN_TIKTOK_INTEGRATION_REF");
    servicesByAdapter["marketing-video-publication"] = {
      secretProvider: createScopedEnvironmentSecretProvider(env),
      profileProvider: {
        async get(requestTenantId, ref) {
          if (requestTenantId !== tenantId || ref !== profileRef) {
            throw new Error("marketing video publication Instagram profile scope mismatch");
          }
          return {
            handle: requiredEnv(env, "LM_INSTAGRAM_HANDLE"),
            accountsPath: runtimeOwnedPath("LM_INSTAGRAM_ACCOUNTS_PATH"),
            settingsPath: runtimeOwnedPath("LM_INSTAGRAM_SETTINGS_PATH"),
            credentialsPath: runtimeOwnedPath("LM_INSTAGRAM_CREDENTIALS_PATH"),
            stateDir: runtimeOwnedPath("LM_INSTAGRAM_PROFILE_STATE_DIR"),
          };
        },
      },
      integrationProvider: {
        async get(requestTenantId, ref) {
          if (requestTenantId !== tenantId || ref !== integrationRef) {
            throw new Error("marketing video publication TikTok integration scope mismatch");
          }
          return requiredEnv(env, "LM_TIKTOK_INTEGRATION");
        },
      },
    };
  }
  if (capabilities.includes("marketing.liveness.telegram")) {
    servicesByAdapter["marketing-liveness-telegram"] = {
      secretProvider: {
        async get(requestTenantId, ref) {
          if (
            requestTenantId !== requiredEnv(env, "LM_RUNTIME_TENANT_ID")
            || ref !== "secret://telegram/bot-token"
          ) throw new Error("marketing liveness Telegram secret scope mismatch");
          return requiredEnv(env, "LM_TELEGRAM_BOT_TOKEN");
        },
      },
      chatProvider: {
        async get(requestTenantId, ref) {
          if (
            requestTenantId !== requiredEnv(env, "LM_RUNTIME_TENANT_ID")
            || ref !== "telegram-chat://owner"
          ) {
            throw new Error("marketing liveness Telegram chat scope mismatch");
          }
          return requiredEnv(env, "LM_TELEGRAM_ALERT_CHAT_ID");
        },
      },
      now: dependencies.now,
    };
  }
  if (capabilities.includes("marketing.observation.collect")) {
    const tenantId = requiredEnv(env, "LM_RUNTIME_TENANT_ID");
    const query = dependencies.query;
    if (typeof query !== "function") {
      throw new Error("marketing observation receipt store is unavailable");
    }
    const fetchImpl = dependencies.fetchImpl || globalThis.fetch;
    const secretProvider = createScopedEnvironmentSecretProvider(env);
    const {
      normalizePostizMetrics,
    } = require("../lib/marketing-observation-adapter.js");
    const unavailablePlatform = () => ({
      views: { status: "unavailable", value: null, reason: "metric_not_supported" },
      likes: { status: "unavailable", value: null, reason: "metric_not_supported" },
      comments: { status: "unavailable", value: null, reason: "metric_not_supported" },
      shares: { status: "unavailable", value: null, reason: "metric_not_supported" },
    });
    servicesByAdapter["marketing-platform-observation"] = {
      receiptProvider: {
        async get(requestTenantId, ref) {
          const match = /^runtime-receipt:\/\/(marketing-daily:[0-9a-f]{64})$/.exec(
            String(ref || ""),
          );
          if (requestTenantId !== tenantId || !match) {
            throw new Error("marketing observation receipt scope mismatch");
          }
          const rows = (await query(`
            SELECT r.receipt
            FROM public.lm_runtime_job_receipts r
            JOIN public.lm_runtime_jobs j
              ON j.job_id = r.job_id
              AND j.tenant_id = r.tenant_id
            WHERE r.tenant_id = $1
              AND r.job_id = $2
              AND r.outcome = 'completed'
              AND j.capability = 'marketing.life-manager.daily.publish'
            ORDER BY r.attempt DESC
            LIMIT 1
          `, [requestTenantId, match[1]])).rows;
          if (rows.length !== 1) {
            throw new Error("marketing publication receipt is unavailable");
          }
          return rows[0].receipt;
        },
      },
      platformMetricProvider: {
        async collect({ publication, window }) {
          if (publication.provider_route !== "postiz") {
            return unavailablePlatform();
          }
          const token = await secretProvider.get(
            tenantId,
            "secret://postiz/api-key",
          );
          const days = { "2h": 1, "24h": 1, "72h": 3, "7d": 7 }[window];
          const response = await fetchImpl(
            `https://api.postiz.com/public/v1/analytics/post/${
              encodeURIComponent(publication.provider_post_id)
            }?date=${days}`,
            {
              headers: { Authorization: token },
              signal: AbortSignal.timeout(30_000),
            },
          );
          if (!response || response.ok !== true) {
            throw new Error("Postiz metric request failed");
          }
          return normalizePostizMetrics(await response.json());
        },
      },
      productMetricProvider: {
        async collect() {
          const value = {
            status: "unavailable",
            value: null,
            reason: "attribution_not_configured",
          };
          return {
            installs: { ...value },
            activations: { ...value },
            trials: { ...value },
            paid: { ...value },
            proceeds_minor: { ...value },
          };
        },
      },
      now: dependencies.now || (() => new Date().toISOString()),
    };
  }
  if (capabilities.includes("outbound.event.apply")) {
    const { createLumaEvidenceStore } = require("../lib/luma-evidence-store.js");
    const evidenceStore = dependencies.lumaEvidenceStore || createLumaEvidenceStore({
      dataDir: path.resolve(requiredEnv(env, "LM_DATA_DIR")),
    });
    let provider = dependencies.lumaProvider;
    if (!provider) {
      const { chromium } = require("playwright-core");
      const {
        createCloakBrowserDailyDriver,
      } = require("../lib/cloakbrowser-daily-driver.js");
      const lookup = dependencies.lookupHost || dns.promises.lookup;
      const dailyDriver = dependencies.lumaDailyDriver || createCloakBrowserDailyDriver({
        connectOverCDP: dependencies.connectOverCDP
          || ((endpoint) => chromium.connectOverCDP(endpoint)),
        resolveEndpoint: dependencies.resolveCloakEndpoint || (async () => {
          const resolved = await lookup("host.docker.internal", { family: 4 });
          const address = typeof resolved === "string" ? resolved : resolved.address;
          return `http://${address}:9222`;
        }),
      });
      const {
        createReadOnlyLumaSessionAuth,
      } = require("../lib/luma-daily-driver-auth.js");
      const {
        createConnectorEventsPack,
      } = require("../lib/connector-events-pack.js");
      const { readLumaFormProfile } = require("../lib/luma-form-profile.js");
      const auth = dependencies.lumaAuth || createReadOnlyLumaSessionAuth({ dailyDriver });
      const readPrivateLumaFormProfile = dependencies.readPrivateLumaFormProfile || readLumaFormProfile;
      const readTrustedLumaFormProfile = dependencies.readLumaFormProfile || (() => (
        readPrivateLumaFormProfile({
          path: path.join(requiredEnv(env, "LM_DATA_DIR"), "private", "connector-luma-form-profile.json"),
        })
      ));
      const pack = (dependencies.createConnectorEventsPack || createConnectorEventsPack)({
        dailyDriver,
        auth,
        evidenceStore,
        readLumaFormProfile: readTrustedLumaFormProfile,
        now: dependencies.now,
      });
      provider = pack.provider;
    }
    servicesByAdapter["outbound-luma-rsvp"] = {
      provider,
      readExternalReceipt: evidenceStore.readExternalReceipt,
      readArtifact: evidenceStore.readArtifact,
      fetchImpl: dependencies.fetchImpl || globalThis.fetch,
      now: dependencies.now,
    };
  }
  const createRegistry = dependencies.createRegistry || (
    require("../lib/loop-adapter-registry.js").createConfiguredLoopAdapterRegistry
  );
  const registry = createRegistry({ servicesByAdapter });
  for (const capability of capabilities) {
    if (!registry.hasCapability(capability)) continue;
    const adapter = registry.getByCapability(capability);
    handlers[capability] = (job) => adapter.execute(job);
  }
  return handlers;
}

function observeWorkerPoll(state, active, now = () => new Date().toISOString()) {
  const observedAt = String(now());
  if (!Number.isFinite(Date.parse(observedAt))) {
    throw new Error("worker poll timestamp is invalid");
  }
  state.lastPollAt = observedAt;
  return active !== true;
}

async function runCapabilityWorker(env = process.env) {
  const { Pool } = require("pg");
  const {
    claimJobs,
    heartbeatJob,
    completeJob,
    completeJobAndEnqueue,
    failJob,
  } = require("../lib/runtime-job-store.js");
  const connectionString = requiredEnv(env, "LM_RUNTIME_DATABASE_URL");
  const capabilities = requiredEnv(env, "LM_WORKER_CAPABILITIES")
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean);
  if (capabilities.length < 1) throw new Error("LM_WORKER_CAPABILITIES is empty");
  const workerId = String(env.LM_WORKER_ID || os.hostname()).trim();
  const pool = new Pool({ connectionString, max: 2 });
  const opts = { query: pool.query.bind(pool) };
  const handlers = createWorkerHandlers(env, capabilities, {
    query: opts.query,
    connect: pool.connect.bind(pool),
  });
  const scoutCycle = capabilities.includes("money-printer.scout")
    ? {
      enqueue: require("../lib/money-printer-scout.js").enqueueMoneyPrinterScoutCycle,
      tenantId: requiredEnv(env, "LM_MONEY_SCOUT_TENANT_ID"),
      intervalMs: env.LM_MONEY_SCOUT_INTERVAL_MS == null || env.LM_MONEY_SCOUT_INTERVAL_MS === ""
        ? undefined : Number(env.LM_MONEY_SCOUT_INTERVAL_MS),
    }
    : null;
  const state = {
    role: "worker",
    workerId,
    capabilities,
    ready: false,
    error: null,
    lastPollAt: null,
  };
  const health = createHealthServer(Number(env.LM_WORKER_HEALTH_PORT || 8790), state);
  const leaseSeconds = Number(env.LM_WORKER_LEASE_SECONDS || 300);
  let active = false;

  async function tick() {
    if (!observeWorkerPoll(state, active)) return;
    active = true;
    try {
      await pool.query("SELECT 1");
      if (scoutCycle) {
        await scoutCycle.enqueue({
          query: opts.query, tenantId: scoutCycle.tenantId, nowMs: Date.now(), intervalMs: scoutCycle.intervalMs,
        });
      }
      const jobs = await claimJobs({
        workerId,
        capabilities,
        limit: 1,
        leaseSeconds,
      }, opts);
      for (const job of jobs) {
        await executeCapabilityJob(job, {
          workerId,
          handlers,
          heartbeatJob,
          completeJob,
          completeJobAndEnqueue,
          failJob,
          storeOptions: opts,
          leaseSeconds,
        });
      }
      state.ready = true;
      state.error = null;
      state.lastPollAt = new Date().toISOString();
    } catch (error) {
      state.error = error;
    } finally {
      active = false;
    }
  }

  await tick();
  const timer = setInterval(tick, Number(env.LM_WORKER_POLL_MS || 1000));
  const stop = async () => {
    clearInterval(timer);
    health.close();
    await pool.end();
  };
  process.once("SIGTERM", () => stop().finally(() => process.exit(0)));
  process.once("SIGINT", () => stop().finally(() => process.exit(0)));
}

async function main(argv = process.argv.slice(2)) {
  if (argv.length !== 1 || argv[0] !== "internal-worker") {
    throw new Error("usage: runtime-up.js internal-worker");
  }
  return runCapabilityWorker();
}

if (require.main === module) {
  main().catch((error) => {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  });
}

module.exports = {
  createScopedEnvironmentSecretProvider,
  createWorkerHandlers,
  observeWorkerPoll,
  executeCapabilityJob,
  runCapabilityWorker,
};
