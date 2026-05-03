/* ===== SARVIS Emotion Orb — Canvas2D, multi-style ===== */

/* ── 사이클 #19: Claude 스타일 따뜻한 감정 팔레트 ───────────────
 * 다크 사이파이 네온 → muted, 자연스러운 톤. 각 감정은 의미적으로 매칭되는
 * 색상 + 펄스 속도를 가진다 (예: thinking=violet/slow, listening=warm amber).
 * 배경이 라이트(#FAF9F7) 라서 too-bright 컬러는 안 보이므로, 채도/명도 적당히. */
const PALETTES = {
  neutral:   { primary: [201, 100, 66],  glow: [232, 165, 130], pulseRate: 0.4, intensity: 0.35 },
  listening: { primary: [217, 164, 65],  glow: [240, 200, 110], pulseRate: 1.5, intensity: 0.65 },
  thinking:  { primary: [139, 122, 184], glow: [180, 165, 215], pulseRate: 1.8, intensity: 0.75 },
  speaking:  { primary: [106, 144, 121], glow: [160, 195, 175], pulseRate: 2.6, intensity: 0.95 },
  happy:     { primary: [217, 164, 65],  glow: [245, 215, 130], pulseRate: 1.2, intensity: 0.7  },
  surprised: { primary: [217, 164, 65],  glow: [245, 215, 130], pulseRate: 2.0, intensity: 0.85 },
  sad:       { primary: [91, 123, 160],  glow: [140, 170, 200], pulseRate: 0.6, intensity: 0.35 },
  angry:     { primary: [200, 74, 63],   glow: [225, 130, 120], pulseRate: 2.4, intensity: 0.9  },
  concerned: { primary: [139, 122, 184], glow: [180, 165, 215], pulseRate: 0.8, intensity: 0.45 },
  alert:     { primary: [200, 74, 63],   glow: [225, 130, 120], pulseRate: 2.8, intensity: 1.0  },
  error:     { primary: [200, 74, 63],   glow: [225, 130, 120], pulseRate: 2.4, intensity: 0.9  },
};

const ORB_STYLES = ['orbital', 'pulse', 'reactor', 'neural'];

class EmotionOrb {
  constructor(canvas, opts = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.targetEmotion = 'neutral';
    this.alphaMult = opts.alphaMult ?? 1.0;
    this.t0 = performance.now();
    this.style = ORB_STYLES.includes(opts.style) ? opts.style : 'orbital';
    this._raf = null;

    // 모든 스타일에서 사용할 자원들
    this.particles = this._initParticles(opts.particles ?? 70);
    this.nodes = this._initNodes(14);
    this.pulseRings = this._initPulseRings(5);

    // 보간 팔레트
    const base = PALETTES.neutral;
    this._curP = {
      primary:   [...base.primary],
      glow:      [...base.glow],
      pulseRate: base.pulseRate,
      intensity: base.intensity,
    };

    // 음성 진폭
    this._amp = 0;
    this._ampTarget = 0;

    // 대화 상태 — listening/thinking/speaking/idle 시각 차별화에 사용.
    // 감정 팔레트와 직교 (감정은 색을 결정, 상태는 호흡/리듬을 결정).
    this.state = 'idle';

    this._observe();
    this._loop();
  }

  setEmotion(name) {
    if (PALETTES[name]) this.targetEmotion = name;
  }

  setAmplitude(val) {
    this._ampTarget = Math.min(1, Math.max(0, val));
  }

  setStyle(name) {
    if (ORB_STYLES.includes(name)) this.style = name;
  }

  /** 대화 상태 — listening / thinking / speaking / idle.
   *  감정과 별개로 "어떻게 살아있는지" 를 표현 (호흡 속도/진폭/오버레이). */
  setState(state) {
    const allowed = new Set(['idle', 'listening', 'thinking', 'speaking']);
    if (allowed.has(state)) this.state = state;
  }

  // ---------- 자원 초기화 ----------
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

