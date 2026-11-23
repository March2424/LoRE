import React, { useEffect, useRef } from "react";

export type AsciiMode = "ascii" | "braille" | "blocks" | "lines" | "hanzi";

export const ASCII_MODES: ReadonlyArray<{ id: AsciiMode; label: string }> = [
  { id: "ascii", label: "ASCII" },
  { id: "braille", label: "盲文点阵" },
  { id: "blocks", label: "像素块" },
  { id: "lines", label: "细线条" },
  { id: "hanzi", label: "水墨汉字" },
];

export const ASCII_STORAGE_MODE = "lore_ascii_mode";
export const ASCII_STORAGE_PAUSED = "lore_ascii_paused";

const MODE_RAMPS: Record<AsciiMode, string[]> = {
  ascii: [" ", ".", ":", "-", "~", "≈", "+", "*", "#", "%", "@"],
  braille: [" ", "⠂", "⠃", "⠇", "⠏", "⠟", "⠿", "⡿", "⣿", "⣷", "⣶"],
  blocks: [" ", "·", "░", "░", "▒", "▒", "▓", "▓", "█", "█", "█"],
  lines: [" ", "·", "-", "─", "═", "╱", "╲", "┼", "╬", "╳", "#"],
  hanzi: [" ", "·", "一", "氵", "川", "波", "鱼", "潮", "渊", "海", "墨"],
};

interface Creature {
  kind: "fish" | "manta" | "jelly" | "school";
  x: number;
  y: number;
  vx: number;
  vy: number;
  phase: number;
  freq: number;
  scale: number;
  depth: number;
}

interface Wake {
  x: number;
  y: number;
  age: number;
  maxAge: number;
  strength: number;
}

function makeCreatures(cols: number, rows: number): Creature[] {
  const list: Creature[] = [];
  const w = Math.max(80, cols);
  const h = Math.max(30, rows);

  // Graceful manta drifting across mid-field
  list.push({
    kind: "manta",
    x: w * 0.72,
    y: h * 0.36,
    vx: -0.068,
    vy: 0.005,
    phase: 0.4,
    freq: 0.036,
    scale: 1.25,
    depth: 0.96,
  });

  // Gentle jellyfish
  for (let i = 0; i < 2; i++) {
    list.push({
      kind: "jelly",
      x: w * (0.2 + i * 0.62),
      y: h * (0.28 + i * 0.42),
      vx: (i % 2 === 0 ? 1 : -1) * 0.02,
      vy: -0.016,
      phase: i * 2.1,
      freq: 0.048,
      scale: 1.0 + i * 0.16,
      depth: 0.76 - i * 0.14,
    });
  }

  // Cruising fish at layered depths
  const fishSpecs = [
    { x: 0.24, y: 0.22, vx: 0.085, scale: 1.08, depth: 0.9 },
    { x: 0.84, y: 0.74, vx: -0.075, scale: 0.95, depth: 0.82 },
    { x: 0.42, y: 0.84, vx: 0.058, scale: 0.82, depth: 0.62 },
    { x: 0.14, y: 0.58, vx: -0.05, scale: 0.78, depth: 0.54 },
  ];
  fishSpecs.forEach((s, idx) => {
    list.push({
      kind: "fish",
      x: w * s.x,
      y: h * s.y,
      vx: s.vx,
      vy: 0,
      phase: idx * 1.4,
      freq: 0.07,
      scale: s.scale,
      depth: s.depth,
    });
  });

  // Small distant school particles
  for (let i = 0; i < 8; i++) {
    list.push({
      kind: "school",
      x: ((i * 19 + 11) % 100) * 0.01 * w,
      y: ((i * 23 + 17) % 80 + 10) * 0.01 * h,
      vx: 0.098 + (i % 3) * 0.014,
      vy: 0,
      phase: i * 0.9,
      freq: 0.085,
      scale: 0.75,
      depth: 0.44 + (i % 3) * 0.12,
    });
  }

  return list;
}

