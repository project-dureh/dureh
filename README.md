# DUREH harness

**DUREH** = **D**esk-led, **U**nbundled, **R**eversibility-aware, **E**vidence-accumulating **H**arness.

이 저장소는 **실행 가능한 DUREH harness의 코드 저장소**입니다. 현재는 구현 착수 전 단계이며 설치·dispatch·기록·복구가 작동한다고 주장하지 않습니다. 설계 문서는 [dureh-whitepaper](https://github.com/dvm-shlee/dureh-whitepaper)로 옮겼습니다. 기존 백서 파일은 이 저장소의 Git 이력에 남습니다.

## 설계 문서

- [영문 백서](https://github.com/dvm-shlee/dureh-whitepaper/blob/main/docs/white-paper.en.md) — 에이전트가 먼저 읽는 정본
- [한국어 백서](https://github.com/dvm-shlee/dureh-whitepaper/blob/main/docs/white-paper.ko.md) — 사람이 읽는 대응판
- [영문 기술 명세](https://github.com/dvm-shlee/dureh-whitepaper/blob/main/docs/technical-spec.en.md) — 구현 계약과 미정 사항
- [시험 워크플로 영문판](https://github.com/dvm-shlee/dureh-whitepaper/blob/main/docs/testing-workflow.en.md) · [한국어판](https://github.com/dvm-shlee/dureh-whitepaper/blob/main/docs/testing-workflow.ko.md)
- [설계 기준 버전](DESIGN-BASELINE.md) — 구현 작업에서 고정해 읽을 문서 커밋

내부 운영 근거는 접근이 제한된 `dureh-internal-evidence`에 둡니다. 현장 기록·사람의 기억·연구 데이터는 이 공개 코드 저장소에 넣지 않습니다.

## 구현 시작

새 에이전트 작업은 [AGENTS.md](AGENTS.md)와 [START-HERE.md](START-HERE.md)를 읽고 Phase 0의 미정 사항과 완료 기준을 정리한 뒤 Phase 1의 작고 검증 가능한 기능부터 구현합니다. 구현·검증 여부를 구분해 보고해야 합니다.

시험은 빠른 편집 피드백과 결과 반영 게이트를 구분합니다. 변경 중에는 관련 시험을, Gate 2 전에는 영향을 받은 구성 요소의 전체 시험과 필요한 안전·통합 검사를 실행합니다. 속도 개선은 먼저 wall/CPU/Slurm 대기 시간을 측정하고, 시험의 판별력과 격리를 유지한 채 진행합니다. 외부 [fast-tests 사례](https://github.com/hunkim/ai-is-fast-your-tests-are-slow)의 속도 수치는 DUREH의 목표값이 아닙니다.
