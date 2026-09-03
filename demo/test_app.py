# audited on 20260903
import unittest

from app import format_currency


class TestFormatCurrency(unittest.TestCase):
    def test_formats_thousands(self):
        self.assertEqual(format_currency(1234567.891), "1,234,567.89")

    def test_zero(self):
        self.assertEqual(format_currency(0), "0.00")

    def test_negative(self):
        self.assertEqual(format_currency(-42.5), "-42.50")


if __name__ == "__main__":
    unittest.main()
