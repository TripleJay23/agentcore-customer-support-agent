"""
AWS Bedrock AgentCore Customer Support Agent
===========================================
A multi-capability AI customer-support system built with Amazon Bedrock
AgentCore, Strands Agents, MCP Gateway, Knowledge Bases, and Memory.

Capabilities:
  1. Order tracking & customer lookup via AgentCore Gateway (MCP)
  2. Refund processing & return shipping labels via Gateway Lambda tools
  3. Product catalog & policy retrieval via Bedrock Knowledge Bases (RAG)
  4. Cross-session customer facts & preferences via AgentCore Memory
  5. Deterministic loyalty discount math via AgentCore Code Interpreter
  6. Live web content retrieval via AgentCore Browser
"""

import json
import logging
import math
import os
from typing import Any, Dict, List, Optional
import uuid

import boto3
from bedrock_agentcore.memory import MemoryClient
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.tools.code_interpreter_client import code_session
from mcp.client.streamable_http import streamable_http_client
from strands import Agent, tool
from strands.hooks import (
    AfterInvocationEvent,
    HookProvider,
    HookRegistry,
    MessageAddedEvent,
)
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient

try:
    from strands_tools.browser import AgentCoreBrowser
except ImportError:
    AgentCoreBrowser = None  # type: ignore

# Load local environment variables if python-dotenv is present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("CSAI_Agent")

# App Initialization (registers ASGI server for AgentCore deployment)
app = BedrockAgentCoreApp()

# Suppress interactive tool-consent prompts (required in headless deployments)
os.environ["BYPASS_TOOL_CONSENT"] = "true"

# ── Configuration ─────────────────────────────────────────────────────────────
# Infrastructure values are read from environment variables to maintain
# portability and security across development and production environments.

REGION = os.getenv("AWS_REGION", "us-east-1")
MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "global.amazon.nova-2-lite-v1:0")
GATEWAY_URL = os.getenv("AGENTCORE_GATEWAY_URL", "").strip()
KB_ID = os.getenv("BEDROCK_KNOWLEDGE_BASE_ID", "").strip()
MEMORY_ID = os.getenv("AGENTCORE_MEMORY_ID", "").strip()


def get_configured_capabilities() -> Dict[str, bool]:
    """Inspect environment configuration and report active capability flags."""
    return {
        "gateway_mcp": bool(GATEWAY_URL),
        "knowledge_base": bool(KB_ID),
        "memory": bool(MEMORY_ID),
        "code_interpreter": True,
        "browser": True,
    }


# ── Model and AWS Clients ─────────────────────────────────────────────────────
model = BedrockModel(model_id=MODEL_ID)
memory_client = MemoryClient(region_name=REGION)
_bedrock_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)


# ── Namespace Helper ──────────────────────────────────────────────────────────
def get_namespaces(mem_client: MemoryClient, memory_id: str) -> Dict[str, str]:
    """Return a dict mapping strategy type → namespace template string."""
    if not memory_id:
        return {}

    try:
        strategies = mem_client.get_memory_strategies(memory_id)
        return {
            strategy["type"]: strategy["namespaces"][0]
            for strategy in strategies
            if strategy.get("namespaces")
        }
    except Exception as exc:
        logger.warning("Failed to retrieve memory strategies: %s", exc)
        return {}


