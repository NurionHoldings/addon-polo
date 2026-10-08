import io
import os
import re
import sys
import tempfile
import unittest
import urllib.parse

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='onbuilding-tests-')
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import app


class OperationalFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.create_user('owner', 'LongSecurePass123!', 'admin')
        with app.conn() as c:
            cls.tenant_id = c.execute(
                "INSERT INTO tenants(name,fee_rate,vat_mode,created_at) VALUES(?,?,?,?)",
                ('테스트 입점사', '10.00', 'taxable', app.now_iso()),
            ).lastrowid
            cls.channel_id = c.execute(
                "INSERT INTO channels(name,channel_type,created_at) VALUES(?,?,?)",
                ('공용 POS', 'POS', app.now_iso()),
            ).lastrowid
        app.create_user('vendor', 'LongSecurePass456!', 'tenant', cls.tenant_id)
        app.create_user('finance', 'LongSecurePass789!', 'finance')
        cls.wsgi = app.application

    def request(self, path, method='GET', data=None, cookie=''):
        parsed = urllib.parse.urlsplit(path)
        body = urllib.parse.urlencode(data or {}).encode()
        environ = {
            'PATH_INFO': parsed.path,
            'QUERY_STRING': parsed.query,
            'REQUEST_METHOD': method,
            'CONTENT_LENGTH': str(len(body)),
            'CONTENT_TYPE': 'application/x-www-form-urlencoded',
            'wsgi.input': io.BytesIO(body),
            'HTTP_COOKIE': cookie,
        }
        result = {}
        def start(status, headers):
            result['status'], result['headers'] = status, headers
        content = b''.join(self.wsgi(environ, start)).decode('utf-8')
        result['body'] = content
        return result

    def login(self, username, password, next_path='/dashboard'):
        first = self.request('/login?next=' + urllib.parse.quote(next_path, safe='/'))
        csrf = re.search(r'name="csrf" value="([^"]+)', first['body']).group(1)
        anonymous_cookie = next(v for k, v in first['headers'] if k == 'Set-Cookie').split(';')[0]
        logged = self.request('/login', 'POST', {
            'csrf': csrf, 'username': username, 'password': password,
            'next': next_path,
        }, anonymous_cookie)
        cookie = next(v for k, v in logged['headers'] if k == 'Set-Cookie').split(';')[0]
        return cookie

    def multipart(self, path, fields, filename, content, cookie):
        boundary = 'codex-test-boundary'
        parts = []
        for name, value in fields.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n')
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: text/csv\r\n\r\n{content}\r\n')
        parts.append(f'--{boundary}--\r\n')
        body = ''.join(parts).encode()
        environ = {
            'PATH_INFO': path, 'REQUEST_METHOD': 'POST',
            'CONTENT_LENGTH': str(len(body)),
            'CONTENT_TYPE': f'multipart/form-data; boundary={boundary}',
            'wsgi.input': io.BytesIO(body), 'HTTP_COOKIE': cookie,
        }
        result = {}
        def start(status, headers): result['status'], result['headers'] = status, headers
        result['body'] = b''.join(self.wsgi(environ, start)).decode()
        return result

    def test_financial_flow_and_tenant_scope(self):
        admin_cookie = self.login('owner', 'LongSecurePass123!')
        sales = self.request('/sales', cookie=admin_cookie)
        csrf = re.search(r'name="csrf" value="([^"]+)', sales['body']).group(1)
        saved = self.request('/sales/add', 'POST', {
            'csrf': csrf,
            'tenant_id': self.tenant_id,
            'channel_id': self.channel_id,
            'order_id': 'POS-TEST-1',
            'event_type': 'sale',
            'occurred_on': '2026-10-08',
            'gross_amount': '11000',
            'tax_amount': '',
            'product': '테스트 상품',
        }, admin_cookie)
        self.assertTrue(saved['status'].startswith('303'))
        with app.conn() as c:
            sale = c.execute('SELECT * FROM sale_events WHERE order_id=?', ('POS-TEST-1',)).fetchone()
            self.assertEqual((sale['gross_amount'], sale['tax_amount']), (11000, 1000))

        statements = self.request('/settlements', cookie=admin_cookie)
        csrf = re.search(r'name="csrf" value="([^"]+)', statements['body']).group(1)
        generated = self.request('/settlements/generate', 'POST', {
            'csrf': csrf, 'period': '2026-10', 'tenant_id': '',
        }, admin_cookie)
        self.assertTrue(generated['status'].startswith('303'))
        with app.conn() as c:
            statement = c.execute('SELECT * FROM statements WHERE tenant_id=?', (self.tenant_id,)).fetchone()
            self.assertEqual((statement['basis_amount'], statement['fee_amount'], statement['vat_amount'], statement['total_amount']), (10000, 1000, 100, 1100))

        vendor_cookie = self.login('vendor', 'LongSecurePass456!')
        vendor_sales = self.request('/sales', cookie=vendor_cookie)
        self.assertIn('POS-TEST-1', vendor_sales['body'])
        self.assertTrue(self.request('/tenants', cookie=vendor_cookie)['status'].startswith('403'))
        self.assertTrue(self.request('/reconcile', cookie=vendor_cookie)['status'].startswith('403'))
        self.assertTrue(self.request('/connectors', cookie=vendor_cookie)['status'].startswith('403'))

    def test_arkaon_analysis_hides_other_tenants_amounts(self):
        with app.conn() as c:
            other_id=c.execute(
                "INSERT INTO tenants(name,fee_rate,vat_mode,created_at) VALUES(?,?,?,?)",
                ('다른 업체 분석격리', '15.00', 'taxable', app.now_iso()),
            ).lastrowid
            other=c.execute('SELECT * FROM tenants WHERE id=?',(other_id,)).fetchone()
            channel=c.execute('SELECT * FROM channels WHERE id=?',(self.channel_id,)).fetchone()
            today=app.today_kst().isoformat()
            app.App.insert_event(c,other,channel,'FOREIGN-ANALYTICS','FOREIGN-ORDER','sale',today,'',987654,0,0,'card','',None,1)
        vendor_cookie=self.login('vendor','LongSecurePass456!')
        response=self.request('/arkaon',cookie=vendor_cookie)
        self.assertNotIn('987,654원',response['body'])

    def test_connector_readiness_page_loads_catalog_for_staff(self):
        admin_cookie=self.login('owner','LongSecurePass123!')
        response=self.request('/connectors',cookie=admin_cookie)
        self.assertTrue(response['status'].startswith('200'))
        self.assertIn('공용 POS / VAN',response['body'])
        self.assertIn('공식 문서',response['body'])

    def test_public_home_and_admin_console_access_boundary(self):
        home=self.request('/')
        self.assertTrue(home['status'].startswith('200'))
        self.assertIn('온빌딩 | 입점업체 매출·수수료 관리',home['body'])
        self.assertIn('관리자 전용 페이지',home['body'])

        anonymous=self.request('/admin')
        self.assertTrue(anonymous['status'].startswith('303'))
        self.assertIn('/login?next=/admin',dict(anonymous['headers'])['Location'])

        admin_cookie=self.login('owner','LongSecurePass123!','/admin')
        admin=self.request('/admin',cookie=admin_cookie)
        self.assertTrue(admin['status'].startswith('200'))
        self.assertIn('관리자 전용 운영실',admin['body'])
        self.assertIn('감사 이력 보기',admin['body'])

        finance_cookie=self.login('finance','LongSecurePass789!','/admin')
        denied=self.request('/admin',cookie=finance_cookie)
        self.assertTrue(denied['status'].startswith('403'))
        self.assertIn('권한이 없습니다',denied['body'])

    def test_financial_mutations_require_csrf(self):
        cookie = self.login('owner', 'LongSecurePass123!')
        denied = self.request('/sales/add', 'POST', {
            'tenant_id': self.tenant_id, 'channel_id': self.channel_id,
            'order_id': 'NO-CSRF', 'event_type': 'sale',
            'occurred_on': '2026-10-08', 'gross_amount': '1000',
        }, cookie)
        self.assertTrue(denied['status'].startswith('403'))
        with app.conn() as c:
            self.assertIsNone(c.execute('SELECT 1 FROM sale_events WHERE order_id=?', ('NO-CSRF',)).fetchone())

    def test_sales_csv_is_validated_and_idempotent(self):
        cookie = self.login('owner', 'LongSecurePass123!')
        page = self.request('/sales', cookie=cookie)
        csrf = re.search(r'name="csrf" value="([^"]+)', page['body']).group(1)
        csv_text = (
            'event_id,order_id,event_type,date,tenant,amount,tax_amount\n'
            f'CSV-1,WEB-1,sale,2026-10-07,테스트 입점사,11000,1000\n'
            f'CSV-1,WEB-1,sale,2026-10-07,테스트 입점사,11000,1000\n'
        )
        fields = {'csrf': csrf, 'channel_id': str(self.channel_id)}
        response = self.multipart('/sales/import', fields, 'sales.csv', csv_text, cookie)
        self.assertTrue(response['status'].startswith('303'))
        with app.conn() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM sale_events WHERE event_key='CSV-1'").fetchone()[0], 1)
            batch = c.execute("SELECT row_count,duplicate_count FROM import_batches WHERE kind='sales'").fetchone()
            self.assertEqual(tuple(batch), (1, 1))
        repeated = self.multipart('/sales/import', fields, 'sales.csv', csv_text, cookie)
        self.assertTrue(repeated['status'].startswith('400'))
        bad_csv = 'event_id,order_id,event_type,date,tenant,amount,tax_amount\nX,WEB-2,sale,bad-date,테스트 입점사,1000,0\n'
        rejected = self.multipart('/sales/import', fields, 'bad.csv', bad_csv, cookie)
        self.assertTrue(rejected['status'].startswith('400'))
        with app.conn() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM sale_events WHERE event_key='X'").fetchone())

    def test_csv_exports_escape_spreadsheet_formulas(self):
        response = {}
        def start(status, headers): response['status'], response['headers'] = status, headers
        result = b''.join(app.application.csv_response(
            start, [['=1+1', '+cmd', '-cmd', '@cmd', '\tcmd', '\rcmd', 'safe']], 'test.csv'
        )).decode('utf-8-sig')
        self.assertTrue(response['status'].startswith('200'))
        for formula in ('=1+1', '+cmd', '-cmd', '@cmd', '\tcmd', '\rcmd'):
            self.assertIn("'" + formula, result)
        self.assertIn('safe', result)


if __name__ == '__main__':
    unittest.main()
