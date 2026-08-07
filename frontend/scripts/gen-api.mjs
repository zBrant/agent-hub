#!/usr/bin/env node
/**
 * Regenerates the two committed-but-generated files in `src/api/`.
 *
 *   FastAPI /openapi.json          --openapi-typescript-->      src/api/schema.d.ts
 *   AgentEvent JSON Schema  --json-schema-to-typescript-->      src/api/events.d.ts
 *
 * docs/architecture.md §7. Hand-writing either file is forbidden.
 *
 *   pnpm gen:api            write the files
 *   pnpm gen:api --check    fail if the committed files are out of date
 *
 * Sources, overridable by environment:
 *   AGENTHUB_OPENAPI_URL     default http://127.0.0.1:8000/openapi.json
 *   AGENTHUB_EVENT_SCHEMA    default ../backend/schemas/agent-event.schema.json
 */
import { readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import { compile } from "json-schema-to-typescript";
import openapiTS, { astToString } from "openapi-typescript";

const here = dirname(fileURLToPath(import.meta.url));
const frontendRoot = resolve(here, "..");
const repoRoot = resolve(frontendRoot, "..");

const OPENAPI_URL =
  process.env.AGENTHUB_OPENAPI_URL ?? "http://127.0.0.1:8000/openapi.json";
const EVENT_SCHEMA_PATH = resolve(
  repoRoot,
  process.env.AGENTHUB_EVENT_SCHEMA ??
    "backend/schemas/agent-event.schema.json",
);
const SCHEMA_OUT = resolve(frontendRoot, "src/api/schema.d.ts");
const EVENTS_OUT = resolve(frontendRoot, "src/api/events.d.ts");

const BANNER = [
  "/**",
  " * GENERATED FILE — do not edit.",
  " * Run `pnpm gen:api`. See src/api/README.md and docs/architecture.md §7.",
  " */",
  "",
].join("\n");

const checkOnly = process.argv.includes("--check");

function fail(message) {
  process.stderr.write(`gen:api — ${message}\n`);
  process.exitCode = 1;
}

async function generateSchema() {
  let document;
  try {
    document = new URL(OPENAPI_URL);
  } catch {
    document = resolve(repoRoot, OPENAPI_URL);
  }
  const ast = await openapiTS(document, { immutable: true });
  return `${BANNER}${astToString(ast)}`;
}

async function generateEvents() {
  const raw = await readFile(EVENT_SCHEMA_PATH, "utf8");
  const compiled = await compile(JSON.parse(raw), "AgentEvent", {
    bannerComment: BANNER,
    additionalProperties: false,
    style: { singleQuote: false },
  });
  return compiled;
}

async function emit(target, produce, hint) {
  let contents;
  try {
    contents = await produce();
  } catch (error) {
    fail(
      `${target}: ${error instanceof Error ? error.message : String(error)}\n  ${hint}`,
    );
    return;
  }

  if (!checkOnly) {
    await writeFile(target, contents, "utf8");
    process.stdout.write(`gen:api — wrote ${target}\n`);
    return;
  }

  const committed = await readFile(target, "utf8").catch(() => null);
  if (committed !== contents) {
    fail(
      `${target} is out of date. Run \`pnpm gen:api\` and commit the result.`,
    );
  }
}

await emit(
  SCHEMA_OUT,
  generateSchema,
  `Is the backend serving ${OPENAPI_URL}? (Phase 1, B5)`,
);
await emit(
  EVENTS_OUT,
  generateEvents,
  `Expected the AgentEvent JSON Schema at ${EVENT_SCHEMA_PATH}, exported by backend/scripts/export_schemas.py.`,
);
