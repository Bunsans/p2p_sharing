import unittest
from datetime import timedelta

from payments.calculations import (
    additional_half_hours,
    rental_price,
    split_deposit,
    split_rent_refund,
)


class RentalPriceTests(unittest.TestCase):
    def test_adds_twenty_percent_to_owner_price(self):
        price = rental_price(80_000)
        self.assertEqual(price.owner_kopecks, 80_000)
        self.assertEqual(price.commission_kopecks, 16_000)
        self.assertEqual(price.total_kopecks, 96_000)

    def test_rounds_to_nearest_kopeck(self):
        for owner, commission in ((0, 0), (1, 0), (2, 0), (3, 1), (7, 1), (8, 2)):
            with self.subTest(owner=owner):
                self.assertEqual(rental_price(owner), (owner, commission, owner + commission))

    def test_keeps_large_amounts_exact(self):
        base = 10**40
        self.assertEqual(rental_price(5 * base + 3), (5 * base + 3, base + 1, 6 * base + 4))

    def test_rejects_invalid_amounts(self):
        with self.assertRaises(ValueError):
            rental_price(-1)
        for value in (None, True, False, 1.0, "1"):
            with self.subTest(value=value), self.assertRaises(TypeError):
                rental_price(value)


class SplitRentRefundTests(unittest.TestCase):
    def test_splits_half_and_remaining_refund(self):
        for refunded_before in (0, 48_000):
            with self.subTest(refunded_before=refunded_before):
                split = split_rent_refund(80_000, 16_000, refunded_before, 48_000)
                self.assertEqual(split.owner_kopecks, 40_000)
                self.assertEqual(split.commission_kopecks, 8_000)

    def test_rounds_owner_ties_up_and_assigns_remainder_to_commission(self):
        self.assertEqual(split_rent_refund(1, 1, 0, 1), (1, 0))
        self.assertEqual(split_rent_refund(1, 1, 1, 1), (0, 1))

    def test_rounds_cumulatively_instead_of_each_refund(self):
        splits = [split_rent_refund(3, 1, before, 1) for before in range(4)]
        self.assertEqual(splits, [(1, 0), (1, 0), (0, 1), (1, 0)])

    def test_repeated_refunds_exhaust_original_shares_without_negative_deltas(self):
        for owner in range(11):
            for commission in range(11):
                if owner + commission == 0:
                    continue
                returned_owner = returned_commission = 0
                for before in range(owner + commission):
                    split = split_rent_refund(owner, commission, before, 1)
                    self.assertEqual(sum(split), 1)
                    self.assertGreaterEqual(split.owner_kopecks, 0)
                    self.assertGreaterEqual(split.commission_kopecks, 0)
                    returned_owner += split.owner_kopecks
                    returned_commission += split.commission_kopecks
                self.assertEqual((returned_owner, returned_commission), (owner, commission))

    def test_zero_refund_and_single_recipient(self):
        for before in (0, 50, 100):
            self.assertEqual(split_rent_refund(80, 20, before, 0), (0, 0))
        self.assertEqual(split_rent_refund(100, 0, 25, 75), (75, 0))
        self.assertEqual(split_rent_refund(0, 100, 25, 75), (0, 75))

    def test_keeps_large_refund_ties_exact(self):
        share = 10**40 + 1
        self.assertEqual(split_rent_refund(share, share, 0, 1), (1, 0))
        self.assertEqual(split_rent_refund(share, share, 1, 2 * share - 1), (share - 1, share))

    def test_rejects_zero_original_total_and_excessive_refunds(self):
        for amounts in ((0, 0, 0, 0), (80, 20, 90, 11), (80, 20, 101, 0)):
            with self.subTest(amounts=amounts), self.assertRaises(ValueError):
                split_rent_refund(*amounts)

    def test_rejects_invalid_amounts_in_every_position(self):
        for position in range(4):
            for value in (None, True, False, 1.0, "1", -1):
                amounts = [80, 20, 0, 1]
                amounts[position] = value
                error = ValueError if value == -1 else TypeError
                with self.subTest(amounts=amounts), self.assertRaises(error):
                    split_rent_refund(*amounts)


class SplitDepositTests(unittest.TestCase):
    def test_distribution_keeps_exact_kopecks(self):
        for deposit, compensation, refund in (
            (300_000, 50_000, 250_000),
            (0, 0, 0),
            (300_000, 0, 300_000),
            (300_000, 300_000, 0),
            (50, 50, 0),
            (100, 50, 50),
        ):
            with self.subTest(deposit=deposit, compensation=compensation):
                result = split_deposit(deposit, compensation)
                self.assertEqual(result.compensation_kopecks, compensation)
                self.assertEqual(result.refund_kopecks, refund)

    def test_distribution_conserves_deposit(self):
        for deposit in range(21):
            for compensation in range(deposit + 1):
                result = split_deposit(deposit, compensation)
                self.assertEqual(sum(result), deposit)
                self.assertGreaterEqual(result.refund_kopecks, 0)

    def test_rejects_negative_or_excessive_amounts(self):
        for amounts in ((-1, 0), (1, -1), (-1, -1), (0, 1), (100, 101)):
            with self.subTest(amounts=amounts), self.assertRaises(ValueError):
                split_deposit(*amounts)

    def test_rejects_non_integer_amounts(self):
        for value in (None, True, False, 1.0, "1"):
            for amounts in ((value, 0), (100, value)):
                with self.subTest(amounts=amounts), self.assertRaises(TypeError):
                    split_deposit(*amounts)


class AdditionalHalfHoursTests(unittest.TestCase):
    def test_rounds_cumulative_time_up_at_exact_boundaries(self):
        half_hour = timedelta(minutes=30)
        microsecond = timedelta(microseconds=1)
        for duration, expected in (
            (timedelta(), 0),
            (microsecond, 1),
            (half_hour - microsecond, 1),
            (half_hour, 1),
            (half_hour + microsecond, 2),
            (timedelta(hours=1), 2),
            (timedelta(days=999_999_999, minutes=30, microseconds=1),
             999_999_999 * 48 + 2),
        ):
            with self.subTest(duration=duration):
                self.assertEqual(additional_half_hours(duration), expected)

    def test_extension_and_delay_share_one_period(self):
        extension = timedelta(minutes=10)
        delay = timedelta(minutes=10)
        self.assertEqual(additional_half_hours(extension + delay), 1)

    def test_rejects_negative_duration(self):
        for duration in (timedelta(microseconds=-1), timedelta(days=-1)):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                additional_half_hours(duration)

    def test_rejects_non_timedelta_values(self):
        for value in (None, True, 0, 1.0, "00:10:00"):
            with self.subTest(value=value), self.assertRaises(TypeError):
                additional_half_hours(value)
