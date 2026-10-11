"""Anthropic (Claude) adapter. Structured output via a single forced tool call."""

from bruno.llm.base import AgentTurn, LLMError, TurnContext, messages_with_corrections, turn_json_schema

TOOL_NAME = "respond_to_customer"


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


class AnthropicLLM:
    name = "anthropic"

    def __init__(self, api_key: str, model: str, timeout: float = 20.0):
        from anthropic import Anthropic

        self.client = Anthropic(api_key=api_key, timeout=timeout)
        self.model = model

    def generate_turn(self, ctx: TurnContext) -> AgentTurn:
        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=ctx.system_prompt,
                messages=_anthropic_messages(messages_with_corrections(ctx)),
                tools=[
                    {
                        "name": TOOL_NAME,
                        "description": "Send the reply and the updated cart state.",
                        "input_schema": turn_json_schema(ctx.menu),
                    }
                ],
                tool_choice={"type": "tool", "name": TOOL_NAME},
            )
            for block in resp.content:
                if block.type == "tool_use" and block.name == TOOL_NAME:
                    return AgentTurn.model_validate(block.input)
            raise LLMError("model did not call the response tool")
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(str(exc)) from exc
