/**
 * jr.suki.ing delay tagging helper.
 *
 * Fetches the baked data/delays.json (GitHub Pages / jsDelivr) and exposes
 * lookup by tmaps key. The baker emits candidates ranked by station-sequence
 * score; when several recurring services of the same stopping pattern tie,
 * pass the train's own departure minutes (HH:MM) to pick the run whose time
 * is closest - the site knows its own times, the baker does not.
 *
 * Zero dependencies, ~1 KB.
 */

export async function loadDelays(src = "/data/delays.json") {
  const res = await fetch(src, { cache: "no-cache" });
  if (!res.ok) throw new Error(`delays ${res.status}`);
  const board = await res.json();
  const byKey = new Map(Object.entries(board.by_suki_key || {}));
  return {
    board,
    generatedAt: board.generated_at,
    delayedCount: board.delayed_count,
    /** Raw slot for a tmaps key, or null. */
    get(key) {
      return byKey.get(String(key)) || null;
    },
    /**
     * Resolve the delay for one of your trains.
     * @param {string} key   tmaps key, e.g. "10780"
     * @param {object} train your train object (used for time tie-breaks)
     * @param {number} [train.depMin] departure minutes from midnight, HH*60+MM
     */
    for(key, train = {}) {
      const slot = byKey.get(String(key));
      if (!slot) return null;
      const base = {
        key: slot.key,
        line: slot.line,
        train: slot.train, // ODPT number, e.g. "346M"
        delayMin: slot.delayMin,
        ambiguous: slot.ambiguous,
      };
      if (!slot.ambiguous) return base;
      const alts = [{ train: slot.train, dep: slot.dep }, ...slot.alternatives];
      if (typeof train.depMin === "number") {
        alts.sort(
          (a, b) =>
            Math.abs((a.dep ?? 0) - train.depMin) - Math.abs((b.dep ?? 0) - train.depMin)
        );
        return { ...base, train: alts[0].train, confidence: "time" };
      }
      return base; // caller decides how to render ambiguity
    },
  };
}
