# Claude 전달용 구현 프롬프트 — AQE 신규 구축

> 이 파일은 2026-11-11 세션에서 사용자가 대화에 붙여 넣은 구현 프롬프트를 프로젝트 폴더에 보존한 사본이다(원본 파일이 폴더에 제공되지 않았음). 원본과 바이트 단위로 같다는 보장은 없다. 차이는 `docs/SPEC_PROVENANCE.md` 참고.

사용 방법: 빈 프로젝트 폴더에 AQE_TradingAgents_Blueprint_KO.md와 이 파일을 함께 넣고, 아래 프롬프트를 Claude Code 또는 파일 읽기·코드 실행이 가능한 Claude 환경에 전달한다. 과거 AQE 파일은 필요하지 않다. 기본 설계의 원문까지 검토하려면 기존 AQE_TradingAgents_Integration_Prompt_KO.md를 선택적으로 함께 둔다.

## 프롬프트 시작

설계도 파일 SHA-256: 1abd54b663dbc362ea547f42cd7053b4f29c3999e1270fb4d45341338030ae67. 첨부한 설계도와 다른 버전이면 먼저 그 차이를 기록하라.

당신은 Python 시계열 머신러닝, 암호화폐 시장 데이터, 검증 가능한 LLM 시스템을 구현하는 엔지니어다. 먼저 현재 폴더의 AQE_TradingAgents_Blueprint_KO.md 버전 1.0 전체를 읽고, 그 설계도를 기준으로 실제 코드를 작성하라. 설계를 다시 제안하는 것으로 끝내지 말고 실행 가능한 작은 경로부터 단계적으로 완성하라.

이 프로젝트는 모든 기존 AQE 파일을 지운 상태에서 새로 시작한다. 기존 코드·모델·DB·S3 데이터·AWS 인프라를 전제하지 말라. 작업 폴더에 다른 파일이 발견되면 상태를 확인하고 보존하라. 디렉터리 삭제나 강제 초기화는 하지 말라.

### 1. 구현할 단일 시스템

핵심 구성은 다음 다섯 가지다.
1. LightGBM: 가격·거래량 및 확보된 수치 피처를 학습하는 기준 모델.
2. 작은 causal TCN: 같은 목표 기간의 시퀀스 패턴을 별도 학습하는 모델.
3. TradingAgents 기반 근거 분석: 뉴스·거시 일정·시장 반응·검증된 흐름 요약의 근거와 반대 시나리오를 분석하는 컨텍스트 계층.
4. 시간 순서 메타 모델: 앞의 수치 예측과 구조화 컨텍스트를 미래 누출 없이 결합·보정하는 계층.
5. 독립 위험관리 코드: 데이터·모델 준비도·비용·한도를 검증하고 보류 여부를 결정하는 코드.

TradingAgents가 '근거 기반 LLM' 역할을 맡는다. 별도의 완성형 LLM 봇과 TradingAgents 봇을 두 개 만드는 것이 아니다. 단순 근거 기반 LLM은 동일 계약을 사용하는 대조 backend로만 둔다. 수치 모델·메타 모델은 실제 데이터 학습이 필요하며, 프롬프트 작성만으로 학습됐다고 간주하지 말라.

초기 용도는 연구·방향성 분석·사람의 수동 검토 지원이다. 롱·숏을 모두 분석하되 주문 클라이언트, 자동 주문 경로, 실거래 모드, 자동 레버리지는 만들지 말라. 모든 출력의 approval_required=true, execution_enabled=false를 유지하라. 이번 작업에서 유료 API 호출·AWS 자원 생성·실제 배포를 임의로 실행하지 말라. 키와 실행 권한이 명시적으로 제공된 연결만 실제 시험하라.

### 2. 공식 저장소를 실제로 사용하라

설계도 B02의 출처를 구현에 연결하고, URL만 나열한 것으로 통합 완료라고 하지 말라.

