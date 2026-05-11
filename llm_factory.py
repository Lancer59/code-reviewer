import os
from typing import Optional, Union, Any
from dotenv import load_dotenv

from langchain_openai import ChatOpenAI, AzureChatOpenAI
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_ollama import ChatOllama
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage

from config import cfg, cfg_bool

load_dotenv()


class PromptDebugCallback(BaseCallbackHandler):
    """Print full prompt before every LLM call. Enable with DEBUG_PRINT_PROMPT=true."""

    _SECRET_KEYS = (
        "GIT_TOKEN", "GIT_PASSWORD", "CHAINLIT_AUTH_SECRET",
        "CHAINLIT_PASSWORD", "AZURE_OPENAI_API_KEY", "OPENAI_API_KEY",
        "GOOGLE_API_KEY",
    )

    def _scrub(self, text: str) -> str:
        for key in self._SECRET_KEYS:
            val = cfg(key, "")
            if val and val in text:
                text = text.replace(val, "***")
        return text

    def on_chat_model_start(self, serialized: dict, messages: list, **kwargs: Any) -> None:
        sep = "=" * 80
        print(f"\n{sep}\nDEBUG PROMPT\n{sep}")
        total = 0
        for batch in messages:
            for msg in batch:
                role = getattr(msg, "type", type(msg).__name__)
                content = msg.content
                if isinstance(content, list):
                    chars = sum(len(b.get("text", "") if isinstance(b, dict) else str(b)) for b in content)
                    total += chars
                    print(f"\n[{role.upper()}] ({chars:,} chars, {len(content)} blocks)")
                    for i, b in enumerate(content):
                        t = b.get("text", str(b)) if isinstance(b, dict) else str(b)
                        t = self._scrub(t)
                        print(f"  block {i+1} ({len(t):,}): {t[:200]}{'...' if len(t)>200 else ''}")
                else:
                    t = self._scrub(str(content))
                    total += len(t)
                    print(f"\n[{role.upper()}] ({len(t):,} chars): {t[:200]}{'...' if len(t)>200 else ''}")
        print(f"\n{sep}\nTOTAL ~{total//4:,} tokens (messages only; tool schemas billed separately)\n{sep}\n")


def get_llm(
    provider: str,
    model_name: Optional[str] = None,
    temperature: float = 1,
    use_responses_api: bool = False,
    **kwargs
) -> Union[ChatOpenAI, AzureChatOpenAI, ChatGoogleGenerativeAI, ChatOllama]:
    """Return a LangChain chat model for the given provider."""
    provider = provider.lower()
    cbs = [PromptDebugCallback()] if cfg_bool("DEBUG_PRINT_PROMPT") else []

    if provider == "openai":
        extra = {"use_responses_api": True} if use_responses_api else {}
        return ChatOpenAI(
            model=model_name or "gpt-4o", temperature=temperature,
            api_key=cfg("OPENAI_API_KEY"), callbacks=cbs or None, **extra, **kwargs)
    elif provider == "azure":
        extra = {"use_responses_api": True} if use_responses_api else {}
        return AzureChatOpenAI(
            azure_deployment=cfg("AZURE_OPENAI_DEPLOYMENT_NAME"),
            openai_api_version=cfg("AZURE_OPENAI_API_VERSION", "2023-05-15"),
            azure_endpoint=cfg("AZURE_OPENAI_ENDPOINT"),
            api_key=cfg("AZURE_OPENAI_API_KEY"),
            temperature=temperature, callbacks=cbs or None, **extra, **kwargs)
    elif provider == "google":
        return ChatGoogleGenerativeAI(
            model=model_name or "gemini-1.5-pro", temperature=temperature,
            google_api_key=cfg("GOOGLE_API_KEY"), callbacks=cbs or None, **kwargs)
    elif provider == "ollama":
        return ChatOllama(
            model=model_name or "llama3", temperature=temperature,
            base_url=cfg("OLLAMA_BASE_URL", "http://localhost:11434"),
            callbacks=cbs or None, **kwargs)
    else:
        raise ValueError(f"Unsupported provider: {provider}. Choose from 'openai', 'azure', 'google', 'ollama'.")
