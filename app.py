#!/usr/bin/env python3
"""Onbuilding tenant sales ledger. Python standard library WSGI + SQLite."""
from __future__ import annotations
import argparse, csv, datetime as dt, hashlib, hmac, html, io, json, os, secrets, sqlite3, sys, time, uuid
from email.parser import BytesParser
from email.policy import default
from http import cookies
from urllib.parse import parse_qs
from wsgiref.simple_server import make_server

ROOT=os.path.dirname(os.path.abspath(__file__))
DATA_DIR=os.environ.get('DATA_DIR',ROOT)
DB_PATH=os.environ.get('DATABASE_PATH',os.path.join(DATA_DIR,'onbuilding.sqlite3'))
SESSION_NAME='onbuilding_session'
SESSION_SECONDS=8*60*60
COOKIE_SECURE=os.environ.get('COOKIE_SECURE','0')=='1'
VAT_RATE=10

SCHEMA='''
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS tenants(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, business_no TEXT NOT NULL DEFAULT '', manager TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '', fee_rate TEXT NOT NULL DEFAULT '10.00', vat_mode TEXT NOT NULL DEFAULT 'taxable' CHECK(vat_mode IN ('taxable','exempt','mixed')), bank_account TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, channel_type TEXT NOT NULL, ingest_mode TEXT NOT NULL DEFAULT 'csv', active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','finance','tenant')), tenant_id INTEGER REFERENCES tenants(id), active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, user_id INTEGER REFERENCES users(id), csrf TEXT NOT NULL, expires_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS login_failures(username_key TEXT NOT NULL, attempted_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS import_batches(id INTEGER PRIMARY KEY, kind TEXT NOT NULL, filename TEXT NOT NULL, file_hash TEXT NOT NULL, imported_by INTEGER REFERENCES users(id), row_count INTEGER NOT NULL, duplicate_count INTEGER NOT NULL, created_at TEXT NOT NULL, UNIQUE(kind,file_hash));
CREATE TABLE IF NOT EXISTS sale_events(id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL REFERENCES tenants(id), channel_id INTEGER NOT NULL REFERENCES channels(id), event_key TEXT NOT NULL, order_id TEXT NOT NULL, event_type TEXT NOT NULL CHECK(event_type IN ('sale','refund','cancel')), occurred_on TEXT NOT NULL, product TEXT NOT NULL DEFAULT '', gross_amount INTEGER NOT NULL, discount_amount INTEGER NOT NULL DEFAULT 0, tax_amount INTEGER NOT NULL DEFAULT 0, payment_method TEXT NOT NULL DEFAULT '', settlement_ref TEXT NOT NULL DEFAULT '', import_batch_id INTEGER REFERENCES import_batches(id), created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL, UNIQUE(channel_id,event_key));
CREATE INDEX IF NOT EXISTS sale_period_idx ON sale_events(tenant_id,occurred_on);
CREATE TABLE IF NOT EXISTS payout_records(id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL REFERENCES channels(id), settlement_ref TEXT NOT NULL, settlement_date TEXT NOT NULL, expected_amount INTEGER NOT NULL DEFAULT 0, received_amount INTEGER NOT NULL DEFAULT 0, provider_fee INTEGER NOT NULL DEFAULT 0, bank_ref TEXT NOT NULL DEFAULT '', created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL, UNIQUE(channel_id,settlement_ref));
CREATE TABLE IF NOT EXISTS statements(id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL REFERENCES tenants(id), period TEXT NOT NULL, fee_rate TEXT NOT NULL, basis_amount INTEGER NOT NULL, fee_amount INTEGER NOT NULL, vat_amount INTEGER NOT NULL, total_amount INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'issued' CHECK(status IN ('issued','paid')), generated_by INTEGER REFERENCES users(id), generated_at TEXT NOT NULL, paid_at TEXT, UNIQUE(tenant_id,period));
CREATE TABLE IF NOT EXISTS statement_lines(id INTEGER PRIMARY KEY, statement_id INTEGER NOT NULL REFERENCES statements(id), sale_event_id INTEGER NOT NULL REFERENCES sale_events(id), included_amount INTEGER NOT NULL, UNIQUE(statement_id,sale_event_id));
CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY, actor_id INTEGER REFERENCES users(id), action TEXT NOT NULL, object_type TEXT NOT NULL, object_id TEXT NOT NULL, detail_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL);
'''

def now_iso(): return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
def today_kst(): return dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).date()
def conn():
    os.makedirs(os.path.dirname(DB_PATH) or '.',exist_ok=True)
    c=sqlite3.connect(DB_PATH,timeout=20)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    c.execute('PRAGMA journal_mode=WAL')
    return c

def init_db():
    with conn() as c: c.executescript(SCHEMA)
def audit(c, actor, action, kind, oid, details=None):
    c.execute('INSERT INTO audit_log(actor_id,action,object_type,object_id,detail_json,created_at) VALUES(?,?,?,?,?,?)',(actor,action,kind,str(oid),json.dumps(details or {},ensure_ascii=False,sort_keys=True),now_iso()))
def password_hash(password, salt=None):
    salt=salt or secrets.token_hex(16)
    derived=hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),310000).hex()
    return f'pbkdf2_sha256$310000${salt}${derived}'
def password_ok(password, encoded):
    try:
        alg,iterations,salt,expected=encoded.split('$')
        actual=hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),int(iterations)).hex()
        return alg=='pbkdf2_sha256' and hmac.compare_digest(actual,expected)
    except Exception: return False

def create_user(username,password,role='admin',tenant_id=None):
    if len(password)<12: raise ValueError('비밀번호는 12자 이상이어야 합니다.')
    if role=='tenant' and not tenant_id: raise ValueError('입점업체 계정에는 tenant_id가 필요합니다.')
    with conn() as c:
        cur=c.execute('INSERT INTO users(username,password_hash,role,tenant_id,created_at) VALUES(?,?,?,?,?)',(username,password_hash(password),role,tenant_id,now_iso()))
        audit(c,cur.lastrowid,'user.create','user',cur.lastrowid,{'username':username,'role':role,'tenant_id':tenant_id})

def esc(v): return html.escape(str(v if v is not None else ''),quote=True)
def won(v): return f'{int(v or 0):,}원'
def page(title, body, user=None, active='dashboard', csrf=''):
    nav=''.join(f'<a class="{("active" if key==active else "")}" href="/{key}">{label}</a>' for key,label in [('dashboard','대시보드'),('sales','매출 원장'),('reconcile','입금 대사'),('settlements','수수료 정산'),('tenants','입점업체'),('channels','판매 채널')])
    who=f'{esc(user["username"])} · {esc(user["role"])}' if user else '로그인 필요'
    logout=f'<form method="post" action="/logout"><input type="hidden" name="csrf" value="{esc(csrf)}"><button class="link">로그아웃</button></form>' if user else ''
    return f'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)} | 온빌딩</title><style>
