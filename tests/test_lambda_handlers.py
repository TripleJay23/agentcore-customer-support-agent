"""
Unit tests for AWS Lambda handlers (Order Tracker & Refund Processor).
These tests validate backend business logic without requiring live AWS infrastructure.
"""

import json
import os
import sys
import unittest

# Ensure the lambda directory is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "lambda")))

import order_tracker
import refund_processor


class MockClientContextCustom:
    def __init__(self, custom_dict):
        self.custom = custom_dict


class MockContext:
    def __init__(self, tool_name: str = ""):
        self.client_context = (
            MockClientContextCustom({"bedrockAgentCoreToolName": tool_name})
            if tool_name
            else None
        )


class TestOrderTrackerLambda(unittest.TestCase):
    """Test suite for the Order Tracker Lambda proxy-integration handler."""

    def test_get_existing_order(self):
        event = {
            "resource": "/orders/{order_id}",
            "httpMethod": "GET",
            "pathParameters": {"order_id": "ORD-001"},
        }
        response = order_tracker.lambda_handler(event, None)
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["order_id"], "ORD-001")
        self.assertEqual(body["status"], "SHIPPED")
        self.assertEqual(body["customer_id"], "CUST-123")

    def test_get_order_case_insensitivity(self):
        event = {
            "resource": "/orders/{order_id}",
            "httpMethod": "GET",
            "pathParameters": {"order_id": "ord-002"},
        }
        response = order_tracker.lambda_handler(event, None)
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["order_id"], "ORD-002")
        self.assertEqual(body["status"], "DELIVERED")

    def test_get_nonexistent_order_returns_404(self):
        event = {
            "resource": "/orders/{order_id}",
            "httpMethod": "GET",
            "pathParameters": {"order_id": "ORD-999"},
        }
        response = order_tracker.lambda_handler(event, None)
        self.assertEqual(response["statusCode"], 404)
        body = json.loads(response["body"])
        self.assertIn("error", body)
        self.assertIn("ORD-999", body["error"])

    def test_get_customer_orders(self):
        event = {
            "resource": "/customers/{customer_id}/orders",
            "httpMethod": "GET",
            "pathParameters": {"customer_id": "CUST-123"},
        }
        response = order_tracker.lambda_handler(event, None)
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["customer_id"], "CUST-123")
        self.assertEqual(len(body["orders"]), 2)

    def test_get_customer_orders_not_found(self):
        event = {
            "resource": "/customers/{customer_id}/orders",
            "httpMethod": "GET",
            "pathParameters": {"customer_id": "CUST-UNKNOWN"},
        }
        response = order_tracker.lambda_handler(event, None)
        self.assertEqual(response["statusCode"], 404)

    def test_get_customer_profile(self):
        event = {
            "resource": "/customers/{customer_id}",
            "httpMethod": "GET",
            "pathParameters": {"customer_id": "CUST-123"},
        }
        response = order_tracker.lambda_handler(event, None)
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["name"], "Jane Smith")
        self.assertEqual(body["tier"], "Gold")
        self.assertEqual(body["loyalty_points"], 4250)

    def test_unrecognized_route_returns_400(self):
        event = {
            "resource": "/unknown/route",
            "httpMethod": "GET",
            "pathParameters": {},
        }
        response = order_tracker.lambda_handler(event, None)
        self.assertEqual(response["statusCode"], 400)
        body = json.loads(response["body"])
        self.assertIn("error", body)


class TestRefundProcessorLambda(unittest.TestCase):
    """Test suite for the Refund Processor Gateway tool handler."""

    def test_initiate_refund(self):
        context = MockContext("CustomerSupport___initiate_refund")
        event = {
            "order_id": "ORD-002",
            "amount": 139.99,
            "reason": "Customer returned item",
        }
        response = refund_processor.lambda_handler(event, context)
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertTrue(body["refund_id"].startswith("REF-"))
        self.assertEqual(body["order_id"], "ORD-002")
        self.assertEqual(body["status"], "APPROVED")
        self.assertEqual(body["amount"], 139.99)
        self.assertIn("Refund approved", body["message"])

    def test_check_refund_status(self):
        context = MockContext("check_refund_status")
        event = {"refund_id": "REF-12345678"}
        response = refund_processor.lambda_handler(event, context)
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["refund_id"], "REF-12345678")
        self.assertEqual(body["status"], "PROCESSING")
        self.assertEqual(body["eta"], "2-3 business days")

    def test_get_return_label(self):
        context = MockContext("Target___get_return_label")
        event = {"order_id": "ORD-001"}
        response = refund_processor.lambda_handler(event, context)
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["order_id"], "ORD-001")
        self.assertIn("ORD-001", body["label_url"])
        self.assertEqual(body["carrier"], "UPS")

    def test_unknown_tool_returns_400(self):
        context = MockContext("unsupported_action")
        event = {}
        response = refund_processor.lambda_handler(event, context)
        self.assertEqual(response["statusCode"], 400)
        body = json.loads(response["body"])
        self.assertIn("error", body)


if __name__ == "__main__":
    unittest.main()
