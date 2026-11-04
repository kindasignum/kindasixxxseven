"""
Разбор отписей свободного бега: «#бег Имя пришёл/ушёл» в свободной форме.

Модуль не зависит от бота: parse() получает текст, время сообщения, справочник sheets.Members и автора,
а возвращает, КТО (people), ЧТО сделал (has_in / has_out), КОГДА (in_ts / out_ts, None — «время сообщения»),
вычет минут (deduct) и пометки для администратора (flags). Что понято неуверенно — попадает в flags.
"""
import difflib
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
RE_RUN = re.compile(r"(?<![\w#])#бег(?!\w)", re.I)
MENTION = re.compile(r"\[id(\d+)\|([^\]]*)\]", re.I)
TOK = re.compile(r"(\d{1,2})[:.](\d{2})(?!\d)|(\d+(?:[.,]\d+)?)|([a-zа-я]+)|([+\-−–±~]+)")

_LAT = "qwertyuiop[]asdfghjkl;'zxcvbnm,."
_CYR = "йцукенгшщзхъфывапролджэячсмитьбю"
LAYOUT = dict(zip(_LAT, _CYR))

IN_W = ["пришел", "пришла", "пришли", "пришол", "прибежал", "прибежала", "прибежали", "зашел", "зашла",
        "зашли", "побежал", "побежала", "побежали", "начал", "начала", "начали", "стартовал",
        "стартовала", "стартовали"]
OUT_W = ["ушел", "ушла", "ушли", "ушол", "убежал", "убежала", "убежали", "отвалился", "отвалилась",
         "отвалились", "закончил", "закончила", "закончили", "финишировал", "финишировала",
         "финишировали", "отбегал", "отбегала", "отбегали", "завершил", "завершила", "завершили"]
STOP = set("""и а но я мы ты он она они мне меня нам нас еще уже тоже тут здесь там то это есть нет да ну ладно
короче ваще вообще просто пожалуйста прошу прощения блять блядь сорри извините спасибо клянусь мамой честно
забыла забыл забыли время час часа часов минут минуты минуту мин назад через лишним щас сейчас потом
вчера сегодня все всех всё был была были бегу бегаю бежал бежала пробыла пробыл пробыли отписала отписал
отписывала отписывал для на по за из от до со что как так или если когда где чтобы тоже случайно
видимо наверное кажется примерно около вроде типа возможно с в к у о мой моя""".split())
APPROX_W = {"видимо", "наверное", "кажется", "примерно", "около", "вроде", "типа", "возможно", "хз"}


def segments(text: str):
    """Текст ПОСЛЕ каждого «#бег» (то, что стоит перед первым #бег, не учитывается)."""
    return [p.strip() for p in RE_RUN.split(text)[1:] if p.strip()]


def clock_ts(msg_ts: int, h: int, m: int):
    """ЧЧ:ММ → ближайший к моменту сообщения момент (вчера/сегодня/завтра)."""
    if h > 23 or m > 59:
        return None
    d0 = datetime.fromtimestamp(msg_ts, MSK).replace(hour=h, minute=m, second=0, microsecond=0)
    return min((int((d0 + timedelta(days=k)).timestamp()) for k in (-1, 0, 1)), key=lambda x: abs(x - msg_ts))


def _tokenize(s: str):
    T = []
    for m in TOK.finditer(s):
        if m.group(1) is not None:
            T.append({"k": "t", "v": (int(m.group(1)), int(m.group(2)))})
        elif m.group(3) is not None:
            T.append({"k": "n", "v": float(m.group(3).replace(",", "."))})
        elif m.group(4):
            T.append({"k": "w", "v": m.group(4)})
        else:
            T.append({"k": "s", "v": m.group(5)})
    # «у ш л и» → «ушли»: три и больше одиночных кириллических букв подряд
    out, i = [], 0
    while i < len(T):
        j = i
        while j < len(T) and T[j]["k"] == "w" and re.fullmatch("[а-я]", T[j]["v"]):
            j += 1
        if j - i >= 3:
            out.append({"k": "w", "v": "".join(t["v"] for t in T[i:j])})
            i = j
        else:
            out.append(T[i])
            i += 1
    for t in out:
        if t["k"] == "w":
            if re.fullmatch("[a-z]+", t["v"]):  # слово набрано в латинской раскладке
                t["orig"], t["v"] = t["v"], "".join(LAYOUT.get(c, c) for c in t["v"])
            t["v"] = re.sub(r"(.)\1{2,}", r"\1", t["v"])
    return out


