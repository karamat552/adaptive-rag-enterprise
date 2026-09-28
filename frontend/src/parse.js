// Deterministic parsers for the FastPath template family (fact_templates.py).
// FAIL CLOSED: any parse miss returns null and the UI falls back to prose —
// the same posture as the gates themselves. These parsers are anchored to
// the KNOWN template shapes and never guess at LLM prose.

const UNIT_ABBR = { million: "M", billion: "B", thousand: "K" };

const FIG = "\\$([\\d,]+(?:\\.\\d+)?)\\s?(million|billion|thousand)?";

const PERIOD = "((?:Q[1-4]|FY)-\\d{4})";

/** Single-metric FastPath answer → hero figure.
 *  "Apple reported total net sales of $89,498 million for Q4-2023 [1]." */
export function parseHero(answer) {
  if (!answer || /^For /.test(answer)) return null;   // comparative, not single
  const m = answer.match(
    new RegExp(`^([A-Za-z][\\w'.& ]*?) reported (.+?) of ${FIG} for ${PERIOD}`));
  if (!m) return null;
  const [, company, label, figure, unit, period] = m;
  const prior = answer.match(
    new RegExp(`up from ${FIG} in ${PERIOD}`));
  const delta = answer.match(/a ([\d.]+)% (increase|decrease) year over year/);
  return {
    company: company.trim(),
    label: label.trim().replace(/^(the )/, ""),
    figure, unit: unit || "", period,
    prior: prior
      ? { figure: prior[1], unit: prior[2] || "", period: prior[3] }
      : null,
    delta: delta ? { pct: delta[1], dir: delta[2] } : null,
  };
}

/** Multi-entity comparative FastPath answer → KPI cards.
 *  "For Q4-2023: Apple reported total net sales of $89,498 million [1];
 *   Tesla reported total revenues of $25,167 million [2]." */
export function parseComparative(answer) {
  if (!answer || !/^For /.test(answer)) return null;
  const pm = answer.match(new RegExp(`^For ${PERIOD}:`));
  if (!pm) return null;
  const period = pm[1];
  const re = new RegExp(
    `([A-Za-z][\\w'.& ]*?) reported (.+?) of ${FIG}\\s*\\[(\\d+)\\]`, "g");
  const cards = [];
  let m;
  while ((m = re.exec(answer)) !== null) {
    cards.push({
      company: m[1].trim(),
      label: m[2].trim().replace(/^(the )/, ""),
      figure: m[3], unit: m[4] || "", cite: m[5],
    });
  }
  if (cards.length < 2) return null;                    // fail closed
  // normalized USD + symmetric deltas (2 entities) or base-relative (3+)
  cards.forEach((c) => {
    c.usd = parseFloat(c.figure.replace(/,/g, "")) *
      ({ million: 1e6, billion: 1e9, thousand: 1e3 }[c.unit] || 1);
  });
  cards.forEach((c, i) => {
    const base = cards.length === 2 ? cards[1 - i] : (i === 0 ? cards[1] : cards[0]);
    const pct = (c.usd - base.usd) / base.usd * 100;
    c.deltaPct = Math.round(pct * 10) / 10;
    c.deltaVs = base.company;
  });
  return { period, cards };
}

export const unitAbbr = (u) => UNIT_ABBR[u] || "";
