"""
Превращает JSON-ключ Google в одну строку base64 для переменной GOOGLE_CREDENTIALS_JSON.

    python encode_credentials.py google_credentials.json

Результат сохраняется в файл credentials_b64.txt (он в .gitignore, в GitHub не попадёт).
Откройте его, скопируйте всё содержимое целиком и вставьте в значение переменной на хостинге.
Файл с результатом после этого лучше удалить.
"""
import base64
import json
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "google_credentials.json"
try:
    raw = open(path, "rb").read()
    info = json.loads(raw)
    assert "client_email" in info and "private_key" in info
except Exception as e:  # noqa: BLE001
    sys.exit(f"Не удалось прочитать ключ из {path}: {e}")

encoded = base64.b64encode(raw).decode("ascii")
with open("credentials_b64.txt", "w", encoding="ascii") as f:
    f.write(encoded)
print(f"Готово: credentials_b64.txt ({len(encoded)} символов).")
print("Сервисный аккаунт:", info["client_email"])
print("Не забудьте дать этому адресу доступ «Редактор» к вашей Google Таблице.")
