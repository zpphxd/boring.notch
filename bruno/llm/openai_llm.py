"""OpenAI (GPT-4o) adapter using Structured Outputs (strict JSON schema)."""

import json

from bruno.llm.base import AgentTurn, LLMError, TurnContext, messages_with_corrections, turn_json_schema


class OpenAILLM:
    name = "openai"

    def __init__(self, api_key: str, model: str, timeout: float = 20.0):
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key, timeout=timeout)
        self.model = model

    def generate_turn(self, ctx: TurnContext) -> AgentTurn:
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                temperature=0.3,
                messages=[{"role": "system", "content": ctx.system_prompt}, *messages_with_corrections(ctx)],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "agent_turn",
                        "strict": True,
                        "schema": turn_json_schema(ctx.menu),
                    },
                },
            )
            content = resp.choices[0].message.content
            if not content:
                raise LLMError("empty response (refusal?)")
            return AgentTurn.model_validate(json.loads(content))
        except LLMError:
            raise
        except Exception as exc:  # network, rate limit, bad JSON
            raise LLMError(str(exc)) from exc