| 구성 | 가져올 공식 소스 | 구현 방식 |
|---|---|---|
| LightGBM | https://github.com/lightgbm-org/LightGBM ; 기존 https://github.com/microsoft/LightGBM | 패키지 사용, 암호화폐 데이터셋·학습 래퍼는 새로 구현 |
| TCN | https://github.com/locuslab/TCN ; https://github.com/pytorch/pytorch | 저자 인과적 구조를 참고해 작은 PyTorch 모델·훈련 루프 구현 |
| TradingAgents | https://github.com/TauricResearch/TradingAgents | 고정 SHA의 실제 그래프를 adapter/worker로 연결 |
| 메타 모델 | https://github.com/scikit-learn/scikit-learn | 로지스틱 회귀 등 사용, 전진 OOS 생성과 결합 파이프라인 직접 구현 |
| 위험관리 | AQE 자체 모듈 | 학습 모델이나 LLM 대신 결정적 규칙 작성 |

공식 코드가 공개되어 있어도 코인 예측 가중치가 준비된 것은 아니다. 코드 출처·패키지 버전·복사 파일·라이선스·변경 내역을 기록하라. LightGBM 등의 전체 저장소를 무작정 복사해 합치지 말라. 실제 설치 검증 후 의존성을 잠그고, 설치하지 못한 버전을 검증 완료로 쓰지 말라.

TradingAgents 기준 SHA는 9fa1196fffbcfd9de5146d2dd2869df84ab99838이며 확인된 패키지 버전은 0.7.1이다. 이 소스를 가져와 다음 API와 내부 경로를 직접 확인하라.

```python
from tradingagents.graph.trading_graph import TradingAgentsGraph
# 확인 대상: propagate(company_name, trade_date, asset_type="stock", portfolio=None)
# crypto 호출 시 asset_type="crypto" 명시
```

다른 SHA로 변경할 필요가 있으면 자동으로 최신판을 택하지 말고 차이·변경 사유·계약 시험을 docs/UPSTREAM_PATCHES.md에 남겨라. 존재하지 않는 API·메모리 비활성화 옵션·구조화 응답을 상상해서 쓰지 말라. 실제 import와 호출 경로가 없는 fake adapter는 TradingAgents 연결 완료가 아니다.

### 3. 설계도와 코드의 연결

설계도의 B01~B15, 요구 R01~R12, 단계 M0~M6, 시험 T01~T14 식별자를 유지하라. 모듈 소유권은 B03, 입력·출력은 B06, 학습은 B07/B09, 컨텍스트는 B08, 위험은 B10을 따른다.

기본 디렉터리는 src/aqe/{contracts,config,ingestion,storage,features,datasets,models,context,ensemble,risk,evaluation,reporting,cli}와 configs/, tests/, docs/, deploy/다. 동작하는 수직 경로에 필요한 디렉터리부터 생성하라. 비어 있는 수십 개 파일을 만들고 구현으로 보고하지 말라.

Python 3.12를 호환성 후보로 현재 OS·Python·디스크·메모리·Docker/WSL·DB 상태를 먼저 진단하라. 과거 대화의 PC 상태를 현재 상태로 단정하지 말라. core와 TradingAgents 의존성이 충돌하면 worker 환경을 분리하고 JSON 계약을 유지하라. 모듈형 단일 프로젝트 + 별도 느린 context worker가 기본이다. 초기부터 불필요한 분산 시스템을 만들지 말라.

기술적 장애로 명세 수정이 필요하면 기존 요구를 보존하는 최소 변경을 ADR에 기록하고 문서·코드·시험을 일치시켜라. 주문 권한·유료 자원·데이터 삭제처럼 범위를 넓히는 변경은 수행하지 말라. 되돌릴 수 있는 통상 구현 결정마다 사용자 확인을 요구하지 말라.

### 4. 데이터와 시각 제한

