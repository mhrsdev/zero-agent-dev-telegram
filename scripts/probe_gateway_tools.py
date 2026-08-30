"""Direct gateway probe: does claude-opus-5 call tools when streamed?"""

import json

import httpx

BASE = "https://api.justwoker.icu/v1"
KEY = "sk-BlwjB2GhsGBwFLjQBBAhKK7FpmfJYP9usqGfrImaLaA1JOKW"
MODEL = "claude-opus-5"

payload = {
    "model": MODEL,
    "max_tokens": 512,
    "stream": True,
    "messages": [
        {
            "role": "user",
            "content": "Use the web_search tool to look up 'Nous Research Hermes agent', then tell me the top result.",
        }
    ],
    "tools": [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the public web (keyless DuckDuckGo backend) and return up to 5 results with title, URL, and snippet.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "search query"}},
                    "required": ["query"],
                },
            },
        }
    ],
}

with httpx.Client(timeout=60) as client:
    with client.stream(
        "POST",
        f"{BASE}/chat/completions",
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        json=payload,
    ) as response:
        print("status:", response.status_code)
        buffer = ""
        for line in response.iter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            choice = (chunk.get("choices") or [{}])[0]
            delta = choice.get("delta") or {}
            if delta.get("content"):
                buffer += delta["content"]
            if delta.get("tool_calls"):
                print("TOOL_CALL DELTA:", json.dumps(delta["tool_calls"])[:300])
            if choice.get("finish_reason"):
                print("finish_reason:", choice["finish_reason"])
        print("TEXT:", buffer[:300])
