"""
Подбор ID реакций. Запуск: python react_probe.py
Затем напишите в тестовой беседе: /тест
Бот по очереди поставит реакции с ID 0..30 на ваше сообщение (раз в 3 секунды).
Смотрите, какая эмодзи появилась, и сверяйте с номером в консоли.
"""
import os
import time

import vk_api
from dotenv import load_dotenv
from vk_api.bot_longpoll import VkBotEventType, VkBotLongPoll
from vk_api.exceptions import ApiError

load_dotenv()
TOKEN = os.getenv("VK_TOKEN", "")
GROUP_ID = int(os.getenv("VK_GROUP_ID", "0"))

session = vk_api.VkApi(token=TOKEN, api_version="5.199")
vk = session.get_api()
lp = VkBotLongPoll(session, GROUP_ID)
print("Жду сообщение /тест в беседе (Ctrl+C — выход)...")

for event in lp.listen():
    if event.type != VkBotEventType.MESSAGE_NEW:
        continue
    msg = event.message
    if (msg.get("text") or "").strip().lower() != "/тест":
        continue
    peer, cmid = msg["peer_id"], msg.get("conversation_message_id")
    print(f"peer_id={peer}, cmid={cmid}")
    for rid in range(0, 31):
        try:
            vk.messages.sendReaction(peer_id=peer, cmid=cmid, reaction_id=rid)
            print(f"ID {rid}: OK — посмотрите, какая реакция сейчас на сообщении")
        except ApiError as e:
            print(f"ID {rid}: ошибка {e}")
            if getattr(e, "code", None) == 27:
                print("Токен сообщества не может ставить реакции (код 27). Остановка.")
                raise SystemExit(1)
        time.sleep(3)
    print("Готово.")
    break
