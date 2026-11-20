"""核对原始业务资料之间的引用与记录形式。"""

import unittest

from inspect_samples import read_samples


class SampleTests(unittest.TestCase):
    def setUp(self):
        self.data = read_samples()

    def test_bundle_uses_catalog_units(self):
        quotes = {(row['week'], row['shop'], row['item']): row for row in self.data['quotes']}
        for bundle in self.data['discounts']:
            for item in bundle['units']:
                self.assertIn((bundle['week'], bundle['shop'], item), quotes)
        for correction in self.data['corrections']:
            record = quotes[correction['week'], correction['shop'], correction['item']]
            self.assertNotEqual(record['grams'], correction['grams'])
            self.assertLess(record['week'], correction['received'])

    def test_out_of_stock_is_not_missing_quote(self):
        statuses = {row['status'] for row in self.data['quotes']}
        self.assertIn('缺货', statuses)
        self.assertIn('未采价', statuses)

    def test_store_correction_and_coupon_events_link_to_consistent_keys(self):
        data = self.data
        for correction in data['corrections']:
            district = data['shops'][correction['shop']]['district']
            self.assertTrue(any(entry['week']==correction['week'] and entry['scope']==district for entry in data['publication']))
        claims = {claim['id'] for claim in data['claims']}
        for event in data['redemptions']:
            self.assertIn(event['claim'], claims)
        self.assertTrue(any(event['kind']=='已取消' and any(earlier['id']==event['id'] and earlier['day']<event['day'] and earlier['kind']=='已送达' for earlier in data['redemptions']) for event in data['redemptions']))
