"""Pygame UI — 로그인 화면 + 메인 화면 (감정 오브 + 카메라 피드)"""
import math
import random
import time
from typing import List, Optional

import cv2
import numpy as np
import pygame

from emotion import Emotion, PALETTES


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
class JarvisUI:
    WIDTH = 1280
    HEIGHT = 800

    def __init__(self):
        pygame.init()
        pygame.display.set_caption("J.A.R.V.I.S")
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

    # ============ 오브 렌더링 (자비스의 얼굴) ============
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

        # 2) 회전 링 4개 — 더 잘 보이게 (자비스의 시그니처)
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
        label = self.font_xs.render("VISUAL FEED · WHAT JARVIS SEES", True, ACCENT)
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
                if auth.create_user(u, p):
                    return u
                message = "사용자명/비밀번호 확인 (4자 이상)"
            else:
                if auth.verify(u, p):
                    return u
                message = "인증 실패"
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
            title = self.font_xl.render("J.A.R.V.I.S", True, ACCENT)
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
        title = self.font_lg.render("J . A . R . V . I . S", True, ACCENT)
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
            "idle": "Say 'JARVIS' to wake up",
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
            who = "YOU" if msg["role"] == "user" else "JARVIS"
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
