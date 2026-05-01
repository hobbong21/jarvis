/* ===== SARVIS Web Client ===== */
(() => {
  // ---------- DOM ----------
  const $ = (id) => document.getElementById(id);

  const mainScreen = $('main-screen');
  const emotionLabel = $('emotion-label');
  const hintLabel = $('hint-label');
  const statePill = $('state-pill');
  const backendLabel = $('backend-label');
  const clockEl = $('clock');
  const toolBadge = $('tool-badge');
  const toolName = $('tool-name');

  const micBtn = $('mic-btn');
  const micLabel = $('mic-label');
  const textForm = $('text-form');
  const textInput = $('text-input');
  const logEl = $('log');
  const orbCanvas = $('orb');
  const orbCanvas2 = $('orb-2');
  const orbPane = document.querySelector('.orb-pane');
  const emoLabel1 = $('emotion-label-1');
  const emoLabel2 = $('emotion-label-2');

  const camVideo = $('cam');
  const camCanvas = $('cam-canvas');
  const faceOverlay = $('face-overlay');
  const camSelect = $('cam-select');
  const camToggle = $('cam-toggle');
  const camStatus = $('cam-status');
  const observeToggle = $('observe-toggle');
  const observationCard = $('observation-card');
  const observationText = $('observation-text');
  const faceForm = $('face-form');
  const faceNameInput = $('face-name-input');
  const faceList = $('face-list');
  const faceCount = $('face-count');
  const faceMsg = $('face-msg');
  const ttsAudio = $('tts-audio');
  const continuousToggle = $('continuous-toggle');

  const mobileMicBtn = $('mobile-mic-btn');
  const mobileTextForm = $('mobile-text-form');
  const mobileTextInput = $('mobile-text-input');
  const orbReply = $('orb-reply');

  // ---------- 상태 ----------
  let ws = null;
  let mediaRecorder = null;
  let recordedChunks = [];
  let recording = false;
  let camStream = null;
  let frameInterval = null;
  let mainOrb = null;
  let secondOrb = null;
  let compareMode = false;

  // ---------- 연속 대화 모드 (기획서 v1.5) ----------
  // 음성 발화 → 응답 종료 → 자동으로 다음 발화 듣기로 전환.
  // 사용자가 텍스트로 입력한 turn 뒤에는 자동 시작하지 않음 (텍스트 선호자 보호).
  // 30 초 무발화 시 자동 OFF (안전장치).
  let continuousMode = (localStorage.getItem('sarvis-continuous') === '1');
  let _lastTurnWasVoice = false;          // 마지막 turn 이 음성 입력이었는지
  let _continuousAutoStartTimer = null;   // 응답 종료 후 자동 시작까지 짧은 지연 타이머
  let _continuousIdleTimer = null;        // 30 초 무발화 자동 OFF 타이머
  let _continuousFailCount = 0;           // 자동 시작 연속 실패 카운터 (마이크 권한 거부 등)
  const CONTINUOUS_AUTO_START_DELAY_MS = 600;
  const CONTINUOUS_IDLE_TIMEOUT_MS = 30_000;
  const CONTINUOUS_MAX_FAILS = 2;         // 연속 2회 실패 → 모드 자동 OFF (무한 재시도 방지)

  // ---------- 스트리밍 TTS (기획서 v1.5) ----------
  // 서버가 응답 음성을 head/tail 두 청크로 쪼개 보낼 수 있음. 마지막 청크의
  // ttsAudio.onended 에서만 maybeAutoStartListening 가 발동하도록 카운터 추적.
  let _remainingTtsChunks = 0;
  // (P0) Barge-in 후 서버에서 늦게 도착하는 tail 음성을 차단하는 latch.
  // 서버는 barge-in 사실을 모르므로 클라이언트에서 새 turn 시작 (state=thinking|speaking)
  // 까지 도착하는 모든 binary TTS 를 폐기.
  let _ignoreTtsBytesUntilNextTurn = false;

  // ---------- 모바일 감지 ----------
  const isMobile = () => window.innerWidth <= 640;

  // ---------- 부팅: 바로 메인 화면 ----------
  const validStyles = window.ORB_STYLES || ['orbital'];
  const stored = localStorage.getItem('orbStyle');
  const savedStyle = validStyles.includes(stored) ? stored : 'orbital';
  mainOrb = new EmotionOrb(orbCanvas, { particles: 70, style: savedStyle });
  if (orbCanvas2) {
    secondOrb = new EmotionOrb(orbCanvas2, { particles: 70, style: savedStyle });
  }
  setupOrbStylePicker(savedStyle);
  setupClock();
  setupHotkeys();
  setupMobileTabs();
  setupPanelToggles();
  setupContinuousToggle();
  connectWS();
  listCameras();
  switchTab('chat');

  // ---------- WebSocket ----------
  function connectWS() {
    if (ws) try { ws.close(); } catch {}
    // 재연결 시 환영 음성 관련 잔존 플래그/큐 초기화 (재연결 후 새 환영 오디오가
    // 이전 세션의 입력-의도 suppression 으로 부당하게 폐기되는 회귀 차단).
    // 첫 호출 시점엔 const 들이 아직 TDZ 상태이므로 try/catch 로 안전 보호.
    try {
      _expectingWelcomeAudio = false;
      _suppressNextWelcomeAudio = false;
      _pendingTtsQueue.length = 0;
    } catch (_e) { /* TDZ on initial connect — 깨끗한 상태이므로 무시 */ }
    // (연속 대화 모드 P1) 재연결 시 마지막 turn voice flag 리셋 — 재연결 후 환영 음성의
    // onended 가 이전 세션의 voice flag 로 자동 마이크를 켜는 사고 방지.
    _lastTurnWasVoice = false;
    cancelContinuousAutoStart();
    cancelContinuousIdleTimer();
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.binaryType = 'arraybuffer';

    ws.onmessage = (ev) => {
      if (typeof ev.data === 'string') {
        try { handleMessage(JSON.parse(ev.data)); } catch (e) { console.error(e); }
      } else {
        playTtsBytes(ev.data);
      }
    };

    ws.onclose = () => {
      setState('disconnected');
      setTimeout(() => connectWS(), 2000);
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

  // 백엔드 ID → 표시 라벨 (UI 버튼과 일치). 매핑 없는 ID 는 대문자 그대로.
  // (frontend-engineer 사이클 #6 P1)
  const BACKEND_LABEL = {
    claude: 'CLAUDE',
    openai: 'OPENAI',
    ollama: 'OLLAMA',
    zhipuai: 'GLM',
    compare: 'COMPARE',
  };
  function backendDisplay(id) {
    if (!id) return '';
    return BACKEND_LABEL[id] || String(id).toUpperCase();
  }

  function handleMessage(m) {
    switch (m.type) {
      case 'ready':
        if (m.backend) backendLabel.textContent = backendDisplay(m.backend);
        if (m.backend === 'compare') setCompareMode(true);
        renderFaces(m.faces || []);
        break;
      case 'face_list':
        renderFaces(m.faces || []);
        break;
      case 'face_register_result':
        showFaceMsg(m.message || (m.ok ? '등록됨' : '등록 실패'), !m.ok);
        if (m.ok) {
          renderFaces(m.faces || []);
          if (faceNameInput) faceNameInput.value = '';
        }
        break;
      case 'face_delete_result':
        renderFaces(m.faces || []);
        if (m.ok) showFaceMsg(`'${m.name}' 삭제됨`, false);
        break;
      case 'state':
        setState(m.state);
        // (P0) 새 turn 이 시작되면 barge-in latch 해제 + 청크 카운트 초기화.
        // 다음에 도착할 tts_chunk_count 가 정상적으로 채워질 수 있도록.
        if (m.state === 'thinking' || m.state === 'speaking') {
          _ignoreTtsBytesUntilNextTurn = false;
          _remainingTtsChunks = 0;
        }
        // 서버가 idle 로 돌아왔는데 TTS 가 차단/생략돼 onended 가 안 불릴 수도 있음 →
        // 연속 모드용 fallback 트리거 + 잔존 카운트 강제 reset (head blocked + tail audio
        // 같은 비대칭 상황에서 _remainingTtsChunks 가 영구 잔존하는 것을 방지).
        if (m.state === 'idle') {
          _remainingTtsChunks = 0;
          maybeAutoStartListening();
        }
        break;
      case 'tts_chunk_count':
        // (스트리밍 TTS 기획서 v1.5) 다음 응답이 N 개 청크로 쪼개져 옴.
        // ttsAudio.onended 에서 카운트가 0 될 때만 자동 마이크 트리거.
        _remainingTtsChunks = Math.max(0, parseInt(m.count, 10) || 0);
        break;
      case 'emotion':
        setEmotion(m.emotion);
        break;
      case 'message':
        addLog(m.role, m.text);
        if (isMobile() && m.role === 'assistant') markTabBadge('chat');
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
        if (isMobile()) markTabBadge('chat');
        // 다음 바이너리 프레임이 환영 인사 오디오임을 표시 (마이크/SEND 즉시 클릭 시 폐기 판단용)
        _expectingWelcomeAudio = !!m.is_welcome;
        break;
      case 'compare_start':
        beginCompareBubbles(m.sources || ['claude', 'openai']);
        setOrbEmotion('claude', 'thinking');
        setOrbEmotion('openai', 'thinking');
        setSubEmotion('claude', 'THINKING');
        setSubEmotion('openai', 'THINKING');
        break;
      case 'compare_chunk':
        appendCompareChunk(m.source, m.text || '');
        // 첫 청크부터 speaking 으로 전환
        setOrbEmotion(m.source, 'speaking');
        setSubEmotion(m.source, 'SPEAKING');
        break;
      case 'compare_end':
        finalizeCompareBubble(m.source, m.text || '', m.emotion || 'neutral');
        setOrbEmotion(m.source, m.emotion || 'neutral');
        setSubEmotion(m.source, (m.emotion || 'NEUTRAL').toUpperCase());
        break;
      case 'compare_done':
        if (isMobile()) markTabBadge('chat');
        break;
      case 'observe_state':
        observeToggle.checked = m.on;
        break;
      case 'backend_changed':
        backendLabel.textContent = backendDisplay(m.backend);
        setCompareMode(m.backend === 'compare');
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
    // 비교 모드에서는 글로벌 emotion 이벤트가 양쪽 오브를 동시에 흔들지 않도록
    // 무시 — 각 source 별로 compare_* 이벤트가 따로 옴.
    if (compareMode) return;
    mainOrb.setEmotion(name);
    emotionLabel.textContent = (name || 'neutral').toUpperCase();
  }

  // 비교 모드용: 특정 source 오브의 감정 설정
  function setOrbEmotion(source, name) {
    const orb = source === 'openai' ? secondOrb : mainOrb;
    if (orb) orb.setEmotion(name);
  }
  function setSubEmotion(source, label) {
    const el = source === 'openai' ? emoLabel2 : emoLabel1;
    if (el) el.textContent = label;
  }

  function setCompareMode(on) {
    compareMode = on;
    if (orbPane) orbPane.classList.toggle('compare-mode', on);
    const secondary = document.querySelector('.orb-unit.secondary');
    if (secondary) {
      if (on) secondary.removeAttribute('hidden');
      else secondary.setAttribute('hidden', '');
    }
    if (on) {
      // 진입 시 둘 다 neutral 로 초기화
      if (mainOrb) mainOrb.setEmotion('neutral');
      if (secondOrb) secondOrb.setEmotion('neutral');
      setSubEmotion('claude', 'NEUTRAL');
      setSubEmotion('openai', 'NEUTRAL');
    }
  }

  // ---------- 마이크 / 녹음 ----------
  micBtn.addEventListener('click', toggleRecording);
  if (mobileMicBtn) mobileMicBtn.addEventListener('click', toggleRecording);

  async function toggleRecording() {
    if (recording) {
      stopRecording();
    } else {
      // (Barge-in 기획서 v1.5) 사비스가 말하는 중에 사용자가 마이크/SPACE 로 끼어들면
      // TTS 즉시 멈추고 듣기 모드로 전환.
      interruptTts();
      await startRecording();
    }
  }

  // ---------- Barge-in (기획서 v1.5) ----------
  // 사비스 발화 중 사용자가 말하기 시작/SEND 누르면 TTS 즉시 정지 + 대기 큐 폐기.
  // 연속 대화 모드의 자동 시작 예약도 취소 (사용자가 직접 말함).
  function interruptTts() {
    let interrupted = false;
    try {
      if (ttsAudio && !ttsAudio.paused && !ttsAudio.ended) {
        ttsAudio.pause();
        try { ttsAudio.currentTime = 0; } catch {}
        interrupted = true;
      }
    } catch {}
    if (_pendingTtsQueue.length) {
      _pendingTtsQueue.length = 0;
      interrupted = true;
    }
    if (interrupted) {
      _stopAmpLoop();
      // 환영 음성이 잠겨 있을 가능성 (사용자 첫 액션이 끼어들기) 도 같이 정리.
      _suppressNextWelcomeAudio = true;
    }
    // (스트리밍 TTS) 남은 청크 카운트도 리셋 — 다음에 새 응답이 와야 정상 카운트.
    _remainingTtsChunks = 0;
    // (P0) 서버는 barge-in 을 즉시 알 수 없어 tail 합성·전송이 진행 중일 수 있다.
    // 다음 turn (state=thinking|speaking) 시작 전까지 도착하는 모든 binary TTS 를 폐기.
    _ignoreTtsBytesUntilNextTurn = true;
    // 응답 종료 후 자동 마이크 켜기 예약이 있으면 취소 (사용자가 직접 시작하므로 중복 방지)
    cancelContinuousAutoStart();
  }

  async function startRecording() {
    try {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        const e = new Error('INSECURE_CONTEXT');
        e.name = 'InsecureContextError';
        throw e;
      }
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
      markVoiceTurn(true);  // 음성으로 시작한 turn → 응답 후 자동 시작 자격
      micBtn.classList.add('recording');
      if (mobileMicBtn) mobileMicBtn.classList.add('recording');
      micLabel.textContent = 'STOP';
      setState('listening');
      setEmotion('listening');

      const micCtx = new (window.AudioContext || window.webkitAudioContext)();
      const micSrc = micCtx.createMediaStreamSource(stream);
      const micAnalyser = micCtx.createAnalyser();
      micAnalyser.fftSize = 512;
      micSrc.connect(micAnalyser);

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
      console.error('[mic]', err);
      flash(friendlyMediaError(err, '마이크'), 'error');
      recording = false;
      micBtn.classList.remove('recording');
      if (mobileMicBtn) mobileMicBtn.classList.remove('recording');
      micLabel.textContent = 'SPEAK';
      setState('idle');
      setEmotion('neutral');
    }
  }

  function stopRecording() {
    if (!recording) return;
    recording = false;
    // (연속 대화 모드 P1 수정) idle timer cancel 은 VAD 가 처음 음성을 감지한 시점에서 처리.
    // stopRecording 시점에서는 cancel 하지 않음 — 그렇게 하면 VAD 의 15s 강제 stop 으로
    // 30s 무발화 자동 OFF 가 실제로는 절대 발동 못 했었음.
    micBtn.classList.remove('recording');
    if (mobileMicBtn) mobileMicBtn.classList.remove('recording');
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
        if (!speaking) {
          // (연속 대화 모드 P1) 사용자가 실제로 발화 시작 — 30s 무발화 자동 OFF 타이머 취소.
          markUserSpoke();
        }
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
    interruptTts();                // (Barge-in) 사비스 발화 중 SEND → TTS 즉시 정지
    markVoiceTurn(false);          // 텍스트 turn → 응답 후 자동 마이크 X
    cancelContinuousAutoStart();   // 진행 중인 자동 시작 타이머 취소
    send({ type: 'text_input', text: t });
    textInput.value = '';
  });

  // ---------- 텍스트 입력 (모바일) ----------
  if (mobileTextForm) {
    mobileTextForm.addEventListener('submit', (e) => {
      e.preventDefault();
      const t = mobileTextInput.value.trim();
      if (!t) return;
      interruptTts();
      markVoiceTurn(false);
      cancelContinuousAutoStart();
      send({ type: 'text_input', text: t });
      mobileTextInput.value = '';
      mobileTextInput.blur();
    });
  }

  // ---------- 단축키 ----------
  function setupHotkeys() {
    document.addEventListener('keydown', (e) => {
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
      if (e.code === 'Space') {
        e.preventDefault();
        toggleRecording();
      } else if (e.key === '1') {
        send({ type: 'switch_backend', backend: 'claude' });
      } else if (e.key === '2') {
        send({ type: 'switch_backend', backend: 'openai' });
      } else if (e.key === '3') {
        send({ type: 'switch_backend', backend: 'ollama' });
      } else if (e.key === '4') {
        send({ type: 'switch_backend', backend: 'zhipuai' });
      } else if (e.key === '5') {
        send({ type: 'switch_backend', backend: 'compare' });
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

  // ---------- 오브 스타일 선택 ----------
  function setupOrbStylePicker(initialStyle) {
    const buttons = document.querySelectorAll('.orb-style-picker [data-orb-style]');
    if (!buttons.length) return;
    const apply = (name) => {
      buttons.forEach((b) => b.classList.toggle('active', b.dataset.orbStyle === name));
      if (mainOrb) mainOrb.setStyle(name);
      if (secondOrb) secondOrb.setStyle(name);
      localStorage.setItem('orbStyle', name);
    };
    apply(initialStyle);
    buttons.forEach((b) => {
      b.addEventListener('click', () => apply(b.dataset.orbStyle));
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
    document.querySelectorAll('.tab-btn').forEach((b) => {
      b.classList.toggle('active', b.dataset.tab === tab);
    });
    const orbPane = document.querySelector('.orb-pane');
    const sidePane = document.querySelector('.side-pane');
    const chatMain = document.querySelector('.chat-main');
    [orbPane, sidePane, chatMain].forEach((el) => el && el.classList.remove('tab-active'));
    if (tab === 'orb' && orbPane) {
      orbPane.classList.add('tab-active');
    } else if (tab === 'side' && sidePane) {
      sidePane.classList.add('tab-active');
    } else if (chatMain) {
      // 기본: chat
      chatMain.classList.add('tab-active');
      logEl.scrollTop = logEl.scrollHeight;
      clearTabBadge('chat');
    }
  }

  // ---------- 데스크톱 패널 토글 (오브 / 비전 / 모드) ----------
  function setupPanelToggles() {
    const layout = document.querySelector('.layout');
    const modePanel = document.getElementById('mode-panel');
    if (!layout) return;

    // 모바일은 별도 풀스크린 자비스 뷰를 쓰므로 데스크톱 패널 클래스 정리.
    const applyDesktopState = () => {
      if (isMobile()) {
        layout.classList.remove('show-orb', 'show-vision');
        return;
      }
      // 저장된 패널 상태 복원 (데스크톱에서만).
      // 첫 방문 시점에는 감정 오브가 보이도록 orb=true 를 기본값으로 적용.
      const saved = (() => {
        try {
          const raw = localStorage.getItem('panelState');
          if (!raw) return { orb: true, vision: false };  // 최초 방문 기본값
          return JSON.parse(raw) || { orb: true, vision: false };
        } catch { return { orb: true, vision: false }; }
      })();
      layout.classList.toggle('show-orb', !!saved.orb);
      layout.classList.toggle('show-vision', !!saved.vision);
      setPressed('toggle-orb', !!saved.orb);
      setPressed('toggle-vision', !!saved.vision);
    };
    applyDesktopState();
    window.addEventListener('resize', applyDesktopState);

    bindToggle('toggle-orb', () => {
      const on = layout.classList.toggle('show-orb');
      setPressed('toggle-orb', on);
      saveState();
    });
    bindToggle('toggle-vision', () => {
      const on = layout.classList.toggle('show-vision');
      setPressed('toggle-vision', on);
      saveState();
    });
    bindToggle('toggle-mode', () => {
      if (!modePanel) return;
      const willOpen = modePanel.hasAttribute('hidden');
      if (willOpen) { modePanel.removeAttribute('hidden'); }
      else { modePanel.setAttribute('hidden', ''); }
      setPressed('toggle-mode', willOpen);
    });

    // 모드 패널 외부 클릭 시 닫기
    document.addEventListener('click', (e) => {
      if (!modePanel || modePanel.hasAttribute('hidden')) return;
      const toggleBtn = document.getElementById('toggle-mode');
      if (modePanel.contains(e.target) || (toggleBtn && toggleBtn.contains(e.target))) return;
      // 모드 패널 안의 버튼 (백엔드 선택) 클릭은 quick-keys 핸들러가 따로 처리
      modePanel.setAttribute('hidden', '');
      setPressed('toggle-mode', false);
    });

    function bindToggle(id, fn) {
      const el = document.getElementById(id);
      if (el) el.addEventListener('click', fn);
    }
    function setPressed(id, on) {
      const el = document.getElementById(id);
      if (el) el.setAttribute('aria-pressed', on ? 'true' : 'false');
    }
    function saveState() {
      try {
        localStorage.setItem('panelState', JSON.stringify({
          orb: layout.classList.contains('show-orb'),
          vision: layout.classList.contains('show-vision'),
        }));
      } catch {}
    }
  }

  // ---------- 연속 대화 모드 (기획서 v1.5) ----------
  function setupContinuousToggle() {
    if (!continuousToggle) return;
    applyContinuousButtonState();
    continuousToggle.addEventListener('click', () => {
      setContinuousMode(!continuousMode, /*fromUser=*/true);
    });
  }

  function applyContinuousButtonState() {
    if (!continuousToggle) return;
    continuousToggle.setAttribute('aria-pressed', continuousMode ? 'true' : 'false');
    continuousToggle.title = continuousMode
      ? '연속 대화 모드 ON — 응답 후 자동으로 다음 발화를 듣습니다 (30초 무발화 시 자동 종료)'
      : '연속 대화 모드 OFF — 응답 후 마이크가 자동으로 켜지지 않습니다';
  }

  function setContinuousMode(on, fromUser) {
    continuousMode = !!on;
    try { localStorage.setItem('sarvis-continuous', continuousMode ? '1' : '0'); } catch {}
    applyContinuousButtonState();
    if (!continuousMode) {
      cancelContinuousAutoStart();
      cancelContinuousIdleTimer();
    }
    if (fromUser) {
      flash(continuousMode
        ? '연속 대화 ON — 응답 후 자동으로 다음 발화를 듣습니다'
        : '연속 대화 OFF', 'info');
    }
  }

  function cancelContinuousAutoStart() {
    if (_continuousAutoStartTimer) {
      clearTimeout(_continuousAutoStartTimer);
      _continuousAutoStartTimer = null;
    }
  }
  function cancelContinuousIdleTimer() {
    if (_continuousIdleTimer) {
      clearTimeout(_continuousIdleTimer);
      _continuousIdleTimer = null;
    }
  }

  // 응답 종료 시점 (TTS audio onended, 또는 state="idle" fallback) 에서 호출.
  // 음성으로 시작한 turn 뒤에만 자동 시작. 이미 녹음 중이거나 모드 OFF 면 noop.
  function maybeAutoStartListening() {
    if (!continuousMode) return;
    if (!_lastTurnWasVoice) return;
    if (recording) return;
    if (compareMode) return;  // compare 모드는 두 백엔드 응답 비교가 목적 — 자동 마이크 X
    cancelContinuousAutoStart();
    _continuousAutoStartTimer = setTimeout(async () => {
      _continuousAutoStartTimer = null;
      // 사이의 사용자 액션(text 전송 등)으로 무효화된 경우 스킵
      if (!continuousMode || !_lastTurnWasVoice || recording) return;
      try {
        await startRecording();
        // (P1 가드) startRecording 내부 catch 가 throw 하지 않으므로 recording 으로 성공 판정.
        // 권한 거부·InsecureContext 등 마이크 실패 시 idle timer 를 걸지 않고 실패 카운트.
        if (!recording) {
          _continuousFailCount += 1;
          if (_continuousFailCount >= CONTINUOUS_MAX_FAILS) {
            setContinuousMode(false, /*fromUser=*/false);
            flash('마이크 시작 실패가 반복되어 연속 대화 모드를 종료했습니다', 'error');
            _continuousFailCount = 0;
          }
          return;
        }
        _continuousFailCount = 0;  // 성공 — 카운터 리셋
        // 녹음 시작 후 30 초 안에 발화 시작이 없으면 모드 자동 OFF.
        // VAD 가 실제 음성을 감지하면 markUserSpoke() 가 idle timer 를 취소함.
        cancelContinuousIdleTimer();
        _continuousIdleTimer = setTimeout(() => {
          _continuousIdleTimer = null;
          if (!continuousMode) return;
          // 사용자가 한 마디도 안 해서 timer 가 발동 — 마이크 끄고 모드도 OFF
          if (recording) stopRecording();
          setContinuousMode(false, /*fromUser=*/false);
          flash('30초간 발화가 없어 연속 대화 모드를 종료했습니다', 'info');
        }, CONTINUOUS_IDLE_TIMEOUT_MS);
      } catch (e) {
        console.warn('[continuous] auto-start failed', e);
        _continuousFailCount += 1;
        if (_continuousFailCount >= CONTINUOUS_MAX_FAILS) {
          setContinuousMode(false, /*fromUser=*/false);
          flash('마이크 시작 실패가 반복되어 연속 대화 모드를 종료했습니다', 'error');
          _continuousFailCount = 0;
        }
      }
    }, CONTINUOUS_AUTO_START_DELAY_MS);
  }

  function markVoiceTurn(isVoice) {
    _lastTurnWasVoice = !!isVoice;
  }

  function markUserSpoke() {
    // 사용자가 실제로 발화를 시작했다 — 30s 무발화 idle 타이머는 더 이상 필요 없음.
    // VAD 의 첫 음성 감지 시점에서 호출됨 (stopRecording 에서 호출하지 않음 — P1 수정).
    cancelContinuousIdleTimer();
  }

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

  const isTouchDevice = () =>
    navigator.maxTouchPoints > 0 || 'ontouchstart' in window;

  let facingMode = 'user';

  async function listCameras() {
    try {
      const tmp = await navigator.mediaDevices.getUserMedia({ video: true });
      tmp.getTracks().forEach((t) => t.stop());
    } catch {
      camStatus.textContent = 'PERMISSION DENIED';
      return;
    }

    if (isTouchDevice()) {
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
      if (isTouchDevice()) facingMode = camSelect.value;
      stopCamera();
      await startCamera();
    }
  });

  if (camFlipBtn) {
    camFlipBtn.addEventListener('click', async () => {
      facingMode = facingMode === 'user' ? 'environment' : 'user';
      camSelect.value = facingMode;
      camFlipBtn.classList.add('spinning');
      setTimeout(() => camFlipBtn.classList.remove('spinning'), 400);
      if (camStream) {
        stopCamera(true);
        await startCamera();
      }
    });
  }

  async function startCamera() {
    try {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        const e = new Error('INSECURE_CONTEXT');
        e.name = 'InsecureContextError';
        throw e;
      }
      let constraints;
      if (isTouchDevice()) {
        constraints = {
          video: {
            facingMode: { ideal: facingMode },
            width:  { ideal: 1280 },
            height: { ideal: 720 },
          },
        };
      } else {
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
      if (facingMode === 'environment') {
        camWrap.classList.add('rear-cam');
      } else {
        camWrap.classList.remove('rear-cam');
      }
      if (isTouchDevice() && camFlipBtn) camFlipBtn.classList.remove('hidden');
    } catch (err) {
      console.error('[cam]', err);
      flash(friendlyMediaError(err, '카메라'), 'error');
      camStatus.textContent = 'PERMISSION DENIED';
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

  // ---------- 행동 인식 ----------
  observeToggle.addEventListener('change', () => {
    if (!camStream && observeToggle.checked) {
      observeToggle.checked = false;
      flash('먼저 카메라를 시작해주세요', 'error');
      return;
    }
    send({ type: 'observe', on: observeToggle.checked, interval: 6.0 });
    if (!observeToggle.checked) observationCard.classList.add('hidden');
  });

  // ---------- 얼굴 등록 / 식별 ----------
  let _faceMsgTimer = null;
  function showFaceMsg(text, isError) {
    if (!faceMsg) return;
    faceMsg.textContent = text || '';
    faceMsg.classList.toggle('error', !!isError);
    faceMsg.classList.add('show');
    clearTimeout(_faceMsgTimer);
    _faceMsgTimer = setTimeout(() => faceMsg.classList.remove('show'), 3500);
  }

  function renderFaces(names) {
    if (!faceList) return;
    faceList.innerHTML = '';
    if (faceCount) faceCount.textContent = String(names.length);
    names.forEach((name) => {
      const chip = document.createElement('span');
      chip.className = 'face-chip';
      const label = document.createElement('span');
      label.textContent = name;
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.title = `${name} 삭제`;
      btn.textContent = '×';
      btn.addEventListener('click', () => {
        if (confirm(`'${name}' 등록을 삭제할까요?`)) {
          send({ type: 'delete_face', name });
        }
      });
      chip.appendChild(label);
      chip.appendChild(btn);
      faceList.appendChild(chip);
    });
  }

  if (faceForm) {
    faceForm.addEventListener('submit', (e) => {
      e.preventDefault();
      const name = (faceNameInput?.value || '').trim();
      if (!name) {
        showFaceMsg('이름을 입력하세요', true);
        return;
      }
      if (!camStream) {
        showFaceMsg('먼저 카메라를 시작하세요', true);
        return;
      }
      showFaceMsg('등록 중...', false);
      send({ type: 'register_face', name });
    });
  }

  // ---------- 스트리밍 버블 ----------
  let _streamEl = null;
  let _orbStreamBuf = '';

  function beginStreamBubble() {
    const div = document.createElement('div');
    div.className = 'log-msg assistant streaming';
    div.innerHTML = '<div class="who">▸ SARVIS</div><div class="text"></div>';
    _streamEl = div.querySelector('.text');
    logEl.appendChild(div);
    logEl.scrollTop = logEl.scrollHeight;
    while (logEl.children.length > 100) logEl.removeChild(logEl.firstChild);
    _orbStreamBuf = '';
    if (orbReply) {
      orbReply.textContent = '';
      orbReply.classList.add('visible', 'streaming');
    }
  }

  function appendStreamChunk(text) {
    if (!_streamEl) return;
    _streamEl.textContent += text;
    logEl.scrollTop = logEl.scrollHeight;
    _orbStreamBuf += text;
    if (orbReply) {
      orbReply.textContent = _orbStreamBuf;
      orbReply.scrollTop = orbReply.scrollHeight;
    }
  }

  function finalizeStreamBubble(cleanText, emotion) {
    if (_streamEl) {
      _streamEl.textContent = cleanText;
      const bubble = _streamEl.closest('.streaming');
      if (bubble) bubble.classList.remove('streaming');
      _streamEl = null;
    }
    logEl.scrollTop = logEl.scrollHeight;
    if (orbReply) {
      orbReply.textContent = cleanText;
      orbReply.classList.add('visible');
      orbReply.classList.remove('streaming');
      orbReply.scrollTop = orbReply.scrollHeight;
    }
    _orbStreamBuf = '';
  }

  // ---------- A/B 비교 모드 ----------
  let _compareEls = {};

  function beginCompareBubbles(sources) {
    _compareEls = {};
    const wrap = document.createElement('div');
    wrap.className = 'log-msg compare-wrap';
    const labels = { claude: 'CLAUDE', openai: 'OPENAI' };
    sources.forEach((src) => {
      const col = document.createElement('div');
      col.className = `compare-col compare-${src} streaming`;
      col.innerHTML = `<div class="who">▸ ${labels[src] || src.toUpperCase()}</div>` +
                      `<div class="text"></div>`;
      wrap.appendChild(col);
      _compareEls[src] = col.querySelector('.text');
    });
    logEl.appendChild(wrap);
    logEl.scrollTop = logEl.scrollHeight;
    while (logEl.children.length > 100) logEl.removeChild(logEl.firstChild);
  }

  function appendCompareChunk(source, text) {
    const el = _compareEls[source];
    if (!el) return;
    el.textContent += text;
    logEl.scrollTop = logEl.scrollHeight;
  }

  function finalizeCompareBubble(source, cleanText, emotion) {
    const el = _compareEls[source];
    if (!el) return;
    el.textContent = cleanText;
    const col = el.closest('.compare-col');
    if (col) {
      col.classList.remove('streaming');
      col.dataset.emotion = emotion;
    }
    logEl.scrollTop = logEl.scrollHeight;
  }

  function updateOrbReply(text, streaming = false) {
    if (!orbReply) return;
    orbReply.textContent = text || '';
    orbReply.classList.toggle('visible', !!text);
    orbReply.classList.toggle('streaming', streaming);
  }

  function clearOrbReply() {
    if (!orbReply) return;
    orbReply.textContent = '';
    orbReply.classList.remove('visible', 'streaming');
  }

  // ---------- 로그 ----------
  let _typeQueue = Promise.resolve();

  function addLog(role, text) {
    const div = document.createElement('div');
    div.className = `log-msg ${role}`;
    div.innerHTML = `<div class="who">▸ ${role === 'user' ? 'YOU' : 'SARVIS'}</div><div class="text"></div>`;
    const textEl = div.querySelector('.text');
    logEl.appendChild(div);
    logEl.scrollTop = logEl.scrollHeight;
    while (logEl.children.length > 100) logEl.removeChild(logEl.firstChild);
    if (role === 'assistant') {
      _typeQueue = _typeQueue.then(() => typeWriter(textEl, text));
      updateOrbReply(text, false);
    } else {
      textEl.textContent = text;
    }
    markTabBadge('chat');
  }

  function typeWriter(el, text) {
    return new Promise((resolve) => {
      let i = 0;
      const speed = Math.max(12, Math.min(40, Math.round(6000 / text.length)));
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
    clearOrbReply();
  }

  function flash(text, kind = 'info') {
    const div = document.createElement('div');
    div.className = `log-msg assistant`;
    div.innerHTML = `<div class="who" style="color:${kind === 'error' ? 'var(--red)' : 'var(--amber)'}">▸ SYSTEM</div><div class="text"></div>`;
    div.querySelector('.text').textContent = text;
    if (kind === 'error' && /새 창|HTTPS|보안 컨텍스트/.test(text) && window.top !== window.self) {
      const btn = document.createElement('button');
      btn.className = 'inline-action-btn';
      btn.textContent = '↗ 새 창에서 열기';
      btn.addEventListener('click', () => {
        window.open(window.location.href, '_blank', 'noopener,noreferrer');
      });
      div.appendChild(btn);
    }
    logEl.appendChild(div);
    logEl.scrollTop = logEl.scrollHeight;
  }

  // 마이크 / 카메라 오류를 친절한 한국어 안내로 변환
  function friendlyMediaError(err, kind) {
    const inIframe = window.top !== window.self;
    const isSecure = window.isSecureContext;

    if (err && err.name === 'InsecureContextError') {
      return `${kind} 사용에는 보안 컨텍스트(HTTPS)가 필요합니다.\n` +
             `→ 주소창의 URL이 https:// 로 시작하는지 확인해주세요.`;
    }
    if (err && err.name === 'NotAllowedError') {
      const lines = [`${kind} 권한이 거부되었습니다.`];
      if (inIframe) {
        lines.push('→ Replit 미리보기 안에서는 권한이 차단되어 있습니다.');
        lines.push('→ 아래 [↗ 새 창에서 열기] 버튼을 누르거나, 미리보기 우측 상단 ⤢ 아이콘으로 새 탭에서 열어주세요.');
      } else {
        lines.push('→ 주소창 좌측 자물쇠/카메라 아이콘을 눌러 ' + kind + ' 권한을 "허용"으로 바꿔주세요.');
        lines.push('→ 권한 변경 후 페이지를 새로고침 해야 적용됩니다.');
      }
      return lines.join('\n');
    }
    if (err && err.name === 'NotFoundError') {
      return `${kind}를 찾을 수 없습니다.\n→ ${kind}가 컴퓨터에 연결되어 있는지 확인해주세요.`;
    }
    if (err && err.name === 'NotReadableError') {
      return `${kind}를 다른 앱이 사용 중입니다.\n→ Zoom, 카카오톡 영상통화, 다른 브라우저 탭 등에서 ${kind}를 사용 중인지 확인해주세요.`;
    }
    if (err && err.name === 'OverconstrainedError') {
      return `${kind} 설정 조건을 만족하는 장치가 없습니다.\n→ 다른 ${kind}를 선택하거나 기본 설정으로 다시 시도해주세요.`;
    }
    if (err && err.name === 'SecurityError') {
      return `${kind} 사용이 보안 정책으로 차단되었습니다.\n→ 새 창에서 직접 열어주세요 (HTTPS 직접 접근).`;
    }
    if (err && err.name === 'AbortError') {
      return `${kind} 시작이 중단되었습니다. 다시 시도해주세요.`;
    }
    if (!isSecure) {
      return `${kind} 사용에는 보안 컨텍스트(HTTPS)가 필요합니다.\n→ 주소창의 URL이 https:// 로 시작하는지 확인해주세요.`;
    }
    if (inIframe) {
      return `${kind}를 시작할 수 없습니다 (${(err && err.name) || '알 수 없는 오류'}).\n` +
             `→ Replit 미리보기 iframe에서는 종종 ${kind}가 차단됩니다. 새 창에서 열어주세요.`;
    }
    return `${kind} 오류: ${(err && (err.message || err.name)) || '알 수 없는 오류'}\n→ 페이지를 새로고침 후 다시 시도해주세요.`;
  }

  // ---------- TTS + 진폭 시각화 ----------
  let _audioCtx = null;
  let _analyser = null;
  let _ampRaf = null;
  let _audioUnlocked = false;          // 첫 사용자 제스처 후 true (브라우저 autoplay 정책)
  const _pendingTtsQueue = [];         // 잠금 해제 전에 도착한 TTS 오디오 버퍼 FIFO (최대 3)
  const PENDING_TTS_MAX = 3;
  let _expectingWelcomeAudio = false;  // 직전 stream_end 가 환영 인사인지 — 다음 바이트 분류 용
  let _suppressNextWelcomeAudio = false; // 사용자가 즉시 입력 시작 시 환영 음성 폐기

  function _ensureAudioCtx() {
    if (_audioCtx) return;
    _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const src = _audioCtx.createMediaElementSource(ttsAudio);
    _analyser = _audioCtx.createAnalyser();
    _analyser.fftSize = 256;
    src.connect(_analyser);
    _analyser.connect(_audioCtx.destination);
  }

  // 첫 사용자 제스처(아무 클릭/터치/키 입력) 시 오디오를 잠금 해제하고
  // 대기열에 있던 환영 TTS 가 있으면 그 시점에 재생한다. 단, 사용자가 마이크
  // 버튼을 눌러 곧바로 발화를 시작하려는 경우엔 환영 음성이 녹음/응답과
  // 겹치지 않도록 폐기한다.
  function _isInputIntentTarget(target) {
    if (!target || !target.closest) return false;
    return !!(
      target.closest('#mic-btn') ||
      target.closest('#text-input') ||
      target.closest('#text-form') ||
      target.closest('#send-btn') ||
      target.closest('#mobile-mic-btn') ||
      target.closest('#mobile-text-input') ||
      target.closest('#mobile-text-form')
    );
  }

  function _unlockAudioOnGesture(ev) {
    const target = ev && ev.target;
    const inputIntent = _isInputIntentTarget(target);

    // 입력 의도는 unlock 여부와 무관하게 환영 음성을 가로채지 않도록 항상 갱신.
    if (inputIntent) {
      _pendingTtsQueue.length = 0;
      _suppressNextWelcomeAudio = true;
    }
    if (_audioUnlocked) return;

    _audioUnlocked = true;
    try {
      _ensureAudioCtx();
      if (_audioCtx && _audioCtx.state === 'suspended') {
        _audioCtx.resume().catch(() => {});
      }
    } catch {}
    if (inputIntent) return;
    if (_pendingTtsQueue.length) {
      setTimeout(() => _flushPendingTts(), 80);
    }
  }
  ['pointerdown', 'keydown', 'touchstart'].forEach((evt) => {
    window.addEventListener(evt, _unlockAudioOnGesture, { capture: true, passive: true });
  });

  function _flushPendingTts() {
    // 대기열의 첫 항목만 즉시 재생 — 후속 항목은 onended 시 자연스럽게 이어가도록
    // playTtsBytes 가 onended 에서 다시 시도하지 않으므로 모두 직렬 재생.
    while (_pendingTtsQueue.length) {
      const buf = _pendingTtsQueue.shift();
      if (_pendingTtsQueue.length === 0) {
        playTtsBytes(buf, /*forceUnlocked=*/true);
      } else {
        // 다중 항목은 마지막만 재생 (가장 최신) — UX 단순화
        continue;
      }
    }
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
      if (mainOrb) mainOrb.setAmplitude(rms * 4);
      _ampRaf = requestAnimationFrame(tick);
    };
    tick();
  }

  function _stopAmpLoop() {
    if (_ampRaf) { cancelAnimationFrame(_ampRaf); _ampRaf = null; }
    if (mainOrb) mainOrb.setAmplitude(0);
  }

  function playTtsBytes(buf, forceUnlocked) {
    // (P0) Barge-in 후 같은 turn 의 tail 음성이 늦게 도착하면 폐기.
    // 카운트가 남아 있으면 같이 줄여 영구 잔존 방지.
    if (_ignoreTtsBytesUntilNextTurn) {
      if (_remainingTtsChunks > 0) _remainingTtsChunks -= 1;
      return;
    }
    // 환영 음성을 사용자가 의도적으로 폐기한 경우 (마이크 즉시 클릭 등).
    if (_expectingWelcomeAudio && _suppressNextWelcomeAudio) {
      _expectingWelcomeAudio = false;
      _suppressNextWelcomeAudio = false;
      return;
    }
    // 자동재생 잠금: 사용자가 아직 페이지와 상호작용하지 않은 상태에서
    // 도착한 환영 오디오는 큐에 보관하고, 첫 클릭/키입력 시점에 재생.
    // 일반 응답 오디오는 사용자가 이미 SEND/마이크를 눌렀으므로 잠금 해제 상태.
    if (!_audioUnlocked && !forceUnlocked) {
      _pendingTtsQueue.push(buf);
      while (_pendingTtsQueue.length > PENDING_TTS_MAX) _pendingTtsQueue.shift();
      _expectingWelcomeAudio = false;
      return;
    }
    _expectingWelcomeAudio = false;
    try { _ensureAudioCtx(); } catch {}
    const blob = new Blob([buf], { type: 'audio/mpeg' });
    const url = URL.createObjectURL(blob);
    let revoked = false;
    const revokeOnce = () => { if (!revoked) { revoked = true; URL.revokeObjectURL(url); } };
    ttsAudio.src = url;
    ttsAudio.play().then(() => {
      if (_analyser) _startAmpLoop();
    }).catch(() => {
      // play() 가 거부되면 (autoplay 차단) 큐로 되돌리고 잠금 표시.
      revokeOnce();
      _audioUnlocked = false;
      _pendingTtsQueue.push(buf);
      while (_pendingTtsQueue.length > PENDING_TTS_MAX) _pendingTtsQueue.shift();
    });
    ttsAudio.onended = () => {
      revokeOnce();
      _stopAmpLoop();
      // (스트리밍 TTS) 청크가 더 남아 있으면 자동 마이크 트리거 보류.
      // _remainingTtsChunks 가 0 (= count 메시지 안 옴 = 단일 합성) 이거나 마지막 청크일 때만 진행.
      if (_remainingTtsChunks > 0) {
        _remainingTtsChunks -= 1;
        if (_remainingTtsChunks > 0) return;
      }
      // 연속 대화 모드: 응답 TTS 가 끝나면 음성 turn 한정으로 자동으로 다음 발화 듣기.
      // (환영 인사는 _lastTurnWasVoice=false 이므로 자동 시작되지 않음 — 의도된 동작)
      maybeAutoStartListening();
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
      const grad = ctx.createLinearGradient(x, y, x, y + bh);
      grad.addColorStop(0, 'rgba(0,217,255,0.9)');
      grad.addColorStop(1, 'rgba(0,217,255,0.3)');
      ctx.strokeStyle = grad;
      ctx.strokeRect(x, y, bw, bh);
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
      const label = name || `FACE ${idx + 1}`;
      ctx.fillStyle = 'rgba(0,217,255,0.85)';
      ctx.fillRect(x, y - 16, ctx.measureText(label).width + 8, 16);
      ctx.fillStyle = '#03070c';
      ctx.fillText(label, x + 4, y);
    });
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
})();
