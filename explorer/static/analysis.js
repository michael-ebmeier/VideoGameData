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
  // `mask` (from sliceMask) limits everything to a slice of the games, e.g. one era or some platforms.
  const opt = o => (o && typeof o === "object"
    ? { scoped: !!o.scoped, trim: !!o.trim, mask: o.mask || null, maskKey: o.mask ? String(o.maskKey || "") : "" }
    : { scoped: !!o, trim: false, mask: null, maskKey: "" });
  const TRIM_SHARE = 0.01;

  /* Per-measure integer columns (NULL where a game doesn't count), built once per table and option set. */
  function prepare(G, options) {
    const o = opt(options), scoped = o.scoped && !!G.rated;
    const key = (scoped ? "rated" : "all") + (o.trim ? "+trim" : "") + (o.mask ? "+m" + o.maskKey : "");
    G._an = G._an || {};
    if (G._an[key]) return G._an[key];
    const n = G.n, raw = {}, max = {}, trims = {};
    const skip = scoped ? G.rated : null, mask = o.mask;     // games outside the scope or the slice count for nothing
    const keys = Object.keys(G._an);                         // each entry is ~7 MB at full size: keep the latest few
    if (keys.length >= 5) delete G._an[keys[0]];
    for (const k of Object.keys(METRICS)) { raw[k] = new Int32Array(n).fill(NULL); max[k] = 0; }
    for (let i = 0; i < n; i++) {
      if ((skip && !skip[i]) || (mask && !mask[i])) continue;
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
    const o = opt(options), scoped = o.scoped, mask = o.mask;
    const names = key => new Map((key === "genre" ? facets.genres : facets.platforms).map(r => [r.id, r.name]));
    const rl = rowKey === "genre" ? G.genres : G.platforms, cl = colKey === "genre" ? G.genres : G.platforms;
    const skip = scoped && G.rated ? G.rated : null;
    const included = i => !((skip && !skip[i]) || (mask && !mask[i]));
    // first pass: how many games in the slice have each category, to choose the 12 largest of each kind
    const count = (lists, known) => { const c = new Map(); for (let i = 0; i < G.n; i++) if (included(i)) for (const id of lists[i]) if (known.has(id)) c.set(id, (c.get(id) || 0) + 1); return c; };
    const rn = names(rowKey), cn = names(colKey), rc = count(rl, rn), cc = rowKey === colKey ? rc : count(cl, cn);
    const top = (nm, c) => [...c].map(([id, n]) => ({ id, name: nm.get(id), n })).sort((p, q) => q.n - p.n || p.name.localeCompare(q.name)).slice(0, 12);
    const rows = top(rn, rc), cols = top(cn, cc);
    const rowAt = new Map(rows.map((r, i) => [r.id, i])), colAt = new Map(cols.map((c, j) => [c.id, j]));
    const cells = rows.map(() => new Array(cols.length).fill(0));
    const rowN = new Array(rows.length).fill(0), colN = new Array(cols.length).fill(0);
    let total = 0;                                           // games in the slice
    for (let i = 0; i < G.n; i++) {
      if (!included(i)) continue;
      total++;
      for (const b of cl[i]) { const c = colAt.get(b); if (c !== undefined) colN[c]++; }
      for (const a of rl[i]) {
        const r = rowAt.get(a); if (r === undefined) continue;
        rowN[r]++;
        for (const b of cl[i]) { const c = colAt.get(b); if (c !== undefined) cells[r][c]++; }
      }
    }
    // share of a row's games that have the column's category, the share of ALL games that have it, and the ratio
    const pct = cells.map((line, r) => line.map(v => (rowN[r] ? (100 * v) / rowN[r] : 0)));
    const base = colN.map(n => (total ? (100 * n) / total : 0));
    const lift = pct.map(line => line.map((v, c) => (base[c] > 0 ? v / base[c] : null)));
    return { type: "grid", rowKey, colKey, show, rows, cols, cells, rowTotal: rowN, colTotal: colN, total, pct, base, lift };
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

  /* ----- slices: eras, platforms, genres ----- */
  const anyIn = (list, set) => { for (let k = 0; k < list.length; k++) if (set.has(list[k])) return true; return false; };

  /* A mask of the games in a slice {from, to, platforms: [ids], genres: [ids]} (and in the scope), or null when
     the slice restricts nothing. Empty lists and null years mean "no restriction". Games with an unknown release
     year are left out as soon as a year limit is set. Anything in a list matches ("any of these platforms"). */
  function sliceMask(G, slice, scoped) {
    const sl = slice || {};
    const plats = sl.platforms && sl.platforms.length ? new Set(sl.platforms) : null;
    const gens = sl.genres && sl.genres.length ? new Set(sl.genres) : null;
    const from = Number.isInteger(sl.from) ? sl.from : null, to = Number.isInteger(sl.to) ? sl.to : null;
    if (!plats && !gens && from === null && to === null) return null;
    const sc = !!(scoped && G.rated);
    const key = [from, to, plats ? [...plats].sort((a, b) => a - b).join("+") : "", gens ? [...gens].sort((a, b) => a - b).join("+") : "", sc ? "r" : "a"].join("|");
    G._masks = G._masks || new Map();
    if (G._masks.has(key)) return G._masks.get(key);
    const mask = new Uint8Array(G.n), rated = sc ? G.rated : null;
    let count = 0;
    for (let i = 0; i < G.n; i++) {
      if (rated && !rated[i]) continue;
      if (from !== null || to !== null) {
        const y = G.year[i];
        if (y === NULL || (from !== null && y < from) || (to !== null && y > to)) continue;
      }
      if (plats && !anyIn(G.platforms[i], plats)) continue;
      if (gens && !anyIn(G.genres[i], gens)) continue;
      mask[i] = 1; count++;
    }
    if (G._masks.size >= 6) G._masks.delete(G._masks.keys().next().value);
    const res = { mask, count, key };
    G._masks.set(key, res);
    return res;
  }

  /* How many games are in the scope (all of them, or only those with an ESRB rating). */
  const scopeCount = (G, scoped) => { if (!(scoped && G.rated)) return G.n; let n = 0; for (let i = 0; i < G.n; i++) if (G.rated[i]) n++; return n; };

  const medianOf = a => { if (!a.length) return null; const b = Int32Array.from(a).sort(); const h = b.length >> 1; return b.length % 2 ? b[h] : (b[h - 1] + b[h]) / 2; };
  const TTB_EDGES = [20, 50, 100, 200, 400, 800];            // tenths of an hour: 2, 5, 10, 20, 40 and 80 hours
  const TTB_LABELS = ["Under 2 h", "2–5 h", "5–10 h", "10–20 h", "20–40 h", "40–80 h", "80+ h"];
  const MIN_GENRE_GAMES = 30, MIN_YEAR_GAMES = 20;

  /* Everything the Stats page shows, for the games in a scope and slice. */
  function summarize(G, facets, options) {
    const o = opt(options), rated = o.scoped && G.rated ? G.rated : null, mask = o.mask;
    const today = Math.floor(Date.now() / 86400000);
    const totals = { games: 0, rated: 0, covered: 0, timed: 0, first_year: null, last_year: null };
    const perYear = new Map(), ratings = new Map(), genreN = new Map(), platN = new Map();
    const buckets = new Array(7).fill(0), ttbGenre = new Map(), ttbYear = new Map();
    const gnames = new Map(facets.genres.map(r => [r.id, r.name])), pnames = new Map(facets.platforms.map(r => [r.id, r.name]));
    for (let i = 0; i < G.n; i++) {
      if ((rated && !rated[i]) || (mask && !mask[i])) continue;
      totals.games++;
      if (G.rating[i] >= 0) totals.rated++;
      if (G.cover[i]) totals.covered++;
      const y = G.year[i];
      if (y !== NULL) {
        if (G.rel[i] !== NULL && G.rel[i] <= today) {
          if (totals.first_year === null || y < totals.first_year) totals.first_year = y;
          if (totals.last_year === null || y > totals.last_year) totals.last_year = y;
        }
        if (y >= 1970 && y <= THIS_YEAR) perYear.set(y, (perYear.get(y) || 0) + 1);
      }
      if (G.rating[i] >= 0 && G.votes[i] >= MIN_RATINGS) {
        const b = Math.min(Math.floor(G.rating[i] / 1000), 9) * 10;
        ratings.set(b, (ratings.get(b) || 0) + 1);
      }
      for (const id of G.genres[i]) if (gnames.has(id)) genreN.set(id, (genreN.get(id) || 0) + 1);
      for (const id of G.platforms[i]) if (pnames.has(id)) platN.set(id, (platN.get(id) || 0) + 1);
      if (G.ttb[i] >= 0 && G.ttbn[i] >= MIN_REPORTS) {
        const t = G.ttb[i];
        totals.timed++;
        let b = 0; while (b < TTB_EDGES.length && t >= TTB_EDGES[b]) b++;
        buckets[b]++;
        for (const id of G.genres[i]) if (gnames.has(id)) { if (!ttbGenre.has(id)) ttbGenre.set(id, []); ttbGenre.get(id).push(t); }
        if (y !== NULL && y >= 1985 && y <= THIS_YEAR) { if (!ttbYear.has(y)) ttbYear.set(y, []); ttbYear.get(y).push(t); }
      }
    }
    const round1 = v => Math.round(v * 10) / 10;
    const topN = (m, names) => [...m].map(([id, n]) => ({ label: names.get(id), value: n, id })).sort((p, q) => q.value - p.value || p.id - q.id).slice(0, 15);
    return {
      totals,
      per_year: [...perYear].sort((p, q) => p[0] - q[0]).map(([label, value]) => ({ label, value })),
      ratings: [...ratings].sort((p, q) => p[0] - q[0]).map(([label, value]) => ({ label, value })),
      top_genres: topN(genreN, gnames), top_platforms: topN(platN, pnames),
      ttb_buckets: totals.timed ? TTB_LABELS.map((label, i) => ({ label, value: buckets[i] })) : [],
      ttb_by_genre: [...ttbGenre].filter(([, a]) => a.length >= MIN_GENRE_GAMES)
        .map(([id, a]) => ({ label: gnames.get(id), value: round1(medianOf(a) / 10), n: a.length }))
        .sort((p, q) => q.value - p.value || p.label.localeCompare(q.label)).slice(0, 15),
      ttb_by_year: [...ttbYear].filter(([, a]) => a.length >= MIN_YEAR_GAMES).sort((p, q) => p[0] - q[0])
        .map(([label, a]) => ({ label, value: round1(medianOf(a) / 10), n: a.length })),
    };
  }

  /* Each platform's game count and the median release year of its games, within a scope and slice.
     The median year is a rough "age" of the platform, used to put platforms in order from old to new. */
  function platformAges(G, facets, options) {
    const o = opt(options), rated = o.scoped && G.rated ? G.rated : null, mask = o.mask;
    const names = new Map(facets.platforms.map(r => [r.id, r.name])), years = new Map(), count = new Map();
    for (let i = 0; i < G.n; i++) {
      if ((rated && !rated[i]) || (mask && !mask[i])) continue;
      const y = G.year[i];
      for (const id of G.platforms[i]) {
        if (!names.has(id)) continue;
        count.set(id, (count.get(id) || 0) + 1);
        if (y !== NULL && y >= 1970 && y <= THIS_YEAR) { if (!years.has(id)) years.set(id, []); years.get(id).push(y); }
      }
    }
    return [...count].map(([id, n]) => ({ id, name: names.get(id), n, median: years.has(id) ? medianOf(years.get(id)) : null }));
  }

  /*
   * How the genre mix changes: the share of games in each genre, column by column, where the columns are eras
   * (release-year bins) or platforms (oldest first). cfg: {across: "era" | "platform", bin: years per era,
   * platformIds: which platforms (default: the 12 largest), order: "age" | "size", minGames: smallest column
   * that gets numbers, options: scope/slice}. `change` is the last column's share minus the first's, in
   * percentage points, using the first and last columns that are big enough.
   */
  /* The columns of a comparison (release eras, or platforms oldest to newest), and colOf(i) -> the column indexes game i falls in.
     `mk(base)` adds the caller's own fields to each column. */
  function makeColumns(G, facets, cfg, o, included, mk) {
    let cols = [], colOf;
    if (cfg.across === "platform") {
      const ages = platformAges(G, facets, options0(o));
      const byId = new Map(ages.map(a => [a.id, a]));
      let ids = (cfg.platformIds || []).filter(id => byId.has(id));
      if (!ids.length) ids = ages.slice().sort((p, q) => q.n - p.n || p.name.localeCompare(q.name)).slice(0, 12).map(a => a.id);
      const picked = ids.map(id => byId.get(id));
      picked.sort(cfg.order === "size" ? (p, q) => q.n - p.n || p.name.localeCompare(q.name)
        : (p, q) => (p.median ?? 1e9) - (q.median ?? 1e9) || q.n - p.n || p.name.localeCompare(q.name));
      cols = picked.map(a => ({ key: a.id, label: a.name, age: a.median, n: 0, ...mk() }));
      const at = new Map(cols.map((c, j) => [c.key, j]));
      colOf = i => { const r = []; for (const id of G.platforms[i]) { const j = at.get(id); if (j !== undefined) r.push(j); } return r; };
    } else {
      const bin = cfg.bin === 5 ? 5 : 10;
      let lo = Infinity, hi = -Infinity;
      for (let i = 0; i < G.n; i++) if (included(i)) { const y = G.year[i]; if (y !== NULL && y >= 1970 && y <= THIS_YEAR) { if (y < lo) lo = y; if (y > hi) hi = y; } }
      if (lo <= hi) for (let s0 = Math.floor(lo / bin) * bin; s0 <= hi; s0 += bin)
        cols.push({ key: s0, label: bin === 10 ? `${s0}s` : `${s0}–${String(s0 + bin - 1).slice(2)}`, from: s0, to: s0 + bin - 1, n: 0, ...mk() });
      const start = cols.length ? cols[0].from : 0;
      colOf = i => { const y = G.year[i]; if (y === NULL || y < 1970 || y > THIS_YEAR) return []; const j = Math.floor((y - start) / bin); return j >= 0 && j < cols.length ? [j] : []; };
    }
    return { cols, colOf };
  }
  function compareShares(G, facets, cfg) {
    const o = opt(cfg.options), rated = o.scoped && G.rated ? G.rated : null, mask = o.mask;
    const minGames = cfg.minGames || 30, genres = facets.genres, gIndex = new Map(genres.map((g, i) => [g.id, i]));
    const included = i => !((rated && !rated[i]) || (mask && !mask[i]));
    const { cols, colOf } = makeColumns(G, facets, cfg, o, included, () => ({ counts: new Int32Array(genres.length) }));
    const overall = new Int32Array(genres.length);
    let nAll = 0;
    for (let i = 0; i < G.n; i++) {
      if (!included(i)) continue;
      const cs = colOf(i);
      if (!cs.length) continue;
      nAll++;
      for (const id of G.genres[i]) { const k = gIndex.get(id); if (k !== undefined) overall[k]++; }
      for (const j of cs) {
        cols[j].n++;
        for (const id of G.genres[i]) { const k = gIndex.get(id); if (k !== undefined) cols[j].counts[k]++; }
      }
    }
    const valid = cols.map((c, j) => (c.n >= minGames ? j : -1)).filter(j => j >= 0);
    const first = valid.length > 1 ? valid[0] : -1, last = valid.length > 1 ? valid[valid.length - 1] : -1;
    let rows = genres.map((g, k) => {
      const shares = cols.map(c => (c.n >= minGames ? (100 * c.counts[k]) / c.n : null));
      return { id: g.id, name: g.name, overall: nAll ? (100 * overall[k]) / nAll : 0, shares, counts: cols.map(c => c.counts[k]),
               change: first >= 0 ? shares[last] - shares[first] : null };
    }).filter(r => overall[gIndex.get(r.id)] > 0);
    const by = cfg.sort || "share";
    rows.sort(by === "rise" ? (p, q) => (q.change ?? -1e9) - (p.change ?? -1e9) || q.overall - p.overall || p.name.localeCompare(q.name)
      : by === "fall" ? (p, q) => (p.change ?? 1e9) - (q.change ?? 1e9) || q.overall - p.overall || p.name.localeCompare(q.name)
      : (p, q) => q.overall - p.overall || p.name.localeCompare(q.name));
    return { across: cfg.across === "platform" ? "platform" : "era", cols, rows, nAll, minGames, first, last, valid };
  }

  /* How game length (time to beat) moves across eras or platforms: the median hours for each genre in each column, plus an
     "all games" row. Only games with MIN_REPORTS+ time reports count. cfg like compareShares plus minCell (8) and minCol (20). */
  function compareLengths(G, facets, cfg) {
    const o = opt(cfg.options), rated = o.scoped && G.rated ? G.rated : null, mask = o.mask;
    const minCell = cfg.minCell || 8, minCol = cfg.minCol || 20, minRow = cfg.minRow || 30;
    const genres = facets.genres, gIndex = new Map(genres.map((g, i) => [g.id, i]));
    const included = i => !((rated && !rated[i]) || (mask && !mask[i]));
    const { cols, colOf } = makeColumns(G, facets, cfg, o, included, () => ({ timed: 0, all: [], by: new Array(genres.length) }));
    const overall = genres.map(() => []), overallAll = [];
    for (let i = 0; i < G.n; i++) {
      if (!included(i) || !(G.ttb[i] >= 1 && G.ttbn[i] >= MIN_REPORTS)) continue;
      const cs = colOf(i);
      if (!cs.length) continue;
      const hrs = G.ttb[i] / 10;
      overallAll.push(hrs);
      const ks = [];
      for (const id of G.genres[i]) { const k = gIndex.get(id); if (k !== undefined) { overall[k].push(hrs); ks.push(k); } }
      for (const j of cs) {
        const c = cols[j]; c.timed++; c.all.push(hrs);
        for (const k of ks) (c.by[k] || (c.by[k] = [])).push(hrs);
      }
    }
    const median = a => { if (!a || !a.length) return null; const s = Float64Array.from(a).sort(); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };
    for (const c of cols) c.n = c.timed;
    const valid = cols.map((c, j) => (c.timed >= minCol ? j : -1)).filter(j => j >= 0);
    const first = valid.length > 1 ? valid[0] : -1, last = valid.length > 1 ? valid[valid.length - 1] : -1;
    const line = (id, name, perCol, total) => {
      const meds = cols.map((c, j) => (perCol(c).length >= minCell && c.timed >= minCol ? median(perCol(c)) : null));
      const ok = first >= 0 && meds[first] != null && meds[last] != null;
      return { id, name, overall: median(total), meds, counts: cols.map(c => perCol(c).length), n: total.length,
               delta: ok ? meds[last] - meds[first] : null, change: ok && meds[first] > 0 ? (100 * (meds[last] - meds[first])) / meds[first] : null };
    };
    const all = line(0, "All games", c => c.all, overallAll);
    let rows = genres.map((g, k) => line(g.id, g.name, c => c.by[k] || [], overall[k])).filter(r => r.n >= minRow);
    const by = cfg.sort || "size";
    const cmp = { size: (p, q) => q.n - p.n, long: (p, q) => q.overall - p.overall, short: (p, q) => p.overall - q.overall,
                  rise: (p, q) => (q.change ?? -1e9) - (p.change ?? -1e9), fall: (p, q) => (p.change ?? 1e9) - (q.change ?? 1e9) }[by] || ((p, q) => q.n - p.n);
    rows.sort((p, q) => cmp(p, q) || q.n - p.n || p.name.localeCompare(q.name));
    return { across: cfg.across === "platform" ? "platform" : "era", cols, rows, all, nTimed: overallAll.length, minCell, minCol, first, last, valid };
  }
  // the scope-and-slice options again, without any trim (the trim only concerns the relationship analysis)
  const options0 = o => ({ scoped: o.scoped, mask: o.mask, maskKey: o.maskKey });

  /* What the trim left out: {measure: {lo, hi, dropped, n}} with lo/hi in the measure's own units. */
  const trimInfo = (G, options) => prepare(G, options).trims;

  window.IGDB_ANALYSIS = { sliceMask, scopeCount, summarize, platformAges, compareShares, compareLengths, trimInfo, buildColumns, METRICS, CATEGORIES, isCat, MIN_CAT_N, MIN_RATINGS, MIN_REPORTS, MAX_POINTS,
                           strength, scatter, categoryBars, crossTab, correlationMatrix, averageRanks, pearson };
})();
