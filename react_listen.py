"""
Показывает ID реакций, которые вы ставите вручную в ВК.
Запуск: python react_listen.py
Затем в тестовой беседе поставьте нужную реакцию на любое сообщение —
в консоли появится reaction_id. Остановка: Ctrl+C.
Перед запуском закройте другие скрипты (bot.py, react_probe.py) —
два Long Poll на одно сообщество «перехватывают» события друг у друга.
"""
import json
import os

import vk_api
from dotenv import load_dotenv
from vk_api.bot_longpoll import VkBotLongPoll

load_dotenv()
TOKEN = os.getenv("VK_TOKEN", "")
GROUP_ID = int(os.getenv("VK_GROUP_ID", "0"))

session = vk_api.VkApi(token=TOKEN, api_version="5.199")
lp = VkBotLongPoll(session, GROUP_ID)
print("Жду реакции. Поставьте реакцию на сообщение в беседе (Ctrl+C — выход)...")
print("Для проверки подписки на события любое другое событие тоже будет показано.\n")

for event in lp.listen():
    etype = getattr(event.type, "value", event.type)
    obj = event.raw.get("object", {})
    if str(etype) == "message_reaction_event":
        print(f"РЕАКЦИЯ: reaction_id = {obj.get('reaction_id')}  "
              f"(кто: {obj.get('reacted_id')}, беседа: {obj.get('peer_id')}, "
              f"сообщение: {obj.get('cmid')})")
        print("  сырые данные:", json.dumps(obj, ensure_ascii=False))
    else:
        print(f"[другое событие: {etype}]")
