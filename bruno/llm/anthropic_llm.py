"""Anthropic (Claude) adapter.

Turns use structured outputs (`output_config.format`): the reply is JSON that
matches the menu-constrained schema. (Forced `tool_choice` is rejected by
current models such as Claude Sonnet 5.5, so it isn't used.) Current models
think adaptively before answering, so `max_tokens` leaves room for that, and
`effort` keeps texting latency low.
"""

import json

from bruno.llm.base import AgentTurn, LLMError, TurnContext, messages_with_corrections, turn_json_schema

TURN_MAX_TOKENS = 16000
WRITE_MAX_TOKENS = 4000


def _anthropic_messages(messages: list[dict]) -> list[dict]:
    """Claude needs the first turn to be 'user' and roles to alternate."""
    out: list[dict] = []
    if messages and messages[0]["role"] != "user":
        out.append({"role": "user", "content": "(Conversation started by our outreach text below.)"})
    for msg in messages:
        if out and out[-1]["role"] == msg["role"]:
            out[-1] = {"role": msg["role"], "content": out[-1]["content"] + "\n" + msg["content"]}
        else:
            out.append(dict(msg))
    return out


def _text(resp) -> str:
    if resp.stop_reason == "refusal":
        raise LLMError("model declined the request")
    if resp.stop_reason == "max_tokens":
        raise LLMError("model ran out of output tokens")
    return "".join(b.text for b in resp.content if b.type == "text").strip()


class AnthropicLLM:
    name = "anthropic"

    def __init__(self, api_key: str, model: str, timeout: float = 20.0, effort: str = "low"):
        from anthropic import Anthropic

        self.client = Anthropic(api_key=api_key, timeout=timeout)
        self.model = model
        self.effort = effort

    def generate_turn(self, ctx: TurnContext) -> AgentTurn:
        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=TURN_MAX_TOKENS,
                system=ctx.system_prompt,
                messages=_anthropic_messages(messages_with_corrections(ctx)),
                output_config={
                    "effort": self.effort,
                    "format": {"type": "json_schema", "schema": turn_json_schema(ctx.menu)},
                },
            )
            return AgentTurn.model_validate(json.loads(_text(resp)))
        except LLMError:
            raise
        except Exception as exc:  # network, rate limit, bad JSON
            raise LLMError(str(exc)) from exc

    def write(self, system: str, prompt: str) -> str:
        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=WRITE_MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": self.effort},
            )
            return _text(resp)
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(str(exc)) from exc
