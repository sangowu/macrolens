from __future__ import annotations

from typing import Callable

from google import genai
from google.genai import types


class GeminiClient:
    provider = "gemini"

    def __init__(self, model: str, api_key: str) -> None:
        self._client = genai.Client(api_key=api_key)
        self.model = model

    def chat(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> str:
        contents = _msgs_to_contents(messages)
        resp = self._client.models.generate_content(
            model=self.model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=max_tokens,
                temperature=temperature,
            ),
        )
        return resp.text.strip()

    def chat_with_tools(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        tool_choice: dict,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> dict:
        function_declarations = _tools_to_declarations(tools)
        forced_name = tool_choice.get("name")

        resp = self._client.models.generate_content(
            model=self.model,
            contents=_msgs_to_contents(messages),
            config=types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=max_tokens,
                temperature=temperature,
                tools=[types.Tool(function_declarations=function_declarations)],
                tool_config=types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(
                        mode="ANY",
                        allowed_function_names=[forced_name] if forced_name else None,
                    )
                ),
            ),
        )

        for part in resp.candidates[0].content.parts:
            if part.function_call:
                return dict(part.function_call.args)
        return {}

    def chat_agentic(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        tool_executor: Callable[[str, dict], str],
        max_tokens: int = 4096,
        max_turns: int = 10,
    ) -> str:
        function_declarations = _tools_to_declarations(tools)
        gemini_tools = [types.Tool(function_declarations=function_declarations)]
        contents = _msgs_to_contents(messages)

        for _ in range(max_turns):
            resp = self._client.models.generate_content(
                model=self.model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    max_output_tokens=max_tokens,
                    temperature=0.0,
                    tools=gemini_tools,
                ),
            )

            candidate = resp.candidates[0]
            contents.append(candidate.content)

            function_calls = [p for p in candidate.content.parts if p.function_call]

            if not function_calls:
                return "".join(
                    p.text for p in candidate.content.parts
                    if hasattr(p, "text") and p.text
                ).strip()

            # 执行所有 function calls，把结果发回
            result_parts = []
            for part in function_calls:
                fc = part.function_call
                result = tool_executor(fc.name, dict(fc.args))
                result_parts.append(types.Part(
                    function_response=types.FunctionResponse(
                        name=fc.name,
                        response={"result": result},
                    )
                ))

            contents.append(types.Content(role="user", parts=result_parts))

        # 超过 max_turns，返回最后一轮的文本
        last = contents[-1]
        return "".join(
            p.text for p in last.parts if hasattr(p, "text") and p.text
        ).strip()


# ── 工具函数 ──────────────────────────────────────────────

def _msgs_to_contents(messages: list[dict]) -> list[types.Content]:
    return [
        types.Content(role=m["role"], parts=[types.Part(text=m["content"])])
        for m in messages
    ]


def _tools_to_declarations(tools: list[dict]) -> list[types.FunctionDeclaration]:
    return [
        types.FunctionDeclaration(
            name=t["name"],
            description=t.get("description", ""),
            parameters=t["input_schema"],
        )
        for t in tools
    ]
