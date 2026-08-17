import os

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
AGENT_RECURSION_LIMIT = int(os.getenv("AGENT_RECURSION_LIMIT", "25"))
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1")

# LLM provider for document generation: "azure" (default) or "anthropic".
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "azure")

# Azure OpenAI — used when LLM_PROVIDER=azure. Authenticated via Azure AD
# (DefaultAzureCredential → `az login`), so no API key is required.
AZURE_OPENAI_ENDPOINT    = os.getenv("AZURE_OPENAI_ENDPOINT", "https://ai-adoption-coe.services.ai.azure.com")
AZURE_OPENAI_DEPLOYMENT  = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5.4")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")

# Claude / Anthropic settings — used only when LLM_PROVIDER=anthropic
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")
CLAUDE_MODEL   = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5")
