import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

from connectors.normalize import MappingError, normalize_sales, normalize_settlement


class ConnectorNormalizationTest(unittest.TestCase):
    def test_sales_map_converts_partner_statuses_and_signed_refunds(self):
        rows=normalize_sales([
            {'id':'source-1','order':{'id':'order-1'},'state':'CAPTURED','paid':'11,000','vat':'1,000','date':'2026-10-08T01:02:03Z'},
            {'id':'source-2','order':{'id':'order-1'},'state':'PARTIAL_REFUND','paid':'-5,500','vat':'-500','date':'2026-10-08'},
        ],{
            'event_id':'id','order_id':'order.id','event_type':'state','amount':'paid','tax_amount':'vat','date':'date',
        },tenant_name='테스트 입점사',status_map={'captured':'sale','partial_refund':'refund'},tax_mode='mixed')
        self.assertEqual((rows[0]['event_type'],rows[0]['amount'],rows[0]['tax_amount']),('sale','11000','1000'))
        self.assertEqual((rows[1]['event_type'],rows[1]['amount'],rows[1]['tax_amount']),('refund','5500','500'))
        self.assertEqual(rows[0]['tenant'],'테스트 입점사')

    def test_mixed_tax_missing_id_and_decimal_amount_fail_closed(self):
        mapping={'event_id':'id','order_id':'order_id','event_type':'state','amount':'paid','date':'date'}
        with self.assertRaisesRegex(MappingError,'혼합과세'):
            normalize_sales([{'id':'a','order_id':'o','state':'sale','paid':1000,'date':'2026-10-08'}],mapping,tenant_name='업체',tax_mode='mixed')
        with self.assertRaisesRegex(MappingError,'필드를 찾을 수 없습니다'):
            normalize_sales([{'order_id':'o','state':'sale','paid':1000,'date':'2026-10-08','vat':0}],{**mapping,'tax_amount':'vat'},tenant_name='업체')
        with self.assertRaisesRegex(MappingError,'정수'):
            normalize_sales([{'id':'a','order_id':'o','state':'sale','paid':'1.5','date':'2026-10-08','vat':0}],{**mapping,'tax_amount':'vat'},tenant_name='업체')

    def test_settlement_normalizer_and_catalog(self):
        normalized=normalize_settlement({'payout':{'id':'set-1','day':'2026-10-08','expected':'10000','fee':'300'},'bank':{'amount':'9700','ref':'bank-1'}},{
            'settlement_ref':'payout.id','settlement_date':'payout.day','expected_amount':'payout.expected','received_amount':'bank.amount','provider_fee':'payout.fee','bank_ref':'bank.ref'
        })
        self.assertEqual(normalized['expected_amount'],'10000')
        self.assertEqual(normalized['received_amount'],'9700')
        catalog=json.loads(pathlib.Path('connectors/catalog.json').read_text(encoding='utf-8'))
        self.assertEqual([x['id'] for x in catalog['connectors']],['venue-pos','commerce-live','pg-card-settlement','bank-deposit'])
        self.assertFalse(catalog['security_defaults']['store_provider_passwords_in_app'])

    def test_cli_writes_import_ready_sales_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp)
            (root/'source.json').write_text(json.dumps([{'id':'a','order':'o-1','state':'paid','paid':'2200','tax':'200','date':'2026-10-08'}]),encoding='utf-8')
            (root/'mapping.json').write_text(json.dumps({'sales_field_map':{'event_id':'id','order_id':'order','event_type':'state','amount':'paid','tax_amount':'tax','date':'date'},'status_map':{'paid':'sale'}}),encoding='utf-8')
            output=root/'normalized.csv'
            result=subprocess.run([sys.executable,'-m','connectors.normalize','--kind','sales','--input',str(root/'source.json'),'--mapping',str(root/'mapping.json'),'--tenant','테스트 업체','--tax-mode','mixed','--output',str(output)],capture_output=True,text=True,check=False)
            self.assertEqual(result.returncode,0,result.stderr)
            with output.open(encoding='utf-8-sig',newline='') as f:
                rows=list(__import__('csv').DictReader(f))
            self.assertEqual((rows[0]['event_id'],rows[0]['event_type'],rows[0]['amount']),('a','sale','2200'))


if __name__=='__main__':
    unittest.main()
