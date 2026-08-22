"""C12 SDK programmatic agent example (P1-6, issue #193).

Demonstrates the SDK as a complete programmatic agent-build surface, mirroring
the Claude Agent SDK entry points:

  1. Agent with hook/memory/graph config (not just metadata)
  2. query() — unified streaming entry point
  3. Tool with a Python handler registered to the daemon (exec'd remotely)
  4. Real SDK import path (agent_runtime.sdk), unlike the old example that
     used the raw runtime API directly.

Run: start the daemon first (./start.sh), then:

    python examples/sdk_programmatic.py
"""

from __future__ import annotations

import asyncio

from agent_runtime.sdk import Agent, AgentClient, Tool


def fetch_price(symbol: str) -> str:
    # Demo Python handler — registered to daemon via tool.register_python.
    # In a real tool this would hit an API; here it returns a fixed value so
    # the example runs fully offline.
    prices = {"AAPL": "189.50", "GOOG": "142.11"}
    return f"{symbol}: ${prices.get(symbol, 'N/A')}"


async def main() -> None:
    client = AgentClient()

    # 1. Define a custom tool with a Python handler.
    price_tool = Tool(
        name="fetch_price",
        description="Get a stock price by symbol",
        parameters={
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
        },
        handler=fetch_price,
    )
    reg = await price_tool.register_to_daemon(client)
    print("tool register:", reg)

    # 2. Build an agent with hook + memory + tool config.
    agent = Agent(
        name="sdk_demo",
        system_prompt="You are a helpful finance assistant.",
        model="qwen2.5-3b-instruct",
        tools=["fetch_price"],
        context_window=8192,
        hooks=[{"event": "tool.call", "action": "log"}],
        memory={"store": {"content": "prefers concise answers", "scope": "user"}},
    )

    # 3. query() — unified entry. stream=True yields events, stream=False
    #    returns the full result dict. Here we collect streamed events.
    events = []
    async for event in agent.query(client, "What is the price of AAPL?", stream=True):
        events.append(event)
        print("event:", event.get("type"), str(event.get("content", ""))[:60])

    print(f"total events: {len(events)}")

    # 4. Non-streaming variant returns the full result.
    result = await agent.query(client, "Hello", stream=False)
    print("non-stream result keys:", list(result.keys()) if isinstance(result, dict) else type(result))


if __name__ == "__main__":
    asyncio.run(main())