# ── Memory Hook ───────────────────────────────────────────────────────────────
class MemoryHook(HookProvider):
    """Long-term memory hook for the customer support agent."""

    def __init__(
        self,
        actor_id: str,
        session_id: str,
        memory_client: MemoryClient,
        memory_id: str,
    ):
        self.actor_id = actor_id
        self.session_id = session_id
        self.memory_client = memory_client
        self.memory_id = memory_id
        self.namespaces = (
            get_namespaces(memory_client, memory_id) if memory_id else {}
        )
        self._current_user_query = None

    def retrieve_customer_context(self, event: MessageAddedEvent):
        """Retrieve relevant memories and prepend them to the user message."""
        if not self.memory_id or not self.namespaces:
            return

        messages = event.agent.messages
        if not messages:
            return

        message = messages[-1]

        # Only process plain-text user messages
        if message.get("role") != "user":
            return

        content = message.get("content") or []
        if not isinstance(content, list):
            return

        # Tool results are also represented as user-role messages.
        # Do not run memory retrieval for those.
        if any(
            isinstance(block, dict) and "toolResult" in block
            for block in content
        ):
            return

        query_parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("text")
        ]
        query = "\n".join(query_parts).strip()

        if not query:
            return

        # Preserve the original query so we do not save injected
        # memory context back into long-term memory.
        self._current_user_query = query

        context_items = []

        for strategy_type, namespace_template in self.namespaces.items():
            namespace = namespace_template.format(actorId=self.actor_id)

            try:
                memories = self.memory_client.retrieve_memories(
                    memory_id=self.memory_id,
                    namespace=namespace,
                    query=query,
                    top_k=5,
                )
            except Exception as exc:
                logger.warning(
                    "Memory retrieval failed for %s: %s",
                    strategy_type,
                    exc,
                )
                continue

            for memory in memories:
                if not isinstance(memory, dict):
                    continue

                memory_content = memory.get("content", {})
                if not isinstance(memory_content, dict):
                    continue

                text = memory_content.get("text", "").strip()
                if text:
                    context_items.append(
                        f"[{strategy_type}] {text}"
                    )

        if context_items:
            message["content"] = [{
                "text": (
                    "Customer Context:\n"
                    + "\n".join(context_items)
                    + "\n\n"
                    + query
                )
            }]

    def save_support_interaction(self, event: AfterInvocationEvent):
        """Save the completed turn to memory after the agent responds."""
        if not self.memory_id:
            return

        messages = event.agent.messages

        customer_query = None
        agent_response = None

        for message in reversed(messages):
            role = message.get("role")
            content = message.get("content") or []

            if not isinstance(content, list):
                continue

            # Skip tool-result user messages
            if role == "user" and any(
                isinstance(block, dict) and "toolResult" in block
                for block in content
            ):
                continue

            text_parts = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("text")
            ]

            text = "\n".join(text_parts).strip()
            if not text:
                continue

            if role == "assistant" and agent_response is None:
                agent_response = text

            elif role == "user" and customer_query is None:
                customer_query = text

            if customer_query and agent_response:
                break

        # Prefer the original user query captured before context injection
        if self._current_user_query:
            customer_query = self._current_user_query

        if not customer_query or not agent_response:
            logger.warning(
                "Skipping memory save: incomplete user/assistant turn"
            )
            return

        try:
            self.memory_client.create_event(
                memory_id=self.memory_id,
                actor_id=self.actor_id,
                session_id=self.session_id,
                messages=[
                    (customer_query, "USER"),
                    (agent_response, "ASSISTANT"),
                ],
            )
        except Exception as exc:
            logger.warning("Failed to save memory event: %s", exc)

    def register_hooks(self, registry: HookRegistry) -> None:  # type: ignore
        """Register both memory callbacks."""
        registry.add_callback(
            MessageAddedEvent,
            self.retrieve_customer_context,
        )
        registry.add_callback(
            AfterInvocationEvent,
            self.save_support_interaction,
        )


# ── Knowledge Base Tool ───────────────────────────────────────────────────────
@tool
def search_knowledge_base(query: str) -> str:
    """
    Search the Amazon product catalog and support knowledge base.
    Use this for product specifications, return policies, warranty
    information, loyalty program details, and order status definitions.

    Args:
        query: The question or topic to search for

    Returns:
        Relevant information retrieved from the knowledge base
    """
    if not KB_ID:
        return "Knowledge base not configured."

    try:
        resp = _bedrock_runtime.retrieve(
            knowledgeBaseId=KB_ID,
            retrievalQuery={"text": query},
            retrievalConfiguration={
                "managedSearchConfiguration": {
                    "numberOfResults": 5
                }
            },
        )
    except Exception as exc:
        logger.warning("Knowledge base retrieval failed: %s", exc)
        return "Knowledge base search is temporarily unavailable."

    results = resp.get("retrievalResults", [])

    if not results:
        return f"No information found for: {query}"

    chunks = []

    for result in results:
        content = result.get("content", {})
        text = content.get("text", "").strip()

        if text:
            chunks.append(text)

    if not chunks:
        return f"No information found for: {query}"

    return "\n---\n".join(chunks)


