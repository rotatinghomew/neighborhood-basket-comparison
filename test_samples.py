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
