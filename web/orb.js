/* ===== SARVIS Emotion Orb — Canvas2D ===== */

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
    this.targetEmotion = 'neutral';
    this.alphaMult = opts.alphaMult ?? 1.0;
    this.t0 = performance.now();
    this.particles = this._initParticles(opts.particles ?? 70);
    this._raf = null;

    // 현재 렌더링 팔레트 (보간 상태) — PALETTES 원본을 절대 수정하지 않음
    const base = PALETTES.neutral;
    this._curP = {
      primary:   [...base.primary],
      glow:      [...base.glow],
      pulseRate: base.pulseRate,
      intensity: base.intensity,
    };

    // 음성 진폭 (0~1), 감쇠 방식
    this._amp = 0;
    this._ampTarget = 0;

    this._observe();
    this._loop();
  }

  // 외부에서 감정 설정
  setEmotion(name) {
    if (PALETTES[name]) this.targetEmotion = name;
  }

  // 외부에서 음성 진폭 전달 (0~1)
  setAmplitude(val) {
    this._ampTarget = Math.min(1, Math.max(0, val));
  }

  _initParticles(n) {
    const arr = [];
    for (let i = 0; i < n; i++) {
      arr.push({
        angle:      Math.random() * Math.PI * 2,
        radiusMult: 0.7 + Math.random() * 1.0,
        speed:      (0.3 + Math.random() * 0.7) * (Math.random() < 0.5 ? -1 : 1),
        size:       1.2 + Math.random() * 1.8,
        phase:      Math.random() * Math.PI * 2,
      });
    }
    return arr;
  }

  _lerpC(a, b, t) {
    return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t];
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

    // ---- 감정 팔레트 부드러운 보간 (PALETTES 원본 보존) ----
    const tgt = PALETTES[this.targetEmotion];
    const LERP_SPEED = 0.055; // 매 프레임 ~5.5% → 약 0.5초 전환
    const p = this._curP;
    p.primary   = this._lerpC(p.primary, tgt.primary, LERP_SPEED);
    p.glow      = this._lerpC(p.glow,    tgt.glow,    LERP_SPEED);
    p.pulseRate = p.pulseRate + (tgt.pulseRate - p.pulseRate) * LERP_SPEED;
    p.intensity = p.intensity + (tgt.intensity - p.intensity) * LERP_SPEED;

    // ---- 음성 진폭 부드러운 반응 ----
    this._amp += (this._ampTarget - this._amp) * 0.15;
    this._ampTarget *= 0.88; // 자연 감쇠

    // ---- 기본 반지름 계산 (진폭에 따라 떨림) ----
    const baseR = Math.min(W, H) * 0.18;
    const pulse = 1 + 0.06 * Math.sin(t * p.pulseRate * Math.PI * 2);
    const ampBump = 1 + 0.35 * this._amp; // 진폭이 클수록 오브 팽창
    const radius = baseR * pulse * ampBump;

    // ---- 배경 클리어 ----
    ctx.clearRect(0, 0, W, H);

    const cx = W / 2, cy = H / 2;
    const [r0, g0, b0] = p.primary.map(Math.round);
    const [rg, gg, bg] = p.glow.map(Math.round);

    // ===== 1) 외곽 글로우 링 (진폭에 따라 강도 증가) =====
    ctx.save();
    ctx.globalCompositeOperation = 'lighter';
    for (let i = 3; i >= 1; i--) {
      const tn = i / 3;
      const alpha = (0.22 + 0.18 * this._amp) * this.alphaMult * tn;
      const gr = radius * (1.1 + i * 0.16);
      ctx.beginPath();
      ctx.arc(cx, cy, gr, 0, Math.PI * 2);
      ctx.lineWidth = Math.max(1, 5 - i);
      ctx.strokeStyle = `rgba(${rg},${gg},${bg},${alpha})`;
      ctx.stroke();
    }
    ctx.restore();

    // ===== 2) 회전 타원 링 4개 (진폭에 따라 두께 증가) =====
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
      ctx.lineWidth = thick + this._amp * 2;
      ctx.strokeStyle = `rgba(${r0},${g0},${b0},${baseAlpha * this.alphaMult})`;
      ctx.stroke();
      ctx.restore();
    }

    // ===== 3) 코어 구체 =====
    const grad = ctx.createRadialGradient(cx, cy, 0, cx, cy, radius * 0.9);
    grad.addColorStop(0, `rgba(${r0},${g0},${b0},${this.alphaMult})`);
    grad.addColorStop(0.7, `rgba(${rg},${gg},${bg},${0.5 * this.alphaMult})`);
    grad.addColorStop(1, `rgba(${rg},${gg},${bg},0)`);
    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.arc(cx, cy, radius * 0.9, 0, Math.PI * 2);
    ctx.fill();

    // ===== 4) 광택 하이라이트 =====
    const hlOff = radius * 0.3;
    const hlR = Math.max(4, radius * 0.16);
    const hlGrad = ctx.createRadialGradient(cx - hlOff, cy - hlOff, 0, cx - hlOff, cy - hlOff, hlR);
    hlGrad.addColorStop(0, `rgba(255,255,255,${0.55 * this.alphaMult})`);
    hlGrad.addColorStop(1, 'rgba(255,255,255,0)');
    ctx.fillStyle = hlGrad;
    ctx.beginPath();
    ctx.arc(cx - hlOff, cy - hlOff, hlR, 0, Math.PI * 2);
    ctx.fill();

    // ===== 5) 입자 (진폭에 따라 속도·크기 증가) =====
    ctx.save();
    ctx.globalCompositeOperation = 'lighter';
    for (const part of this.particles) {
      const speedBoost = 1 + 2.5 * this._amp;
      const angle = part.angle + t * part.speed * p.intensity * speedBoost;
      const orbit = radius * part.radiusMult * (1 + 0.08 * Math.sin(t * 2 + part.phase));
      const px = cx + orbit * Math.cos(angle);
      const py = cy + orbit * Math.sin(angle) * 0.45;
      const sz = part.size * 0.8 * (1 + 0.4 * Math.sin(t * 3 + part.phase)) * (1 + 0.6 * this._amp);
      const grd = ctx.createRadialGradient(px, py, 0, px, py, sz * 2);
      grd.addColorStop(0, `rgba(${r0},${g0},${b0},${0.78 * this.alphaMult})`);
      grd.addColorStop(1, `rgba(${r0},${g0},${b0},0)`);
      ctx.fillStyle = grd;
      ctx.beginPath();
      ctx.arc(px, py, sz * 2, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.restore();

    // CSS 변수 동기화 (라벨 색상)
    const root = document.documentElement;
    root.style.setProperty('--emo-primary', `${r0} ${g0} ${b0}`);
    root.style.setProperty('--emo-glow', `${rg} ${gg} ${bg}`);
  }

  destroy() {
    if (this._raf) cancelAnimationFrame(this._raf);
  }
}

window.EmotionOrb = EmotionOrb;
