"""vision.py 단위 테스트 — _safe_filename / FaceRegistry / WebVision 의 cv2 미설치 경로.

architect 사이클 #7 follow-up:
  - _safe_filename 의 한글/특수문자/길이 제한
  - FaceRegistry register/delete/list/get_references/is_empty
  - WebVision 이 cv2 없이도 실패하지 않고 None/False 반환

cv2 가 사용 가능한 환경에서는 실제 imdecode 도 검증.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("SARVIS_SKIP_CV2_PRELOAD", "1")

import vision as vision_mod  # noqa: E402
from vision import FaceRegistry, WebVision, _safe_filename  # noqa: E402


class SafeFilenameTests(unittest.TestCase):
    def test_keeps_alnum_and_korean(self):
        self.assertEqual(_safe_filename("alice123"), "alice123")
        self.assertEqual(_safe_filename("앨리스"), "앨리스")
        self.assertEqual(_safe_filename("kim_철수"), "kim_철수")

    def test_replaces_unsafe_chars(self):
        self.assertEqual(_safe_filename("a/b\\c"), "a_b_c")
        self.assertEqual(_safe_filename("name with space!"), "name_with_space_")

    def test_strips_outer_whitespace(self):
        self.assertEqual(_safe_filename("  alice  "), "alice")

    def test_truncates_long(self):
        out = _safe_filename("a" * 100)
        self.assertEqual(len(out), 40)

    def test_empty_returns_unknown(self):
        self.assertEqual(_safe_filename(""), "unknown")
        self.assertEqual(_safe_filename("   "), "unknown")


class FaceRegistryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "faces"
        self.reg = FaceRegistry(str(self.dir))

    def tearDown(self):
        self._tmp.cleanup()

    def test_initial_state_is_empty(self):
        self.assertTrue(self.reg.is_empty())
        self.assertEqual(self.reg.list_people(), [])
        self.assertEqual(self.reg.get_references(), [])

    def test_register_creates_jpeg_and_index(self):
        self.reg.register("앨리스", b"\xff\xd8\xff\xe0FAKE")
        files = list(self.dir.glob("*.jpg"))
        self.assertEqual(len(files), 1)
        self.assertFalse(self.reg.is_empty())
        self.assertIn("앨리스", self.reg.list_people())
        # _index.json 도 저장되어야
        self.assertTrue((self.dir / "_index.json").exists())

    def test_register_validation(self):
        with self.assertRaises(ValueError):
            self.reg.register("", b"data")
        with self.assertRaises(ValueError):
            self.reg.register("alice", b"")

    def test_register_overwrites_same_name(self):
        self.reg.register("alice", b"v1")
        self.reg.register("alice", b"v2")
        files = list(self.dir.glob("*.jpg"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_bytes(), b"v2")

    def test_delete_existing(self):
        self.reg.register("bob", b"data")
        self.assertTrue(self.reg.delete("bob"))
        self.assertTrue(self.reg.is_empty())

    def test_delete_missing_returns_false(self):
        self.assertFalse(self.reg.delete("ghost"))

    def test_get_references_returns_base64(self):
        self.reg.register("carol", b"\x00\x01\x02")
        refs = self.reg.get_references()
        self.assertEqual(len(refs), 1)
        name, b64 = refs[0]
        self.assertEqual(name, "carol")
        import base64
        self.assertEqual(base64.standard_b64decode(b64), b"\x00\x01\x02")

    def test_index_persists_across_instances(self):
        self.reg.register("앨리스", b"x")
        # 새 인스턴스로 디스크에서 다시 로드
        reg2 = FaceRegistry(str(self.dir))
        self.assertIn("앨리스", reg2.list_people())

    def test_corrupt_index_resets(self):
        (self.dir).mkdir(parents=True, exist_ok=True)
        (self.dir / "_index.json").write_text("{not json", encoding="utf-8")
        reg2 = FaceRegistry(str(self.dir))
        self.assertEqual(reg2._index, {})


class WebVisionWithoutCv2Tests(unittest.TestCase):
    """cv2 미설치 환경에서도 모듈이 안전하게 None/False 를 반환해야."""

    def test_push_jpeg_returns_false_without_cv2(self):
        wv = WebVision()
        with patch.object(vision_mod, "_ensure_cv2", return_value=False):
            self.assertFalse(wv.push_jpeg(b"\xff\xd8FAKE"))

    def test_read_returns_none_when_no_frame(self):
        wv = WebVision()
        # 한 번도 push 한 적 없음 → None
        self.assertIsNone(wv.read())

    def test_read_returns_none_when_stale(self):
        import time as _t
        wv = WebVision()
        # 직접 frame 을 5초 전으로 설정
        wv._frame = object()
        wv._frame_ts = _t.time() - 10.0
        self.assertIsNone(wv.read())

    def test_get_frame_size_initial_zero(self):
        wv = WebVision()
        self.assertEqual(wv.get_frame_size(), (0, 0))

    def test_release_clears_state(self):
        wv = WebVision()
        wv._frame = object()
        wv.face_boxes = [(0, 1, 2, 3)]
        wv.release()
        self.assertIsNone(wv._frame)
        self.assertEqual(wv.face_boxes, [])

    def test_update_face_recognition_is_noop(self):
        # WebVision.update_face_recognition 은 의도적으로 no-op
        wv = WebVision()
        # 에러 없이 종료해야 함
        self.assertIsNone(wv.update_face_recognition(object()))

    def test_crop_largest_face_returns_none_without_cv2(self):
        wv = WebVision()
        with patch.object(vision_mod, "_ensure_cv2", return_value=False):
            self.assertIsNone(wv.crop_largest_face_jpeg())

    def test_crop_largest_face_returns_none_when_no_frame(self):
        wv = WebVision()
        # cv2 가 사용 가능해도 frame 이 없으면 None
        with patch.object(vision_mod, "_ensure_cv2", return_value=True):
            self.assertIsNone(wv.crop_largest_face_jpeg())


@unittest.skipUnless(vision_mod._ensure_cv2(), "cv2 미설치 환경에서는 건너뜀")
class WebVisionWithCv2Tests(unittest.TestCase):
    """cv2 가 사용 가능하면 실제 imdecode 동작도 검증."""

    def test_push_jpeg_invalid_returns_false(self):
        wv = WebVision()
        # 임의 바이트는 imdecode 가 None 반환 → False
        self.assertFalse(wv.push_jpeg(b"\x00\x01garbage"))

    def test_push_then_read_real_jpeg(self):
        import numpy as np
        cv2 = vision_mod.cv2
        # 작은 검정 이미지를 JPEG 인코드
        img = np.zeros((20, 30, 3), dtype=np.uint8)
        ok, buf = cv2.imencode(".jpg", img)
        self.assertTrue(ok)
        wv = WebVision()
        # 첫 push: 1초 throttle 으로 인해 face 감지는 첫 호출에서 발생
        wv.push_jpeg(buf.tobytes())
        frame = wv.read()
        self.assertIsNotNone(frame)
        self.assertEqual(wv.get_frame_size(), (30, 20))


if __name__ == "__main__":
    unittest.main()
