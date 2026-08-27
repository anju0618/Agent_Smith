"""
テストコード
"""
import os, json, re, sys, traceback
import requests
from dotenv import load_dotenv

load_dotenv()
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
MODEL = "openai/gpt-oss-120b"
MAX_ITERATIONS = 5

SYETEM_PROMPT = """You are a coding agent...
Respond as:
Thought: <reasoning>
Code:
```python
<code defining the function>
"""