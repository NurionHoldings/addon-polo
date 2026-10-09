# Netlify 배포 준비

## 구성

- Netlify가 `public/`의 공개 안내 홈 화면과 배포 미리보기를 제공합니다.
- `netlify/functions/backend.mjs`가 로그인, 관리자 화면과 기존 앱 요청을 Python WSGI 운영 서버로 같은 도메인에서 전달합니다.
- Python 앱과 SQLite 원장은 영구 디스크를 지원하는 컨테이너 서버에서 실행합니다. Netlify의 정적 파일/함수 디스크를 업무 원장 저장소로 쓰지 않습니다.
- 로그인 세션은 기존 HttpOnly·SameSite 쿠키와 CSRF 확인을 유지합니다. `/admin`은 서버에서 `admin` 역할을 확인합니다.

## Netlify 프로젝트 설정

저장소 루트에서 `netlify.toml`을 사용합니다.

- Publish directory: `public`
- Functions directory: `netlify/functions`
- Function 환경 변수 `ONBUILDING_BACKEND_URL`: Python 앱의 HTTPS 원본 주소만 입력합니다. 예: `https://ledger-api.example.com` (경로·아이디·비밀번호를 넣지 않습니다.)
- 임시 로컬 개발은 `.env`에 `ONBUILDING_BACKEND_URL=http://127.0.0.1:8000`을 설정하고 `netlify dev`를 실행합니다. 이 파일은 저장소에 올리지 않습니다.

Netlify 함수는 서버 환경변수에서만 원본 주소를 읽습니다. 브라우저가 전달한 호스트나 임의 URL로 전달하지 않습니다. 매출 화면 응답에는 캐시를 사용하지 않습니다.

## Python 운영 서버

Netlify는 공개 홈과 요청 프록시를 제공하고, 현재 Python WSGI 앱은 영구 저장소가 있는 컨테이너에서 실행해야 합니다. `Dockerfile`을 사용하고 `/data`에 영구 볼륨을 연결합니다. `COOKIE_SECURE=1`을 유지하고 HTTPS를 적용합니다. 최초 운영자 개설 후에는 `BOOTSTRAP_ADMIN_PASSWORD` 환경 변수를 제거합니다.

앱의 DB 초기화와 계정 생성 명령은 README를 따릅니다. 실제 운영 데이터는 저장소에 포함하지 않습니다.

## 배포 전 확인

1. 관리자 계정으로 `/admin`에 로그인하고 관리자 홈이 표시되는지 확인합니다.
2. 정산 담당자와 입점업체 계정으로 `/admin`을 열어 서버가 403으로 차단하는지 확인합니다.
3. 로그인, 로그아웃, CSRF 확인, CSV 업로드/다운로드, 업체별 자료 범위를 점검합니다.
4. 컨테이너를 재시작한 뒤 DB가 유지되는지 확인하고 백업을 만들어 복구까지 시험합니다.
5. Netlify Deploy Preview에서 정적 홈, 로그인 전달, 파일 업로드, 다운로드, 세션 쿠키를 점검한 뒤 운영 공개를 결정합니다.

이 저장소에는 현재 Python 앱의 영구 운영 서버 주소가 등록되어 있지 않습니다. 해당 주소와 `ONBUILDING_BACKEND_URL`이 설정되기 전까지 Netlify의 공개 홈은 표시할 수 있지만, 관리자 앱 요청은 준비 중(503)으로 응답합니다. 운영 자료를 넣은 채 바로 공개하지 않습니다.
