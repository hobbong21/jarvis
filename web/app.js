/* ===== SARVIS Web Client ===== */
(() => {
  // ---------- DOM ----------
  const $ = (id) => document.getElementById(id);

  const mainScreen = $('main-screen');
  const emotionLabel = $('emotion-label');
  const hintLabel = $('hint-label');
  const statePill = $('state-pill');
  const backendLabel = $('backend-label');
  const emotionMini = $('emotion-mini');
  const emotionMiniGlyph = emotionMini ? emotionMini.querySelector('.emo-glyph') : null;
  const emotionMiniText = emotionMini ? emotionMini.querySelector('.emo-text') : null;
  const memoryMini = $('memory-mini');
  const memoryMiniStatus = $('memory-mini-status');
  let memoryMiniTimer = null;
  // 사이클 #19 — 감정 → 한국어 레이블 + 글리프. UI 가 더 자연스럽게 표현.
  const EMOTION_GLYPHS = {
    neutral: '◔', happy: '✦', surprised: '✧', thinking: '◌',
    speaking: '◉', listening: '◍', sad: '◐', angry: '◈', error: '◇',
    concerned: '◍', alert: '◈',
  };
  const EMOTION_LABELS_KO = {
    neutral: '차분함', happy: '기쁨', surprised: '놀람', thinking: '생각 중',
    speaking: '말하는 중', listening: '듣는 중', sad: '안타까움', angry: '경계',
    error: '오류', concerned: '걱정', alert: '주의',
  };
  const koEmotionLabel = (k) => EMOTION_LABELS_KO[(k || 'neutral').toLowerCase()] || k || '차분함';
  function flashMemory(kind, label, ms) {
    if (!memoryMini) return;
    if (memoryMiniTimer) { clearTimeout(memoryMiniTimer); memoryMiniTimer = null; }
    memoryMini.classList.remove('recall', 'learned');
    if (kind) memoryMini.classList.add(kind);
    if (memoryMiniStatus && label) memoryMiniStatus.textContent = label;
    memoryMiniTimer = setTimeout(() => {
      if (!memoryMini) return;
      memoryMini.classList.remove('recall', 'learned');
      if (memoryMiniStatus) memoryMiniStatus.textContent = 'ON';
      memoryMiniTimer = null;
    }, ms || 2200);
  }
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
  if (Notification.permission === 'default') {
    Notification.requestPermission().catch(() => {});
  }
  const continuousToggle = $('continuous-toggle');

  const mobileMicBtn = $('mobile-mic-btn');
  const mobileTextForm = $('mobile-text-form');
  const mobileTextInput = $('mobile-text-input');
  const orbReply = $('orb-reply');
  // 채팅 헤더에 통합된 SARVIS 아바타 (감정 오브 + 상태 라벨)
  const chatOrbCanvas = $('chat-orb');
  const chatAvatarEl = document.querySelector('.chat-avatar');
  const chatAvatarStateEl = $('chat-avatar-state');
  let chatOrb = null;

  // ---------- 상태 ----------
  let ws = null;
  let mediaRecorder = null;
  let recordedChunks = [];
  let recording = false;
  let camStream = null;
  let frameInterval = null;
  let videoRecorder = null;
  let videoChunks = [];
  let videoRecording = false;
  let videoRecordingStart = 0;
  let videoRecordingLabel = '';
  let audioRecorder = null;
  let audioChunks = [];
  let audioRecording = false;
  let audioRecordingStart = 0;
  let audioRecordingLabel = '';
  let audioStream = null;
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
  // 채팅 아바타 — 감정 오브의 컴팩트 변형 (입자 수 감소, 동일 스타일 공유)
  if (chatOrbCanvas) {
    chatOrb = new EmotionOrb(chatOrbCanvas, { particles: 36, style: savedStyle });
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
    // 사이클 #21 — 생산성 패널이 추가 메시지 리스너를 부착할 수 있게 노출.
    window.__ws = ws;
    window.__sendWS = send;

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

    // 사이클 #7 — 연결 직후 모델 카탈로그 요청. WS open 이벤트는 setTimeout 으로
    // 메시지 핸들러가 바인딩된 후 안전 전송 (readyState 가드).
    ws.addEventListener('open', () => {
      try { send({ type: 'models_list' }); } catch (_e) {}
      try { send({ type: 'voices_list' }); } catch (_e) {}
    });
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
    gemini: 'GEMINI',
    compare: 'COMPARE',
  };
  function backendDisplay(id) {
    if (!id) return '';
    return BACKEND_LABEL[id] || String(id).toUpperCase();
  }

  // 사이클 #7 — 모델 카탈로그 캐시 + 드롭다운 갱신 헬퍼.
  let modelCatalog = {}; // {backend: {models: [...], current: '...'}}
  function currentBackend() {
    // 헤더 BRAIN 라벨에서 역추론 (대문자 라벨 → BACKEND_LABEL 역매핑).
    const lbl = (backendLabel.textContent || '').trim().toUpperCase();
    for (const [id, name] of Object.entries(BACKEND_LABEL)) {
      if (name === lbl) return id;
    }
    return lbl.toLowerCase();
  }
  function updateModelSelectFor(backend) {
    const sel = document.getElementById('model-select');
    const lbl = document.getElementById('model-backend-label');
    if (!sel) return;
    if (lbl) lbl.textContent = backendDisplay(backend);
    if (backend === 'compare') {
      sel.innerHTML = '<option value="">(compare 는 변경 불가)</option>';
      sel.disabled = true;
      return;
    }
    const entry = modelCatalog[backend];
    if (!entry || !entry.models || entry.models.length === 0) {
      sel.innerHTML = '<option value="">(모델 없음)</option>';
      sel.disabled = true;
      return;
    }
    sel.disabled = false;
    sel.innerHTML = entry.models.map((m) => {
      const selAttr = m === entry.current ? ' selected' : '';
      return `<option value="${m}"${selAttr}>${m}</option>`;
    }).join('');
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
      case 'gesture':
        // 사이클 #28 — 서버가 MediaPipe 로 감지한 손/포즈 제스처.
        // (a) 손 제스처 명령: thumbs_up=확인, open_palm=정지, fist=취소
        // (b) 손 들기로 호출: raised_hand → 마이크 자동 시작
        handleGestureEvent(m.name, Number(m.confidence) || 0);
        break;
      case 'message':
        addLog(m.role, m.text);
        if (isMobile() && m.role === 'assistant') markTabBadge('chat');
        break;
      case 'memory_event':
        if (m.kind === 'learned') {
          flashMemory('learned', '학습', 2800);
          const facts = (m.facts || []).map(f => `${f.key}=${f.value}`).join(', ');
          if (facts) addLog('system', `· 기억에 저장: ${facts}`);
        } else if (m.kind === 'recall') {
          flashMemory('recall', '회상', 1800);
        }
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
      case 'turn_logged':
        // 사이클 #22 (HARN-12) — 응답 메시지 옆 👍/👎 버튼 활성화.
        if (m.cmd_id) attachFeedbackButtons(m.cmd_id);
        break;
      case 'compare_start':
        beginCompareBubbles(m.sources || ['claude', 'openai']);
        setOrbEmotion('claude', 'thinking');
        setOrbEmotion('openai', 'thinking');
        setSubEmotion('claude', koEmotionLabel('thinking'));
        setSubEmotion('openai', koEmotionLabel('thinking'));
        break;
      case 'compare_chunk':
        appendCompareChunk(m.source, m.text || '');
        // 첫 청크부터 speaking 으로 전환
        setOrbEmotion(m.source, 'speaking');
        setSubEmotion(m.source, koEmotionLabel('speaking'));
        break;
      case 'compare_end':
        finalizeCompareBubble(m.source, m.text || '', m.emotion || 'neutral');
        setOrbEmotion(m.source, m.emotion || 'neutral');
        setSubEmotion(m.source, koEmotionLabel(m.emotion || 'neutral'));
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
        // 사이클 #7: 백엔드 변경 시 모델 드롭다운도 해당 백엔드의 카탈로그로 갱신.
        updateModelSelectFor(m.backend);
        break;
      case 'models_list':
        // 사이클 #7: WS 연결 직후 서버가 보낸 모델 카탈로그 캐시.
        modelCatalog = m.catalog || {};
        updateModelSelectFor(currentBackend());
        break;
      case 'model_changed':
        // 모델 변경 ack — 드롭다운에 currentModel 반영.
        if (modelCatalog[m.backend]) modelCatalog[m.backend].current = m.model;
        if (m.backend === currentBackend()) {
          const sel = document.getElementById('model-select');
          if (sel) sel.value = m.model;
        }
        flash(`✓ 모델 변경: ${m.model}`);
        break;
      case 'voices_list':
        // 음성 카탈로그 + 현재 프리셋 ID
        applyVoiceCatalog(m.catalog || [], m.current || 'default');
        break;
      case 'voice_changed':
        // 음성 변경 ack — 드롭다운에 반영 + 라벨 갱신
        applyVoiceChanged(m);
        flash(`✓ 음성 변경: ${m.label || m.preset}`);
        break;
      case 'voice_preview':
        // 미리듣기 — base64 → blob → audio 재생
        playVoicePreview(m);
        break;
      case 'reset_ack':
        clearLog();
        break;
      case 'timer_expired':
        flash(`⏰ 타이머: ${m.label}`);
        if (Notification.permission === 'granted') {
          new Notification('⏰ ' + (m.label || '타이머'), { body: '타이머가 만료되었습니다.' });
        }
        break;
      case 'sys_capture_photo':
        captureAndSendPhoto(m.label || '');
        break;
      case 'sys_open_url':
        if (m.url) { window.open(m.url, '_blank', 'noopener,noreferrer'); }
        flash(`🌐 ${m.url} 열기`);
        break;
      case 'sys_notification':
        flash(`🔔 ${m.title || '알림'}`);
        if (Notification.permission === 'granted') {
          new Notification(m.title || '사비스', { body: m.body || '' });
        } else if (Notification.permission !== 'denied') {
          Notification.requestPermission().then(p => {
            if (p === 'granted') new Notification(m.title || '사비스', { body: m.body || '' });
          });
        }
        break;
      case 'sys_set_volume':
        if (typeof m.level === 'number') {
          window.__sarvisVolume = m.level / 100;
          document.querySelectorAll('audio').forEach(a => { a.volume = window.__sarvisVolume; });
        }
        flash(`🔊 음량: ${m.level}%`);
        break;
      case 'sys_change_setting':
        if (m.setting === 'backend') {
          send({ type: 'switch_backend', backend: m.value });
        } else if (m.setting === 'model') {
          send({ type: 'switch_model', backend: '', model: m.value });
        } else if (m.setting === 'voice') {
          send({ type: 'switch_voice', preset: m.value });
        }
        flash(`⚙ 설정 변경: ${m.setting} → ${m.value}`);
        break;
      case 'error':
        flash(`⚠ ${m.message}`, 'error');
        break;
      // ── 사이클 #18: 주인 인증 ────────────────────────────
      case 'auth_status':
        applyAuthStatus(m);
        break;
      case 'auth_progress':
        applyAuthProgress(m);
        break;
      case 'auth_complete':
        applyAuthComplete(m);
        break;
      case 'auth_required':
        // 서버가 미인증 상태에서 명령을 거부 — 오버레이 다시 표시.
        if (authState && !authState.authed) showAuthOverlay();
        if (m.message) flash(`🔒 ${m.message}`, 'error');
        break;
      case 'auth_reset_ok':
        // 서버가 등록을 지웠음 → 등록 폼으로 전환.
        applyAuthStatus({ enrolled: false, face_ok: true, voice_ok: true,
                          authed: false, face_name: '' });
        flash('🔓 주인 등록이 초기화되었습니다.', 'ok');
        break;
      case 'enroll_owner_result':
        applyEnrollResult(m);
        break;
      case 'recording_cmd':
        handleRecordingCmd(m);
        break;
      case 'recording_saved':
        handleRecordingSaved(m);
        if (document.querySelector('.mypage-tab[data-tab="storage-tab"].active')) refreshStorage();
        break;
    }
  }

  // 채팅 아바타 상태 라벨 — AI 가 지금 무엇을 하고 있는지 친근한 한국어로.
  const CHAT_AVATAR_STATE_LABELS = {
    idle:         '듣고 있어요',
    listening:    '듣는 중…',
    thinking:     '생각하고 있어요…',
    speaking:     '말하고 있어요…',
    disconnected: '연결을 다시 시도하고 있어요…',
  };

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
    // 채팅 헤더 아바타에도 상태 반영 — CSS 의 data-state attribute 가 헤일로 애니
    // 메이션을 전환하고, orb 의 setState 가 호흡 리듬을 조정한다.
    const orbState = (state === 'listening' || state === 'thinking'
                      || state === 'speaking') ? state : 'idle';
    if (chatAvatarEl) chatAvatarEl.dataset.state = orbState;
    if (chatAvatarStateEl) {
      chatAvatarStateEl.textContent =
        CHAT_AVATAR_STATE_LABELS[state] || CHAT_AVATAR_STATE_LABELS.idle;
    }
    if (mainOrb && mainOrb.setState) mainOrb.setState(orbState);
    if (chatOrb && chatOrb.setState) chatOrb.setState(orbState);
  }

  function updateEmotionMini(name) {
    if (!emotionMini) return;
    const key = (name || 'neutral').toLowerCase();
    emotionMini.dataset.emotion = key;
    // 사이클 #19: 한국어 자연어 레이블 (UPPERCASE 영문 → 부드러운 한국어).
    if (emotionMiniText) emotionMiniText.textContent = EMOTION_LABELS_KO[key] || key;
    if (emotionMiniGlyph) emotionMiniGlyph.textContent = EMOTION_GLYPHS[key] || '◔';
  }

  function setEmotion(name) {
    if (!mainOrb) {
      updateEmotionMini(name);
      return;
    }
    // 비교 모드에서는 글로벌 emotion 이벤트가 양쪽 오브를 동시에 흔들지 않도록
    // 무시 — 각 source 별로 compare_* 이벤트가 따로 옴. mini 인디케이터도 비교 모드에선
    // 헤더가 좁아 표시 안 함 (orb-pane 의 sub-emotion 라벨이 대신함).
    if (compareMode) return;
    mainOrb.setEmotion(name);
    if (chatOrb) chatOrb.setEmotion(name);  // 채팅 아바타도 같은 감정으로 동기화
    const key = (name || 'neutral').toLowerCase();
    emotionLabel.textContent = EMOTION_LABELS_KO[key] || key;
    updateEmotionMini(name);
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
    // 디자인 통합 후 orb-pane 은 평소 숨김. 비교 모드일 때만 펼쳐서 두 번째 오브 표시.
    const layoutEl = document.querySelector('.layout');
    if (layoutEl) layoutEl.classList.toggle('show-orb', on && !isMobile());
    const secondary = document.querySelector('.orb-unit.secondary');
    if (secondary) {
      if (on) secondary.removeAttribute('hidden');
      else secondary.setAttribute('hidden', '');
    }
    // 비교 모드에선 좌측에 큰 오브 2개가 나오므로 채팅 헤더의 작은 아바타는 숨김
    // (3-orb 동시 노출의 시각적 잡음 회피).
    if (chatAvatarEl) {
      if (on) chatAvatarEl.setAttribute('hidden', '');
      else chatAvatarEl.removeAttribute('hidden');
    }
    if (on) {
      // 진입 시 둘 다 neutral 로 초기화
      if (mainOrb) mainOrb.setEmotion('neutral');
      if (secondOrb) secondOrb.setEmotion('neutral');
      setSubEmotion('claude', koEmotionLabel('neutral'));
      setSubEmotion('openai', koEmotionLabel('neutral'));
    }
  }

  // ---------- 사이클 #28 — 제스처 명령 핸들러 (MediaPipe Hands+Pose) ----------
  // 서버가 보낸 'gesture' 이벤트를 사용자 액션으로 매핑.
  //   raised_hand  → 마이크 자동 시작 (멀리서 호출)
  //   open_palm   → 사비스 발화 즉시 정지 (barge-in)
  //   thumbs_up   → "네" 텍스트 입력
  //   fist        → "아니요" 텍스트 입력
  //   peace       → 토스트만 (보조)
  // 모든 액션은 카메라가 켜져 있을 때만 발동 (관측 모드 ON 시).
  const GESTURE_LABEL = {
    raised_hand: '✋ 손 들기 감지 — 마이크를 켭니다',
    open_palm:   '🖐 정지',
    thumbs_up:   '👍 확인 (네)',
    fist:        '✊ 취소 (아니요)',
    peace:       '✌ 평화',
  };
  let _lastGestureActionTs = 0;
  // opt-in: 손 들기로 마이크 자동 시작 허용 여부 (localStorage 영속화).
  // 기본 ON. 사용자는 콘솔에서 sarvisGestureWake(false) 로 끌 수 있음.
  let _gestureWakeEnabled = (() => {
    try { return localStorage.getItem('sarvis_gesture_wake') !== '0'; }
    catch { return true; }
  })();
  window.sarvisGestureWake = function(on) {
    _gestureWakeEnabled = !!on;
    try { localStorage.setItem('sarvis_gesture_wake', on ? '1' : '0'); } catch {}
    flash(`제스처 호출 ${on ? '켜짐' : '꺼짐'}`, 'info');
    return _gestureWakeEnabled;
  };
  function handleGestureEvent(name, confidence) {
    const now = Date.now();
    // 클라이언트 추가 디바운스 — 서버 쿨다운(2s) 외에 빠른 연쇄 차단
    if (now - _lastGestureActionTs < 800) return;
    // 카메라가 꺼져 있으면 어떤 제스처도 무시 (안전 가드)
    if (!camStream) return;
    _lastGestureActionTs = now;
    const label = GESTURE_LABEL[name] || `제스처: ${name}`;
    try { flash(label, 'info'); } catch {}
    if (name === 'raised_hand') {
      // 명시적 안전 게이트: opt-in 토글 + 카메라 활성 + 미녹음 상태
      if (_gestureWakeEnabled && !recording) {
        interruptTts();
        toggleRecording();
      }
    } else if (name === 'open_palm') {
      interruptTts();
    } else if (name === 'thumbs_up') {
      interruptTts();
      markVoiceTurn(false);
      cancelContinuousAutoStart();
      send({ type: 'text_input', text: '네' });
    } else if (name === 'fist') {
      interruptTts();
      markVoiceTurn(false);
      cancelContinuousAutoStart();
      send({ type: 'text_input', text: '아니요' });
    }
  }

  // ---------- 사이클 #27 — Web Speech API (실시간 한국어 자막) ----------
  // Chrome/Edge/Safari 의 webkitSpeechRecognition 으로 즉시 문자 변환.
  // 미지원 브라우저(Firefox 등) 는 자동으로 기존 Whisper 흐름으로 폴백.
  const SR_Class = window.SpeechRecognition || window.webkitSpeechRecognition;
  let speechRec = null;                 // 현재 활성 SpeechRecognition 인스턴스
  let speechFinalSegments = [];         // isFinal 누적 텍스트
  let currentMicStream = null;          // 시각화/VAD 용 별도 stream (SR 와 병행)
  let _liveCaptionEl = null;
  let _liveCaptionTextEl = null;
  function _ensureCaptionRefs() {
    if (!_liveCaptionEl) _liveCaptionEl = document.getElementById('live-caption');
    if (_liveCaptionEl && !_liveCaptionTextEl) {
      _liveCaptionTextEl = _liveCaptionEl.querySelector('.live-caption-text');
    }
  }
  function showLiveCaption(finalTxt, interimTxt) {
    _ensureCaptionRefs();
    if (!_liveCaptionEl || !_liveCaptionTextEl) return;
    _liveCaptionEl.hidden = false;
    const f = (finalTxt || '').trim();
    const i = (interimTxt || '').trim();
    let html = '';
    if (f) html += escapeHtml(f);
    if (i) {
      if (f) html += ' ';
      html += '<span class="interim">' + escapeHtml(i) + '</span>';
    }
    if (!html) html = '<span class="interim">듣고 있어요…</span>';
    _liveCaptionTextEl.innerHTML = html;
  }
  function hideLiveCaption() {
    _ensureCaptionRefs();
    if (_liveCaptionEl) {
      _liveCaptionEl.hidden = true;
      if (_liveCaptionTextEl) _liveCaptionTextEl.innerHTML = '';
    }
  }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }
  function startWebSpeech() {
    if (!SR_Class) return false;
    try {
      speechRec = new SR_Class();
      speechRec.lang = 'ko-KR';
      speechRec.continuous = true;
      speechRec.interimResults = true;
      speechRec.maxAlternatives = 1;
      speechFinalSegments = [];
      showLiveCaption('', '');
      speechRec.onresult = (e) => {
        let interim = '';
        for (let i = e.resultIndex; i < e.results.length; i++) {
          const r = e.results[i];
          if (r.isFinal) {
            const t = (r[0] && r[0].transcript || '').trim();
            if (t) speechFinalSegments.push(t);
            // 사용자가 한 번 발화한 시점 = continuous idle timer 취소 대상
            try { markUserSpoke && markUserSpoke(); } catch {}
          } else {
            interim += (r[0] && r[0].transcript || '');
          }
        }
        showLiveCaption(speechFinalSegments.join(' '), interim);
      };
      speechRec.onerror = (e) => {
        const code = e && e.error;
        // no-speech / aborted 는 정상 종료의 일부 — 무시
        if (code === 'no-speech' || code === 'aborted' || code === 'audio-capture') return;
        console.warn('[speech] error:', code);
      };
      speechRec.onend = () => {
        // recording 이 아직 ON 이면 (continuous 가 끊긴 케이스) 자동 재시작
        if (recording && speechRec) {
          try { speechRec.start(); return; } catch {}
        }
        speechRec = null;
      };
      speechRec.start();
      return true;
    } catch (err) {
      console.warn('[speech] start failed, falling back:', err);
      speechRec = null;
      return false;
    }
  }
  function finalizeSpeechRecognition() {
    const rec = speechRec;
    speechRec = null;  // onend 의 자동 재시작 차단
    if (rec) {
      try { rec.onend = null; rec.onresult = null; rec.onerror = null; } catch {}
      try { rec.stop(); } catch {}
    }
    const finalText = speechFinalSegments.join(' ').trim();
    speechFinalSegments = [];
    hideLiveCaption();
    if (finalText) {
      send({ type: 'text_input', text: finalText });
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
      currentMicStream = stream;
      // 사이클 #27 — Web Speech API 우선 사용. 브라우저 내장이 한국어 정확도/지연이
      // Whisper-tiny 보다 압도적으로 좋고, interim 결과로 실시간 자막도 가능.
      // 미지원 브라우저(Firefox 등) 는 자동으로 기존 MediaRecorder→Whisper 흐름.
      const useWebSpeech = startWebSpeech();
      if (!useWebSpeech) {
        const mime = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
          ? 'audio/webm;codecs=opus'
          : 'audio/webm';
        mediaRecorder = new MediaRecorder(stream, { mimeType: mime });
        recordedChunks = [];
        mediaRecorder.ondataavailable = (e) => {
          if (e.data.size > 0) recordedChunks.push(e.data);
        };
        mediaRecorder.onstop = async () => {
          try { stream.getTracks().forEach((t) => t.stop()); } catch {}
          const blob = new Blob(recordedChunks, { type: mime });
          if (blob.size < 800) return;
          const buf = await blob.arrayBuffer();
          sendBinary(0x02, buf);
        };
        mediaRecorder.start();
      }
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
        if (!recording) {
          if (mainOrb) mainOrb.setAmplitude(0);
          if (chatOrb) chatOrb.setAmplitude(0);
          return;
        }
        micAnalyser.getByteTimeDomainData(micBuf);
        let sum = 0;
        for (let i = 0; i < micBuf.length; i++) {
          const v = (micBuf[i] - 128) / 128;
          sum += v * v;
        }
        const rms = Math.sqrt(sum / micBuf.length);
        if (mainOrb) mainOrb.setAmplitude(rms * 3.5);
        if (chatOrb) chatOrb.setAmplitude(rms * 3.5);
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
    // 사이클 #27 — Web Speech 경로: SR finalize + 시각화용 mic stream 정리.
    // 미지원 브라우저(Whisper 경로) 는 mediaRecorder.onstop 에서 stream 을 정리.
    if (speechRec) {
      finalizeSpeechRecognition();
      if (currentMicStream) {
        try { currentMicStream.getTracks().forEach((t) => t.stop()); } catch {}
        currentMicStream = null;
      }
    } else if (mediaRecorder && mediaRecorder.state !== 'inactive') {
      mediaRecorder.stop();
      currentMicStream = null;  // tracks 는 onstop 에서 정리
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
        send({ type: 'switch_backend', backend: 'gemini' });
      } else if (e.key === '6') {
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
    // 사이클 #7 — 모델 드롭다운 change 이벤트.
    const modelSel = document.getElementById('model-select');
    if (modelSel) {
      modelSel.addEventListener('change', () => {
        const backend = currentBackend();
        const model = modelSel.value;
        if (!model || backend === 'compare') return;
        send({ type: 'switch_model', backend, model });
      });
    }
    setupVoicePicker();
  }

  // ---------- 음성 선택 (voice picker) ----------
  let voiceCatalog = [];        // [{id, label, voice, rate, pitch, gender, description}, ...]
  let voiceCurrent = 'default';

  function setupVoicePicker() {
    const sel = document.getElementById('voice-select');
    const previewBtn = document.getElementById('voice-preview-btn');
    const applyBtn = document.getElementById('voice-apply-btn');
    const desc = document.getElementById('voice-description');
    if (!sel) return;

    sel.addEventListener('change', () => {
      const id = sel.value;
      const p = voiceCatalog.find((v) => v.id === id);
      if (desc && p) desc.textContent = p.description || '';
    });
    if (previewBtn) {
      previewBtn.addEventListener('click', () => {
        const id = sel.value;
        if (!id) return;
        previewBtn.disabled = true;
        previewBtn.textContent = '⏳ 합성 중...';
        send({ type: 'preview_voice', preset: id });
        // 12초 후 자동 복원 (서버 응답이 없거나 느린 네트워크 대비).
        // 정상 응답은 voice_preview 메시지에서 즉시 복원하므로 이 타이머는 안전망.
        setTimeout(() => {
          if (previewBtn.disabled) {
            previewBtn.disabled = false;
            previewBtn.textContent = '▶ 미리듣기';
          }
        }, 12000);
      });
    }
    if (applyBtn) {
      applyBtn.addEventListener('click', () => {
        const id = sel.value;
        if (!id || id === voiceCurrent) return;
        send({ type: 'switch_voice', preset: id });
      });
    }
  }

  function applyVoiceCatalog(catalog, current) {
    voiceCatalog = Array.isArray(catalog) ? catalog : [];
    voiceCurrent = current || 'default';
    const sel = document.getElementById('voice-select');
    const desc = document.getElementById('voice-description');
    const label = document.getElementById('voice-current-label');
    if (!sel) return;
    sel.innerHTML = '';
    if (!voiceCatalog.length) {
      const opt = document.createElement('option');
      opt.value = ''; opt.textContent = '(목록 없음)';
      sel.appendChild(opt);
      return;
    }
    voiceCatalog.forEach((p) => {
      const opt = document.createElement('option');
      opt.value = p.id;
      opt.textContent = p.label || p.id;
      sel.appendChild(opt);
    });
    sel.value = voiceCurrent;
    const cur = voiceCatalog.find((v) => v.id === voiceCurrent);
    if (label) label.textContent = cur ? cur.label : voiceCurrent;
    if (desc && cur) desc.textContent = cur.description || '';
  }

  function applyVoiceChanged(m) {
    voiceCurrent = m.preset || voiceCurrent;
    const sel = document.getElementById('voice-select');
    const label = document.getElementById('voice-current-label');
    const desc = document.getElementById('voice-description');
    if (sel) sel.value = voiceCurrent;
    const cur = voiceCatalog.find((v) => v.id === voiceCurrent);
    if (label) label.textContent = cur ? cur.label : (m.label || voiceCurrent);
    if (desc && cur) desc.textContent = cur.description || '';
  }

  function playVoicePreview(m) {
    const previewBtn = document.getElementById('voice-preview-btn');
    if (previewBtn) {
      previewBtn.disabled = false;
      previewBtn.textContent = '▶ 미리듣기';
    }
    const audioEl = document.getElementById('voice-preview-audio');
    if (!audioEl || !m.audio_b64) return;
    try {
      const bin = atob(m.audio_b64);
      const len = bin.length;
      const buf = new Uint8Array(len);
      for (let i = 0; i < len; i++) buf[i] = bin.charCodeAt(i);
      const blob = new Blob([buf], { type: m.mime || 'audio/mpeg' });
      const url = URL.createObjectURL(blob);
      audioEl.src = url;
      audioEl.play().catch(() => {});
      audioEl.onended = () => { try { URL.revokeObjectURL(url); } catch (_e) {} };
    } catch (e) {
      console.error('[voice preview] decode 실패:', e);
    }
  }

  // ---------- 오브 스타일 선택 ----------
  function setupOrbStylePicker(initialStyle) {
    const buttons = document.querySelectorAll('.orb-style-picker [data-orb-style]');
    // 초기 스타일을 chatOrb 에도 즉시 반영 (chatOrb 는 스타일 picker 가 끝난 뒤에
    // 만들어지지 않고 부팅 시 함께 만들어지지만, picker 클릭 시점에 chatOrb 가
    // 살아있는지 보장하기 위해 매번 적용한다).
    if (!buttons.length) return;
    const apply = (name) => {
      buttons.forEach((b) => b.classList.toggle('active', b.dataset.orbStyle === name));
      if (mainOrb) mainOrb.setStyle(name);
      if (secondOrb) secondOrb.setStyle(name);
      if (chatOrb) chatOrb.setStyle(name);  // 채팅 헤더 아바타도 동일 스타일 유지
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
    const sidePane = document.querySelector('.side-pane');
    const chatMain = document.querySelector('.chat-main');
    // 통합 후 'orb' 탭은 더 이상 없음 — 채팅 헤더에 항상 아바타 표시.
    [sidePane, chatMain].forEach((el) => el && el.classList.remove('tab-active'));
    if (tab === 'side' && sidePane) {
      sidePane.classList.add('tab-active');
    } else if (chatMain) {
      // 기본: chat (orb 탭이 chat 으로 폴백)
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
    // 디자인 통합 후: 오브가 채팅 헤더에 항상 표시되므로 'show-orb' 토글은 제거.
    // 비전 패널만 토글 가능 (기본 숨김).
    const applyDesktopState = () => {
      layout.classList.remove('show-orb');  // 통합 후 항상 false
      if (isMobile()) {
        layout.classList.remove('show-vision');
        return;
      }
      const saved = (() => {
        try {
          const raw = localStorage.getItem('panelState');
          if (!raw) return { vision: false };
          const obj = JSON.parse(raw) || {};
          return { vision: !!obj.vision };
        } catch { return { vision: false }; }
      })();
      layout.classList.toggle('show-vision', !!saved.vision);
      setPressed('toggle-vision', !!saved.vision);
    };
    applyDesktopState();
    window.addEventListener('resize', applyDesktopState);

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
        background:var(--accent); box-shadow:0 0 6px rgba(201,100,66,0.8);
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
    if (videoRecording) stopVideoRecording();
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
      // 사이클 #22: cmd_id 가 도착하면 피드백 버튼 부착 — 우선 placeholder 슬롯만.
      if (bubble && !bubble.querySelector('.fb-row')) {
        const fb = document.createElement('div');
        fb.className = 'fb-row';
        fb.dataset.pending = '1';
        bubble.appendChild(fb);
      }
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

  // 사이클 #22 (HARN-12) — 마지막 응답 버블에 피드백 버튼 부착.
  function attachFeedbackButtons(cmdId) {
    const pending = logEl.querySelectorAll('.fb-row[data-pending="1"]');
    const slot = pending[pending.length - 1];
    if (!slot) return;
    delete slot.dataset.pending;
    slot.dataset.cmdId = String(cmdId);
    slot.innerHTML =
      `<button class="fb-btn fb-up"   type="button" title="이 답변이 도움됐어요">👍</button>` +
      `<button class="fb-btn fb-down" type="button" title="이 답변은 아쉬워요">👎</button>` +
      `<span class="fb-status" aria-live="polite"></span>`;
    const status = slot.querySelector('.fb-status');
    function send(rating) {
      const w = window.__sendWS;
      if (!w) return;
      w({ type: 'feedback_submit', cmd_id: cmdId, rating });
      status.textContent = '저장 중…';
    }
    slot.querySelector('.fb-up').addEventListener('click', () => send(1));
    slot.querySelector('.fb-down').addEventListener('click', () => send(-1));
  }
  window.__attachFeedbackButtons = attachFeedbackButtons;

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
    const label = role === 'user' ? 'YOU'
                : role === 'system' ? 'MEMORY'
                : 'SARVIS';
    div.innerHTML = `<div class="who">▸ ${label}</div><div class="text"></div>`;
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
    if (typeof window.__sarvisVolume === 'number') ttsAudio.volume = window.__sarvisVolume;
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
      grad.addColorStop(0, 'rgba(201,100,66,0.9)');
      grad.addColorStop(1, 'rgba(201,100,66,0.3)');
      ctx.strokeStyle = grad;
      ctx.strokeRect(x, y, bw, bh);
      const cs = Math.min(bw, bh) * 0.18;
      ctx.strokeStyle = '#C96442';
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
      ctx.fillStyle = 'rgba(201,100,66,0.85)';
      ctx.fillRect(x, y - 16, ctx.measureText(label).width + 8, 16);
      ctx.fillStyle = '#FAF9F7';
      ctx.fillText(label, x + 4, y);
    });
    if (_faceBoxTimeout) clearTimeout(_faceBoxTimeout);
    _faceBoxTimeout = setTimeout(() => {
      const c2 = faceOverlay.getContext('2d');
      c2.clearRect(0, 0, faceOverlay.width, faceOverlay.height);
    }, 3000);
  }

  // ---------- 시계 ----------
  // ============================================================
  // 사이클 #18 — 주인 인증 오버레이 (등록/로그인)
  // ============================================================
  const authOverlay = $('auth-overlay');
  const authSubtitle = $('auth-subtitle');
  const authEnrollForm = $('auth-enroll-form');
  const authEnrollName = $('auth-enroll-name');
  const authEnrollPass = $('auth-enroll-passphrase');
  const authEnrollMsg = $('auth-enroll-msg');
  const authLoginPanel = $('auth-login-panel');
  const authLoginName = $('auth-login-name');
  const authLoginMsg = $('auth-login-msg');
  const authVoiceBtn = $('auth-voice-btn');
  const authResetBtn = $('auth-reset-btn');
  const authStepFace = $('auth-step-face');
  const authStepVoice = $('auth-step-voice');
  const authFaceStatus = $('auth-face-status');
  const authVoiceStatus = $('auth-voice-status');
  const authCamPreview = $('auth-cam-preview');
  // 사이클 #20 — 5각도 캡처 + 챌린지
  const authEnrollAngles = $('auth-enroll-angles');
  const authAnglePrompt = $('auth-angle-prompt');
  const authEnrollStartBtn = $('auth-enroll-start-btn');
  const authEnrollSubmitBtn = $('auth-enroll-submit-btn');
  const authChallengeBox = $('auth-challenge-box');
  const authChallengeText = $('auth-challenge-text');
  const authChallengeRefresh = $('auth-challenge-refresh');

  let authState = {
    enrolled: false, face_ok: true, voice_ok: true, blink_ok: true,
    blink_required: false, authed: true, challenge: null,
  };
  let authVoiceRecording = false;
  let authMediaRecorder = null;
  let authChunks = [];

  // 사이클 #20 — 5각도 캡처 시퀀스 상태
  const ANGLE_LABELS_KO = {
    front: '정면', left: '왼쪽', right: '오른쪽', up: '위', down: '아래',
  };
  const ANGLE_HINTS_KO = {
    front: '카메라를 똑바로 봐주세요',
    left: '얼굴을 살짝 왼쪽으로 돌려주세요',
    right: '얼굴을 살짝 오른쪽으로 돌려주세요',
    up: '얼굴을 살짝 위로 들어주세요',
    down: '얼굴을 살짝 아래로 숙여주세요',
  };
  let enrollSequence = null;   // {angles:[], frames:[], idx:0, timer}

  function captureCameraJpegBase64(quality = 0.85) {
    // camVideo 의 현재 프레임을 JPEG base64 로 추출. 카메라 미준비 시 null.
    if (!camStream || !camVideo || !camVideo.videoWidth) return null;
    const w = camVideo.videoWidth;
    const h = camVideo.videoHeight;
    // 인코딩 비용 절감 — 너무 크면 480 너비로 축소.
    const targetW = Math.min(w, 480);
    const targetH = Math.round(h * (targetW / w));
    const c = document.createElement('canvas');
    c.width = targetW; c.height = targetH;
    const ctx = c.getContext('2d');
    ctx.drawImage(camVideo, 0, 0, targetW, targetH);
    const dataUrl = c.toDataURL('image/jpeg', quality);
    const i = dataUrl.indexOf(',');
    return i >= 0 ? dataUrl.slice(i + 1) : null;
  }

  function setAngleStepClass(angle, klass) {
    const el = document.querySelector(`.auth-angle-step[data-angle="${angle}"]`);
    if (!el) return;
    el.classList.remove('current', 'done', 'failed');
    if (klass) el.classList.add(klass);
  }
  function resetAngleSteps() {
    document.querySelectorAll('.auth-angle-step').forEach((el) => {
      el.classList.remove('current', 'done', 'failed');
    });
  }

  function startEnrollCaptureSequence(angles) {
    // 각도별로 프롬프트 표시 → 1.4초 후 캡처 → 다음 각도. 끝나면 submit 버튼 활성.
    if (!camStream) {
      ensureAuthCamera();
      if (authEnrollMsg) {
        authEnrollMsg.textContent = '카메라를 시작하는 중입니다. 잠시 후 다시 시도해주세요.';
        authEnrollMsg.className = 'auth-msg error';
      }
      return;
    }
    enrollSequence = { angles: angles.slice(), frames: [], capturedAngles: [], idx: 0 };
    if (authEnrollAngles) authEnrollAngles.hidden = false;
    if (authEnrollStartBtn) authEnrollStartBtn.hidden = true;
    if (authEnrollSubmitBtn) authEnrollSubmitBtn.hidden = true;
    resetAngleSteps();
    advanceEnrollCapture();
  }

  function advanceEnrollCapture() {
    if (!enrollSequence) return;
    const { angles, idx } = enrollSequence;
    if (idx >= angles.length) {
      // 완료 — 등록 버튼 노출.
      if (authAnglePrompt) {
        authAnglePrompt.textContent = `✓ ${enrollSequence.frames.length}장 캡처 완료. 아래 [등록 완료] 버튼을 눌러주세요.`;
      }
      if (authEnrollSubmitBtn) authEnrollSubmitBtn.hidden = false;
      return;
    }
    const angle = angles[idx];
    setAngleStepClass(angle, 'current');
    if (authAnglePrompt) {
      authAnglePrompt.textContent =
        `(${idx + 1}/${angles.length}) ${ANGLE_LABELS_KO[angle] || angle} — ${ANGLE_HINTS_KO[angle] || ''}`;
    }
    setTimeout(() => {
      const b64 = captureCameraJpegBase64();
      if (b64) {
        enrollSequence.frames.push(b64);
        enrollSequence.capturedAngles.push(angle);
        setAngleStepClass(angle, 'done');
      } else {
        setAngleStepClass(angle, 'failed');
      }
      enrollSequence.idx += 1;
      // 다음 단계 살짝 텀.
      setTimeout(advanceEnrollCapture, 350);
    }, 1400);
  }

  function showAuthOverlay() {
    if (!authOverlay) return;
    authOverlay.hidden = false;
    authOverlay.setAttribute('aria-hidden', 'false');
  }
  function hideAuthOverlay() {
    if (!authOverlay) return;
    authOverlay.hidden = true;
    authOverlay.setAttribute('aria-hidden', 'true');
  }
  function setAuthStep(el, statusEl, state, label) {
    if (!el) return;
    el.classList.remove('ok', 'fail', 'pending');
    el.classList.add(state);
    if (statusEl && label != null) statusEl.textContent = label;
  }

  function ensureAuthCamera() {
    // 인증 흐름엔 카메라가 필수 — 자동 시작 + 오버레이 미리보기 연결.
    if (!camStream) {
      try { startCamera().catch(() => {}); } catch (_e) {}
    }
    setTimeout(() => {
      if (camStream && authCamPreview && authCamPreview.srcObject !== camStream) {
        authCamPreview.srcObject = camStream;
      }
    }, 250);
  }

  function applyAuthStatus(m) {
    authState.enrolled = !!m.enrolled;
    authState.face_ok = !!m.face_ok;
    authState.voice_ok = !!m.voice_ok;
    authState.blink_ok = ('blink_ok' in m) ? !!m.blink_ok : true;
    authState.blink_required = !!m.blink_required;
    authState.authed = !!m.authed;
    if ('challenge' in m) {
      authState.challenge = m.challenge || null;
      if (authChallengeText) authChallengeText.textContent = authState.challenge || '— (등록 후 발급)';
    }
    if (m.authed) { hideAuthOverlay(); return; }
    showAuthOverlay();
    ensureAuthCamera();
    if (m.enrolled) {
      if (authEnrollForm) authEnrollForm.hidden = true;
      if (authLoginPanel) authLoginPanel.hidden = false;
      if (authLoginName) authLoginName.textContent = m.face_name || '주인';
      if (authSubtitle) authSubtitle.textContent = '얼굴(라이브니스 포함)과 음성으로 본인을 확인합니다.';
    } else {
      if (authEnrollForm) authEnrollForm.hidden = false;
      if (authLoginPanel) authLoginPanel.hidden = true;
      if (authSubtitle) authSubtitle.textContent = '아직 주인이 등록되지 않았습니다. 본인을 등록해 주세요.';
    }
    setAuthStep(authStepFace, authFaceStatus, m.face_ok ? 'ok' : 'pending',
                m.face_ok ? '✓ 통과' : (authState.blink_required ? '얼굴 + 깜빡임 대기' : '대기 중'));
    setAuthStep(authStepVoice, authVoiceStatus, m.voice_ok ? 'ok' : 'pending',
                m.voice_ok ? '✓ 통과' : '대기 중');
    if (authVoiceBtn) authVoiceBtn.disabled = !m.face_ok || m.voice_ok;
  }

  function applyAuthProgress(m) {
    if ('face_ok' in m) authState.face_ok = !!m.face_ok;
    if ('voice_ok' in m) authState.voice_ok = !!m.voice_ok;
    if ('blink_ok' in m) authState.blink_ok = !!m.blink_ok;
    if ('challenge' in m && m.challenge) {
      authState.challenge = m.challenge;
      if (authChallengeText) authChallengeText.textContent = m.challenge;
    }
    let faceLabel;
    if (authState.face_ok) {
      faceLabel = m.degraded ? '✓ (간이)' : '✓ 통과';
    } else if (m.face_match_ok && !authState.blink_ok) {
      faceLabel = '얼굴 ✓ · 깜빡임 대기';
    } else {
      faceLabel = '대기 중';
    }
    setAuthStep(authStepFace, authFaceStatus, authState.face_ok ? 'ok' : 'pending', faceLabel);
    if ('voice_attempt_ok' in m) {
      if (m.voice_attempt_ok) {
        const tag = m.voice_matched_against === 'challenge' ? '챌린지 ✓' : '✓ 통과';
        setAuthStep(authStepVoice, authVoiceStatus, 'ok', tag);
      } else {
        const heard = m.voice_attempt_text ? `들린 말: "${m.voice_attempt_text}"` : '불일치';
        setAuthStep(authStepVoice, authVoiceStatus, 'fail', heard);
      }
    } else {
      setAuthStep(authStepVoice, authVoiceStatus, authState.voice_ok ? 'ok' : 'pending',
                  authState.voice_ok ? '✓ 통과' : '대기 중');
    }
    if (authLoginMsg) {
      authLoginMsg.textContent = m.message || '';
      authLoginMsg.classList.remove('error', 'ok');
      if (m.voice_attempt_ok === false) authLoginMsg.classList.add('error');
      else if (m.voice_attempt_ok === true || authState.face_ok) authLoginMsg.classList.add('ok');
    }
    if (authVoiceBtn) authVoiceBtn.disabled = !authState.face_ok || authState.voice_ok;
  }

  function applyAuthComplete(m) {
    authState.face_ok = true;
    authState.voice_ok = true;
    authState.authed = true;
    setAuthStep(authStepFace, authFaceStatus, 'ok', '✓ 통과');
    setAuthStep(authStepVoice, authVoiceStatus, 'ok', '✓ 통과');
    if (authLoginMsg) {
      authLoginMsg.textContent = `환영합니다, ${m.face_name || '주인'} 님.`;
      authLoginMsg.className = 'auth-msg ok';
    }
    setTimeout(() => {
      hideAuthOverlay();
      if (authCamPreview) authCamPreview.srcObject = null;
    }, 700);
  }

  function applyEnrollResult(m) {
    if (!authEnrollMsg) return;
    authEnrollMsg.textContent = m.message || '';
    authEnrollMsg.className = 'auth-msg ' + (m.ok ? 'ok' : 'error');
    if (m.ok) {
      // 시퀀스 정리 — UI 깔끔하게.
      enrollSequence = null;
      resetAngleSteps();
      if (authEnrollAngles) authEnrollAngles.hidden = true;
    } else if (Array.isArray(m.failed_angles) && m.failed_angles.length) {
      m.failed_angles.forEach((a) => setAngleStepClass(a, 'failed'));
    }
  }

  if (authEnrollStartBtn) {
    authEnrollStartBtn.addEventListener('click', () => {
      // 이름/패스프레이즈 사전 검증 → 5각도 캡처 시퀀스 시작.
      const name = (authEnrollName?.value || '').trim();
      const pass = (authEnrollPass?.value || '').trim();
      if (!name || pass.length < 4) {
        if (authEnrollMsg) {
          authEnrollMsg.textContent = '이름과 4자 이상의 패스프레이즈를 먼저 입력해주세요.';
          authEnrollMsg.className = 'auth-msg error';
        }
        return;
      }
      if (!camStream) {
        if (authEnrollMsg) {
          authEnrollMsg.textContent = '카메라를 먼저 시작해주세요 (권한 허용 필요).';
          authEnrollMsg.className = 'auth-msg error';
        }
        ensureAuthCamera();
        return;
      }
      if (authEnrollMsg) {
        authEnrollMsg.textContent = '5각도 얼굴 캡처를 시작합니다. 안내에 따라 얼굴을 돌려주세요.';
        authEnrollMsg.className = 'auth-msg';
      }
      startEnrollCaptureSequence(['front', 'left', 'right', 'up', 'down']);
    });
  }

  if (authEnrollForm) {
    authEnrollForm.addEventListener('submit', (e) => {
      e.preventDefault();
      const name = (authEnrollName?.value || '').trim();
      const pass = (authEnrollPass?.value || '').trim();
      if (!name || !pass) {
        authEnrollMsg.textContent = '이름과 패스프레이즈를 입력해주세요.';
        authEnrollMsg.className = 'auth-msg error';
        return;
      }
      // 5각도 시퀀스 결과 사용 — 없으면 구버전(서버 측 라이브 캡처 1장) 폴백.
      const frames = enrollSequence ? enrollSequence.frames : [];
      const angles = enrollSequence ? enrollSequence.capturedAngles : [];
      if (frames.length === 0) {
        authEnrollMsg.textContent = '얼굴 캡처가 완료되지 않았습니다. [5각도 얼굴 캡처 시작] 버튼부터 눌러주세요.';
        authEnrollMsg.className = 'auth-msg error';
        return;
      }
      authEnrollMsg.textContent = `${frames.length}장 인코딩 + 등록 중...`;
      authEnrollMsg.className = 'auth-msg';
      send({
        type: 'enroll_owner',
        face_name: name,
        voice_passphrase: pass,
        frames_b64: frames,
        angles: angles,
      });
    });
  }

  if (authChallengeRefresh) {
    authChallengeRefresh.addEventListener('click', () => {
      send({ type: 'auth_new_challenge' });
    });
  }

  if (authResetBtn) {
    authResetBtn.addEventListener('click', () => {
      if (!confirm('주인 등록을 초기화하고 처음부터 다시 등록하시겠습니까?')) return;
      enrollSequence = null;
      resetAngleSteps();
      if (authEnrollAngles) authEnrollAngles.hidden = true;
      if (authEnrollStartBtn) authEnrollStartBtn.hidden = false;
      if (authEnrollSubmitBtn) authEnrollSubmitBtn.hidden = true;
      send({ type: 'auth_reset' });
    });
  }

  if (authVoiceBtn) {
    authVoiceBtn.addEventListener('click', async () => {
      if (authVoiceRecording) {
        if (authMediaRecorder && authMediaRecorder.state !== 'inactive') {
          authMediaRecorder.stop();
        }
        return;
      }
      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: { channelCount: 1, sampleRate: 16000, echoCancellation: true, noiseSuppression: true },
        });
        const mime = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
          ? 'audio/webm;codecs=opus' : 'audio/webm';
        authMediaRecorder = new MediaRecorder(stream, { mimeType: mime });
        authChunks = [];
        authMediaRecorder.ondataavailable = (e) => {
          if (e.data.size > 0) authChunks.push(e.data);
        };
        authMediaRecorder.onstop = async () => {
          stream.getTracks().forEach((t) => t.stop());
          authVoiceRecording = false;
          authVoiceBtn.classList.remove('recording');
          authVoiceBtn.textContent = '🎙 음성 패스프레이즈 말하기';
          const blob = new Blob(authChunks, { type: mime });
          if (blob.size < 800) {
            if (authLoginMsg) {
              authLoginMsg.textContent = '음성이 너무 짧습니다. 다시 시도해주세요.';
              authLoginMsg.className = 'auth-msg error';
            }
            return;
          }
          const buf = await blob.arrayBuffer();
          sendBinary(0x02, buf);
          if (authLoginMsg) {
            authLoginMsg.textContent = '음성 인식 중...';
            authLoginMsg.className = 'auth-msg';
          }
        };
        authMediaRecorder.start();
        authVoiceRecording = true;
        authVoiceBtn.classList.add('recording');
        authVoiceBtn.textContent = '⏹ 녹음 중지 (말씀 끝나면)';
        // 8초 안전 자동 정지.
        setTimeout(() => {
          if (authMediaRecorder && authMediaRecorder.state !== 'inactive') {
            authMediaRecorder.stop();
          }
        }, 8000);
      } catch (err) {
        console.error('[auth voice]', err);
        if (authLoginMsg) {
          authLoginMsg.textContent = '마이크 접근 실패: ' + (err.message || err);
          authLoginMsg.className = 'auth-msg error';
        }
      }
    });
  }

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

