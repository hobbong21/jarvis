/* ===== JARVIS Emotion Orb — Canvas2D 포팅 ===== */
// pygame 의 _draw_orb 와 동일한 패턴: 외곽 글로우, 회전 링 4개, 코어 구체, 광택, 입자

const PALETTES = {
  neutral:   { primary: [40, 140, 220],  glow: [0, 180, 255],   pulseRate: 0.5, intensity: 0.35 },
  listening: { primary: [255, 170, 60],  glow: [255, 200, 80],  pulseRate: 1.6, intensity: 0.65 },
  thinking:  { primary: [0, 200, 255],   glow: [50, 220, 255],  pulseRate: 2.4, intensity: 0.9  },
  speaking:  { primary: [100, 240, 255], glow: [150, 255, 255], pulseRate: 3.2, intensity: 1.0  },
  happy:     { primary: [100, 255, 180], glow: [160, 255, 210], pulseRate: 1.4, intensity: 0.75 },
  concerned: { primary: [180, 100, 255], glow: [220, 160, 255], pulseRate: 1.0, intensity: 0.5  },
  alert:     { primary: [255, 80, 100],  glow: [255, 140, 160], pulseRate: 2.8, intensity: 1.0  },
};

class EmotionOrb {
  constructor(canvas, opts = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.emotion = 'neutral';
    this.targetEmotion = 'neutral';
    this.alphaMult = opts.alphaMult ?? 1.0;
    this.t0 = performance.now();
    this.particles = this._initParticles(opts.particles ?? 70);
    this._raf = null;
    this._observe();
    this._loop();
  }

  setEmotion(name) {
    if (PALETTES[name]) this.targetEmotion = name;
  }

  _initParticles(n) {
    const arr = [];
    for (let i = 0; i < n; i++) {
      arr.push({
        angle: Math.random() * Math.PI * 2,
        radiusMult: 0.7 + Math.random() * 1.0,
        speed: (0.3 + Math.random() * 0.7) * (Math.random() < 0.5 ? -1 : 1),
        size: 1.2 + Math.random() * 1.8,
        phase: Math.random() * Math.PI * 2,
      });
    }
    return arr;
  }

  // 색상 보간 (감정 전환 부드럽게)
  _lerpPalette(a, b, t) {
    const lerp = (x, y) => x + (y - x) * t;
    const lc = (x, y) => [lerp(x[0], y[0]), lerp(x[1], y[1]), lerp(x[2], y[2])];
    return {
      primary: lc(a.primary, b.primary),
      glow: lc(a.glow, b.glow),
      pulseRate: lerp(a.pulseRate, b.pulseRate),
      intensity: lerp(a.intensity, b.intensity),
    };
  }

  _observe() {
    const update = () => {
      const r = this.canvas.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      this.canvas.width = Math.round(r.width * dpr);
      this.canvas.height = Math.round(r.height * dpr);
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      this._w = r.width;
      this._h = r.height;
    };
    update();
    new ResizeObserver(update).observe(this.canvas);
  }

  _loop = () => {
    this._draw();
    this._raf = requestAnimationFrame(this._loop);
  };

