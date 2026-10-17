// Provenance of responses.json beside it; never imported or run by the test suite.
//
// It records what the engine's control server actually writes for an interaction answer:
// the exported DaemonControlApiServer of a BUILT Claudexor checkout (its handshake, the
// POST /v2/runs/:id/interactions/:iid/answer route, json() -> controlProblemError ->
// problemBody), started on an ephemeral loopback port with a synthetic one-run facade and
// a synthetic answer service. No harness, model, run or installed daemon is involved.
//
// In each Claudexor checkout (Node >= 24.15, pnpm from its packageManager field):
//   pnpm install --frozen-lockfile --filter "@claudexor/control-api..."
//   pnpm --filter "@claudexor/control-api..." run build
// then: node record.mjs <checkout> [<checkout> ...] > responses.json
// Every checkout must produce byte-identical responses; each engine identity is listed.
import { createHash, randomBytes } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const SOURCES = [
  "packages/control-api/src/daemon-server.ts",
  "packages/control-api/src/problem-response.ts",
  "packages/util/src/problem-safety.ts",
  "packages/schema/src/problem.ts",
];
// interaction id -> what the synthetic answer service reports for it
const OUTCOMES = {
  delivered: { status: "delivered", message: "delivered to the live session" },
  already_resolved: { status: "already_resolved", message: "interaction was already resolved" },
  already_resolved_without_message: { status: "already_resolved" },
  not_found: { status: "not_found", message: "no pending interaction" },
  rejected: { status: "rejected", message: "answer does not match the question set" },
};
const CASES = [
  ...Object.keys(OUTCOMES).map((name) => ({ name, service: true, run: "run-1" })),
  { name: "unknown_run", service: true, run: "run-gone" },
  { name: "no_answer_service", service: false, run: "run-1" },
];
const ANSWERS = { answers: [{ questionId: "q1", selectedLabels: [], freeText: "9090" }] };

async function record(checkout) {
  const load = (path) => import(pathToFileURL(join(checkout, path)).href);
  const { DaemonControlApiServer } = await load("packages/control-api/dist/index.js");
  const { CONTROL_PROTOCOL_MAJOR } = await load("packages/schema/dist/index.js");
  const run = { id: "run-1", runId: "run-1", state: "running" };
  const daemon = { list: async (query) => (query?.id === run.id ? [run] : []) };
  let engine;
  const responses = [];
  for (const { name, service, run: runId } of CASES) {
    const token = randomBytes(24).toString("hex");
    const services = service ? { answerInteraction: (_run, interaction) => OUTCOMES[interaction] } : {};
    const server = new DaemonControlApiServer({ token, daemon, services, host: "127.0.0.1", port: 0 });
    const { host, port } = await server.start();
    const post = (path, body) => fetch(`http://${host}:${port}${path}`, {
      method: "POST",
      headers: { authorization: `Bearer ${token}`, "content-type": "application/json",
                 "x-claudexor-protocol-major": String(CONTROL_PROTOCOL_MAJOR) },
      body: JSON.stringify(body),
    });
    try {
      const handshake = await (await post("/v2/handshake", { protocolMajor: CONTROL_PROTOCOL_MAJOR,
                                                              client: "fixture" })).json();
      engine ??= { version: handshake.engine.version, sha: handshake.engine.sha };
      const interaction = service && OUTCOMES[name] ? name : "int-1";
      const response = await post(`/v2/runs/${runId}/interactions/${interaction}/answer`, ANSWERS);
      responses.push({ case: name, service_outcome: (service && OUTCOMES[name]) || null,
                       status: response.status, content_type: response.headers.get("content-type"),
                       body: await response.text() });
    } finally {
      await server.stop();
    }
  }
  const sources = Object.fromEntries(SOURCES.map((path) => [
    path, createHash("sha256").update(readFileSync(join(checkout, path))).digest("hex")]));
  return { engine: { ...engine, sources }, responses };
}

const recorded = [];
for (const checkout of process.argv.slice(2)) recorded.push(await record(checkout));
if (!recorded.length) throw new Error("usage: node record.mjs <built Claudexor checkout> [...]");
const bytes = JSON.stringify(recorded[0].responses);
for (const { engine, responses } of recorded) {
  if (JSON.stringify(responses) !== bytes) throw new Error(`engine ${engine.version} serializes differently`);
}
process.stdout.write(`${JSON.stringify({
  _provenance: "Recorded by record.mjs beside this file from each listed engine build's real control "
    + "server with a synthetic run facade and answer service; no harness, model or run.",
  engines: recorded.map(({ engine }) => engine),
  responses: recorded[0].responses,
}, null, 2)}\n`);
