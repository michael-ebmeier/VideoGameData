/*
 * Browser-side replacement for the Python API, used by the static build (build_static.py).
 * It answers the same calls the UI makes (api/games, api/game/<id>, api/facets, api/stats)
 * from the data files published next to the page, so no server code runs on the host.
 *
 * Data layout (see build_static.py): data/manifest.json -> data/<version>/games.dat (all games
 * as parallel columns), facets.json, stats.json, and d/<n>.dat (details, 500 games per file).
 * Files ending in .dat are gzip'd JSON; they are decompressed here so the result does not
 * depend on how the web server is configured.
 */
"use strict";
(function () {
  const NULL = -2147483648;   // "no value" in the integer columns
  const PAGE = 48;
  const SHARD = 500;
  const cache = {};

  async function bytes(url, opts) {
    const res = await fetch(url, opts);
    if (!res.ok) throw new Error(`${url}: ${res.status} ${res.statusText}`);
    return new Uint8Array(await res.arrayBuffer());
  }
  async function parse(buf) {
    if (buf[0] === 0x1f && buf[1] === 0x8b) {                      // gzip magic number
      if (typeof DecompressionStream === "undefined")
        throw new Error("This browser is too old to open the data (needs a 2023-or-newer browser).");
      const stream = new Blob([buf]).stream().pipeThrough(new DecompressionStream("gzip"));
      return new Response(stream).json();
    }
    return JSON.parse(new TextDecoder().decode(buf));
  }
  const once = (key, make) => cache[key] || (cache[key] = make().catch(e => { delete cache[key]; throw e; }));

  const manifest = () => once("manifest", async () =>
    parse(await bytes("data/manifest.json", { cache: "no-store" })));
  const versioned = async path => `data/${(await manifest()).version}/${path}`;

  const games = () => once("games", async () => {
    const d = await parse(await bytes(await versioned("games.dat")));
    const G = IGDB_ANALYSIS.buildColumns(d);           // shared with the analysis code (analysis.js)
    G.lower = d.name.map(s => s.toLowerCase());
    G.orders = {};
    return G;
  });
  // facets.json / stats.json describe the default scope (games with an ESRB rating). When the data has
  // ratings there is also facets_all.json / stats_all.json for "wild west" (every game).
  const defaultFacets = () => once("facets", async () => parse(await bytes(await versioned("facets.json"))));
  const defaultStats = () => once("stats", async () => parse(await bytes(await versioned("stats.json"))));
  const facets = scope => (scope !== "all" ? defaultFacets() : once("facets-all", async () => {
    const base = await defaultFacets();
    return base.scope && base.scope.available ? parse(await bytes(await versioned("facets_all.json"))) : base;
  }));
  const stats = scope => (scope !== "all" ? defaultStats() : once("stats-all", async () => {
    const base = await defaultStats();
    return base.scope && base.scope.available ? parse(await bytes(await versioned("stats_all.json"))) : base;
  }));

  /* ----- search ----- */
  function cardAt(G, i) {
    return {
      id: G.id[i], name: G.name[i],
      year: G.year[i] === NULL ? null : G.year[i],
      rating: G.rating[i] < 0 ? null : G.rating[i] / 100,
      votes: G.votes[i] < 0 ? null : G.votes[i],
      cover: G.cover[i] || null,
      ttb: G.ttb[i] < 0 ? null : G.ttb[i] / 10,
    };
  }

  // Global sort orders, computed the first time they're needed and then kept.
  function orderFor(G, sort) {
    if (G.orders[sort]) return G.orders[sort];
    const idx = Int32Array.from({ length: G.n }, (_, i) => i);
    const desc = col => (a, b) => col[b] - col[a] || a - b;           // nulls are the smallest, so they come last
    const oldest = (a, b) => {
      const x = G.rel[a], y = G.rel[b];
      if (x === NULL || y === NULL) return x === y ? a - b : (x === NULL ? 1 : -1);
      return x - y || a - b;
    };
    const shortest = (a, b) => {                                      // nulls last
      const x = G.ttb[a], y = G.ttb[b];
      if (x < 0 || y < 0) return x === y ? a - b : (x < 0 ? 1 : -1);
      return x - y || a - b;
    };
    const cmp = {
      popular: desc(G.votes), rating: desc(G.rating), newest: desc(G.rel), oldest,
      shortest, longest: desc(G.ttb),
      name: (a, b) => (G.name[a] < G.name[b] ? -1 : G.name[a] > G.name[b] ? 1 : a - b),
    }[sort];
    return (G.orders[sort] = idx.sort(cmp));
  }

  async function search(p) {
    const G = await games();
    const text = String(p.q || "").trim().toLowerCase();
    const genre = /^\d+$/.test(p.genre || "") ? +p.genre : null;
    const platform = /^\d+$/.test(p.platform || "") ? +p.platform : null;
    const from = /^-?\d{1,4}$/.test(p.from || "") ? +p.from : null;
    const to = /^-?\d{1,4}$/.test(p.to || "") ? +p.to : null;
    const sort = ["popular", "rating", "newest", "oldest", "name", "shortest", "longest"].includes(p.sort) ? p.sort : "popular";
    const len = /^(\d{1,3})-(\d{0,3})$/.exec(p.length || "");     // hours, e.g. "10-20" or "40-"
    const lenMin = len ? +len[1] * 10 : null, lenMax = len && len[2] ? +len[2] * 10 : null;
    const MIN_REPORTS = 3;
    const minVotes = /^\d+$/.test(p.min_votes || "") ? +p.min_votes : (sort === "rating" ? 10 : 0);
    const today = Math.floor(Date.now() / 86400000);
    const page = /^\d+$/.test(p.page || "") ? Math.max(1, +p.page) : 1;
    const rated = p.scope !== "all" && G.rated ? G.rated : null;       // default scope: games with an ESRB rating

    const start = (page - 1) * PAGE, end = start + PAGE;
    const order = orderFor(G, sort);
    const out = [];
    let total = 0;
    for (let k = 0; k < order.length; k++) {
      const i = order[k];
      if (rated && !rated[i]) continue;
      if (text && !G.lower[i].includes(text)) continue;
      if (genre !== null && !G.genres[i].includes(genre)) continue;
      if (platform !== null && !G.platforms[i].includes(platform)) continue;
      if (from !== null && (G.year[i] === NULL || G.year[i] < from)) continue;
      if (to !== null && (G.year[i] === NULL || G.year[i] > to)) continue;
      if (sort === "newest" && (G.rel[i] === NULL || G.rel[i] > today)) continue;
      if (minVotes && G.votes[i] < minVotes) continue;
      if (len && (G.ttb[i] < 0 || G.ttb[i] < lenMin || (lenMax !== null && G.ttb[i] >= lenMax))) continue;
      if ((sort === "shortest" || sort === "longest") && (G.ttb[i] < 0 || G.ttbn[i] < MIN_REPORTS)) continue;
      if (total >= start && total < end) out.push(cardAt(G, i));
      total++;
    }
    return { total, page, page_size: PAGE, games: out };
  }

  /* ----- one game ----- */
  function indexOf(G, id) {                         // ids are stored in ascending order
    let lo = 0, hi = G.n - 1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (G.id[mid] === id) return mid;
      if (G.id[mid] < id) lo = mid + 1; else hi = mid - 1;
    }
    return -1;
  }

  async function game(id, p) {
    const G = await games();
    const i = indexOf(G, id);
    if (i < 0) throw new Error("not found");
    const shard = Math.floor(id / SHARD);
    const data = await once(`shard${shard}`, async () => parse(await bytes(await versioned(`d/${shard}.dat`))));
    const d = data[String(id)] || {};
    const rated = !p || p.scope === "all" || !G.rated ? null : G.rated;
    const similar = (d.similar_ids || []).map(sid => indexOf(G, sid)).filter(k => k >= 0 && (!rated || rated[k])).map(k => cardAt(G, k));
    return {
      id, name: G.name[i], slug: null, summary: null, storyline: null, release: null, rating: null,
      votes: null, user_rating: null, critic_rating: null, url: null,
      genres: [], genre_ids: [], platforms: [], platform_ids: [], themes: [], modes: [], perspectives: [],
      screenshots: [], developers: [], publishers: [], time_to_beat: null,
      ...d, cover: G.cover[i] || null, similar, rated: G.rated ? !!G.rated[i] : null,
    };
  }

  /* ----- the same entry point the UI already calls ----- */
  async function api(path, params) {
    if (path === "api/games") return search(params);
    if (path === "api/facets") return facets(params.scope === "all" ? "all" : "rated");
    if (path === "api/stats") return stats(params.scope === "all" ? "all" : "rated");
    const m = /^api\/game\/(\d+)$/.exec(path);
    if (m) return game(+m[1], params);
    throw new Error("not found");
  }

  window.IGDB_STATIC = { api, columns: games };
})();
