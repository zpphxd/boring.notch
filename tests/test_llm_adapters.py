"""Request/response plumbing for the real providers, with the network stubbed out."""

import json
from types import SimpleNamespace

from bruno.fsm import Intent, State
from bruno.llm.anthropic_llm import AnthropicLLM, _anthropic_messages
from bruno.llm.base import TurnContext
from bruno.llm.openai_llm import OpenAILLM
from bruno.menu import CartItem

TURN = {
    "intent": "update_order",
    "cart": [{"item_id": "pizza", "size": "large", "quantity": 1, "modifiers": ["mushroom"], "notes": None}],
    "pickup_time": None,
    "reply": "You got it!",
}


def ctx(deps, corrections=()):
    return TurnContext(
        system_prompt="SYSTEM",
        history=[
            {"role": "assistant", "content": "Hey Dave! Want your usual?"},
            {"role": "user", "content": "swap pepperoni for mushroom"},
        ],
        customer_message="swap pepperoni for mushroom",
        current_cart=[CartItem(item_id="pizza", size="large", modifiers=["pepperoni"])],
        usual_order=[],
        menu=deps.menu,
        state=State.AWAITING_RESPONSE,
        corrections=list(corrections),
    )


def test_openai_structured_output_request(deps):
    llm = OpenAILLM(api_key="test", model="gpt-4o")
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        msg = SimpleNamespace(content=json.dumps(TURN))
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    turn = llm.generate_turn(ctx(deps, corrections=["What size Pizza?"]))

    assert turn.intent == Intent.UPDATE_ORDER and turn.cart[0].modifiers == ["mushroom"]
    rf = captured["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True
    assert captured["messages"][0] == {"role": "system", "content": "SYSTEM"}
    assert "validator" in captured["messages"][-1]["content"]


def test_anthropic_structured_output_request(deps):
    llm = AnthropicLLM(api_key="test", model="claude-sonnet-5-5", effort="low")
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(stop_reason="end_turn", content=[
            SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=json.dumps(TURN))])

    llm.client = SimpleNamespace(messages=SimpleNamespace(create=create))
    turn = llm.generate_turn(ctx(deps))

    assert turn.reply == "You got it!"
    assert "tool_choice" not in captured and "tools" not in captured  # forced tool use 400s on current models
    assert captured["output_config"]["format"]["type"] == "json_schema"
    assert captured["output_config"]["effort"] == "low"
    assert captured["max_tokens"] >= 4000  # leave room for adaptive thinking
    assert captured["system"] == "SYSTEM"
    assert captured["messages"][0]["role"] == "user"  # Claude needs a user turn first
    roles = [m["role"] for m in captured["messages"]]
    assert all(a != b for a, b in zip(roles, roles[1:]))


def test_anthropic_truncation_and_refusal_raise(deps):
    import pytest

    from bruno.llm import LLMError

    llm = AnthropicLLM(api_key="test", model="claude-sonnet-5-5")
    for reason in ("max_tokens", "refusal"):
        llm.client = SimpleNamespace(messages=SimpleNamespace(
            create=lambda **kw: SimpleNamespace(stop_reason=reason, content=[SimpleNamespace(type="text", text="Hi Ray, it's Em")])))
        with pytest.raises(LLMError):
            llm.write("s", "p")


def test_anthropic_merges_consecutive_roles():
    msgs = _anthropic_messages(
        [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}, {"role": "assistant", "content": "c"}]
    )
    assert msgs == [{"role": "user", "content": "a\nb"}, {"role": "assistant", "content": "c"}]