- B04의 자산 키·UTC·`available_at <= as_of` 조건을 모든 조회에 적용하라. event_time, published_at, received_at, available_at, as_of, generated_at, valid_until을 혼용하지 말라.
- 최초 자산은 BTCUSDT USDⓈ-M 무기한 선물이다. 5분 봉·5분 갱신과 1시간/24시간 예측 기간을 구분하고, 기간별 타깃·모델·메타·보정·보고를 분리하라. ETHUSDT는 첫 수직 경로 이후 확장하라.
- 실제 backfill 가능한 ohlcv_v0부터 구현하라. flow_v1의 L2·체결·OI/funding과 context_v1의 시점별 근거는 확보 여부를 따로 관리하라. 없는 L2/CVD를 0으로 채워 그 피처를 학습한 모델처럼 표시하지 말라.
- 캔들 이력 다운로드를 실제 과거 수신 기록이라고 하지 말라. LIVE_CAPTURE, VERIFIED_ARCHIVE, ASSUMED_LATENCY_REPLAY, SYNTHETIC_FIXTURE provenance를 구분하라.
- B05에 따라 원본 압축 로그·Parquet/manifest·PostgreSQL/TimescaleDB 저장·재시작·중복 제거를 구현하라. DB 미설치 시 기능 시험은 진행하되 실제 DB 통과로 보고하지 말라.
- Binance 공식 최신 문서를 읽어 depth /public, aggTrade·markPrice·kline /market의 경로를 확인하라. WS 연결 성공만으로 메시지 수신 완료라고 하지 말라.
- L2는 USDⓈ-M의 U/u/pu·snapshot 절차를 사용하라. gap이면 즉시 invalid→resync이며, Spot의 절차를 섞지 말라. B05.3과 T02를 구현하라.
- 현재 OI와 제한된 기간의 OI 이력을 구분하라. 역사 L2·뉴스 수신 시각·과거 OI를 지어내지 말라.
- 거시/실적 일정·발표치·수정본·뉴스/루머·교차자산·온체인은 제공자 계약과 missing 상태를 갖춰라. 확인되지 않은 X 스크래핑이나 유료 공급자가 이미 연결됐다고 쓰지 말라.

사용자가 원하는 것은 매집·지지 유지·허수 벽·악재 이후 반등·Nasdaq 충격에 따른 지지 붕괴까지 검토하는 것이다. 관측 가능한 호가/체결/가격 반응과 시나리오로 표현하라. 공개 데이터만으로 특정 세력의 지갑·계좌·보유 포지션·의도를 확정하거나 '세력 매집 정답 라벨'을 임의 생성하지 말라.

### 5. 실제 모델 학습

LightGBM과 TCN의 학습·평가·추론 경로를 각각 구현하라. 입력 피처·타깃 정의·기간·단위·클래스 순서 [down, neutral, up]와 학습 라벨 최종 시각을 artifact에 고정하라. 확률 합·범위·NaN·피처 순서를 검사하라.

TCN 초기 구조는 B07.3의 입력 256시점, 채널 32, 6개 residual block, 각 causal Conv1d 2개, kernel 3, dilations [1,2,4,8,16,32], dropout 0.1, 마지막 시점 classifier다. receptive field 253을 검산하라. 미래 패딩과 시간 축 정규화로 인과성을 깨지 말라. CPU smoke가 가능하도록 만들고 batch size는 현재 자원으로 결정하라.

무작위 train/test split, 전체 자료로 scaler/feature selection fit, 미래 지표 수정본 사용을 금지한다. 합성 데이터 학습은 smoke test일 뿐이며 실제 시장 학습 성과로 보고하지 말라. 학습 데이터가 부족하면 코드와 실행 경로를 구현한 뒤 IMPLEMENTED_UNTRAINED 또는 해당 미완료 상태로 남겨라.

메타 모델은 B09의 전진 OOS 입력으로 학습한다. 하이퍼파라미터와 전처리도 각 fold의 미래를 보지 않아야 한다. label_end_at으로 중첩을 purge하고 자산별 시간을 같은 경계로 자른다. 기본 sklearn stacking CV로 대체하지 말라.

개발/OOS 생성 → base·meta 고정 → 더 뒤의 독립 보정 → 잠금 최종 시험 순서를 지켜라. 기본 보정은 scalar temperature scaling이며 별도 구간으로 적합하라. 메타 학습·보정·최종 시험 사이도 라벨 종료 시각으로 중첩을 제거하라. 보정 이후 base/meta 재학습을 했다면 기존 보정·최종 시험 결과를 재사용하지 말라. 단순 확률만으로 수익 크기나 EV를 만들어내지 말라.

