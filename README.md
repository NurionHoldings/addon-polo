# 누리온 세일즈허브 | 아세아홀딩스

아세아홀딩스의 입점업체 오프라인 POS, 라이브커머스, 온라인 판매 내역을 서버에 보관하고, 주문 원장·수수료 명세·카드/PG/은행 정산자료를 대조하는 운영용 웹 애플리케이션입니다.

## 구현 범위

- 관리자, 정산담당자, 입점업체 3개 역할과 업체별 거래 조회 범위
- 비밀번호 PBKDF2 해시, HttpOnly·SameSite 세션 쿠키, CSRF 확인, 로그인 실패 제한
- 매출·환불·취소의 추가 기록형 원장(기존 거래 덮어쓰기/삭제 없음)
- 주문 이벤트 ID와 파일 SHA-256 기반 CSV 중복 차단
- 거래 CSV와 정산 묶음 CSV 업로드·내려받기
- POS/온라인·라이브/PG/은행 연결 현황과 업체 협의 질문 목록
- 업체 JSON 자료를 표준 매출·정산 CSV로 바꾸는 커넥터 정규화 도구
- 원장근거 기반 ARKAON 운영분석 화면(업체별 접근범위 적용)
- 정산 참조번호 기준 예상 입금액·실입금·결제수수료 비교
- 계약별 수수료율, 거래별 상품세액, 수수료 부가세(별도 10%) 계산
- 월별 정산 명세 스냅샷과 수납 처리, 변경 감사기록
- CSV 스프레드시트 수식 삽입 방지, SQLite 온라인 백업

## Docker로 시작

영구 저장 볼륨을 연결하고 최초 운영자 계정 정보를 환경 변수로 전달합니다. 최초 계정이 생성되면 BOOTSTRAP_ADMIN_PASSWORD는 배포 환경에서 제거하세요.

    docker build -t onbuilding-ledger .
    docker run -d --name onbuilding-ledger -p 8000:8000 -v onbuilding-data:/data -e COOKIE_SECURE=1 -e BOOTSTRAP_ADMIN_USERNAME=owner -e BOOTSTRAP_ADMIN_PASSWORD='12자 이상의 긴 임의 비밀번호' onbuilding-ledger

서비스 앞단에 HTTPS를 적용해야 합니다. 운영 중에는 COOKIE_SECURE=1을 유지하세요. 데이터는 /data/onbuilding.sqlite3에 저장됩니다. 백업은 다음과 같이 실행합니다.

    docker exec onbuilding-ledger python app.py backup --output /data/backups/onbuilding.sqlite3

로컬 테스트를 위해서는 COOKIE_SECURE를 0으로 두고 python app.py serve --host 127.0.0.1 --port 8000을 실행합니다. 첫 관리자는 python app.py create-user owner --role admin으로 만들 수 있습니다.

## 첫 설정 및 일상 업무

처음 사용하는 실무자는 [실무자 업무 매뉴얼](docs/실무자-업무매뉴얼.md)을 먼저 확인하세요. 화면별 입력 순서, CSV 예시, 월 마감 절차와 오류 대응을 쉬운 말로 설명합니다.

관리자·정산담당자는 **연동 준비** 화면과 [업체 협의 양식](connectors/partner-intake.md)에서 POS·판매채널·PG·은행 업체에 확인할 사양을 볼 수 있습니다. 업체 문서와 비식별 표본을 받은 뒤 [표준 필드 매핑 양식](connectors/provider-map.template.json)을 채우면 다음처럼 표준 CSV로 변환해 시험할 수 있습니다. 현재 자동 API 수집은 활성화되어 있지 않습니다.

    python -m connectors.normalize --kind sales --input vendor-sample.json --mapping provider-map.json --tenant '등록된 업체명' --tax-mode taxable --output normalized-sales.csv
    python -m connectors.normalize --kind settlement --input payout-sample.json --mapping provider-map.json --output normalized-payouts.csv

1. 로그인한 뒤 입점업체와 판매 채널을 등록합니다.
2. 입점업체별 수수료율과 과세 구분을 계약서에 맞게 설정합니다.
3. 오프라인 POS 및 온라인 판매 자료를 주문 이벤트 CSV로 가져오거나 직접 등록합니다.
4. 취소·환불은 원 거래를 지우지 않고 환불/취소 이벤트로 추가합니다.
5. 카드사·PG 정산 묶음과 통장 실입금 CSV를 정산 참조번호별로 대조합니다.
6. 월별 거래 확인 후 정산 스냅샷을 생성하고 수납 사실을 기록합니다.
7. 정산 담당자 계정은 python app.py create-user staff --role finance로 만들고, 업체 계정은 업체 등록 후 python app.py create-user vendor --role tenant --tenant-id 업체ID로 발급합니다.

## 공개 홈과 관리자 전용 화면