function sampleFish(dx: number, dy: number, facing: number, swim: number, scale: number): number {
  const lx = (dx * facing) / scale;
  const ly = (dy + Math.sin(lx * 0.42 - swim) * 0.36) / scale;
  if (lx >= -3.5 && lx <= 4.5) {
    const norm = (lx + 3.5) / 8;
    const halfH = Math.sin(norm * Math.PI) * 1.9 + (norm > 0.45 && norm < 0.75 ? 0.35 : 0);
    if (Math.abs(ly) <= halfH) {
      if (Math.abs(lx - 2.6) < 0.7 && Math.abs(ly + 0.45) < 0.55) return 0.96;
      return 0.74 + (1 - Math.abs(ly) / Math.max(0.5, halfH)) * 0.22;
    }
    if (lx > -1.2 && lx < 1.2 && ly < -halfH && ly > -halfH - 1.15 && lx < 0.4) {
      return 0.56;
    }
  }
  if (lx >= -6.8 && lx < -3.2) {
    const t = (-3.2 - lx) / 3.6;
    const spread = 0.6 + t * 1.9;
    if (Math.abs(Math.abs(ly) - spread * 0.65) < 0.85) return 0.65;
  }
  return 0;
}

function sampleManta(dx: number, dy: number, facing: number, flap: number, scale: number): number {
  const lx = (dx * facing) / scale;
  const wingWave = Math.sin(flap - Math.abs(dy) * 0.25) * 0.9;
  const ay = Math.abs(dy) / scale;
  const span = 7.5;
  if (ay <= span) {
    const wingRatio = ay / span;
    const front = 4.6 - wingRatio * 7.2 + wingWave * wingRatio * 0.6;
    const back = -2.2 - wingRatio * 2.8 + wingWave * wingRatio * 0.6;
    if (lx <= front && lx >= back) {
      const centerBoost = (1 - wingRatio) * 0.28;
      return Math.min(0.96, 0.56 + centerBoost);
    }
  }
  if (ay < 0.65 && lx < -2.0 && lx > -11.5) {
    const wave = Math.sin(lx * 0.55 + flap * 1.4) * 0.45;
    if (Math.abs(dy / scale - wave) < 0.45) return 0.48;
  }
  return 0;
}

function sampleJelly(dx: number, dy: number, pulse: number, scale: number): number {
  const lx = dx / scale;
  const ly = dy / scale;
  const bellW = 3.2 + Math.sin(pulse) * 0.45;
  if (ly >= -2.4 && ly <= 0.4) {
    const norm = (ly + 2.4) / 2.8;
    const w = Math.sin(norm * Math.PI * 0.55) * bellW;
    if (Math.abs(lx) <= w) return 0.52 + (1 - Math.abs(lx) / Math.max(0.5, w)) * 0.36;
  }
  if (ly > 0.4 && ly <= 5.4) {
    const sway = Math.sin(ly * 0.7 - pulse) * 0.65;
    for (let t = -1.6; t <= 1.61; t += 1.6) {
      if (Math.abs(lx - t - sway * (ly * 0.18)) < 0.42) return 0.42;
    }
  }
  return 0;
}

