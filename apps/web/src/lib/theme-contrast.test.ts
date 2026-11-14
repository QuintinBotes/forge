import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

// `text-<status>-foreground` is the ink for a SOLID `bg-<status>` fill; it is
// near-black in the dark theme. On a translucent tint (`bg-<status>/10`) the
// page background shows through, so that ink is dark-on-dark. Use
// `text-foreground` (or the bright `text-<status>`) on tints instead.
const STATUSES = ["warning", "success", "danger", "destructive"];
const SRC = join(__dirname, "..");

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return sourceFiles(path);
    return /\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name) ? [path] : [];
  });
}

describe("theme contrast", () => {
  it("never puts solid-fill foreground ink on a translucent status tint", () => {
    const offenders: string[] = [];
    for (const file of sourceFiles(SRC)) {
      for (const literal of readFileSync(file, "utf8").match(/"[^"\n]*"/g) ??
        []) {
        for (const status of STATUSES) {
          // Unprefixed only: `hover:bg-x/90` darkens a solid fill, it is not a tint.
          const tint = new RegExp(`(^|[\\s"])bg-${status}/\\d+`);
          const ink = new RegExp(`\\btext-${status}-foreground\\b`);
          if (tint.test(literal) && ink.test(literal)) {
            offenders.push(`${file.slice(SRC.length + 1)}: ${literal}`);
          }
        }
      }
    }
    expect(offenders).toEqual([]);
  });
});