### 6. TradingAgents 근거 계층

EvidenceBundle의 읽기 전용 시점 snapshot을 만들어 실제 그래프의 데이터 조회를 통제하라. 주식용 기본 도구를 그대로 코인 분석 완료라고 쓰지 말라. B08의 시장/흐름·뉴스·거시/일정·암호화폐 기초 정보·찬반 근거 역할을 구현하라.

trade_date는 날짜 단위다. 암호화폐 모드나 날짜 인자만으로 intraday as-of 검증이 됐다고 하지 말라. 모든 tool 조회·cache·checkpoint·memory·reflection·pending settlement 경로를 검토하라. historical/replay에서 실시간 조회 우회가 남으면 UNVERIFIED로 분류하고 엄격한 성능 평가에서 제외하라.

초기 historical 실행의 기억·reflection·settlement는 실제 hook 또는 최소 패치로 차단·격리하고 T07/T08로 확인하라. cache key에는 정확한 as-of, 자산/상품, bundle hash, fold/run, upstream/model/prompt/config, portfolio snapshot을 넣어라. 날짜만으로 캐시를 공유하지 말라. 프로세스 격리·전역 설정·실행별 config 복사도 검증하라.

출력은 evidence ID를 가진 주장·반대 근거·이벤트 flags·coverage·상태·시점 검증 범위로 구조화하라. 환각된 ID와 무근거 출력은 INVALID/PARTIAL이다. TA rating을 p_up이나 매수 주문으로 바꾸지 말라. REVIEW, missing, neutral을 구분하라. LLM은 모델 확률·위험 규칙·포트폴리오를 덮어쓰지 못한다.

worker는 lease·idempotency·재시작·timeout·호출량·비용 상한을 갖추고 빠른 수집/예측을 막지 않아야 한다. context 없는 경우 학습된 numeric_only fallback을 사용하거나 최종 결합을 ABSTAIN하라. 임의 중립값을 넣어 정상 처리하지 말라.

LLM 제공자는 설정으로 선택하고 API 모델 ID·실제 응답 모델·프롬프트 해시·사용량을 기록하라. 선택적 Qwen3 등 로컬 가중치는 별도 자원/라이선스/품질 검증 전에는 기본 설치하지 말라. LLM 파라미터의 역사 지식 오염 가능성을 한계로 기록하고 전향 검증을 남겨라.

### 7. 독립 위험 판정과 보고

B10의 순서로 준비도·시각/신선도·스키마·불일치/OOD·비용/이벤트·포트폴리오/한도를 검사하라. RESEARCH_ONLY, ABSTAIN, REVIEW_CANDIDATE와 PASS/FAIL/UNKNOWN을 분리하라. 미제공 포트폴리오를 flat으로 간주하지 말라.

사람용 보고서는 최소 다음을 담아라.
- 자산/상품·as-of·실제 생성 시각·기간·입력 프로필·데이터 지연/품질.
- 기간별 수치 확률, 모델 간 충돌, 모델/메타/보정 버전과 준비도.
- 근거와 반대 근거, 사실과 추정, 중요한 일정·충격 시나리오, 누락된 소스.
- 위험 규칙별 pass/fail/unknown, 보류 사유·유효기간, 포트폴리오 상태.
- approval_required=true, execution_enabled=false, 아직 검증하지 못한 부분.

1시간과 24시간 의견을 한 개의 방향으로 억지 투표하지 말라. 수치와 LLM이 충돌하면 검증된 메타 입력·불일치 기준으로 처리하고, 학습되지 않은 재량 가중치를 넣지 말라. 설명용 LLM을 추가로 쓰더라도 확정된 판정을 바꿀 권한은 주지 말라.

### 8. 단계별 구현과 검증

설계도 B12의 명령 인터페이스를 구현하라. 다음 순서를 따른다.

