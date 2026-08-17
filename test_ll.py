import httpx
from azure.identity import AzureCliCredential, get_bearer_token_provider
from openai import AzureOpenAI

AZURE_ENDPOINT  = "https://ai-adoption-coe.services.ai.azure.com"
DEPLOYMENT_NAME = "gpt-5.4"
API_VERSION     = "2025-01-01-preview"

token_provider = get_bearer_token_provider(
    AzureCliCredential(),
    "https://cognitiveservices.azure.com/.default",
)

client = AzureOpenAI(
    azure_endpoint=AZURE_ENDPOINT,
    azure_ad_token_provider=token_provider,
    api_version=API_VERSION,
    http_client=httpx.Client(verify=False),
)

try:
    print("Checking connection to Azure LLM deployment...")
    response = client.chat.completions.create(
        model=DEPLOYMENT_NAME,
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user",   "content": "What is the capital of France?"},
        ],
        max_completion_tokens=200,
    )
    print("\nConnection Status: Verification Successful!")
    print(response.choices[0].message.content)

except Exception as e:
    print(f"\nConnection Status: Verification Failed!")
    print(f"Error Details: {str(e)}")
