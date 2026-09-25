// Visual flow capture — the console's repeatable screenshot test.
// Runs the P0 user journey (covered query -> FastPath flash -> answer +
// receipt) in headless Edge and saves three state images:
//   ../gui_shots/t1_initial.png    idle console + health pills
//   ../gui_shots/t2_streaming.png  mid-flight pipeline rail
//   ../gui_shots/t3_result.png     certified answer + receipt explorer
// Usage: node scripts/screenshot-flow.mjs [--full]
import { chromium } from "playwright";
import { mkdirSync } from "node:fs";

const OUT = (process.env.SHOT_DIR
  ? process.env.SHOT_DIR.replace(/\\/g, "/").replace(/\/$/, "") + "/"
  : new URL("../../gui_shots/", import.meta.url).pathname.replace(/^\/(\w:)/, "$1"));
mkdirSync(OUT, { recursive: true });
const CONSOLE_URL = process.env.CONSOLE_URL || "http://localhost:5173/";
const FULL = process.argv.includes("--full");

const browser = await chromium.launch({ channel: "msedge", headless: true });
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });

await page.goto(CONSOLE_URL, { waitUntil: "domcontentloaded" });
await page.getByText("backend live").first().waitFor({ timeout: 20000 });
await page.waitForTimeout(600);

// tenant auth: Render enforces QUERY_API_KEYS — the key is stored locally
// in the browser via the header pill (the production posture)
if (process.env.CONSOLE_API_KEY) {
  await page.getByRole("button", { name: "api key" }).click();
  await page.getByPlaceholder("X-API-Key").fill(process.env.CONSOLE_API_KEY);
  await page.getByRole("button", { name: "save" }).click();
  await page.waitForTimeout(400);
}
await page.screenshot({ path: OUT + "t1_initial.png", fullPage: FULL });

// submit a FastPath-covered query via the example chip (real click path)
await page.getByRole("button", { name: "What was Apple's total net sales in Q4 2023?" }).click();
await page.waitForTimeout(1400); // mid-flight: rail lit, button streaming
await page.screenshot({ path: OUT + "t2_streaming.png", fullPage: FULL });

await page.getByText("CERTIFIED · GROUNDED").first().waitFor({ timeout: 45000 });
await page.waitForTimeout(900); // receipt fetch settles
await page.screenshot({ path: OUT + "t3_result.png", fullPage: true });

const answer = await page.locator("section >> text=reported total net sales").first()
  .innerText().catch(() => "(answer text not found)");
console.log("flow captured:", { t1: "initial", t2: "streaming", t3: "result" });
console.log("answer excerpt:", answer.slice(0, 120));
await browser.close();