// ─────────────────────────────────────────────────────────────
// 사이클 #21 — F-04 회의록 + F-10 할 일/캘린더 미니 패널.
// 기존 app.js 의 send() / WS 인스턴스 (ws) / 메시지 분기 패턴을 그대로 재사용.
// ─────────────────────────────────────────────────────────────
(function setupProductivityDock() {
  const $ = (id) => document.getElementById(id);
  const dock = $("productivity-dock");
  if (!dock) return;
  const fab = $("prod-toggle");
  const panels = $("prod-panels");
  fab.addEventListener("click", () => {
    const open = !panels.hidden;
    panels.hidden = open;
    fab.setAttribute("aria-expanded", String(!open));
    if (open === false) {
      // 열 때 todo 자동 새로고침.
      try { window.__sendWS && window.__sendWS({ type: "todo_list" }); } catch (_) {}
    }
  });

  // ── 송신 헬퍼 — 기존 send() 가 클로저 안이므로 ws 객체로 직접. ──
  function sendMsg(obj) {
    if (window.__sendWS) { window.__sendWS(obj); return; }
    // 전역 헬퍼가 없으면 window 의 ws 후보를 시도 (보수적 fallback).
    const w = window.ws || window.__ws;
    if (w && w.readyState === 1) { w.send(JSON.stringify(obj)); }
  }

  // ── 회의 패널 ────────────────────────────────────
  const mStatus = $("meeting-status");
  const mTitle = $("meeting-title");
  const mStartBtn = $("meeting-start-btn");
  const mEndBtn = $("meeting-end-btn");
  const mText = $("meeting-text");
  const mAddBtn = $("meeting-add-btn");
  const mTrans = $("meeting-transcript");
  const mSummary = $("meeting-summary");

  function setMeetingActive(active) {
    mStartBtn.disabled = !!active;
    mEndBtn.disabled = !active;
    mAddBtn.disabled = !active;
    mTitle.disabled = !!active;
    mStatus.textContent = active ? "진행 중" : "대기";
    mStatus.dataset.state = active ? "active" : "";
    // 사이클 #26 — 회의 시작 시 정리 영역만 펼침. dock 패널 자체는 자동 오픈하지 않는다.
    // (사용자가 📋 FAB 을 눌러야만 dock 카드가 표시된다 — 메인 화면 깨끗함 유지)
    if (active) {
      const area = $("meeting-active-area");
      if (area) area.hidden = false;
    }
  }
  setMeetingActive(false);
  // 페이지 첫 진입 시점엔 dock 패널/정리 영역 모두 닫힌 상태 보장.
  { const a = $("meeting-active-area"); if (a) a.hidden = true; }
  if (panels) {
    panels.hidden = true;
    if (fab) fab.setAttribute("aria-expanded", "false");
  }

  mStartBtn.addEventListener("click", () => {
    mTrans.innerHTML = "";
    mSummary.hidden = true;
    sendMsg({ type: "meeting_start", title: mTitle.value || "" });
  });
  mEndBtn.addEventListener("click", () => sendMsg({ type: "meeting_end" }));
  function addUtterance() {
    const t = mText.value.trim();
    if (!t) return;
    sendMsg({ type: "meeting_chunk", text: t, speaker: "Owner" });
    mText.value = "";
  }
  mAddBtn.addEventListener("click", addUtterance);
  mText.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); addUtterance(); }
  });

  function appendUtterance(ts, sp, txt) {
    const mm = String(Math.floor(ts / 60)).padStart(2, "0");
    const ss = String(Math.floor(ts % 60)).padStart(2, "0");
    const div = document.createElement("div");
    div.className = "ut";
    div.innerHTML = `<span class="ts">[${mm}:${ss}]</span>` +
                    `<span class="sp">${escapeHtml(sp)}:</span> ${escapeHtml(txt)}`;
    mTrans.appendChild(div);
    mTrans.scrollTop = mTrans.scrollHeight;
  }
  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  }

  function renderSummary(msg) {
    let html = "<h4>요약</h4><div>" + escapeHtml(msg.summary || "(없음)") + "</div>";
    if (Array.isArray(msg.decisions) && msg.decisions.length) {
      html += "<h4>결정사항</h4><ul>" +
              msg.decisions.map(d => "<li>" + escapeHtml(d) + "</li>").join("") + "</ul>";
    }
    if (Array.isArray(msg.action_items) && msg.action_items.length) {
      html += "<h4>액션 아이템</h4><ul>" +
              msg.action_items.map(it =>
                "<li><b>" + escapeHtml(it.owner || "—") + "</b>: " +
                escapeHtml(it.task || "") +
                (it.due ? ` <i>(${escapeHtml(it.due)})</i>` : "") + "</li>"
              ).join("") + "</ul>";
    }
    mSummary.innerHTML = html;
    mSummary.hidden = false;
  }

  // ── 할 일 패널 ───────────────────────────────────
  const tList = $("todo-list");
  const tTitle = $("todo-title");
  const tDue = $("todo-due");
  const tPrio = $("todo-priority");
  const tAddBtn = $("todo-add-btn");
  const tRefreshBtn = $("todo-refresh-btn");
  const tExtractText = $("todo-extract-text");
  const tExtractBtn = $("todo-extract-btn");

  function addTodo() {
    const title = tTitle.value.trim();
    if (!title) return;
    sendMsg({
      type: "todo_add", title, due: tDue.value.trim(),
      priority: tPrio.value, source: "manual",
    });
    tTitle.value = ""; tDue.value = "";
  }
  tAddBtn.addEventListener("click", addTodo);
  tTitle.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); addTodo(); }
  });
  tRefreshBtn.addEventListener("click", () => sendMsg({ type: "todo_list" }));
  tExtractBtn.addEventListener("click", () => {
    const t = tExtractText.value.trim();
    if (t.length < 4) return;
    sendMsg({ type: "todo_extract", text: t });
    tExtractText.value = "";
  });

  function renderTodos(active, done) {
    tList.innerHTML = "";
    const items = (active || []).concat((done || []).slice(0, 5));
    for (const it of items) {
      const li = document.createElement("li");
      li.className = "todo-item" + (it.done ? " done" : "");
      li.innerHTML =
        `<input type="checkbox" ${it.done ? "checked" : ""} data-id="${escapeHtml(it.id)}">` +
        `<span class="todo-title">${escapeHtml(it.title)}</span>` +
        (it.due ? `<span class="todo-due">${escapeHtml(it.due)}</span>` : "") +
        `<span class="todo-prio" data-p="${escapeHtml(it.priority)}">${escapeHtml(it.priority)}</span>` +
        `<button type="button" data-remove="${escapeHtml(it.id)}" title="삭제">✕</button>`;
      tList.appendChild(li);
    }
    tList.querySelectorAll('input[type="checkbox"]').forEach(cb => {
      cb.addEventListener("change", () =>
        sendMsg({ type: "todo_done", id: cb.dataset.id, done: cb.checked }));
    });
    tList.querySelectorAll('button[data-remove]').forEach(btn => {
      btn.addEventListener("click", () =>
        sendMsg({ type: "todo_remove", id: btn.dataset.remove }));
    });
  }

  // ── 사이클 #22 — My Sarvis (HARN-05 미니) ─────────────────
  const mySumEl = $("my-sarvis-summary");
  const mySumBtn = $("my-sarvis-refresh-btn");
  function escapeMs(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  }
  function renderMySarvis(s) {
    if (!mySumEl) return;
    const fb = s.feedback || {};
    const sat = (fb.satisfaction_pct == null) ? "—"
              : (Math.round(fb.satisfaction_pct) + "%");
    const kinds = (s.top_kinds || [])
      .map(k => `<span class="ms-pill">${escapeMs(k.kind)} · ${k.n}</span>`)
      .join("") || "<i>아직 데이터 없음</i>";
    const neg = (s.recent_negative || [])
      .map(r => `<li><b>👎</b> ${escapeMs((r.command_text || "").slice(0,40))}` +
                (r.comment ? ` — <i>${escapeMs(r.comment)}</i>` : "") + "</li>")
      .join("");
    mySumEl.innerHTML =
      `<div class="ms-row"><b>최근 ${s.window_days}일</b>` +
      ` · 명령 <b>${s.command_count}</b>` +
      ` · 오류 ${s.error_count}` +
      ` · 저장 ${s.storage_mb} MB</div>` +
      `<div class="ms-row">만족도 <b>${sat}</b>` +
      ` <small>(👍 ${fb.up||0} / 👎 ${fb.down||0} / 평가 ${fb.rated||0})</small></div>` +
      `<div class="ms-row">자주 쓴 종류: ${kinds}</div>` +
      (neg ? `<div class="ms-row">최근 아쉬웠던 답변<ul class="ms-neg">${neg}</ul></div>` : "");
  }
  function refreshMySarvis() {
    if (mySumEl) mySumEl.querySelector('.my-sarvis-loading') &&
      (mySumEl.querySelector('.my-sarvis-loading').textContent = '집계 중…');
    sendMsg({ type: "my_sarvis_summary", window_days: 7 });
  }
  if (mySumBtn) mySumBtn.addEventListener("click", refreshMySarvis);

  // 피드백 결과 처리 — 버튼 상태/카운터 갱신.
  function handleFeedbackResult(msg) {
    const slots = logEl.querySelectorAll('.fb-row[data-cmd-id]');
    slots.forEach(slot => {
      if (Number(slot.dataset.cmdId) !== Number(msg.cmd_id)) return;
      const status = slot.querySelector('.fb-status');
      if (!msg.ok) {
        if (status) status.textContent = "오류: " + (msg.message || "");
        return;
      }
      slot.querySelectorAll('.fb-btn').forEach(b => b.classList.remove('active'));
      const cls = msg.rating > 0 ? '.fb-up' : msg.rating < 0 ? '.fb-down' : null;
      if (cls) slot.querySelector(cls).classList.add('active');
      if (status) status.textContent = msg.rating > 0 ? '감사합니다 👍'
                                       : msg.rating < 0 ? '의견 반영하겠습니다 👎'
                                       : '취소됨';
    });
    // My Sarvis 자동 갱신.
    refreshMySarvis();
  }

  // ── 메인 app.js 의 WS 메시지 분기를 보강 (monkey-patch) ──
  // 기존 코드가 onmessage 리스너를 한 번에 register 하므로, document 전역 이벤트로 hooking.
  // app.js 가 발행하는 'sarvis:ws' CustomEvent 가 없어도 직접 ws 객체에 추가 리스너 부착.
  function attachWSListener() {
    const w = window.__ws || window.ws;
    if (!w || w.readyState === undefined) {
      setTimeout(attachWSListener, 500); return;
    }
    w.addEventListener("message", (ev) => {
      let msg; try { msg = JSON.parse(ev.data); } catch (_) { return; }
      if (!msg || !msg.type) return;
      switch (msg.type) {
        case "meeting_started":
          setMeetingActive(true);
          mStatus.textContent = "진행 중: " + (msg.title || "(무제)");
          mStatus.dataset.state = "active";
          break;
        case "meeting_chunk_added":
          appendUtterance(msg.ts || 0, msg.speaker || "Owner", msg.text || "");
          break;
        case "meeting_chunk_skipped":
          // 무시 — 잡음 필터.
          break;
        case "meeting_ended":
          setMeetingActive(false);
          mStatus.textContent = "종료됨";
          mStatus.dataset.state = "ended";
          renderSummary(msg);
          break;
        case "meeting_error":
          mStatus.textContent = "오류";
          mStatus.dataset.state = "";
          alert("[회의] " + (msg.message || "오류"));
          break;
        case "todo_list":
          renderTodos(msg.active || [], msg.done || []);
          break;
        case "todo_added":
        case "todo_done":
        case "todo_removed":
          sendMsg({ type: "todo_list" });
          break;
        case "todo_extract_result":
          if (msg.message) {
            const note = document.createElement("li");
            note.className = "todo-item";
            note.innerHTML = `<span class="todo-title" style="color:#93c5fd">${escapeHtml(msg.message)}</span>`;
            tList.prepend(note);
            setTimeout(() => note.remove(), 4000);
          }
          sendMsg({ type: "todo_list" });
          break;
        case "todo_error":
          alert("[할 일] " + (msg.message || "오류"));
          break;
        case "my_sarvis_summary":
          renderMySarvis(msg);
          break;
        case "feedback_result":
          handleFeedbackResult(msg);
          break;
        case "profile_data":
          fillProfileForm(msg); break;
        case "profile_saved":
          fillProfileForm(msg);
          if (pfSaveBtn) pfSaveBtn.disabled = false;
          if (pfMsg) {
            pfMsg.textContent = "저장 완료 ✓";
            setTimeout(() => { pfMsg.textContent = ""; }, 3000);
          }
          break;
        case "storage_list":
          renderStorageList(msg); break;
        case "storage_deleted":
          handleStorageDeleted(msg); break;
        case "error":
          if (pfSaveBtn) pfSaveBtn.disabled = false;
          if (pfMsg) pfMsg.textContent = "";
          break;
        case "ha_observer_result":
          haRenderObserver(msg); break;
        case "ha_issues_list":
          haRenderIssues(msg.issues || []); break;
        case "ha_growth_diary":
          haRenderDiary(msg); break;
        case "ha_kill_switch":
          if (msg.ok) haKillToggle.checked = !!msg.active;
          haStatus.textContent = msg.ok ?
            ("Kill Switch: " + (msg.active ? "활성" : "해제")) :
            ("Kill Switch 오류: " + (msg.message || ""));
          break;
        case "ha_optout":
          if (msg.ok) haOptoutToggle.checked = !!msg.opted_out;
          haStatus.textContent = msg.ok ?
            ("옵트아웃: " + (msg.opted_out ? "ON" : "OFF")) :
            ("옵트아웃 오류: " + (msg.message || ""));
          break;
        case "ha_blocked":
          haStatus.textContent = "차단됨: " + (msg.message || "Kill Switch 활성");
          break;
        case "ha_diagnostician_result":
          if (!msg.ok) { haStatus.textContent = "Diagnostician: " + (msg.message || "실패"); break; }
          haStatus.textContent = `Diagnostician 완료 — 진단 ${msg.count}건`;
          haRenderDiagnoses(msg.diagnoses || []);
          break;
        case "ha_diagnoses_for_issue":
          if (msg.ok) haRenderDiagnoses(msg.diagnoses || []);
          break;
        case "ha_strategist_result":
          haStatus.textContent = msg.ok ?
            `Strategist 완료 — 전략 ${msg.count}건` :
            `Strategist: ${msg.message || "실패"}`;
          break;
        case "ha_improver_result":
          haStatus.textContent = msg.ok ?
            `Improver 완료 — 제안 ${msg.count}건` :
            `Improver: ${msg.message || "실패"}`;
          if (msg.ok) sendMsg({ type: "ha_proposals_list", status: "pending", limit: 20 });
          break;
        case "ha_validator_result":
          haStatus.textContent = msg.ok ?
            `Validator 완료 — 검증 ${msg.count}건` :
            `Validator: ${msg.message || "실패"}`;
          if (msg.ok) sendMsg({ type: "ha_proposals_list", status: "pending", limit: 20 });
          break;
        case "ha_proposals_list":
          if (msg.ok) haRenderProposals(msg.proposals || []);
          else haStatus.textContent = "제안 큐: " + (msg.message || "실패");
          break;
        case "ha_proposal_decision":
          haStatus.textContent = msg.ok ?
            `제안 ${msg.proposal_id} → ${msg.decision} (적용 X)` :
            `결정 실패: ${msg.message || ""}`;
          if (msg.ok) sendMsg({ type: "ha_proposals_list", status: "pending", limit: 20 });
          break;
      }
    });
    // 초기 todo 목록 + My Sarvis + 프로필 로드.
    setTimeout(() => sendMsg({ type: "todo_list" }), 1000);
    setTimeout(refreshMySarvis, 1500);
    setTimeout(() => sendMsg({ type: "profile_get" }), 800);
  }

  // ── 마이페이지 탭 전환 ────────────────────────
  document.querySelectorAll('.mypage-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.mypage-tab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.mypage-tab-content').forEach(c => c.classList.remove('active'));
      tab.classList.add('active');
      const target = $(tab.dataset.tab);
      if (target) target.classList.add('active');
      if (tab.dataset.tab === 'storage-tab') refreshStorage();
    });
  });

  // ── 프로필 카드 ────────────────────────
  const pfNickname = $("profile-nickname");
  const pfEmail = $("profile-email");
  const pfTone = $("profile-tone");
  const pfInterests = $("profile-interests");
  const pfBio = $("profile-bio");
  const pfSaveBtn = $("profile-save-btn");
  const pfStatus = $("profile-status");
  const pfMsg = $("profile-save-msg");

  function fillProfileForm(p) {
    if (!pfNickname) return;
    pfNickname.value = p.nickname || "";
    pfEmail.value = p.email || "";
    pfTone.value = p.tone || "friendly";
    pfInterests.value = p.interests || "";
    pfBio.value = p.bio || "";
    pfStatus.textContent = p.updated_at ? "저장됨" : "미설정";
  }

  if (pfSaveBtn) {
    pfSaveBtn.addEventListener("click", () => {
      sendMsg({
        type: "profile_save",
        nickname: pfNickname.value.trim(),
        email: pfEmail.value.trim(),
        tone: pfTone.value,
        interests: pfInterests.value.trim(),
        bio: pfBio.value.trim(),
      });
      pfSaveBtn.disabled = true;
      pfMsg.textContent = "저장 중…";
    });
  }

  // ── 저장 공간 ────────────────────────
  const storageListEl = $("storage-list");
  const storageFilterEl = $("storage-filter");
  const storageRefreshBtn = $("storage-refresh-btn");
  const storageCountEl = $("storage-count");

  function refreshStorage() {
    const kind = storageFilterEl ? storageFilterEl.value : "";
    sendMsg({ type: "storage_list", kind, limit: 100 });
  }

  if (storageRefreshBtn) storageRefreshBtn.addEventListener("click", refreshStorage);
  if (storageFilterEl) storageFilterEl.addEventListener("change", refreshStorage);

  function storageKindIcon(kind) {
    if (kind === 'photo') return '📷';
    if (kind === 'audio') return '🎙️';
    return '🎬';
  }
  function storageKindLabel(kind) {
    if (kind === 'photo') return '사진';
    if (kind === 'audio') return '음성';
    return '영상';
  }
  function formatStorageDate(ts) {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    const mm = String(d.getMonth() + 1).padStart(2, '0');
    const dd = String(d.getDate()).padStart(2, '0');
    const hh = String(d.getHours()).padStart(2, '0');
    const mi = String(d.getMinutes()).padStart(2, '0');
    return `${mm}/${dd} ${hh}:${mi}`;
  }
  function formatFileSize(bytes) {
    if (!bytes) return '';
    if (bytes < 1024) return bytes + 'B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + 'KB';
    return (bytes / (1024 * 1024)).toFixed(1) + 'MB';
  }

  function escapeStorageText(s) {
    if (!s) return '';
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  function renderStorageList(msg) {
    if (!storageListEl) return;
    const items = msg.items || [];
    if (storageCountEl) storageCountEl.textContent = `${items.length}건`;
    storageListEl.innerHTML = '';
    if (!items.length) {
      const empty = document.createElement('div');
      empty.className = 'storage-empty';
      empty.textContent = '저장된 파일이 없습니다.';
      storageListEl.appendChild(empty);
      return;
    }
    items.forEach(item => {
      const row = document.createElement('div');
      row.className = 'storage-item';
      row.dataset.id = item.id;

      if (item.kind === 'photo') {
        const thumb = document.createElement('img');
        thumb.className = 'storage-thumb';
        thumb.src = '/api/recordings/' + item.id;
        thumb.alt = '';
        thumb.loading = 'lazy';
        row.appendChild(thumb);
      } else {
        const icon = document.createElement('span');
        icon.className = 'storage-icon';
        icon.textContent = storageKindIcon(item.kind);
        row.appendChild(icon);
      }

      const info = document.createElement('div');
      info.className = 'storage-info';
      const nameEl = document.createElement('div');
      nameEl.className = 'storage-name';
      nameEl.textContent = storageKindLabel(item.kind) + (item.label ? ' — ' + item.label : '');
      info.appendChild(nameEl);
      const metaEl = document.createElement('div');
      metaEl.className = 'storage-meta';
      const dur = (item.kind !== 'photo' && item.duration_ms)
        ? (item.duration_ms / 1000).toFixed(1) + '초 · ' : '';
      metaEl.textContent = formatStorageDate(item.created_at) + ' · ' + dur + formatFileSize(item.size_bytes);
      info.appendChild(metaEl);
      row.appendChild(info);

      const actions = document.createElement('div');
      actions.className = 'storage-actions';
      const openBtn = document.createElement('button');
      openBtn.className = 'storage-open';
      openBtn.textContent = '열기';
      openBtn.title = '열기/다운로드';
      openBtn.addEventListener('click', () => window.open('/api/recordings/' + item.id, '_blank'));
      actions.appendChild(openBtn);
      const delBtn = document.createElement('button');
      delBtn.className = 'storage-del';
      delBtn.textContent = '삭제';
      delBtn.title = '삭제';
      delBtn.addEventListener('click', () => {
        if (confirm('이 파일을 삭제하시겠습니까?')) sendMsg({ type: "storage_delete", id: item.id });
      });
      actions.appendChild(delBtn);
      row.appendChild(actions);

      storageListEl.appendChild(row);
    });
  }

  function handleStorageDeleted(msg) {
    if (msg.ok && msg.id) {
      const el = storageListEl ? storageListEl.querySelector(`[data-id="${msg.id}"]`) : null;
      if (el) {
        el.style.opacity = '0';
        el.style.transform = 'translateX(20px)';
        el.style.transition = 'all 0.25s';
        setTimeout(() => { el.remove(); updateStorageCount(); }, 260);
      }
      flash('파일이 삭제되었습니다.', 'ok');
    } else {
      flash(msg.message || '삭제 실패', 'error');
    }
  }

  function updateStorageCount() {
    if (!storageListEl || !storageCountEl) return;
    const count = storageListEl.querySelectorAll('.storage-item').length;
    storageCountEl.textContent = `${count}건`;
    if (count === 0) {
      storageListEl.innerHTML = '<div class="storage-empty">저장된 파일이 없습니다.</div>';
    }
  }

  // ── 사이클 #23 — HA 성장 일기 카드 ────────────────────────
  const haObsBtn = $("ha-observer-btn");
  const haRefreshBtn = $("ha-refresh-btn");
  const haWindowSel = $("ha-window");
  const haOptoutToggle = $("ha-optout-toggle");
  const haKillToggle = $("ha-kill-toggle");
  const haStatus = $("ha-status");
  const haIssuesEl = $("ha-issues");

  function haSeverityIcon(sev) {
    return ({critical:"🚨", high:"⚠️", medium:"🔶", low:"🔷", info:"ℹ️"})[sev] || "•";
  }
  function haRenderObserver(msg) {
    if (!msg.ok) { haStatus.textContent = "Observer: " + (msg.message || "실패"); return; }
    haStatus.textContent = `Observer 완료 — 윈도우 ${msg.window_days}d, 이슈 ${msg.issue_count}건`;
    haRenderIssues(msg.issues || []);
  }
  function haRenderIssues(issues) {
    if (!haIssuesEl) return;
    if (!issues.length) {
      haIssuesEl.innerHTML = "<div class='ms-row'><i>이슈 없음</i></div>";
      return;
    }
    haIssuesEl.innerHTML = issues.slice(0, 20).map(i => {
      const sev = i.severity || "info";
      const cat = i.category || "?";
      const narr = escapeMs(i.narrative || i.narrative_summary || "");
      const sig = escapeMs(i.signal || i.statistical_signal || "");
      const conf = (i.confidence != null) ? (Number(i.confidence)*100).toFixed(0)+"%" : "—";
      return `<div class="ms-row">${haSeverityIcon(sev)} <b>[${escapeMs(cat)}]</b> ${narr}` +
             `<br><small>${sig} · 신뢰도 ${conf}</small></div>`;
    }).join("");
  }
  function haRenderDiary(d) {
    if (!d.ok && d.message) { haStatus.textContent = d.message; return; }
    haStatus.textContent = `${d.stage || "S2"} · ${d.autonomy_level || "L1"} · ` +
      `활성 ${(d.active_agents||[]).join(", ")}`;
    haRenderIssues(d.issues || []);
    if ((d.diagnoses || []).length) haRenderDiagnoses(d.diagnoses);
  }
  function haRenderDiagnoses(diags) {
    if (!haIssuesEl || !diags.length) return;
    const html = diags.slice(0, 10).map(d => {
      const root = escapeMs(d.root_cause || "(미정)");
      const conf = (Number(d.confidence||0)*100).toFixed(0)+"%";
      const rec = escapeMs(d.recommended_action || "");
      const hyps = (d.hypotheses || []).slice(0, 3).map(h =>
        `<li>${escapeMs(h.name)} — ${(Number(h.posterior||h.prior||0)*100).toFixed(0)}%` +
        (h.source === "llm" ? " 🤖" : "") + "</li>").join("");
      const whys = (d.five_whys || []).slice(0, 5).map(w =>
        `<li>${escapeMs(w)}</li>`).join("");
      return `<div class="ms-row">🔎 <b>${escapeMs(d.issue_id||"")}</b> — ${root}` +
             ` <small>(신뢰도 ${conf}, ${escapeMs(d.method||"")})</small>` +
             (whys ? `<details><summary>5 Whys</summary><ol class="ms-neg">${whys}</ol></details>` : "") +
             (hyps ? `<ul class="ms-neg">${hyps}</ul>` : "") +
             (rec ? `<small>다음: ${rec}</small>` : "") + "</div>";
    }).join("");
    haIssuesEl.insertAdjacentHTML("beforeend", html);
  }
  if (haObsBtn) haObsBtn.addEventListener("click", () => {
    const days = Number(haWindowSel?.value || 7);
    haStatus.textContent = "Observer 실행 중…";
    sendMsg({ type: "ha_run_observer", window_days: days });
  });
  const haDiagBtn = $("ha-diag-btn");
  if (haDiagBtn) haDiagBtn.addEventListener("click", () => {
    haStatus.textContent = "Diagnostician 실행 중…";
    sendMsg({ type: "ha_run_diagnostician", limit: 20 });
  });
  const haStratBtn = $("ha-strat-btn");
  if (haStratBtn) haStratBtn.addEventListener("click", () => {
    haStatus.textContent = "Strategist 실행 중…";
    sendMsg({ type: "ha_run_strategist", limit: 5 });
  });
  const haImproverBtn = $("ha-improver-btn");
  if (haImproverBtn) haImproverBtn.addEventListener("click", () => {
    haStatus.textContent = "Improver 실행 중…";
    sendMsg({ type: "ha_run_improver", limit: 50 });
  });
  const haValidatorBtn = $("ha-validator-btn");
  if (haValidatorBtn) haValidatorBtn.addEventListener("click", () => {
    haStatus.textContent = "Validator 실행 중…";
    sendMsg({ type: "ha_run_validator", limit: 50 });
  });
  const haProposalsBtn = $("ha-proposals-btn");
  if (haProposalsBtn) haProposalsBtn.addEventListener("click", () =>
    sendMsg({ type: "ha_proposals_list", status: "pending", limit: 20 }));
  function haRenderProposals(props) {
    if (!haIssuesEl) return;
    if (!props.length) {
      haIssuesEl.insertAdjacentHTML("beforeend",
        `<div class="ms-row">📋 대기 중 제안 없음.</div>`);
      return;
    }
    const html = props.slice(0, 20).map(p => {
      const v = p.latest_validation || {};
      const lvl = (v.risk_level || p.risk_level || "med").toUpperCase();
      const score = (Number(v.risk_score || p.risk_score || 0)*100).toFixed(0)+"%";
      const cat = escapeMs(p.target || "");
      const after = escapeMs((p.after_text || "").slice(0, 200));
      const status = escapeMs(p.status || "pending");
      const blocked = (v.auto_approval_blocked === false) ? "" :
                       " <small>(자동승인 차단 — L1)</small>";
      const buttons = (p.status === "pending") ?
        `<button class="prod-btn mini" data-pid="${escapeMs(p.proposal_id)}" data-dec="approved">승인</button>` +
        `<button class="prod-btn ghost mini" data-pid="${escapeMs(p.proposal_id)}" data-dec="rejected">거부</button>` : "";
      return `<div class="ms-row">📋 <b>[${lvl} · ${score}]</b> <code>${cat}</code> · ${status}${blocked}` +
             `<br><small>${after}</small><br>${buttons}</div>`;
    }).join("");
    haIssuesEl.insertAdjacentHTML("beforeend", html);
    haIssuesEl.querySelectorAll("button[data-pid]").forEach(btn => {
      btn.addEventListener("click", () => {
        sendMsg({ type: "ha_proposal_decision",
                  proposal_id: btn.dataset.pid,
                  decision: btn.dataset.dec, by: "owner" });
      });
    });
  }
  if (haRefreshBtn) haRefreshBtn.addEventListener("click", () =>
    sendMsg({ type: "ha_growth_diary", limit: 10 }));
  if (haOptoutToggle) haOptoutToggle.addEventListener("change", () =>
    sendMsg({ type: "ha_optout", on: haOptoutToggle.checked }));
  if (haKillToggle) haKillToggle.addEventListener("change", () => {
    const on = haKillToggle.checked;
    if (on && !confirm("HA Kill Switch 를 활성화하면 모든 자율 동작이 중지됩니다. 계속?")) {
      haKillToggle.checked = false; return;
    }
    sendMsg({ type: "ha_kill_switch", on });
  });
  setTimeout(() => sendMsg({ type: "ha_growth_diary", limit: 10 }), 2000);

  // -------- 녹화 기능 --------
  function handleRecordingCmd(m) {
    const kind = m.kind || 'video';
    if (kind === 'audio') {
      handleAudioRecordingCmd(m);
      return;
    }
    if (m.action === 'start') {
      startVideoRecording(m.label || '');
    } else if (m.action === 'stop') {
      stopVideoRecording();
    }
  }

  function handleAudioRecordingCmd(m) {
    if (m.action === 'start') {
      startAudioRecording(m.label || '');
    } else if (m.action === 'stop') {
      stopAudioRecording();
    }
  }

  function handleRecordingSaved(m) {
    const label = m.label ? ` (${m.label})` : '';
    if (m.kind === 'photo') {
      const size = m.size_mb ? `${m.size_mb}MB` : '';
      flash(`📷 사진 저장 완료${label} — ${size}`, 'ok');
      return;
    }
    const dur = m.duration_s ? `${m.duration_s}초` : '';
    const size = m.size_mb ? `${m.size_mb}MB` : '';
    const kindLabel = (m.kind === 'audio') ? '녹음' : '녹화';
    flash(`${kindLabel} 저장 완료${label} — ${dur}, ${size}`, 'ok');
  }

  function captureAndSendPhoto(label) {
    if (!camStream || !camVideo || !camVideo.videoWidth) {
      flash('카메라가 꺼져 있습니다.', 'error');
      return;
    }
    const w = camVideo.videoWidth;
    const h = camVideo.videoHeight;
    const c = document.createElement('canvas');
    c.width = w; c.height = h;
    c.getContext('2d').drawImage(camVideo, 0, 0, w, h);
    c.toBlob((blob) => {
      if (!blob) { flash('사진 캡처 실패', 'error'); return; }
      blob.arrayBuffer().then((buf) => {
        const jpegBytes = new Uint8Array(buf);
        const labelBytes = new TextEncoder().encode(label);
        const header = new Uint8Array(3 + labelBytes.length);
        header[0] = 0x0B;
        header[1] = (labelBytes.length >> 8) & 0xFF;
        header[2] = labelBytes.length & 0xFF;
        header.set(labelBytes, 3);
        const packet = new Uint8Array(header.length + jpegBytes.length);
        packet.set(header, 0);
        packet.set(jpegBytes, header.length);
        const ws = window.__ws || window.ws;
        if (ws && ws.readyState === 1) {
          ws.send(packet.buffer);
        }
      });
    }, 'image/jpeg', 0.92);
  }

  function startVideoRecording(label) {
    if (videoRecording) return;
    if (!camStream) { flash('카메라가 꺼져 있습니다.', 'error'); return; }
    videoChunks = [];
    videoRecordingLabel = label;
    const mimeType = MediaRecorder.isTypeSupported('video/webm;codecs=vp9')
      ? 'video/webm;codecs=vp9' : 'video/webm';
    try {
      videoRecorder = new MediaRecorder(camStream, { mimeType });
    } catch (e) {
      flash('녹화를 시작할 수 없습니다: ' + e.message, 'error');
      return;
    }
    videoRecorder.ondataavailable = (e) => {
      if (e.data && e.data.size > 0) videoChunks.push(e.data);
    };
    videoRecorder.onstop = () => {
      const blob = new Blob(videoChunks, { type: 'video/webm' });
      videoChunks = [];
      videoRecording = false;
      videoRecorder = null;
      flash('녹화 중지. 서버로 전송 중...', 'ok');
      sendRecordingBlob(blob);
      updateRecordingUI(false);
    };
    videoRecorder.start(1000);
    videoRecording = true;
    videoRecordingStart = Date.now();
    updateRecordingUI(true);
    flash('🔴 영상 녹화를 시작했습니다.' + (label ? ` (${label})` : ''), 'ok');
  }

  function stopVideoRecording() {
    if (!videoRecorder) return;
    if (!videoRecording) return;
    videoRecording = false;
    try { videoRecorder.stop(); } catch (_) {
      videoRecorder = null;
      updateRecordingUI(false);
    }
  }

  function sendRecordingBlob(blob) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    blob.arrayBuffer().then(buf => {
      const durationMs = Date.now() - videoRecordingStart;
      const labelBytes = new TextEncoder().encode(videoRecordingLabel);
      const header = new Uint8Array(1 + 4 + 2 + labelBytes.length);
      header[0] = 0x09;
      header[1] = (durationMs >> 24) & 0xFF;
      header[2] = (durationMs >> 16) & 0xFF;
      header[3] = (durationMs >> 8) & 0xFF;
      header[4] = durationMs & 0xFF;
      header[5] = (labelBytes.length >> 8) & 0xFF;
      header[6] = labelBytes.length & 0xFF;
      header.set(labelBytes, 7);
      const payload = new Uint8Array(header.length + buf.byteLength);
      payload.set(header, 0);
      payload.set(new Uint8Array(buf), header.length);
      ws.send(payload.buffer);
    });
  }

  let recTimerInterval = null;
  function updateRecordingUI(active) {
    const indicator = document.getElementById('recording-indicator');
    const timeEl = document.getElementById('recording-time');
    if (!indicator) return;
    if (active) {
      indicator.classList.add('active');
      const start = Date.now();
      recTimerInterval = setInterval(() => {
        const elapsed = Math.floor((Date.now() - start) / 1000);
        const mm = String(Math.floor(elapsed / 60)).padStart(2, '0');
        const ss = String(elapsed % 60).padStart(2, '0');
        if (timeEl) timeEl.textContent = `${mm}:${ss}`;
      }, 500);
    } else {
      indicator.classList.remove('active');
      if (recTimerInterval) { clearInterval(recTimerInterval); recTimerInterval = null; }
      if (timeEl) timeEl.textContent = '00:00';
    }
  }

  // -------- 음성 녹음 기능 --------
  async function startAudioRecording(label) {
    if (audioRecording) return;
    audioChunks = [];
    audioRecordingLabel = label;
    try {
      audioStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      flash('마이크 권한이 필요합니다.', 'error');
      return;
    }
    const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
      ? 'audio/webm;codecs=opus' : 'audio/webm';
    try {
      audioRecorder = new MediaRecorder(audioStream, { mimeType });
    } catch (e) {
      flash('녹음을 시작할 수 없습니다: ' + e.message, 'error');
      audioStream.getTracks().forEach(t => t.stop());
      audioStream = null;
      return;
    }
    audioRecorder.ondataavailable = (e) => {
      if (e.data && e.data.size > 0) audioChunks.push(e.data);
    };
    audioRecorder.onstop = () => {
      const blob = new Blob(audioChunks, { type: 'audio/webm' });
      audioChunks = [];
      audioRecording = false;
      audioRecorder = null;
      if (audioStream) {
        audioStream.getTracks().forEach(t => t.stop());
        audioStream = null;
      }
      flash('녹음 중지. 서버로 전송 중...', 'ok');
      sendAudioRecordingBlob(blob);
      updateAudioRecordingUI(false);
    };
    audioRecorder.start(1000);
    audioRecording = true;
    audioRecordingStart = Date.now();
    updateAudioRecordingUI(true);
    flash('🎙️ 음성 녹음을 시작했습니다.' + (label ? ` (${label})` : ''), 'ok');
  }

  function stopAudioRecording() {
    if (!audioRecorder) return;
    if (!audioRecording) return;
    audioRecording = false;
    try { audioRecorder.stop(); } catch (_) {
      audioRecorder = null;
      if (audioStream) { audioStream.getTracks().forEach(t => t.stop()); audioStream = null; }
      updateAudioRecordingUI(false);
    }
  }

  function sendAudioRecordingBlob(blob) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    blob.arrayBuffer().then(buf => {
      const durationMs = Date.now() - audioRecordingStart;
      const labelBytes = new TextEncoder().encode(audioRecordingLabel);
      const header = new Uint8Array(1 + 4 + 2 + labelBytes.length);
      header[0] = 0x0A;
      header[1] = (durationMs >> 24) & 0xFF;
      header[2] = (durationMs >> 16) & 0xFF;
      header[3] = (durationMs >> 8) & 0xFF;
      header[4] = durationMs & 0xFF;
      header[5] = (labelBytes.length >> 8) & 0xFF;
      header[6] = labelBytes.length & 0xFF;
      header.set(labelBytes, 7);
      const payload = new Uint8Array(header.length + buf.byteLength);
      payload.set(header, 0);
      payload.set(new Uint8Array(buf), header.length);
      ws.send(payload.buffer);
    });
  }

  let audioRecTimerInterval = null;
  function updateAudioRecordingUI(active) {
    const indicator = document.getElementById('audio-recording-indicator');
    const timeEl = document.getElementById('audio-recording-time');
    if (!indicator) return;
    if (active) {
      indicator.classList.add('active');
      const start = Date.now();
      audioRecTimerInterval = setInterval(() => {
        const elapsed = Math.floor((Date.now() - start) / 1000);
        const mm = String(Math.floor(elapsed / 60)).padStart(2, '0');
        const ss = String(elapsed % 60).padStart(2, '0');
        if (timeEl) timeEl.textContent = `${mm}:${ss}`;
      }, 500);
    } else {
      indicator.classList.remove('active');
      if (audioRecTimerInterval) { clearInterval(audioRecTimerInterval); audioRecTimerInterval = null; }
      if (timeEl) timeEl.textContent = '00:00';
    }
  }

  attachWSListener();
})();
