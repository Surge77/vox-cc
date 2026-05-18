import os
import sys

if getattr(sys, "frozen", False):
    BASE = os.path.dirname(sys.executable)  # dist/sidecar/ — models/ is sibling to exe
else:
    BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_DIR = os.environ.get("VOX_MODEL_DIR") or os.path.join(BASE, "models")
LLM_FILENAME = "qwen2.5-3b-instruct-q4_k_m.gguf"
LLM_PATH = os.path.join(MODEL_DIR, LLM_FILENAME)


def load_llm():
    from llama_cpp import Llama
    return Llama(
        model_path=LLM_PATH,
        n_gpu_layers=-1,
        n_ctx=2048,
        verbose=False,
    )


def run_llm(llm, system_prompt: str, user_text: str) -> str:
    response = llm.create_chat_completion(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        max_tokens=512,
        temperature=0.0,
    )
    return response["choices"][0]["message"]["content"].strip()


def run_groq(api_key: str, system_prompt: str, user_text: str) -> str:
    from groq import Groq
    client = Groq(api_key=api_key)
    response = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        max_tokens=512,
        temperature=0.0,
    )
    return response.choices[0].message.content.strip()


def prewarm_llm(llm) -> None:
    try:
        run_llm(llm, "You are a helpful assistant.", "ping")
    except Exception:
        pass
