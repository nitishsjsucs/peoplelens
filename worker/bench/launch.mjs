// Runs one bench mode in this fresh Node process (see bench/cpu.ts and bench/run.mjs).
// CPU is process CPU (user plus system, including GC) from process.cpuUsage().
import { readFileSync, writeFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const [bundle, mode, memoPath, itemsPath, outPath] = process.argv.slice(2);
const cpuMs = () => {
  const u = process.cpuUsage();
  return (u.user + u.system) / 1000;
};
// Worker logs are built (stringified) by logEvent exactly as in production, then dropped here.
console.log = () => {};

const t0 = cpuMs();
const bench = await import(pathToFileURL(bundle).href);
const startupMs = cpuMs() - t0;

const items = JSON.parse(readFileSync(itemsPath, "utf8"));
const memo = JSON.parse(readFileSync(memoPath, "utf8"));

// Node loads its web APIs (Request, Response, Headers and fetch from undici, URL,
// text codecs, WebCrypto) lazily on first use, about 80 to 100 ms of CPU that workerd
// does not spend, because its web APIs are native and ready when an isolate starts.
// Touch them here, before measuring. No Worker or library code runs in this step.
async function initNodeRuntime() {
  const t = cpuMs();
  await new Request("https://runtime.invalid/", { method: "POST", body: "{}", headers: { a: "b" } }).json();
  await new Response("{}", { headers: { "content-type": "application/json" } }).json();
  await new Response("x").text();
  new Headers({ a: "b" }).get("a");
  new URL("https://runtime.invalid/a?b=c").searchParams.get("b");
  new URLSearchParams({ a: "b" }).toString();
  new TextDecoder().decode(new TextEncoder().encode("x"));
  crypto.randomUUID();
  crypto.getRandomValues(new Uint8Array(8));
  await crypto.subtle.digest("SHA-256", new Uint8Array(8));
  structuredClone({ a: [1] });
  AbortSignal.timeout(1000);
  return cpuMs() - t;
}
const runtimeInitMs = await initNodeRuntime();
let result;
if (mode === "warm") {
  await bench.record(items, memo);
  writeFileSync(memoPath, JSON.stringify(memo));
  result = await bench.measure(items, memo, cpuMs, 5, 1);
} else if (mode === "cold") {
  result = await bench.measureCold(items, memo, cpuMs, 5);
} else {
  throw new Error(`unknown mode ${mode}`);
}
writeFileSync(outPath, JSON.stringify({ mode, startupMs, runtimeInitMs, ...result }));
