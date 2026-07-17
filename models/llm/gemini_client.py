from __future__ import annotations

from collections.abc import Callable

from google import genai
from google.genai import types

from models.llm.tracing import end_generation, start_generation


def _usage(resp: object) -> tuple[int | None, int | None]:
    """从 Gemini 响应提取 (input_tokens, output_tokens)，缺失时返回 None。"""
    um = getattr(resp, "usage_metadata", None)
    if um is None:
        return None, None
    return getattr(um, "prompt_token_count", None), getattr(um, "candidates_token_count", None)


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
        gen = start_generation("gemini.chat", self.model, messages)
        resp = self._client.models.generate_content(
            model=self.model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=max_tokens,
                temperature=temperature,
            ),
        )
        text = (resp.text or "").strip()
        in_tok, out_tok = _usage(resp)
        end_generation(gen, output=text, input_tokens=in_tok, output_tokens=out_tok)
        return text

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

        gen = start_generation(f"gemini.tool:{forced_name or 'any'}", self.model, messages)
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

        result: dict = {}
        if resp.candidates:
            content = resp.candidates[0].content
            if content is not None:
                for part in content.parts or []:
                    if part.function_call:
                        result = dict(part.function_call.args)
                        break

        in_tok, out_tok = _usage(resp)
        end_generation(gen, output=result, input_tokens=in_tok, output_tokens=out_tok)
        return result

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

        # 整个 agentic loop 记为一条 generation，跨轮累加 token 用量
        gen = start_generation("gemini.chat_agentic", self.model, messages)
        total_in = 0
        total_out = 0
        final_text = ""

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
            in_tok, out_tok = _usage(resp)
            total_in += in_tok or 0
            total_out += out_tok or 0

            if not resp.candidates:
                break
            candidate = resp.candidates[0]
            if candidate.content is None:
                break
            contents.append(candidate.content)

            parts = candidate.content.parts or []
            function_calls = [p for p in parts if p.function_call]

            if not function_calls:
                final_text = "".join(
                    p.text for p in parts if hasattr(p, "text") and p.text
                ).strip()
                break

            # 沙箱执行 function calls，把结果发回 LLM
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

        # not function_calls 时 final_text 已填；其余情况（提前 break / max_turns 用尽）
        # 兜底取最后一轮文本，保持与原实现等价的返回值
        if not final_text:
            last_parts = (contents[-1].parts or []) if contents else []
            final_text = "".join(
                p.text for p in last_parts if hasattr(p, "text") and p.text
            ).strip()

        end_generation(gen, output=final_text, input_tokens=total_in, output_tokens=total_out)
        return final_text


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