:root{{--navy:#13223d;--blue:#4969e8;--bg:#f4f6fa;--line:#e5e9f0;--muted:#78849a;--text:#202b3e;--green:#13865f;--red:#bd454d}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);font:14px/1.5 system-ui,"Noto Sans KR",sans-serif;color:var(--text)}}header{{height:64px;background:#fff;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:0 max(20px,calc((100vw - 1320px)/2));position:sticky;top:0;z-index:2}}.brand{{font-weight:850;color:var(--navy);font-size:18px;text-decoration:none}}.user{{display:flex;gap:16px;align-items:center;color:var(--muted);font-size:12px}}.link{{background:none;border:0;color:var(--blue);cursor:pointer}}nav{{display:flex;gap:4px;overflow:auto;padding:12px max(14px,calc((100vw - 1320px)/2));background:var(--navy)}}nav a{{color:#c5d0e1;text-decoration:none;padding:8px 12px;border-radius:8px;white-space:nowrap;font-size:12px}}nav a.active,nav a:hover{{background:#263a5b;color:white}}main{{max-width:1320px;margin:26px auto;padding:0 18px 55px}}h1{{font-size:24px;letter-spacing:-.5px;margin:0 0 4px}}h2{{font-size:16px;margin:0 0 14px}}.sub{{color:var(--muted);font-size:12px;margin-bottom:20px}}.row{{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:16px}}.card,.panel{{background:#fff;border:1px solid var(--line);border-radius:12px;padding:18px;box-shadow:0 2px 8px #1b31540b}}.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}}.metric small{{color:var(--muted)}}.metric strong{{display:block;font-size:23px;margin-top:8px}}.grid{{display:grid;grid-template-columns:1.2fr 1fr;gap:14px;margin-bottom:15px}}.btn{{display:inline-block;border:1px solid #dce2ec;background:white;color:#33425b;text-decoration:none;border-radius:8px;padding:8px 12px;cursor:pointer;font-weight:650;font-size:12px}}.btn.primary{{background:var(--blue);color:#fff;border-color:var(--blue)}}input,select,textarea{{font:inherit;border:1px solid #dbe1eb;border-radius:7px;padding:9px 10px;background:#fff;max-width:100%}}label{{display:block;font-size:11px;font-weight:700;color:#637086;margin:10px 0 5px}}form.inline{{display:flex;gap:8px;align-items:end;flex-wrap:wrap}}.tablewrap{{overflow:auto}}table{{width:100%;border-collapse:collapse;white-space:nowrap}}th{{background:#f8f9fb;color:#768298;text-align:left;font-size:10px;padding:10px;border-bottom:1px solid var(--line)}}td{{font-size:11px;padding:10px;border-bottom:1px solid #edf0f4}}.right{{text-align:right}}.badge{{display:inline-block;background:#e9f5ef;color:#137d5c;padding:3px 8px;border-radius:20px;font-size:10px}}.warn{{background:#fff4df;color:#a96c0e}}.bad{{background:#ffeded;color:#b33e48}}.note{{background:#fff8e8;color:#805d19;border-radius:8px;padding:11px 13px;font-size:11px;margin-top:12px}}.error{{background:#fff0f0;color:#a4303b;padding:10px;border-radius:8px;margin:12px 0;font-size:12px}}.success{{background:#eaf7f1;color:#176c52;padding:10px;border-radius:8px;margin:12px 0;font-size:12px}}.formgrid{{display:grid;grid-template-columns:repeat(3,1fr);gap:4px 12px}}.small{{font-size:10px;color:var(--muted)}}.spacer{{height:14px}}.login{{max-width:440px;margin:80px auto}}.danger{{color:var(--red)}}@media(max-width:800px){{.cards{{grid-template-columns:repeat(2,1fr)}}.grid{{grid-template-columns:1fr}}.formgrid{{grid-template-columns:1fr 1fr}}main{{margin-top:18px}}}}@media(max-width:520px){{.cards{{grid-template-columns:1fr 1fr;gap:8px}}.metric strong{{font-size:17px}}header{{padding:0 14px}}.formgrid{{grid-template-columns:1fr}}}}
</style></head><body><header><a class="brand" href="/dashboard">온빌딩 · SALES CONTROL</a><div class="user">{who}{logout}</div></header><nav>{nav}</nav><main>{body}</main></body></html>'''

def login_page(message='', csrf=''):
    return page('로그인',f'''<section class="login panel"><h1>운영자 로그인</h1><p class="sub">매출·정산 자료는 권한이 있는 사용자만 볼 수 있습니다.</p>{message}<form method="post" action="/login"><input type="hidden" name="csrf" value="{esc(csrf)}"><label>아이디</label><input name="username" autocomplete="username" required style="width:100%"><label>비밀번호</label><input name="password" type="password" autocomplete="current-password" required style="width:100%"><div class="spacer"></div><button class="btn primary" style="width:100%">로그인</button></form></section>''')

class App:
    def __init__(self): init_db()
    def __call__(self,environ,start_response):
        path=environ.get('PATH_INFO','/')
        method=environ.get('REQUEST_METHOD','GET').upper()
        session,user,csrf,new_cookie=self.session_for(environ)
        try:
            if path=='/health': return self.respond(start_response,'200 OK','ok', [('Content-Type','text/plain; charset=utf-8')],new_cookie)
            if path=='/login' and method=='POST':
                data=self.form(environ);self.check_csrf(data,csrf)
                with conn() as c:
                    username=data.get('username','').strip()
                    username_key=hashlib.sha256(username.casefold().encode()).hexdigest()
                    cutoff=int(time.time())-900
                    failures=c.execute('SELECT COUNT(*) n FROM login_failures WHERE username_key=? AND attempted_at>?',(username_key,cutoff)).fetchone()['n']
                    u=c.execute('SELECT * FROM users WHERE username=? AND active=1',(username,)).fetchone()
                    if failures>=10 or not u or not password_ok(data.get('password',''),u['password_hash']):
                        c.execute('INSERT INTO login_failures(username_key,attempted_at) VALUES(?,?)',(username_key,int(time.time())))
                        return self.html(start_response,login_page('<div class="error">아이디 또는 비밀번호를 확인해 주세요.</div>',csrf),'200 OK',new_cookie=new_cookie)
                    c.execute('DELETE FROM login_failures WHERE username_key=?',(username_key,))
                    raw=secrets.token_urlsafe(32);c.execute('DELETE FROM sessions WHERE token_hash=?',(session,));c.execute('INSERT INTO sessions(token_hash,user_id,csrf,expires_at) VALUES(?,?,?,?)',(hashlib.sha256(raw.encode()).hexdigest(),u['id'],secrets.token_urlsafe(24),int(time.time())+SESSION_SECONDS));audit(c,u['id'],'auth.login','user',u['id'])
                    start_response('303 See Other',[('Location','/dashboard'),('Set-Cookie',self.cookie(raw)) ,('Cache-Control','no-store')]);return [b'']
            if path=='/logout' and method=='POST':
                self.check_csrf(self.form(environ),csrf)
                with conn() as c:c.execute('DELETE FROM sessions WHERE token_hash=?',(session,))
                start_response('303 See Other',[('Location','/login'),('Set-Cookie',self.cookie('',max_age=0)),('Cache-Control','no-store')]);return [b'']
            if not user:
                if path=='/login': return self.html(start_response,login_page(csrf=csrf),'200 OK',new_cookie=new_cookie)
                start_response('303 See Other',[('Location','/login'),('Cache-Control','no-store')]);return [b'']
            if method=='POST':
                data=self.form(environ)
                self.check_csrf(data,csrf)
                return self.post(path,data,user,start_response,csrf)
            if method!='GET': return self.error(start_response,'405 Method Not Allowed','지원하지 않는 요청입니다.',user,csrf)
            if path.endswith('.csv'):
                return self.get_csv(path,user,start_response)
            if path=='/':path='/dashboard'
            routes={'/dashboard':self.dashboard,'/sales':self.sales,'/reconcile':self.reconcile,'/settlements':self.settlements,'/tenants':self.tenants,'/channels':self.channels,'/audit':self.audit_page}
            if path not in routes:return self.error(start_response,'404 Not Found','요청한 화면을 찾지 못했습니다.',user,csrf)
            if path=='/settlements':
                selected=parse_qs(environ.get('QUERY_STRING','')).get('period',[''])[0]
                if selected:
                    try:dt.date.fromisoformat(selected+'-01')
                    except ValueError:raise ValueError('정산월 형식은 YYYY-MM이어야 합니다.')
                body=routes[path](user,csrf,selected or None)
            else:body=routes[path](user,csrf)
            return self.html(start_response,body,user=user,csrf=csrf,new_cookie=new_cookie)
        except PermissionError as e:return self.error(start_response,'403 Forbidden',str(e),user,csrf)
        except ValueError as e:return self.error(start_response,'400 Bad Request',str(e),user,csrf)
        except Exception as e:
            print('request error:',repr(e),file=sys.stderr)
            return self.error(start_response,'500 Internal Server Error','처리 중 오류가 발생했습니다. 입력 파일과 필수 항목을 확인해 주세요.',user,csrf)
    def cookie(self,value,max_age=SESSION_SECONDS):
        return f'{SESSION_NAME}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}'+('; Secure' if COOKIE_SECURE else '')
    def session_for(self,env):
        jar=cookies.SimpleCookie();jar.load(env.get('HTTP_COOKIE',''));raw=jar[SESSION_NAME].value if SESSION_NAME in jar else ''
        token=hashlib.sha256(raw.encode()).hexdigest() if raw else ''
        with conn() as c:
            row=c.execute('SELECT s.csrf,s.expires_at,u.* FROM sessions s LEFT JOIN users u ON u.id=s.user_id WHERE s.token_hash=?',(token,)).fetchone() if token else None
            if row and row['expires_at']>int(time.time()):return token,dict(row) if row['username'] else None,row['csrf'],None
            if token:c.execute('DELETE FROM sessions WHERE token_hash=?',(token,))
            raw=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(24);token=hashlib.sha256(raw.encode()).hexdigest();c.execute('INSERT INTO sessions(token_hash,csrf,expires_at) VALUES(?,?,?)',(token,csrf,int(time.time())+SESSION_SECONDS))
            return token,None,csrf,self.cookie(raw)
    def respond(self,start,status,body,headers=None,new_cookie=None):
        b=body.encode('utf-8') if isinstance(body,str) else body
        hs=[('Content-Length',str(len(b))),('X-Content-Type-Options','nosniff'),('Referrer-Policy','same-origin'),('X-Frame-Options','DENY'),('Cache-Control','no-store')]+(headers or [])
        if new_cookie:hs.append(('Set-Cookie',new_cookie))
        start(status,hs);return [b]
    def html(self,start,body,status='200 OK',user=None,csrf='',new_cookie=None):return self.respond(start,status,body,[('Content-Type','text/html; charset=utf-8')],new_cookie)
    def error(self,start,status,msg,user,csrf):return self.html(start,page('알림',f'<h1>요청을 처리하지 못했습니다</h1><div class="error">{esc(msg)}</div><a class="btn" href="/dashboard">대시보드</a>',user,csrf=csrf),status,user=user,csrf=csrf)
    def form(self,env):
        try:n=int(env.get('CONTENT_LENGTH') or 0)
        except ValueError:n=0
        if n>6_000_000:raise ValueError('파일은 6MB 이하로 제한됩니다.')
        body=env['wsgi.input'].read(n)
        ctype=env.get('CONTENT_TYPE','')
        if 'multipart/form-data' in ctype:
            msg=BytesParser(policy=default).parsebytes(b'Content-Type: '+ctype.encode()+b'\r\nMIME-Version: 1.0\r\n\r\n'+body)
            out={}
            for part in msg.iter_parts():
                name=part.get_param('name',header='content-disposition')
                if not name:continue
                payload=part.get_payload(decode=True) or b''
                if part.get_filename():out[name+'_filename']=part.get_filename();out[name]=payload.decode('utf-8-sig',errors='replace')
                else:out[name]=payload.decode(part.get_content_charset() or 'utf-8',errors='replace')
            return out
        return {k:v[-1] for k,v in parse_qs(body.decode('utf-8',errors='replace'),keep_blank_values=True).items()}
    def check_csrf(self,data,csrf):
        if not csrf or not hmac.compare_digest(str(data.get('csrf','')),str(csrf)):raise PermissionError('보안 토큰이 만료되었습니다. 새로고침 후 다시 시도해 주세요.')
    def require(self,user,*roles):
        if not user or user['role'] not in roles:raise PermissionError('이 기능을 사용할 권한이 없습니다.')
    def scoped(self,user,sql,args=()):
        if user['role']=='tenant':return sql+' AND s.tenant_id=?',tuple(args)+(user['tenant_id'],)
        return sql,args
    def dashboard(self,user,csrf):
        with conn() as c:
            where='WHERE event_type IN ("sale","refund","cancel")';args=()
            if user['role']=='tenant':where+=' AND tenant_id=?';args=(user['tenant_id'],)
            today=today_kst().isoformat();s=c.execute(f'SELECT COALESCE(SUM(gross_amount),0) gross,COALESCE(SUM(tax_amount),0) tax,COUNT(*) n FROM sale_events {where} AND occurred_on=?',args+(today,)).fetchone()
            unpaid={'amt':0,'n':0} if user['role']=='tenant' else c.execute('SELECT COALESCE(SUM(MAX(expected_amount-received_amount,0)),0) amt,COUNT(*) n FROM payout_records WHERE expected_amount<>received_amount').fetchone()
            recent_sql='SELECT s.*,t.name tenant,ch.name channel FROM sale_events s JOIN tenants t ON t.id=s.tenant_id JOIN channels ch ON ch.id=s.channel_id WHERE 1=1';recent_args=()
            if user['role']=='tenant':recent_sql+=' AND s.tenant_id=?';recent_args=(user['tenant_id'],)
            recent=c.execute(recent_sql+' ORDER BY s.id DESC LIMIT 8',recent_args).fetchall()
            fee=float(c.execute('SELECT fee_rate FROM tenants WHERE id=?',(user['tenant_id'],)).fetchone()['fee_rate']) if user['role']=='tenant' else 10
            exp=c.execute('SELECT COUNT(*) n FROM payout_records WHERE expected_amount<>received_amount').fetchone()['n'] if user['role']!='tenant' else unpaid['n']
        base=int(s['gross']-s['tax']);feeamt=round(base*fee/100);blocks=''.join(f'<div class="card metric"><small>{label}</small><strong>{value}</strong></div>' for label,value in [('오늘 매출(결제·환불 순액)',won(s['gross'])),('오늘 수수료 예상',won(feeamt)),('입금 차액 대기',won(unpaid['amt'])),('확인할 대사 건',f'{exp}건')])
        rows=''.join(self.sale_row(x) for x in recent) or '<tr><td colspan="8">거래 기록이 없습니다. 매출을 등록하거나 CSV를 가져오세요.</td></tr>'
        body=f'<div class="row"><div><h1>통합 매출 대시보드</h1><div class="sub">주문 원장, 수수료와 입금 대사 현황</div></div><a class="btn primary" href="/sales">매출 원장 열기</a></div><div class="cards">{blocks}</div><div class="grid"><section class="panel"><h2>미대사 정산</h2>{self.recon_preview(user)}</section><section class="panel"><h2>정산 운영 상태</h2><p class="small">수수료율은 업체 계약 설정을 따릅니다. 월 마감 시점에 해당 월 정산 스냅샷을 생성하세요.</p><a class="btn" href="/settlements">정산 생성·조회</a><div class="note">원본 거래는 수정·삭제하지 않습니다. 환불은 별도 음수 거래로 등록되어 기존 매출과 이력이 보존됩니다.</div></section></div><section class="panel"><h2>최근 거래</h2><div class="tablewrap"><table>{self.sale_head()}<tbody>{rows}</tbody></table></div></section>'
        return page('대시보드',body,user,'dashboard',csrf)
    def sale_head(self):return '<thead><tr><th>발생일</th><th>주문번호</th><th>업체</th><th>채널</th><th>유형</th><th>결제액</th><th>세액</th><th>결제수단</th></tr></thead>'
    def sale_row(self,r):return f'<tr><td>{esc(r["occurred_on"])}</td><td>{esc(r["order_id"])}</td><td>{esc(r["tenant"])}</td><td>{esc(r["channel"])}</td><td>{esc(r["event_type"])}</td><td class="right">{won(r["gross_amount"])}</td><td class="right">{won(r["tax_amount"])}</td><td>{esc(r["payment_method"])}</td></tr>'
    def recon_preview(self,user):
        if user['role']=='tenant': return '<p class="small">업체별 정산 입금 자료는 운영자 정산 화면에서 관리합니다.</p>'
        with conn() as c:
            q='SELECT p.*,ch.name channel FROM payout_records p JOIN channels ch ON ch.id=p.channel_id WHERE p.expected_amount<>p.received_amount'
            a=()
            if user['role']=='tenant':q+=' AND p.channel_id IN (SELECT channel_id FROM sale_events WHERE tenant_id=?)';a=(user['tenant_id'],)
            rows=c.execute(q+' ORDER BY p.settlement_date DESC LIMIT 5',a).fetchall()
        return '<div class="tablewrap"><table><tr><th>정산참조</th><th>채널</th><th>예정</th><th>입금</th></tr>'+''.join(f'<tr><td>{esc(r["settlement_ref"])}</td><td>{esc(r["channel"])}</td><td>{won(r["expected_amount"])}</td><td>{won(r["received_amount"])}</td></tr>' for r in rows)+'</table></div>' if rows else '<p class="small">미대사 자료가 없습니다.</p>'
    def sales(self,user,csrf):
        with conn() as c:
            q='SELECT s.*,t.name tenant,ch.name channel FROM sale_events s JOIN tenants t ON t.id=s.tenant_id JOIN channels ch ON ch.id=s.channel_id';a=()
            if user['role']=='tenant':q+=' WHERE s.tenant_id=?';a=(user['tenant_id'],)
            rows=c.execute(q+' ORDER BY s.occurred_on DESC,s.id DESC LIMIT 500',a).fetchall()
            tenants=c.execute('SELECT id,name FROM tenants WHERE active=1'+(' AND id=?' if user['role']=='tenant' else ''),((user['tenant_id'],) if user['role']=='tenant' else ())).fetchall()
            channels=c.execute('SELECT id,name FROM channels WHERE active=1').fetchall()
        tenant_opts=''.join(f'<option value="{t["id"]}">{esc(t["name"])}</option>' for t in tenants);ch_opts=''.join(f'<option value="{x["id"]}">{esc(x["name"])}</option>' for x in channels)
        can=user['role'] in ('admin','finance')
        add=f'''<section class="panel"><h2>거래 직접 등록</h2><form method="post" action="/sales/add"><input type="hidden" name="csrf" value="{esc(csrf)}"><div class="formgrid"><div><label>입점업체</label><select name="tenant_id" required>{tenant_opts}</select></div><div><label>판매 채널</label><select name="channel_id" required>{ch_opts}</select></div><div><label>주문번호</label><input name="order_id" required maxlength="120"></div><div><label>유형</label><select name="event_type"><option value="sale">매출</option><option value="refund">환불</option><option value="cancel">취소</option></select></div><div><label>발생일</label><input type="date" name="occurred_on" value="{today_kst().isoformat()}" required></div><div><label>실결제/환불액 (원)</label><input type="number" min="0" name="gross_amount" required></div><div><label>이 거래의 상품 부가세 (원)</label><input type="number" min="0" name="tax_amount" placeholder="과세 업체는 자동 계산 · 혼합과세는 입력"></div><div><label>상품</label><input name="product"></div><div><label>결제수단</label><input name="payment_method"></div><div><label>정산 참조번호</label><input name="settlement_ref"></div></div><p class="small">환불·취소 금액은 양수로 입력합니다. 시스템이 원장에 음수로 기록합니다. 과세 거래는 상품 부가세액을 입력해야 합니다.</p><button class="btn primary">원장에 추가</button></form></section>''' if can else ''
        import_form=f'''<section class="panel"><h2>매출 CSV 가져오기</h2><form method="post" action="/sales/import" enctype="multipart/form-data"><input type="hidden" name="csrf" value="{esc(csrf)}"><div class="formgrid"><div><label>채널</label><select name="channel_id" required>{ch_opts}</select></div><div><label>CSV 파일</label><input type="file" name="file" accept=".csv,text/csv" required></div></div><p class="small">필수 열: event_id,order_id,event_type,date,tenant,amount,tax_amount. 선택 열: discount,product,payment_method,settlement_ref. UTF-8 CSV, 6MB 이하.</p><button class="btn">검증 후 가져오기</button></form></section>''' if can else ''
        rows_html=''.join(self.sale_row(r) for r in rows) or '<tr><td colspan="8">거래 자료가 없습니다.</td></tr>'
        return page('매출 원장',f'<div class="row"><div><h1>매출 원장</h1><div class="sub">매출·환불을 변경 불가능한 거래 이벤트로 기록합니다.</div></div><a class="btn" href="/sales.csv">CSV 내려받기</a></div>{add}{import_form}<section class="panel"><h2>최근 거래 · 최대 500건</h2><div class="tablewrap"><table>{self.sale_head()}<tbody>{rows_html}</tbody></table></div></section>',user,'sales',csrf)
    def reconcile(self,user,csrf):
        self.require(user,'admin','finance')
        with conn() as c:
            q='SELECT p.*,ch.name channel FROM payout_records p JOIN channels ch ON ch.id=p.channel_id';a=()
            if user['role']=='tenant':q+=' WHERE p.channel_id IN (SELECT channel_id FROM sale_events WHERE tenant_id=?)';a=(user['tenant_id'],)
            rows=c.execute(q+' ORDER BY p.settlement_date DESC,p.id DESC LIMIT 500',a).fetchall();chs=c.execute('SELECT id,name FROM channels WHERE active=1').fetchall()
        can=user['role'] in ('admin','finance'); chopts=''.join(f'<option value="{x["id"]}">{esc(x["name"])}</option>' for x in chs)
        f=f'''<section class="panel"><h2>정산 묶음 직접 등록</h2><form method="post" action="/reconcile/add"><input type="hidden" name="csrf" value="{esc(csrf)}"><div class="formgrid"><div><label>채널</label><select name="channel_id">{chopts}</select></div><div><label>정산 참조번호</label><input name="settlement_ref" required></div><div><label>정산일</label><input type="date" name="settlement_date" value="{today_kst().isoformat()}" required></div><div><label>예상 입금액</label><input type="number" min="0" name="expected_amount" value="0"></div><div><label>실제 입금액</label><input type="number" min="0" name="received_amount" value="0"></div><div><label>PG·카드 수수료</label><input type="number" min="0" name="provider_fee" value="0"></div><div><label>은행 참조번호</label><input name="bank_ref"></div></div><button class="btn primary">대사 자료 기록</button></form><div class="spacer"></div><h2>정산 CSV 가져오기</h2><form method="post" action="/reconcile/import" enctype="multipart/form-data"><input type="hidden" name="csrf" value="{esc(csrf)}"><div class="formgrid"><div><label>채널</label><select name="channel_id">{chopts}</select></div><div><label>CSV 파일</label><input type="file" name="file" accept=".csv,text/csv" required></div></div><p class="small">필수 열: settlement_ref,settlement_date,expected_amount,received_amount. 선택 열: provider_fee,bank_ref.</p><button class="btn">정산 CSV 가져오기</button></form></section>''' if can else ''
        tbl=''.join(f'<tr><td>{esc(r["settlement_date"])}</td><td>{esc(r["settlement_ref"])}</td><td>{esc(r["channel"])}</td><td class="right">{won(r["expected_amount"])}</td><td class="right">{won(r["received_amount"])}</td><td class="right">{won(r["provider_fee"])}</td><td class="right">{won(r["expected_amount"]-r["received_amount"])}</td><td><span class="badge {"" if r["expected_amount"]==r["received_amount"] else "warn"}">{"대사완료" if r["expected_amount"]==r["received_amount"] else "확인필요"}</span></td></tr>' for r in rows)
        return page('입금 대사',f'<div class="row"><div><h1>입금 대사</h1><div class="sub">정산 묶음 단위로 예상액·실입금·수수료 차이를 대조합니다.</div></div><a class="btn" href="/payouts.csv">CSV 내려받기</a></div>{f}<section class="panel"><h2>대사 내역</h2><div class="tablewrap"><table><thead><tr><th>정산일</th><th>정산 참조</th><th>채널</th><th>예정액</th><th>실입금</th><th>결제 수수료</th><th>차액</th><th>상태</th></tr></thead><tbody>{tbl or "<tr><td colspan=8>정산 자료가 없습니다.</td></tr>"}</tbody></table></div></section><div class="note">카드·PG 입금은 여러 주문이 한 번에 입금될 수 있으므로 주문 단위가 아닌 정산 참조번호·정산 묶음으로 대사합니다.</div>',user,'reconcile',csrf)
    def settlements(self,user,csrf,selected_period=None):
        period=selected_period or today_kst().strftime('%Y-%m')
        with conn() as c:
            q='SELECT st.*,t.name tenant FROM statements st JOIN tenants t ON t.id=st.tenant_id WHERE st.period=?';args=[period]
            if user['role']=='tenant':q+=' AND st.tenant_id=?';args.append(user['tenant_id'])
            rows=c.execute(q+' ORDER BY t.name',args).fetchall();ts=c.execute('SELECT id,name FROM tenants WHERE active=1'+(' AND id=?' if user['role']=='tenant' else ''),((user['tenant_id'],) if user['role']=='tenant' else ())).fetchall()
        opts=''.join(f'<option value="{t["id"]}">{esc(t["name"])}</option>' for t in ts);gen=f'<section class="panel"><h2>월 정산 스냅샷 생성</h2><form class="inline" method="post" action="/settlements/generate"><input type="hidden" name="csrf" value="{esc(csrf)}"><label>정산월 <input type="month" name="period" value="{period}" required></label><label>업체 <select name="tenant_id"><option value="">전체 업체</option>{opts}</select></label><button class="btn primary">정산 생성</button></form><p class="small">한 번 생성된 명세는 고정됩니다. 이후 환불·정정은 다음 정산 기간의 조정 거래로 반영하세요.</p></section>' if user['role'] in ('admin','finance') else ''
        rowshtml=''.join(f'<tr><td>{esc(r["period"])}</td><td>{esc(r["tenant"])}</td><td>{r["fee_rate"]}%</td><td class="right">{won(r["basis_amount"])}</td><td class="right">{won(r["fee_amount"])}</td><td class="right">{won(r["vat_amount"])}</td><td class="right"><b>{won(r["total_amount"])}</b></td><td><span class="badge {"" if r["status"]=="paid" else "warn"}">{"수납완료" if r["status"]=="paid" else "발행"}</span></td><td>{esc(r["generated_at"][:10])}</td><td>{f'<form method="post" action="/settlements/paid"><input type="hidden" name="csrf" value="{esc(csrf)}"><input type="hidden" name="statement_id" value="{r["id"]}"><button class="btn">수납 처리</button></form>' if user["role"] in ("admin","finance") and r["status"]!="paid" else "—"}</td></tr>' for r in rows)
        picker=f'<form class="inline" method="get" action="/settlements"><label>조회 월 <input type="month" name="period" value="{esc(period)}" required></label><button class="btn">조회</button></form>'
        return page('수수료 정산',f'<div class="row"><div><h1>수수료 정산</h1><div class="sub">계약 수수료율과 매출 이벤트를 기준으로 잠금형 월별 명세를 생성합니다.</div></div><a class="btn" href="/statements.csv">정산 내역 CSV</a></div>{picker}{gen}<section class="panel"><h2>{period} 명세</h2><div class="tablewrap"><table><thead><tr><th>기간</th><th>업체</th><th>율</th><th>수수료 기준액</th><th>수수료</th><th>수수료 부가세</th><th>청구액</th><th>상태</th><th>생성일</th><th>처리</th></tr></thead><tbody>{rowshtml or "<tr><td colspan=10>해당 월에 생성된 정산 명세가 없습니다.</td></tr>"}</tbody></table></div></section><div class="note">공급가액 기준 수수료에 부가세 10% 별도 부과. 과세 거래의 상품 부가세는 거래 입력 시 함께 기록해야 합니다. 면세 입점업체는 업체 설정의 면세 구분을 따릅니다.</div>',user,'settlements',csrf)
    def tenants(self,user,csrf):
        self.require(user,'admin','finance')
        with conn() as c:rows=c.execute('SELECT * FROM tenants ORDER BY id DESC').fetchall()
        form=f'''<section class="panel"><h2>입점업체 등록</h2><form method="post" action="/tenants/add"><input type="hidden" name="csrf" value="{esc(csrf)}"><div class="formgrid"><div><label>업체명</label><input name="name" required></div><div><label>사업자번호</label><input name="business_no"></div><div><label>담당자</label><input name="manager"></div><div><label>연락처</label><input name="phone"></div><div><label>수수료율(%)</label><input type="number" name="fee_rate" min="0" max="100" step="0.01" value="10" required></div><div><label>상품 과세</label><select name="vat_mode"><option value="taxable">과세</option><option value="exempt">면세</option><option value="mixed">혼합·거래별 세액 입력</option></select></div><div><label>정산 계좌(마스킹 권장)</label><input name="bank_account"></div></div><button class="btn primary">업체 저장</button></form></section>'''
        users=('<div class="note">사용자 계정은 서버 명령으로 개설합니다: python app.py create-user 아이디 --role tenant --tenant-id 업체ID</div>' if user['role']=='admin' else '')
        return page('입점업체',form+users+'<section class="panel"><h2>등록 업체</h2><div class="tablewrap"><table><thead><tr><th>ID</th><th>업체</th><th>담당자</th><th>사업자번호</th><th>수수료율</th><th>과세</th><th>정산계좌</th></tr></thead><tbody>'+''.join(f'<tr><td>{r["id"]}</td><td>{esc(r["name"])}</td><td>{esc(r["manager"])}</td><td>{esc(r["business_no"])}</td><td>{r["fee_rate"]}%</td><td>{esc(r["vat_mode"])}</td><td>{esc(r["bank_account"])}</td></tr>' for r in rows)+'</tbody></table></div></section>',user,'tenants',csrf)
    def channels(self,user,csrf):
        self.require(user,'admin','finance')
        with conn() as c:rows=c.execute('SELECT * FROM channels ORDER BY id DESC').fetchall()
        form=f'''<section class="panel"><h2>판매 채널 등록</h2><form class="inline" method="post" action="/channels/add"><input type="hidden" name="csrf" value="{esc(csrf)}"><label>채널명 <input name="name" required></label><label>유형 <select name="channel_type"><option>POS</option><option>LIVE</option><option>ONLINE</option><option>CASH</option><option>OTHER</option></select></label><label>자료 수집 방식 <select name="ingest_mode"><option value="csv">CSV 업로드</option><option value="manual">직접 등록</option><option value="api_pending">API 연동 예정</option></select></label><button class="btn primary">채널 저장</button></form></section>'''
        return page('판매 채널',form+'<section class="panel"><h2>등록 채널</h2><div class="tablewrap"><table><thead><tr><th>ID</th><th>채널명</th><th>유형</th><th>자료 수집</th><th>활성</th></tr></thead><tbody>'+''.join(f'<tr><td>{r["id"]}</td><td>{esc(r["name"])}</td><td>{esc(r["channel_type"])}</td><td>{esc(r["ingest_mode"])}</td><td>{"활성" if r["active"] else "중지"}</td></tr>' for r in rows)+'</tbody></table></div></section>',user,'channels',csrf)
    def audit_page(self,user,csrf):
        self.require(user,'admin')
        with conn() as c:rows=c.execute('SELECT a.*,u.username FROM audit_log a LEFT JOIN users u ON u.id=a.actor_id ORDER BY a.id DESC LIMIT 300').fetchall()
        tbl=''.join(f'<tr><td>{esc(r["created_at"])}</td><td>{esc(r["username"] or "system")}</td><td>{esc(r["action"])}</td><td>{esc(r["object_type"])} #{esc(r["object_id"])}</td><td><code>{esc(r["detail_json"])}</code></td></tr>' for r in rows)
        return page('감사 이력',f'<h1>감사 이력</h1><div class="sub">최근 300건의 사용자·재무 데이터 변경 이력</div><section class="panel"><div class="tablewrap"><table><thead><tr><th>일시</th><th>사용자</th><th>행위</th><th>대상</th><th>상세</th></tr></thead><tbody>{tbl}</tbody></table></div></section>',user,'dashboard',csrf)
    def post(self,path,d,user,start,csrf):
        if path=='/sales/add':
            self.require(user,'admin','finance')
            with conn() as c:
                tenant=c.execute('SELECT * FROM tenants WHERE id=? AND active=1',(int(d['tenant_id']),)).fetchone();channel=c.execute('SELECT * FROM channels WHERE id=? AND active=1',(int(d['channel_id']),)).fetchone()
                if not tenant or not channel:raise ValueError('업체 또는 판매 채널을 확인해 주세요.')
                event_type=d.get('event_type','sale');gross=self.integer(d,'gross_amount');discount=self.integer(d,'discount_amount',0)
                tax_value=d.get('tax_amount','').strip()
                tax=self.integer(d,'tax_amount') if tax_value else (round(gross/11) if tenant['vat_mode']=='taxable' else 0)
                if tenant['vat_mode']=='mixed' and not tax_value:raise ValueError('혼합 과세 업체는 거래별 상품 부가세를 입력해야 합니다.')
                if event_type in ('refund','cancel'):gross=-gross;tax=-tax;discount=-discount
                key=str(uuid.uuid4());self.insert_event(c,tenant,channel,key,d['order_id'],event_type,d['occurred_on'],d.get('product',''),gross,discount,tax,d.get('payment_method',''),d.get('settlement_ref',''),None,user['id']);audit(c,user['id'],'sale_event.append',event_type,key,{'order_id':d['order_id'],'amount':gross})
            return self.redirect(start,'/sales')
        if path=='/sales/import':return self.import_sales(d,user,start)
        if path=='/reconcile/add':
            self.require(user,'admin','finance')
            with conn() as c:
                ch=c.execute('SELECT * FROM channels WHERE id=?',(int(d['channel_id']),)).fetchone()
                if not ch:raise ValueError('채널을 확인해 주세요.')
                vals=(ch['id'],d['settlement_ref'],d['settlement_date'],self.integer(d,'expected_amount'),self.integer(d,'received_amount'),self.integer(d,'provider_fee',0),d.get('bank_ref',''),user['id'],now_iso())
                cur=c.execute('INSERT INTO payout_records(channel_id,settlement_ref,settlement_date,expected_amount,received_amount,provider_fee,bank_ref,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)',vals);audit(c,user['id'],'payout.create','payout',cur.lastrowid,{'settlement_ref':d['settlement_ref'],'expected':vals[3],'received':vals[4]})
            return self.redirect(start,'/reconcile')
        if path=='/reconcile/import':return self.import_payouts(d,user,start)
        if path=='/tenants/add':
            self.require(user,'admin','finance')
            rate=float(d.get('fee_rate','10'))
            if not 0<=rate<=100:raise ValueError('수수료율은 0~100% 범위로 입력해 주세요.')
            with conn() as c:
                cur=c.execute('INSERT INTO tenants(name,business_no,manager,phone,fee_rate,vat_mode,bank_account,created_at) VALUES(?,?,?,?,?,?,?,?)',(d['name'].strip(),d.get('business_no',''),d.get('manager',''),d.get('phone',''),f'{rate:.2f}',d.get('vat_mode','taxable'),d.get('bank_account',''),now_iso()));audit(c,user['id'],'tenant.create','tenant',cur.lastrowid,{'name':d['name'],'fee_rate':rate,'vat_mode':d.get('vat_mode')})
            return self.redirect(start,'/tenants')
        if path=='/channels/add':
            self.require(user,'admin','finance')
            with conn() as c:
                cur=c.execute('INSERT INTO channels(name,channel_type,ingest_mode,created_at) VALUES(?,?,?,?)',(d['name'].strip(),d.get('channel_type','OTHER'),d.get('ingest_mode','csv'),now_iso()));audit(c,user['id'],'channel.create','channel',cur.lastrowid,{'name':d['name'],'type':d.get('channel_type')})
            return self.redirect(start,'/channels')
        if path=='/settlements/generate':
            self.require(user,'admin','finance')
            period=d.get('period','')
            try:startdate=dt.date.fromisoformat(period+'-01');enddate=(startdate.replace(day=28)+dt.timedelta(days=4)).replace(day=1)
            except ValueError:raise ValueError('정산월 형식은 YYYY-MM이어야 합니다.')
            with conn() as c:
                c.execute('BEGIN IMMEDIATE')
                q='SELECT * FROM tenants WHERE active=1';args=[]
                if d.get('tenant_id'):q+=' AND id=?';args.append(int(d['tenant_id']))
                tenants=c.execute(q,args).fetchall();made=0
                for t in tenants:
                    if c.execute('SELECT 1 FROM statements WHERE tenant_id=? AND period=?',(t['id'],period)).fetchone():continue
                    evs=c.execute('SELECT * FROM sale_events WHERE tenant_id=? AND occurred_on>=? AND occurred_on<? ORDER BY id',(t['id'],startdate.isoformat(),enddate.isoformat())).fetchall()
                    basis=0
                    for ev in evs:
                        taxable=(ev['gross_amount']-ev['tax_amount']) if t['vat_mode']!='exempt' else ev['gross_amount']
                        basis+=taxable
                    if not evs:continue
                    if basis<0:raise ValueError(f'{t["name"]}의 {period} 환불액이 매출액을 초과했습니다. 이월 환불 조정을 먼저 확정해 주세요.')
                    rate=float(t['fee_rate']);fee=round(basis*rate/100);vat=round(fee*VAT_RATE/100)
                    cur=c.execute('INSERT INTO statements(tenant_id,period,fee_rate,basis_amount,fee_amount,vat_amount,total_amount,status,generated_by,generated_at) VALUES(?,?,?,?,?,?,?,"issued",?,?)',(t['id'],period,t['fee_rate'],basis,fee,vat,fee+vat,user['id'],now_iso()))
                    c.executemany('INSERT INTO statement_lines(statement_id,sale_event_id,included_amount) VALUES(?,?,?)',[(cur.lastrowid,e['id'],e['gross_amount']-e['tax_amount'] if t['vat_mode']!='exempt' else e['gross_amount']) for e in evs]);audit(c,user['id'],'statement.generate','statement',cur.lastrowid,{'period':period,'tenant_id':t['id'],'basis':basis,'fee':fee,'vat':vat});made+=1
            return self.redirect(start,'/settlements')
        if path=='/settlements/paid':
            self.require(user,'admin','finance');sid=int(d['statement_id'])
            with conn() as c:
                c.execute('BEGIN IMMEDIATE');r=c.execute('SELECT * FROM statements WHERE id=?',(sid,)).fetchone()
                if not r:raise ValueError('정산 명세를 찾을 수 없습니다.')
                if r['status']!='paid':c.execute('UPDATE statements SET status="paid",paid_at=? WHERE id=?',(now_iso(),sid));audit(c,user['id'],'statement.paid','statement',sid,{'amount':r['total_amount']})
            return self.redirect(start,'/settlements')
        return self.error(start,'404 Not Found','요청한 작업을 찾지 못했습니다.',user,csrf)
    @staticmethod
    def integer(d,key,default=None):
        value=d.get(key,'')
        if value=='' and default is not None:return default
        try:n=int(str(value).replace(',','').strip())
        except Exception:raise ValueError(f'{key} 금액은 원 단위 정수로 입력해 주세요.')
        if n<0:raise ValueError(f'{key} 금액은 음수일 수 없습니다.')
        return n
    @staticmethod
    def insert_event(c,tenant,channel,event_key,order_id,event_type,on,product,gross,discount,tax,payment,settlement,batch,user):
        if event_type not in ('sale','refund','cancel'):raise ValueError('event_type은 sale/refund/cancel 중 하나여야 합니다.')
        try:dt.date.fromisoformat(on)
        except ValueError:raise ValueError('발생일은 YYYY-MM-DD 형식이어야 합니다.')
        if event_type=='sale' and (gross<0 or tax<0):raise ValueError('매출 이벤트 금액은 0 이상이어야 합니다.')
        if event_type in ('refund','cancel') and (gross>0 or tax>0):raise ValueError('환불·취소 이벤트는 원장에 음수로 기록해야 합니다.')
        c.execute('INSERT INTO sale_events(tenant_id,channel_id,event_key,order_id,event_type,occurred_on,product,gross_amount,discount_amount,tax_amount,payment_method,settlement_ref,import_batch_id,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(tenant['id'],channel['id'],event_key,order_id,event_type,on,product,gross,discount,tax,payment,settlement,batch,user,now_iso()))
    def import_sales(self,d,user,start):
        self.require(user,'admin','finance');raw=d.get('file','');filename=d.get('file_filename','upload.csv')
        if not raw:raise ValueError('CSV 파일을 선택해 주세요.')
        payload=raw.encode('utf-8');fhash=hashlib.sha256(payload).hexdigest();channel_id=int(d.get('channel_id','0'))
        try:reader=csv.DictReader(io.StringIO(raw));required={'event_id','order_id','event_type','date','tenant','amount','tax_amount'}
        except Exception as e:raise ValueError(f'CSV를 읽지 못했습니다: {e}')
        if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):raise ValueError('필수 CSV 열이 없습니다: '+', '.join(sorted(required)))
        parsed=[];errs=[]
        with conn() as c:
            channel=c.execute('SELECT * FROM channels WHERE id=?',(channel_id,)).fetchone()
            if not channel:raise ValueError('판매 채널을 확인해 주세요.')
            for i,row in enumerate(reader,start=2):
                try:
                    tenant=c.execute('SELECT * FROM tenants WHERE name=? AND active=1',(row['tenant'].strip(),)).fetchone()
                    if not tenant:raise ValueError('입점업체 이름이 등록되지 않았습니다.')
                    typ=row['event_type'].strip().lower();typ={'sale':'sale','매출':'sale','refund':'refund','환불':'refund','cancel':'cancel','취소':'cancel'}.get(typ)
                    if not typ:raise ValueError('event_type은 sale/refund/cancel입니다.')
                    amount=int(row['amount'].replace(',','').strip());tax_raw=(row.get('tax_amount') or '').replace(',','').strip();tax=int(tax_raw) if tax_raw else (round(amount/11) if tenant['vat_mode']=='taxable' else 0);discount=int((row.get('discount') or '0').replace(',','').strip())
                    if tenant['vat_mode']=='mixed' and not tax_raw:raise ValueError('혼합 과세 업체는 tax_amount가 필요합니다.')
                    if amount<0 or tax<0 or discount<0:raise ValueError('CSV 금액은 양수로 입력해야 합니다.')
                    if typ in ('refund','cancel'):amount=-amount;tax=-tax;discount=-discount
                    on=row['date'].strip();dt.date.fromisoformat(on)
                    parsed.append((tenant,channel,row['event_id'].strip(),row['order_id'].strip(),typ,on,row.get('product',''),amount,discount,tax,row.get('payment_method',''),row.get('settlement_ref','')))
                except Exception as e:errs.append(f'{i}행: {e}')
            if errs:raise ValueError('CSV 검증 실패 — 아무 행도 저장하지 않았습니다. '+' / '.join(errs[:8]))
            if c.execute('SELECT 1 FROM import_batches WHERE kind="sales" AND file_hash=?',(fhash,)).fetchone():raise ValueError('같은 파일을 이미 가져왔습니다.')
            c.execute('BEGIN IMMEDIATE');cur=c.execute('INSERT INTO import_batches(kind,filename,file_hash,imported_by,row_count,duplicate_count,created_at) VALUES("sales",?,?,?,?,0,?)',(filename,fhash,user['id'],len(parsed),now_iso()));batch=cur.lastrowid;inserted=0;dupes=0
            for item in parsed:
                tenant,ch,event_id,order_id,typ,on,product,amount,discount,tax,payment,settlement=item
                if not event_id:event_id=hashlib.sha256(json.dumps(item[2:],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
                try:self.insert_event(c,tenant,ch,event_id,order_id,typ,on,product,amount,discount,tax,payment,settlement,batch,user['id']);inserted+=1
                except sqlite3.IntegrityError:
                    dupes+=1
            c.execute('UPDATE import_batches SET row_count=?,duplicate_count=? WHERE id=?',(inserted,dupes,batch));audit(c,user['id'],'sales.import','import_batch',batch,{'filename':filename,'rows':inserted,'duplicates':dupes,'sha256':fhash})
        return self.redirect(start,'/sales')
    def import_payouts(self,d,user,start):
        self.require(user,'admin','finance');raw=d.get('file','');filename=d.get('file_filename','payout.csv')
        if not raw:raise ValueError('정산 CSV 파일을 선택해 주세요.')
        channel_id=int(d.get('channel_id','0'));fhash=hashlib.sha256(raw.encode()).hexdigest()
        reader=csv.DictReader(io.StringIO(raw));required={'settlement_ref','settlement_date','expected_amount','received_amount'}
        if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):raise ValueError('필수 열: '+', '.join(sorted(required)))
        parsed=[]
        for i,r in enumerate(reader,start=2):
            try:
                dt.date.fromisoformat(r['settlement_date']);vals=[int((r.get(k) or '0').replace(',','').strip()) for k in ('expected_amount','received_amount','provider_fee')]
                if min(vals)<0:raise ValueError('금액은 음수일 수 없습니다.')
                parsed.append((r['settlement_ref'].strip(),r['settlement_date'].strip(),*vals,r.get('bank_ref','').strip()))
            except Exception as e:raise ValueError(f'{i}행 검증 오류: {e}; 아무 자료도 저장하지 않았습니다.')
        with conn() as c:
            ch=c.execute('SELECT * FROM channels WHERE id=?',(channel_id,)).fetchone()
            if not ch:raise ValueError('채널을 확인해 주세요.')
            if c.execute('SELECT 1 FROM import_batches WHERE kind="payout" AND file_hash=?',(fhash,)).fetchone():raise ValueError('같은 파일을 이미 가져왔습니다.')
            c.execute('BEGIN IMMEDIATE');cur=c.execute('INSERT INTO import_batches(kind,filename,file_hash,imported_by,row_count,duplicate_count,created_at) VALUES("payout",?,?,?,?,0,?)',(filename,fhash,user['id'],len(parsed),now_iso()));batch=cur.lastrowid;added=0
            for ref,date,expected,received,fee,bank in parsed:
                c.execute('INSERT OR IGNORE INTO payout_records(channel_id,settlement_ref,settlement_date,expected_amount,received_amount,provider_fee,bank_ref,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)',(channel_id,ref,date,expected,received,fee,bank,user['id'],now_iso()));added+=c.execute('SELECT changes()').fetchone()[0]
            audit(c,user['id'],'payout.import','import_batch',batch,{'filename':filename,'rows':added,'input_rows':len(parsed),'sha256':fhash})
        return self.redirect(start,'/reconcile')
    def redirect(self,start,path):start('303 See Other',[('Location',path),('Cache-Control','no-store')]);return [b'']
    def csv_response(self,start,rows,filename):
        safe_rows=[[("'"+v) if isinstance(v,str) and v[:1] in ('=','+','-','@','\\t','\\r') else v for v in row] for row in rows]
        buf=io.StringIO(newline='');w=csv.writer(buf);w.writerows(safe_rows);data='\ufeff'+buf.getvalue();return self.respond(start,'200 OK',data,[('Content-Type','text/csv; charset=utf-8'),('Content-Disposition',f'attachment; filename="{filename}"')])
    def get_csv(self,path,user,start):
        if path=='/payouts.csv': self.require(user,'admin','finance')
        with conn() as c:
            if path=='/sales.csv':
                q='SELECT e.event_key,e.order_id,e.event_type,e.occurred_on,t.name,ch.name,e.product,e.gross_amount,e.discount_amount,e.tax_amount,e.payment_method,e.settlement_ref FROM sale_events e JOIN tenants t ON t.id=e.tenant_id JOIN channels ch ON ch.id=e.channel_id';a=()
                if user['role']=='tenant':q+=' WHERE e.tenant_id=?';a=(user['tenant_id'],)
                rows=c.execute(q+' ORDER BY e.id',a).fetchall();return self.csv_response(start,[['event_id','order_id','event_type','date','tenant','channel','product','amount','discount','tax_amount','payment_method','settlement_ref'],*[list(r) for r in rows]],'sales-ledger.csv')
            if path=='/payouts.csv':
                q='SELECT p.settlement_ref,p.settlement_date,ch.name,p.expected_amount,p.received_amount,p.provider_fee,p.bank_ref FROM payout_records p JOIN channels ch ON ch.id=p.channel_id';a=()
                if user['role']=='tenant':q+=' WHERE EXISTS (SELECT 1 FROM sale_events e WHERE e.channel_id=p.channel_id AND e.tenant_id=?)';a=(user['tenant_id'],)
                rows=c.execute(q+' ORDER BY p.settlement_date',a).fetchall();return self.csv_response(start,[['settlement_ref','settlement_date','channel','expected_amount','received_amount','provider_fee','bank_ref'],*[list(r) for r in rows]],'payout-reconciliation.csv')
            q='SELECT st.period,t.name,st.fee_rate,st.basis_amount,st.fee_amount,st.vat_amount,st.total_amount,st.status FROM statements st JOIN tenants t ON t.id=st.tenant_id';a=()
            if user['role']=='tenant':q+=' WHERE st.tenant_id=?';a=(user['tenant_id'],)
            rows=c.execute(q+' ORDER BY st.period,t.name',a).fetchall();return self.csv_response(start,[['period','tenant','fee_rate','basis_amount','fee_amount','vat_amount','total_amount','status'],*[list(r) for r in rows]],'commission-statements.csv')

def main():
    parser=argparse.ArgumentParser(description='온빌딩 매출·수수료 운영 원장')
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('init-db',help='데이터베이스 초기화')
    u=sub.add_parser('create-user',help='관리자·정산담당·입점업체 사용자 생성');u.add_argument('username');u.add_argument('--role',choices=('admin','finance','tenant'),default='admin');u.add_argument('--tenant-id',type=int)
    srv=sub.add_parser('serve',help='내장 WSGI 서버 실행');srv.add_argument('--host',default=os.environ.get('HOST','127.0.0.1'));srv.add_argument('--port',type=int,default=int(os.environ.get('PORT','8000')))
    backup=sub.add_parser('backup',help='SQLite 온라인 백업');backup.add_argument('--output',required=True)
    args=parser.parse_args();init_db()
    if args.command=='init-db':print(f'Database ready: {DB_PATH}');return
    if args.command=='create-user':
        import getpass
        password=getpass.getpass('새 비밀번호(12자 이상): ');confirm=getpass.getpass('비밀번호 확인: ')
        if password!=confirm:raise SystemExit('비밀번호가 일치하지 않습니다.')
        create_user(args.username,password,args.role,args.tenant_id);print(f'사용자 {args.username} 생성 완료 ({args.role})');return
    if args.command=='backup':
        output=os.path.abspath(args.output);os.makedirs(os.path.dirname(output),exist_ok=True)
        with conn() as source, sqlite3.connect(output) as target: source.backup(target)
        print(f'Backup created: {output}');return
    if args.command=='serve':
        app=App();print(f'온빌딩 운영 원장 실행: http://{args.host}:{args.port} (데이터: {DB_PATH})')
        with make_server(args.host,args.port,app) as server:server.serve_forever()

def bootstrap_first_admin():
    username=os.environ.get('BOOTSTRAP_ADMIN_USERNAME','').strip()
    password=os.environ.get('BOOTSTRAP_ADMIN_PASSWORD','')
    if not username or not password:return
    with conn() as c:
        exists=c.execute('SELECT 1 FROM users LIMIT 1').fetchone()
    if not exists:
        create_user(username,password,'admin')
        print(f'Bootstrap administrator created: {username}')

bootstrap_first_admin()
application=App()
if __name__=='__main__':main()