export function AsciiOcean({
  mode,
  paused,
}: {
  mode: AsciiMode;
  paused: boolean;
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const modeRef = useRef<AsciiMode>(mode);
  const pausedRef = useRef<boolean>(paused);

  modeRef.current = mode;
  pausedRef.current = paused;

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
    const cellW = 10;
    const cellH = 18;

    let width = 0;
    let height = 0;
    let cols = 0;
    let rows = 0;
    let creatures: Creature[] = [];
    const wakes: Wake[] = [];
    let pointerCol = -999;
    let pointerRow = -999;
    let lastWakeAt = 0;

    const resize = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      width = window.innerWidth;
      height = window.innerHeight;
      canvas.width = Math.floor(width * dpr);
      canvas.height = Math.floor(height * dpr);
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      cols = Math.max(24, Math.floor(width / cellW));
      rows = Math.max(14, Math.floor(height / cellH));
      creatures = makeCreatures(cols, rows);
    };

    resize();

    const pushWake = (c: number, r: number, maxAge: number, strength: number) => {
      wakes.push({ x: c, y: r, age: 0.6, maxAge, strength });
      if (wakes.length > 24) wakes.shift();
    };

    const onMove = (e: PointerEvent) => {
      const c = e.clientX / cellW;
      const r = e.clientY / cellH;
      const now = performance.now();
      const dist = Math.hypot(c - pointerCol, r - pointerRow);
      if (dist > 1.4 || now - lastWakeAt > 95) {
        pushWake(c, r, 19, 0.92);
        lastWakeAt = now;
      }
      pointerCol = c;
      pointerRow = r;
    };

    const onDown = (e: PointerEvent) => {
      const c = e.clientX / cellW;
      const r = e.clientY / cellH;
      pointerCol = c;
      pointerRow = r;
      pushWake(c, r, 26, 1.18);
      pushWake(c, r, 20, 0.85);
    };

    const onLeave = () => {
      pointerCol = -999;
      pointerRow = -999;
    };

    window.addEventListener("resize", resize);
    window.addEventListener("pointermove", onMove, { passive: true });
    window.addEventListener("pointerdown", onDown, { passive: true });
    window.addEventListener("blur", onLeave);

    let frame = 0;
    let rafId = 0;
    let lastDraw = 0;

    const renderFrame = (now: number) => {
      rafId = requestAnimationFrame(renderFrame);
      if (pausedRef.current && frame > 0) return;
      if (now - lastDraw < 36 && frame > 0) return;
      lastDraw = now;

      if (!reduceMotion && !pausedRef.current) {
        frame += 1;
        for (const c of creatures) {
          c.phase += c.freq;
          let boostX = 0;
          let boostY = 0;
          if (pointerCol > -100) {
            const dx = c.x - pointerCol;
            const dy = (c.y - pointerRow) * 1.4;
            const d = Math.hypot(dx, dy);
            if (d < 16 && d > 0.5) {
              const push = (1 - d / 16) * 0.08;
              boostX = (dx / d) * push;
              boostY = (dy / d) * push * 0.5;
            }
          }
          c.x += c.vx + boostX;
          c.y += c.vy + Math.sin(c.phase * 0.45) * 0.018 + boostY;
          if (c.vx > 0 && c.x > cols + 16) c.x = -16;
          if (c.vx < 0 && c.x < -16) c.x = cols + 16;
          if (c.y < -10) c.y = rows + 10;
          if (c.y > rows + 10) c.y = -10;
        }
        for (let i = wakes.length - 1; i >= 0; i--) {
          wakes[i].age += 0.42;
          if (wakes[i].age > wakes[i].maxAge) wakes.splice(i, 1);
        }
      }

      ctx.clearRect(0, 0, width, height);
      const currentMode = modeRef.current;
      const ramp = MODE_RAMPS[currentMode] || MODE_RAMPS.ascii;
      const fontFamily =
        currentMode === "hanzi"
          ? '"Geist Mono", "PingFang SC", "Noto Sans SC", monospace'
          : '"Geist Mono", ui-monospace, SFMono-Regular, Menlo, monospace';
      ctx.font = `400 13.5px ${fontFamily}`;
      ctx.textBaseline = "middle";

      const t = frame * 0.055;

      for (let r = 0; r < rows; r++) {
        const py = r * cellH + cellH * 0.5;
        for (let c = 0; c < cols; c++) {
          // Subtle ambient water current ripples across open ocean
          const wave = Math.sin(c * 0.13 - t + r * 0.24) * Math.cos(r * 0.17 + t * 0.65);
          let intensity = wave > 0.46 ? (wave - 0.46) * 0.62 : 0;
          let depth = 0.42;
          let isWake = false;

          // Interactive cursor aura
          if (pointerCol > -100) {
            const pDist = Math.hypot((c - pointerCol) * 0.68, r - pointerRow);
            if (pDist < 3.4) {
              const aura = (1 - pDist / 3.4) * 0.66;
              if (aura > intensity) {
                intensity = aura;
                depth = 0.92;
                isWake = true;
              }
            }
          }

          // Expanding multi-ring pointer ripple wakes
          for (let wIdx = 0; wIdx < wakes.length; wIdx++) {
            const w = wakes[wIdx];
            const dist = Math.hypot((c - w.x) * 0.68, r - w.y);
            const primary = Math.abs(dist - w.age);
            const secondary = w.age > 3.2 ? Math.abs(dist - (w.age - 3.2)) : 99;
            if (primary < 1.85 || secondary < 1.35) {
              const life = 1 - w.age / w.maxAge;
              const pFade = primary < 1.85 ? life * (1 - primary / 1.85) * w.strength : 0;
              const sFade = secondary < 1.35 ? life * (1 - secondary / 1.35) * w.strength * 0.62 : 0;
              const rVal = Math.max(pFade, sFade);
              if (rVal > intensity) {
                intensity = Math.min(1, rVal);
                depth = Math.max(depth, 0.95);
                isWake = true;
              }
            }
          }

          // Creatures
          for (let i = 0; i < creatures.length; i++) {
            const cr = creatures[i];
            const dx = c - cr.x;
            const dy = r - cr.y;
            if (Math.abs(dx) > 16 || Math.abs(dy) > 11) continue;

            let sample = 0;
            if (cr.kind === "fish") {
              sample = sampleFish(dx, dy, cr.vx >= 0 ? 1 : -1, cr.phase, cr.scale);
            } else if (cr.kind === "manta") {
              sample = sampleManta(dx, dy, cr.vx >= 0 ? 1 : -1, cr.phase, cr.scale);
            } else if (cr.kind === "jelly") {
              sample = sampleJelly(dx, dy, cr.phase, cr.scale);
            } else {
              if (Math.abs(dy) < 0.6 && Math.abs(dx) < 1.45) sample = 0.58;
            }
            if (sample > intensity) {
              intensity = sample;
              depth = Math.max(depth, cr.depth);
              isWake = false;
            }
          }

          if (intensity <= 0.04) continue;
          const idx = Math.min(ramp.length - 1, Math.max(1, Math.round(intensity * (ramp.length - 1))));
          const ch = isWake && currentMode === "ascii"
            ? (intensity > 0.68 ? "≈" : intensity > 0.36 ? "~" : "·")
            : ramp[idx];
          if (!ch || ch === " ") continue;

          const baseAlpha = isWake ? 0.24 + intensity * 0.48 : 0.14 + intensity * depth * 0.48;
          const alpha = Math.min(0.74, baseAlpha);
          ctx.fillStyle = `rgba(48, 53, 45, ${alpha.toFixed(3)})`;
          ctx.fillText(ch, c * cellW, py);
        }
      }
    };

    rafId = requestAnimationFrame(renderFrame);

    return () => {
      cancelAnimationFrame(rafId);
      window.removeEventListener("resize", resize);
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerdown", onDown);
      window.removeEventListener("blur", onLeave);
    };
  }, []);

  return <canvas id="ocean" ref={canvasRef} className="ascii-ocean" aria-hidden="true" />;
}

