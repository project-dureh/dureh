# DUREH project blueprint

**DUREH** = **D**esk-led, **U**nbundled, **R**eversibility-aware, **E**vidence-accumulating **H**arness.

이 저장소는 DUREH를 구현하기 위한 **설계 출발점**입니다. 현재 들어 있는 것은 백서와 기술 명세이며, 실행 가능한 harness는 아직 없습니다.

## 문서

- [대외용 백서](docs/DUREH-public-white-paper.md): 문제, 핵심 설계, 평가·보안 요구사항
- [기술 명세](docs/DUREH-technical-specification.md): 구성 요소, 레코드, 설치·배포, 단계별 완료 기준과 미해결 사항
- [변경 기록](docs/DUREH-change-log.md): 원문에서 바뀐 내용
- [에이전트 시작 지침](AGENTS.md): 이 저장소에서 작업할 때 지킬 범위와 검증 방식
- [첫 작업 요청문](START-HERE.md): 저장소 주소와 함께 에이전트에게 전달할 문장
- 내부판(결정 기록·심사 기록·측정 원본 포인터)은 비공개 저장소 `dureh-internal-evidence`에 있습니다. 이 저장소는 공개판만 담습니다.

## 시작 방법

1. 이 저장소를 접근 가능한 Git 서버에 push합니다.
2. 새 에이전트 작업에 저장소 URL과 [`START-HERE.md`](START-HERE.md)의 요청문을 전달합니다.
3. 에이전트는 저장소를 clone하고 `AGENTS.md` 및 두 설계 문서를 읽은 뒤 **Phase 0**부터 시작합니다.
4. 각 단계의 산출물과 테스트 결과를 Git commit 또는 pull request로 검토합니다. 단계가 끝났다는 주장은 실제 실행 결과가 있을 때만 합니다.

저장소 URL은 문서를 찾는 방법일 뿐, 그 자체로 구현 지시가 아닙니다. `AGENTS.md`와 시작 요청문이 작업 범위를 알려주며, 기술 명세의 미정 사항은 구현 전에 결정해야 합니다. Claude Code 외 런타임, RIA 복구, OOD/VM 배포, 보안 통제, 성능 개선은 현재 검증되지 않았습니다.
