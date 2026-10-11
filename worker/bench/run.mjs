// `npm run bench:cpu --workspace worker`: CPU per /api/ask on the production request
// path (SPEC.md 25.4 D). Not a CI gate. Bundles bench/cpu.ts with the esbuild that
// wrangler already pins (no new dependency), prepares keys and tokens, then runs one
// warm process (a recording pass, one warm-up pass, then 5 measured passes over the
// 214 eval turns) and 5 cold processes (module evaluation, then the first 5 asks).
import { spawnSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { build } from "esbuild";
import { SignJWT, exportJWK, exportPKCS8, generateKeyPair } from "jose";

const here = path.dirname(fileURLToPath(import.meta.url));
const worker = path.resolve(here, "..");
const out = path.join(worker, ".wrangler", "bench");
mkdirSync(out, { recursive: true });
const bundle = path.join(out, "cpu.mjs");

await build({
  entryPoints: [path.join(here, "cpu.ts")],
  bundle: true,
  platform: "node",
  format: "esm",
  target: "node22",
  // the same package builds wrangler bundles for workerd
  conditions: ["workerd", "worker", "browser"],
  outfile: bundle,
  logLevel: "warning",
  plugins: [
    {
      name: "no-cloudflare-runtime",
      setup(b) {
        b.onResolve({ filter: /^cloudflare:/ }, (args) => ({
          errors: [{ text: `the bench must not import ${args.path} (imported by ${args.importer})` }],
        }));
      },
    },
  ],
});
const bench = await import(pathToFileURL(bundle).href);

const questions = readFileSync(path.resolve(worker, "..", "eval", "questions.jsonl"), "utf8")
  .split("\n")
  .filter((l) => l.trim())
  .map((l) => JSON.parse(l))
  .map((i) => ({ id: i.id, role: i.role, turns: i.turns.map((t) => ({ text: t.text })) }));
const itemsPath = path.join(out, "items.json");
writeFileSync(itemsPath, JSON.stringify(questions));

const access = await generateKeyPair("RS256", { extractable: true });
const jwk = { ...(await exportJWK(access.publicKey)), kid: "bench-1", alg: "RS256", use: "sig" };
const roles = { people_analytics: "analyst@example.test", hr_business_partner: "hrbp@example.test", talent_acquisition: "recruiter@example.test", executive: "exec@example.test" };
const tokens = {};
for (const [role, email] of Object.entries(roles)) {
  tokens[role] = await new SignJWT({ email, sub: `user-${email}`, type: "app" })
    .setProtectedHeader({ alg: "RS256", kid: "bench-1" })
    .setIssuedAt()
    .setIssuer(`https://${bench.TEAM}`)
    .setAudience([bench.AUD])
    .setExpirationTime("2h")
    .sign(access.privateKey);
}
const sa = await generateKeyPair("RS256", { extractable: true });
const saKeyJson = JSON.stringify({
  type: "service_account",
  client_email: `peoplelens-worker@${bench.PROJECT}.iam.gserviceaccount.com`,
  private_key: await exportPKCS8(sa.privateKey),
});
const memoPath = path.join(out, "memo.json");
writeFileSync(memoPath, JSON.stringify({ jwks: { keys: [jwk] }, saKeyJson, tokens, llm: {}, bq: {} }));

function launch(mode, tag) {
  const result = path.join(out, `result-${tag}.json`);
  const r = spawnSync(process.execPath, [path.join(here, "launch.mjs"), bundle, mode, memoPath, itemsPath, result], { stdio: "inherit" });
  if (r.status !== 0) throw new Error(`bench ${mode} process failed (exit ${r.status})`);
  return JSON.parse(readFileSync(result, "utf8"));
}

const warm = launch("warm", "warm");
const colds = [1, 2, 3, 4, 5].map((i) => launch("cold", `cold-${i}`));

const sorted = [...warm.asks].sort((a, b) => a - b);
const pct = (p) => sorted[Math.max(0, Math.ceil((p / 100) * sorted.length) - 1)];
const r2 = (x) => Math.round(x * 100) / 100;
const range = (xs) => `${r2(Math.min(...xs))} to ${r2(Math.max(...xs))}`;
const summary = {
  date: new Date().toLocaleDateString("en-CA"),
  node: process.version,
  cpu: os.cpus()[0]?.model ?? "unknown",
  warm: { asks: sorted.length, answers: warm.answers, p50: r2(pct(50)), p95: r2(pct(95)), p99: r2(pct(99)), max: r2(sorted[sorted.length - 1]) },
  cold: {
    processes: colds.length,
    startup_ms: colds.map((c) => r2(c.startupMs)),
    // createApp, including the warm-up pass: part of startup in production (createWorker runs at module scope)
    app_build_ms: colds.map((c) => r2(c.appMs ?? 0)),
    node_runtime_init_ms: colds.map((c) => r2(c.runtimeInitMs)),
    first: colds.map((c) => r2(c.asks[0])),
    second: colds.map((c) => r2(c.asks[1])),
    next_three: colds.flatMap((c) => c.asks.slice(2).map(r2)),
  },
  free_cpu_limit_ms: 10,
};
writeFileSync(path.join(out, "summary.json"), JSON.stringify(summary, null, 1) + "\n");
console.log(`bench:cpu ${summary.date}, Node ${summary.node}, ${summary.cpu}`);
console.log(`warm /api/ask (${summary.warm.asks} asks over 5 passes of the 214 eval turns, ${summary.warm.answers} answers): p50 ${summary.warm.p50}, p95 ${summary.warm.p95}, p99 ${summary.warm.p99}, max ${summary.warm.max} ms`);
console.log(`cold isolate (${colds.length} fresh processes): module evaluation ${range(summary.cold.startup_ms)} ms plus app build with warm-up ${range(summary.cold.app_build_ms)} ms, both startup in production (Node built-ins initialized before measuring: ${range(summary.cold.node_runtime_init_ms)} ms, not counted); first /api/ask ${range(summary.cold.first)} ms; second ${range(summary.cold.second)} ms; next three ${range(summary.cold.next_three)} ms`);
console.log(`Workers Free CPU limit: 10 ms per request. Full numbers: ${path.relative(worker, path.join(out, "summary.json"))}`);
