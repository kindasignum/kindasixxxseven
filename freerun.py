"""
Разбор отписей свободного бега: «#бег Имя пришёл/ушёл» в свободной форме.

Модуль не зависит от бота: parse() получает текст, время сообщения, справочник sheets.Members, автора и
историю недавних отписей, а возвращает, КТО (people), ЧТО сделал (has_in / has_out), КОГДА (in_ts / out_ts,
None — «время сообщения»), вычет минут (deduct) и пометки для администратора (flags).
Что понято неуверенно — попадает в flags; несуществующее время даёт invalid=True (бот ответит ❓).
"""
import difflib
import re
import types
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
        "стартовала", "стартовали", "вернулся", "вернулась", "вернулись"]
OUT_W = ["ушел", "ушла", "ушли", "ушол", "убежал", "убежала", "убежали", "отвалился", "отвалилась",
         "отвалились", "закончил", "закончила", "закончили", "финишировал", "финишировала",
         "финишировали", "отбегал", "отбегала", "отбегали", "завершил", "завершила", "завершили",
         "отошел", "отошла", "отошли"]
INTENT_WORDS = sorted(IN_W + OUT_W, key=len, reverse=True)
STOP = set("""и а но я мы ты он она они мне меня нам нас еще уже тоже тут здесь там то это есть нет да ну ладно
короче ваще вообще просто пожалуйста прошу прощения блять блядь сорри извините спасибо клянусь мамой честно
забыла забыл забыли время час часа часов минут минуты минуту мин назад через лишним щас сейчас потом
вчера сегодня все всех всё был была были бегу бегаю бежал бежала пробыла пробыл пробыли отписала отписал
отписывала отписывал для на по за из от до со что как так или если когда где чтобы тоже случайно
видимо наверное кажется примерно около вроде типа возможно с в к у о мой моя ровно""".split())
APPROX_W = {"видимо", "наверное", "кажется", "примерно", "около", "вроде", "типа", "возможно", "хз"}
HOUR_PREP = ("в", "до", "с", "со", "к", "около", "примерно", "ровно")
DAYPART = {"ночи": 0, "утра": 0, "дня": 12, "вечера": 12}


def segments(text: str):
    """
    Тексты отписей. Берётся то, что стоит ПОСЛЕ каждого «#бег»; если после него ничего нет
    (хештег в конце сообщения) — то, что стоит перед ним.
    """
    parts = RE_RUN.split(text)
    after = [p.strip() for p in parts[1:] if p.strip()]
    if after:
        return after
    before = parts[0].strip()
    return [before] if before and len(parts) > 1 else []


def clock_ts(msg_ts: int, h: int, m: int):
    """ЧЧ:ММ → ближайший к моменту сообщения момент (вчера/сегодня/завтра). None — времени не бывает."""
    if h > 23 or m > 59:
        return None
    d0 = datetime.fromtimestamp(msg_ts, MSK).replace(hour=h, minute=m, second=0, microsecond=0)
    return min((int((d0 + timedelta(days=k)).timestamp()) for k in (-1, 0, 1)), key=lambda x: abs(x - msg_ts))


# ───────────────────────── токены ─────────────────────────
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
    out, i = [], 0  # «у ш л и» → «ушли»
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


def _split_glued(T, vocab):
    """«пламяушел» → «пламя» + «ушел» (команда и имя написаны слитно)."""
    out = []
    for t in T:
        w = t["v"] if t["k"] == "w" else ""
        if len(w) >= 7 and w not in vocab and w not in IN_W and w not in OUT_W:
            for iw in INTENT_WORDS:
                if w.endswith(iw) and len(w) - len(iw) >= 3:
                    out += [dict(t, v=w[:-len(iw)]), {"k": "w", "v": iw}]
                    break
                if w.startswith(iw) and len(w) - len(iw) >= 3:
                    out += [{"k": "w", "v": iw}, dict(t, v=w[len(iw):])]
                    break
            else:
                out.append(t)
            continue
        out.append(t)
    return out


def _intent(w: str, vocab: set):
    if len(w) < 4 or w in vocab:
        return None
    if w in IN_W or w.startswith("пашл"):
        return "in"
    if w in OUT_W or w.startswith(("ушл", "уше")):
        return "out"
    if len(w) >= 5:
        m = difflib.get_close_matches(w, INTENT_WORDS, n=1, cutoff=0.8)
        if m:
            return "in" if m[0] in IN_W else "out"
    return None


