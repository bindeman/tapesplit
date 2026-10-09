// The cover's box of tapes, in 3D: three VHS cassettes in a pile on the page, lit like
// a product shot, with soft shadows on the paper. The drawn tapes underneath stay as the
// fallback; this only takes over once WebGL has rendered a frame.

const stack = document.querySelector(".vhs-stack");
const canvas = stack && stack.querySelector(".vhs-3d");
const saveData = navigator.connection && navigator.connection.saveData;

if (canvas && !saveData) {
  const start = () => main().catch(() => { /* keep the drawn tapes */ });
  if ("requestIdleCallback" in window) requestIdleCallback(start, { timeout: 1500 });
  else setTimeout(start, 300);
}

async function main() {
  const THREE = await import("./vendor/three/three.module.min.js");
  const { RoundedBoxGeometry } = await import("./vendor/three/RoundedBoxGeometry.js");
  const { RoomEnvironment } = await import("./vendor/three/RoomEnvironment.js");
  const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const theme = document.documentElement.dataset.theme;
  const dark = theme === "dark" || (theme !== "light" && matchMedia("(prefers-color-scheme: dark)").matches);

  const renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true, powerPreference: "low-power" });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.NeutralToneMapping;
  renderer.toneMappingExposure = 1.05;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;

  const scene = new THREE.Scene();
  const pmrem = new THREE.PMREMGenerator(renderer);
  scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
  scene.environmentIntensity = 0.55;

  // Light: a warm key from the upper left that casts the shadows, a cool rim from behind.
  const key = new THREE.DirectionalLight(0xfff0d8, 2.6);
  key.position.set(-2.6, 5.5, 2.8);
  key.castShadow = true;
  key.shadow.mapSize.set(1024, 1024);
  key.shadow.radius = 6;
  key.shadow.bias = -0.0004;
  Object.assign(key.shadow.camera, { left: -2.2, right: 2.2, top: 2.2, bottom: -2.2, near: 1, far: 14 });
  scene.add(key);
  // black plastic on a dark page needs more edge light to read
  const rim = new THREE.DirectionalLight(0xd6e4ff, dark ? 2.6 : 1.1);
  rim.position.set(3, 2.4, -4);
  scene.add(rim);
  scene.add(new THREE.HemisphereLight(0xf3efe6, 0xb08a5a, dark ? 0.9 : 0.55));

  // The page itself catches the shadows; everything else stays transparent.
  const ground = new THREE.Mesh(new THREE.PlaneGeometry(12, 12), new THREE.ShadowMaterial({ opacity: 0.2 }));
  ground.rotation.x = -Math.PI / 2;
  ground.receiveShadow = true;
  scene.add(ground);

  // ---------------------------------------------------------------- one cassette
  // A real VHS cassette is 187 × 103 × 25 mm; one unit here is 100 mm.
  const W = 1.87, D = 1.03, H = 0.25;
  const shellMat = new THREE.MeshPhysicalMaterial({ color: 0x1a1a1d, roughness: 0.5, clearcoat: 0.3, clearcoatRoughness: 0.45 });
  const shellGeo = new RoundedBoxGeometry(W, H, D, 4, 0.035);
  const windowMat = new THREE.MeshStandardMaterial({ color: 0x0c0c0f, roughness: 0.35 });
  const glassMat = new THREE.MeshPhysicalMaterial({ color: 0xffffff, roughness: 0.04, transparent: true, opacity: 0.1, clearcoat: 1 });
  const tapeMat = new THREE.MeshStandardMaterial({ color: 0x2b1e17, roughness: 0.55 });
  const hubMat = new THREE.MeshStandardMaterial({ color: 0xeceae3, roughness: 0.4 });
  const holeMat = new THREE.MeshStandardMaterial({ color: 0x111113, roughness: 0.6 });
  const screwMat = new THREE.MeshStandardMaterial({ color: 0x2a2a2e, roughness: 0.3, metalness: 0.6 });

  await document.fonts.load('700 150px "Caveat"').catch(() => {});
  const labelTexture = (text) => {
    const c = document.createElement("canvas");
    c.width = 1024;
    c.height = 330;
    const g = c.getContext("2d");
    g.fillStyle = "#f3ebd6";
    g.fillRect(0, 0, c.width, c.height);
    for (let i = 0; i < 2400; i += 1) { // paper grain
      g.fillStyle = `rgba(120, 96, 60, ${Math.random() * 0.05})`;
      g.fillRect(Math.random() * c.width, Math.random() * c.height, 2, 2);
    }
    g.fillStyle = "#c9433a";
    g.fillRect(0, 34, c.width, 5);
    g.strokeStyle = "rgba(80, 120, 190, 0.35)";
    g.lineWidth = 3;
    for (let y = 108; y < c.height; y += 74) {
      g.beginPath();
      g.moveTo(0, y);
      g.lineTo(c.width, y);
      g.stroke();
    }
    if (text) {
      g.fillStyle = "#28469a";
      g.font = '700 170px "Caveat", "Bradley Hand", cursive';
      g.textAlign = "center";
      g.textBaseline = "middle";
      g.save();
      g.translate(c.width / 2, c.height / 2 + 26);
      g.rotate(-0.05);
      g.fillText(text, 0, 0);
      g.restore();
    }
    const t = new THREE.CanvasTexture(c);
    t.colorSpace = THREE.SRGBColorSpace;
    t.anisotropy = renderer.capabilities.getMaxAnisotropy();
    return t;
  };

  const reel = (tapeRadius) => {
    const r = new THREE.Group();
    const pack = new THREE.Mesh(new THREE.CylinderGeometry(tapeRadius, tapeRadius, 0.006, 48), tapeMat);
    r.add(pack);
    const hub = new THREE.Mesh(new THREE.CylinderGeometry(0.072, 0.072, 0.012, 32), hubMat);
    hub.position.y = 0.004;
    r.add(hub);
    for (let i = 0; i < 6; i += 1) {
      const spoke = new THREE.Mesh(new THREE.BoxGeometry(0.11, 0.014, 0.012), hubMat);
      spoke.position.y = 0.004;
      spoke.rotation.y = (i / 6) * Math.PI;
      r.add(spoke);
    }
    const hole = new THREE.Mesh(new THREE.CylinderGeometry(0.03, 0.03, 0.016, 20), holeMat);
    hole.position.y = 0.005;
    r.add(hole);
    return r;
  };

  const cassette = (text, left, right) => {
    const g = new THREE.Group();
    const shell = new THREE.Mesh(shellGeo, shellMat);
    shell.castShadow = true;
    shell.receiveShadow = true;
    g.add(shell);
    const top = H / 2;
    const label = new THREE.Mesh(new THREE.PlaneGeometry(1.5, 0.48), new THREE.MeshStandardMaterial({ map: labelTexture(text), roughness: 0.85 }));
    label.rotation.x = -Math.PI / 2;
    label.position.set(0, top + 0.002, -0.22);
    label.receiveShadow = true;
    g.add(label);
    const win = new THREE.Mesh(new THREE.PlaneGeometry(1.04, 0.3), windowMat);
    win.rotation.x = -Math.PI / 2;
    win.position.set(0, top + 0.002, 0.25);
    g.add(win);
    const reels = [reel(left), reel(right)];
    reels[0].position.set(-0.3, top + 0.004, 0.25);
    reels[1].position.set(0.3, top + 0.004, 0.25);
    g.add(...reels);
    const glass = new THREE.Mesh(new THREE.PlaneGeometry(1.04, 0.3), glassMat);
    glass.rotation.x = -Math.PI / 2;
    glass.position.set(0, top + 0.02, 0.25);
    g.add(glass);
    for (const [x, z] of [[-0.86, -0.45], [0.86, -0.45], [-0.86, 0.45], [0.86, 0.45], [0, 0.45]]) {
      const screw = new THREE.Mesh(new THREE.CylinderGeometry(0.022, 0.022, 0.004, 16), screwMat);
      screw.position.set(x, top + 0.001, z);
      g.add(screw);
    }
    g.userData.reels = reels;
    return g;
  };

  // ---------------------------------------------------------------- the pile
  const pile = new THREE.Group();
  scene.add(pile);
  const tapes = [
    // fanned, so the labels underneath peek out from behind the top one
    { tape: cassette("", 0.13, 0.17), pos: [0.24, H / 2, -0.12], rot: 0.34 },
    { tape: cassette("?", 0.16, 0.11), pos: [-0.2, H * 1.5, -0.36], rot: -0.26 },
    { tape: cassette("??", 0.12, 0.155), pos: [0.04, H * 2.5, 0.1], rot: 0.05 },
  ];
  for (const t of tapes) {
    t.tape.position.set(...t.pos);
    t.tape.rotation.y = t.rot;
    pile.add(t.tape);
  }

  const camera = new THREE.PerspectiveCamera(24, 1, 0.1, 50);
  const target = new THREE.Vector3(0, 0.3, -0.12);
  const home = new THREE.Vector3(0.2, 4.3, 4.6);
  const base = home.clone();

  const size = () => {
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (!w || !h) return;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    // keep the whole pile in frame whatever the canvas shape
    base.copy(home).multiplyScalar(Math.max(1, 1.3 / camera.aspect));
    camera.updateProjectionMatrix();
  };
  size();

  // pointer parallax, eased
  const aim = { x: 0, y: 0 }, now = { x: 0, y: 0 };
  if (!still) {
    addEventListener("pointermove", (e) => {
      aim.x = (e.clientX / innerWidth - 0.5) * 2;
      aim.y = (e.clientY / innerHeight - 0.5) * 2;
    }, { passive: true });
  }

  const ease = (x) => { const c = 1.6; return 1 + (c + 1) * Math.pow(x - 1, 3) + c * Math.pow(x - 1, 2); }; // overshoot
  const t0 = performance.now();
  const draw = (ms) => {
    const t = still ? 10 : (ms - t0) / 1000;
    tapes.forEach((s, i) => {
      const k = Math.min(1, Math.max(0, (t - 0.15 - i * 0.18) / 0.55)); // drop in, one by one
      s.tape.position.y = s.pos[1] + (1 - ease(k)) * 1.6;
      s.tape.visible = k > 0;
    });
    pile.rotation.y = still ? 0 : Math.sin(t * 0.45) * 0.05;
    const reels = tapes[2].tape.userData.reels;
    if (!still) { reels[0].rotation.y = -t * 0.9; reels[1].rotation.y = -t * 0.9; }
    now.x += (aim.x - now.x) * 0.05;
    now.y += (aim.y - now.y) * 0.05;
    camera.position.set(base.x + now.x * 0.45, base.y - now.y * 0.25, base.z);
    camera.lookAt(target);
    renderer.render(scene, camera);
  };
  new ResizeObserver(() => { size(); if (still || !raf) draw(performance.now()); }).observe(canvas);

  var visible = true, raf = 0;
  const loop = (ms) => { draw(ms); raf = visible && !still ? requestAnimationFrame(loop) : 0; };
  new IntersectionObserver(([entry]) => {
    visible = entry.isIntersecting;
    if (visible && !raf && !still) raf = requestAnimationFrame(loop);
  }).observe(canvas);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { cancelAnimationFrame(raf); raf = 0; } else if (visible && !raf && !still) raf = requestAnimationFrame(loop);
  });

  draw(performance.now());
  stack.classList.add("is-3d");
  if (!still) raf = requestAnimationFrame(loop);
}
