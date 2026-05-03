"""Diagnostician 에이전트 (기획서 §4.2) — Stage S2 (L1).

책임:
- ha_issues(status='open') 를 입력으로, 5 Whys + 베이지안 가설 랭킹 휴리스틱
  으로 근본원인 + 가설 후보를 산출.
- (옵션) LLM 가설 보강.
- 결과를 ha_diagnoses 에 영속, ha_issues.status → 'diagnosed', Reporter 에
  메시지 emit.

여전히 변경 적용 없음 (L1 = 진단까지). 어떤 코드/안전 프롬프트도 수정 불가.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .base import HAAgent
from .safety import ensure_running, mask_pii


# 카테고리 → (가설 후보 리스트, 권장 다음 액션) 룰 트리.
# 각 가설은 prior(휴리스틱 사전 확률), explanation, suggested_check 를 포함.
# 카테고리 → 5 Whys 인과 사슬 (가장 빈번한 원인 흐름).
# 각 단계는 "왜?" 에 대한 답으로, 이전 단계의 원인을 한 번 더 파고듦.
_FIVE_WHYS: Dict[str, List[str]] = {
    "spike": [
        "Why 1: 단기 에러율이 임계(10%)를 초과했다.",
        "Why 2: 특정 도구·백엔드 경로의 실패 비율이 급증했다.",
        "Why 3: 외부 의존(API/모델/네트워크) 응답이 변동했다.",
        "Why 4: 호출 인자·계약·쿼터 중 하나가 사일런트로 변경되었다.",
        "Why 5: 변경 감지·페일오버 정책이 해당 경로를 커버하지 못했다.",
    ],
    "drift": [
        "Why 1: 7일 만족도가 28일 베이스라인 대비 현저히 하락했다.",
        "Why 2: 동일 명령에 대한 응답 톤·정확도·길이 분포가 바뀌었다.",
        "Why 3: 모델/프롬프트/지식 중 하나의 입력 분포가 이동했다.",
        "Why 4: 최근 사이클의 변경(백엔드 fallback / 시스템 프롬프트 / 메모리 적재)과 시점이 겹친다.",
        "Why 5: 회귀 시드가 해당 변화 패턴을 사전에 잡아내지 못했다.",
    ],
    "anomaly": [
        "Why 1: 부정 피드백이 짧은 윈도우에 군집했다.",
        "Why 2: 그 군집은 특정 카테고리/도구/주제에 집중된다.",
        "Why 3: 그 영역의 응답 후처리·검증 단계가 비대칭으로 약하다.",
        "Why 4: 시나리오/엣지 케이스 커버리지에 사각이 있다.",
        "Why 5: Validator 사전 집합이 해당 사각을 시드하지 못했다.",
    ],
    "cost": [
        "Why 1: 평균/꼬리 지연이 임계(8s)를 넘었다.",
        "Why 2: 단계별 시간 분포에서 특정 도구·LLM 호출이 비대해졌다.",
        "Why 3: 더 큰 모델/더 긴 체인/더 잦은 외부 호출 중 하나가 도입됐다.",
        "Why 4: 비용 대비 품질 가드(예산/배지)가 적용되지 않았다.",
        "Why 5: 비용 회귀 알람이 사전 차단하지 못했다.",
    ],
    "underutilization": [
        "Why 1: 일정 윈도우 동안 트래픽이 비정상적으로 적다.",
        "Why 2: 사용자가 평소 쓰던 흐름을 시작하지 않았다.",
        "Why 3: 진입 동기(발견성/응답 품질/접근성) 중 하나가 약화됐다.",
        "Why 4: 최근 UI/권한/응답 변화 중 하나가 회피를 유발했다.",
        "Why 5: 침묵 신호를 조기 경보로 연결할 자동 알림이 없었다.",
    ],
}
_FIVE_WHYS_FALLBACK = [
    "Why 1: 신호가 잡혔으나 카테고리가 정의되지 않았다.",
    "Why 2: Observer 휴리스틱이 새 패턴을 분류하지 못한다.",
    "Why 3: 룰셋이 최신 사용 양상을 반영하지 못한다.",
    "Why 4: 신규 패턴 추가 절차가 정해져 있지 않다.",
    "Why 5: 분기별 룰 리뷰 사이클이 부재하다.",
]

_RULES: Dict[str, Dict[str, Any]] = {
    "spike": {
        "hypotheses": [
            {"name": "외부 API 일시 장애", "prior": 0.35,
             "check": "백엔드/모델 라우팅 로그 확인"},
            {"name": "도구 호출 입력 형식 변경", "prior": 0.25,
             "check": "최근 tool_use 인자 diff"},
            {"name": "사용자 입력 분포 변화 (신규 도메인)", "prior": 0.20,
             "check": "오류 명령 텍스트 토픽 분석"},
            {"name": "메모리/디스크 자원 한계", "prior": 0.10,
             "check": "df -h, sqlite WAL 크기"},
            {"name": "안전 가드 (필터) 차단", "prior": 0.10,
             "check": "안전 프롬프트 trip 로그"},
        ],
        "recommended_action":
            "동일 윈도우의 오류 명령 5건 수동 재현 → 백엔드 fallback 검증",
    },
    "drift": {
        "hypotheses": [
            {"name": "모델 백엔드 자동 전환 (compare/fallback)", "prior": 0.30,
             "check": "최근 backend_changed 로그"},
            {"name": "프롬프트/페르소나 변경", "prior": 0.25,
             "check": "system prompt diff"},
            {"name": "지식/메모리 노이즈 누적", "prior": 0.20,
             "check": "knowledge confidence 분포"},
            {"name": "사용자 기대치 상승 (신기능 도입)", "prior": 0.15,
             "check": "최근 사이클 기능 매트릭스"},
            {"name": "샘플 편향 (소수 부정 사용자)", "prior": 0.10,
             "check": "rated user 분포"},
        ],
        "recommended_action":
            "👎 명령 + 코멘트 N=10 수동 검토 후 회귀 시드로 추가",
    },
    "anomaly": {  # negative_cluster + 일반 이상치
        "hypotheses": [
            {"name": "특정 카테고리 응답 톤 부적합", "prior": 0.30,
             "check": "👎 명령 카테고리 분포"},
            {"name": "도구 결과 후처리 누락", "prior": 0.25,
             "check": "tool result → final answer 변환 로그"},
            {"name": "STT 오인식으로 인한 오해", "prior": 0.20,
             "check": "원본 STT vs 정정 텍스트"},
            {"name": "긴 응답 끝에서 사실 왜곡", "prior": 0.15,
             "check": "응답 길이 vs rating 상관"},
            {"name": "사용자 옵트아웃되지 않은 PII 우려", "prior": 0.10,
             "check": "코멘트에서 'privacy/개인정보' 키워드"},
        ],
        "recommended_action":
            "샘플 5건의 입력/응답 쌍을 Validator 사전 시나리오로 등록",
    },
    "cost": {  # latency 등 비용 카드
        "hypotheses": [
            {"name": "도구 체인 길이 증가 (불필요 호출)", "prior": 0.35,
             "check": "tool_use 카운트 분포"},
            {"name": "LLM 모델 변경 (대형 모델 사용)", "prior": 0.25,
             "check": "최근 switch_model 로그"},
            {"name": "네트워크 지연 (외부 API)", "prior": 0.20,
             "check": "분 단위 latency 분포"},
            {"name": "STT/TTS 오디오 처리 폭주", "prior": 0.10,
             "check": "audio 처리 시간 평균"},
            {"name": "메모리/검색 인덱스 비대화", "prior": 0.10,
             "check": "chromadb 크기 추이"},
        ],
        "recommended_action":
            "p95 latency 시점 명령 3건 trace_view 로 단계별 시간 분해",
    },
    "underutilization": {  # silence
        "hypotheses": [
            {"name": "사용자 부재 (실제 비활성)", "prior": 0.40,
             "check": "주인 인증 세션 로그"},
            {"name": "주요 기능 발견성 저하", "prior": 0.25,
             "check": "최근 UI 변경 + 진입 메뉴 위치"},
            {"name": "음성 입력 인식률 저하로 사용자 회피", "prior": 0.15,
             "check": "STT 오류율"},
            {"name": "응답 품질 불만 (과거 부정 피드백 누적)", "prior": 0.10,
             "check": "직전 사이클 만족도 추세"},
            {"name": "환경 문제 (마이크/카메라 권한)", "prior": 0.10,
             "check": "permission denied 로그"},
        ],
        "recommended_action":
            "다음 1주간 일일 침묵 카드 추세 모니터링 — 추세 지속 시 운영자 알림",
    },
}

# 폴백 룰 — 알 수 없는 카테고리.
_FALLBACK = {
    "hypotheses": [
        {"name": "분류 누락 — 미정의 신호", "prior": 1.0,
         "check": "Observer 휴리스틱 카테고리 보강 필요"},
    ],
    "recommended_action": "Observer 카테고리 정의에 새 분류 추가 검토",
}


@dataclass
class DiagnosisResult:
    diagnosis_id: str
    issue_id: str
    hypotheses: List[Dict[str, Any]]   # 정렬됨 (확률 내림차순)
    root_cause: Optional[str]
    confidence: float
    recommended_action: Optional[str]
    five_whys: List[str] = field(default_factory=list)  # 5단계 인과 사슬
    method: str = "heuristic"

    def to_payload(self) -> Dict[str, Any]:
        return {
            "diagnosis_id": self.diagnosis_id,
            "issue_id": self.issue_id,
            "hypotheses": self.hypotheses,
            "root_cause": self.root_cause,
            "confidence": self.confidence,
            "recommended_action": self.recommended_action,
            "five_whys": self.five_whys,
            "method": self.method,
            "requires_human": True,  # L1 — 항상 사람 검토
        }


class Diagnostician(HAAgent):
    name = "Diagnostician"
    read_scope = {"ha_issues", "ha_messages", "commands", "command_feedback"}
    # L1: 진단 결과만 영속. 안전 프롬프트/코드 수정 불가.
    write_scope = {"ha_messages", "ha_diagnoses", "ha_issue_status"}

    def __init__(self, memory=None, brain=None) -> None:
        super().__init__(memory=memory)
        self.brain = brain  # 옵션 LLM

    def _new_id(self) -> str:
        return f"DIAG-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"

    # ── 단일 issue 진단 ──────────────────────────────────────────
    def diagnose(self, issue: Dict[str, Any]) -> DiagnosisResult:
        ensure_running()
        category = issue.get("category", "")
        rule = _RULES.get(category, _FALLBACK)
        # 베이지안 풍 사후 확률: prior 를 그대로 정규화 (L1 단계 단순화).
        # 추후 트레이스 evidence 로 likelihood 곱셈 도입 예정.
        hyps = [dict(h) for h in rule["hypotheses"]]
        total = sum(h["prior"] for h in hyps) or 1.0
        for h in hyps:
            h["posterior"] = round(h["prior"] / total, 3)
        hyps.sort(key=lambda h: h["posterior"], reverse=True)
        root = hyps[0]["name"] if hyps else None
        # 신뢰도 = 1위 가설의 사후 확률 × issue 자체 신뢰도 (보수적).
        issue_conf = float(issue.get("confidence") or 0.5)
        diag_conf = round(min(1.0, hyps[0]["posterior"] * 0.8 + issue_conf * 0.2), 3) \
            if hyps else 0.3
        rec = rule.get("recommended_action")

        # 옵션 LLM 보강 — 실패해도 휴리스틱 결과는 유지.
        method = "heuristic"
        if self.brain is not None:
            try:
                extra = self._llm_augment(issue, hyps[:3])
                if extra:
                    hyps.insert(0, extra)
                    root = extra["name"]
                    method = "heuristic+llm"
            except Exception as ex:
                print(f"[Diagnostician] LLM 보강 실패: {ex!r}")

        result = DiagnosisResult(
            diagnosis_id=self._new_id(),
            issue_id=issue.get("issue_id", "UNKNOWN"),
            hypotheses=hyps,
            root_cause=root,
            confidence=diag_conf,
            recommended_action=rec,
            five_whys=list(_FIVE_WHYS.get(category, _FIVE_WHYS_FALLBACK)),
            method=method,
        )
        # 영속 + emit + 상태 갱신
        if self.memory is not None:
            try:
                self.memory.ha_diagnosis_insert(
                    diagnosis_id=result.diagnosis_id,
                    issue_id=result.issue_id,
                    hypotheses=result.hypotheses,
                    root_cause=result.root_cause,
                    confidence=result.confidence,
                    recommended_action=result.recommended_action,
                    five_whys=result.five_whys,
                    method=result.method,
                )
                self.memory.ha_issue_set_status(result.issue_id, "diagnosed")
                self.emit("Reporter", result.to_payload())
            except Exception as ex:
                print(f"[Diagnostician] 영속 실패 {result.diagnosis_id}: {ex!r}")
        return result

    def run_pending(self, limit: int = 20) -> List[DiagnosisResult]:
        ensure_running()
        if self.memory is None:
            return []
        opens = self.memory.ha_issues_open(limit=limit)
        out: List[DiagnosisResult] = []
        for issue in opens:
            try:
                out.append(self.diagnose(issue))
            except Exception as ex:
                print(f"[Diagnostician] 진단 실패 {issue.get('issue_id')}: {ex!r}")
        return out

    # ── (옵션) LLM 가설 보강 ─────────────────────────────────────
    def _llm_augment(
        self, issue: Dict[str, Any], top3: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        if self.brain is None:
            return None
        # PII 마스킹 후 송신
        narr = mask_pii(str(issue.get("narrative") or ""))
        sig = mask_pii(str(issue.get("signal") or ""))
        prompt = (
            "다음 HA 이슈에 대한 새 가설 1개를 제안. 기존 휴리스틱 상위 3개와 "
            "겹치지 않게. JSON 한 줄: {\"name\": str, \"prior\": float (0~1), "
            "\"check\": str}. 가설 없으면 {}.\n\n"
            f"카테고리: {issue.get('category')}\n신호: {sig}\n요약: {narr}\n"
            f"기존 상위 3: {[h['name'] for h in top3]}"
        )
        try:
            text = self.brain.think_once_text(prompt)  # 동기 단발 (Brain 미보유 시 AttributeError)
        except AttributeError:
            return None
        except Exception:
            return None
        import json as _json
        try:
            s = text.find("{"); e = text.rfind("}")
            if s < 0 or e <= s:
                return None
            obj = _json.loads(text[s:e+1])
            name = obj.get("name")
            if not name:
                return None
            prior = float(obj.get("prior", 0.1) or 0.1)
            prior = max(0.0, min(1.0, prior))
            return {
                "name": str(name)[:200],
                "prior": prior,
                "posterior": prior,
                "check": str(obj.get("check") or "")[:200],
                "source": "llm",
            }
        except (ValueError, _json.JSONDecodeError):
            return None
