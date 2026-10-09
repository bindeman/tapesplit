(() => {
  "use strict";

  const H = window.TAPESPLIT_HOW;
  const T = window.TAPESPLIT;
  if (!H || !T) return;
  document.documentElement.classList.add("reveal-on");
  const $ = (sel, root = document) => root.querySelector(sel);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const NS = "http://www.w3.org/2000/svg";
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const clock = (s) => {
    s = Math.max(0, Math.round(s));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = String(s % 60).padStart(2, "0");
    return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
  };
  const nice = (iso) => {
    const [y, m, d] = iso.split("-").map(Number);
    return `${MONTHS[m - 1]} ${d}, ${y}`;
  };
  const num = (n) => Number(n).toLocaleString("en-US");
  const quote = (s) => `“${s}”`;
  const boxes = (list, extra = "") => list.map((o, i) =>
    `<span class="box${extra}" style="--x:${o.box[0]};--y:${o.box[1]};--w:${o.box[2]};--h:${o.box[3]};--i:${i}"></span>`).join("");

  // ------------------------------------------------------------ reveal on scroll
  const callbacks = new Map();
  const io = !reduce && "IntersectionObserver" in window
    ? new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        entry.target.classList.add("in");
        io.unobserve(entry.target);
        const cb = callbacks.get(entry.target);
        if (cb) cb();
      }
    }, { threshold: 0.18, rootMargin: "0px 0px -8% 0px" })
    : null;
  const watch = (el, cb) => {
    if (!el) return;
    if (io) {
      if (cb) callbacks.set(el, cb);
      io.observe(el);
    } else {
      el.classList.add("in");
      if (cb) cb();
    }
  };

  // ------------------------------------------------------------ cover: what goes in, what comes out
  document.querySelectorAll("[data-stat]").forEach((el) => {
    const v = H.stats[el.dataset.stat];
    if (v != null) el.textContent = num(v);
  });
  const outThumbs = $("#out-thumbs");
  if (outThumbs && T.examples) {
    // the last one is drawn on top
    outThumbs.innerHTML = [["189", "Moscow"], ["254", "Davos"], ["260", "Hawaiʻi"]].map(([id, label]) => {
      const e = T.examples.find((x) => x.event === id);
      const year = (String(e && e.when.label).match(/\b(19|20)\d\d\b/) || [""])[0];
      return e ? `<figure><img src="${e.image || e.poster}" alt="" width="160" height="120"><figcaption>${esc(label)}, ${year}</figcaption></figure>` : "";
    }).join("");
  }
  const outChrono = $("#out-chrono");
  if (outChrono) {
    const tapes = H.archive.tapes, W = 300, top = 3, rowH = 5, y0 = 1996, y1 = 2009;
    const X = (y) => 6 + ((y - y0) / (y1 - y0)) * (W - 12);
    const hgt = top + tapes.length * rowH + 13;
    outChrono.setAttribute("viewBox", `0 0 ${W} ${hgt}`);
    let g = "";
    tapes.forEach((t, r) => {
      const cy = top + r * rowH + rowH / 2;
      if (t.direct && t.window[0]) g += `<line class="w" x1="${X(t.window[0]).toFixed(1)}" x2="${X(t.window[1] + 1).toFixed(1)}" y1="${cy}" y2="${cy}"/>`;
      for (const [iso, kind] of t.dates) {
        const [y, m, d] = iso.split("-").map(Number);
        g += `<circle class="${kind}" cx="${X(y + ((m - 1) * 30.44 + d) / 365.25).toFixed(1)}" cy="${cy}" r="1.9"/>`;
      }
    });
    for (const y of [1997, 2002, 2007]) g += `<text x="${X(y + 0.5).toFixed(1)}" y="${hgt - 2}">${y}</text>`;
    outChrono.innerHTML = g;
  }
  const outMap = $("#out-map");
  if (outMap && T.map) {
    const MP = T.map, RD = Math.PI / 180;
    const merc = (lat) => Math.log(Math.tan(Math.PI / 4 + (lat * RD) / 2));
    const K = MP.width / (MP.lon1 - MP.lon0), top = merc(MP.latTop);
    const proj = (lat, lng) => [(lng - MP.lon0) * K, ((top - merc(lat)) / RD) * K];
    const PINS = ["Kilauea volcano", "Katmai National Park", "Jakobshorn", "Red Square", "Florence, Oregon", "Castle Lake"];
    outMap.setAttribute("viewBox", `0 0 ${MP.width} ${MP.height}`);
    outMap.innerHTML = `<use href="#ts-land" class="land"/>` +
      T.dots.map((d) => { const [x, y] = proj(d.lat, d.lng); return `<circle class="dot" cx="${x.toFixed(0)}" cy="${y.toFixed(0)}" r="9"/>`; }).join("") +
      T.dots.filter((d) => PINS.includes(d.name)).map((d) => { const [x, y] = proj(d.lat, d.lng); return `<circle class="pin" cx="${x.toFixed(0)}" cy="${y.toFixed(0)}" r="22"/>`; }).join("");
  }
  const outTree = $("#out-tree");
  if (outTree) {
    const P = { grandma: [31, 19], grandpa: [69, 19], mom: [15, 54], dad: [85, 54], phil: [50, 80] };
    outTree.innerHTML = `<svg viewBox="0 0 100 100" preserveAspectRatio="none">
        <path class="ok" d="M15 54 C 24 72, 36 80, 50 80"/><path d="M85 54 C 76 72, 64 80, 50 80"/>
        <path d="M31 19 C 34 40, 46 54, 50 80"/><path d="M69 19 C 66 40, 54 54, 50 80"/></svg>` +
      Object.entries(P).map(([k, [x, y]]) => `<img src="media/how/avatars/${k}.svg" alt="" width="30" height="30" style="left:${x}%;top:${y}%">`).join("");
  }

  // ------------------------------------------------------------ tape 18 at a glance
  const tp = H.tape;
  const pct = (t) => ((t / tp.duration) * 100).toFixed(3);
  const bar = $("#t18-bar");
  if (bar) {
    const [f0, f1] = tp.focus;
    let html = tp.gaps.map(([a, b]) => `<i class="gap" style="left:${pct(a)}%;width:${pct(b - a)}%"></i>`).join("");
    html += tp.moments.map((m) => {
      const focus = m.t0 >= f0 - 1 && m.t1 <= f1 + 1;
      return `<i class="mo${focus ? " focus" : ""}" style="left:${pct(m.t0)}%;width:${pct(m.t1 - m.t0)}%" title="${esc(m.title)}${m.date ? " · " + esc(m.date) : ""}"></i>`;
    }).join("");
    html += H.sequence.map((s) => `<i class="tick" style="left:${pct(s.t)}%"></i>`).join("");
    html += `<span class="bracket" style="left:${pct(f0)}%;width:${pct(f1 - f0)}%"></span>`;
    bar.innerHTML = html;
    const axis = $("#t18-axis");
    const marks = [];
    for (let t = 0; t <= tp.duration; t += 1800) marks.push(t);
    axis.innerHTML = marks.map((t) => `<span style="left:${pct(t)}%">${clock(t)}</span>`).join("");
    $("#t18-meta").textContent = `${tp.file} · ${clock(tp.duration)} · ${tp.moments.length} moments`;
    const note = $("#t18-note");
    note.style.left = `calc(${pct(f0)}% + 6px)`;
  }

  // ------------------------------------------------------------ the two stamps
  const SEQ_ALT = [
    "Me and my mom by the car on a sunny lawn on my first day of first grade, with the date stamp SEP 7 2005",
    "A kid in a red shirt next to a wooden rocking chair, with the date stamp FEB 17 2006",
  ];
  const SEQ_STYLE = [["-2deg", "var(--washi-yellow)"], ["1.8deg", "var(--washi-pink)"]];
  const seq = $("#sequence");
  if (seq) {
    seq.innerHTML = H.sequence.map((s, i) => `
      <figure class="polaroid reveal" style="--r:${SEQ_STYLE[i][0]};--tc:${SEQ_STYLE[i][1]};transition-delay:${i * 120}ms">
        <div class="pic"><img src="${s.image}" alt="${esc(SEQ_ALT[i])}" loading="lazy" width="960" height="720">${boxes(s.ocr)}</div>
        <span class="dymo seq-tag" style="--dr:${i % 2 ? 2 : -3}deg">${esc(s.stamp)}${s.place ? " · " + esc(s.place.split(",")[0]) : ""}</span>
        <figcaption>${esc(s.title)}</figcaption>
      </figure>`).join("");
    seq.querySelectorAll(".polaroid").forEach((el) => watch(el));
  }

  // ------------------------------------------------------------ when
  const eugene = H.sequence.find((s) => s.stamp.startsWith("SEP 7")) || H.sequence[0];
  const stampPic = $("#when-stamp");
  if (stampPic) {
    stampPic.innerHTML = `<img src="${eugene.image}" alt="The first morning of school: me by the car, with the camcorder date stamp SEP 7 2005 in the corner" loading="lazy" width="960" height="720">${boxes(eugene.ocr)}`;
    const wb = H.whiteboard;
    // One box around the date it read: the handwriting slopes, so the per-line boxes overlap.
    const dateParts = wb.ocr.filter((o) => !/learn names/i.test(o.text));
    const x0 = Math.min(...dateParts.map((o) => o.box[0])), y0 = Math.min(...dateParts.map((o) => o.box[1]));
    const x1 = Math.max(...dateParts.map((o) => o.box[0] + o.box[2])), y1 = Math.max(...dateParts.map((o) => o.box[1] + o.box[3]));
    $("#when-board").innerHTML = `<img src="${wb.image}" alt="A classroom whiteboard: Good Morning! Today is Wednesday, September 7, 2005. We will learn names today." loading="lazy" width="960" height="720">${boxes([{ box: [x0, y0, x1 - x0, y1 - y0] }])}`;
    document.querySelectorAll("#ch-when .reveal").forEach((el) => watch(el));
  }

  const winBar = $("#cw-bar");
  if (winBar) {
    const y0 = H.window.from - 1, y1 = H.window.to + 2;
    const x = (year) => (((year - y0) / (y1 - y0)) * 100).toFixed(2);
    let html = `<span class="rail"></span><span class="win" style="left:${x(H.window.from)}%;width:${x(H.window.to + 1) - x(H.window.from)}%"></span>`;
    for (let y = y0; y <= y1; y += 1) html += `<span class="yr" style="left:${x(y)}%">${y}</span>`;
    html += `<span class="pt" style="left:${x(2005 + 249 / 365)}%" data-label="Sep 7, 2005"></span>`;
    winBar.innerHTML = html;
    $("#window-text").textContent = `Tape 18's own date stamps and spoken dates put it between ${H.window.from} and ${H.window.to}. A date outside a tape's window is treated as history, not as the day it was filmed.`;
  }
  const ignored = $("#ignored");
  if (ignored) {
    ignored.innerHTML = H.ignored.map((g, i) => `<li style="--i:${i}"><b>${esc(g.label)}</b><span>${esc(g.why)}</span></li>`).join("");
  }
  const st = H.stats;
  const whenStat = $("#when-stat");
  if (whenStat) {
    whenStat.innerHTML = `Across the archive, <b>${st.dated_day} of ${st.moments}</b> moments got an exact day: <b>${st.dated_by_stamp}</b> confirmed by a date stamp Apple Vision read, and <b>${st.dated_by_speech}</b> from what the video model saw or heard in the moment (${st.dated_by_both} had both). The rest borrow a month or year from the moments around them.`;
  }

  // ------------------------------------------------------------ where
  const ICON_SPEECH = `<svg viewBox="0 0 24 24"><path d="M4 5h16v10H9l-5 4z"/></svg>`;
  const ICON_SIGN = `<svg viewBox="0 0 24 24"><rect x="3.5" y="4" width="17" height="10" rx="1.5"/><path d="M12 14v7M7.5 9h9"/></svg>`;
  const clues = $("#clues");
  if (clues) {
    clues.innerHTML = H.where.clues.map((c, i) => {
      const speech = c.kind === "speech";
      const body = speech
        ? `<span class="clue-text" lang="ru">“${esc(c.ru)}”</span><span class="clue-note">${quote(esc(c.en))}</span>`
        : `<span class="sign-text">${esc(c.text)}</span><span class="clue-note">${esc(c.note)}</span>`;
      return `<div class="glass clue ${speech ? "speech" : "sign"} reveal" style="transition-delay:${i * 90}ms">
        <span class="clue-icon">${speech ? ICON_SPEECH : ICON_SIGN}</span>
        <div class="clue-body">
          <span class="clue-meta">${speech ? "Said on tape" : "Read off the screen"} · ${esc(c.clock)}</span>
          ${body}
          ${c.note && speech ? `<span class="clue-note">${esc(c.note)}</span>` : ""}
          <span class="gives">→ ${esc(c.gives)}</span>
        </div>
      </div>`;
    }).join("");
    clues.querySelectorAll(".reveal").forEach((el) => watch(el));
  }

  const usMap = $("#us-map");
  if (usMap && T.map) {
    const M = T.map;
    const RAD = Math.PI / 180;
    const mercY = (lat) => Math.log(Math.tan(Math.PI / 4 + (lat * RAD) / 2));
    const K = M.width / (M.lon1 - M.lon0);
    const Y0 = mercY(M.latTop);
    const P = (lat, lng) => [(lng - M.lon0) * K, ((Y0 - mercY(lat)) / RAD) * K];
    const [x0, y0] = P(50.2, -127.5);
    const [x1, y1] = P(23.2, -64.5);
    usMap.setAttribute("viewBox", `${x0.toFixed(1)} ${y0.toFixed(1)} ${(x1 - x0).toFixed(1)} ${(y1 - y0).toFixed(1)}`);
    const keep = H.where.candidates.find((c) => c.kept);
    const [ex, ey] = P(keep.lat, keep.lng);
    let html = `<use href="#ts-land" class="land"/><use href="#ts-borders" class="borders"/>`;
    html += `<circle class="era" cx="${ex.toFixed(1)}" cy="${ey.toFixed(1)}" r="30"/>`;
    html += `<text class="cand-sub" x="${(ex - 27).toFixed(1)}" y="${(ey + 42).toFixed(1)}">home from ${H.where.era.from}</text>`;
    html += H.where.candidates.map((c) => {
      const [px, py] = P(c.lat, c.lng);
      const left = c.lng > -90;
      const tx = left ? -10 : 10;
      const anchor = left ? "end" : "start";
      return `<g class="cand${c.kept ? " keep" : " out"}${c.later ? " later" : ""}${c.first ? " first" : ""}" transform="translate(${px.toFixed(1)} ${py.toFixed(1)})">
        ${c.kept ? `<circle class="keep-ring" cy="-17" r="11"/>` : ""}
        <g class="drop">
          <path d="M0 0V-12" stroke="#5b5b60" stroke-width="1.8" stroke-linecap="round"/>
          <circle cy="-17" r="6" fill="url(#pinGrad)" stroke="#7d0d06" stroke-width=".6"/>
          <ellipse cx="-2" cy="-19" rx="2" ry="1.3" fill="#fff" opacity=".7"/>
        </g>
        <g class="cand-text">
          <text class="cand-label" x="${tx}" y="-17" text-anchor="${anchor}">${esc(c.label)}${c.first ? " · first pick" : ""}</text>
          <text class="cand-sub" x="${tx}" y="-5" text-anchor="${anchor}">${esc(c.city)}</text>
        </g>
      </g>`;
    }).join("");
    usMap.innerHTML = html;

    const w = H.where;
    const first = w.candidates.find((c) => c.first);
    const others = w.candidates.filter((c) => !c.first && !c.kept).map((c) => c.city.split(",")[0]);
    const steps = [
      `Searched for <code>${esc(w.query)}</code>. The best match was in ${esc(first.city)}, with others in ${others.map(esc).join(" and ")}.`,
      `None is anywhere this family lived, so the match was flagged. They lived in <strong>${esc(w.era.label.replace(" years", ""))}</strong> from ${w.era.from}, so tapesplit searched again near home: <code>${esc(w.scoped_query)}</code>.`,
      `A separate verifier checked the pairing on its own and agreed: the school is in <strong>Eugene, Oregon</strong>.`,
    ];
    const confs = [w.loose_confidence, w.scoped_confidence, w.context_confidence];
    const list = $("#where-steps");
    list.innerHTML = steps.map((s, i) => `<li><span>${s}</span><span class="conf${i === 2 ? " good" : ""}">${confs[i].toFixed(2)}</span></li>`).join("");
    const items = [...list.children];
    const stage = (n) => {
      usMap.classList.add(`s${n}`);
      items[n - 1].classList.add("on");
    };
    watch($("#where-map"), () => {
      if (reduce || !io) {
        [1, 2, 3].forEach(stage);
        return;
      }
      stage(1);
      setTimeout(() => stage(2), 1500);
      setTimeout(() => stage(3), 3000);
    });
  }
  document.querySelectorAll("#ch-where .where-photos .reveal").forEach((el) => watch(el));

  // ------------------------------------------------------------ who: voices
  const V = H.voices;
  const player = $("#who-player");
  if (player) {
    const video = $("#who-video");
    const dur = V.clip.dur;
    const W = 1000, HT = 92, N = V.wave.length;
    const voiceAt = (t) => {
      for (const s of V.segments) if (t >= s.t0 && t <= s.t1) return s.voice;
      return 0;
    };
    let svg = `<svg viewBox="0 0 ${W} ${HT}" preserveAspectRatio="none" aria-hidden="true">`;
    svg += `<line x1="0" x2="${W}" y1="46" y2="46" stroke="rgba(255,255,255,.08)" stroke-width="1"/>`;
    const bw = W / N;
    for (let i = 0; i < N; i += 1) {
      const t = ((i + 0.5) / N) * dur;
      const v = voiceAt(t);
      const a = V.wave[i];
      if (v) {
        const h = 4 + a * 34;
        const cy = v === 1 ? 23 : 69;
        svg += `<rect class="wv v${v}" data-t="${t.toFixed(2)}" x="${(i * bw + 0.6).toFixed(1)}" y="${(cy - h / 2).toFixed(1)}" width="${(bw - 1.6).toFixed(1)}" height="${h.toFixed(1)}" rx="1.5"/>`;
      } else {
        const h = 1.5 + a * 6;
        svg += `<rect class="wv" data-t="${t.toFixed(2)}" x="${(i * bw + 0.6).toFixed(1)}" y="${(46 - h / 2).toFixed(1)}" width="${(bw - 1.6).toFixed(1)}" height="${h.toFixed(1)}" rx="1"/>`;
      }
    }
    svg += `<line class="head-line" id="who-head" x1="0" x2="0" y1="0" y2="${HT}"/></svg>`;
    const plot = $("#lane-plot");
    plot.innerHTML = svg;
    const bars = [...plot.querySelectorAll(".wv")].map((el) => [el, Number(el.dataset.t)]);
    const head = $("#who-head");
    const lines = $("#who-lines");
    lines.innerHTML = V.segments.map((s, i) => `<li data-i="${i}" style="--vc:var(--voice-${s.voice})"><span><span class="who-name">${esc(V.names[s.voice])}</span><span class="ru" lang="ru">${esc(s.ru)}</span><span class="en">${esc(s.en)}</span></span></li>`).join("");
    const lineEls = [...lines.children];
    const tc = $("#who-tc");
    let last = -1;
    const paint = (t) => {
      head.setAttribute("x1", ((t / dur) * W).toFixed(1));
      head.setAttribute("x2", ((t / dur) * W).toFixed(1));
      for (const [el, bt] of bars) el.classList.toggle("past", bt <= t);
      tc.textContent = clock(V.clip.t0 + t);
      let cur = -1;
      V.segments.forEach((s, i) => { if (t >= s.t0 - 0.15) cur = i; });
      if (cur !== last) {
        lineEls.forEach((el, i) => el.classList.toggle("on", i === cur));
        const el = lineEls[cur];
        if (el && lines.scrollHeight > lines.clientHeight) {
          lines.scrollTo({ top: Math.max(0, el.offsetTop - lines.clientHeight / 3), behavior: reduce ? "auto" : "smooth" });
        }
        last = cur;
      }
    };
    paint(0);
    let raf = 0;
    const loop = () => {
      paint(video.currentTime);
      raf = requestAnimationFrame(loop);
    };
    const load = () => {
      if (!video.src) video.src = V.clip.video;
    };
    const play = () => {
      load();
      video.play().catch(() => {});
    };
    video.addEventListener("play", () => {
      player.classList.add("playing");
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(loop);
    });
    video.addEventListener("pause", () => {
      player.classList.remove("playing");
      cancelAnimationFrame(raf);
      paint(video.currentTime);
    });
    $("#who-play").addEventListener("click", () => (video.paused ? play() : video.pause()));
    const seek = (t) => {
      load();
      const go = () => {
        video.currentTime = Math.max(0, Math.min(dur - 0.05, t));
        paint(video.currentTime);
        video.play().catch(() => {});
      };
      if (video.readyState >= 1) go();
      else video.addEventListener("loadedmetadata", go, { once: true });
    };
    lines.addEventListener("click", (e) => {
      const li = e.target.closest("li");
      if (li) seek(V.segments[Number(li.dataset.i)].t0);
    });
    plot.addEventListener("click", (e) => {
      const r = plot.getBoundingClientRect();
      seek(((e.clientX - r.left) / r.width) * dur);
    });
    if (reduce) {
      for (const [el] of bars) el.classList.add("past");
      lineEls.forEach((el) => el.classList.add("on"));
    }
    watch(player, () => {
      if (!reduce) play();
    });
    // pause when scrolled away
    if ("IntersectionObserver" in window) {
      new IntersectionObserver((entries) => {
        for (const entry of entries) if (!entry.isIntersecting && !video.paused) video.pause();
      }, { threshold: 0 }).observe(player);
    }
  }

  const tf = $("#track-frames");
  if (tf) {
    tf.innerHTML = H.faces.frames.map((f) => `<div class="tf" style="background-image:url(${f.image})"><span class="box face on" style="--x:${f.box[0]};--y:${f.box[1]};--w:${f.box[2]};--h:${f.box[3]}"></span><span class="t">${clock(f.t)}</span></div>`).join("");
    $("#track-text").textContent = `One face track: ${H.faces.track_frames} frames over ${H.faces.span} seconds. InsightFace follows a face through the shot, then tracks are matched against faces on other tapes and grouped into a person. Naming the group takes one click.`;
  }
  const names = $("#names");
  if (names) {
    names.innerHTML = H.language.names.map((n, i) => `<span class="${/[А-Яа-яЁё]/.test(n) ? "cy" : ""}" style="--i:${i}">${esc(n)}</span>`).join("");
  }
  document.querySelectorAll("#ch-who .who-more .reveal").forEach((el) => watch(el));

  // ------------------------------------------------------------ family, and a few friends
  const F = H.family;
  const board = $("#family-board");
  const fcard = $("#family-card");
  if (F && board && fcard) {
    const AV = (k) => `media/how/avatars/${k}.svg`;
    const REL = Object.fromEntries(F.relations.map((r) => [r.key, r]));
    // A few people from outside the family, grouped under me by where they turn up.
    const FRIENDS = (H.friends && H.friends.featured) || [];
    const FR = Object.fromEntries(FRIENDS.map((f) => [`f:${f.key}`, f]));
    const isF = (key) => key.startsWith("f:");
    // Board coordinates are 600 wide (and 760 tall with the friends); the SVG stretches with
    // the board, so the nodes (placed in %) and the lines stay together at any aspect ratio.
    const BW = 600, BH = FRIENDS.length ? 760 : 480;
    const px = (x) => ((x / BW) * 100).toFixed(2), py = (y) => ((y / BH) * 100).toFixed(2);
    if (FRIENDS.length) board.classList.add("with-friends");
    const NODES = [
      { k: "grandma", x: 200, y: 78, name: "Grandma", sub: "«бабушка»", rel: "grandparents" },
      { k: "grandpa", x: 400, y: 78, name: "Grandpa", sub: "«дедушка»", rel: "grandparents" },
      { k: "mom", x: 100, y: 250, name: "Mom", sub: "«мама»", rel: "mom" },
      { k: "dad", x: 500, y: 250, name: "Dad", sub: "«папа»", rel: "dad", cam: true },
      { k: "phil", x: 300, y: 372, name: "me", sub: "Филя · Phillip", rel: null },
    ];
    const EDGES = [
      { rel: "mom", d: "M100 250 C 190 268, 240 326, 300 372", tag: [220, 307, -3] },
      { rel: "dad", d: "M500 250 C 410 268, 360 326, 300 372", tag: [380, 307, 2.5] },
      { rel: "grandparents", d: "M200 186 C 203 200, 250 205, 300 205", tag: null },
      { rel: "grandparents", d: "M400 186 C 397 200, 350 205, 300 205", tag: null },
      { rel: "grandparents", d: "M300 205 L 300 372", tag: [300, 205, -1.5] },
    ];
    // Two place boxes under me, three people in each. A friend of Mom's or Dad's sits on the
    // outside, under that parent; everyone else hangs off my side of the tree.
    const PLACES = [{ g: "Madison", x: 16 }, { g: "Eugene", x: 310 }];
    const BOX = { y: 488, w: 274, h: 236 }, FY = 584;
    const ANCHOR = { mom: [66, 286], dad: [534, 286], meL: [254, 386], meR: [346, 386] };
    FRIENDS.forEach((f) => {
      const box = PLACES.find((p) => p.g === f.group);
      const slot = FRIENDS.filter((o) => o.group === f.group).indexOf(f);
      f.x = box.x + (BOX.w * (slot + 0.5)) / 3;
      const [ax, ay] = ANCHOR[f.to === "me" ? (f.x < 300 ? "meL" : "meR") : f.to];
      const ty = FY - 36;
      f.d = `M${ax} ${ay} C ${ax} ${ay + (ty - ay) * 0.55}, ${f.x} ${ty - (ty - ay) * 0.5}, ${f.x.toFixed(1)} ${ty}`;
    });
    const years = (g) => {
      const ys = FRIENDS.filter((f) => f.group === g).flatMap((f) => [f.from, f.until]).filter(Boolean).map((d) => d.slice(0, 4)).sort();
      return ys.length ? (ys[0] === ys[ys.length - 1] ? ys[0] : `${ys[0]}–${ys[ys.length - 1]}`) : "";
    };
    const TO = { me: ["phil", "me"], mom: ["mom", "Mom"], dad: ["dad", "Dad"] };
    const NAME = { dad: "Dad", mom: "Mom", grandparents: "Grandparents" };
    const PRED = { father: "father of", mother: "mother of", grandparent: "grandparents of" };
    const STATUS = { needs_review: "Waiting for you", confirmed: "Confirmed", rejected: "Rejected" };
    const QUESTION = {
      dad: "Is the man everyone calls папа Phillip's father?",
      grandparents: "Are the people called бабушка and дедушка Phillip's grandparents?",
    };
    const decided = {};
    let sel = "dad";
    let lastFriend = FRIENDS.length ? `f:${FRIENDS[0].key}` : null;
    const status = (key) => decided[key] || (isF(key) ? FR[key].status : REL[key].status);
    const KIN = /(пап[аеуы]|папой|мам[аеуы]|мамой|бабушк[аеиу]|дедушк[аеиу]|mother|подруга|детский садик)/gi;
    const NM = /(Филипп\p{L}*|Филя|Phillip|Filip)/gu;
    const hl = (s) => esc(s).replace(KIN, '<mark class="kin">$1</mark>').replace(NM, '<mark class="nm">$1</mark>');
    const tagText = (key) => {
      const r = REL[key], st = status(key);
      if (st === "rejected") return `${r.predicate} ✕`;
      if (st === "confirmed") return `${key === "grandparents" ? "grandparents" : r.predicate} ✓`;
      return key === "grandparents" ? `grandparents? ${r.confidence.toFixed(2)} · ${r.confidence_2.toFixed(2)}` : `${r.predicate}? ${r.confidence.toFixed(2)}`;
    };
    const camIcon = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="2" y="7" width="13" height="10" rx="2"/><path d="M15 11l6-3.5v9L15 13z"/></svg>';
    const tickIcon = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/></svg>';

    const lines = $("#fam-lines");
    lines.setAttribute("viewBox", `0 0 ${BW} ${BH}`);
    lines.innerHTML = EDGES.map((e) => `<path data-rel="${e.rel}" d="${e.d}"/>`).join("")
      + FRIENDS.map((f) => `<path class="friend" data-rel="f:${f.key}" d="${f.d}"/>`).join("");
    board.insertAdjacentHTML("beforeend", (FRIENDS.length ? PLACES.map((p) => `<div class="fam-place" style="left:${px(p.x)}%;top:${py(BOX.y)}%;width:${px(BOX.w)}%;height:${py(BOX.h)}%"><span class="fam-place-label">${esc(p.g)} <i>${years(p.g)}</i></span></div>`).join("") : "")
      + NODES.map((n, i) => {
        const tagName = n.rel ? "button" : "div";
        const attrs = n.rel ? ` type="button" data-rel="${n.rel}" aria-controls="family-card" aria-label="${esc(n.name)}: show the evidence"` : "";
        return `<${tagName} class="fam-node fam-${n.k}"${attrs} style="--x:${px(n.x)};--y:${py(n.y)};--i:${i}">
          <span class="fam-av"><img src="${AV(n.k)}" alt="" width="96" height="96"></span>
          ${n.cam ? `<span class="fam-badge cam" title="behind the camera">${camIcon}</span>` : ""}
          ${n.k === "mom" ? `<span class="fam-badge ok" title="confirmed">${tickIcon}</span>` : ""}
          <span class="fam-name">${esc(n.name)}</span><span class="fam-sub">${esc(n.sub)}</span>
        </${tagName}>`;
      }).join("")
      + EDGES.filter((e) => e.tag).map((e) => `<button type="button" class="fam-tag" data-rel="${e.rel}" aria-controls="family-card" style="--x:${px(e.tag[0])};--y:${py(e.tag[1])};--dr:${e.tag[2]}deg"></button>`).join("")
      + FRIENDS.map((f, i) => `<button type="button" class="fam-node fam-friend" data-rel="f:${f.key}" aria-controls="family-card" aria-label="${esc(f.name)}, ${esc(f.tag)}: show the evidence" style="--x:${px(f.x)};--y:${py(FY)};--i:${6 + i}">
          <span class="fam-av"><img src="${AV(f.key)}" alt="" width="64" height="64"></span>
          <span class="fam-name">${esc(f.name)}</span><span class="fam-ftag">${esc(f.tag)}</span>
        </button>`).join(""));

    const conf = (label, v, st) => `<div class="row ${st}"><span>${esc(label)}</span><span class="bar"><i style="--v:${v}"></i></span><b>${v.toFixed(2)}</b></div>`;
    const lineItem = (l) => `<li><p class="ru" lang="ru">${hl(l.ru)}</p><span class="gl">${quote(esc(l.en))}</span><span class="where">Tape ${l.tape} · ${esc(l.clock)}</span></li>`;
    const momentItem = (f, m) => `<li class="sum"><p class="en-sum">${esc(m.title)}</p><span class="where">The video model names ${esc(f.name)} · Tape ${m.tape}${m.date ? ` · ${esc(nice(m.date))}` : ""}</span></li>`;
    const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    const ym = (iso) => `${MON[Number(iso.slice(5, 7)) - 1]} ${iso.slice(0, 4)}`;
    const span = (a, b) => !a ? "no date yet" : ym(a) === ym(b || a) ? ym(a) : `${ym(a)} – ${ym(b)}`;
    const tabs = () => `<div class="fc-tabs" role="tablist" aria-label="Relationships">${Object.keys(NAME).map((k) => `<button type="button" role="tab" data-rel="${k}" aria-selected="${k === sel}">${REL[k].who.map((w) => `<img src="${AV(w)}" alt="" width="20" height="20">`).join("")}${NAME[k]}</button>`).join("")}${lastFriend ? `<button type="button" role="tab" data-rel="friends" aria-selected="${isF(sel)}"><img src="${AV(FR[lastFriend].key)}" alt="" width="20" height="20">Friends</button>` : ""}</div>`;
    const reviewBlock = (key, question, n) => {
      const st = status(key);
      if (!isF(key) && REL[key].status === "confirmed") {
        return `<p class="fc-q">Confirmed in review.</p><p class="fc-note">Corrections live in their own file and are replayed after every rebuild.</p>`;
      }
      if (decided[key]) {
        return `<p class="fc-q">${st === "confirmed" ? "Confirmed." : "Rejected."} In the app, that answer is saved with your corrections and replayed after every rebuild.</p>
          <div class="fc-actions"><button type="button" class="fc-btn" data-act="undo">Undo</button></div>`;
      }
      return `<p class="fc-q">${esc(question)}</p>
        <div class="fc-actions"><button type="button" class="fc-btn ok" data-act="confirm">${n > 1 ? "Confirm All" : "Confirm"}</button><button type="button" class="fc-btn no" data-act="reject">${n > 1 ? "Reject All" : "Reject"}</button></div>
        ${n > 1 ? `<p class="fc-note">This decision applies to ${n} supporting relationship observations.</p>` : ""}`;
    };
    const friendCard = () => {
      const f = FR[sel], st = status(sel), [toAv, toName] = TO[f.to];
      const list = (f.lines || []).map(lineItem).join("") + (f.moments || []).map((m) => momentItem(f, m)).join("");
      const facts = `${f.group}, ${span(f.from, f.until)} · ${f.count} moment${f.count === 1 ? "" : "s"}${f.with_me ? `, ${f.with_me === f.count ? (f.count === 1 ? "with me" : "all with me") : `${f.with_me} with me`}` : ""}`;
      fcard.innerHTML = `${tabs()}
        <div class="fc-head"><span class="fc-pair"><img src="${AV(f.key)}" alt="${esc(f.name)}" width="34" height="34"><span class="fc-pred">${esc(f.tag)} of</span><img src="${AV(toAv)}" alt="${esc(toName)}" width="34" height="34"></span><span class="fc-status s-${st}">${STATUS[st]}</span></div>
        <p class="fc-text">${esc(f.why)}</p>
        <p class="fc-facts">${esc(facts)}</p>
        <ol class="fc-lines">${list}</ol>
        ${f.lines ? `<p class="fc-legend"><mark class="kin">relationship word</mark> <mark class="nm">name</mark> · translations added for this page</p>` : ""}
        ${f.confidence != null ? `<div class="fc-conf">${conf(f.name, f.confidence, st)}</div>` : ""}
        <div class="fc-review"><span class="src">${f.kind === "guess" ? "In the review queue" : "If tapesplit asked"}</span>${reviewBlock(sel, f.question, 1)}</div>`;
    };
    const renderCard = () => {
      if (isF(sel)) return friendCard();
      const r = REL[sel], st = status(sel);
      let text = "", confs = "";
      if (sel === "dad") {
        text = "Nobody on the tapes ever says his name. To everyone he's папа, often from behind the camera, so tapesplit keeps him as an unnamed father until someone names him.";
        confs = conf("Dad", r.confidence, st);
      } else if (sel === "mom") {
        text = `The family words alone made a ${r.guess.toFixed(2)} guess. Then a summary from the video model said it outright, which starts at ${r.confidence.toFixed(2)}, and it was confirmed in review.`;
        confs = conf("Mom", r.confidence, st);
      } else {
        text = "Both guesses come from one moment on Tape 15. tapesplit won't guess which side of the family they're on; that's for a person to say.";
        confs = conf("Grandma", r.confidence, st) + conf("Grandpa", r.confidence_2, st);
      }
      let list = r.lines.map(lineItem).join("");
      if (r.summary) {
        const sum = esc(r.summary.text).replace("[Mom]", '<span class="redact" title="name hidden on this page">Mom</span>').replace(/(mother)/, '<mark class="kin">$1</mark>').replace(/(Filip)/, '<mark class="nm">$1</mark>');
        list += `<li class="sum"><p class="en-sum">${sum}</p><span class="where">The video model's summary · Tape ${r.summary.tape} · ${esc(r.summary.clock)}</span></li>`;
      }
      fcard.innerHTML = `${tabs()}
        <div class="fc-head"><span class="fc-pair">${r.who.map((w) => `<img src="${AV(w)}" alt="${w === "grandma" ? "Grandma" : w === "grandpa" ? "Grandpa" : NAME[sel]}" width="34" height="34">`).join("")}<span class="fc-pred">${PRED[r.predicate]}</span><img src="${AV("phil")}" alt="me" width="34" height="34"></span><span class="fc-status s-${st}">${STATUS[st]}</span></div>
        <p class="fc-text">${text}</p>
        <ol class="fc-lines">${list}</ol>
        <p class="fc-legend"><mark class="kin">family word</mark> <mark class="nm">name</mark> · translations added for this page</p>
        <div class="fc-conf">${confs}</div>
        <div class="fc-review"><span class="src">In the review queue</span>${reviewBlock(sel, QUESTION[sel], r.observations)}</div>`;
    };
    const lineClass = (key) => {
      const st = status(key);
      return `${st === "confirmed" ? "confirmed" : st === "rejected" ? "rejected" : "guess"}${key === sel ? " on" : " dim"}`;
    };
    const paint = () => {
      lines.querySelectorAll("path").forEach((p) => {
        p.setAttribute("class", `${p.classList.contains("friend") ? "friend " : ""}${lineClass(p.dataset.rel)}`);
      });
      board.querySelectorAll(".fam-node[data-rel]").forEach((n) => {
        n.classList.toggle("on", n.dataset.rel === sel);
        if (isF(n.dataset.rel)) n.dataset.status = status(n.dataset.rel);
      });
      board.querySelectorAll(".fam-tag").forEach((t) => {
        const key = t.dataset.rel;
        t.textContent = tagText(key);
        t.className = `fam-tag ${status(key)}${key === sel ? " on" : ""}`;
        t.setAttribute("aria-label", `${NAME[key]}: ${tagText(key)}. Show the evidence`);
      });
      renderCard();
    };
    const select = (key) => {
      if (key === "friends") key = lastFriend;
      if (isF(key)) lastFriend = key;
      sel = key;
      paint();
    };
    board.addEventListener("click", (ev) => {
      const el = ev.target.closest("[data-rel]");
      if (el) select(el.dataset.rel);
    });
    fcard.addEventListener("click", (ev) => {
      const tab = ev.target.closest(".fc-tabs [data-rel]");
      if (tab) return select(tab.dataset.rel);
      const act = ev.target.closest("[data-act]");
      if (!act) return;
      if (act.dataset.act === "undo") delete decided[sel];
      else decided[sel] = act.dataset.act === "confirm" ? "confirmed" : "rejected";
      paint();
      const again = fcard.querySelector(`[data-act="${act.dataset.act === "undo" ? "confirm" : "undo"}"]`);
      if (again) again.focus();
    });
    fcard.addEventListener("keydown", (ev) => {
      const tab = ev.target.closest(".fc-tabs [role=tab]");
      if (!tab || (ev.key !== "ArrowRight" && ev.key !== "ArrowLeft")) return;
      const keys = [...Object.keys(NAME), ...(lastFriend ? ["friends"] : [])];
      const here = isF(sel) ? "friends" : sel;
      const next = keys[(keys.indexOf(here) + (ev.key === "ArrowRight" ? 1 : keys.length - 1)) % keys.length];
      select(next);
      fcard.querySelector(`.fc-tabs [data-rel="${next}"]`).focus();
    });
    paint();
    watch(board);
    watch(fcard);
    document.querySelectorAll(".family-steps .reveal").forEach((el, i) => {
      el.style.transitionDelay = `${i * 90}ms`;
      watch(el);
    });
    for (const [id, v] of [["b-loop", F.loop.count], ["b-loop-case", F.loop.in_case]]) {
      const el = document.getElementById(id);
      if (el && v != null) el.textContent = num(v);
    }
    const fs = $("#family-stat");
    if (fs) fs.innerHTML = `Across the archive: <b>${F.totals.candidates}</b> relationship guesses so far, <b>${F.totals.confirmed}</b> confirmed in review and <b>${F.totals.waiting}</b> waiting.${FRIENDS.length ? " The friends on the board are examples with made-up names: Zhenya’s and Fedya’s labels are tapesplit’s guesses, the other four are mine." : ""}`;
  }

  // ------------------------------------------------------------ language
  const L = H.language;
  const sw = $("#switch-card");
  if (sw) {
    sw.innerHTML = L.switch.map((line, i) => `
      <div class="sw-line${i ? " small" : ""}">
        <span class="sw-meta">Tape 18 · ${esc(line.clock)}${i === 2 ? " · at the classroom door" : ""}</span>
        <p class="sw-text">${line.parts.map(([lang, text]) => `<span class="part ${lang}" lang="${lang}"><span class="pill ${lang}">${lang.toUpperCase()}</span>${esc(text)}</span>`).join("")}</p>
        ${line.en ? `<span class="sw-gloss">${quote(esc(line.en))}</span>` : ""}
      </div>`).join("") + (L.journal ? `
      <div class="sw-journal">
        <span class="sw-meta">In the Journal for ${esc(L.journal.date)} · Tape ${L.journal.tape} · ${esc(L.journal.clock)}</span>
        <blockquote lang="ru">“${esc(L.journal.ru)}”</blockquote>
        <span class="tr">${esc(L.journal.en)}</span>
        <span class="sw-meta">Quotes stay verbatim. The translation is tapesplit's own.</span>
      </div>` : "");
    watch(sw);
  }
  const demo = $("#search-demo");
  if (demo) {
    const q = $("#search-q");
    const hits = $("#search-hits");
    hits.innerHTML = L.search.hits.map((h) => `<li><span class="ru" lang="ru">${esc(h.ru)}</span><span class="en">${quote(esc(h.en))}</span><span class="where">Tape ${h.tape} · ${esc(h.clock)}</span></li>`).join("");
    $("#search-foot").textContent = "Found by meaning, not spelling: a multilingual model (" + L.search.model + ") matches the English words to Russian speech. Translations added for this page.";
    const items = [...hits.children];
    const finish = () => {
      q.textContent = L.search.query;
      items.forEach((el) => el.classList.add("on"));
    };
    watch(demo, () => {
      if (reduce || !io) return finish();
      let k = 0;
      const type = () => {
        k += 1;
        q.textContent = L.search.query.slice(0, k);
        if (k < L.search.query.length) setTimeout(type, 55 + Math.random() * 45);
        else items.forEach((el, i) => setTimeout(() => el.classList.add("on"), 350 + i * 220));
      };
      setTimeout(type, 400);
    });
  }
  const langStat = $("#lang-stat");
  if (langStat) {
    langStat.innerHTML = `Across the archive: <b>${st.speech_ru_hours} hours</b> of Russian speech and <b>${st.speech_en_hours} hours</b> of English, in ${num(st.speaker_segments)} diarized lines.`;
  }

  // ------------------------------------------------------------ landmarks: from words to a pin
  const LM = H.landmarks;
  const lmGrid = $("#lm-grid");
  if (LM && lmGrid) {
    const deg = (v, pos, neg) => `${Math.abs(v).toFixed(4)}° ${v >= 0 ? pos : neg}`;
    const VERDICT = { support: "Agrees", refute: "Disagrees", insufficient: "Not sure" };
    const lookupItem = (l) => {
      return `<li class="lm-step look${l.wrong ? " miss" : " hit"}"><b>Looked up</b>
        <span class="lm-q">“${esc(l.query)}”</span> → <span class="lm-r">${esc(l.name)}</span>
        <span class="lm-conf">${l.confidence.toFixed(2)}</span>${l.suspect ? `<span class="lm-flag">flagged</span>` : ""}
        <span class="lm-sub">${l.note ? esc(l.note) : `${deg(l.lat, "N", "S")}, ${deg(l.lng, "E", "W")}`}</span></li>`;
    };
    const voteItem = (v) => `<li class="lm-step vote ${esc(v.verdict)}"><b>Checked</b>
        <span class="lm-r">${VERDICT[v.verdict] || esc(v.verdict)}</span> <span class="lm-conf">${v.confidence.toFixed(2)}</span>
        <span class="lm-sub">“${esc(v.reasoning)}”</span></li>`;
    const saidItem = (s) => `<li class="lm-step heard"><b>Heard</b><span class="lm-ru" lang="ru">«${esc(s.ru)}»</span>
        <span class="lm-sub">“${esc(s.en)}” · Tape ${s.tape} · ${esc(s.clock)}</span></li>`;
    lmGrid.innerHTML = LM.cards.map((c) => {
      const steps = [];
      if (c.ocr) steps.push(`<li class="lm-step read"><b>Read</b><span class="lm-sign">${esc(c.ocr.text)}</span>
        <span class="lm-sub">Apple Vision · Tape ${c.ocr.tape} · ${esc(c.ocr.clock)}</span></li>`);
      if (c.said) steps.push(saidItem(c.said));
      if (c.said2) steps.push(saidItem(c.said2));
      if (c.vote.verdict === "refute") {
        steps.push(lookupItem(c.lookups[0]), voteItem(c.vote), ...c.lookups.slice(1).map(lookupItem));
      } else {
        steps.push(...c.lookups.map(lookupItem), voteItem(c.vote));
      }
      return `<article class="lm-card reveal">
        <div class="lm-pic"><img src="${c.image}" alt="${esc(c.title)}" loading="lazy" width="960" height="656">${c.ocr ? boxes([{ box: c.ocr.box }]) : ""}</div>
        <div class="lm-body">
          <span class="lm-kind">${esc(c.kind)}</span>
          <h4>${esc(c.title)} <span>${esc(c.place)}</span></h4>
          <ol class="lm-chain">${steps.join("")}</ol>
          <p class="lm-story">${esc(c.story)}</p>
        </div>
      </article>`;
    }).join("");
    lmGrid.querySelectorAll(".lm-card").forEach((el) => watch(el, () => el.querySelectorAll(".box").forEach((b) => b.classList.add("on"))));
    const cv = LM.convict;
    $("#lm-aside").innerHTML = `On Tape ${cv.tape} at ${esc(cv.clock)}, the local transcript heard <i>“${esc(cv.heard)}”</i> The video model heard <i>“${esc(cv.model)}”</i> The lookup agreed with the video model: ${esc(cv.lookup.where)}, ${cv.lookup.confidence.toFixed(2)}.`;
    $("#lm-slips").innerHTML = LM.slips.map((s) => `<li><b>${esc(s.what)}</b> ${esc(s.why)}</li>`).join("");
  }

  // ------------------------------------------------------------ chronology
  const chart = $("#chrono-svg");
  if (chart) {
    const tapes = H.archive.tapes;
    const left = 64, right = 18, rowH = 22;
    const W = 1000;
    const Y0 = 1996, Y1 = 2010;
    const plotW = W - left - right;
    const X = (year) => left + ((year - Y0) / (Y1 - Y0)) * plotW;
    const fy = (iso) => {
      const [y, m, d] = iso.split("-").map(Number);
      return y + ((m - 1) * 30.44 + (d || 15)) / 365.25;
    };
    // Trips get stacked label lanes so nearby trips don't overprint each other.
    const trips = (H.archive.trips || []).map((t) => ({ ...t, x0: X(fy(t.from)), x1: X(fy(t.to)) }));
    const laneEnds = [];
    for (const t of trips) {
      const width = 8 + t.label.length * 6.4;
      let lane = laneEnds.findIndex((end) => end < t.x0 - 4);
      if (lane < 0) { lane = laneEnds.length; laneEnds.push(0); }
      laneEnds[lane] = t.x0 + width;
      t.lane = lane;
    }
    const tripTop = 40, laneH = 15;
    const top = tripTop + Math.max(1, laneEnds.length) * laneH + 22;
    const rowsBottom = top + tapes.length * rowH;
    const HGT = rowsBottom + 30;
    chart.setAttribute("viewBox", `0 0 ${W} ${HGT}`);
    let html = `<defs><pattern id="chrono-hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="3" height="6" fill="rgba(242,96,47,.35)"/></pattern></defs>`;
    for (let y = Y0; y <= Y1; y += 1) {
      html += `<line class="grid" x1="${X(y)}" x2="${X(y)}" y1="${top - 6}" y2="${rowsBottom}"/>`;
      if (y < Y1) html += `<text class="yr" x="${X(y + 0.5)}" y="${rowsBottom + 18}">${y}</text>`;
    }
    // Layer 1: where we lived. tapesplit's residence eras, and the years it hasn't placed.
    html += `<text class="layer-label" x="${left - 10}" y="22">Home</text>`;
    const firstEra = Math.min(...H.archive.eras.map((e) => e.from));
    html += `<g class="era era-before"><rect x="${X(Y0)}" y="6" width="${X(firstEra) - X(Y0)}" height="24"/><text x="${X(Y0) + 8}" y="22">Before Madison · not placed yet</text></g>`;
    H.archive.eras.forEach((e, i) => {
      const a = X(e.from), b = X(e.to + 1);
      html += `<g class="era era-${i + 1}"><rect x="${a}" y="6" width="${b - a}" height="24"/>`;
      for (const c of e.conflict || []) html += `<rect class="conflict" x="${X(c)}" y="6" width="${X(c + 1) - X(c)}" height="24"/>`;
      html += `<text x="${a + 8}" y="22">${esc(e.label)}</text></g>`;
    });
    // Layer 2: trips, from dated moments away from home and the places tapesplit found in them.
    // Each trip drops a faint column through the tape rows, so its moments line up under it.
    html += `<text class="layer-label" x="${left - 10}" y="${tripTop + 11}">Trips</text>`;
    trips.forEach((t, i) => {
      const y = tripTop + t.lane * laneH;
      const w = Math.max(5, t.x1 - t.x0);
      html += `<rect class="trip-col" data-trip="${i}" x="${(t.x0 + w / 2 - Math.max(4, w) / 2).toFixed(1)}" y="${y + 10}" width="${Math.max(4, w).toFixed(1)}" height="${(rowsBottom - y - 10).toFixed(1)}"/>`;
      html += `<g class="trip" tabindex="0" data-trip="${i}" data-tip="${esc(t.label)}|${esc(nice(t.from))}${t.to !== t.from ? " to " + esc(nice(t.to)) : ""} · ${t.moments} moment${t.moments === 1 ? "" : "s"}">`
        + `<rect x="${t.x0.toFixed(1)}" y="${y + 3}" width="${w.toFixed(1)}" height="7" rx="3.5"/>`
        + `<text x="${(t.x0 + w + 4).toFixed(1)}" y="${y + 10}">${esc(t.label)}</text></g>`;
    });
    // The years before I was born: Dad's tapes.
    const born = 2000;
    html += `<rect class="before-me" x="${X(Y0)}" y="${top - 6}" width="${X(born) - X(Y0)}" height="${rowsBottom - top + 6}" rx="6"/>`;
    html += `<line class="born" x1="${X(born)}" x2="${X(born)}" y1="${top - 14}" y2="${rowsBottom}"/>`;
    html += `<text class="note" x="${X(born) - 8}" y="${top - 10}" text-anchor="end">Dad's tapes, before I was born</text>`;
    let k = 0;
    tapes.forEach((t, r) => {
      const cy = top + r * rowH + rowH / 2;
      const hl = t.tape === H.tape.tape;
      html += `<g class="crow${hl ? " hl" : ""}"><rect class="row-bg" x="0" y="${cy - rowH / 2}" width="${W}" height="${rowH}" rx="4"/>`;
      html += `<text class="tape-label" x="${left - 10}" y="${cy + 4}">Tape ${t.tape}</text>`;
      if (t.window[0]) {
        const a = X(t.window[0]), b = X(t.window[1] + 1);
        html += `<rect class="win${t.direct ? "" : " none"}" x="${a}" y="${cy - 4}" width="${b - a}" height="8" rx="4"/>`;
      }
      for (const [iso, kind] of t.dates) {
        const [y, m, d] = iso.split("-").map(Number);
        const frac = (new Date(Date.UTC(y, m - 1, d)) - Date.UTC(y, 0, 1)) / (365.25 * 864e5);
        html += `<circle class="d ${kind}" tabindex="0" data-iso="${iso}" style="--i:${k}" cx="${X(y + frac).toFixed(1)}" cy="${cy}" r="${kind === "stamp" ? 4.6 : 4}" data-tip="Tape ${t.tape} · ${nice(iso)}|${kind === "stamp" ? "Confirmed by an on-screen date stamp" : "Seen or heard in the moment"}"><title>Tape ${t.tape}, ${nice(iso)}: ${kind === "stamp" ? "confirmed by an on-screen date stamp" : "seen or heard in the moment"}</title></circle>`;
        k += 1;
      }
      html += `</g>`;
    });
    const hlRow = tapes.findIndex((t) => t.tape === H.tape.tape);
    if (hlRow >= 0) {
      const cy = top + hlRow * rowH + rowH / 2;
      const px = X(2005 + 250 / 365);
      html += `<path class="note-line" d="M${px - 150} ${cy - 18} C ${px - 60} ${cy - 22}, ${px - 22} ${cy - 18}, ${px - 6} ${cy - 6}"/>`;
      html += `<text class="note" x="${px - 156}" y="${cy - 16}" text-anchor="end">the first day of school</text>`;
    }
    chart.innerHTML = chart.innerHTML + html;
    const tip = $("#chrono-tip");
    const box = $("#chrono-chart");
    const show = (el) => {
      const [a, b] = el.dataset.tip.split("|");
      tip.innerHTML = `<b>${esc(a)}</b>${esc(b)}`;
      const r = el.getBoundingClientRect(), c = box.getBoundingClientRect();
      tip.style.left = `${r.left - c.left + box.scrollLeft + r.width / 2}px`;
      tip.style.top = `${r.top - c.top}px`;
      tip.hidden = false;
    };
    chart.querySelectorAll(".d, .trip").forEach((el) => {
      el.addEventListener("pointerenter", () => show(el));
      el.addEventListener("focus", () => show(el));
      el.addEventListener("pointerleave", () => { tip.hidden = true; });
      el.addEventListener("blur", () => { tip.hidden = true; });
    });
    // Pointing at a trip lights up its column and the moments dated inside it.
    const light = (i, on) => {
      const t = trips[i];
      chart.querySelector(`.trip-col[data-trip="${i}"]`).classList.toggle("hot", on);
      chart.querySelectorAll(".d").forEach((d) => {
        if (d.dataset.iso >= t.from && d.dataset.iso <= t.to) d.classList.toggle("hot", on);
      });
    };
    chart.querySelectorAll(".trip").forEach((el) => {
      const i = Number(el.dataset.trip);
      for (const ev of ["pointerenter", "focus"]) el.addEventListener(ev, () => light(i, true));
      for (const ev of ["pointerleave", "blur"]) el.addEventListener(ev, () => light(i, false));
    });
    watch(box);
    const facts = [
      [`${st.dated_day}`, `of ${st.moments} moments dated to the exact day`],
      [`${st.dated_by_stamp}`, "confirmed by a date stamp that Apple Vision read"],
      [`${st.dated_by_speech}`, "dated from what the video model saw or heard"],
      ["2026", "the date every file claims. Ignored: that's when they were digitized"],
    ];
    $("#chrono-facts").innerHTML = facts.map(([b, s]) => `<div class="fact reveal"><b>${esc(b)}</b><span>${esc(s)}</span></div>`).join("");
    document.querySelectorAll("#chrono-facts .reveal").forEach((el, i) => {
      el.style.transitionDelay = `${i * 90}ms`;
      watch(el);
    });
  }

  // ------------------------------------------------------------ bento
  const setText = (id, value) => {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  };
  setText("b-blank", st.blank_hours);
  setText("b-hours", st.hours);
  setText("b-unrelated", st.unrelated);
  setText("b-tracks", num(st.face_tracks));
  setText("b-review", num(st.review_items));
  const ja = $("#journal-art");
  if (ja && L.entry) {
    ja.innerHTML = `<span class="k">${esc(L.entry.kicker)} · ${esc(L.entry.date)}</span><span class="ttl">${esc(L.entry.title)}</span><span class="dek">${esc(L.entry.dek)}</span>`;
  }
  const blank = $("#blank-art");
  if (blank) {
    const gaps = tp.gaps.filter(([a, b]) => b - a >= 8);
    blank.innerHTML = `<div class="bar">${gaps.map(([a, b], i) => `<i style="left:${pct(a)}%;width:${Math.max(0.5, pct(b - a))}%;--i:${i % 40}"></i>`).join("")}</div>
      <div class="cap"><span>Tape 18 · ${clock(tp.duration)}</span><span>striped = skipped</span></div>
      <div class="screens"><span class="s-blue">BLUE</span><span class="s-black">BLACK</span><span class="s-static">STATIC</span></div>`;
  }
  document.querySelectorAll("#bento .reveal").forEach((el, i) => {
    el.style.transitionDelay = `${(i % 3) * 80}ms`;
    watch(el);
  });

  // ------------------------------------------------------------ short muted clips play while on screen
  const clips = [...document.querySelectorAll(".why-clip, .loop-clip")];
  if (clips.length && !reduce && "IntersectionObserver" in window) {
    const clipIO = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        const v = entry.target;
        if (entry.isIntersecting) {
          v.preload = "auto";
          v.play().catch(() => {});
        } else if (!v.paused) {
          v.pause();
        }
      }
    }, { threshold: 0.35 });
    clips.forEach((v) => clipIO.observe(v));
  }

  // the chapter heads and the rest of the static reveals
  document.querySelectorAll("#inside .reveal:not(.in), #timeline .reveal:not(.in)").forEach((el) => {
    if (!callbacks.has(el)) watch(el);
  });
})();
