"""Pygame UI — 로그인 화면 + 메인 화면 (감정 오브 + 카메라 피드)"""
import math
import random
import threading
import time
from queue import Empty, Queue
from typing import List, Optional

import cv2
import numpy as np
import pygame

from .emotion import Emotion, PALETTES
from .owner_auth import (
    ENROLL_FACE_ANGLES,
    ENROLL_FACE_LABELS_KO,
    random_challenge,
)
from .vision import compute_face_encoding_from_jpeg


# ============ Colors ============
BG_DEEP    = (3, 7, 12)
BG_PANEL   = (8, 14, 22)
GRID       = (8, 18, 30)
ACCENT     = (0, 217, 255)
ACCENT_DIM = (0, 90, 130)
TEXT       = (207, 233, 245)
TEXT_DIM   = (120, 150, 170)
RED        = (255, 80, 100)
AMBER      = (255, 180, 60)


def get_font(size: int, bold: bool = False) -> pygame.font.Font:
    """한국어를 지원하는 시스템 폰트 시도"""
    candidates = (
        "malgungothic,applesdgothicneo,nanumgothic,notosanscjkkr,"
        "consolas,menlo,dejavusansmono,monospace"
    )
    return pygame.font.SysFont(candidates, size, bold=bold)


# ============ 위젯들 ============
class TextInput:
    def __init__(self, rect, font, placeholder="", password=False):
        self.rect = pygame.Rect(rect)
        self.font = font
        self.placeholder = placeholder
        self.password = password
        self.text = ""
        self.active = False
        self._cursor_visible = True
        self._cursor_t = time.time()

    def handle_event(self, event):
        if event.type == pygame.MOUSEBUTTONDOWN:
            self.active = self.rect.collidepoint(event.pos)
        elif event.type == pygame.KEYDOWN and self.active:
            if event.key == pygame.K_BACKSPACE:
                self.text = self.text[:-1]
            elif event.key == pygame.K_RETURN:
                return "submit"
            elif event.key == pygame.K_TAB:
                return "tab"
            elif event.unicode and event.unicode.isprintable():
                self.text += event.unicode
        return None

    def draw(self, surface):
        color = ACCENT if self.active else ACCENT_DIM
        pygame.draw.rect(surface, (10, 16, 24), self.rect)
        pygame.draw.rect(surface, color, self.rect, 1)
        # 모서리 강조
        L = 8
        for cx, cy, sx, sy in [
            (self.rect.left, self.rect.top, 1, 1),
            (self.rect.right, self.rect.top, -1, 1),
            (self.rect.left, self.rect.bottom, 1, -1),
            (self.rect.right, self.rect.bottom, -1, -1),
        ]:
            pygame.draw.line(surface, color, (cx, cy), (cx + L * sx, cy), 2)
            pygame.draw.line(surface, color, (cx, cy), (cx, cy + L * sy), 2)

        display = self.text if not self.password else "•" * len(self.text)
        if not display and not self.active:
            text_surf = self.font.render(self.placeholder, True, TEXT_DIM)
        else:
            text_surf = self.font.render(display, True, TEXT)
        surface.blit(
            text_surf,
            (self.rect.x + 14, self.rect.y + (self.rect.h - text_surf.get_height()) // 2),
        )

        if self.active:
            now = time.time()
            if now - self._cursor_t > 0.5:
                self._cursor_visible = not self._cursor_visible
                self._cursor_t = now
            if self._cursor_visible:
                cx = self.rect.x + 14 + text_surf.get_width() + 2
                pygame.draw.line(
                    surface, ACCENT,
                    (cx, self.rect.y + 8), (cx, self.rect.y + self.rect.h - 8), 2
                )


class Button:
    def __init__(self, rect, text, font, color=ACCENT):
        self.rect = pygame.Rect(rect)
        self.text = text
        self.font = font
        self.color = color
        self.hover = False

    def handle_event(self, event):
        if event.type == pygame.MOUSEMOTION:
            self.hover = self.rect.collidepoint(event.pos)
        elif event.type == pygame.MOUSEBUTTONDOWN and self.rect.collidepoint(event.pos):
            return True
        return False

    def draw(self, surface):
        bg = (10, 60, 80) if self.hover else (10, 30, 40)
        pygame.draw.rect(surface, bg, self.rect)
        pygame.draw.rect(surface, self.color, self.rect, 1)
        text_surf = self.font.render(self.text, True, self.color)
        surface.blit(text_surf, text_surf.get_rect(center=self.rect.center))


# ============ 메인 UI ============
class SarvisUI:
    WIDTH = 1280
    HEIGHT = 800

    def __init__(self):
        pygame.init()
        pygame.display.set_caption("S.A.R.V.I.S")
        self.screen = pygame.display.set_mode((self.WIDTH, self.HEIGHT))
        self.clock = pygame.time.Clock()

        self.font_xs = get_font(11)
        self.font_sm = get_font(13)
        self.font_md = get_font(16)
        self.font_lg = get_font(22, bold=True)
        self.font_xl = get_font(38, bold=True)

        self._t0 = time.time()
        self._particles = self._init_particles(70)

    @property
    def t(self):
        return time.time() - self._t0

    def _init_particles(self, n):
        return [
            {
                "angle": random.uniform(0, 2 * math.pi),
                "radius_mult": random.uniform(0.7, 1.7),
                "speed": random.uniform(0.3, 1.0) * random.choice([-1, 1]),
                "size": random.uniform(1.2, 3.0),
                "phase": random.uniform(0, 2 * math.pi),
            }
            for _ in range(n)
        ]

    def _draw_grid_bg(self):
        self.screen.fill(BG_DEEP)
        spacing = 40
        for x in range(0, self.WIDTH, spacing):
            pygame.draw.line(self.screen, GRID, (x, 0), (x, self.HEIGHT))
        for y in range(0, self.HEIGHT, spacing):
            pygame.draw.line(self.screen, GRID, (0, y), (self.WIDTH, y))

    # ============ 오브 렌더링 (사비스의 얼굴) ============
    def _draw_orb(self, surface, cx, cy, base_radius, emotion: Emotion, alpha_mult=1.0):
        palette = PALETTES[emotion]
        rate = palette.pulse_rate
        intensity = palette.intensity
        pulse = 1 + 0.06 * math.sin(self.t * rate * 2 * math.pi)
        radius = base_radius * pulse

        W, H = surface.get_width(), surface.get_height()

        # 1) 부드러운 외곽 글로우 — 3개의 미묘한 링
        glow = pygame.Surface((W, H), pygame.SRCALPHA)
        for i in range(3, 0, -1):
            t = i / 3
            alpha = int(55 * alpha_mult * t)
            r = int(radius * (1.1 + i * 0.16))
            thickness = max(1, 5 - i)
            pygame.draw.circle(glow, (*palette.glow, alpha), (cx, cy), r, thickness)
        surface.blit(glow, (0, 0), special_flags=pygame.BLEND_ADD)

        # 2) 회전 링 4개 — 더 잘 보이게 (사비스의 시그니처)
        ring_specs = [
            (0.35, 1.5, 0.45, 2, 220),
            (-0.55, 1.85, 0.65, 2, 180),
            (0.85, 2.2, 0.35, 2, 150),
            (-1.2, 2.6, 0.25, 1, 120),
        ]
        for rot_speed, rx, ry, thick, base_alpha in ring_specs:
            ring_w = int(radius * rx * 2 + 24)
            ring_h = int(radius * ry * 2 + 24)
            ring_surf = pygame.Surface((ring_w, ring_h), pygame.SRCALPHA)
            alpha = int(base_alpha * alpha_mult)
            pygame.draw.ellipse(
                ring_surf, (*palette.primary, alpha),
                (12, 12, int(radius * rx * 2), int(radius * ry * 2)), thick,
            )
            angle_deg = math.degrees(self.t * rot_speed * intensity)
            rotated = pygame.transform.rotate(ring_surf, angle_deg)
            surface.blit(rotated, rotated.get_rect(center=(cx, cy)))

        # 3) 코어 구체 (방사형 그라데이션, 작고 밝게)
        gsize = int(radius * 2.2)
        gsurf = pygame.Surface((gsize, gsize), pygame.SRCALPHA)
        gx, gy = gsize // 2, gsize // 2
        steps = max(12, int(radius // 2))
        for s in range(steps, 0, -1):
            tn = s / steps
            color = (
                int(palette.primary[0] * tn + palette.glow[0] * (1 - tn)),
                int(palette.primary[1] * tn + palette.glow[1] * (1 - tn)),
                int(palette.primary[2] * tn + palette.glow[2] * (1 - tn)),
                int(255 * alpha_mult),
            )
            pygame.draw.circle(gsurf, color, (gx, gy), int(radius * 0.9 * tn))
        surface.blit(gsurf, (cx - gx, cy - gy))

        # 4) 광택 하이라이트 (좌상단)
        hl_off = int(radius * 0.3)
        hl_r = max(4, int(radius * 0.16))
        hl = pygame.Surface((hl_r * 4, hl_r * 4), pygame.SRCALPHA)
        for s in range(hl_r, 0, -1):
            a = int(110 * alpha_mult * (1 - s / hl_r))
            pygame.draw.circle(hl, (255, 255, 255, a), (hl_r * 2, hl_r * 2), s)
        surface.blit(hl, (cx - hl_off - hl_r * 2, cy - hl_off - hl_r * 2))

        # 5) 입자 (궤도)
        for p in self._particles:
            angle = p["angle"] + self.t * p["speed"] * intensity
            orbit = radius * p["radius_mult"] * (1 + 0.08 * math.sin(self.t * 2 + p["phase"]))
            px = cx + orbit * math.cos(angle)
            py = cy + orbit * math.sin(angle) * 0.45  # 타원 궤도
            sz = p["size"] * 0.8 * (1 + 0.4 * math.sin(self.t * 3 + p["phase"]))
            ps = pygame.Surface((int(sz * 4), int(sz * 4)), pygame.SRCALPHA)
            pygame.draw.circle(
                ps, (*palette.primary, int(200 * alpha_mult)),
                (int(sz * 2), int(sz * 2)), int(sz),
            )
            surface.blit(ps, (px - sz * 2, py - sz * 2), special_flags=pygame.BLEND_ADD)

    # ============ 카메라 피드 렌더링 ============
    def _draw_camera(self, frame: Optional[np.ndarray], x, y, w, h):
        # 배경 박스
        pygame.draw.rect(self.screen, BG_PANEL, (x, y, w, h))

        if frame is None:
            no_signal = self.font_sm.render("NO SIGNAL", True, RED)
            self.screen.blit(no_signal, no_signal.get_rect(center=(x + w // 2, y + h // 2)))
        else:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            rgb = cv2.resize(rgb, (w, h))
            surface = pygame.image.frombuffer(rgb.tobytes(), (w, h), "RGB")
            self.screen.blit(surface, (x, y))

        # 외곽선과 코너
        pygame.draw.rect(self.screen, ACCENT_DIM, (x, y, w, h), 1)
        L = 12
        for cx, cy, sx, sy in [
            (x, y, 1, 1), (x + w - 1, y, -1, 1),
            (x, y + h - 1, 1, -1), (x + w - 1, y + h - 1, -1, -1),
        ]:
            pygame.draw.line(self.screen, ACCENT, (cx, cy), (cx + L * sx, cy), 2)
            pygame.draw.line(self.screen, ACCENT, (cx, cy), (cx, cy + L * sy), 2)

        # 라벨
        label_bg = pygame.Surface((w, 22), pygame.SRCALPHA)
        label_bg.fill((0, 0, 0, 140))
        self.screen.blit(label_bg, (x, y))
        label = self.font_xs.render("VISUAL FEED · WHAT SARVIS SEES", True, ACCENT)
        self.screen.blit(label, (x + 8, y + 5))

        # 스캔라인
        if frame is not None:
            scan_y = int(y + (self.t * 60) % h)
            line_surf = pygame.Surface((w, 2), pygame.SRCALPHA)
            line_surf.fill((*ACCENT, 100))
            self.screen.blit(line_surf, (x, scan_y))

    # ============ 로그인 화면 ============
    def run_login(self, auth) -> Optional[str]:
        is_first = not auth.has_users()

        cx = self.WIDTH // 2
        username_input = TextInput((cx - 160, 380, 320, 46), self.font_md, "Username")
        password_input = TextInput((cx - 160, 442, 320, 46), self.font_md, "Password", password=True)
        login_btn_label = "INITIALIZE SYSTEM" if is_first else "AUTHENTICATE"
        login_button = Button((cx - 160, 514, 320, 46), login_btn_label, self.font_md)

        message = ""
        username_input.active = True

        def submit():
            nonlocal message
            u, p = username_input.text, password_input.text
            if is_first:
                err = auth.create_user_detail(u, p)
                if err is None:
                    return u
                message = err
                password_input.text = ""
            else:
                if auth.verify(u, p):
                    return u
                message = "인증 실패 — 사용자명 또는 비밀번호가 잘못되었습니다."
                password_input.text = ""
            return None

        running = True
        while running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    return None
                if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                    return None

                a1 = username_input.handle_event(event)
                if a1 in ("tab", "submit"):
                    username_input.active = False
                    password_input.active = True

                a2 = password_input.handle_event(event)
                if a2 == "submit":
                    result = submit()
                    if result:
                        return result

                if login_button.handle_event(event):
                    result = submit()
                    if result:
                        return result

            self._draw_grid_bg()

            # 배경 오브 (장식용, 작게)
            self._draw_orb(self.screen, cx, 200, 70, Emotion.NEUTRAL, alpha_mult=0.7)

            # 타이틀
            title = self.font_xl.render("S.A.R.V.I.S", True, ACCENT)
            self.screen.blit(title, title.get_rect(center=(cx, 300)))
            sub = "INITIAL SETUP — CREATE YOUR ACCOUNT" if is_first else "AUTHENTICATION REQUIRED"
            sub_surf = self.font_sm.render(sub, True, TEXT_DIM)
            self.screen.blit(sub_surf, sub_surf.get_rect(center=(cx, 340)))

            username_input.draw(self.screen)
            password_input.draw(self.screen)
            login_button.draw(self.screen)

            if message:
                msg_surf = self.font_sm.render(message, True, RED)
                self.screen.blit(msg_surf, msg_surf.get_rect(center=(cx, 580)))

            # 풋터 힌트
            hint_text = "TAB: 다음 필드   ENTER: 제출   ESC: 종료"
            hint = self.font_xs.render(hint_text, True, TEXT_DIM)
            self.screen.blit(hint, hint.get_rect(center=(cx, self.HEIGHT - 40)))

            pygame.display.flip()
            self.clock.tick(60)

        return None

    # ============ Owner 인증 (사이클 #29 — 데스크톱 통합) ============
    # 데스크톱 모드는 single-user. 5각도 얼굴 + 음성 패스프레이즈 + 이름으로 등록,
    # 이후 얼굴 매칭 + 음성 패스프레이즈/챌린지로 로그인. 1시간 후 백그라운드 재인증
    # (자리비움 시 자동 로그아웃) 은 main.py 에서 처리한다.

    def _extract_encoding_bgr(self, frame_bgr) -> Optional[List[float]]:
        """BGR numpy frame 에서 얼굴 인코딩(128 floats)을 추출. 실패 시 None."""
        if frame_bgr is None:
            return None
        ok, buf = cv2.imencode(".jpg", frame_bgr, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if not ok:
            return None
        return compute_face_encoding_from_jpeg(buf.tobytes())

    def _draw_auth_header(self, title: str, subtitle: str, cx: int):
        """인증 화면 공통 상단 — 그리드 배경 + 작은 오브 + 타이틀."""
        self._draw_grid_bg()
        self._draw_orb(self.screen, cx, 100, 38, Emotion.NEUTRAL, alpha_mult=0.6)
        title_surf = self.font_xl.render(title, True, ACCENT)
        self.screen.blit(title_surf, title_surf.get_rect(center=(cx, 180)))
        sub_surf = self.font_sm.render(subtitle, True, TEXT_DIM)
        self.screen.blit(sub_surf, sub_surf.get_rect(center=(cx, 220)))

    def _draw_message(self, text: str, cx: int, y: int, color=AMBER):
        if not text:
            return
        msg_surf = self.font_sm.render(text, True, color)
        self.screen.blit(msg_surf, msg_surf.get_rect(center=(cx, y)))

    def _draw_hint(self, text: str, cx: int):
        hint = self.font_xs.render(text, True, TEXT_DIM)
        self.screen.blit(hint, hint.get_rect(center=(cx, self.HEIGHT - 40)))

    def _capture_face_for_angle(
        self,
        vision,
        title: str,
        angle_label: str,
        progress: str,
    ) -> Optional[List[float]]:
        """카메라 라이브뷰 + SPACE/캡처 버튼 → 얼굴 인코딩 반환. ESC 면 None.

        사용자가 해당 방향을 보고 SPACE 를 누르면 그 프레임에서 인코딩을 시도.
        실패하면 메시지 갱신 후 재시도. ESC 또는 창 닫기는 전체 등록 취소.
        """
        cx = self.WIDTH // 2
        cam_x, cam_y, cam_w, cam_h = cx - 320, 280, 640, 360
        capture_btn = Button(
            (cx - 120, cam_y + cam_h + 32, 240, 46),
            "CAPTURE  (SPACE)", self.font_md,
        )

        msg = ""
        msg_color = AMBER
        while True:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    return None
                if event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE:
                        return None
                    if event.key == pygame.K_SPACE:
                        enc = self._extract_encoding_bgr(vision.read())
                        if enc is None:
                            msg = "얼굴이 인식되지 않습니다. 카메라 정면을 보고 다시 시도하세요."
                            msg_color = RED
                            continue
                        return enc
                if capture_btn.handle_event(event):
                    enc = self._extract_encoding_bgr(vision.read())
                    if enc is None:
                        msg = "얼굴이 인식되지 않습니다. 카메라 정면을 보고 다시 시도하세요."
                        msg_color = RED
                        continue
                    return enc

            frame = vision.read()
            self._draw_auth_header(title, progress, cx)
            instr = f"{angle_label}을(를) 봐주세요"
            instr_surf = self.font_lg.render(instr, True, TEXT)
            self.screen.blit(instr_surf, instr_surf.get_rect(center=(cx, 254)))
            self._draw_camera(frame, cam_x, cam_y, cam_w, cam_h)
            capture_btn.draw(self.screen)
            self._draw_message(msg, cx, cam_y + cam_h + 100, msg_color)
            self._draw_hint("SPACE: 캡처   ESC: 취소", cx)
            pygame.display.flip()
            self.clock.tick(60)

    def _record_voice_text(
        self,
        recorder,
        stt,
        vision,
        title: str,
        instruction: str,
        min_chars: int,
        password: bool = False,
        challenge_text: Optional[str] = None,
    ) -> Optional[str]:
        """SPACE → 마이크 녹음 → STT → 텍스트 편집/확정.

        challenge_text 가 있으면 사용자에게 따라 말할 문장으로 표시 (로그인 챌린지).
        ESC 면 None 반환 → 호출자가 흐름 중단.
        """
        cx = self.WIDTH // 2
        cam_x, cam_y, cam_w, cam_h = cx - 320, 280, 640, 240
        text_input = TextInput(
            (cx - 220, cam_y + cam_h + 36, 440, 46),
            self.font_md, "STT 결과 (수정 가능)", password=password,
        )
        confirm_btn = Button(
            (cx - 220, cam_y + cam_h + 96, 210, 42),
            "CONFIRM  (ENTER)", self.font_md,
        )
        retry_btn = Button(
            (cx + 10, cam_y + cam_h + 96, 210, 42),
            "RE-RECORD  (F2)", self.font_md,
        )

        state = "idle"   # idle | recording | recording_done
        result_q: "Queue[tuple]" = Queue()
        msg = ""
        msg_color = AMBER

        def _start_record():
            def _worker():
                try:
                    audio = recorder.record()
                    text = stt.transcribe(audio).strip()
                    result_q.put(("ok", text))
                except Exception as e:
                    result_q.put(("err", f"{type(e).__name__}: {e}"))
            threading.Thread(target=_worker, daemon=True).start()

        while True:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    return None
                if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                    return None

                if state == "idle":
                    if event.type == pygame.KEYDOWN and event.key == pygame.K_SPACE:
                        msg = ""
                        state = "recording"
                        _start_record()
                elif state == "recording_done":
                    if event.type == pygame.KEYDOWN and event.key == pygame.K_F2:
                        msg = ""
                        state = "recording"
                        _start_record()
                        continue
                    if retry_btn.handle_event(event):
                        msg = ""
                        state = "recording"
                        _start_record()
                        continue
                    action = text_input.handle_event(event)
                    if action == "submit":
                        if len(text_input.text.strip()) >= min_chars:
                            return text_input.text.strip()
                        msg = f"{min_chars}자 이상 입력해주세요."
                        msg_color = RED
                    if confirm_btn.handle_event(event):
                        if len(text_input.text.strip()) >= min_chars:
                            return text_input.text.strip()
                        msg = f"{min_chars}자 이상 입력해주세요."
                        msg_color = RED

            if state == "recording":
                try:
                    kind, payload = result_q.get_nowait()
                except Empty:
                    pass
                else:
                    if kind == "ok":
                        text_input.text = payload
                        text_input.active = True
                        if not payload:
                            msg = "음성을 인식하지 못했습니다. 다시 시도하세요."
                            msg_color = RED
                            state = "idle"
                        else:
                            state = "recording_done"
                    else:
                        msg = f"녹음 실패 — {payload}"
                        msg_color = RED
                        state = "idle"

            self._draw_auth_header(title, instruction, cx)
            if challenge_text:
                cl_surf = self.font_lg.render(f"“{challenge_text}”", True, AMBER)
                self.screen.blit(cl_surf, cl_surf.get_rect(center=(cx, 254)))

            self._draw_camera(vision.read(), cam_x, cam_y, cam_w, cam_h)
            self._draw_state_indicator(state, cx, cam_y - 12)

            if state == "idle":
                hint_surf = self.font_md.render(
                    "SPACE 를 눌러 녹음 시작 (말씀하신 후 잠시 침묵하면 자동 종료)",
                    True, TEXT,
                )
                self.screen.blit(hint_surf, hint_surf.get_rect(center=(cx, cam_y + cam_h + 56)))
            else:
                text_input.draw(self.screen)
                if state == "recording_done":
                    confirm_btn.draw(self.screen)
                    retry_btn.draw(self.screen)

            self._draw_message(msg, cx, cam_y + cam_h + 160, msg_color)
            self._draw_hint(
                "SPACE: 녹음   ENTER: 확정   F2: 재녹음   ESC: 취소",
                cx,
            )
            pygame.display.flip()
            self.clock.tick(60)

    def _draw_state_indicator(self, state: str, cx: int, y: int):
        """녹음 상태를 카메라 위에 점멸 표시."""
        if state == "recording":
            pulse = 0.5 + 0.5 * math.sin(self.t * 6)
            color = (int(255 * pulse), 60, 80)
            text = "● RECORDING — 말씀해주세요"
        elif state == "recording_done":
            color = (100, 240, 180)
            text = "● 녹음 완료 — 결과를 확인하고 확정하세요"
        else:
            color = TEXT_DIM
            text = "○ 대기 중"
        surf = self.font_sm.render(text, True, color)
        self.screen.blit(surf, surf.get_rect(center=(cx, y)))

    def run_owner_enroll(self, vision, recorder, stt, owner) -> bool:
        """주인 등록 — 5각도 얼굴 + 음성 패스프레이즈 + 이름. 성공 시 True.

        사용자가 ESC 누르거나 창을 닫으면 False (취소). 음성 인식이 실패하면
        호출자가 다시 시도할 수 있도록 한 단계만 포기하지 않고 _record_voice_text
        내부에서 재녹음을 지원한다.
        """
        cx = self.WIDTH // 2
        angles = list(ENROLL_FACE_ANGLES)
        encs: List[List[float]] = []

        # 1) 5각도 얼굴 캡처 — 각 단계마다 사용자가 자세를 바꾸도록 안내.
        for idx, angle in enumerate(angles):
            label = ENROLL_FACE_LABELS_KO.get(angle, angle)
            progress = f"얼굴 등록 ({idx + 1}/{len(angles)}): {label}"
            enc = self._capture_face_for_angle(
                vision,
                title="INITIAL ENROLLMENT",
                angle_label=label,
                progress=progress,
            )
            if enc is None:
                return False
            encs.append(enc)

        # 2) 음성 패스프레이즈 — 정규화 후 4자 이상.
        passphrase = self._record_voice_text(
            recorder, stt, vision,
            title="VOICE PASSPHRASE",
            instruction="기억할 패스프레이즈를 4자 이상 또렷하게 말씀해주세요",
            min_chars=4,
            password=True,
        )
        if passphrase is None:
            return False

        # 3) 이름 — 음성으로 받고 사용자가 STT 결과를 검토/수정.
        name = self._record_voice_text(
            recorder, stt, vision,
            title="YOUR NAME",
            instruction="이름을 말씀해주세요 (필요하면 결과를 수정하고 확정)",
            min_chars=1,
            password=False,
        )
        if name is None:
            return False

        # 4) 저장 — 실패 시 사용자에게 알리고 호출자에 False 반환.
        try:
            owner.enroll(
                face_name=name,
                voice_passphrase=passphrase,
                face_encodings=encs,
                face_angles=angles,
            )
        except ValueError as ve:
            self._show_blocking_message(
                title="ENROLLMENT FAILED",
                message=str(ve),
                color=RED,
            )
            return False
        return True

    def run_owner_login(self, vision, recorder, stt, owner) -> Optional[str]:
        """주인 로그인 — 카메라에서 얼굴 매칭 → 음성 패스프레이즈/챌린지.

        통과 시 owner.face_name 반환. 사용자가 취소하면 None.
        """
        cx = self.WIDTH // 2
        cam_x, cam_y, cam_w, cam_h = cx - 320, 280, 640, 360
        retry_btn = Button(
            (cx - 120, cam_y + cam_h + 32, 240, 46),
            "RE-CHECK FACE  (R)", self.font_md,
        )

        # 1) 얼굴 매칭 — 매 프레임 인코딩 추출 시도. 자동 통과 (사용자 수동 트리거 불필요).
        msg = "카메라를 정면으로 바라봐 주세요."
        msg_color = TEXT
        last_attempt = 0.0
        face_matched = False
        while not face_matched:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    return None
                if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                    return None
                if event.type == pygame.KEYDOWN and event.key == pygame.K_r:
                    last_attempt = 0.0  # 즉시 재시도
                if retry_btn.handle_event(event):
                    last_attempt = 0.0

            now = time.time()
            if now - last_attempt >= 0.5:
                last_attempt = now
                enc = self._extract_encoding_bgr(vision.read())
                if enc is not None:
                    if owner.verify_face_encoding(enc):
                        face_matched = True
                        msg = "얼굴 인증 통과 — 음성 인증으로 진행합니다."
                        msg_color = (100, 240, 180)
                    else:
                        dist = owner.face_distance_min(enc)
                        msg = f"등록된 얼굴과 일치하지 않습니다 (거리 {dist:.2f})."
                        msg_color = RED

            self._draw_auth_header(
                "AUTHENTICATION",
                f"환영합니다, {owner.face_name} 님 — 얼굴 확인 중",
                cx,
            )
            self._draw_camera(vision.read(), cam_x, cam_y, cam_w, cam_h)
            retry_btn.draw(self.screen)
            self._draw_message(msg, cx, cam_y + cam_h + 100, msg_color)
            self._draw_hint("R: 재시도   ESC: 취소", cx)
            pygame.display.flip()
            self.clock.tick(60)

            if face_matched:
                # 통과 메시지를 잠시 보여주기 위한 짧은 지연.
                pygame.time.wait(450)

        # 2) 음성 패스프레이즈/챌린지 — 챌린지를 함께 발급해 녹음 재생 공격 차단.
        challenge = random_challenge()
        spoken = self._record_voice_text(
            recorder, stt, vision,
            title="VOICE AUTHENTICATION",
            instruction="패스프레이즈 또는 아래 문장 중 하나를 말씀해주세요",
            min_chars=1,
            password=False,
            challenge_text=challenge,
        )
        if spoken is None:
            return None

        ok, sim, matched_against = owner.verify_voice(spoken, challenge_text=challenge)
        if not ok:
            self._show_blocking_message(
                title="VOICE AUTH FAILED",
                message=(
                    f"음성이 일치하지 않습니다 (유사도 {sim:.2f}). "
                    "ESC 로 종료하거나 창을 닫고 다시 시도해주세요."
                ),
                color=RED,
            )
            return None

        return owner.face_name

    def _show_blocking_message(self, title: str, message: str, color=AMBER):
        """결과 메시지 화면 — 사용자가 ENTER/SPACE/ESC 누를 때까지 대기."""
        cx = self.WIDTH // 2
        cy = self.HEIGHT // 2
        ok_btn = Button((cx - 80, cy + 40, 160, 42), "OK  (ENTER)", self.font_md)
        while True:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    return
                if event.type == pygame.KEYDOWN and event.key in (
                    pygame.K_RETURN, pygame.K_SPACE, pygame.K_ESCAPE,
                ):
                    return
                if ok_btn.handle_event(event):
                    return
            self._draw_grid_bg()
            title_surf = self.font_xl.render(title, True, color)
            self.screen.blit(title_surf, title_surf.get_rect(center=(cx, cy - 80)))
            msg_surf = self.font_md.render(message, True, TEXT)
            self.screen.blit(msg_surf, msg_surf.get_rect(center=(cx, cy - 20)))
            ok_btn.draw(self.screen)
            pygame.display.flip()
            self.clock.tick(60)

    # ============ 메인 화면 ============
    def render_main(
        self,
        frame: Optional[np.ndarray],
        state: str,
        emotion: Emotion,
        logged_user: str,
        camera_user: Optional[str],
        chat_log: List[dict],
        backend: str,
        current_tool: Optional[str] = None,
    ):
        self._draw_grid_bg()

        # ===== 상단 바 =====
        pygame.draw.rect(self.screen, BG_PANEL, (0, 0, self.WIDTH, 50))
        pygame.draw.line(self.screen, ACCENT_DIM, (0, 50), (self.WIDTH, 50))
        title = self.font_lg.render("S . A . R . V . I . S", True, ACCENT)
        self.screen.blit(title, (24, 13))

        state_color = {
            "idle": TEXT_DIM, "listening": AMBER,
            "thinking": ACCENT, "speaking": (100, 240, 255),
        }.get(state, TEXT)
        state_surf = self.font_sm.render(f"STATE  {state.upper()}", True, state_color)
        self.screen.blit(state_surf, state_surf.get_rect(center=(self.WIDTH // 2, 25)))

        right_info = (
            f"{time.strftime('%H:%M:%S')}    "
            f"USER  {logged_user}    "
            f"BRAIN  {backend.upper()}"
        )
        info_surf = self.font_sm.render(right_info, True, TEXT)
        self.screen.blit(info_surf, info_surf.get_rect(midright=(self.WIDTH - 24, 25)))

        # ===== 레이아웃: 좌(오브) | 우(카메라+로그) =====
        orb_w = int(self.WIDTH * 0.62)
        panel_x = orb_w
        panel_w = self.WIDTH - orb_w
        pygame.draw.line(self.screen, ACCENT_DIM, (panel_x, 50), (panel_x, self.HEIGHT))

        # ===== 메인 오브 =====
        orb_cx = orb_w // 2
        orb_cy = 50 + (self.HEIGHT - 50) // 2 - 50
        orb_radius = min(orb_w // 7, (self.HEIGHT - 50) // 6)
        self._draw_orb(self.screen, orb_cx, orb_cy, orb_radius, emotion)

        # 오브 아래 라벨
        emo_color = PALETTES[emotion].primary
        emo_label = self.font_lg.render(emotion.value.upper(), True, emo_color)
        self.screen.blit(emo_label, emo_label.get_rect(center=(orb_cx, orb_cy + orb_radius + 90)))

        hints = {
            "idle": "Say 'SARVIS' to wake up",
            "listening": "▸ Listening...",
            "thinking": "▸ Processing...",
            "speaking": "▸ Speaking...",
        }
        hint = self.font_sm.render(hints.get(state, ""), True, TEXT_DIM)
        self.screen.blit(hint, hint.get_rect(center=(orb_cx, orb_cy + orb_radius + 122)))

        # 도구 사용 중일 때 표시
        if current_tool:
            tool_text = f"⚙ USING TOOL  ›  {current_tool.upper()}"
            tool_surf = self.font_sm.render(tool_text, True, AMBER)
            tool_bg = pygame.Surface(
                (tool_surf.get_width() + 24, tool_surf.get_height() + 12),
                pygame.SRCALPHA,
            )
            tool_bg.fill((30, 20, 0, 180))
            pygame.draw.rect(tool_bg, AMBER, tool_bg.get_rect(), 1)
            tb_rect = tool_bg.get_rect(center=(orb_cx, orb_cy + orb_radius + 156))
            self.screen.blit(tool_bg, tb_rect)
            self.screen.blit(tool_surf, tool_surf.get_rect(center=tb_rect.center))

        # 좌하단 단축키 안내
        keys_text = "Q:종료    1:Claude    2:Ollama    R:대화 리셋"
        keys_surf = self.font_xs.render(keys_text, True, TEXT_DIM)
        self.screen.blit(keys_surf, (24, self.HEIGHT - 28))

        # ===== 우측 패널 =====
        # 카메라
        pad = 18
        cam_w = panel_w - pad * 2
        cam_h = int(cam_w * 9 / 16)
        cam_x = panel_x + pad
        cam_y = 50 + pad
        self._draw_camera(frame, cam_x, cam_y, cam_w, cam_h)

        # 카메라 아래 사용자 식별 정보
        info_y = cam_y + cam_h + 12
        identity = camera_user or "— UNKNOWN —"
        id_color = ACCENT if camera_user else RED
        id_label = self.font_xs.render("DETECTED  ", True, TEXT_DIM)
        id_value = self.font_sm.render(identity, True, id_color)
        self.screen.blit(id_label, (cam_x, info_y))
        self.screen.blit(id_value, (cam_x + id_label.get_width(), info_y - 1))

        # 로그 패널
        log_y = info_y + 28
        log_h = self.HEIGHT - log_y - 18
        pygame.draw.rect(self.screen, BG_PANEL, (cam_x, log_y, cam_w, log_h))
        pygame.draw.rect(self.screen, ACCENT_DIM, (cam_x, log_y, cam_w, log_h), 1)

        log_header = self.font_xs.render("COMMUNICATION LOG", True, ACCENT)
        self.screen.blit(log_header, (cam_x + 12, log_y + 10))
        pygame.draw.line(
            self.screen, ACCENT_DIM,
            (cam_x + 12, log_y + 30), (cam_x + cam_w - 12, log_y + 30),
        )

        # 메시지 (최신이 아래)
        msg_y = log_y + 42
        max_y = log_y + log_h - 14
        line_w_max = cam_w - 30

        # 마지막 N개만 그리되, 위에서 아래로
        visible = chat_log[-12:]
        for msg in visible:
            if msg_y > max_y - 16:
                break
            who = "YOU" if msg["role"] == "user" else "SARVIS"
            who_color = AMBER if msg["role"] == "user" else ACCENT
            who_surf = self.font_xs.render(f"▸ {who}", True, who_color)
            self.screen.blit(who_surf, (cam_x + 12, msg_y))
            msg_y += 16

            # 텍스트 줄바꿈
            words = msg["text"].split()
            line = ""
            for word in words:
                test = (line + " " + word).strip() if line else word
                if self.font_sm.size(test)[0] > line_w_max:
                    if line:
                        surf = self.font_sm.render(line, True, TEXT)
                        self.screen.blit(surf, (cam_x + 18, msg_y))
                        msg_y += 18
                    line = word
                    if msg_y > max_y - 14:
                        break
                else:
                    line = test
            if line and msg_y <= max_y - 14:
                surf = self.font_sm.render(line, True, TEXT)
                self.screen.blit(surf, (cam_x + 18, msg_y))
                msg_y += 18
            msg_y += 6

    def tick(self, fps=60):
        self.clock.tick(fps)

    def quit(self):
        pygame.quit()
