import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request("http://localhost/", {
      headers: { accept: "text/html" },
    }),
    {
      ASSETS: {
        fetch: async () => new Response("Not found", { status: 404 }),
      },
    },
    {
      waitUntil() {},
      passThroughOnException() {},
    },
  );
}

test("server-renders the production nesting comparison dashboard", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<title>排料方案对比中心<\/title>/i);
  assert.match(html, /两套排料方案对比/);
  assert.match(html, /方案 B 综合表现更优/);
  assert.match(html, /99\.0834%/);
  assert.match(html, /99\.4922%/);
  assert.match(html, /生产执行明细/);
  assert.doesNotMatch(html, /codex-preview|Your site is taking shape|react-loading-skeleton/i);
});

test("ships interactions, responsive styles, and no starter preview", async () => {
  const [page, layout, css, packageJson] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/layout.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/globals.css", import.meta.url), "utf8"),
    readFile(new URL("../package.json", import.meta.url), "utf8"),
  ]);

  assert.match(page, /downloadComparison/);
  assert.match(page, /navigator\.clipboard\.writeText/);
  assert.match(page, /原材料切法/);
  assert.match(page, /管段拼法/);
  assert.match(page, /校验与归一化/);
  assert.match(css, /@media \(max-width: 680px\)/);
  assert.match(css, /prefers-reduced-motion/);
  assert.match(layout, /lang="zh-CN"/);
  assert.doesNotMatch(layout, /codex-preview|_sites-preview/i);
  assert.doesNotMatch(packageJson, /react-loading-skeleton/);

  await assert.rejects(access(new URL("../app/_sites-preview", import.meta.url)));
});