export function AsciiDock({
  mode,
  paused,
  onCycleMode,
  onTogglePause,
}: {
  mode: AsciiMode;
  paused: boolean;
  onCycleMode: () => void;
  onTogglePause: () => void;
}) {
  const modeLabel = ASCII_MODES.find((m) => m.id === mode)?.label ?? "ASCII";
  return (
    <div className="ascii-dock glass" role="group" aria-label="背景字符风格控制">
      <button
        type="button"
        className="ascii-dock__btn"
        onClick={onCycleMode}
        title="切换背景字符材质 (ASCII / 盲文点阵 / 像素块 / 细线条 / 水墨汉字)"
      >
        <span className="ascii-dock__tag">~</span>
        <span>{modeLabel}</span>
      </button>
      <span className="ascii-dock__sep" aria-hidden="true" />
      <button
        type="button"
        className="ascii-dock__icon"
        onClick={onTogglePause}
        title={paused ? "继续海流动画" : "暂停海流动画"}
        aria-label={paused ? "继续海流动画" : "暂停海流动画"}
      >
        {paused ? (
          <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
            <polygon points="7 5 19 12 7 19 7 5" />
          </svg>
        ) : (
          <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
            <rect x="6" y="5" width="4" height="14" rx="1" />
            <rect x="14" y="5" width="4" height="14" rx="1" />
          </svg>
        )}
      </button>
    </div>
  );
}