def calculate_loyalty_values(
    loyalty_points: int,
    tier: str,
    order_total: float,
    product_category: str = "standard",
) -> Dict[str, Any]:
    """
    Pure calculation helper implementing the loyalty program business logic.

    Business Rules:
      - loyalty points cannot be negative (clamped to 0)
      - order total cannot be negative (clamped to 0.0)
      - points redeem only in blocks of 500
      - 100 points = $1
      - point redemption can cover at most 50% of order total
      - Tier discount rates: Silver = 0%, Gold = 10%, Platinum = 15% (unknown = 0%)
      - Tier discount is applied after points redemption (on subtotal after points)
      - Category earn rates: standard = 1, device = 2, fresh = 5 (unknown = 1)
      - Points earned are based on final total (floor(final_total * earn_rate))
      - remaining points = initial points - redeemed points + earned points
    """
    safe_points = max(0, int(loyalty_points))
    normalized_tier = str(tier).strip().title()
    safe_order_total = max(0.0, float(order_total))
    normalized_category = str(product_category).strip().lower()

    earn_rates = {
        "standard": 1,
        "device": 2,
        "fresh": 5,
    }
    tier_rates = {
        "Silver": 0.00,
        "Gold": 0.10,
        "Platinum": 0.15,
    }

    earn_rate = earn_rates.get(normalized_category, earn_rates["standard"])
    tier_rate = tier_rates.get(normalized_tier, 0.00)

    # Points can only be redeemed in blocks of 500
    redeemable_from_balance = (safe_points // 500) * 500

    # 100 points = $1. Points may cover at most 50% of the order
    max_redemption_dollars = safe_order_total * 0.50
    max_points_for_order = math.floor((max_redemption_dollars * 100) / 500) * 500

    points_redeemed = min(redeemable_from_balance, max_points_for_order)
    points_discount = points_redeemed / 100.0

    subtotal_after_points = max(0.0, safe_order_total - points_discount)
    tier_discount = round(subtotal_after_points * tier_rate, 2)
    final_total = round(subtotal_after_points - tier_discount, 2)
    total_savings = round(safe_order_total - final_total, 2)
    points_earned = math.floor(final_total * earn_rate)
    remaining_points = safe_points - points_redeemed + points_earned

    return {
        "loyalty_points": safe_points,
        "tier": normalized_tier,
        "order_total": round(safe_order_total, 2),
        "product_category": normalized_category,
        "earn_rate": earn_rate,
        "points_redeemed": points_redeemed,
        "points_discount": round(points_discount, 2),
        "tier_discount_rate": tier_rate,
        "tier_discount": tier_discount,
        "total_savings": total_savings,
        "final_total": final_total,
        "points_earned": points_earned,
        "remaining_points": remaining_points,
    }


# ── Loyalty Discount Tool (Code Interpreter) ──────────────────────────────────
@tool
def calculate_loyalty_discount(
    loyalty_points: int,
    tier: str,
    order_total: float,
    product_category: str = "standard",
) -> str:
    """
    Calculate the loyalty discount for a customer order using the
    AgentCore Code Interpreter. Runs exact arithmetic in a secure sandbox.

    Args:
        loyalty_points:   Customer's current points balance
        tier:             Customer tier — Silver, Gold, or Platinum
        order_total:      Order total in USD
        product_category: standard, device, or fresh

    Returns:
        Full discount breakdown and final price
    """
    code = f"""
import json
import math

loyalty_points = max(0, int({loyalty_points!r}))
tier = {tier!r}.title()
order_total = max(0.0, float({order_total!r}))
product_category = {product_category!r}.lower()

earn_rates = {{
    "standard": 1,
    "device": 2,
    "fresh": 5
}}

tier_rates = {{
    "Silver": 0.00,
    "Gold": 0.10,
    "Platinum": 0.15
}}

earn_rate = earn_rates.get(product_category, earn_rates["standard"])
tier_rate = tier_rates.get(tier, 0.00)

# Points can only be redeemed in blocks of 500.
redeemable_from_balance = (loyalty_points // 500) * 500

# 100 points = $1. Points may cover at most 50% of the order.
max_redemption_dollars = order_total * 0.50
max_points_for_order = math.floor(
    (max_redemption_dollars * 100) / 500
) * 500

points_redeemed = min(
    redeemable_from_balance,
    max_points_for_order
)

points_discount = points_redeemed / 100.0

subtotal_after_points = max(
    0.0,
    order_total - points_discount
)

tier_discount = round(
    subtotal_after_points * tier_rate,
    2
)

final_total = round(
    subtotal_after_points - tier_discount,
    2
)

total_savings = round(
    order_total - final_total,
    2
)

points_earned = math.floor(
    final_total * earn_rate
)

remaining_points = (
    loyalty_points
    - points_redeemed
    + points_earned
)

result = {{
    "loyalty_points": loyalty_points,
    "tier": tier,
    "order_total": round(order_total, 2),
    "product_category": product_category,
    "earn_rate": earn_rate,
    "points_redeemed": points_redeemed,
    "points_discount": round(points_discount, 2),
    "tier_discount_rate": tier_rate,
    "tier_discount": tier_discount,
    "total_savings": total_savings,
    "final_total": final_total,
    "points_earned": points_earned,
    "remaining_points": remaining_points
}}

print(json.dumps(result))
"""

    try:
        with code_session(REGION) as code_client:
            response = code_client.invoke(
                "executeCode",
                {
                    "code": code,
                    "language": "python",
                    "clearContext": True,
                },
            )

            for event in response["stream"]:
                if "result" in event:
                    return json.dumps(event["result"])

            raise RuntimeError(
                "Code Interpreter returned no result event."
            )

    except Exception as exc:
        logger.warning(
            "Code Interpreter execution failed: %s; using local fallback",
            exc,
        )
        fallback_result = calculate_loyalty_values(
            loyalty_points=loyalty_points,
            tier=tier,
            order_total=order_total,
            product_category=product_category,
        )
        fallback_result["fallback"] = True
        return json.dumps(fallback_result)


# ── Agent Entrypoint ──────────────────────────────────────────────────────────
@app.entrypoint
async def invoke(payload, context=None):
    """
    Main handler called by AgentCore for every incoming request.

    Expected payload keys:
      prompt      (str, required) — the customer's message
      customer_id (str, optional) — unique customer identifier
      session_id  (str, optional) — session identifier; generated if absent
    """
    user_input = str(payload.get("prompt", "")).strip()
    actor_id = str(
        payload.get("customer_id") or "anonymous-customer"
    )
    session_id = str(
        payload.get("session_id") or uuid.uuid4()
    )

    if not user_input:
        return "Please provide a customer-support question."

    memory_hook = MemoryHook(
        actor_id=actor_id,
        session_id=session_id,
        memory_client=memory_client,
        memory_id=MEMORY_ID,
    )

    tools = [
        search_knowledge_base,
        calculate_loyalty_discount,
    ]

    if AgentCoreBrowser is not None:
        agent_core_browser = AgentCoreBrowser(region=REGION)
        tools.append(agent_core_browser.browser)
    else:
        logger.warning(
            "AgentCoreBrowser not available; browser tool disabled."
        )

    system_prompt = """
You are a helpful, accurate AI customer-support agent for an online store.

You have several specialized tools. Use them instead of guessing.

TOOL RULES

1. ORDER AND CUSTOMER INFORMATION
Use the AgentCore Gateway tools for:
- tracking orders
- looking up order details
- looking up customer details
- listing a customer's orders
- initiating refunds
- checking refund status
- obtaining return labels

Never invent order, tracking, refund, customer, or shipping data.

2. PRODUCT AND POLICY KNOWLEDGE
Use search_knowledge_base for:
- product specifications
- return windows
- refund timelines
- warranty information
- loyalty program rules
- loyalty tier benefits
- order-status definitions

For questions that depend on store policy or catalog facts, retrieve the
knowledge-base information before answering.

3. LOYALTY CALCULATIONS
Use calculate_loyalty_discount whenever exact loyalty-point redemption,
tier discounts, totals, savings, points earned, or remaining points must
be calculated. Do not perform those calculations mentally when the tool
is available.

4. LIVE WEB BROWSING
Use the browser tool when the customer explicitly asks you to visit,
inspect, or obtain information from a live webpage or URL.

When creating browser sessions, session names must contain only lowercase
letters, digits, and hyphens. Do not use spaces or underscores.

5. LONG-TERM CUSTOMER MEMORY
A user message may contain a "Customer Context:" section automatically
retrieved from long-term memory. Treat that context as remembered customer
information. Use it naturally when relevant, but do not expose internal
memory mechanics unless asked.

GENERAL BEHAVIOR
- Be concise, clear, and customer-friendly.
- Honor remembered communication preferences when available.
- State concrete tool results accurately.
- If a tool fails, explain the limitation rather than fabricating data.
- Use only information returned by tools or explicitly supplied by the
  customer for account-specific facts.
"""

    try:
        if GATEWAY_URL:
            mcp_client = MCPClient(
                lambda: streamable_http_client(GATEWAY_URL)
            )
            with mcp_client:
                gateway_tools = mcp_client.list_tools_sync()
                all_tools = tools + list(gateway_tools)

                agent = Agent(
                    model=model,
                    system_prompt=system_prompt,
                    tools=all_tools,
                    hooks=[memory_hook],
                )
                response = await agent.invoke_async(user_input)
        else:
            logger.info(
                "Running agent without AgentCore Gateway MCP tools "
                "(AGENTCORE_GATEWAY_URL not set)."
            )
            agent = Agent(
                model=model,
                system_prompt=system_prompt,
                tools=tools,
                hooks=[memory_hook],
            )
            response = await agent.invoke_async(user_input)

        content = response.message.get("content", [])

        if (
            content
            and isinstance(content[0], dict)
            and content[0].get("text")
        ):
            return content[0]["text"]

        return str(response)

    except Exception as exc:
        logger.exception("Agent invocation failed")
        return "I encountered an internal error while processing your request."


if __name__ == "__main__":
    app.run()