def _intent(w: str, vocab: set):
    if len(w) < 4 or w in vocab:
        return None
    if w in IN_W or w.startswith("пашл"):
        return "in"
    if w in OUT_W or w.startswith(("ушл", "уше")):
        return "out"
    if len(w) >= 5:
        m = difflib.get_close_matches(w, IN_W + OUT_W, n=1, cutoff=0.8)
        if m:
            return "in" if m[0] in IN_W else "out"
    return None


def _lookup(cand: str, size: int, mem, keys):
    """(список записей | None, точное ли совпадение)."""
    if size == 1 and len(cand) < 3:
        return None, False
    exact = (mem.by_clean.get(cand) or (mem.by_first2.get(cand) if size == 2 else None)
             or (mem.by_first1.get(cand) if size == 1 else None))
    if exact:
        return exact, True
    if size == 1:
        if len(cand) >= 5:
            m = difflib.get_close_matches(cand, keys["first1"], n=1, cutoff=0.78)
            if m:
                return mem.by_first1[m[0]], False
    else:
        m = difflib.get_close_matches(cand, keys["clean"] if size == 3 else keys["first2"] + keys["clean"],
                                      n=1, cutoff=0.85)
        if m:
            return mem.by_clean.get(m[0]) or mem.by_first2.get(m[0]), False
    return None, False


