import os
from dotenv import load_dotenv
from google import genai

load_dotenv()

api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    print("Error: GEMINI_API_KEY not found in environment or .env file.")
    exit(1)

print(f"API key loaded: {api_key[:6]}...{api_key[-4:]}")
print("Connecting to Gemini API...")

try:
    client = genai.Client()

    # Заменяем на актуальную модель
    response = client.models.generate_content(
        model="gemini-3.8-flash",
        contents="Ответь одним словом: 'Работает!'",
    )

    print("Success! Gemini response:")
    print(response.text)

except Exception as e:
    print(f"Connection failed: {e}")