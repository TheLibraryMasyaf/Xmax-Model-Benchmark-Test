#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "..");
const sdkPackage = JSON.parse(
  fs.readFileSync(path.join(root, "node_modules", "@xmaxai", "sdk", "package.json"), "utf8"),
);
const outputDirectory = path.join(root, "var", "sdk");
const output = path.join(outputDirectory, `xmax-sdk-${sdkPackage.version}.js`);
fs.mkdirSync(outputDirectory, { recursive: true });

await build({
  entryPoints: [path.join(root, "node_modules", "@xmaxai", "sdk", "dist", "index.js")],
  outfile: output,
  bundle: true,
  platform: "browser",
  format: "esm",
  target: ["chrome120"],
  sourcemap: false,
  legalComments: "none",
});

process.stdout.write(`${JSON.stringify({ sdkVersion: sdkPackage.version, output })}\n`);
