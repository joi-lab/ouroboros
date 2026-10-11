import { copyFile, mkdir, rm } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const siteRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const sourceRoot = resolve(siteRoot, "..", "assets");
const targetRoot = resolve(siteRoot, "public", "assets");
const assetNames = [
  "bench-cl-bench.svg",
  "bench-osworld.svg",
  "bench-terminal-bench.svg",
  "evolution.png",
  "game-demo.png",
  "icon_1024.png",
  "install-macos.png",
  "og-preview.png",
  "skill-hub.png",
  "social-preview.png",
  "swarm.jpg"
];

await rm(targetRoot, { recursive: true, force: true });
await mkdir(targetRoot, { recursive: true });
await Promise.all(assetNames.map((name) => copyFile(resolve(sourceRoot, name), resolve(targetRoot, name))));
