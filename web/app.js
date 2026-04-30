/* ===== JARVIS Web Client ===== */
(() => {
  // ---------- DOM ----------
  const $ = (id) => document.getElementById(id);

  const loginScreen = $('login-screen');
  const mainScreen = $('main-screen');
  const loginForm = $('login-form');
  const loginMsg = $('login-msg');
  const loginSub = $('login-sub');
  const loginBtn = $('login-btn');
  const usernameInput = $('username');
  const passwordInput = $('password');

  const orbCanvas = $('orb');
  const loginOrbCanvas = $('login-orb');
  const emotionLabel = $('emotion-label');
  const hintLabel = $('hint-label');
  const statePill = $('state-pill');
  const userLabel = $('user-label');
  const backendLabel = $('backend-label');
  const clockEl = $('clock');
  const toolBadge = $('tool-badge');
  const toolName = $('tool-name');

  const micBtn = $('mic-btn');
  const micLabel = $('mic-label');
  const textForm = $('text-form');
  const textInput = $('text-input');
  const logoutBtn = $('logout-btn');
  const logEl = $('log');

  const camVideo = $('cam');
  const camCanvas = $('cam-canvas');
  const faceOverlay = $('face-overlay');
  const camSelect = $('cam-select');
  const camToggle = $('cam-toggle');
  const camStatus = $('cam-status');
  const observeToggle = $('observe-toggle');
  const observationCard = $('observation-card');
  const observationText = $('observation-text');
  const ttsAudio = $('tts-audio');

  // Mobile-only elements
  const fabMic = $('fab-mic');
  const mobileTextForm = $('mobile-text-form');
  const mobileTextInput = $('mobile-text-input');

  // ---------- 상태 ----------
  const TOKEN_KEY = 'jarvis.token';
  const USER_KEY = 'jarvis.user';
  let token = localStorage.getItem(TOKEN_KEY);
  let username = localStorage.getItem(USER_KEY);
  let ws = null;
  let mediaRecorder = null;
  let recordedChunks = [];
  let recording = false;
  let camStream = null;
  let frameInterval = null;
  let mainOrb = null;

  // ---------- 모바일 감지 ----------
  const isMobile = () => window.innerWidth <= 640;

  // ---------- 부팅 ----------
  init();

  async function init() {
    // 로그인 오브 (장식)
    new EmotionOrb(loginOrbCanvas, { particles: 30, alphaMult: 0.85 });

    // 회원가입/로그인 모드 결정
    try {
      const r = await fetch('/api/auth/status', { method: 'POST' });
      const j = await r.json();
      const isFirst = !j.has_users;
      loginSub.textContent = isFirst ? 'INITIAL SETUP — CREATE YOUR ACCOUNT' : 'AUTHENTICATION REQUIRED';
      loginBtn.textContent = isFirst ? 'INITIALIZE SYSTEM' : 'AUTHENTICATE';
      loginForm.dataset.mode = isFirst ? 'register' : 'login';
    } catch {}

    // 토큰이 있으면 곧장 메인 시도
    if (token && username) enterMain();

    setupClock();
    setupHotkeys();
    setupMobileTabs();
  }

  // ---------- 로그인 ----------
  loginForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    loginMsg.textContent = '';
    const u = usernameInput.value.trim();
    const p = passwordInput.value;
    if (!u || !p) return;

    const isRegister = loginForm.dataset.mode === 'register';
    const url = isRegister ? '/api/auth/register' : '/api/auth/login';
    const body = new FormData();
    body.append('username', u);
    body.append('password', p);

    try {
      const r = await fetch(url, { method: 'POST', body });
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        loginMsg.textContent = j.detail || `오류 (${r.status})`;
        passwordInput.value = '';
        return;
      }
      const j = await r.json();
      token = j.token;
      username = j.username;
      localStorage.setItem(TOKEN_KEY, token);
      localStorage.setItem(USER_KEY, username);
      enterMain();
    } catch (err) {
      loginMsg.textContent = '네트워크 오류: ' + err.message;
    }
  });

  document.addEventListener('keydown', (e) => {
    if (loginScreen.classList.contains('hidden')) return;
    if (e.key === 'Escape') {
      usernameInput.value = '';
      passwordInput.value = '';
    }
  });

  // ---------- 메인 진입 ----------
  function enterMain() {
    loginScreen.classList.add('hidden');
    mainScreen.classList.remove('hidden');
    userLabel.textContent = username;
    if (!mainOrb) mainOrb = new EmotionOrb(orbCanvas, { particles: 70 });
    connectWS();
    listCameras();
    // 기본 탭: orb
    switchTab('orb');
  }

  // ---------- WebSocket ----------
  function connectWS() {
    if (ws) try { ws.close(); } catch {}
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(token)}`);
    ws.binaryType = 'arraybuffer';

    ws.onmessage = (ev) => {
      if (typeof ev.data === 'string') {
        try { handleMessage(JSON.parse(ev.data)); } catch (e) { console.error(e); }
      } else {
        // 바이너리 = TTS MP3
        playTtsBytes(ev.data);
      }
    };

    ws.onclose = (ev) => {
      if (ev.code === 4001) {
        logout(true);
        return;
      }
      setState('disconnected');
      setTimeout(() => { if (token) connectWS(); }, 2000);
    };

    ws.onerror = () => setState('disconnected');
  }

  function send(obj) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify(obj));
  }

  function sendBinary(magic, payload) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const u8 = new Uint8Array(payload.byteLength + 1);
    u8[0] = magic;
    u8.set(new Uint8Array(payload), 1);
    ws.send(u8.buffer);
  }

  function handleMessage(m) {
    switch (m.type) {
      case 'ready':
        backendLabel.textContent = (m.backend || 'claude').toUpperCase();
        break;
      case 'state':
        setState(m.state);
        break;
      case 'emotion':
        setEmotion(m.emotion);
        break;
      case 'message':
        addLog(m.role, m.text);
        // 모바일에서 로그가 오면 side 탭으로 잠깐 배지 표시
        if (isMobile() && m.role === 'assistant') markTabBadge('side');
        break;
      case 'tool_event':
        if (m.status === 'start') {
          toolBadge.classList.remove('hidden');
          toolName.textContent = m.tool.toUpperCase();
        } else {
          toolBadge.classList.add('hidden');
        }
        break;
      case 'observation':
        observationCard.classList.remove('hidden');
        observationText.textContent = m.description;
        break;
      case 'faces':
        drawFaceBoxes(m.boxes || [], m.fw, m.fh);
        break;
      case 'stream_start':
        beginStreamBubble();
        break;
      case 'stream_chunk':
        appendStreamChunk(m.text || '');
        break;
      case 'stream_end':
        finalizeStreamBubble(m.text || '', m.emotion || 'neutral');
        if (isMobile()) markTabBadge('side');
        break;
      case 'observe_state':
        observeToggle.checked = m.on;
        break;
      case 'backend_changed':
        backendLabel.textContent = m.backend.toUpperCase();
        break;
      case 'reset_ack':
        clearLog();
        break;
      case 'timer_expired':
        flash(`⏰ 타이머: ${m.label}`);
        break;
      case 'error':
        flash(`⚠ ${m.message}`, 'error');
        break;
    }
  }

  function setState(state) {
    statePill.classList.remove('listening', 'thinking', 'speaking');
    if (state === 'listening' || state === 'thinking' || state === 'speaking') {
      statePill.classList.add(state);
    }
    statePill.textContent = `STATE · ${(state || 'idle').toUpperCase()}`;
    if (state === 'idle') {
      hintLabel.textContent = '버튼을 누르거나 SPACE 를 눌러 말하세요';
    } else if (state === 'listening') {
      hintLabel.textContent = '▸ Listening...';
    } else if (state === 'thinking') {
      hintLabel.textContent = '▸ Processing...';
    } else if (state === 'speaking') {
      hintLabel.textContent = '▸ Speaking...';
    } else if (state === 'disconnected') {
      hintLabel.textContent = '▸ 연결 끊김 — 재시도 중';
    }
  }

  function setEmotion(name) {
    if (!mainOrb) return;
    mainOrb.setEmotion(name);
    emotionLabel.textContent = (name || 'neutral').toUpperCase();
  }

  // ---------- 마이크 / 녹음 ----------
  micBtn.addEventListener('click', toggleRecording);
  if (fabMic) fabMic.addEventListener('click', toggleRecording);

  async function toggleRecording() {
    if (recording) {
      stopRecording();
    } else {
      await startRecording();
    }
  }

  async function startRecording() {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, sampleRate: 16000, echoCancellation: true, noiseSuppression: true },
      });
      const mime = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
        ? 'audio/webm;codecs=opus'
        : 'audio/webm';
      mediaRecorder = new MediaRecorder(stream, { mimeType: mime });
      recordedChunks = [];
      mediaRecorder.ondataavailable = (e) => {
        if (e.data.size > 0) recordedChunks.push(e.data);
      };
      mediaRecorder.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop());
        const blob = new Blob(recordedChunks, { type: mime });
        if (blob.size < 800) return;
        const buf = await blob.arrayBuffer();
        sendBinary(0x02, buf);
      };
      mediaRecorder.start();
      recording = true;
      micBtn.classList.add('recording');
      if (fabMic) fabMic.classList.add('recording');
      micLabel.textContent = 'STOP';
      setState('listening');
      setEmotion('listening');

      // 마이크 분석기 — VAD + 오브 진폭 공유
      const micCtx = new (window.AudioContext || window.webkitAudioContext)();
      const micSrc = micCtx.createMediaStreamSource(stream);
      const micAnalyser = micCtx.createAnalyser();
      micAnalyser.fftSize = 512;
      micSrc.connect(micAnalyser);

      // 오브에 마이크 진폭 전달
      const micBuf = new Uint8Array(micAnalyser.fftSize);
      const feedOrbMic = () => {
        if (!recording) { if (mainOrb) mainOrb.setAmplitude(0); return; }
        micAnalyser.getByteTimeDomainData(micBuf);
        let sum = 0;
        for (let i = 0; i < micBuf.length; i++) {
          const v = (micBuf[i] - 128) / 128;
          sum += v * v;
        }
        const rms = Math.sqrt(sum / micBuf.length);
        if (mainOrb) mainOrb.setAmplitude(rms * 3.5);
        requestAnimationFrame(feedOrbMic);
      };
      feedOrbMic();

      vadAutoStop(stream, micAnalyser, micCtx);
    } catch (err) {
      flash(`마이크 오류: ${err.message}`, 'error');
    }
  }

  function stopRecording() {
    if (!recording) return;
    recording = false;
    micBtn.classList.remove('recording');
    if (fabMic) fabMic.classList.remove('recording');
    micLabel.textContent = 'SPEAK';
    if (mediaRecorder && mediaRecorder.state !== 'inactive') {
      mediaRecorder.stop();
    }
  }

  function vadAutoStop(stream, sharedAnalyser, sharedCtx) {
    const analyser = sharedAnalyser;
    const audioCtx = sharedCtx;
    const data = new Uint8Array(analyser.fftSize);
    let speaking = false;
    let silenceStart = 0;
    const startedAt = performance.now();

    const tick = () => {
      if (!recording) {
        if (audioCtx) audioCtx.close().catch(() => {});
        return;
      }
      analyser.getByteTimeDomainData(data);
      let sum = 0;
      for (let i = 0; i < data.length; i++) {
        const v = (data[i] - 128) / 128;
        sum += v * v;
      }
      const rms = Math.sqrt(sum / data.length);
      const now = performance.now();
      if (rms > 0.04) {
        speaking = true;
        silenceStart = 0;
      } else if (speaking) {
        if (!silenceStart) silenceStart = now;
        if (now - silenceStart > 1500) {
          stopRecording();
          return;
        }
      }
      if (now - startedAt > 15000) {
        stopRecording();
        return;
      }
      requestAnimationFrame(tick);
    };
    tick();
  }

  // ---------- 텍스트 입력 (데스크톱) ----------
  textForm.addEventListener('submit', (e) => {
    e.preventDefault();
    const t = textInput.value.trim();
    if (!t) return;
    send({ type: 'text_input', text: t });
    textInput.value = '';
  });

  // ---------- 텍스트 입력 (모바일) ----------
  if (mobileTextForm) {
    mobileTextForm.addEventListener('submit', (e) => {
      e.preventDefault();
      const t = mobileTextInput.value.trim();
      if (!t) return;
      send({ type: 'text_input', text: t });
      mobileTextInput.value = '';
      mobileTextInput.blur();
    });
  }

  // ---------- 단축키 ----------
  function setupHotkeys() {
    document.addEventListener('keydown', (e) => {
      if (mainScreen.classList.contains('hidden')) return;
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
      if (e.code === 'Space') {
        e.preventDefault();
        toggleRecording();
      } else if (e.key === '1') {
        send({ type: 'switch_backend', backend: 'claude' });
      } else if (e.key === '2') {
        send({ type: 'switch_backend', backend: 'ollama' });
      } else if (e.key === 'r' || e.key === 'R') {
        send({ type: 'reset' });
      }
    });
    document.querySelectorAll('.quick-keys [data-key]').forEach((b) => {
      b.addEventListener('click', () => {
        const k = b.dataset.key;
        if (k === 'reset') send({ type: 'reset' });
        else send({ type: 'switch_backend', backend: k });
      });
    });
  }

  // ---------- 모바일 탭 ----------
  function setupMobileTabs() {
    document.querySelectorAll('.tab-btn').forEach((btn) => {
      btn.addEventListener('click', () => {
        const tab = btn.dataset.tab;
        switchTab(tab);
      });
    });
  }

  function switchTab(tab) {
    // 탭 버튼 활성 상태
    document.querySelectorAll('.tab-btn').forEach((b) => {
      b.classList.toggle('active', b.dataset.tab === tab);
    });

    // 패널 전환
    const orbPane = document.querySelector('.orb-pane');
    const sidePane = document.querySelector('.side-pane');

    if (tab === 'orb') {
      orbPane.classList.add('tab-active');
      sidePane.classList.remove('tab-active');
    } else {
      sidePane.classList.add('tab-active');
      orbPane.classList.remove('tab-active');
      // 로그 스크롤 최하단
      logEl.scrollTop = logEl.scrollHeight;
      // 배지 제거
      clearTabBadge('side');
    }
  }

  // 탭 배지 (새 메시지 알림)
  function markTabBadge(tabName) {
    const btn = document.querySelector(`.tab-btn[data-tab="${tabName}"]`);
    if (!btn || btn.classList.contains('active')) return;
    if (!btn.querySelector('.tab-badge')) {
      const dot = document.createElement('span');
      dot.className = 'tab-badge';
      dot.style.cssText = `
        position:absolute; top:6px; right:calc(50% - 14px);
        width:8px; height:8px; border-radius:50%;
        background:var(--accent); box-shadow:0 0 6px rgba(0,217,255,0.8);
      `;
      btn.style.position = 'relative';
      btn.appendChild(dot);
    }
  }

  function clearTabBadge(tabName) {
    const btn = document.querySelector(`.tab-btn[data-tab="${tabName}"]`);
    if (!btn) return;
    const dot = btn.querySelector('.tab-badge');
    if (dot) dot.remove();
  }

  // ---------- 카메라 ----------
  const camWrap = camVideo.closest('.cam-wrap');
  const camFlipBtn = $('cam-flip-btn');

  // 모바일 터치 기기 감지
  const isTouchDevice = () =>
    navigator.maxTouchPoints > 0 || 'ontouchstart' in window;

  // 현재 facing mode (front = 'user', back = 'environment')
  let facingMode = 'user';

  async function listCameras() {
    // 먼저 권한 요청 (권한 없으면 라벨이 빈값)
    try {
      const tmp = await navigator.mediaDevices.getUserMedia({ video: true });
      tmp.getTracks().forEach((t) => t.stop());
    } catch {
      camStatus.textContent = 'PERMISSION DENIED';
      return;
    }

    if (isTouchDevice()) {
      // 모바일: 전면/후면 옵션으로 표시
      camSelect.innerHTML = '';
      const opts = [
        { value: 'user',        label: '📷 전면 카메라 (Front)' },
        { value: 'environment', label: '📸 후면 카메라 (Back)' },
      ];
      opts.forEach(({ value, label }) => {
        const opt = document.createElement('option');
        opt.value = value;
        opt.textContent = label;
        camSelect.appendChild(opt);
      });
      camSelect.value = facingMode;
    } else {
      // 데스크톱: 실제 디바이스 목록
      const devices = await navigator.mediaDevices.enumerateDevices();
      const cams = devices.filter((d) => d.kind === 'videoinput');
      camSelect.innerHTML = '';
      cams.forEach((c, i) => {
        const opt = document.createElement('option');
        opt.value = c.deviceId;
        opt.textContent = c.label || `Camera ${i + 1}`;
        camSelect.appendChild(opt);
      });
      if (!cams.length) {
        const o = document.createElement('option');
        o.textContent = 'No camera';
        camSelect.appendChild(o);
      }
    }
  }

  camToggle.addEventListener('click', async () => {
    if (camStream) {
      stopCamera();
    } else {
      await startCamera();
    }
  });

  camSelect.addEventListener('change', async () => {
    if (camStream) {
      if (isTouchDevice()) {
        facingMode = camSelect.value;
      }
      stopCamera();
      await startCamera();
    }
  });

  // 플립 버튼 (모바일 전/후면 전환)
  if (camFlipBtn) {
    camFlipBtn.addEventListener('click', async () => {
      facingMode = facingMode === 'user' ? 'environment' : 'user';
      camSelect.value = facingMode;

      // 회전 애니메이션
      camFlipBtn.classList.add('spinning');
      setTimeout(() => camFlipBtn.classList.remove('spinning'), 400);

      if (camStream) {
        stopCamera(true); // silent — 관찰 유지
        await startCamera();
      }
    });
  }

  async function startCamera() {
    try {
      let constraints;
      if (isTouchDevice()) {
        // 모바일: facingMode 사용
        constraints = {
          video: {
            facingMode: { ideal: facingMode },
            width:  { ideal: 1280 },
            height: { ideal: 720 },
          },
        };
      } else {
        // 데스크톱: deviceId 사용
        const deviceId = camSelect.value;
        constraints = {
          video: deviceId ? { deviceId: { exact: deviceId } } : true,
        };
      }

      camStream = await navigator.mediaDevices.getUserMedia(constraints);
      camVideo.srcObject = camStream;
      await camVideo.play();
      camToggle.textContent = 'STOP';
      camStatus.textContent = 'LIVE';
      camStatus.classList.add('on');
      observeToggle.disabled = false;
      frameInterval = setInterval(sendFrame, 1000);

      // 후면 카메라는 미러 해제
      if (facingMode === 'environment') {
        camWrap.classList.add('rear-cam');
      } else {
        camWrap.classList.remove('rear-cam');
      }

      // 모바일에서 카메라 활성 시 플립 버튼 표시
      if (isTouchDevice() && camFlipBtn) {
        camFlipBtn.classList.remove('hidden');
      }
    } catch (err) {
      flash(`카메라 오류: ${err.message}`, 'error');
    }
  }

  function stopCamera(silent = false) {
    if (camStream) camStream.getTracks().forEach((t) => t.stop());
    camStream = null;
    camVideo.srcObject = null;
    if (frameInterval) clearInterval(frameInterval);
    frameInterval = null;
    camToggle.textContent = 'START';
    camStatus.textContent = 'OFF';
    camStatus.classList.remove('on');
    camWrap.classList.remove('rear-cam');
    if (camFlipBtn) camFlipBtn.classList.add('hidden');
    if (!silent) {
      observeToggle.disabled = true;
      if (observeToggle.checked) {
        observeToggle.checked = false;
        send({ type: 'observe', on: false });
      }
    }
  }

  function sendFrame() {
    if (!camStream || !camVideo.videoWidth) return;
    const w = camVideo.videoWidth;
    const h = camVideo.videoHeight;
    const scale = Math.min(1, 640 / w);
    camCanvas.width = Math.round(w * scale);
    camCanvas.height = Math.round(h * scale);
    const ctx = camCanvas.getContext('2d');
    ctx.drawImage(camVideo, 0, 0, camCanvas.width, camCanvas.height);
    camCanvas.toBlob(
      async (blob) => {
        if (!blob) return;
        const buf = await blob.arrayBuffer();
        sendBinary(0x01, buf);
      },
      'image/jpeg',
      0.7,
    );
  }

  // ---------- 행동 인식 토글 ----------
  observeToggle.addEventListener('change', () => {
    if (!camStream && observeToggle.checked) {
      observeToggle.checked = false;
      flash('먼저 카메라를 시작해주세요', 'error');
      return;
    }
    send({ type: 'observe', on: observeToggle.checked, interval: 6.0 });
    if (!observeToggle.checked) observationCard.classList.add('hidden');
  });

  // ---------- 스트리밍 버블 ----------
  let _streamEl = null; // 현재 스트리밍 중인 텍스트 요소

  function beginStreamBubble() {
    const div = document.createElement('div');
    div.className = 'log-msg assistant streaming';
    div.innerHTML = '<div class="who">▸ JARVIS</div><div class="text"></div>';
    _streamEl = div.querySelector('.text');
    logEl.appendChild(div);
    logEl.scrollTop = logEl.scrollHeight;
    while (logEl.children.length > 100) logEl.removeChild(logEl.firstChild);
  }

  function appendStreamChunk(text) {
    if (!_streamEl) return;
    _streamEl.textContent += text;
    logEl.scrollTop = logEl.scrollHeight;
  }

  function finalizeStreamBubble(cleanText, emotion) {
    if (_streamEl) {
      _streamEl.textContent = cleanText;
      const bubble = _streamEl.closest('.streaming');
      if (bubble) bubble.classList.remove('streaming');
      _streamEl = null;
    }
    logEl.scrollTop = logEl.scrollHeight;
  }

  // ---------- 로그 ----------
  let _typeQueue = Promise.resolve(); // 타이핑 직렬화 큐

  function addLog(role, text) {
    const div = document.createElement('div');
    div.className = `log-msg ${role}`;
    div.innerHTML = `<div class="who">▸ ${role === 'user' ? 'YOU' : 'JARVIS'}</div><div class="text"></div>`;
    const textEl = div.querySelector('.text');
    logEl.appendChild(div);
    logEl.scrollTop = logEl.scrollHeight;
    while (logEl.children.length > 100) logEl.removeChild(logEl.firstChild);

    if (role === 'assistant') {
      // 타이핑 효과 — 큐에 직렬화하여 중복 방지
      _typeQueue = _typeQueue.then(() => typeWriter(textEl, text));
    } else {
      textEl.textContent = text;
    }
  }

  function typeWriter(el, text) {
    return new Promise((resolve) => {
      let i = 0;
      const speed = Math.max(12, Math.min(40, Math.round(6000 / text.length))); // 길이에 따라 속도 조정
      const tick = () => {
        if (i < text.length) {
          el.textContent = text.slice(0, ++i);
          logEl.scrollTop = logEl.scrollHeight;
          setTimeout(tick, speed);
        } else {
          resolve();
        }
      };
      tick();
    });
  }

  function clearLog() {
    logEl.innerHTML = '';
    _typeQueue = Promise.resolve();
  }

  function flash(text, kind = 'info') {
    const div = document.createElement('div');
    div.className = `log-msg assistant`;
    div.innerHTML = `<div class="who" style="color:${kind === 'error' ? 'var(--red)' : 'var(--amber)'}">▸ SYSTEM</div><div class="text"></div>`;
    div.querySelector('.text').textContent = text;
    logEl.appendChild(div);
    logEl.scrollTop = logEl.scrollHeight;
  }

  // ---------- TTS + 진폭 시각화 ----------
  let _audioCtx = null;
  let _analyser = null;
  let _ampRaf = null;

  function _ensureAudioCtx() {
    if (_audioCtx) return;
    _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const src = _audioCtx.createMediaElementSource(ttsAudio);
    _analyser = _audioCtx.createAnalyser();
    _analyser.fftSize = 256;
    src.connect(_analyser);
    _analyser.connect(_audioCtx.destination);
  }

  function _startAmpLoop() {
    if (_ampRaf) return;
    const buf = new Uint8Array(_analyser.fftSize);
    const tick = () => {
      _analyser.getByteTimeDomainData(buf);
      let sum = 0;
      for (let i = 0; i < buf.length; i++) {
        const v = (buf[i] - 128) / 128;
        sum += v * v;
      }
      const rms = Math.sqrt(sum / buf.length);
      if (mainOrb) mainOrb.setAmplitude(rms * 4); // 0~1 범위로 스케일
      _ampRaf = requestAnimationFrame(tick);
    };
    tick();
  }

  function _stopAmpLoop() {
    if (_ampRaf) { cancelAnimationFrame(_ampRaf); _ampRaf = null; }
    if (mainOrb) mainOrb.setAmplitude(0);
  }

  function playTtsBytes(buf) {
    try { _ensureAudioCtx(); } catch {}
    const blob = new Blob([buf], { type: 'audio/mpeg' });
    const url = URL.createObjectURL(blob);
    ttsAudio.src = url;
    ttsAudio.play().then(() => {
      if (_analyser) _startAmpLoop();
    }).catch(() => {});
    ttsAudio.onended = () => {
      URL.revokeObjectURL(url);
      _stopAmpLoop();
    };
  }

  // ---------- 얼굴 박스 오버레이 ----------
  let _faceBoxTimeout = null;

  function drawFaceBoxes(boxes, fw, fh) {
    if (!faceOverlay) return;
    const dpr = window.devicePixelRatio || 1;
    const W = faceOverlay.clientWidth;
    const H = faceOverlay.clientHeight;
    faceOverlay.width = Math.round(W * dpr);
    faceOverlay.height = Math.round(H * dpr);
    const ctx = faceOverlay.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);

    if (!boxes.length || !fw || !fh) return;

    // 박스 좌표는 JPEG 픽셀값 → 화면 비율로 변환
    const scaleX = W / fw;
    const scaleY = H / fh;

    ctx.lineWidth = 2;
    ctx.font = '11px monospace';
    ctx.textBaseline = 'bottom';

    boxes.forEach((box, idx) => {
      const [top, right, bottom, left, name] = box;
      const x = left * scaleX;
      const y = top * scaleY;
      const bw = (right - left) * scaleX;
      const bh = (bottom - top) * scaleY;

      // 스캔라인 애니메이션용 그라데이션
      const grad = ctx.createLinearGradient(x, y, x, y + bh);
      grad.addColorStop(0, 'rgba(0,217,255,0.9)');
      grad.addColorStop(1, 'rgba(0,217,255,0.3)');
      ctx.strokeStyle = grad;
      ctx.strokeRect(x, y, bw, bh);

      // 코너 마커
      const cs = Math.min(bw, bh) * 0.18;
      ctx.strokeStyle = '#00d9ff';
      ctx.lineWidth = 2.5;
      [ [x, y, cs, 0, cs, 0],
        [x + bw, y, -cs, 0, -cs, 0],
        [x, y + bh, cs, 0, cs, 0],
        [x + bw, y + bh, -cs, 0, -cs, 0],
      ].forEach(([sx, sy, dx1, dy1, dx2, dy2]) => {
        ctx.beginPath();
        ctx.moveTo(sx + dx1, sy);
        ctx.lineTo(sx, sy);
        ctx.lineTo(sx, sy + (sy < y + bh / 2 ? cs : -cs));
        ctx.stroke();
      });

      // 이름 라벨
      const label = name || `FACE ${idx + 1}`;
      ctx.fillStyle = 'rgba(0,217,255,0.85)';
      ctx.fillRect(x, y - 16, ctx.measureText(label).width + 8, 16);
      ctx.fillStyle = '#03070c';
      ctx.fillText(label, x + 4, y);
    });

    // 3초 후 박스 자동 클리어
    if (_faceBoxTimeout) clearTimeout(_faceBoxTimeout);
    _faceBoxTimeout = setTimeout(() => {
      const c2 = faceOverlay.getContext('2d');
      c2.clearRect(0, 0, faceOverlay.width, faceOverlay.height);
    }, 3000);
  }

  // ---------- 시계 ----------
  function setupClock() {
    const tick = () => {
      const d = new Date();
      const z = (n) => String(n).padStart(2, '0');
      clockEl.textContent = `${z(d.getHours())}:${z(d.getMinutes())}:${z(d.getSeconds())}`;
    };
    tick();
    setInterval(tick, 1000);
  }

  // ---------- 로그아웃 ----------
  logoutBtn.addEventListener('click', () => logout(false));

  async function logout(silent) {
    const tk = token;
    token = null;
    username = null;
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
    if (ws) try { ws.close(); } catch {}
    stopCamera();
    if (!silent && tk) {
      const fd = new FormData();
      fd.append('token', tk);
      try { await fetch('/api/auth/logout', { method: 'POST', body: fd }); } catch {}
    }
    mainScreen.classList.add('hidden');
    loginScreen.classList.remove('hidden');
    passwordInput.value = '';
    clearLog();
  }
})();
