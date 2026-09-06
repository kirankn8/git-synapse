/* ==========================================================================
   Force-directed coupling graph, drawn on a canvas.

   Written from scratch rather than pulling in d3: the layout is a few hundred
   lines of Verlet integration, and avoiding a bundled dependency keeps the
   container free of a JS toolchain and the page free of external requests.

   Physics per tick:
     - repulsion between every node pair, approximated on a spatial grid so the
       cost is roughly O(n) instead of O(n^2)
     - spring attraction along each edge, with rest length inversely
       proportional to coupling strength, so strongly coupled files sit closer
     - a weak centring force and velocity damping so the layout settles
   ========================================================================== */

const REPULSION = 5200;
const SPRING = 0.035;
const DAMPING = 0.86;
const CENTER_PULL = 0.0016;
const MIN_ALPHA = 0.0015;
const CELL = 90; // spatial hash cell size, in world units

/** Deterministic hue per directory, so files in one package share a colour. */
function dirColor(dir) {
  let hash = 0;
  const key = (dir || '').split('/').slice(0, 2).join('/');
  for (let i = 0; i < key.length; i++) hash = (hash * 31 + key.charCodeAt(i)) | 0;
  const hue = Math.abs(hash) % 360;
  return `hsl(${hue} 62% 58%)`;
}

/**
 * Render an interactive coupling graph into `container`.
 *
 * @param {HTMLElement} container
 * @param {{nodes: object[], edges: object[]}} data
 * @param {object} opts  {measureLabel, centerId, onNodeClick, onNodeFocus}
 */