def parse(text: str, msg_ts: int, members, author: dict):
    """
    author = {"vk_id": int, "name": str, "known": bool} — на него относится отпись без имён.
    Возвращает словарь: people, all, has_in, has_out, in_ts, out_ts, deduct, flags.
    """
    flags, people = [], []
    mentions = []

    def _men(m):
        mentions.append((int(m.group(1)), m.group(2)))
        return " "
    s = MENTION.sub(_men, text).lower().replace("ё", "е")
    T = _tokenize(s)
    vocab = {w for k in (members.by_clean if members else {}) for w in k.split()}

    # вычет минут: «-30 минут», «минус 15 мин», «вычтите мне 15 минут»
    deduct = 0
    for i, t in enumerate(T):
        word_trig = t["k"] == "w" and t["v"].startswith(("вычт", "вычес", "минус", "отним"))
        sign_trig = t["k"] == "s" and t["v"] in ("-", "−", "–")
        if t.get("used") or not (word_trig or sign_trig):
            continue
        for j in range(i + 1, min(i + (2 if sign_trig else 5), len(T))):
            if T[j]["k"] == "n":
                nxt = T[j + 1] if j + 1 < len(T) else None
                if nxt and nxt["k"] == "w" and (nxt["v"].startswith("мин") or nxt["v"] == "м"):
                    deduct += int(T[j]["v"])
                    for x in T[i:j + 2]:
                        x["used"] = True
                break
    # «40 минут назад», «час назад», «полчаса назад» → относительное время
    i = 0
    while i < len(T) - 1:
        t, nx = T[i], T[i + 1]
        secs = None
        if t["k"] == "n" and nx["k"] == "w" and i + 2 < len(T) and T[i + 2]["v"] == "назад":
            if nx["v"].startswith("мин"):
                secs = t["v"] * 60
            elif nx["v"].startswith("час"):
                secs = t["v"] * 3600
            span = 3
        elif t["k"] == "w" and t["v"] in ("час", "полчаса") and nx["v"] == "назад":
            secs, span = (3600 if t["v"] == "час" else 1800), 2
        if secs and not t.get("used"):
            T[i:i + span] = [{"k": "rel", "v": -int(secs)}]
        i += 1
    # неразобранные «N минут» — просим проверить
    for i, t in enumerate(T):
        if (t["k"] == "n" and not t.get("used") and i + 1 < len(T) and T[i + 1]["k"] == "w"
                and T[i + 1]["v"].startswith("мин")):
            flags.append("в тексте есть минуты, которые бот не применил — проверьте")
            break

    # намерения
    for t in T:
        if t["k"] == "w" and not t.get("used"):
            t["intent"] = _intent(t["v"], vocab)
    intents = [(i, t["intent"]) for i, t in enumerate(T) if t.get("intent")]
    has_in = any(k == "in" for _, k in intents)
    has_out = any(k == "out" for _, k in intents)

    # время: роль по предлогу («до» — уход, «с» — приход) или по ближайшему слову-намерению
    in_ts = out_ts = None
    for i, t in enumerate(T):
        if t["k"] not in ("t", "rel"):
            continue
        ts = clock_ts(msg_ts, *t["v"]) if t["k"] == "t" else msg_ts + t["v"]
        if ts is None:
            continue
        prev = T[i - 1] if i else {}
        role = "out" if prev.get("v") == "до" else ("in" if prev.get("v") in ("с", "со", "от") else None)
        if role is None and intents:  # ближайшее слово-намерение ПЕРЕД временем, иначе ближайшее после
            before = [ik for ik in intents if ik[0] < i]
            after = [ik for ik in intents if ik[0] > i]
            if before and (i - before[-1][0] <= 4 or not after):
                role = before[-1][1]
            else:
                role = after[0][1]
        if role is None:
            flags.append("в тексте есть время, но не понятно, к чему оно относится")
        elif role == "in":
            has_in, in_ts = True, (in_ts if in_ts is not None else ts)
            if t["k"] == "rel":
                flags.append("время прихода указано как «N минут назад»")
        else:
            has_out, out_ts = True, (out_ts if out_ts is not None else ts)
            if t["k"] == "rel":
                flags.append("время ухода указано как «N минут назад»")
    if has_in and has_out and in_ts is None and out_ts is None:
        flags.append("приход и уход без времени — укажите время начала")
    if any(t["k"] == "w" and t["v"] in APPROX_W for t in T) or any(t["k"] == "s" and ("+-" in t["v"] or "±" in t["v"]
                                                                                       or "~" in t["v"]) for t in T):
        flags.append("отпись неточная («видимо», «+-» и т. п.)")

    # имена
    all_flag = any(t["k"] == "w" and t["v"] in ("все", "всех") for t in T) and (has_in or has_out)
    for vid, shown in mentions:
        rec = (members.by_vk.get(vid) or [None])[0] if members else None
        people.append({"name": (rec["name"] if rec and rec["name"] else shown.strip().title()),
                       "vk_id": vid, "known": bool(rec), "flags": []})
    if members:
        keys = {"clean": list(members.by_clean), "first2": list(members.by_first2),
                "first1": list(members.by_first1)}

        def avail(j):
            return (j < len(T) and T[j]["k"] == "w" and not T[j].get("used") and not T[j].get("intent")
                    and T[j]["v"] not in STOP)
        i = 0
        while i < len(T):
            if not avail(i):
                i += 1
                continue
            for size in (3, 2, 1):
                if not all(avail(i + d) for d in range(size)):
                    continue
                cand = " ".join(T[i + d]["v"] for d in range(size))
                recs, exact = _lookup(cand, size, members, keys)
                if not recs:
                    continue
                fl = []
                if len({str(r["cw_id"]) for r in recs}) > 1:
                    p = {"name": cand.title(), "vk_id": None, "known": False,
                         "flags": [f"имя «{cand}» неоднозначно на листе «Участники»"]}
                else:
                    r = recs[0]
                    if not exact:
                        fl.append(f"имя «{cand}» распознано как «{r['name']}»")
                    p = {"name": r["name"] or cand.title(), "vk_id": r["vk_id"], "known": True, "flags": fl}
                if any("orig" in T[i + d] for d in range(size)):
                    p["flags"].append("имя набрано в другой раскладке")
                if all(x["name"] != p["name"] for x in people):
                    people.append(p)
                for d in range(size):
                    T[i + d]["used"] = True
                break
            i += 1
    for t in T:  # слова в другой раскладке, ставшие командой
        if t.get("intent") and "orig" in t:
            flags.append("слово набрано в другой раскладке")
            break
    if not people and not all_flag and (has_in or has_out or deduct):
        left = [t["v"] for t in T if t["k"] == "w" and not t.get("used") and not t.get("intent")
                and t["v"] not in STOP and len(t["v"]) >= 4][:3]
        people.append({"name": author["name"], "vk_id": author["vk_id"], "known": author.get("known", False),
                       "flags": ([f"имя из текста не найдено на листе: «{' '.join(left)}» — записан автор"]
                                 if left else [])})
    if deduct:
        flags.append(f"вычет {deduct} мин по отписи участника")
    return {"people": people, "all": all_flag, "has_in": has_in, "has_out": has_out,
            "in_ts": in_ts, "out_ts": out_ts, "deduct": deduct, "flags": list(dict.fromkeys(flags))}