공개 안내 홈은 `public/index.html`에 있습니다. 로그인하면 운영자는 `/admin`의 관리자 전용 운영실에서 업체·채널 등록, 정산 차이, 미수 명세와 최근 변경 이력을 확인할 수 있습니다. `/admin` 권한은 화면 버튼 숨김이 아니라 서버에서 `admin` 역할을 확인합니다.

Netlify는 공개 홈과 요청 프록시로 사용합니다. 현재 원장 앱은 Python WSGI와 SQLite이므로 영구 저장소가 있는 컨테이너 서버도 함께 필요합니다. 배포 변수 설정과 검증 순서는 [Netlify 배포 준비](docs/netlify-deployment.md)를 확인하세요. Python 운영 서버 주소가 설정되기 전에는 로그인/관리자 요청을 공개하지 않습니다.

## CSV 형식

매출 이벤트 필수 헤더:

    event_id,order_id,event_type,date,tenant,amount,tax_amount

선택 헤더:

    discount,product,payment_method,settlement_ref

event_type은 sale, refund, cancel 또는 한글 동의어입니다. 금액은 원 단위 정수로 양수 입력하며 환불·취소 이벤트도 양수로 올리면 원장에 음수로 기록됩니다. 과세 업체의 tax_amount가 비어 있으면 결제액의 1/11을 세액으로 계산합니다. 면세는 0, 혼합과세는 거래별 세액을 입력해야 합니다.

입금 정산 묶음 필수 헤더:

    settlement_ref,settlement_date,expected_amount,received_amount

선택 헤더는 provider_fee,bank_ref입니다. 정산 묶음 자료를 업로드할 채널은 화면에서 선택합니다. 입금 CSV의 정산 건은 운영자·정산담당자만 볼 수 있습니다.

## 금액과 정산 기준

- 매출 행의 amount는 소비자가 실제 결제한 금액(할인 반영 후)입니다.
- 수수료 대상액은 업체 과세 구분에 따라 상품세액을 제외한 공급가액입니다.
- 수수료는 업체 계약 수수료율을 적용합니다. 기본값은 10%이며, 수수료 용역 부가세 10%를 별도 산출합니다.
- 금액은 원 단위로 저장·반올림합니다.
- 한 정산월 환불액이 매출액을 초과하면 정산 생성이 중단됩니다. 마감 이후 환불을 별도 이월 조정으로 확정하는 업무 규칙은 계약·세무 처리에 맞춰 추가해야 합니다.
- 판매 플랫폼과 지급 주기가 서로 다를 수 있으므로, 자동 API 연결 전에는 입점업체의 공식 API 권한 또는 공식 정산 CSV를 사용합니다.

## 커넥터 협의 및 분석

현재 앱에는 POS·온라인/라이브·PG·은행 자료의 연결 목표와 표준 필드 형식이 준비되어 있습니다. JSON 정규화 코드는 파트너 표본을 원장 CSV 형식으로 변환하며, 금액/세액/날짜/이벤트 ID를 엄격히 검사합니다. 업체별 API 인증, 웹훅 서명, 페이지 처리, 재시도는 각 업체의 공식 문서와 샌드박스에 맞춰 구현하고 시험해야 합니다. 자격증명은 앱 DB나 Git에 기록하지 말고 운영 환경 Secret Manager/환경변수로 전달합니다.

**아르카온 분석** 화면은 등록된 거래와 정산 데이터를 읽어 입금 차이, 매출자료가 없는 활성 채널, 정산 참조번호 연결 여부, 세액 이상값, 환불 이월 검토항목을 표시합니다. 원본 자료가 들어오지 않은 거래를 알아낼 수는 없으므로, 채널 원본 합계와 정기 대조해야 합니다.

## 운영 경계

실제 POS/VAN, 네이버·카카오 라이브커머스, 자사몰, 은행 API 자격증명은 연결되어 있지 않습니다. CSV 가져오기와 직접 등록은 동작하지만 실제 판매 채널 자료를 자동으로 수집하려면 해당 계정의 승인된 API·웹훅 권한과 자료 스키마가 필요합니다. 애플리케이션은 고객 판매대금을 보관·분배하거나 자동 이체하지 않습니다. 결제와 판매대금 지급은 각 입점업체와 등록된 결제대행사 흐름을 유지해야 합니다.

공용 POS·채널 자동수집을 붙이기 전에는 업종별 결제·세금계산서 흐름, 수수료 기준, 거래 분쟁 책임을 실제 계약과 대조하세요. 관리자와 정산담당자 계정에만 민감한 전체 정산 데이터를 제공합니다.

## 검증

    python -m py_compile app.py
    python -m unittest discover -s tests -v

자동화 테스트는 로그인·CSRF·매출 등록·상품 부가세·수수료 정산·입점업체 권한, 정산 CSV 변환, 커넥터 준비 화면, 분석 화면의 업체별 숫자 격리, CSV 수식 방지를 확인합니다.
