import { rmSync } from "node:fs";
import path from "node:path";

export default async function globalSetup() {
  rmSync(path.resolve(process.cwd(), "cache_test_e2e"), {
    recursive: true,
    force: true,
  });
}
