import asyncio
import json

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main():
    async with streamablehttp_client("http://127.0.0.1:8765/mcp") as (reader, writer, _):
        async with ClientSession(reader, writer) as client:
            await client.initialize()
            listed = await client.call_tool("list_skills", {"limit": 10})
            assert not listed.isError, "list_skills failed"
            payload = json.loads(listed.content[0].text)
            entries = payload if isinstance(payload, list) else payload.get("skills", payload.get("results"))
            assert entries, "The example catalog should contain a skill"
            name = entries[0]["name"]
            loaded = await client.call_tool("get_skill", {"name": name})
            document = json.loads(loaded.content[0].text)
            assert not loaded.isError and document.get("body"), "Could not load the listed skill"
            print(f"Loaded {name} through MCP")
            found = await client.call_tool("search_prompts", {"query": "summary", "limit": 5})
            prompts = json.loads(found.content[0].text)["results"]
            assert any(row["name"] == "change-summary" for row in prompts)
            prompt = await client.call_tool("get_prompt", {"name": "change-summary"})
            assert not prompt.isError and json.loads(prompt.content[0].text).get("body")
            print("Loaded the example prompt through MCP")


asyncio.run(main())
