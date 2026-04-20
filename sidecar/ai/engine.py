# from openai import OpenAI
# import os

# class AIEngine:
#     def __init__(self, api_key: str):
#         self.client = OpenAI(api_key=api_key)
#         self.model = "gpt-4-turbo-preview" # Или gpt-3.5-turbo

#     def ask_about_symbol(self, symbol_name: str, context: str, user_question: str):
#         system_prompt = f"""
#         You are a Surgical Context AI. You help developers understand code by looking at a specific
#         symbol and its dependencies.

#         Below is the 'Surgical Context' for the symbol '{symbol_name}'.
#         It includes the target code and its direct dependencies from the call graph.

#         CONTEXT:
#         {context}
#         """

#         response = self.client.chat.completions.create(
#             model=self.model,
#             messages=[
#                 {"role": "system", "content": system_prompt},
#                 {"role": "user", "content": user_question}
#             ],
#             temperature=0.2 # Низкая температура для точности
#         )

#         return response.choices[0].message.content
# import os
# from anthropic import Anthropic

# class AIEngine:
#     # A simple registry to handle model aliasing
#     MODEL_REGISTRY = {
#         "fast": "claude-3-haiku-20240307",
#         "balanced": "claude-3-5-sonnet-20240620",
#         "powerful": "claude-3-opus-20240229"
#     }

#     def __init__(self, api_key: str = None, tier: str = "balanced"):
#         self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")

#         # Priority: Env Var 'ANTHROPIC_MODEL' > Registry Alias > Default String
#         env_model = os.getenv("ANTHROPIC_MODEL")
#         if env_model:
#             self.model = env_model
#         else:
#             self.model = self.MODEL_REGISTRY.get(tier, self.MODEL_REGISTRY["balanced"])

#         self.client = Anthropic(api_key=self.api_key)
#         print(f"🤖 AI Engine initialized with model: {self.model}")

#     def ask_about_symbol(self, symbol_name: str, context: str, user_question: str):
#         # Определяем системный промпт
#         system_msg = f"""You are a Surgical Context AI.
#         Analyze the symbol '{symbol_name}' and its dependencies provided below.

#         CONTEXT:
#         {context}"""

#         # Передаем именно system_msg в аргумент system
#         message = self.client.messages.create(
#             model=self.model,
#             max_tokens=2048,
#             system=system_msg, # Было system_prompt, стало system_msg
#             messages=[
#                 {"role": "user", "content": user_question}
#             ],
#             temperature=0
#         )

#         return message.content[0].text
import os

from claude_api import Client


class AIEngine:
    def __init__(self):
        # Добавь CLAUDE_SESSION_KEY в свой .env
        self.session_key = os.getenv("CLAUDE_SESSION_KEY")
        if not self.session_key:
            raise ValueError("Нужен CLAUDE_SESSION_KEY из куки браузера!")

        self.client = Client(self.session_key)

    def ask_about_symbol(self, symbol_name: str, context: str, user_question: str):
        # Создаем новый чат или используем существующий
        chat_id = self.client.create_new_chat()["uuid"]

        prompt = f"Analyze code for '{symbol_name}':\n{context}\n\nQuestion: {user_question}"

        # Отправляем запрос (это имитирует ввод текста в чат)
        response = self.client.send_message(prompt, chat_id)
        return response
