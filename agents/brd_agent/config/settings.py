import os

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
AGENT_RECURSION_LIMIT = int(os.getenv("AGENT_RECURSION_LIMIT", "25"))
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1")
