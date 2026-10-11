from bruno.config import Settings
from bruno.llm.base import AgentTurn, LLMClient, LLMError, TurnContext


def build_llm(settings: Settings) -> LLMClient:
    provider = settings.llm_provider
    if provider == "auto":
        if settings.openai_api_key:
            provider = "openai"
        elif settings.anthropic_api_key:
            provider = "anthropic"
        else:
            provider = "mock"

    if provider == "openai":
        from bruno.llm.openai_llm import OpenAILLM

        if not settings.openai_api_key:
            raise ValueError("LLM_PROVIDER=openai requires OPENAI_API_KEY")
        return OpenAILLM(settings.openai_api_key, settings.openai_model, settings.llm_timeout_seconds)
    if provider == "anthropic":
        from bruno.llm.anthropic_llm import AnthropicLLM

        if not settings.anthropic_api_key:
            raise ValueError("LLM_PROVIDER=anthropic requires ANTHROPIC_API_KEY")
        return AnthropicLLM(settings.anthropic_api_key, settings.anthropic_model, settings.llm_timeout_seconds)
    if provider == "mock":
        from bruno.llm.mock_llm import MockLLM

        return MockLLM()
    raise ValueError(f"Unknown LLM_PROVIDER: {provider}")


__all__ = ["AgentTurn", "LLMClient", "LLMError", "TurnContext", "build_llm"]