# ───────────────────────── имена ─────────────────────────
def _dl(a: str, b: str) -> int:
    """Расстояние Дамерау — Левенштейна (учитывает перестановку соседних букв: «плаям» → «пламя»)."""
    la, lb = len(a), len(b)
    d = [[0] * (lb + 1) for _ in range(la + 1)]
    for i in range(la + 1):
        d[i][0] = i
    for j in range(lb + 1):
        d[0][j] = j
    for i in range(1, la + 1):
        for j in range(1, lb + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[la][lb]


def _sim(tok: str, key: str) -> float:
    """Похожесть слова на имя: опечатки, перестановки, а также «начало имени» (звезд → звездочет)."""
    best = max(difflib.SequenceMatcher(None, tok, key).ratio(), 1 - _dl(tok, key) / max(len(tok), len(key)))
    if len(tok) >= 4 and len(key) > len(tok):
        for L in (len(tok), len(tok) + 1):
            best = max(best, 1 - _dl(tok, key[:L]) / max(len(tok), L) - 0.02)
    return best


def _index(mem):
    idx = getattr(mem, "_fr_idx", None)
    if idx:
        return idx
    nospace = {}
    for d in (mem.by_clean, mem.by_first2):
        for k, recs in d.items():
            if " " in k:
                nospace.setdefault(k.replace(" ", ""), []).extend(recs)
    idx = types.SimpleNamespace(
        nospace=nospace,
        fz1=dict(mem.by_first1),
        fz2={**mem.by_first2, **{k: v for k, v in mem.by_clean.items() if " " in k}},
        fz3={k: v for k, v in mem.by_clean.items() if k.count(" ") >= 2},
    )
    mem._fr_idx = idx
    return idx


def _rank(name: str, hist) -> int:
    if not hist:
        return 999
    if name in hist.get("sender", []):
        return hist["sender"].index(name)
    if name in hist.get("chat", []):
        return 100 + hist["chat"].index(name)
    return 999


def _match(cand: str, size: int, mem, hist):
    """
    Ищет персонажа по слову/словам. Возвращает (запись | None, пометки, варианты-при-неоднозначности).
    Точное совпадение → опечатки/перестановки/начало имени → слитное написание. Если подходят несколько
    персонажей — выбирается тот, что недавно отписывался (этот человек или беседа), иначе — неоднозначно.
    """
    idx = _index(mem)
    if size == 1 and len(cand) < 3:
        return None, [], []
    recs = mem.by_clean.get(cand)
    if not recs and size == 2:
        recs = mem.by_first2.get(cand) or idx.nospace.get(cand.replace(" ", ""))
    if not recs and size == 1:
        recs = mem.by_first1.get(cand) or idx.nospace.get(cand)
    cands, exact = {}, bool(recs)
    for r in recs or []:
        cands[r["name"]] = (1.0, r)
    if not cands:
        if size == 1:
            if len(cand) < 5:
                return None, [], []
            keys = idx.fz1 if len(cand) < 8 else {**idx.fz1, **idx.nospace}
            cut = 0.78
        else:
            keys, cut = (idx.fz2 if size == 2 else idx.fz3), 0.85
        for k, rs in keys.items():
            sc = _sim(cand, k)
            if sc >= cut:
                for r in rs:
                    if r["name"] not in cands or cands[r["name"]][0] < sc:
                        cands[r["name"]] = (sc, r)
    if not cands:
        return None, [], []
    best = max(sc for sc, _ in cands.values())
    top = {n: v for n, v in cands.items() if v[0] >= (best if exact else best - 0.12)}
    flags = []
    if len(top) > 1:
        ranked = sorted(top, key=lambda n: (_rank(n, hist), -top[n][0]))
        if _rank(ranked[0], hist) < 999 and (_rank(ranked[0], hist) < _rank(ranked[1], hist)):
            flags.append(f"имя «{cand}»: выбрано по недавним отпискам из вариантов: {', '.join(ranked[:4])}")
            top = {ranked[0]: top[ranked[0]]}
        else:
            return None, [], sorted(top, key=lambda n: -top[n][0])[:4]
    name, (sc, rec) = next(iter(top.items()))
    if not exact:
        flags.append(f"имя «{cand}» распознано как «{rec['name']}»")
    return rec, flags, []


# ───────────────────────── разбор ─────────────────────────
def parse(text: str, msg_ts: int, members, author: dict, history: dict = None):
    """
    author  = {"vk_id", "name", "known"} — запасной вариант, если имя нигде не найдено.
    history = {"last": [{"name","vk_id"}], "sender": [имена], "chat": [имена]} — недавние отписи
              (для сообщений без имени и для выбора между похожими именами).
    Возвращает: people, explicit (имена названы в тексте), all, has_in, has_out, in_ts, out_ts, deduct,
    flags, invalid.
    """
    history = history or {}
    flags, people, mentions = [], [], []

    def _men(m):
        mentions.append((int(m.group(1)), m.group(2)))
        return " "
    s = MENTION.sub(_men, text).lower().replace("ё", "е")
    vocab = {w for k in (members.by_clean if members else {}) for w in k.split()}
    T = _split_glued(_tokenize(s), vocab)

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
    # «40 минут назад», «час назад» (назад) и «через час», «через 20 минут» (вперёд)
    i = 0
    while i < len(T):
        t = T[i]
        nx = T[i + 1] if i + 1 < len(T) else {}
        n2 = T[i + 2] if i + 2 < len(T) else {}
        new = None
        if t["k"] == "n" and nx.get("k") == "w" and n2.get("v") == "назад" and not t.get("used"):
            if nx["v"].startswith("мин"):
                new, span = {"k": "rel", "v": -int(t["v"] * 60)}, 3
            elif nx["v"].startswith("час"):
                new, span = {"k": "rel", "v": -int(t["v"] * 3600)}, 3
        elif t["k"] == "w" and t["v"] in ("час", "полчаса") and nx.get("v") == "назад":
            new, span = {"k": "rel", "v": -(3600 if t["v"] == "час" else 1800)}, 2
        elif t["k"] == "w" and t["v"] == "через":
            secs, span = None, 0
            if nx.get("k") == "n" and n2.get("k") == "w" and n2["v"].startswith(("мин", "час")):
                secs, span = int(nx["v"] * (60 if n2["v"].startswith("мин") else 3600)), 3
            elif nx.get("k") == "w" and nx["v"].startswith("час"):
                secs, span = 3600, 2
            elif nx.get("k") == "w" and nx["v"].startswith("мин"):
                secs, span = 60, 2
            elif nx.get("k") == "w" and nx["v"] == "полчаса":
                secs, span = 1800, 2
            if secs:
                tail = [x.get("v") for x in T[i + span:i + span + 3]]
                if any(w in ("лишним", "щас", "сейчас", "хвостиком") for w in tail):
                    flags.append("«через час…» не применено: из текста похоже, что время уже прошло")
                    for x in T[i:i + span]:
                        x["used"] = True
                else:
                    new = {"k": "fwd", "v": secs}
        if new:
            T[i:i + span] = [new]
        i += 1
    # «в 3», «до 5 часов», «в 8 вечера» → время; без «дня/вечера» меньше 13 — неоднозначно (3 или 15 часов)
    i = 0
    while i < len(T) - 1:
        if (T[i]["k"] == "w" and T[i]["v"] in HOUR_PREP and T[i + 1]["k"] == "n" and not T[i + 1].get("used")
                and T[i + 1]["v"] == int(T[i + 1]["v"]) and 0 <= T[i + 1]["v"] <= 24):
            after = T[i + 2] if i + 2 < len(T) else {}
            if not (after.get("k") == "w" and after["v"].startswith(("мин", "сек"))):
                h, span, amb = int(T[i + 1]["v"]) % 24, 1, None
                if after.get("k") == "w" and after["v"].startswith("час"):
                    span = 2
                    part = T[i + 2 + 0 + 1].get("v") if i + 3 < len(T) else None
                    if part in DAYPART:
                        span = 3
                        h = (h % 12) + DAYPART[part]
                    elif h <= 12:
                        amb = h
                elif after.get("k") == "w" and after["v"] in DAYPART:
                    span, h = 2, (h % 12) + DAYPART[after["v"]]
                elif h <= 12:
                    amb = h
                T[i + 1:i + 1 + span] = [{"k": "t", "v": (h, 0), "amb": amb}]
        i += 1
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
    shift = {"in": 0, "out": 0}
    invalid = False
    for i, t in enumerate(T):
        if t["k"] not in ("t", "rel", "fwd"):
            continue
        ts = None
        if t["k"] == "t":
            h, m = t["v"]
            if h > 23 and 1 <= m <= 12 and h <= 31:  # похоже на дату «24.10», а не на время
                continue
            if t.get("amb") is not None:  # «в 3»: 03:00 или 15:00 — берём ближайшее к времени сообщения
                opts = [clock_ts(msg_ts, h, 0), clock_ts(msg_ts, (h + 12) % 24, 0)]
                ts = min(opts, key=lambda x: abs(x - msg_ts))
                flags.append(f"время «в {h}» понято как {datetime.fromtimestamp(ts, MSK):%H:%M}")
            else:
                ts = clock_ts(msg_ts, h, m)
                if ts is None:
                    invalid = True
                    flags.append(f"несуществующее время «{h}:{m:02d}»")
                    continue
        elif t["k"] == "rel":
            ts = msg_ts + t["v"]
        prev = T[i - 1] if i else {}
        role = "out" if prev.get("v") == "до" else ("in" if prev.get("v") in ("с", "со", "от") else None)
        if role is None and intents:  # ближайшее слово-намерение ПЕРЕД временем, иначе ближайшее после
            before = [ik for ik in intents if ik[0] < i]
            after = [ik for ik in intents if ik[0] > i]
            role = before[-1][1] if before and (i - before[-1][0] <= 4 or not after) else after[0][1]
        if role is None:
            flags.append("в тексте есть время, но не понятно, к чему оно относится")
            continue
        if t["k"] == "fwd":
            shift[role] += t["v"]
            continue
        if role == "in":
            has_in, in_ts = True, (in_ts if in_ts is not None else ts)
        else:
            has_out, out_ts = True, (out_ts if out_ts is not None else ts)
        if t["k"] == "rel":
            flags.append(f"время {'прихода' if role == 'in' else 'ухода'} указано как «N минут назад»")
    if shift["in"]:
        has_in, in_ts = True, (in_ts if in_ts is not None else msg_ts) + shift["in"]
    if shift["out"]:
        has_out, out_ts = True, (out_ts if out_ts is not None else msg_ts) + shift["out"]
    if shift["in"] or shift["out"]:
        flags.append("время сдвинуто по приписке «через …»")
    if has_in and has_out and in_ts is None and out_ts is None:
        flags.append("приход и уход без времени — укажите время начала")
    if any(t["k"] == "w" and t["v"] in APPROX_W for t in T) or any(
            t["k"] == "s" and ("+-" in t["v"] or "±" in t["v"] or "~" in t["v"]) for t in T):
        flags.append("отпись неточная («видимо», «+-» и т. п.)")
    if invalid:
        return {"people": [], "explicit": False, "all": False, "has_in": False, "has_out": False, "in_ts": None,
                "out_ts": None, "deduct": 0, "flags": list(dict.fromkeys(flags)), "invalid": True,
                "norm": s, "unknown": []}

    # имена
    all_flag = any(t["k"] == "w" and t["v"] in ("все", "всех") for t in T) and (has_in or has_out)
    for vid, shown in mentions:
        rec = (members.by_vk.get(vid) or [None])[0] if members else None
        people.append({"name": (rec["name"] if rec and rec["name"] else shown.strip().title()),
                       "vk_id": vid, "known": bool(rec), "flags": []})
    if members:
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
                rec, fl, variants = _match(cand, size, members, history)
                if rec is None and not variants:
                    continue
                if rec is None:  # несколько подходящих персонажей, выбрать нечем — админ выберет сам
                    p = {"name": cand.title(), "vk_id": None, "known": False,
                         "flags": [f"имя «{cand}» неоднозначно, подходят: {', '.join(variants)}"]}
                else:
                    p = {"name": rec["name"] or cand.title(), "vk_id": rec["vk_id"], "known": True,
                         "flags": list(fl)}
                if any("orig" in T[i + d] for d in range(size)):
                    p["flags"].append("имя набрано в другой раскладке")
                if all(x["name"] != p["name"] for x in people):
                    people.append(p)
                for d in range(size):
                    T[i + d]["used"] = True
                break
            i += 1
    for t in T:
        if t.get("intent") and "orig" in t:
            flags.append("слово набрано в другой раскладке")
            break
    explicit = bool(people)
    if not people and not all_flag and (has_in or has_out or deduct):
        left = [t["v"] for t in T if t["k"] == "w" and not t.get("used") and not t.get("intent")
                and t["v"] not in STOP and len(t["v"]) >= 4][:3]
        note = f" (слова «{' '.join(left)}» не найдены на листе)" if left else ""
        if history.get("last"):
            for p in history["last"]:
                people.append({"name": p["name"], "vk_id": p.get("vk_id"), "known": True,
                               "flags": [f"имя не указано — взято из прошлой отписи этого автора{note}"]})
        else:
            people.append({"name": author["name"], "vk_id": author["vk_id"], "known": author.get("known", False),
                           "flags": [f"имя не указано — записан автор сообщения{note}"]})
    if deduct:
        flags.append(f"вычет {deduct} мин по отписи участника")
    # слова, которые бот не отнёс ни к имени, ни к действию, ни к «шумовым» (для отладки непонятых отписей)
    unknown = [t["v"] for t in T if t["k"] == "w" and not t.get("used") and not t.get("intent") and t["v"] not in STOP]
    return {"people": people, "explicit": explicit, "all": all_flag, "has_in": has_in, "has_out": has_out,
            "in_ts": in_ts, "out_ts": out_ts, "deduct": deduct, "flags": list(dict.fromkeys(flags)),
            "invalid": False, "norm": s, "unknown": unknown}