| 단계 | 이번 단계의 실제 목표 |
|---|---|
| M0 | 환경 진단 → 설치/패키지/계약 → fixture 입력에서 검증·위험 보류·보고까지 실행 |
| M1 | 실제 공개 데이터 수집·원본·DB migration·L2 복원·v0 dataset |
| M2 | LightGBM 실데이터 학습·추론·기초 평가 |
| M3 | 작은 TCN 학습·인과성 검증·전진 OOS 생성 |
| M4 | 실제 TradingAgents + 단순 LLM 대조 adapter·시간/기억 격리 |
| M5 | 기간/backend별 메타·보정·위험 통합·잠금 평가 |
| M6 | E0~E5 가용 범위 비교·shadow 운영·배포 준비 문서 |

우선 M0를 실제로 실행한 뒤 M1 이후의 독립 구현을 계속하라. 하나의 세션에서 모든 훈련·장기간 전향 검증을 완료하라는 뜻이 아니다. 데이터/API/실행 시간이 부족한 결과는 차단 조건을 명시하고 나머지 구현을 진행하라. 평범한 단계 전환마다 진행 허락을 묻지 말라.

T01~T14 중 구현한 기능에 해당하는 실패 경로를 검사하라. 특히 미래 누출·호가 재동기화·TCN 인과성·artifact 불일치·TA 조회/기억 우회·context 결측·unknown portfolio·롱숏 비용·재시작은 실제 위험을 잡는 시험이어야 한다. 빈 함수를 호출하고 성공하는 테스트로 수를 채우지 말라.

E0~E5는 같은 기간·자산·소스·비용·위험 규칙으로 비교하라. E4/E5의 근거 bundle·가능한 LLM을 맞추고 추가 토큰·호출량·latency를 기록하라. 판단/체결은 실제 결과 생성 이후로만 가정하고 LLM 결과를 과거 봉에 소급 적용하지 말라. TradingAgents를 추가했다는 이유로 시너지가 검증됐다고 보고하지 말라.

### 9. 산출물과 세션 인수인계

다음 산출물을 실제 구현 상태에 맞게 생성·갱신하라.
1. 실행 코드·설정·의존성 lock·환경변수 예시·필요한 migration.
2. README.md: 설치, 실제 동작 명령, 현재 제약과 데이터 준비 절차.
3. BUILD_STATUS.md: M/R/T 식별자별 상태, 증거 경로, 명령과 실제 결과, 미해결 조건, 다음 첫 작업.
4. docs/THIRD_PARTY_SOURCES.md, THIRD_PARTY_NOTICES.md, docs/UPSTREAM_PATCHES.md: 출처·버전·라이선스·재사용·변경.
5. 계약 schema, dataset/artifact manifest, 학습·검증·실연결 상태 기록.
6. 구현 단계에 맞는 시험과 실행 결과, 비교 실험 보고, 운영·복구 문서.

모든 모델에 대해 코드 구현 / 실제 학습 / OOS 평가 / 전향 평가를 별도 열로 관리하라. fixture 통과, offline 계약 시험, 실 API 연결, 시장 성능 검증을 서로 바꿔 표현하지 말라. 실제로 실행하지 않은 명령은 '미실행'으로 남겨라.

한 세션이 끝날 때 BUILD_STATUS.md와 docs/NEXT_SESSION.md에 현재 파일·artifact·실행 결과·차단 원인·정확한 다음 명령을 기록하라. 다음 세션에서는 설계도와 이 두 상태 파일부터 읽어 이미 된 작업을 재생성하지 말라. 학습 파일이나 데이터가 없으면 있다고 가정하지 말라.

최종 보고는 '무엇이 실제로 동작하는지 → 어떤 검증을 실행했는지 → 어떤 데이터/API/학습이 아직 필요한지 → 다음 실행 명령' 순서로 한국어로 작성하라. 가짜 성과·과장된 수익률·검증되지 않은 모델 정확도를 제시하지 말라.

이제 설계도 전체 읽기와 환경 진단부터 시작하고, M0를 계획 설명으로 끝내지 말고 실제 파일 작성·실행·검증까지 수행하라.

## 프롬프트 끝
