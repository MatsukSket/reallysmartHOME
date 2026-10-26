import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

api_key = os.getenv("DEEPSEEK_API_KEY")
if not api_key:
    print("Error: DEEPSEEK_API_KEY not found in .env file.")
    exit(1)

print(f"API key loaded: {api_key[:6]}...{api_key[-4:]}")
print("Connecting to DeepSeek API...")

try:
    # DeepSeek использует базовый URL https://api.deepseek.com
    client = OpenAI(
        api_key=api_key,
        base_url="https://api.deepseek.com"
    )

    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Ответь одним словом: 'Работает!'"}
        ],
        stream=False
    )

    print("Success! DeepSeek response:")
    print(response.choices[0].message.content)

except Exception as e:
    print(f"Connection failed: {e}")