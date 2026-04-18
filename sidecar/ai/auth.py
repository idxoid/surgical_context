import os
import time

import httpx


class GitHubAuth:
    def __init__(self):
        # Берем Client ID из .env (или вставь его сюда напрямую, если не используешь .env)
        self.client_id = os.getenv("GITHUB_CLIENT_ID", "твой_client_id_здесь")

    def get_token(self):
        if not self.client_id or self.client_id == "твой_client_id_здесь":
            raise ValueError("GITHUB_CLIENT_ID не настроен! Проверь файл .env")

        # 1. Запрашиваем код устройства
        resp = httpx.post("https://github.com/login/device/code", data={
            "client_id": self.client_id,
            "scope": "repo read:user"
        }, headers={"Accept": "application/json"})
        
        data = resp.json()
        if "verification_uri" not in data:
            raise Exception(f"Ошибка получения device code: {data}")

        print(f"\n🔗 Перейди по адресу: {data['verification_uri']}")
        print(f"🔢 Введи код: {data['user_code']}\n")
        print("⏳ Ожидаю подтверждения в браузере...")

        # 2. Опрос (Polling) сервера GitHub
        device_code = data['device_code']
        interval = data['interval']
        
        while True:
            time.sleep(interval + 1) # +1 секунда для надежности, чтобы не словить rate limit
            
            check = httpx.post("https://github.com/login/oauth/access_token", data={
                "client_id": self.client_id,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code"
            }, headers={"Accept": "application/json"})
            
            res_data = check.json()
            if "access_token" in res_data:
                return res_data['access_token']
            elif res_data.get("error") != "authorization_pending":
                raise Exception(f"Ошибка авторизации: {res_data}")