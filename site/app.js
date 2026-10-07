(() => {
  "use strict";

  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Polaroid clips play only while on screen, and not at all for reduced motion.
  const clips = document.querySelectorAll("video[data-autoplay]");
  if (!reduceMotion && "IntersectionObserver" in window) {
    const seen = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          const video = entry.target;
          if (entry.isIntersecting) {
            if (video.preload === "none") video.preload = "auto";
            video.play().catch(() => {});
          } else {
            video.pause();
          }
        }
      },
      { threshold: 0.25 }
    );
    clips.forEach((video) => seen.observe(video));
  }

  // ---------- Tape 16 strip ----------
  const dataNode = document.getElementById("strip-data");
  const track = document.getElementById("strip-track");
  if (dataNode && track) {
    const tape = JSON.parse(dataNode.textContent);
    const duration = tape.duration;
    const chapters = [
      { key: "hawaii", label: "Hawaii · March 2006", from: 0, to: 43 * 60, color: "var(--hawaii)" },
      { key: "coast", label: "Oregon coast · April 2006", from: 43 * 60, to: 70 * 60, color: "var(--coast)" },
      { key: "spring", label: "Spring at home · May 2006", from: 70 * 60, to: duration, color: "var(--spring)" },
    ];
    const chapterFor = (t) => chapters.find((c) => t >= c.from && t < c.to) || chapters[chapters.length - 1];
    const pct = (t) => `${((t / duration) * 100).toFixed(3)}%`;
    const clock = (s) => {
      s = Math.max(0, Math.round(s));
      const h = Math.floor(s / 3600);
      const m = Math.floor((s % 3600) / 60);
      const sec = String(s % 60).padStart(2, "0");
      return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
    };
    const length = (s) => (s >= 90 ? `${Math.round(s / 60)} min` : `${Math.round(s)} s`);

    const meta = document.getElementById("strip-meta");
    if (meta) {
      const h = Math.floor(duration / 3600);
      const m = Math.round((duration % 3600) / 60);
      meta.textContent = `${h} h ${String(m).padStart(2, "0")} min · ${tape.events.length} moments`;
    }

    for (const [a, b] of tape.gaps) {
      const gap = document.createElement("div");
      gap.className = "strip-gap";
      gap.style.left = pct(a);
      gap.style.width = pct(b - a);
      track.appendChild(gap);
    }

    const chapterRow = document.getElementById("strip-chapters");
    for (const c of chapters) {
      const label = document.createElement("span");
      label.className = "strip-chapter";
      label.style.left = pct(c.from);
      label.style.color = c.color;
      label.textContent = c.label;
      chapterRow.appendChild(label);
    }

    const axis = document.getElementById("strip-axis");
    for (let t = 0; t <= duration; t += 15 * 60) {
      const tick = document.createElement("span");
      tick.style.left = pct(t);
      if (t === 0) tick.style.transform = "none";
      tick.textContent = clock(t);
      axis.appendChild(tick);
    }

    const img = document.getElementById("strip-img");
    const name = document.getElementById("strip-name");
    const facts = document.getElementById("strip-facts");
    const buttons = [];

    const show = (event, button) => {
      buttons.forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
      if (event.thumb) {
        img.src = event.thumb;
        img.alt = `Frame from "${event.title}"`;
      }
      name.textContent = event.title;
      facts.replaceChildren();
      const chapter = chapterFor(event.t0);
      const items = [
        `${clock(event.t0)}–${clock(event.t1)} on tape`,
        length(event.t1 - event.t0),
        chapter.label,
      ];
      if (event.date) items.push(`dated ${event.date}`);
      if ((event.people || []).some((p) => /Fil|Phil/.test(p))) items.push("Filip");
      for (const text of items) {
        const fact = document.createElement("span");
        fact.className = "fact";
        fact.textContent = text;
        facts.appendChild(fact);
      }
    };

    tape.events.forEach((event) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "strip-event";
      button.style.left = pct(event.t0);
      button.style.width = pct(Math.max(event.t1 - event.t0, 6));
      button.style.setProperty("--chapter", chapterFor(event.t0).color);
      button.setAttribute("aria-pressed", "false");
      button.setAttribute("aria-label", `${event.title}, at ${clock(event.t0)}`);
      button.title = event.title;
      button.addEventListener("click", () => show(event, button));
      button.addEventListener("mouseenter", () => show(event, button));
      button.addEventListener("focus", () => show(event, button));
      track.appendChild(button);
      buttons.push(button);
    });

    const startIndex = Math.max(0, tape.events.findIndex((e) => e.id === "266"));
    show(tape.events[startIndex], buttons[startIndex]);
  }

  // ---------- archive tour tabs ----------
  const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
  const select = (tab, focus) => {
    for (const t of tabs) {
      const on = t === tab;
      t.setAttribute("aria-selected", String(on));
      t.tabIndex = on ? 0 : -1;
      document.getElementById(t.getAttribute("aria-controls")).hidden = !on;
    }
    if (focus) tab.focus();
  };
  tabs.forEach((tab, i) => {
    tab.addEventListener("click", () => select(tab, false));
    tab.addEventListener("keydown", (e) => {
      let next = null;
      if (e.key === "ArrowRight") next = tabs[(i + 1) % tabs.length];
      if (e.key === "ArrowLeft") next = tabs[(i - 1 + tabs.length) % tabs.length];
      if (e.key === "Home") next = tabs[0];
      if (e.key === "End") next = tabs[tabs.length - 1];
      if (next) {
        e.preventDefault();
        select(next, true);
      }
    });
  });

  // ---------- copy buttons ----------
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
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        button.textContent = "Selected";
      }
      setTimeout(() => (button.textContent = "Copy"), 1800);
    });
  });
})();
