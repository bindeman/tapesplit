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

  // ------------------------------------------------------------ the three stamps
  const SEQ_ALT = [
    "A tree-lined Moscow courtyard with the camcorder's date stamp, SEP 1 2005, in the corner",
    "Filip and his mom by the car on a sunny lawn, with the date stamp SEP 7 2005",
    "An empty room with the date stamp FEB 17 2006",
  ];
  const SEQ_STYLE = [["-2.4deg", "var(--washi-blue)"], ["1.6deg", "var(--washi-yellow)"], ["-1.2deg", "var(--washi-pink)"]];
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
  const eugene = H.sequence[1];
  const stampPic = $("#when-stamp");
  if (stampPic) {
    stampPic.innerHTML = `<img src="${eugene.image}" alt="The first morning of school: Filip by the car, with the camcorder date stamp SEP 7 2005 in the corner" loading="lazy" width="960" height="720">${boxes(eugene.ocr)}`;
    const wb = H.whiteboard;
    $("#when-board").innerHTML = `<img src="${wb.image}" alt="A classroom whiteboard: Good Morning! Today is Wednesday, September 7, 2005. We will learn names today." loading="lazy" width="960" height="720">${boxes(wb.ocr)}`;
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
    $("#window-text").textContent = `Tape 18's own date stamps and spoken dates put it between ${H.window.from} and ${H.window.to}. A date outside a tape's window is flagged instead of trusted.`;
  }
  const ignored = $("#ignored");
  if (ignored) {
    ignored.innerHTML = H.ignored.map((g, i) => `<li style="--i:${i}"><b>${esc(g.label)}</b><span>${esc(g.why)}</span></li>`).join("");
  }
  const st = H.stats;
  const whenStat = $("#when-stat");
  if (whenStat) {
    whenStat.innerHTML = `Across the archive, <b>${st.dated_day} of ${st.moments}</b> moments got an exact day: <b>${st.dated_by_stamp}</b> from a date stamp and <b>${st.dated_by_speech}</b> from a date someone said or wrote (${st.dated_by_both} had both). The rest borrow a month or year from the moments around them.`;
  }

  // ------------------------------------------------------------ where
  const ICON_SPEECH = `<svg viewBox="0 0 24 24"><path d="M4 5h16v10H9l-5 4z"/></svg>`;
  const ICON_SIGN = `<svg viewBox="0 0 24 24"><rect x="3.5" y="4" width="17" height="10" rx="1.5"/><path d="M12 14v7M7.5 9h9"/></svg>`;
  const clues = $("#clues");
  if (clues) {
    clues.innerHTML = H.where.clues.map((c, i) => {
      const speech = c.kind === "speech";
      const body = speech
        ? `<span class="clue-text" lang="ru">“${esc(c.ru)}”</span><span class="clue-note">“${esc(c.en)}”</span>`
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
      `None is anywhere this family lived, so the match was flagged. They lived in <strong>${esc(w.era.label.replace(" years", ""))}</strong> from ${w.era.from}, so TapeSplit searched again near home: <code>${esc(w.scoped_query)}</code>.`,
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
    lines.innerHTML = V.segments.map((s, i) => `<li data-i="${i}" style="--vc:var(--voice-${s.voice})"><span><span class="ru" lang="ru">${esc(s.ru)}</span><span class="en">${esc(s.en)}</span></span></li>`).join("");
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

  // ------------------------------------------------------------ language
  const L = H.language;
  const sw = $("#switch-card");
  if (sw) {
    sw.innerHTML = L.switch.map((line, i) => `
      <div class="sw-line${i ? " small" : ""}">
        <span class="sw-meta">Tape 18 · ${esc(line.clock)}${i === 2 ? " · at the classroom door" : ""}</span>
        <p class="sw-text">${line.parts.map(([lang, text]) => `<span class="part ${lang}" lang="${lang}"><span class="pill ${lang}">${lang.toUpperCase()}</span>${esc(text)}</span>`).join("")}</p>
        ${line.en ? `<span class="sw-gloss">“${esc(line.en)}”</span>` : ""}
      </div>`).join("") + (L.journal ? `
      <div class="sw-journal">
        <span class="sw-meta">In the Journal for ${esc(L.journal.date)} · Tape ${L.journal.tape} · ${esc(L.journal.clock)}</span>
        <blockquote lang="ru">“${esc(L.journal.ru)}”</blockquote>
        <span class="tr">${esc(L.journal.en)}</span>
        <span class="sw-meta">Quotes stay verbatim. The translation is TapeSplit's own.</span>
      </div>` : "");
    watch(sw);
  }
  const demo = $("#search-demo");
  if (demo) {
    const q = $("#search-q");
    const hits = $("#search-hits");
    hits.innerHTML = L.search.hits.map((h) => `<li><span class="ru" lang="ru">${esc(h.ru)}</span><span class="en">“${esc(h.en)}”</span><span class="where">Tape ${h.tape} · ${esc(h.clock)}</span></li>`).join("");
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

  // ------------------------------------------------------------ chronology
  const chart = $("#chrono-svg");
  if (chart) {
    const tapes = H.archive.tapes;
    const left = 64, right = 18, top = 46, rowH = 22;
    const W = 1000;
    const Y0 = 1996, Y1 = 2010;
    const plotW = W - left - right;
    const X = (year) => left + ((year - Y0) / (Y1 - Y0)) * plotW;
    const rowsBottom = top + tapes.length * rowH;
    const HGT = rowsBottom + 30;
    chart.setAttribute("viewBox", `0 0 ${W} ${HGT}`);
    let html = `<defs><pattern id="chrono-hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="3" height="6" fill="rgba(242,96,47,.35)"/></pattern></defs>`;
    for (let y = Y0; y <= Y1; y += 1) {
      html += `<line class="grid" x1="${X(y)}" x2="${X(y)}" y1="${top - 6}" y2="${rowsBottom}"/>`;
      if (y < Y1) html += `<text class="yr" x="${X(y + 0.5)}" y="${rowsBottom + 18}">${y}</text>`;
    }
    H.archive.eras.forEach((e, i) => {
      const a = X(e.from), b = X(e.to + 1);
      html += `<g class="era era-${i + 1}"><rect x="${a}" y="6" width="${b - a}" height="24"/>`;
      for (const c of e.conflict || []) html += `<rect class="conflict" x="${X(c)}" y="6" width="${X(c + 1) - X(c)}" height="24"/>`;
      html += `<text x="${a + 8}" y="22">${esc(e.label)}${e.conflict && e.conflict.length ? ` · ${e.conflict.join(", ")} also has a trip to Moscow` : ""}</text></g>`;
    });
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
        html += `<circle class="d ${kind}" tabindex="0" style="--i:${k}" cx="${X(y + frac).toFixed(1)}" cy="${cy}" r="${kind === "stamp" ? 4.6 : 4}" data-tip="Tape ${t.tape} · ${nice(iso)}|${kind === "stamp" ? "Camcorder date stamp" : "Said or written on tape"}"><title>Tape ${t.tape}, ${nice(iso)}: ${kind === "stamp" ? "camcorder date stamp" : "said or written on tape"}</title></circle>`;
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
    chart.querySelectorAll(".d").forEach((el) => {
      el.addEventListener("pointerenter", () => show(el));
      el.addEventListener("focus", () => show(el));
      el.addEventListener("pointerleave", () => { tip.hidden = true; });
      el.addEventListener("blur", () => { tip.hidden = true; });
    });
    watch(box);
    const facts = [
      [`${st.dated_day}`, `of ${st.moments} moments dated to the exact day`],
      [`${st.dated_by_stamp}`, "dated by the camcorder's own date stamp"],
      [`${st.dated_by_speech}`, "dated by something said or written on screen"],
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

  // the chapter heads and the rest of the static reveals
  document.querySelectorAll("#inside .reveal:not(.in), #timeline .reveal:not(.in)").forEach((el) => {
    if (!callbacks.has(el)) watch(el);
  });
})();
