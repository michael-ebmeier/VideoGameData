/*
 * Relationship analysis over the games table: correlations between measures (rating, time to beat,
 * release year, popularity, number of platforms) and comparisons by genre or platform. Everything
 * runs in the browser on the same per-game columns that power search (data/<version>/games.dat on
 * the static site, /api/columns on the local Python explorer), so there is one implementation.
 *
 * Measures and the games that count for each:
 *   rating     the average user + critic rating, for games with at least MIN_RATINGS ratings
 *   ttb        normal-playthrough time to beat, for games with at least MIN_REPORTS reports
 *   year       release year (1970 to this year; future dates are left out)
 *   votes      number of ratings (at least 1)
 *   platforms  how many platforms the game is on
 * Time to beat and number of ratings are heavily skewed, so they are analysed and plotted on a
 * log scale. Spearman's rho (rank correlation) ignores scale and outliers altogether.
 */
"use strict";
(function () {
  const NULL = -2147483648;                  // "no value" in the integer columns
  const MIN_RATINGS = 5, MIN_REPORTS = 3;
  const MIN_CAT_N = 30;                      // a genre/platform needs this many games to be compared
  const MAX_POINTS = 1200;                   // scatter points drawn (statistics always use every game)
  const THIS_YEAR = new Date().getUTCFullYear();

  /* Turn the parsed games table (parallel arrays) into typed columns. */
  function buildColumns(d) {
    const n = d.id.length;
    const ints = (arr, none) => Int32Array.from(arr, v => (v === null ? none : v));
    return {
      n,
      id: Int32Array.from(d.id),
      name: d.name,
      year: ints(d.year, NULL),
      rel: ints(d.rel, NULL),
      rating: ints(d.rating, -1),            // hundredths of a point (68.62 is 6862)
      votes: ints(d.votes, -1),
      // Time to beat: tenths of an hour (12.5 h is 125) and the number of reports behind it.
      // Data built before this existed has neither column, which means "no time-to-beat data".
      ttb: d.ttb ? ints(d.ttb, -1) : new Int32Array(n).fill(-1),
      ttbn: d.ttbn ? ints(d.ttbn, -1) : new Int32Array(n).fill(-1),
      cover: d.cover,
      genres: d.genres,
      platforms: d.platforms,
      // 1 for games with an ESRB rating (the default scope); missing when the data has no ratings
      rated: d.rated ? Int8Array.from(d.rated) : null,
    };
  }

  const METRICS = {
    rating:    { label: "Average rating",      phrase: "rating", short: "Rating",       log: false, value: r => r / 100, note: `games with ${MIN_RATINGS}+ ratings` },
    ttb:       { label: "Time to beat", phrase: "time to beat",        short: "Time to beat", log: true, trim: true,  value: r => r / 10,  note: `games with ${MIN_REPORTS}+ reports` },
    year:      { label: "Release year", phrase: "release year",        short: "Year",         log: false, value: r => r,       note: "released games" },
    votes:     { label: "Number of ratings", phrase: "number of ratings",   short: "Ratings",      log: true, trim: true,  value: r => r,       note: "games with at least 1 rating" },
    platforms: { label: "Number of platforms", phrase: "number of platforms", short: "Platforms",    log: false, value: r => r,       note: "all games" },
  };
  const CATEGORIES = { genre: "Genre", platform: "Platform" };
  const isCat = key => key in CATEGORIES;
  const transform = (key, raw) => { const v = METRICS[key].value(raw); return METRICS[key].log ? Math.log10(v) : v; };

  // Analysis options: `scoped` limits to games with an ESRB rating; `trim` leaves out the lowest and highest 1%
  // of the heavy-tailed measures (time to beat, number of ratings). A bare boolean means just `scoped`.
  const opt = o => (o && typeof o === "object" ? { scoped: !!o.scoped, trim: !!o.trim } : { scoped: !!o, trim: false });
  const TRIM_SHARE = 0.01;

  /* Per-measure integer columns (NULL where a game doesn't count), built once per table and option set. */
  function prepare(G, options) {
    const o = opt(options), scoped = o.scoped && !!G.rated;
    const key = (scoped ? "rated" : "all") + (o.trim ? "+trim" : "");
    G._an = G._an || {};
    if (G._an[key]) return G._an[key];
    const n = G.n, raw = {}, max = {}, trims = {};
    const skip = scoped ? G.rated : null;                    // games outside the scope count for nothing
    for (const k of Object.keys(METRICS)) { raw[k] = new Int32Array(n).fill(NULL); max[k] = 0; }
    for (let i = 0; i < n; i++) {
      if (skip && !skip[i]) continue;
      const y = G.year[i];
      if (y !== NULL && y >= 1970 && y <= THIS_YEAR) raw.year[i] = y;
      if (G.rating[i] >= 0 && G.votes[i] >= MIN_RATINGS) raw.rating[i] = G.rating[i];
      if (G.ttb[i] >= 1 && G.ttbn[i] >= MIN_REPORTS) raw.ttb[i] = G.ttb[i];
      if (G.votes[i] >= 1) raw.votes[i] = G.votes[i];
      const np = G.platforms[i].length;
      if (np >= 1) raw.platforms[i] = np;
    }
    if (o.trim) {
      for (const k of Object.keys(METRICS)) {
        if (!METRICS[k].trim) continue;
        const vals = [];
        for (let i = 0; i < n; i++) if (raw[k][i] !== NULL) vals.push(raw[k][i]);
        if (vals.length < 2) continue;
        const sorted = Int32Array.from(vals).sort();         // typed arrays sort numerically
        const lo = sorted[Math.floor(TRIM_SHARE * (sorted.length - 1))], hi = sorted[Math.ceil((1 - TRIM_SHARE) * (sorted.length - 1))];
        let dropped = 0;
        for (let i = 0; i < n; i++) if (raw[k][i] !== NULL && (raw[k][i] < lo || raw[k][i] > hi)) { raw[k][i] = NULL; dropped++; }
        // ties at the cut-off are kept, so a lot of identical values never gets trimmed away
        if (dropped) trims[k] = { lo: METRICS[k].value(lo), hi: METRICS[k].value(hi), dropped, n: vals.length };
      }
    }
    for (const k of Object.keys(METRICS)) for (let i = 0; i < n; i++) if (raw[k][i] > max[k]) max[k] = raw[k][i];
    return (G._an[key] = { raw, max, pairs: {}, trims });
  }

  /* ----- statistics ----- */
  function pearson(xs, ys) {
    const n = xs.length;
    if (n < 3) return null;
    let sx = 0, sy = 0;
    for (let i = 0; i < n; i++) { sx += xs[i]; sy += ys[i]; }
    const mx = sx / n, my = sy / n;
    let sxy = 0, sxx = 0, syy = 0;
    for (let i = 0; i < n; i++) { const dx = xs[i] - mx, dy = ys[i] - my; sxy += dx * dy; sxx += dx * dx; syy += dy * dy; }
    if (sxx === 0 || syy === 0) return null;
    return { r: sxy / Math.sqrt(sxx * syy), slope: sxy / sxx, intercept: my - (sxy / sxx) * mx };
  }

  /* Average ranks of small non-negative integers by counting (O(n + max)); ties share their mean rank. */
  function averageRanks(vals, max) {
    const cnt = new Float64Array(max + 2);
    for (let i = 0; i < vals.length; i++) cnt[vals[i] + 1]++;
    for (let v = 1; v < cnt.length; v++) cnt[v] += cnt[v - 1];      // cnt[v] = how many values are below v
    const out = new Float64Array(vals.length);
    for (let i = 0; i < vals.length; i++) out[i] = (cnt[vals[i]] + 1 + cnt[vals[i] + 1]) / 2;
    return out;
  }

  /* Both coefficients for a pair of measures, over the games that count for both. */
  function pairData(G, a, b, keepPoints, options) {
    const A = prepare(G, options), ra = A.raw[a], rb = A.raw[b];
    const cached = A.pairs[a + "|" + b];
    if (cached && !keepPoints) return cached;
    let m = 0;
    for (let i = 0; i < G.n; i++) if (ra[i] !== NULL && rb[i] !== NULL) m++;
    const idx = new Int32Array(m), xs = new Float64Array(m), ys = new Float64Array(m);
    const rawA = new Int32Array(m), rawB = new Int32Array(m);
    for (let i = 0, k = 0; i < G.n; i++) {
      if (ra[i] === NULL || rb[i] === NULL) continue;
      idx[k] = i; rawA[k] = ra[i]; rawB[k] = rb[i];
      xs[k] = transform(a, ra[i]); ys[k] = transform(b, rb[i]);
      k++;
    }
    const p = pearson(xs, ys);
    const s = m >= 3 ? pearson(averageRanks(rawA, A.max[a]), averageRanks(rawB, A.max[b])) : null;
    const stats = { n: m, pearson: p && p.r, spearman: s && s.r, slope: p && p.slope, intercept: p && p.intercept };
    A.pairs[a + "|" + b] = stats;
    return keepPoints ? { ...stats, idx, xs, ys } : stats;
  }

  const strength = r => {
    const a = Math.abs(r);
    return (a < 0.1 ? "negligible" : a < 0.3 ? "weak" : a < 0.5 ? "moderate" : a < 0.7 ? "strong" : "very strong") +
      (a < 0.1 ? "" : r > 0 ? " positive" : " negative");
  };

  /* ----- the three kinds of comparison ----- */

  /* Two measures: scatter of a sample of games plus the coefficients over all of them. */
  function scatter(G, xk, yk, options) {
    const d = pairData(G, xk, yk, true, options);
    const out = { type: "scatter", xk, yk, n: d.n, pearson: d.pearson, spearman: d.spearman, slope: d.slope, intercept: d.intercept, points: [] };
    if (d.n < 3) return out;
    const take = Math.min(MAX_POINTS, d.n), step = d.n / take;
    let seed = 12345 + d.n;                                  // fixed seed: the same sample every time
    const rand = () => ((seed = (seed * 1664525 + 1013904223) >>> 0) / 4294967296);
    for (let k = 0; k < take; k++) {
      const j = Math.min(d.n - 1, Math.floor(k * step + rand() * step));
      out.points.push({ i: d.idx[j], x: d.xs[j], y: d.ys[j] });
    }
    let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
    for (let j = 0; j < d.n; j++) {
      if (d.xs[j] < x0) x0 = d.xs[j]; if (d.xs[j] > x1) x1 = d.xs[j];
      if (d.ys[j] < y0) y0 = d.ys[j]; if (d.ys[j] > y1) y1 = d.ys[j];
    }
    Object.assign(out, { x0, x1, y0, y1 });
    return out;
  }

  const mean = a => a.reduce((s, v) => s + v, 0) / a.length;
  const median = a => { const b = Float64Array.from(a).sort(); const h = b.length >> 1; return b.length % 2 ? b[h] : (b[h - 1] + b[h]) / 2; };
  const round1 = v => Math.round(v * 10) / 10;

  /* A genre or platform against one measure: the average (or median) per category. */
  function categoryBars(G, facets, catKey, numKey, agg, options) {
    const A = prepare(G, options), raw = A.raw[numKey];
    const lists = catKey === "genre" ? G.genres : G.platforms;
    const names = new Map((catKey === "genre" ? facets.genres : facets.platforms).map(r => [r.id, r.name]));
    const by = new Map(), all = [];
    for (let i = 0; i < G.n; i++) {
      if (raw[i] === NULL) continue;
      const v = METRICS[numKey].value(raw[i]);
      all.push(v);
      for (const id of lists[i]) if (names.has(id)) { if (!by.has(id)) by.set(id, []); by.get(id).push(v); }
    }
    const f = agg === "median" ? median : mean;
    const rows = [];
    for (const [id, arr] of by) if (arr.length >= MIN_CAT_N) rows.push({ id, label: names.get(id), value: round1(f(arr)), n: arr.length });
    rows.sort((p, q) => q.value - p.value || p.label.localeCompare(q.label));
    return { type: "bars", catKey, numKey, agg, rows: rows.slice(0, 20), omitted: Math.max(0, rows.length - 20),
             overall: all.length ? round1(f(all)) : null, n: all.length };
  }

  /* Two categories: how many games have both (a genre's overlap with itself is genre co-occurrence). */
  function crossTab(G, facets, rowKey, colKey, show, options) {
    const scoped = opt(options).scoped;
    const list = key => (key === "genre" ? facets.genres : facets.platforms).slice().sort((p, q) => q.n - p.n || p.name.localeCompare(q.name));
    const rows = list(rowKey).slice(0, 12), cols = list(colKey).slice(0, 12);
    const rowAt = new Map(rows.map((r, i) => [r.id, i])), colAt = new Map(cols.map((c, j) => [c.id, j]));
    const cells = rows.map(() => new Array(cols.length).fill(0));
    const rl = rowKey === "genre" ? G.genres : G.platforms, cl = colKey === "genre" ? G.genres : G.platforms;
    const skip = scoped && G.rated ? G.rated : null;
    for (let i = 0; i < G.n; i++) {
      if (skip && !skip[i]) continue;
      for (const a of rl[i]) { const r = rowAt.get(a); if (r === undefined) continue;
        for (const b of cl[i]) { const c = colAt.get(b); if (c !== undefined) cells[r][c]++; } }
    }
    const rowTotal = rows.map(r => r.n);
    return { type: "grid", rowKey, colKey, show, rows, cols, cells, rowTotal,
             pct: cells.map((line, r) => line.map(v => (rowTotal[r] ? (100 * v) / rowTotal[r] : 0))) };
  }

  /* Every pair among the chosen measures: the correlation matrix. */
  function correlationMatrix(G, keys, options) {
    const cells = keys.map(() => keys.map(() => null));
    keys.forEach((a, i) => keys.forEach((b, j) => {
      if (i === j) return;
      const [p, q] = i < j ? [a, b] : [b, a];
      cells[i][j] = pairData(G, p, q, false, options);
    }));
    return { keys, cells };
  }

  /* What the trim left out: {measure: {lo, hi, dropped, n}} with lo/hi in the measure's own units. */
  const trimInfo = (G, options) => prepare(G, options).trims;

  window.IGDB_ANALYSIS = { trimInfo, buildColumns, METRICS, CATEGORIES, isCat, MIN_CAT_N, MIN_RATINGS, MIN_REPORTS, MAX_POINTS,
                           strength, scatter, categoryBars, crossTab, correlationMatrix, averageRanks, pearson };
})();
