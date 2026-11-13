"""Curated OpenRouter models. Persist the selected ID on every job."""
import os

DEFAULT_MODEL = 'qwen/qwen3.6-flash'
MODELS = (
    {'id': DEFAULT_MODEL, 'name': 'Qwen3.6 Flash'},
    {'id': 'qwen/qwen3.6-plus', 'name': 'Qwen3.6 Plus'},
    {'id': 'google/gemini-3-flash-preview', 'name': 'Gemini 3 Flash Preview'},
    {'id': 'deepseek/deepseek-v3.2', 'name': 'DeepSeek V3.2'},
)
MODEL_IDS = frozenset(m['id'] for m in MODELS)


def default_model():
    configured = os.getenv('OPENROUTER_MODEL', DEFAULT_MODEL)
    return configured if configured in MODEL_IDS else DEFAULT_MODEL