  _initNodes(n) {
    const arr = [];
    for (let i = 0; i < n; i++) {
      const a = (i / n) * Math.PI * 2 + Math.random() * 0.3;
      const r = 0.55 + Math.random() * 0.45;
      arr.push({
        baseAngle: a,
        baseRadius: r,
        wobblePhase: Math.random() * Math.PI * 2,
        wobbleAmt: 0.04 + Math.random() * 0.06,
        size: 2 + Math.random() * 2.5,
      });
    }
    return arr;
  }

  _initPulseRings(n) {
    const arr = [];
    for (let i = 0; i < n; i++) {
      arr.push({ phase: i / n });
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
    this._ro = new ResizeObserver(update);
    this._ro.observe(this.canvas);
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
    const LERP = 0.055;
    const p = this._curP;
    p.primary   = this._lerpC(p.primary, tgt.primary, LERP);
    p.glow      = this._lerpC(p.glow,    tgt.glow,    LERP);
    p.pulseRate = p.pulseRate + (tgt.pulseRate - p.pulseRate) * LERP;
    p.intensity = p.intensity + (tgt.intensity - p.intensity) * LERP;

    // ---- 음성 진폭 부드러운 반응 ----
    this._amp += (this._ampTarget - this._amp) * 0.15;
    this._ampTarget *= 0.88;

    // ---- 대화 상태별 리듬 ----
    // idle: 천천히 호흡 (사람이 숨쉬듯) — pulseRate 줄여서 차분.
    // listening: 빠른 펄스 + 살짝 큰 진폭 (귀 기울임).
    // thinking: 느린 호흡 + intensity 증가 (집중).
    // speaking: 빠른 펄스 + 큰 호흡 (활발).
    // 색 팔레트는 그대로 두고 호흡 모양만 바꿔 "AI 가 살아있다" 는 느낌 강화.
    let stateRateMult = 1.0;
    let stateBreathAmp = 0.06;  // 기본 호흡 진폭
    let stateAmpBoost = 0.0;    // 호흡 외 자체 진폭 (마이크 입력 없을 때도 약간 움직이게)
    switch (this.state) {
      case 'listening':
        stateRateMult = 1.6; stateBreathAmp = 0.08; stateAmpBoost = 0.10;
        break;
      case 'thinking':
        stateRateMult = 0.7; stateBreathAmp = 0.10; stateAmpBoost = 0.06;
        break;
      case 'speaking':
        stateRateMult = 2.0; stateBreathAmp = 0.12; stateAmpBoost = 0.18;
        break;
      case 'idle':
      default:
        // 더 느린 호흡 — 차분히 살아있는 느낌
        stateRateMult = 0.55; stateBreathAmp = 0.07; stateAmpBoost = 0.04;
        break;
    }

    // ---- 공통 상태 ----
    const baseR = Math.min(W, H) * 0.18;
    const effectiveRate = p.pulseRate * stateRateMult;
    const pulse = 1 + stateBreathAmp * Math.sin(t * effectiveRate * Math.PI * 2);
    const synthAmp = stateAmpBoost * (0.5 + 0.5 * Math.sin(t * effectiveRate * Math.PI * 2 + 1.0));
    const ampBump = 1 + 0.35 * (this._amp + synthAmp);
    const radius = baseR * pulse * ampBump;

    ctx.clearRect(0, 0, W, H);

    const state = {
      ctx, W, H, t,
      cx: W / 2, cy: H / 2,
      p, radius, baseR, pulse, ampBump,
      amp: this._amp,
      r0: Math.round(p.primary[0]), g0: Math.round(p.primary[1]), b0: Math.round(p.primary[2]),
      rg: Math.round(p.glow[0]),    gg: Math.round(p.glow[1]),    bg: Math.round(p.glow[2]),
      alphaMult: this.alphaMult,
    };

    // ---- 스타일별 렌더 ----
    switch (this.style) {
      case 'pulse':   this._drawPulse(state);   break;
      case 'reactor': this._drawReactor(state); break;
      case 'neural':  this._drawNeural(state);  break;
      case 'orbital':
      default:        this._drawOrbital(state); break;
    }

    // CSS 변수 동기화 (라벨 색상)
    const root = document.documentElement;
    root.style.setProperty('--emo-primary', `${state.r0} ${state.g0} ${state.b0}`);
    root.style.setProperty('--emo-glow', `${state.rg} ${state.gg} ${state.bg}`);
  }

  // ============================================================
  // STYLE 1: ORBITAL — 토성형 링 + 입자 궤도 + 코어 (기존 디자인)
  // ============================================================
  _drawOrbital(s) {
    const { ctx, t, cx, cy, radius, p, r0, g0, b0, rg, gg, bg, amp, alphaMult } = s;

    // 외곽 글로우 링
    ctx.save();
    ctx.globalCompositeOperation = 'lighter';
    for (let i = 3; i >= 1; i--) {
      const tn = i / 3;
      const alpha = (0.22 + 0.18 * amp) * alphaMult * tn;
      const gr = radius * (1.1 + i * 0.16);
      ctx.beginPath();
      ctx.arc(cx, cy, gr, 0, Math.PI * 2);
      ctx.lineWidth = Math.max(1, 5 - i);
      ctx.strokeStyle = `rgba(${rg},${gg},${bg},${alpha})`;
      ctx.stroke();
    }
    ctx.restore();

    // 회전 타원 링
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
      ctx.lineWidth = thick + amp * 2;
      ctx.strokeStyle = `rgba(${r0},${g0},${b0},${baseAlpha * alphaMult})`;
      ctx.stroke();
      ctx.restore();
    }

    // 코어 구체
    const grad = ctx.createRadialGradient(cx, cy, 0, cx, cy, radius * 0.9);
    grad.addColorStop(0, `rgba(${r0},${g0},${b0},${alphaMult})`);
    grad.addColorStop(0.7, `rgba(${rg},${gg},${bg},${0.5 * alphaMult})`);
    grad.addColorStop(1, `rgba(${rg},${gg},${bg},0)`);
    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.arc(cx, cy, radius * 0.9, 0, Math.PI * 2);
    ctx.fill();

    // 광택
    const hlOff = radius * 0.3;
    const hlR = Math.max(4, radius * 0.16);
    const hlGrad = ctx.createRadialGradient(cx - hlOff, cy - hlOff, 0, cx - hlOff, cy - hlOff, hlR);
    hlGrad.addColorStop(0, `rgba(255,255,255,${0.55 * alphaMult})`);
    hlGrad.addColorStop(1, 'rgba(255,255,255,0)');
    ctx.fillStyle = hlGrad;
    ctx.beginPath();
    ctx.arc(cx - hlOff, cy - hlOff, hlR, 0, Math.PI * 2);
    ctx.fill();

    // 입자
    ctx.save();
    ctx.globalCompositeOperation = 'lighter';
    for (const part of this.particles) {
      const speedBoost = 1 + 2.5 * amp;
      const angle = part.angle + t * part.speed * p.intensity * speedBoost;
      const orbit = radius * part.radiusMult * (1 + 0.08 * Math.sin(t * 2 + part.phase));
      const px = cx + orbit * Math.cos(angle);
      const py = cy + orbit * Math.sin(angle) * 0.45;
      const sz = part.size * 0.8 * (1 + 0.4 * Math.sin(t * 3 + part.phase)) * (1 + 0.6 * amp);
      const grd = ctx.createRadialGradient(px, py, 0, px, py, sz * 2);
      grd.addColorStop(0, `rgba(${r0},${g0},${b0},${0.78 * alphaMult})`);
      grd.addColorStop(1, `rgba(${r0},${g0},${b0},0)`);
      ctx.fillStyle = grd;
      ctx.beginPath();
      ctx.arc(px, py, sz * 2, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.restore();
  }

  // ============================================================
  // STYLE 2: PULSE — 동심원 파동 (소나/레이더형)
  // ============================================================
  _drawPulse(s) {
    const { ctx, t, cx, cy, radius, p, r0, g0, b0, rg, gg, bg, amp, alphaMult, baseR } = s;
    const maxR = Math.min(s.W, s.H) * 0.48;

    // 1) 확산 파동 — 여러 링이 외곽으로 퍼지며 페이드
    ctx.save();
    ctx.globalCompositeOperation = 'lighter';
    const cycleSpeed = 0.4 + 0.6 * p.intensity + 0.4 * amp;
    for (const ring of this.pulseRings) {
      const phase = (t * cycleSpeed + ring.phase) % 1;
      const r = baseR * 1.1 + (maxR - baseR * 1.1) * phase;
      const alpha = (1 - phase) * (0.55 + 0.4 * amp) * alphaMult;
      ctx.beginPath();
      ctx.arc(cx, cy, r, 0, Math.PI * 2);
      ctx.lineWidth = 2 + 2 * (1 - phase) + amp * 2;
      ctx.strokeStyle = `rgba(${rg},${gg},${bg},${alpha})`;
      ctx.stroke();
    }
    ctx.restore();

    // 2) 십자 가이드 라인 (HUD 느낌)
    ctx.save();
    ctx.strokeStyle = `rgba(${rg},${gg},${bg},${0.18 * alphaMult})`;
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 6]);
    ctx.beginPath();
    ctx.moveTo(cx - maxR, cy); ctx.lineTo(cx + maxR, cy);
    ctx.moveTo(cx, cy - maxR); ctx.lineTo(cx, cy + maxR);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.restore();

    // 3) 회전 스캔 라인 — 레이더 sweep
    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate(t * (0.4 + p.intensity * 0.6));
    const sweepGrad = ctx.createLinearGradient(0, 0, maxR, 0);
    sweepGrad.addColorStop(0, `rgba(${r0},${g0},${b0},${0.6 * alphaMult})`);
    sweepGrad.addColorStop(1, `rgba(${r0},${g0},${b0},0)`);
    ctx.strokeStyle = sweepGrad;
    ctx.lineWidth = 2 + amp * 2;
    ctx.beginPath();
    ctx.moveTo(0, 0);
    ctx.lineTo(maxR, 0);
    ctx.stroke();
    ctx.restore();

    // 4) 중앙 코어 — 작은 펄스 노드
    const coreR = baseR * 0.45 * (1 + 0.15 * amp);
    const grad = ctx.createRadialGradient(cx, cy, 0, cx, cy, coreR);
    grad.addColorStop(0, `rgba(255,255,255,${0.85 * alphaMult})`);
    grad.addColorStop(0.4, `rgba(${r0},${g0},${b0},${alphaMult})`);
    grad.addColorStop(1, `rgba(${rg},${gg},${bg},0)`);
    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.arc(cx, cy, coreR, 0, Math.PI * 2);
    ctx.fill();

    // 5) 외곽 경계 링
    ctx.beginPath();
    ctx.arc(cx, cy, maxR, 0, Math.PI * 2);
    ctx.strokeStyle = `rgba(${r0},${g0},${b0},${0.35 * alphaMult})`;
    ctx.lineWidth = 1.5;
    ctx.stroke();
  }

