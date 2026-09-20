"""
Unit Tests for Loyalty Calculation Business Logic
=================================================
Validates the deterministic loyalty calculation helper:
  - Multi-tier discounts (Silver, Gold, Platinum, unknown)
  - Points redemption in 500-point increments
  - 50% order total redemption cap
  - Multi-category earn rates (standard, device, fresh, unknown)
  - Safe handling of negative inputs
  - Cross-session points retention & accrual

These tests are fully offline and do not require AWS credentials or services.
"""

import unittest
from main import calculate_loyalty_values


class TestLoyaltyCalculation(unittest.TestCase):
    """Offline unit tests for calculate_loyalty_values business logic."""

    def test_gold_tier_standard_order(self):
        """
        Scenario 1: Gold tier with 4,000 points, $200 order, standard category.
        Expected verified behavior:
          - points_redeemed = 4000
          - points_discount = 40.00
          - tier_discount_rate = 0.10
          - tier_discount = 16.00 (10% of $160 subtotal after points)
          - final_total = 144.00
          - total_savings = 56.00
          - points_earned = 144
          - remaining_points = 144 (4000 - 4000 + 144)
        """
        res = calculate_loyalty_values(
            loyalty_points=4000,
            tier="Gold",
            order_total=200.0,
            product_category="standard",
        )
        self.assertEqual(res["loyalty_points"], 4000)
        self.assertEqual(res["tier"], "Gold")
        self.assertEqual(res["order_total"], 200.0)
        self.assertEqual(res["product_category"], "standard")
        self.assertEqual(res["earn_rate"], 1)
        self.assertEqual(res["points_redeemed"], 4000)
        self.assertEqual(res["points_discount"], 40.0)
        self.assertEqual(res["tier_discount_rate"], 0.10)
        self.assertEqual(res["tier_discount"], 16.0)
        self.assertEqual(res["final_total"], 144.0)
        self.assertEqual(res["total_savings"], 56.0)
        self.assertEqual(res["points_earned"], 144)
        self.assertEqual(res["remaining_points"], 144)

    def test_verified_execution_starting_points_retained(self):
        """
        Scenario 1b: Verified execution from deployment logs where customer
        profile had 4,250 points, redeeming 4,000 points on a $200 Gold order.
        Remaining points: 4,250 - 4,000 + 144 = 394 points.
        """
        res = calculate_loyalty_values(
            loyalty_points=4250,
            tier="Gold",
            order_total=200.0,
            product_category="standard",
        )
        self.assertEqual(res["points_redeemed"], 4000)
        self.assertEqual(res["points_earned"], 144)
        self.assertEqual(res["remaining_points"], 394)

    def test_point_redemption_capped_at_fifty_percent(self):
        """
        Scenario 2: Point redemption can cover at most 50% of order total.
        Order total: $100.00 -> 50% cap is $50.00 (5,000 points).
        Customer has 10,000 points balance.
        Redemption must be capped at 5,000 points ($50.00).
        """
        res = calculate_loyalty_values(
            loyalty_points=10000,
            tier="Gold",
            order_total=100.0,
            product_category="standard",
        )
        self.assertEqual(res["points_redeemed"], 5000)
        self.assertEqual(res["points_discount"], 50.0)
        # Subtotal after points: 100 - 50 = 50.0
        # Tier discount (10%): 5.0
        # Final total: 45.0
        self.assertEqual(res["final_total"], 45.0)

    def test_points_rounded_down_to_500_blocks(self):
        """
        Scenario 3: Points can only be redeemed in blocks of 500.
        Customer has 1,499 points -> redeemable from balance is 1,000 points.
        """
        res = calculate_loyalty_values(
            loyalty_points=1499,
            tier="Silver",
            order_total=100.0,
            product_category="standard",
        )
        self.assertEqual(res["points_redeemed"], 1000)
        self.assertEqual(res["points_discount"], 10.0)
        # Remaining: 1499 - 1000 + 90 (earned on 90.0) = 589
        self.assertEqual(res["remaining_points"], 589)

    def test_silver_tier_zero_discount(self):
        """
        Scenario 4: Silver tier receives 0% tier discount rate.
        """
        res = calculate_loyalty_values(
            loyalty_points=1000,
            tier="Silver",
            order_total=100.0,
            product_category="standard",
        )
        self.assertEqual(res["tier_discount_rate"], 0.0)
        self.assertEqual(res["tier_discount"], 0.0)
        self.assertEqual(res["final_total"], 90.0)
        self.assertEqual(res["total_savings"], 10.0)

    def test_platinum_tier_fifteen_percent(self):
        """
        Scenario 5: Platinum tier receives 15% tier discount on post-point subtotal.
        """
        res = calculate_loyalty_values(
            loyalty_points=0,
            tier="Platinum",
            order_total=100.0,
            product_category="standard",
        )
        self.assertEqual(res["tier_discount_rate"], 0.15)
        self.assertEqual(res["tier_discount"], 15.0)
        self.assertEqual(res["final_total"], 85.0)
        self.assertEqual(res["total_savings"], 15.0)
        self.assertEqual(res["points_earned"], 85)
        self.assertEqual(res["remaining_points"], 85)

    def test_negative_inputs_handled_safely(self):
        """
        Scenario 6: Negative points and order totals are clamped to 0.
        """
        res = calculate_loyalty_values(
            loyalty_points=-500,
            tier="Gold",
            order_total=-150.0,
            product_category="standard",
        )
        self.assertEqual(res["loyalty_points"], 0)
        self.assertEqual(res["order_total"], 0.0)
        self.assertEqual(res["points_redeemed"], 0)
        self.assertEqual(res["points_discount"], 0.0)
        self.assertEqual(res["tier_discount"], 0.0)
        self.assertEqual(res["final_total"], 0.0)
        self.assertEqual(res["total_savings"], 0.0)
        self.assertEqual(res["points_earned"], 0)
        self.assertEqual(res["remaining_points"], 0)

    def test_unknown_product_category_fallback(self):
        """
        Scenario 7: Unknown product categories fall back to standard earn rate (1).
        Also validates known category earn rates: device (2), fresh (5).
        """
        res_unknown = calculate_loyalty_values(
            loyalty_points=0,
            tier="Silver",
            order_total=100.0,
            product_category="office_supplies",
        )
        self.assertEqual(res_unknown["earn_rate"], 1)
        self.assertEqual(res_unknown["points_earned"], 100)

        res_device = calculate_loyalty_values(
            loyalty_points=0,
            tier="Silver",
            order_total=100.0,
            product_category="device",
        )
        self.assertEqual(res_device["earn_rate"], 2)
        self.assertEqual(res_device["points_earned"], 200)

        res_fresh = calculate_loyalty_values(
            loyalty_points=0,
            tier="Silver",
            order_total=100.0,
            product_category="fresh",
        )
        self.assertEqual(res_fresh["earn_rate"], 5)
        self.assertEqual(res_fresh["points_earned"], 500)

    def test_unknown_tier_receives_zero_discount(self):
        """
        Scenario 8: Unknown tier string receives 0% tier discount rate.
        """
        res = calculate_loyalty_values(
            loyalty_points=0,
            tier="Diamond",
            order_total=100.0,
            product_category="standard",
        )
        self.assertEqual(res["tier_discount_rate"], 0.0)
        self.assertEqual(res["tier_discount"], 0.0)
        self.assertEqual(res["final_total"], 100.0)
        self.assertEqual(res["total_savings"], 0.0)


if __name__ == "__main__":
    unittest.main()
