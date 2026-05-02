"""얼굴 등록 — 처음 사용 전에 실행해서 본인 얼굴 5장 캡처"""
import sys

import cv2
import numpy as np

try:
    import face_recognition
except ImportError:
    print("face_recognition 미설치. 다음 명령어로 설치:")
    print("  pip install face_recognition")
    sys.exit(1)

from .config import cfg
from .vision import FaceMemory


def main():
    print("=" * 50)
    print("사비스 얼굴 등록")
    print("=" * 50)
    name = input("이름을 입력하세요: ").strip()
    if not name:
        print("취소됨")
        return

    print(f"\n[{name}] 얼굴을 등록합니다.")
    print("창이 뜨면 카메라를 보세요.")
    print("  SPACE: 캡처 (5장 권장, 다양한 각도)")
    print("  Q: 종료\n")

    cap = cv2.VideoCapture(cfg.camera_index)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.camera_width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.camera_height)

    encodings = []

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                continue
            frame = cv2.flip(frame, 1)

            # 미리보기로 얼굴 박스 표시
            small = cv2.resize(frame, (0, 0), fx=0.5, fy=0.5)
            rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
            locs = face_recognition.face_locations(rgb)

            for (t, r, b, l) in locs:
                cv2.rectangle(frame, (l * 2, t * 2), (r * 2, b * 2), (255, 217, 0), 2)

            cv2.putText(
                frame, f"Captured: {len(encodings)}/5  -  {name}",
                (16, 36), cv2.FONT_HERSHEY_DUPLEX, 0.7, (255, 217, 0), 1, cv2.LINE_AA,
            )
            cv2.putText(
                frame, "SPACE = capture   Q = finish",
                (16, frame.shape[0] - 20), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                (245, 233, 207), 1, cv2.LINE_AA,
            )

            cv2.imshow("Face Registration", frame)
            key = cv2.waitKey(1) & 0xFF

            if key == ord(" "):
                if not locs:
                    print("얼굴이 감지되지 않습니다.")
                    continue
                # 큰 프레임에서 정확하게 인코딩
                big_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                big_locs = face_recognition.face_locations(big_rgb)
                if not big_locs:
                    continue
                encs = face_recognition.face_encodings(big_rgb, big_locs)
                if encs:
                    encodings.append(encs[0])
                    print(f"  ✓ 캡처 {len(encodings)}/5")
                    if len(encodings) >= 5:
                        break
            elif key in (ord("q"), 27):  # Q or ESC
                break
    finally:
        cap.release()
        cv2.destroyAllWindows()

    if not encodings:
        print("\n등록 취소: 캡처 없음")
        return

    memory = FaceMemory()
    avg = np.mean(encodings, axis=0)
    memory.add(name, avg)
    print(f"\n✓ {name}님 얼굴 등록 완료 ({len(encodings)}장 평균)")
    print(f"  저장 위치: {memory.path}")


if __name__ == "__main__":
    main()