  // ============================================================
  // STYLE 3: REACTOR — Iron Man arc reactor (삼각형 + 동심 폴리곤 + 호 세그먼트)
  // ============================================================
  _drawReactor(s) {
    const { ctx, t, cx, cy, radius, p, r0, g0, b0, rg, gg, bg, amp, alphaMult } = s;
    const R = radius * 1.5;

    // 1) 외곽 호 세그먼트 (분절 링)
    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate(t * 0.5 * p.intensity);
    const segs = 12;
    for (let i = 0; i < segs; i++) {
      const a0 = (i / segs) * Math.PI * 2;
      const a1 = a0 + (Math.PI * 2 / segs) * 0.7;
      ctx.beginPath();
      ctx.arc(0, 0, R * 1.1, a0, a1);
      ctx.lineWidth = 2.5 + amp * 2;
      ctx.strokeStyle = `rgba(${rg},${gg},${bg},${(0.5 + 0.4 * amp) * alphaMult})`;
      ctx.stroke();
    }
    ctx.restore();

    // 2) 동심 다각형 (육각형 + 삼각형 역방향)
    const drawPoly = (sides, scale, rotSpeed, alpha, lineW) => {
      ctx.save();
      ctx.translate(cx, cy);
      ctx.rotate(t * rotSpeed * p.intensity);
      ctx.beginPath();
      for (let i = 0; i <= sides; i++) {
        const a = (i / sides) * Math.PI * 2 - Math.PI / 2;
        const x = Math.cos(a) * R * scale;
        const y = Math.sin(a) * R * scale;
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.lineWidth = lineW + amp * 1.5;
      ctx.strokeStyle = `rgba(${r0},${g0},${b0},${alpha * alphaMult})`;
      ctx.stroke();
      ctx.restore();
    };
    drawPoly(6, 0.95, 0.3, 0.75, 2);
    drawPoly(6, 0.75, -0.5, 0.65, 1.5);
    drawPoly(3, 0.55, 0.8, 0.85, 2);
    drawPoly(3, 0.40, -1.1, 0.6, 1.2);

    // 3) 호 세그먼트 안쪽 — 짧은 마커들
    ctx.save();
    ctx.translate(cx, cy);
    const markerRot = -t * 0.25 * p.intensity;
    ctx.rotate(markerRot);
    const markers = 24;
    for (let i = 0; i < markers; i++) {
      const a = (i / markers) * Math.PI * 2;
      const r1 = R * 0.85, r2 = R * 0.92;
      ctx.beginPath();
      ctx.moveTo(Math.cos(a) * r1, Math.sin(a) * r1);
      ctx.lineTo(Math.cos(a) * r2, Math.sin(a) * r2);
      ctx.strokeStyle = `rgba(${rg},${gg},${bg},${0.5 * alphaMult})`;
      ctx.lineWidth = 1;
      ctx.stroke();
    }
    ctx.restore();

    // 4) 중앙 빛나는 코어 (작고 강렬한)
    const coreR = R * 0.28 * (1 + 0.18 * amp);
    const grad = ctx.createRadialGradient(cx, cy, 0, cx, cy, coreR);
    grad.addColorStop(0, `rgba(255,255,255,${alphaMult})`);
    grad.addColorStop(0.5, `rgba(${rg},${gg},${bg},${0.9 * alphaMult})`);
    grad.addColorStop(1, `rgba(${r0},${g0},${b0},0)`);
    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.arc(cx, cy, coreR, 0, Math.PI * 2);
    ctx.fill();

    // 5) 코어 주변 외곽 글로우
    ctx.save();
    ctx.globalCompositeOperation = 'lighter';
    const glowGrad = ctx.createRadialGradient(cx, cy, coreR * 0.8, cx, cy, R * 0.55);
    glowGrad.addColorStop(0, `rgba(${rg},${gg},${bg},${(0.35 + 0.3 * amp) * alphaMult})`);
    glowGrad.addColorStop(1, `rgba(${rg},${gg},${bg},0)`);
    ctx.fillStyle = glowGrad;
    ctx.beginPath();
    ctx.arc(cx, cy, R * 0.55, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();
  }

  // ============================================================
  // STYLE 4: NEURAL — 연결된 노드 (별자리/뉴럴넷)
  // ============================================================
  _drawNeural(s) {
    const { ctx, t, cx, cy, radius, p, r0, g0, b0, rg, gg, bg, amp, alphaMult } = s;
    const R = radius * 1.7;

    // 노드 위치 계산
    const points = this.nodes.map(n => {
      const wob = Math.sin(t * 0.8 + n.wobblePhase) * n.wobbleAmt;
      const ang = n.baseAngle + t * 0.08 * p.intensity;
      const r = R * (n.baseRadius + wob);
      return {
        x: cx + Math.cos(ang) * r,
        y: cy + Math.sin(ang) * r,
        size: n.size,
        angle: ang,
      };
    });

    // 1) 노드 간 연결선 (가까운 점끼리)
    ctx.save();
    ctx.globalCompositeOperation = 'lighter';
    for (let i = 0; i < points.length; i++) {
      for (let j = i + 1; j < points.length; j++) {
        const dx = points[i].x - points[j].x;
        const dy = points[i].y - points[j].y;
        const d = Math.sqrt(dx * dx + dy * dy);
        const maxD = R * 0.95;
        if (d > maxD) continue;
        const alpha = (1 - d / maxD) * (0.45 + 0.3 * amp) * alphaMult;
        ctx.beginPath();
        ctx.moveTo(points[i].x, points[i].y);
        ctx.lineTo(points[j].x, points[j].y);
        ctx.strokeStyle = `rgba(${rg},${gg},${bg},${alpha})`;
        ctx.lineWidth = 1 + amp;
        ctx.stroke();
      }
    }
    ctx.restore();

    // 2) 중앙 → 각 노드 연결 (활성도 표시)
    ctx.save();
    ctx.globalCompositeOperation = 'lighter';
    for (let i = 0; i < points.length; i++) {
      const pt = points[i];
      // 펄스 진행 (각 노드별 다른 위상)
      const pulsePos = (t * (0.6 + p.intensity) + i * 0.13) % 1;
      const px = cx + (pt.x - cx) * pulsePos;
      const py = cy + (pt.y - cy) * pulsePos;
      const sz = 1.5 + 1.5 * (1 - pulsePos) + amp * 2;
      const grd = ctx.createRadialGradient(px, py, 0, px, py, sz * 3);
      grd.addColorStop(0, `rgba(${r0},${g0},${b0},${alphaMult * 0.9})`);
      grd.addColorStop(1, `rgba(${r0},${g0},${b0},0)`);
      ctx.fillStyle = grd;
      ctx.beginPath();
      ctx.arc(px, py, sz * 3, 0, Math.PI * 2);
      ctx.fill();

      // 가는 가이드 라인
      const lineGrad = ctx.createLinearGradient(cx, cy, pt.x, pt.y);
      lineGrad.addColorStop(0, `rgba(${rg},${gg},${bg},${0.25 * alphaMult})`);
      lineGrad.addColorStop(1, `rgba(${rg},${gg},${bg},${0.1 * alphaMult})`);
      ctx.strokeStyle = lineGrad;
      ctx.lineWidth = 0.7;
      ctx.beginPath();
      ctx.moveTo(cx, cy);
      ctx.lineTo(pt.x, pt.y);
      ctx.stroke();
    }
    ctx.restore();

    // 3) 노드 자체 그리기
    for (const pt of points) {
      const sz = pt.size * (1 + 0.3 * amp);
      const grd = ctx.createRadialGradient(pt.x, pt.y, 0, pt.x, pt.y, sz * 2.5);
      grd.addColorStop(0, `rgba(255,255,255,${alphaMult})`);
      grd.addColorStop(0.4, `rgba(${rg},${gg},${bg},${0.85 * alphaMult})`);
      grd.addColorStop(1, `rgba(${rg},${gg},${bg},0)`);
      ctx.fillStyle = grd;
      ctx.beginPath();
      ctx.arc(pt.x, pt.y, sz * 2.5, 0, Math.PI * 2);
      ctx.fill();
    }

    // 4) 중앙 메인 노드
    const coreR = radius * 0.45 * (1 + 0.18 * amp);
    const grad = ctx.createRadialGradient(cx, cy, 0, cx, cy, coreR);
    grad.addColorStop(0, `rgba(255,255,255,${alphaMult})`);
    grad.addColorStop(0.4, `rgba(${r0},${g0},${b0},${alphaMult})`);
    grad.addColorStop(1, `rgba(${rg},${gg},${bg},0)`);
    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.arc(cx, cy, coreR, 0, Math.PI * 2);
    ctx.fill();
  }

  destroy() {
    if (this._raf) cancelAnimationFrame(this._raf);
    if (this._ro) { try { this._ro.disconnect(); } catch (_) {} }
  }
}

window.EmotionOrb = EmotionOrb;
window.ORB_STYLES = ORB_STYLES;
