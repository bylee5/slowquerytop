# slowquerytop

[Box Anemometer](https://github.com/box/Anemometer)를 Python으로 다시 구현한 슬로우 쿼리 분석 도구입니다.
원본은 MySQL `pt-query-digest` 결과만 보여주지만, slowquerytop은 **SQL Server**(Query Store, 플랜 캐시 DMV)를
기본 대상으로 하고 MySQL slow log도 직접 가져올 수 있습니다.

| Anemometer (PHP) | slowquerytop (Python) |
|---|---|
| `pt-query-digest --review --history` | `slowquerytop collect` (SQL Server) / `slowquerytop import-slowlog` (MySQL) |
| `global_query_review` | `query_review` |
| `global_query_review_history` | `query_review_history` (서버·DB·쿼리·플랜·시간 구간 단위) |
| `conf/config.inc.php`의 datasources | `config.yaml`의 `datasources` |
| Report: 표 보기 / 그래프 보기, 필드 선택, 정렬, 퍼머링크 | 동일 (+ JSON API) |
| Query 상세: 샘플, fingerprint, 리뷰, EXPLAIN | 동일. EXPLAIN 대신 저장된 실행 계획 분석(연산자 트리, 누락 인덱스, 경고, 파라미터) |
| 임의 SQL `WHERE` 입력 | 화이트리스트 필드와 바인딩된 필터만 허용 (SQL 인젝션 방지) |

## 구성

```
slowquerytop/
  collectors/
    querystore.py     SQL Server Query Store 수집 (2016+, 권장)
    dmv.py            sys.dm_exec_query_stats 스냅샷 차분 수집 (Query Store가 없을 때)
    mysql_slowlog.py  MySQL/MariaDB/Percona slow log 파서 + 집계
    fingerprint.py    pt-query-digest 방식의 쿼리 정규화
  repository/
    schema.py         저장소 테이블 (SQLite / SQL Server / MySQL 어디든 SQLAlchemy로 생성)
    store.py          적재(upsert), 리뷰 저장, 보존기간 정리
    report.py         리포트·그래프·상세 조회
  plan.py             showplan XML 요약
  web/                FastAPI + Jinja2 + Chart.js (오프라인에서도 동작하도록 Chart.js 포함)
```

## 설치

```bash
python -m venv .venv
.venv\Scripts\activate          # Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
```

SQL Server에서 수집하려면 Microsoft ODBC Driver 17 또는 18 for SQL Server가 필요합니다.

## 빠르게 둘러보기 (데모 데이터)

```bash
slowquerytop demo                 # ./slowquerytop.db 에 7일치 가상 데이터 생성
slowquerytop serve                # http://127.0.0.1:8080
```

## 실제 사용

1. 설정 파일을 만듭니다.
   ```bash
   copy config.example.yaml config.yaml
   ```
   - `datasources`: 수집 결과를 저장할 DB입니다. SQLite 파일, 또는 `odbc:`로 지정한 SQL Server DB를 쓸 수 있습니다.
   - `targets`: 수집 대상 SQL Server입니다. `type: querystore`(권장) 또는 `type: dmv`.
   - 비밀번호는 `${ENV_VAR}` 형태로 환경 변수에서 읽게 하세요.
2. 수집 대상 DB에 Query Store를 켭니다(아직 꺼져 있다면).
   ```sql
   ALTER DATABASE [shop] SET QUERY_STORE = ON
     (OPERATION_MODE = READ_WRITE, INTERVAL_LENGTH_MINUTES = 60, WAIT_STATS_CAPTURE_MODE = ON);
   ```
3. 수집 계정 권한:
   - querystore: 각 DB에 `VIEW DATABASE STATE`
   - dmv: `VIEW SERVER STATE`
4. 저장소 테이블을 만들고 수집합니다.
   ```bash
   slowquerytop init-db
   slowquerytop collect              # 한 번 실행 (작업 스케줄러 / cron에 등록)
   slowquerytop collect --loop 600   # 또는 10분마다 계속 실행
   slowquerytop serve
   ```

### MySQL slow log

```bash
slowquerytop import-slowlog /var/log/mysql/slow.log --hostname db01
# 같은 파일을 다시 가져와도 중복되지 않음 (--mode replace, 기본값)
# 같은 시간대를 덮는 여러 로그를 합칠 때는 --mode add
```

## 수집 방식별 특징

| | Query Store (`querystore`) | 플랜 캐시 (`dmv`) |
|---|---|---|
| 지원 버전 | SQL Server 2016+, Azure SQL | 모든 버전 |
| 정확도 | 구간별 통계를 SQL Server가 직접 보관하므로 정확함 | 수집 사이에 캐시에서 밀려난 문장은 누락. min/max는 플랜 생존 기간 전체 값 |
| 증분 처리 | 마지막으로 읽은 구간부터 다시 읽음(열린 구간 갱신, 재실행해도 안전) | 이전 스냅샷과의 차이. 첫 실행은 기준점만 기록 |
| Lock 대기 시간 | 2017+에서 wait stats로 수집 | 없음 |
| 실행 계획 | 새 플랜마다 XML 저장 | 새 플랜마다 XML 저장 |

- 같은 인스턴스를 두 방식으로 **같은 datasource에** 수집하면 통계가 두 번 잡히므로, 설정 단계에서 오류로 막습니다.
- SQL Server Express는 새 DB를 `AUTO_CLOSE ON`으로 만듭니다. DB가 닫히면 플랜 캐시도 비워지므로, dmv 방식을 쓰려면 `ALTER DATABASE ... SET AUTO_CLOSE OFF`가 필요합니다.

## 화면

- **Report (표)**: 기간, 호스트, DB, 쿼리 텍스트, 체크섬, 리뷰 상태, 최소 실행 횟수로 필터링합니다. 그룹 기준은 쿼리, 쿼리+호스트/DB, 호스트, 호스트+DB 중에서 고릅니다.
  표시 컬럼은 39개 중에서 선택할 수 있습니다(총/평균/최대 시간, CPU, logical/physical reads, writes, rows, 전체 대비 %, reads/row, index ratio 등). 컬럼 머리글을 누르면 정렬됩니다.
- **Report (그래프)**: 지표별 Top N 쿼리(또는 호스트/DB)의 시계열입니다. 구간은 5분, 1시간, 1일, 자동 중에서 고르며, 일 단위 구간은 표시 시간대의 자정에 맞춰집니다.
- **Query 상세**: fingerprint와 샘플, 실행 횟수와 평균 시간 추이, 호스트/DB별 통계, **플랜별 통계**(플랜 회귀 확인용), 리뷰(검토자, 코멘트, 검토 완료) 입력을 제공합니다.
- **Plan 상세**: 연산자 트리(자체 비용 %, 예상 행 수, 조건자), 누락 인덱스 제안 DDL, 플랜 경고, 컴파일된 파라미터 값을 보여주고 `.sqlplan` 파일로 내려받을 수 있습니다(SSMS에서 그래픽으로 열림).
- **Copy permalink**: 현재 필터 상태를 URL로 복사합니다.
- **JSON API**: `/api/report`, `/api/graph`는 화면과 같은 파라미터를 받습니다. 문서는 `/api/docs`에 있습니다.

시간은 저장소에 UTC로 저장되고, 화면에는 `web.timezone`(예: `Asia/Seoul`) 기준으로 표시·입력됩니다.

## 보안

- 리뷰 저장이 가능하므로 내부망 밖에 노출한다면 `web.auth`(HTTP Basic)를 켜고 앞단에 HTTPS 리버스 프록시를 두세요.
- 수집 계정은 읽기 권한(`VIEW DATABASE STATE` / `VIEW SERVER STATE`)만으로 충분합니다.
- 모든 화면 입력은 화이트리스트로 검증하거나 바인딩 파라미터로 처리합니다.

## 테스트

```bash
pytest
# SQL Server 통합 테스트 (DB를 만들 수 있는 계정 필요. slowquerytop_itest DB를 생성합니다)
set SLOWQUERYTOP_TEST_ODBC=DRIVER={ODBC Driver 18 for SQL Server};SERVER=.\SQLEXPRESS;Trusted_Connection=yes;TrustServerCertificate=yes
pytest tests/test_mssql_integration.py
```
