// Read-only layout audit: horizontal overflow, oversized elements, and
// text/background contrast on key surfaces. Run: node scripts/layout-audit.mjs
import { chromium } from "playwright";

const url = process.env.CONSOLE_URL || "http://localhost:5173/";
const browser = await chromium.launch({ channel: "msedge", headless: true });

for (const vp of [{ width: 1280, height: 900 }, { width: 390, height: 844 }]) {
  const page = await browser.newPage({ viewport: vp });
  await page.goto(url, { waitUntil: "domcontentloaded" });
  await page.getByText("backend live").first().waitFor({ timeout: 20000 });
  await page.waitForTimeout(400);
  const audit = await page.evaluate(() => {
    const vw = document.documentElement.clientWidth;
    const overflowX = document.documentElement.scrollWidth > vw;
    const wide = [];
    for (const el of document.querySelectorAll("body *")) {
      const r = el.getBoundingClientRect();
      if (r.width > vw + 2 && el.children.length === 0) {
        wide.push((el.textContent || el.className || el.tagName).slice(0, 40));
      }
    }
    const lum = (c) => {
      const m = c.match(/(\d+(\.\d+)?)/g);
      if (!m) return 0;
      const [r, g, b] = m.map(Number);
      const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
      return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
    };
    const pairs = [];
    for (const sel of ["h1", "p", ".pill", "label", "footer span"]) {
      const el = document.querySelector(sel);
      if (!el) continue;
      const cs = getComputedStyle(el);
      let bg = el, bgc = "rgb(10,11,15)";
      while (bg && bg !== document.documentElement) {
        const b = getComputedStyle(bg).backgroundColor;
        if (b && b !== "rgba(0, 0, 0, 0)") { bgc = b; break; }
        bg = bg.parentElement;
      }
      const ratio = ((l1, l2) => {
        const [a, b] = [l1, l2].sort((x, y) => y - x);
        return ((a + 0.05) / (b + 0.05)).toFixed(2);
      })(lum(cs.color), lum(bgc));
      pairs.push(`${sel}: contrast ${ratio}:1 (${cs.color} on ${bgc})`);
    }
    return { viewport: vw, overflowX, wideCount: wide.length, wide: wide.slice(0, 5), pairs };
  });
  console.log(`--- viewport ${vp.width}px ---`);
  console.log(JSON.stringify(audit, null, 1));
  await page.screenshot({ path: `../gui_shots/t4_${vp.width}.png`, fullPage: vp.width < 500 });
  await page.close();
}
await browser.close();