export function renderGraph(container, data, opts = {}) {
  container.replaceChildren();

  const canvas = document.createElement('canvas');
  canvas.id = 'graph-canvas';
  container.appendChild(canvas);

  const tooltip = document.createElement('div');
  tooltip.className = 'graph-tooltip';
  container.appendChild(tooltip);

  const overlay = document.createElement('div');
  overlay.className = 'graph-overlay';
  overlay.innerHTML = `
    <div class="graph-legend">
      <div><strong>${data.nodes.length}</strong> files · <strong>${data.edges.length}</strong> couplings</div>
      <div class="row"><span class="dot" style="background:var(--accent)"></span>edge width = ${escapeHtml(opts.measureLabel || 'score')}</div>
      <div class="row"><span class="dot" style="background:var(--info)"></span>node size = change frequency</div>
      <div class="row"><span class="dot" style="background:var(--violet)"></span>colour = top-level directory</div>
    </div>`;
  container.appendChild(overlay);

  const controls = document.createElement('div');
  controls.className = 'graph-controls';
  container.appendChild(controls);

  const ctx = canvas.getContext('2d');
  let width = 0;
  let height = 0;
  let dpr = Math.min(window.devicePixelRatio || 1, 2);

  /* ------------------------------------------------------------ model -- */

  const maxChanges = Math.max(1, ...data.nodes.map((n) => n.change_count || 0));
  const scores = data.edges.map((e) => Math.abs(Number(e.score) || 0));
  const maxScore = Math.max(1e-9, ...scores);
  const minScore = Math.min(...scores, 0);

  const nodes = data.nodes.map((n, i) => {
    // Seed on a phyllotaxis spiral: spreads nodes evenly and avoids the
    // degenerate all-at-one-point start that random seeding can produce.
    const angle = i * 2.399963;
    const radius = 13 * Math.sqrt(i + 1);
    return {
      ...n,
      x: Math.cos(angle) * radius,
      y: Math.sin(angle) * radius,
      vx: 0,
      vy: 0,
      r: 3.6 + 9 * Math.sqrt((n.change_count || 1) / maxChanges),
      color: dirColor(n.dir_path),
      degree: 0,
      pinned: false,
    };
  });

  const index = new Map(nodes.map((n, i) => [n.id, i]));
  const edges = [];
  for (const e of data.edges) {
    const a = index.get(e.source);
    const b = index.get(e.target);
    if (a === undefined || b === undefined) continue;
    const norm = maxScore > minScore ? (Math.abs(e.score) - minScore) / (maxScore - minScore) : 0.5;
    edges.push({ a, b, norm, raw: e });
    nodes[a].degree++;
    nodes[b].degree++;
  }

  const centerIdx = opts.centerId !== null && opts.centerId !== undefined ? index.get(opts.centerId) : undefined;
  if (centerIdx !== undefined) {
    nodes[centerIdx].x = 0;
    nodes[centerIdx].y = 0;
    nodes[centerIdx].isCenter = true;
  }

  /* ------------------------------------------------------- view state -- */

  let scale = 1;
  let offsetX = 0;
  let offsetY = 0;
  let alpha = 1;
  let hovered = null;
  let dragging = null;
  let panning = false;
  let lastPointer = { x: 0, y: 0 };
  let running = true;

  const toWorld = (px, py) => ({ x: (px - width / 2 - offsetX) / scale, y: (py - height / 2 - offsetY) / scale });

  function resize() {
    const rect = container.getBoundingClientRect();
    width = Math.max(320, rect.width);
    height = Math.max(420, Math.min(760, Math.round(window.innerHeight * 0.66)));
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    canvas.style.width = width + 'px';
    canvas.style.height = height + 'px';
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  /* ---------------------------------------------------------- physics -- */

  function step() {
    if (alpha < MIN_ALPHA) return;

    // Spatial hash: only nodes in neighbouring cells repel each other. Without
    // this the all-pairs loop would dominate at a few hundred nodes.
    const grid = new Map();
    for (let i = 0; i < nodes.length; i++) {
      const n = nodes[i];
      const key = `${Math.floor(n.x / CELL)},${Math.floor(n.y / CELL)}`;
      let bucket = grid.get(key);
      if (!bucket) {
        bucket = [];
        grid.set(key, bucket);
      }
      bucket.push(i);
    }

    for (let i = 0; i < nodes.length; i++) {
      const n = nodes[i];
      const cx = Math.floor(n.x / CELL);
      const cy = Math.floor(n.y / CELL);
      for (let gx = cx - 1; gx <= cx + 1; gx++) {
        for (let gy = cy - 1; gy <= cy + 1; gy++) {
          const bucket = grid.get(`${gx},${gy}`);
          if (!bucket) continue;
          for (const j of bucket) {
            if (j <= i) continue;
            const m = nodes[j];
            let dx = n.x - m.x;
            let dy = n.y - m.y;
            let d2 = dx * dx + dy * dy;
            if (d2 < 0.01) {
              // Coincident nodes: nudge apart deterministically by index so
              // the layout stays reproducible across reloads.
              dx = (i - j) * 0.01 + 0.05;
              dy = 0.05;
              d2 = dx * dx + dy * dy;
            }
            if (d2 > CELL * CELL * 4) continue;
            const force = REPULSION / d2;
            const d = Math.sqrt(d2);
            const fx = (dx / d) * force;
            const fy = (dy / d) * force;
            n.vx += fx;
            n.vy += fy;
            m.vx -= fx;
            m.vy -= fy;
          }
        }
      }
    }

    for (const e of edges) {
      const n = nodes[e.a];
      const m = nodes[e.b];
      const dx = m.x - n.x;
      const dy = m.y - n.y;
      const d = Math.hypot(dx, dy) || 0.01;
      // Strong coupling pulls to a shorter rest length.
      const rest = 150 - 95 * e.norm;
      const force = (d - rest) * SPRING;
      const fx = (dx / d) * force;
      const fy = (dy / d) * force;
      n.vx += fx;
      n.vy += fy;
      m.vx -= fx;
      m.vy -= fy;
    }

    for (const n of nodes) {
      if (n.pinned) {
        n.vx = 0;
        n.vy = 0;
        continue;
      }
      n.vx -= n.x * CENTER_PULL * (n.isCenter ? 24 : 1);
      n.vy -= n.y * CENTER_PULL * (n.isCenter ? 24 : 1);
      n.vx *= DAMPING;
      n.vy *= DAMPING;
      // Cap velocity so a dense cluster cannot explode on the first tick.
      const speed = Math.hypot(n.vx, n.vy);
      if (speed > 28) {
        n.vx = (n.vx / speed) * 28;
        n.vy = (n.vy / speed) * 28;
      }
      n.x += n.vx * alpha;
      n.y += n.vy * alpha;
    }

    alpha *= 0.994;
  }

  /* ------------------------------------------------------------- draw -- */

  const css = getComputedStyle(document.documentElement);
  const readVar = (name, fallback) => (css.getPropertyValue(name) || fallback).trim();

  function draw() {
    const bg = readVar('--bg-panel', '#101a2c');
    const textColor = readVar('--text', '#e6edf7');
    const faint = readVar('--text-faint', '#64789a');

    ctx.save();
    ctx.fillStyle = bg;
    ctx.fillRect(0, 0, width, height);
    ctx.translate(width / 2 + offsetX, height / 2 + offsetY);
    ctx.scale(scale, scale);

    const hoverNeighbours = new Set();
    if (hovered !== null) {
      for (const e of edges) {
        if (e.a === hovered) hoverNeighbours.add(e.b);
        if (e.b === hovered) hoverNeighbours.add(e.a);
      }
    }

    for (const e of edges) {
      const n = nodes[e.a];
      const m = nodes[e.b];
      const touching = hovered === null || e.a === hovered || e.b === hovered;
      ctx.beginPath();
      ctx.moveTo(n.x, n.y);
      ctx.lineTo(m.x, m.y);
      ctx.lineWidth = (0.35 + e.norm * 2.6) / Math.max(scale, 0.55);
      ctx.strokeStyle = touching
        ? `rgba(94, 234, 212, ${0.22 + e.norm * 0.65})`
        : `rgba(100, 120, 154, ${0.05 + e.norm * 0.1})`;
      ctx.stroke();
    }

    for (let i = 0; i < nodes.length; i++) {
      const n = nodes[i];
      const emphasised = hovered === null || i === hovered || hoverNeighbours.has(i);
      ctx.beginPath();
      ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2);
      ctx.fillStyle = n.color;
      ctx.globalAlpha = emphasised ? 1 : 0.22;
      ctx.fill();
      if (n.isCenter || i === hovered) {
        ctx.lineWidth = 2 / scale;
        ctx.strokeStyle = readVar('--accent', '#5eead4');
        ctx.stroke();
      }
      ctx.globalAlpha = 1;
    }

    // Label the biggest nodes only, and only once the view is zoomed enough
    // that the text will not collide into noise.
    const labelled = [...nodes.keys()]
      .sort((a, b) => nodes[b].r - nodes[a].r)
      .slice(0, scale > 1.5 ? 44 : 18);
    ctx.font = `${11 / scale}px ui-monospace, monospace`;
    ctx.textAlign = 'center';
    for (const i of labelled) {
      const n = nodes[i];
      if (hovered !== null && i !== hovered && !hoverNeighbours.has(i)) continue;
      ctx.fillStyle = i === hovered ? textColor : faint;
      ctx.fillText(n.basename || '', n.x, n.y - n.r - 4 / scale);
    }

    ctx.restore();
  }

  function frame() {
    if (!running) return;
    step();
    draw();
    requestAnimationFrame(frame);
  }

  /* ------------------------------------------------------ interaction -- */

  function pick(px, py) {
    const w = toWorld(px, py);
    let best = null;
    let bestDist = Number.POSITIVE_INFINITY;
    for (let i = 0; i < nodes.length; i++) {
      const n = nodes[i];
      const d = Math.hypot(n.x - w.x, n.y - w.y);
      if (d < n.r + 6 / scale && d < bestDist) {
        best = i;
        bestDist = d;
      }
    }
    return best;
  }

  canvas.addEventListener('pointermove', (e) => {
    const rect = canvas.getBoundingClientRect();
    const px = e.clientX - rect.left;
    const py = e.clientY - rect.top;

    if (dragging !== null) {
      const w = toWorld(px, py);
      nodes[dragging].x = w.x;
      nodes[dragging].y = w.y;
      nodes[dragging].vx = 0;
      nodes[dragging].vy = 0;
      alpha = Math.max(alpha, 0.35);
      return;
    }
    if (panning) {
      offsetX += px - lastPointer.x;
      offsetY += py - lastPointer.y;
      lastPointer = { x: px, y: py };
      return;
    }

    const hit = pick(px, py);
    if (hit !== hovered) {
      hovered = hit;
      canvas.style.cursor = hit === null ? 'grab' : 'pointer';
    }
    if (hit !== null) {
      const n = nodes[hit];
      tooltip.innerHTML =
        `<div class="t">${escapeHtml(n.path)}</div>` +
        `<div class="m">${n.change_count} changes · ${n.degree} coupling partner${n.degree === 1 ? '' : 's'} · click to open</div>`;
      tooltip.classList.add('on');
      tooltip.style.left = Math.min(px + 14, width - 350) + 'px';
      tooltip.style.top = Math.max(py - 40, 6) + 'px';
    } else {
      tooltip.classList.remove('on');
    }
  });

  canvas.addEventListener('pointerdown', (e) => {
    const rect = canvas.getBoundingClientRect();
    const px = e.clientX - rect.left;
    const py = e.clientY - rect.top;
    const hit = pick(px, py);
    canvas.setPointerCapture(e.pointerId);
    if (hit !== null) {
      dragging = hit;
      nodes[hit].pinned = true;
    } else {
      panning = true;
      lastPointer = { x: px, y: py };
      canvas.classList.add('dragging');
    }
  });

  canvas.addEventListener('pointerup', (e) => {
    if (dragging !== null) {
      nodes[dragging].pinned = false;
      dragging = null;
    }
    panning = false;
    canvas.classList.remove('dragging');
    canvas.releasePointerCapture(e.pointerId);
  });

  canvas.addEventListener('click', (e) => {
    const rect = canvas.getBoundingClientRect();
    const hit = pick(e.clientX - rect.left, e.clientY - rect.top);
    if (hit === null) return;
    const id = nodes[hit].id;
    if (e.shiftKey) opts.onNodeFocus && opts.onNodeFocus(id);
    else opts.onNodeClick && opts.onNodeClick(id);
  });

  canvas.addEventListener(
    'wheel',
    (e) => {
      e.preventDefault();
      const rect = canvas.getBoundingClientRect();
      const px = e.clientX - rect.left;
      const py = e.clientY - rect.top;
      const before = toWorld(px, py);
      scale = Math.max(0.15, Math.min(6, scale * (e.deltaY < 0 ? 1.13 : 1 / 1.13)));
      const after = toWorld(px, py);
      // Keep the point under the cursor fixed while zooming.
      offsetX += (after.x - before.x) * scale;
      offsetY += (after.y - before.y) * scale;
    },
    { passive: false },
  );

  canvas.addEventListener('pointerleave', () => {
    tooltip.classList.remove('on');
    hovered = null;
  });

  /* --------------------------------------------------------- controls -- */

  const button = (label, title, fn) => {
    const b = document.createElement('button');
    b.className = 'btn sm';
    b.textContent = label;
    b.title = title;
    b.addEventListener('click', fn);
    controls.appendChild(b);
    return b;
  };

  button('−', 'Zoom out', () => { scale = Math.max(0.15, scale / 1.3); });
  button('+', 'Zoom in', () => { scale = Math.min(6, scale * 1.3); });
  button('Fit', 'Fit the graph to the viewport', fit);
  button('Reheat', 'Restart the layout simulation', () => {
    alpha = 1;
    for (const n of nodes) n.pinned = false;
  });

  function fit() {
    if (!nodes.length) return;
    const xs = nodes.map((n) => n.x);
    const ys = nodes.map((n) => n.y);
    const w = Math.max(1, Math.max(...xs) - Math.min(...xs));
    const hgt = Math.max(1, Math.max(...ys) - Math.min(...ys));
    scale = Math.max(0.15, Math.min(3, Math.min((width - 90) / w, (height - 90) / hgt)));
    offsetX = -((Math.max(...xs) + Math.min(...xs)) / 2) * scale;
    offsetY = -((Math.max(...ys) + Math.min(...ys)) / 2) * scale;
  }

  /* -------------------------------------------------------- lifecycle -- */

  const onResize = () => {
    resize();
    draw();
  };
  window.addEventListener('resize', onResize);

  // Stop the animation loop when the graph leaves the DOM, so navigating away
  // does not leave a requestAnimationFrame running forever.
  const observer = new MutationObserver(() => {
    if (!document.body.contains(container)) {
      running = false;
      window.removeEventListener('resize', onResize);
      observer.disconnect();
    }
  });
  observer.observe(document.body, { childList: true, subtree: true });

  resize();
  // Let the simulation settle before the first paint so the graph does not
  // visibly explode outward from the seed spiral.
  for (let i = 0; i < 120; i++) step();
  fit();
  frame();
}

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[c]);
}
