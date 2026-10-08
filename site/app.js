(() => {
  "use strict";

  const T = window.TAPESPLIT;
  if (!T) return;
  const $ = (sel, root = document) => root.querySelector(sel);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const canHover = window.matchMedia("(hover: hover)").matches;
  const isDark = () => {
    const theme = document.documentElement.dataset.theme;
    return theme === "dark" || (!theme && window.matchMedia("(prefers-color-scheme: dark)").matches);
  };
  const clock = (s) => {
    s = Math.max(0, Math.round(s));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = String(s % 60).padStart(2, "0");
    return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
  };
  const prettyPlace = (label) => {
    if (!label) return "";
    const m = /^(.*?) \((.*)\)$/.exec(label);
    const name = m ? m[1] : label;
    const scope = m ? m[2].replace(/ context$/, "") : "";
    const cap = name.charAt(0).toUpperCase() + name.slice(1);
    return scope && !name.includes(",") ? `${cap}, ${scope}` : cap;
  };

  // ------------------------------------------------------------ map projection
  const M = T.map;
  const RAD = Math.PI / 180;
  const mercY = (lat) => Math.log(Math.tan(Math.PI / 4 + (lat * RAD) / 2));
  const K = M.width / (M.lon1 - M.lon0);
  const Y0 = mercY(M.latTop);
  const project = (lat, lng) => [(lng - M.lon0) * K, ((Y0 - mercY(lat)) / RAD) * K];

  // Land and borders live once in the document; maps reference them with <use>.
  const NS = "http://www.w3.org/2000/svg";
  const defs = document.createElementNS(NS, "svg");
  defs.setAttribute("width", "0");
  defs.setAttribute("height", "0");
  defs.setAttribute("aria-hidden", "true");
  defs.style.position = "absolute";
  defs.innerHTML = `<defs><path id="ts-land" d="${M.land}"/><path id="ts-borders" d="${M.borders}"/></defs>`;
  document.body.appendChild(defs);

  // ------------------------------------------------------------ lightbox
  const lb = $("#lb");
  const lbBody = $("#lb-body");
  const lbMedia = $("#lb-media");
  const lbStrip = $("#lb-strip");
  const lbInfo = $("#lb-info");
  const lbCaption = $("#lb-caption");
  const lbCount = $("#lb-count");
  const lbPrev = $("#lb-prev");
  const lbNext = $("#lb-next");
  let lbItems = [];
  let lbIndex = 0;
  let lbOpener = null;

  function miniMap(where) {
    const [x, y] = project(where.lat, where.lng);
    const w = 84, h = 52;
    const vx = Math.min(Math.max(x - w / 2, -40), M.width + 40 - w);
    const vy = Math.min(Math.max(y - h / 2, -40), M.height + 40 - h);
    const ring = where.approx ? `<circle cx="${x}" cy="${y}" r="5" fill="none" stroke="#ff6b5f" stroke-width="0.7" stroke-dasharray="1.5 1.2"/>` : "";
    return `<svg class="minimap" viewBox="${vx.toFixed(1)} ${vy.toFixed(1)} ${w} ${h}" role="img" aria-label="Map of ${esc(where.label)}">
      <use href="#ts-land" class="land"/><use href="#ts-borders" class="borders"/>${ring}
      <circle cx="${x}" cy="${y}" r="2" fill="#ff3b30" stroke="#fff" stroke-width="0.6"/></svg>`;
  }

  function coord(lat, lng) {
    const ns = lat >= 0 ? "N" : "S", ew = lng >= 0 ? "E" : "W";
    return `${Math.abs(lat).toFixed(4)}° ${ns}, ${Math.abs(lng).toFixed(4)}° ${ew}`;
  }

  function infoHTML(m) {
    const rows = [];
    rows.push(`<div><dt>When</dt><dd>${m.when.label ? `<span class="big">${esc(m.when.label)}</span>` : `<span class="big">No date yet</span>`}<span class="basis">${esc(m.when.basis)}</span></dd></div>`);
    if (m.where) {
      const apple = `https://maps.apple.com/?ll=${m.where.lat},${m.where.lng}&q=${encodeURIComponent(prettyPlace(m.where.label))}&z=11`;
      rows.push(`<div><dt>Where</dt><dd><span class="big">${esc(prettyPlace(m.where.label))}</span><span class="basis">${esc(m.where.basis)}${m.where.approx ? " Approximate." : ""}</span>
        ${miniMap(m.where)}
        <div class="lb-maplinks"><span>${coord(m.where.lat, m.where.lng)}</span><a href="${apple}" target="_blank" rel="noopener">Open in Maps ›</a></div></dd></div>`);
    } else if (m.nopin) {
      rows.push(`<div><dt>Where</dt><dd><span class="big">Not on the map</span><span class="basis">${esc(m.nopin)}</span></dd></div>`);
    }
    if (m.places && m.places.length) {
      const title = m.where || m.nopin ? "Other places it suggested" : "Places it suggested";
      rows.push(`<div><dt>${title}</dt><dd>${m.places.map((p) => `<span class="tag unconfirmed">${esc(prettyPlace(p))}</span>`).join("")}<span class="basis">Unconfirmed until someone reviews them.</span></dd></div>`);
    }
    if (m.continuity && m.continuity.length) {
      rows.push(`<div><dt>Carried from nearby</dt><dd>${m.continuity.map((c) => `<span class="basis">${esc(c)}</span>`).join("")}</dd></div>`);
    }
    if (m.people && m.people.length) {
      rows.push(`<div><dt>Who</dt><dd>${m.people.map((p) => `<span class="tag">${esc(p)}</span>`).join("")}</dd></div>`);
    }
    rows.push(`<div><dt>tapesplit says</dt><dd>${esc(m.summary)}</dd></div>`);
    if (m.timing) rows.push(`<div><dt>Timing check</dt><dd>${esc(m.timing)}</dd></div>`);
    if (m.reviews) rows.push(`<div><dd class="lb-review">${m.reviews} ${m.reviews === 1 ? "detail is" : "details are"} waiting for review in the app.</dd></div>`);
    return `<h2 id="lb-title">${esc(m.title)}</h2>
      <p class="lb-sub">Tape ${m.tape.tape} · ${esc(m.tape.start)}–${esc(m.tape.end)} · ${esc(m.tape.length)}</p>
      <dl>${rows.join("")}</dl>
      <p class="lb-note">tapesplit's output for this moment, unedited except for names: everyone but Filip has a stand-in name.</p>`;
  }

  function showMedia(item, which) {
    lbMedia.innerHTML = "";
    if (item.kind === "image") {
      const img = document.createElement("img");
      img.src = isDark() ? item.dark : item.light;
      img.alt = item.alt;
      lbMedia.appendChild(img);
      return;
    }
    const m = item.moment;
    if (which === "video" && m.video) {
      const v = document.createElement("video");
      v.src = m.video;
      v.poster = m.poster;
      v.muted = true;
      v.loop = true;
      v.playsInline = true;
      v.autoplay = true;
      v.setAttribute("aria-label", `Clip: ${m.title}`);
      v.addEventListener("click", () => (v.paused ? v.play() : v.pause()));
      lbMedia.appendChild(v);
      v.play().catch(() => {});
    } else {
      const img = document.createElement("img");
      img.className = "moment-media";
      img.src = which && which !== "video" ? which : m.image || (m.frames && m.frames[0]);
      img.alt = `Frame from “${m.title}”`;
      lbMedia.appendChild(img);
    }
    lbStrip.querySelectorAll("button").forEach((b) => b.setAttribute("aria-current", String(b.dataset.src === which)));
  }

  function renderLightbox() {
    const item = lbItems[lbIndex];
    lbCount.textContent = lbItems.length > 1 ? `${lbIndex + 1} of ${lbItems.length}` : "";
    lbPrev.hidden = lbNext.hidden = lbItems.length < 2;
    lbStrip.innerHTML = "";
    if (item.kind === "image") {
      lbBody.classList.add("no-info");
      lbInfo.hidden = true;
      lbCaption.hidden = false;
      lbCaption.textContent = item.caption;
      lb.setAttribute("aria-label", item.caption);
      lb.removeAttribute("aria-labelledby");
      showMedia(item);
      return;
    }
    const m = item.moment;
    lbBody.classList.remove("no-info");
    lbInfo.hidden = false;
    lbCaption.hidden = true;
    lb.setAttribute("aria-labelledby", "lb-title");
    lb.removeAttribute("aria-label");
    lbInfo.innerHTML = infoHTML(m);
    lbInfo.scrollTop = 0;
    const strip = [];
    if (m.video) strip.push({ src: "video", thumb: m.poster, label: "Play the clip", clip: true });
    else if (m.image) strip.push({ src: m.image, thumb: m.image, label: "Main frame" });
    (m.frames || []).forEach((f, i) => strip.push({ src: f, thumb: f, label: `Frame ${i + 1}` }));
    if (strip.length > 1) {
      for (const s of strip) {
        const b = document.createElement("button");
        b.type = "button";
        b.dataset.src = s.src;
        b.className = s.clip ? "clipthumb" : "";
        b.style.backgroundImage = `url("${s.thumb}")`;
        b.setAttribute("aria-label", s.label);
        b.addEventListener("click", () => showMedia(item, s.src));
        lbStrip.appendChild(b);
      }
    }
    showMedia(item, m.video ? "video" : m.image || (m.frames || [])[0]);
  }

  function openLightbox(items, index, opener) {
    lbItems = items;
    lbIndex = index;
    lbOpener = opener || document.activeElement;
    lb.hidden = false;
    document.documentElement.classList.add("lb-open");
    renderLightbox();
    $("#lb-close").focus();
  }

  function closeLightbox() {
    lb.hidden = true;
    lbMedia.innerHTML = "";
    document.documentElement.classList.remove("lb-open");
    if (lbOpener && document.contains(lbOpener)) lbOpener.focus();
  }

  function step(delta) {
    if (lbItems.length < 2) return;
    lbIndex = (lbIndex + delta + lbItems.length) % lbItems.length;
    renderLightbox();
  }

  $("#lb-close").addEventListener("click", closeLightbox);
  lbPrev.addEventListener("click", () => step(-1));
  lbNext.addEventListener("click", () => step(1));
  lb.addEventListener("click", (e) => {
    if (e.target === lb || e.target === lbBody) closeLightbox();
  });
  document.addEventListener("keydown", (e) => {
    if (lb.hidden) return;
    if (e.key === "Escape") closeLightbox();
    else if (e.key === "ArrowLeft") step(-1);
    else if (e.key === "ArrowRight") step(1);
    else if (e.key === "Tab") {
      const focusable = [...lb.querySelectorAll("button, a[href], video, [tabindex]:not([tabindex='-1'])")].filter((n) => !n.hidden && n.offsetParent !== null);
      if (!focusable.length) return;
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  });

  // ------------------------------------------------------------ examples grid
  const exampleItems = T.examples.map((m) => ({ kind: "moment", moment: m }));
  const events = $("#events");
  T.examples.forEach((m, i) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "event";
    const place = (m.metaPlace || (m.where ? prettyPlace(m.where.label) : "Unplaced")).split(", ").slice(0, 2).join(", ");
    const year = m.when.label ? (/\d{4}(–\d{4})?$/.exec(m.when.label) || [m.when.label])[0] : "No date yet";
    const frames = [m.poster || m.image, ...(m.frames || [])].filter(Boolean);
    b.innerHTML = `<span class="event-thumb" style="background-image:url('${frames[0]}')">
        ${m.video ? `<span class="event-badge"><svg viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M2 4.5A1.5 1.5 0 0 1 3.5 3h6A1.5 1.5 0 0 1 11 4.5v1.2l3-1.7v8l-3-1.7v1.2A1.5 1.5 0 0 1 9.5 13h-6A1.5 1.5 0 0 1 2 11.5z"/></svg>${esc(m.tape.length)}</span>` : ""}
        <span class="event-skim" aria-hidden="true">${frames.map((_, k) => `<i class="${k === 0 ? "on" : ""}"></i>`).join("")}</span>
      </span>
      <span class="event-title">${esc(m.title)}</span>
      <span class="event-meta">${esc(place)} · ${esc(year)}</span>`;
    b.setAttribute("aria-label", `${m.title}, ${place}, ${year}. Show details.`);
    const thumb = b.querySelector(".event-thumb");
    const bars = [...b.querySelectorAll(".event-skim i")];
    let loaded = false, shown = 0;
    const showFrame = (k) => {
      if (k === shown) return;
      shown = k;
      thumb.style.backgroundImage = `url('${frames[k]}')`;
      bars.forEach((bar, j) => bar.classList.toggle("on", j === k));
    };
    b.addEventListener("pointerenter", () => {
      if (loaded) return;
      loaded = true;
      frames.forEach((f) => { const im = new Image(); im.src = f; });
    });
    b.addEventListener("pointermove", (e) => {
      if (e.pointerType !== "mouse") return;
      const r = thumb.getBoundingClientRect();
      const k = Math.min(frames.length - 1, Math.max(0, Math.floor(((e.clientX - r.left) / r.width) * frames.length)));
      showFrame(k);
    });
    b.addEventListener("pointerleave", () => showFrame(0));
    b.addEventListener("click", () => openLightbox(exampleItems, i, b));
    events.appendChild(b);
  });

  // ------------------------------------------------------------ tape 16 skimmer
  const tape = T.tape16;
  const tapeItems = tape.moments.map((m) => ({ kind: "moment", moment: m }));
  const chapterColor = { hawaii: "var(--hawaii)", coast: "var(--coast)", spring: "var(--spring)" };
  const chapterByKey = Object.fromEntries(tape.chapters.map((c) => [c.key, c]));
  const pct = (t) => `${((t / tape.duration) * 100).toFixed(3)}%`;
  const track = $("#im-track");
  const viewerImg = $("#im-image");
  const blank = $("#im-blank");
  const playhead = document.createElement("div");
  playhead.className = "im-playhead";
  playhead.hidden = true;
  track.appendChild(playhead);

  for (const [a, b] of tape.gaps) {
    const g = document.createElement("div");
    g.className = "im-gap";
    g.style.left = pct(a);
    g.style.width = pct(b - a);
    track.appendChild(g);
  }
  const chaptersEl = $("#im-chapters");
  for (const c of tape.chapters) {
    const s = document.createElement("span");
    s.style.left = pct(c.start);
    s.style.color = chapterColor[c.key];
    s.textContent = c.label;
    chaptersEl.appendChild(s);
  }
  const ruler = $("#im-ruler");
  for (let t = 0; t <= tape.duration; t += 15 * 60) {
    const s = document.createElement("span");
    s.style.left = pct(t);
    s.textContent = clock(t);
    ruler.appendChild(s);
  }

  let current = -1;
  const buttons = tape.moments.map((m, i) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "im-moment";
    b.style.left = pct(m.tape.t0);
    b.style.width = pct(Math.max(m.tape.t1 - m.tape.t0, 8));
    b.style.setProperty("--chapter", chapterColor[m.chapter]);
    b.style.backgroundImage = `url('${m.thumb}')`;
    b.setAttribute("aria-label", `${m.title}, ${m.tape.start} to ${m.tape.end}`);
    b.addEventListener("focus", () => select(i));
    b.addEventListener("click", (e) => {
      e.stopPropagation();
      if (!canHover && current !== i) { select(i); return; }
      openLightbox(tapeItems, i, b);
    });
    b.addEventListener("keydown", (e) => {
      if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
        e.preventDefault();
        const j = Math.min(tape.moments.length - 1, Math.max(0, i + (e.key === "ArrowRight" ? 1 : -1)));
        buttons[j].focus();
      }
    });
    track.appendChild(b);
    return b;
  });

  const details = $("#im-details");
  const chip = $("#im-chapter");
  let blankShown = false;

  function select(i, t) {
    const m = tape.moments[i];
    blank.hidden = true;
    details.hidden = false;
    chip.hidden = false;
    if (current !== i || blankShown) {
      blankShown = false;
      current = i;
      viewerImg.src = m.image;
      viewerImg.alt = `Frame from “${m.title}”`;
      $("#im-name").textContent = m.title;
      $("#im-range").textContent = `${m.tape.start} – ${m.tape.end} · ${m.tape.length}`;
      const c = chapterByKey[m.chapter];
      chip.textContent = c.label;
      chip.style.setProperty("--chip", chapterColor[m.chapter]);
      buttons.forEach((b, j) => b.setAttribute("aria-current", String(j === i)));
    }
    $("#im-timecode").textContent = clock(t ?? m.tape.t0);
  }

  function showBlank(t) {
    const gap = tape.gaps.find(([a, b]) => t >= a && t <= b);
    blank.hidden = false;
    details.hidden = true;
    chip.hidden = true;
    blankShown = true;
    $("#im-name").textContent = "Blank tape";
    $("#im-range").textContent = gap ? `${clock(gap[0])} – ${clock(gap[1])} · skipped` : "Skipped";
    $("#im-timecode").textContent = clock(t);
  }

  track.addEventListener("pointermove", (e) => {
    const r = track.getBoundingClientRect();
    const x = Math.min(Math.max(e.clientX - r.left, 0), r.width);
    const t = (x / r.width) * tape.duration;
    playhead.hidden = false;
    playhead.style.left = `${x}px`;
    const i = tape.moments.findIndex((m) => t >= m.tape.t0 && t <= Math.max(m.tape.t1, m.tape.t0 + 8));
    if (i >= 0) select(i, t);
    else showBlank(t);
  });
  track.addEventListener("pointerleave", () => {
    playhead.hidden = true;
    if (current >= 0) select(current);
  });
  track.addEventListener("click", () => {
    if (!blank.hidden) return;
    if (current >= 0 && canHover) openLightbox(tapeItems, current, buttons[current]);
  });
  details.addEventListener("click", () => openLightbox(tapeItems, Math.max(0, current), details));
  select(tape.moments.findIndex((m) => m.event === "266"));

  // Warm the skimmer's frames once the section is close to the viewport.
  const imovie = $("#imovie");
  if ("IntersectionObserver" in window) {
    const io = new IntersectionObserver((entries) => {
      if (entries.some((en) => en.isIntersecting)) {
        tape.moments.forEach((m) => { const im = new Image(); im.src = m.image; });
        io.disconnect();
      }
    }, { rootMargin: "300px" });
    io.observe(imovie);
  }

  // ------------------------------------------------------------ places map
  const svg = $("#map-svg");
  svg.setAttribute("viewBox", `0 0 ${M.width} ${M.height}`);
  svg.innerHTML = `<rect width="${M.width}" height="${M.height}" fill="url(#oceanGrad)"/>
    <use href="#ts-land" class="land"/><use href="#ts-borders" class="borders"/>`;
  const mapInner = $("#map-inner");
  const at = (x, y) => `left:${((x / M.width) * 100).toFixed(3)}%;top:${((y / M.height) * 100).toFixed(3)}%`;
  const tip = document.createElement("div");
  tip.className = "map-tip";
  tip.hidden = true;
  mapInner.appendChild(tip);

  for (const d of T.dots) {
    const [x, y] = project(d.lat, d.lng);
    const dot = document.createElement("span");
    dot.className = "dot";
    dot.setAttribute("style", at(x, y));
    dot.setAttribute("role", "img");
    const label = d.region && !d.name.includes(",") ? `${d.name}, ${d.region}` : d.name;
    dot.setAttribute("aria-label", label);
    dot.addEventListener("pointerenter", () => {
      tip.textContent = `${label}${d.n > 1 ? ` · ${d.n} moments` : ""}`;
      tip.setAttribute("style", at(x, y));
      tip.hidden = false;
    });
    dot.addEventListener("pointerleave", () => (tip.hidden = true));
    mapInner.appendChild(dot);
  }

  const REGION = {
    "Florence, Oregon": "Oregon coast",
    "sea lion cave (Oregon)": "Oregon coast",
    "Kilauea volcano (Hawaii)": "Hawaiʻi",
    "Kilauea Caldera (Hawaii Island, Hawaii)": "Hawaiʻi",
    "Hawaii": "Hawaiʻi",
    "Katmai National Park (Alaska)": "Katmai, Alaska",
    "Mount Redoubt (Alaska)": "Mount Redoubt, Alaska",
    "Jakobshorn (Davos, Switzerland)": "Davos, Switzerland",
    "St. Basil's Cathedral (Moscow, Russia)": "Moscow, Russia",
  };
  const clusters = [];
  T.examples.forEach((m, i) => {
    if (!m.where) return;
    const [x, y] = project(m.where.lat, m.where.lng);
    let c = clusters.find((k) => Math.hypot(k.x - x, k.y - y) < 16);
    if (!c) {
      c = { x, y, items: [], name: REGION[m.where.label] || prettyPlace(m.where.label), approx: true };
      clusters.push(c);
    }
    c.items.push(i);
    c.approx = c.approx && m.where.approx;
  });

  const mapWrap = $("#map-wrap");
  const popover = document.createElement("div");
  popover.className = "map-popover";
  popover.hidden = true;
  mapWrap.appendChild(popover);
  let activePin = null;
  const pinSVG = (n, approx) => `<svg viewBox="0 0 30 44" aria-hidden="true">
      ${approx ? `<circle class="ring" cx="15" cy="38" r="7"/>` : ""}
      <ellipse class="shadow" cx="18" cy="39" rx="5" ry="2"/>
      <path class="needle" d="M15 22V38"/>
      <circle class="head" cx="15" cy="13" r="10"/>
      <ellipse class="shine" cx="11.5" cy="9" rx="3.6" ry="2.4"/>
      ${n > 1 ? `<text class="count" x="15" y="13.5">${n}</text>` : ""}
    </svg>`;

  function closePopover() {
    popover.hidden = true;
    if (activePin) activePin.classList.remove("active");
    document.querySelectorAll(".places-list button.active").forEach((b) => b.classList.remove("active"));
    activePin = null;
  }

  function openCluster(c, pin, opener) {
    if (c.items.length === 1) {
      openLightbox(exampleItems, c.items[0], opener || pin);
      return;
    }
    closePopover();
    activePin = pin;
    pin.classList.add("active");
    document.querySelectorAll(`.places-list button[data-cluster="${clusters.indexOf(c)}"]`).forEach((b) => b.classList.add("active"));
    popover.innerHTML = `<strong>${esc(c.name)} · ${c.items.length} moments</strong><ul>${c.items
      .map((i) => {
        const m = T.examples[i];
        return `<li><button type="button" data-i="${i}"><img src="${m.poster || m.image}" alt=""><span>${esc(m.title)}<small>${esc(m.when.label || "No date yet")}</small></span></button></li>`;
      })
      .join("")}</ul>`;
    popover.hidden = false;
    const pr = pin.getBoundingClientRect(), wr = mapWrap.getBoundingClientRect();
    const px = pr.left + pr.width / 2 - wr.left;
    const pw = popover.offsetWidth, ph = popover.offsetHeight;
    const left = Math.min(Math.max(px - pw / 2, 4), wr.width - pw - 4);
    let top = pr.top - wr.top - ph - 10;
    const below = top < 4;
    if (below) top = pr.bottom - wr.top + 10;
    popover.style.left = `${left}px`;
    popover.style.top = `${top}px`;
    popover.style.setProperty("--arrow", `${px - left}px`);
    popover.classList.toggle("below", below);
    popover.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
      const i = Number(b.dataset.i);
      openLightbox(exampleItems, i, pin);
    }));
    popover.querySelector("button").focus();
  }

  clusters.forEach((c, ci) => {
    const pin = document.createElement("button");
    pin.type = "button";
    pin.className = "pin";
    pin.setAttribute("style", at(c.x, c.y));
    pin.setAttribute("aria-label", `${c.name}: ${c.items.length} ${c.items.length === 1 ? "moment" : "moments"}`);
    pin.innerHTML = pinSVG(c.items.length, c.approx);
    pin.addEventListener("click", (e) => {
      e.stopPropagation();
      tip.hidden = true;
      if (activePin === pin) closePopover();
      else openCluster(c, pin);
    });
    pin.addEventListener("pointerenter", () => {
      if (activePin) return;
      const unitsPerPx = M.height / (svg.getBoundingClientRect().height || M.height);
      tip.textContent = c.name;
      tip.setAttribute("style", at(c.x, c.y - 34 * unitsPerPx));
      tip.hidden = false;
    });
    pin.addEventListener("pointerleave", () => (tip.hidden = true));
    c.pin = pin;
    c.index = ci;
    mapInner.appendChild(pin);
  });
  document.addEventListener("click", (e) => {
    if (!popover.hidden && !popover.contains(e.target) && !e.target.closest(".pin, .places-list")) closePopover();
  });
  window.addEventListener("resize", () => { if (!popover.hidden) closePopover(); });
  $(".map-scroll").addEventListener("scroll", () => { if (!popover.hidden) closePopover(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !popover.hidden && lb.hidden) {
      const pin = activePin;
      closePopover();
      if (pin) pin.focus();
    }
  });

  const list = $("#places-list");
  const sorted = [...clusters].sort((a, b) => b.items.length - a.items.length || a.name.localeCompare(b.name));
  list.innerHTML = `<h3>Trips on this page</h3>${sorted
    .map((c) => `<button type="button" data-cluster="${c.index}"><span>${esc(c.name)}</span><span class="n">${c.items.length}</span></button>`)
    .join("")}`;
  list.querySelectorAll("button[data-cluster]").forEach((b) => b.addEventListener("click", (e) => {
    e.stopPropagation();
    const c = clusters[Number(b.dataset.cluster)];
    c.pin.scrollIntoView({ block: "nearest", inline: "center", behavior: "smooth" });
    openCluster(c, c.pin, b);
  }));
  const offmap = T.examples.map((m, i) => [m, i]).filter(([m]) => !m.where);
  if (offmap.length) {
    const box = document.createElement("div");
    box.className = "offmap";
    box.innerHTML = `Not on the map yet:<ul>${offmap
      .map(([m, i]) => `<li><button type="button" data-i="${i}">${esc(m.title)}</button></li>`)
      .join("")}</ul>`;
    box.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => openLightbox(exampleItems, Number(b.dataset.i), b)));
    list.appendChild(box);
  }

  // ------------------------------------------------------------ screenshot zoom
  const shots = {
    "library-tape16": "The Library: tape 16's moments as cards.",
    "event-davos": "A moment, opened: the cable car above Davos, dated from the camcorder stamp.",
    "journal-post": "A Journal entry drafted from the beach footage.",
    "review": "The review queue: the guesses that still need a person.",
  };
  const shotItems = Object.entries(shots).map(([key, caption]) => ({
    kind: "image",
    key,
    caption,
    light: `media/ui/large/${key}-light.webp`,
    dark: `media/ui/large/${key}-dark.webp`,
    alt: caption,
  }));
  document.querySelectorAll("[data-zoom]").forEach((b) => {
    b.addEventListener("click", () => openLightbox(shotItems, shotItems.findIndex((s) => s.key === b.dataset.zoom), b));
  });

  // ------------------------------------------------------------ copy buttons
  document.querySelectorAll("[data-copy]").forEach((button) => {
    button.addEventListener("click", async () => {
      const source = document.querySelector(button.dataset.copy);
      const text = source.innerText.trim();
      try {
        await navigator.clipboard.writeText(text);
        button.textContent = "Copied";
      } catch {
        const range = document.createRange();
        range.selectNodeContents(source);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
        button.textContent = "Selected";
      }
      setTimeout(() => (button.textContent = "Copy"), 1800);
    });
  });
})();
