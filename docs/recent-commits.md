# 최근 공개 커밋 카드

`scripts/generate_recent_commits.py`는 GitHub Commit Search API에서 프로필 소유자가 작성한 공개 커밋을 찾고, SVG 카드와 README 마커 영역을 갱신합니다. GitHub Actions의 `GITHUB_TOKEN`만 사용하며 별도 서버나 개인 액세스 토큰은 필요하지 않습니다.

## 수집 범위

- `author:{username} is:public merge:false` 검색 뒤 응답의 저장소 공개 여부와 연결된 작성자 로그인을 다시 확인합니다.
- 봇, 중복 SHA, 병합 커밋, 프로필 저장소, 설정된 자동 메시지를 제외합니다.
- 필터를 통과한 커밋을 작성 시간 기준 최신순으로 정렬해 최대 3개 표시합니다.
- 프로필 README에서는 GitHub 표 테두리가 생기지 않도록 `align="left/right"` 이미지 플로트로 대표 프로젝트를 왼쪽, 최근 커밋을 오른쪽에 배치합니다.
- 이 맞춤 레이아웃에서는 `RECENT_COMMIT_1~3_START/END` 슬롯만 갱신하므로 대표 프로젝트 마크업은 자동 생성 과정에서 보존됩니다. 슬롯이 없는 README에서는 기존 `RECENT_COMMITS_START/END` 범위를 사용합니다.
- 최근 7일은 `Asia/Seoul` 기준 오늘을 포함한 7개 달력일입니다.
- 연속 활동이 조회 구간의 시작일까지 이어지면 이전 구간을 추가 조회해 첫 비활동일까지 계산합니다.
- Commit Search는 기본 브랜치에 포함된 커밋만 검색합니다. 아직 병합되지 않은 기능 브랜치 커밋과 GitHub 검색 인덱스에 아직 반영되지 않은 커밋은 잠시 보이지 않을 수 있습니다.
- GitHub 검색은 한 쿼리에서 최대 1,000개 결과를 제공합니다. 생성기는 기간을 더 작은 구간으로 나눠 수집하며, 하루 범위도 완전하게 가져올 수 없거나 `incomplete_results`가 반환되면 기존 카드 보존을 위해 실패로 종료합니다.

API나 네트워크 오류가 발생하면 SVG와 README를 쓰기 전에 중단합니다. 정상적인 빈 검색 결과일 때만 빈 상태 카드를 만듭니다.

## 로컬 미리보기

```bash
python scripts/generate_recent_commits.py --fixture
```

fixture 결과는 `preview/recent-commits/`에 생성되며 실제 README와 `assets/recent-commits/`는 건드리지 않습니다. 실제 데이터 갱신은 `GITHUB_TOKEN` 또는 `GH_TOKEN`을 환경 변수로 제공한 뒤 옵션 없이 실행합니다.