  _draw() {
    const ctx = this.ctx;
    const W = this._w, H = this._h;
    if (!W || !H) return;
    const t = (performance.now() - this.t0) / 1000;

    // 감정 전환 보간
    const cur = PALETTES[this.emotion];
    const tgt = PALETTES[this.targetEmotion];
    const blendT = Math.min(1, 0.06);  // 매 프레임 6% 씩 보간 → 약 0.5초 전환
    const p = this._lerpPalette(cur, tgt, blendT);
    // 보간을 누적: 현재 색을 cur 에 저장
    Object.assign(PALETTES[this.emotion], {
      primary: p.primary, glow: p.glow,
      pulseRate: p.pulseRate, intensity: p.intensity,
    });
    if (this.emotion !== this.targetEmotion) {
      const close = (a, b) => Math.abs(a[0] - b[0]) + Math.abs(a[1] - b[1]) + Math.abs(a[2] - b[2]) < 5;
      if (close(p.primary, tgt.primary)) {
        this.emotion = this.targetEmotion;
      }
    }

    const cx = W / 2;
    const cy = H / 2;
    const baseR = Math.min(W, H) * 0.18;
    const pulse = 1 + 0.06 * Math.sin(t * p.pulseRate * Math.PI * 2);
    const radius = baseR * pulse;

    // 배경 클리어 (투명)
    ctx.clearRect(0, 0, W, H);

    // ===== 1) 외곽 글로우 링 3개 =====
    ctx.save();
    ctx.globalCompositeOperation = 'lighter';
    for (let i = 3; i >= 1; i--) {
      const tn = i / 3;
      const alpha = 0.22 * this.alphaMult * tn;
      const r = radius * (1.1 + i * 0.16);
      ctx.beginPath();
      ctx.arc(cx, cy, r, 0, Math.PI * 2);
      ctx.lineWidth = Math.max(1, 5 - i);
      ctx.strokeStyle = `rgba(${p.glow[0]|0}, ${p.glow[1]|0}, ${p.glow[2]|0}, ${alpha})`;
      ctx.stroke();
    }
    ctx.restore();

    // ===== 2) 회전 타원 링 4개 =====
    const ringSpecs = [
      [0.35, 1.5, 0.45, 2, 0.86],
      [-0.55, 1.85, 0.65, 2, 0.70],
      [0.85, 2.2, 0.35, 2, 0.59],
      [-1.2, 2.6, 0.25, 1, 0.47],
    ];
    for (const [rotSpeed, rx, ry, thick, baseAlpha] of ringSpecs) {
      ctx.save();
      ctx.translate(cx, cy);
      ctx.rotate(t * rotSpeed * p.intensity);
      ctx.beginPath();
      ctx.ellipse(0, 0, radius * rx, radius * ry, 0, 0, Math.PI * 2);
      ctx.lineWidth = thick;
      ctx.strokeStyle = `rgba(${p.primary[0]|0}, ${p.primary[1]|0}, ${p.primary[2]|0}, ${baseAlpha * this.alphaMult})`;
      ctx.stroke();
      ctx.restore();
    }

    // ===== 3) 코어 구체 (방사형 그라데이션) =====
    const grad = ctx.createRadialGradient(cx, cy, 0, cx, cy, radius * 0.9);
    grad.addColorStop(0, `rgba(${p.primary[0]|0}, ${p.primary[1]|0}, ${p.primary[2]|0}, ${this.alphaMult})`);
    grad.addColorStop(0.7, `rgba(${p.glow[0]|0}, ${p.glow[1]|0}, ${p.glow[2]|0}, ${0.5 * this.alphaMult})`);
    grad.addColorStop(1, `rgba(${p.glow[0]|0}, ${p.glow[1]|0}, ${p.glow[2]|0}, 0)`);
    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.arc(cx, cy, radius * 0.9, 0, Math.PI * 2);
    ctx.fill();

    // ===== 4) 좌상단 광택 하이라이트 =====
    const hlOff = radius * 0.3;
    const hlR = Math.max(4, radius * 0.16);
    const hlGrad = ctx.createRadialGradient(
      cx - hlOff, cy - hlOff, 0,
      cx - hlOff, cy - hlOff, hlR,
    );
    hlGrad.addColorStop(0, `rgba(255, 255, 255, ${0.55 * this.alphaMult})`);
    hlGrad.addColorStop(1, 'rgba(255, 255, 255, 0)');
    ctx.fillStyle = hlGrad;
    ctx.beginPath();
    ctx.arc(cx - hlOff, cy - hlOff, hlR, 0, Math.PI * 2);
    ctx.fill();

    // ===== 5) 입자 (타원 궤도) =====
    ctx.save();
    ctx.globalCompositeOperation = 'lighter';
    for (const part of this.particles) {
      const angle = part.angle + t * part.speed * p.intensity;
      const orbit = radius * part.radiusMult * (1 + 0.08 * Math.sin(t * 2 + part.phase));
      const px = cx + orbit * Math.cos(angle);
      const py = cy + orbit * Math.sin(angle) * 0.45;
      const sz = part.size * 0.8 * (1 + 0.4 * Math.sin(t * 3 + part.phase));
      const grd = ctx.createRadialGradient(px, py, 0, px, py, sz * 2);
      grd.addColorStop(0, `rgba(${p.primary[0]|0}, ${p.primary[1]|0}, ${p.primary[2]|0}, ${0.78 * this.alphaMult})`);
      grd.addColorStop(1, `rgba(${p.primary[0]|0}, ${p.primary[1]|0}, ${p.primary[2]|0}, 0)`);
      ctx.fillStyle = grd;
      ctx.beginPath();
      ctx.arc(px, py, sz * 2, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.restore();

    // CSS 변수 동기화 (라벨 색상)
    if (!this._lastEmoCss || this._lastEmoCss !== this.targetEmotion) {
      const root = document.documentElement;
      root.style.setProperty('--emo-primary', `${tgt.primary[0]|0} ${tgt.primary[1]|0} ${tgt.primary[2]|0}`);
      root.style.setProperty('--emo-glow', `${tgt.glow[0]|0} ${tgt.glow[1]|0} ${tgt.glow[2]|0}`);
      this._lastEmoCss = this.targetEmotion;
    }
  }

  destroy() {
    if (this._raf) cancelAnimationFrame(this._raf);
  }
}

window.EmotionOrb = EmotionOrb;
