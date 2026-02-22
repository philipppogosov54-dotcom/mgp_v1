"""
Yandex GPT Function Calling Handler (Responses API)
Связывает AI модель с TourVisor API
Миграция на Responses API с встроенным web_search
+ Поддержка Streaming и асинхронности
"""

import os
import json
import asyncio
import time
import logging
import re
from datetime import datetime as _dt, timedelta as _td
from difflib import SequenceMatcher
from typing import Optional, Dict, Any, List, Callable, AsyncIterator, Tuple
import requests
from dotenv import load_dotenv
from tourvisor_client import (
    TourVisorClient,
    TourIdExpiredError,
    SearchNotFoundError,
    NoResultsError
)

load_dotenv()

logger = logging.getLogger("mgp_bot")

# Тип для callback функции streaming
StreamCallback = Callable[[str], None]

# ── Hotel name search helpers ──────────────────────────────────────────────
_CYR_TO_LAT = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd',
    'е': 'e', 'ё': 'e', 'ж': 'zh', 'з': 'z', 'и': 'i',
    'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm', 'н': 'n',
    'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't',
    'у': 'u', 'ф': 'f', 'х': 'h', 'ц': 'ts', 'ч': 'ch',
    'ш': 'sh', 'щ': 'sch', 'ъ': '', 'ы': 'y', 'ь': '',
    'э': 'e', 'ю': 'yu', 'я': 'ya',
}
_CYR_TO_LAT_ALT = {**_CYR_TO_LAT, 'ф': 'ph', 'х': 'kh'}


def _transliterate(text: str, mapping: dict = None) -> str:
    """Cyrillic → Latin transliteration optimised for hotel name matching."""
    m = mapping or _CYR_TO_LAT
    return ''.join(m.get(c, c) for c in text.lower())


def _fuzzy_hotel_match(queries, hotels: list, threshold: float = 0.65) -> list:
    """Word-level fuzzy matching of one or more *queries* against hotel names.

    For each hotel the best score across ALL query variants is kept.
    Returns hotels above *threshold*, sorted by score descending.
    """
    if isinstance(queries, str):
        queries = [queries]
    scored: list = []
    for h in hotels:
        hotel_name = h.get("name", "").lower()
        hotel_words = hotel_name.split()
        if not hotel_words:
            continue
        best_score = 0.0
        for query in queries:
            query_words = query.lower().split()
            if not query_words:
                continue
            best_per_qword = []
            for qw in query_words:
                word_scores = [SequenceMatcher(None, qw, hw).ratio() for hw in hotel_words]
                best_per_qword.append(max(word_scores))
            avg_score = sum(best_per_qword) / len(best_per_qword)
            full_ratio = SequenceMatcher(None, query.lower(), hotel_name).ratio()
            best_score = max(best_score, avg_score, full_ratio)
        if best_score >= threshold:
            scored.append((best_score, h))
    scored.sort(key=lambda x: -x[0])
    return [h for _, h in scored]


def _is_self_moderation(text: str) -> bool:
    """
    Детектирует ответы самомодерации Yandex GPT.
    Модель иногда генерирует "Я не могу обсуждать эту тему" вместо реального ответа
    при запутанном контексте. Это НЕ ответ, а ошибка, которую нужно обработать.
    """
    if not text:
        return False
    lower = text.lower().strip().lstrip('#').strip()
    moderation_phrases = [
        "не могу обсуждать эту тему",
        "я не могу обсуждать",
        "не могу помочь с этим",
        "давайте поговорим о чём-нибудь",
        "поговорим о чём-нибудь ещё",
        "я не могу отвечать на этот вопрос",
    ]
    return any(phrase in lower for phrase in moderation_phrases)


def _is_promised_search(text: str) -> bool:
    """
    Детектирует ситуацию когда модель ПООБЕЩАЛА выполнить поиск/действие,
    но вернула текст вместо function_call.
    Например: «Сейчас начну поиск подходящих туров для вас.»
    Это КРИТИЧЕСКАЯ ОШИБКА — модель должна вызывать функцию, а не описывать намерение.
    
    Синхронизировано с system_prompt.md § 0.0.1
    """
    if not text:
        return False
    lower = text.lower().strip()
    
    # Полный список запрещённых фраз (синхронизирован с system_prompt.md § 0.0.1)
    promise_phrases = [
        # Поиск
        "начну поиск", "начинаю поиск", "запускаю поиск", "приступаю к поиску",
        "сейчас поищу", "сейчас найду", "сейчас подберу", "сейчас подбираю",
        # Подбор
        "начну подбор", "начинаю подбор",
        "подберу для вас", "поищу для вас", "найду для вас",
        # Поиск вариантов
        "ищу подходящие", "ищу для вас", "ищу варианты",
        # Давайте...
        "давайте поищу", "давайте найду", "давайте подберу",
        # Сейчас проверю/узнаю (для actualize_tour, get_hotel_info и т.д.)
        "сейчас посмотрю", "сейчас проверю", "сейчас узнаю",
        "сейчас уточню", "сейчас загружу",
        # Момент/секунду
        "момент, ищу", "секунду, подбираю", "минуту, проверяю",
        "одну секунду", "один момент",
        # Статус поиска (модель описывает запущенный процесс вместо вызова функции)
        "поиск запущен", "ожидаю результат", "жду результат",
        "запущен, ожидаю", "результаты скоро будут",
    ]
    return any(phrase in lower for phrase in promise_phrases)


# ── FIX B3: Список всех валидных имён функций (из function_schemas.json) ──
_VALID_FUNCTION_NAMES = frozenset([
    "get_current_date", "search_tours", "get_search_status", "get_search_results",
    "continue_search", "get_dictionaries", "actualize_tour", "get_tour_details",
    "get_hotel_info", "get_hot_tours",
])

# Regex: function_name(...)  — Python-like вызов
_RE_PLAINTEXT_CALL = re.compile(
    r'(?:```[a-z]*\s*\n?)?\b(' + '|'.join(_VALID_FUNCTION_NAMES) + r')\s*\(([^)]*)\)\s*(?:\n?```)?',
    re.IGNORECASE | re.DOTALL
)

# P7: Regex: function_name\n{json} — модель пишет имя функции, затем JSON на новой строке (сценарий 4)
_FUNC_NAMES_PATTERN = '|'.join(_VALID_FUNCTION_NAMES)
_RE_FUNCNAME_NEWLINE_JSON = re.compile(
    r'\b(' + _FUNC_NAMES_PATTERN + r')\s*\n\s*(\{[^}]+\})',
    re.IGNORECASE | re.DOTALL
)

# P7: Regex: [TOOL_CALL_START]function_name\n{json} (сценарий 7)
_RE_TOOL_CALL_START = re.compile(
    r'\[TOOL_CALL_START\]\s*(' + _FUNC_NAMES_PATTERN + r')\s*(?:\n\s*(\{[^}]+\}))?',
    re.IGNORECASE | re.DOTALL
)

# P7: Regex: {"role": "assistant", "message": "text"} — JSON-обёртка (сценарий 11)
_RE_JSON_WRAPPER = re.compile(
    r'\{\s*"role"\s*:\s*"assistant"\s*,\s*"message"\s*:\s*"([^"]+)"\s*\}',
    re.IGNORECASE
)


def _parse_python_kwargs(raw: str) -> Dict:
    """
    Парсит Python-like kwargs строку: key1=value1, key2="value2", key3=123
    Также обработает JSON формат: {"key": value, ...}
    Возвращает dict аргументов.
    """
    raw = raw.strip()
    
    # Случай 1: JSON формат (начинается с {)
    if raw.startswith('{'):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    
    # Случай 2: Python kwargs формат: key=value, key2="str", key3=123
    result = {}
    if not raw:
        return result
    
    # Разбиваем по запятым, но уважая кавычки
    parts = []
    current = []
    in_quotes = False
    quote_char = None
    for ch in raw:
        if ch in ('"', "'") and not in_quotes:
            in_quotes = True
            quote_char = ch
            current.append(ch)
        elif ch == quote_char and in_quotes:
            in_quotes = False
            current.append(ch)
        elif ch == ',' and not in_quotes:
            parts.append(''.join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        parts.append(''.join(current).strip())
    
    for part in parts:
        if '=' not in part:
            continue
        key, _, val = part.partition('=')
        key = key.strip()
        val = val.strip()
        
        if not key:
            continue
        
        # Определяем тип значения
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            result[key] = val[1:-1]  # строка
        elif val.lower() in ('true',):
            result[key] = True
        elif val.lower() in ('false',):
            result[key] = False
        elif val.lower() in ('none', 'null'):
            result[key] = None
        else:
            try:
                result[key] = int(val)
            except ValueError:
                try:
                    result[key] = float(val)
                except ValueError:
                    result[key] = val  # строка без кавычек
    
    return result


def _extract_plaintext_tool_calls(text: str) -> List[Tuple[str, str]]:
    """
    FIX B3 + P7: Safety-net для yandexgpt/rc.
    Извлекает вызовы функций из текстового ответа модели.
    
    Поддерживаемые форматы:
    1. search_tours(country=47, departure=99, ...) — Python-like
    2. search_tours({"country": 47, ...}) — Python-like с JSON
    3. function_name\n{json} — имя функции + JSON на новой строке (сценарий 4)
    4. [TOOL_CALL_START]function_name\n{json} — маркер TOOL_CALL_START (сценарий 7)
    
    ⚠️ НЕ поддерживает: [function_name]: {args} — конфликтует с full_history!
    
    Возвращает список кортежей (function_name, arguments_json_string).
    Пустой список если plaintext tool calls не найдены.
    """
    if not text or len(text) > 5000:  # Если текст слишком длинный — вряд ли это plaintext call
        return []
    
    calls = []
    seen = set()  # Дедупликация — избегаем двойного захвата одного вызова
    
    # ── Паттерн 1: function_name(args) — оригинальный формат ──
    for match in _RE_PLAINTEXT_CALL.finditer(text):
        func_name = match.group(1)
        raw_args = match.group(2)
        
        if func_name not in _VALID_FUNCTION_NAMES:
            continue
        
        try:
            parsed_args = _parse_python_kwargs(raw_args)
            args_json = json.dumps(parsed_args, ensure_ascii=False)
            key = (func_name, args_json)
            if key not in seen:
                seen.add(key)
                calls.append((func_name, args_json))
                logger.warning(
                    "⚠️ PLAINTEXT-TOOL-CALL [pattern1]: %s(%s)",
                    func_name, args_json[:200]
                )
        except Exception as e:
            logger.error("❌ PLAINTEXT-TOOL-CALL PARSE ERROR [pattern1]: %s — %s", func_name, e)
    
    # ── Паттерн 2: function_name\n{json} (сценарий 4) ──
    for match in _RE_FUNCNAME_NEWLINE_JSON.finditer(text):
        func_name = match.group(1)
        json_str = match.group(2)
        
        if func_name not in _VALID_FUNCTION_NAMES:
            continue
        
        try:
            parsed = json.loads(json_str)
            args_json = json.dumps(parsed, ensure_ascii=False)
            key = (func_name, args_json)
            if key not in seen:
                seen.add(key)
                calls.append((func_name, args_json))
                logger.warning(
                    "⚠️ PLAINTEXT-TOOL-CALL [pattern2 func\\njson]: %s(%s)",
                    func_name, args_json[:200]
                )
        except (json.JSONDecodeError, Exception) as e:
            logger.error("❌ PLAINTEXT-TOOL-CALL PARSE ERROR [pattern2]: %s — %s", func_name, e)
    
    # ── Паттерн 3: [TOOL_CALL_START]function_name\n{json} (сценарий 7) ──
    for match in _RE_TOOL_CALL_START.finditer(text):
        func_name = match.group(1)
        json_str = match.group(2) if match.group(2) else "{}"
        
        if func_name not in _VALID_FUNCTION_NAMES:
            continue
        
        try:
            parsed = json.loads(json_str)
            # Fix P1+F4: Отклоняем пустые вызовы для функций с обязательными параметрами
            # Fix F4: get_dictionaries({}) без type всегда возвращает ошибку — блокируем
            if not parsed and func_name in ("search_tours", "get_hot_tours", "get_hotel_info",
                                             "get_search_status", "get_search_results",
                                             "get_tour_details", "actualize_tour",
                                             "continue_search", "get_dictionaries"):
                logger.warning("⚠️ PLAINTEXT-TOOL-CALL REJECTED [pattern3]: %s({}) — пустые аргументы", func_name)
                continue
            args_json = json.dumps(parsed, ensure_ascii=False)
            key = (func_name, args_json)
            if key not in seen:
                seen.add(key)
                calls.append((func_name, args_json))
                logger.warning(
                    "⚠️ PLAINTEXT-TOOL-CALL [pattern3 TOOL_CALL_START]: %s(%s)",
                    func_name, args_json[:200]
                )
        except (json.JSONDecodeError, Exception) as e:
            logger.error("❌ PLAINTEXT-TOOL-CALL PARSE ERROR [pattern3]: %s — %s", func_name, e)
    
    # ── Паттерн 4 (Fix C3): JSON-формат {"version": "1.0", "calls": [...]} ──
    # Модель иногда выводит JSON-обёртку вместо вызова через API.
    # Пример из Сценария 13: {"version":"1.0","calls":[{"id":"1","function":"get_current_date","arguments":{}},...]}
    if not calls and '{' in text:
        try:
            # Пробуем найти JSON-объект с "calls" или "function" в тексте
            _json_match = re.search(r'(\{[^{}]*"(?:calls|function)"[^{}]*(?:\{[^{}]*\}[^{}]*)*\})', text, re.DOTALL)
            if _json_match:
                _json_obj = json.loads(_json_match.group(1))
                _json_calls = []
                
                # Формат 1: {"calls": [{"function": "...", "arguments": {...}}, ...]}
                if "calls" in _json_obj and isinstance(_json_obj["calls"], list):
                    _json_calls = _json_obj["calls"]
                # Формат 2: {"function": "...", "arguments": {...}} (одиночный вызов)
                elif "function" in _json_obj:
                    _json_calls = [_json_obj]
                
                for _jc in _json_calls:
                    _fn = _jc.get("function", "")
                    _fa = _jc.get("arguments", {})
                    if _fn in _VALID_FUNCTION_NAMES:
                        _fa_json = json.dumps(_fa, ensure_ascii=False)
                        _key = (_fn, _fa_json)
                        if _key not in seen:
                            seen.add(_key)
                            calls.append((_fn, _fa_json))
                            logger.warning(
                                "⚠️ PLAINTEXT-TOOL-CALL [pattern4 JSON-wrapper]: %s(%s)",
                                _fn, _fa_json[:200]
                            )
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.debug("JSON-wrapper parse attempt failed (non-critical): %s", e)
    
    return calls


def _extract_json_wrapper_message(text: str) -> Optional[str]:
    """
    P7: Извлекает текст из JSON-обёртки {"role": "assistant", "message": "..."}.
    Сценарий 11 — модель оборачивает ответ в JSON вместо прямого текста.
    Возвращает извлечённый текст или None.
    """
    if not text:
        return None
    match = _RE_JSON_WRAPPER.search(text)
    if match:
        extracted = match.group(1)
        logger.warning("⚠️ JSON-WRAPPER detected: extracted message='%s'", extracted[:200])
        return extracted
    return None


_DEPARTURE_PATTERNS = [
    r'\b(?:москв[аыуе]|мск)\b',
    r'\b(?:петербург\w*|питер\w*|спб|санкт-петербург\w*)\b',
    r'\b(?:екатеринбург\w*|еката|екб)\b',
    r'\b(?:новосибирск\w*)\b',
    r'\b(?:казан[ьи]\w*)\b',
    r'\b(?:краснодар\w*)\b',
    r'\b(?:красноярск\w*)\b',
    r'\b(?:самар\w*)\b',
    r'\b(?:уф[аыуе]\w*)\b',
    r'\b(?:перм[ьи]\w*)\b',
    r'\b(?:челябинск\w*)\b',
    r'\b(?:ростов\w*)\b',
    r'\b(?:минеральн\w+\s*вод|мин\s*вод)\b',
    r'\b(?:тюмен[ьи])\b',
    r'\b(?:нижн\w+\s*новгород|нижний)\b',
    r'\b(?:волгоград)\b',
    r'\b(?:воронеж)\b',
    r'\b(?:омск)\b',
    r'\b(?:иркутск)\b',
    r'\b(?:хабаровск)\b',
    r'(?:вылет|вылетаем|летим|улетаем)\s+(?:из|с)\s+\w+',
    r'(?:из|с)\s+\w+\s+(?:вылет|вылетаем|улетаем)',
    r'без\s*перел[её]т',
]


def _check_cascade_slots(full_history: List[Dict], args: Dict, is_follow_up: bool = False) -> Tuple[bool, List[str]]:
    """
    Проверяет, что клиент ЯВНО указал критичные слоты каскада:
      Слот 2 — город вылета
      Слот 3 — даты и длительность
      Слот 4 — состав путешественников
      Слот 5 — Quality Check (звёздность / питание) ИЛИ явный skip
    Возвращает (is_complete, missing_slots).
    
    Синхронизировано с system_prompt.md § 0.0.2 / § 0.4
    
    Логика:
    - Собираем все сообщения пользователя из истории
    - Ищем паттерны, указывающие на явное упоминание каждого слота
    - Если не найдено — слот считается пропущенным
    """
    missing = []
    
    # ── Early pass: если args уже содержат ВСЕ критичные параметры — доверяем модели.
    # Только для follow-up поисков (когда _last_search_params уже заполнен),
    # чтобы модель не могла обойти QC, выдумав stars/meal на первом поиске.
    _dep = args.get("departure")
    _df = args.get("datefrom", "")
    _nf = args.get("nightsfrom")
    _ad = args.get("adults")
    _st = args.get("stars")
    _ml = args.get("meal")
    if (is_follow_up
            and _dep and isinstance(_dep, int) and _dep > 0
            and _df and re.match(r'\d{2}\.\d{2}\.\d{4}', str(_df))
            and _nf and isinstance(_nf, int) and _nf >= 3
            and _ad and isinstance(_ad, int) and _ad > 0
            and ((_st and isinstance(_st, int) and _st > 0)
                 or (_ml and isinstance(_ml, int) and _ml > 0))):
        return (True, [])
    
    # Собираем ВСЕ сообщения пользователя из истории (не только [-20:]),
    # чтобы оригинальный запрос не выпадал из окна после многих function-call циклов.
    # Fix C1: Исключаем результаты функций (хранятся как role="user"),
    # чтобы даты из get_current_date не обманывали валидатор дат каскада
    user_messages = [
        msg.get("content", "") for msg in full_history
        if msg.get("role") == "user" and msg.get("content")
        and not msg.get("content", "").startswith("Результаты вызванных функций")
        and not msg.get("content", "").startswith("Результаты запросов:")
    ]
    user_text = " ".join(user_messages).lower()
    
    # ─── Слот 2: Город вылета ───
    has_departure_mention = any(re.search(p, user_text) for p in _DEPARTURE_PATTERNS)
    
    if not has_departure_mention:
        missing.append("город вылета")
    
    # ─── Слот 3: Даты/месяц вылета ───
    # Fix P5: Разделяем на "конкретные даты" и "голый месяц"
    # Конкретные даты / части месяца / праздники = слот заполнен
    # Голый месяц (без начале/середине/конце) = нужно уточнить промежуток
    
    _MONTH_NAMES_RX = r'(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)'
    
    # Паттерны для КОНКРЕТНЫХ дат (слот полностью заполнен — НЕ спрашиваем)
    specific_date_patterns = [
        r'\d{1,2}\.\d{1,2}(?:\.\d{2,4})?',                           # 21.03 или 21.03.2026
        r'\d{1,2}\s+' + _MONTH_NAMES_RX,                              # "15 марта" — конкретная дата
        r'(?:в\s+)?(?:начал|середин|конц)\w*\s+' + _MONTH_NAMES_RX,   # "в начале марта" — часть месяца
        r'(?:в\s+)?(?:начал|середин|конц)\w*\s+месяца',               # "в конце месяца"
        r'(?:на\s+)?(?:майские|новогодние|новый год|8 марта|23 февраля|каникул)',  # праздники
        r'(?:завтра|послезавтра|через\s+\w+\s+дн|через\s+неделю|через\s+месяц)',  # относительные
        r'(?:в\s+)?(?:этом|следующем)\s+месяце',
        r'(?:в\s+)?ближайшее\s+время',
        r'(?:первой|второй)\s+половин[еы]',                           # "в первой половине"
        r'ближе\s+к\s+(?:начал|конц|середин)',                        # "ближе к концу"
        r'(?:под|к)\s+конец',                                          # "под конец мая"
        r'(?:весь|целый)\s+\w*' + _MONTH_NAMES_RX.replace('|', r'\w*|').replace(r'(?:', '(?:'),  # "весь октябрь"
    ]
    has_specific_date = any(re.search(p, user_text) for p in specific_date_patterns)
    
    # Паттерн для голого упоминания месяца ("в марте", "март", "апреле")
    bare_month_rx = r'(?:январ[еья]|феврал[еья]|март[еа]?|апрел[еья]|ма[еяй]|июн[еья]|июл[еья]|август[еа]?|сентябр[еья]|октябр[еья]|ноябр[еья]|декабр[еья])'
    has_bare_month = re.search(bare_month_rx, user_text) is not None
    
    has_date_mention = has_specific_date or has_bare_month
    
    # Fix P5: Если только голый месяц без конкретных дат — нужен промежуток
    if has_bare_month and not has_specific_date:
        # Проверяем: может клиент в другом сообщении ответил "в начале"/"в середине"/"в конце"
        # (например, первое сообщение "в марте", второе "в начале")
        month_qualifier_loose = [
            r'\b(?:начал[еоу]|начало)\b',          # "в начале", "начале", "начало"
            r'\b(?:середин[еуы]|середина)\b',       # "в середине"
            r'\b(?:конц[еуы]|конец)\b',             # "в конце", "конце"
            r'(?:перв\w+|втор\w+)\s+половин',       # "первой половине", "второй половине"
        ]
        has_qualifier_loose = any(re.search(p, user_text) for p in month_qualifier_loose)
        if not has_qualifier_loose:
            missing.append("промежуток в месяце (начало/середина/конец)")
    
    # ─── Слот 3: Длительность (ночи/дни) ───
    nights_patterns = [
        r'\d+\s*(?:ноч|дн|день|дней|ночей)',
        r'(?:на\s+)?(?:неделю|недельку|две недели|2 недели)',
        r'\bнедел[яюи]\b',  # "неделя", "неделю", "недели" без "на"
        r'(?:на\s+)?(?:выходные|уикенд)',
        r'(?:с\s+)?\d{1,2}(?:\.\d{1,2})?(?:\s+)?(?:по|-)(?:\s+)?\d{1,2}',  # с 10 по 17, 10-17
    ]
    has_nights_mention = any(re.search(p, user_text) for p in nights_patterns)
    
    # Если нет ни дат, ни длительности — слот 3 пропущен
    if not has_date_mention and not has_nights_mention:
        missing.append("даты/месяц и длительность")
    elif not has_date_mention:
        missing.append("даты/месяц вылета")
    # Примечание: если есть дата, но нет длительности — это может быть OK
    # (например, "с 10 по 17 марта" уже содержит длительность)
    
    # ─── Слот 4: Состав путешественников ───
    travelers_patterns = [
        r'(?:взрослы[хй]|взр\.?|вз\.?|adults)',  # "взрослых", "взр", "вз", "adults"
        r'(?:дет(?:ей|и|ьми|ям)?|ребен(?:ок|ка)|child)',
        r'(?:я\s+)?(?:один|одна|сам|одиночк)',
        r'(?:двое|два|две)\s+(?:взрослы[хй]|человек|чел\.?)',  # "двое взрослых", "два человека"
        r'(?:трое|три|четыре|пять|шесть)\s+(?:взрослы[хй]|человек|чел\.?)',
        r'\d+\s*(?:взрослы[хй]|человек|чел\.?|взр|вз)',  # "2 взрослых", "3 человека", "1 вз"
        r'\d+\s*(?:в|вз)\s*\+',  # "2в+", "1 вз+" — shorthand
        r'(?:с\s+)?(?:мужем|женой|парнем|девушкой|подругой|другом)',
        r'(?:вдво[её]м|втро[её]м|вчетвером|впятером)',
        # НЕ включаем "семьёй/компанией/группой" — они слишком расплывчаты,
        # не дают точного состава (кол-во взрослых/детей), AI должен уточнить
        r'(?:мы\s+с\s+)',
    ]
    has_travelers_mention = any(re.search(p, user_text) for p in travelers_patterns)
    
    if not has_travelers_mention:
        missing.append("состав путешественников")
    
    # ─── P9: Проверка childage при child > 0 ───
    try:
        child_count = int(args.get("child", 0))
    except (ValueError, TypeError):
        child_count = 0
    if child_count > 0:
        has_childage = any(args.get(f"childage{i}") for i in [1, 2, 3])
        if not has_childage:
            # Проверяем, не указан ли возраст в тексте пользователя (например "ребёнок 7 лет")
            childage_text_patterns = [
                r'(?:ребен\w*|дет\w*|дочк\w*|сын\w*|малыш\w*)\s*(?:\d{1,2}\s*(?:лет|года?|мес))',
                r'\d{1,2}\s*(?:лет|года?)\s*(?:ребен|дет|дочк|сын)',
                r'(?:реб|ребёнок|ребенок)\s*\(\s*\d{1,2}',
                r'реб?\s*\d{1,2}\s*лет',
                r'\d+\s*(?:взр|в)\s*\+\s*(?:реб|р)?\s*\d{1,2}\s*(?:лет|г)',
            ]
            has_age_in_text = any(re.search(p, user_text) for p in childage_text_patterns)
            if not has_age_in_text:
                missing.append("возраст ребёнка")
    
    # ─── Слот 5: Quality Check (звёздность + питание) ───
    # Проверяем: клиент ЯВНО указал stars/meal ИЛИ явно "скипнул" (любой/не важно/и т.д.)
    # Также skip если клиент назвал конкретный отель/бренд (stars берётся из базы)
    
    stars_patterns = [
        r'\d[\s\-]*(?:зв[её]зд|\*|⭐)',                        # "5 звёзд", "4*", "5⭐", "5-звёздочный"
        r'(?:пяти|четыр[её]х|тр[её]х)зв[её]зд',               # "пятизвёздочный", "четырёхзвёздочный"
        r'\b(?:пять|четыре|три|два)\s+зв[её]зд',             # "пять звезд", "четыре звезды"
        r'\b(?:пят[её]рк|четв[её]рк|тройк)',                    # разг. "пятёрка"/"пятерка", "четвёрка"/"четверка"
    ]
    meal_patterns = [
        r'вс[её]\s*включен',                                  # "все включено" (е) И "всё включено" (ё)
        r'ультра\s*вс[её]\s*включ',                           # "ультра все включено"
        r'all\s*incl',                                         # "all inclusive"
        r'ол+\s*инклюзив',                                    # "олл инклюзив", "ол инклюзив"
        r'\b(?:аи|уаи)\b',                                    # "АИ", "УАИ" (word boundary — не матчит "Каир")
        r'\b(?:ai|uai)\b',                                    # Latin AI, UAI
        r'(?:полупансион|half\s*board|\bhb\b)',                # полупансион
        r'(?:полный\s*пансион|full\s*board|\bfb\b)',           # полный пансион
        r'(?:только\s*)?завтрак\w*',                            # "завтрак", "завтраки", "завтраками", "только завтрак"
        r'\b(?:bb|ro|ob)\b',                                   # bed&breakfast, room only, only bed
        r'(?:без\s*питани)',                                   # "без питания"
    ]
    skip_quality_patterns = [
        # Контекстные паттерны: "любой" только в связке со звёздностью/отелем/питанием
        r'(?:любой|любую|любое|любые)\s+(?:отель|категори|звёзд|звезд|питани)',
        r'(?:любой|любая|любое)\b',  # одиночный ответ "любой" на вопрос QC (последнее сообщение)
        r'(?:без\s*разницы|всё\s*равно|все\s*равно)',
        r'(?:не\s*важно|неважно|не\s*принципиально)',
        r'(?:на\s+(?:ваше?|твоё?|твое?)\s+усмотрени)',
        r'(?:рассмотрим\s+вариант|покажите?\s+что\s+есть|какие\s+есть)',
        r'(?:покажите?\s+что-нибудь|что\s+посоветуете)',
    ]
    # Бренды/конкретные отели — тоже skip quality check
    hotel_brand_patterns = [
        r'\b(?:rixos|hilton|delphin|swissotel|kempinski|calista|titanic|gloria|regnum|maxx\s*royal)\b',
        r'\b(?:iberostar|marriott|sheraton|radisson|accor|hyatt|intercontinental)\b',
        # "отель [Название с заглавной]" — но НЕ "отель красивый"
        # Этот паттерн ловит только конкретные упоминания с "хочу в отель ..."
        r'(?:в\s+)?отел[ьеи]\s+[а-яА-Яa-zA-Z]{3,}',
    ]
    
    # stars/meal/brand ищем по ВСЕМ сообщениям (user_text)
    has_stars = any(re.search(p, user_text) for p in stars_patterns)
    has_meal = any(re.search(p, user_text) for p in meal_patterns)
    has_brand = any(re.search(p, user_text) for p in hotel_brand_patterns)
    
    # Если бренд/отель обнаружен в тексте, проверяем: не вернул ли get_dictionaries пустой результат?
    # Если отель НЕ найден в каталоге TourVisor — QC НЕ должен быть автоматически пройден,
    # т.к. мы больше не ищем конкретный отель и нужно уточнить звёздность/питание.
    if has_brand:
        for i in range(len(full_history) - 1, -1, -1):
            msg = full_history[i]
            content = msg.get("content", "")
            # Ищем сообщение ассистента с вызовом get_dictionaries для отелей
            if msg.get("role") == "assistant" and "get_dictionaries" in content and ("hotel" in content or "name" in content):
                # Проверяем следующее сообщение (результат функции)
                if i + 1 < len(full_history):
                    result_msg = full_history[i + 1]
                    result_content = result_msg.get("content", "")
                    if "[get_dictionaries]: []" in result_content or '"hotels": []' in result_content:
                        has_brand = False
                        break
                    elif "[get_dictionaries]:" in result_content and "[]" not in result_content:
                        break
    
    # skip_quality ищем ТОЛЬКО по последнему сообщению пользователя
    # (чтобы "любой курорт" из раннего сообщения не пометил QC как пройденный)
    last_user_msg = user_messages[-1].lower() if user_messages else ""
    has_skip = any(re.search(p, last_user_msg) for p in skip_quality_patterns)
    
    # Quality Check пройден если:
    # - клиент указал И звёздность И тип питания
    # - ИЛИ клиент явно скипнул ("любой", "не важно")
    # - ИЛИ клиент назвал конкретный бренд/отель И указал тип питания
    #   (Fix P3: бренд пропускает звёздность, но НЕ тип питания)
    quality_check_passed = (has_stars and has_meal) or has_skip or (has_brand and has_meal)
    
    if not quality_check_passed:
        # Проверяем: может быть модель уже задала вопрос о QC, 
        # а клиент ответил чем-то неожиданным — не блокируем повторно
        # Ищем в истории ассистента вопрос про звёздность/питание
        # ВАЖНО: фильтруем function-result summaries — они содержат _hint текст
        # с "звёздами", "питанием" и т.д., вызывая ложное срабатывание
        assistant_messages = [
            msg.get("content", "") for msg in full_history[-10:] 
            if msg.get("role") == "assistant" and msg.get("content")
            and not msg.get("content", "").startswith("Результаты запросов:")
        ]
        assistant_text = " ".join(assistant_messages).lower()
        # Используем СПЕЦИФИЧНЫЕ фразы, уникальные для вопросов ассистента о QC,
        # а не короткие подстроки вроде "звёзд" которые матчат "звёздами" из _hint
        qc_asked = any(phrase in assistant_text for phrase in [
            "категорию отеля",              # "Уточните категорию отеля"
            "тип питания",                  # "Какой тип питания?"
            "питание предпочитаете",        # "Какое питание предпочитаете?"
            "какой отель предпочитаете",    # "Какой отель предпочитаете?"
            "какую звёздность",             # "Какую звёздность?"
            "какую звездность",             # е-вариант
            "сколько звёзд",               # "Сколько звёзд?"
            "сколько звезд",               # е-вариант
            "звёздность отел",             # "Звёздность отеля?"
            "звездность отел",             # е-вариант
        ])
        # Если ассистент спрашивал QC — проверяем, что клиент ОТВЕТИЛ после вопроса.
        # Модель может задать QC-вопрос в тексте одновременно с tool call и сразу
        # запустить search_tours, не дождавшись ответа. В таком случае qc_asked=True,
        # но пользователь ещё не ответил — блокируем.
        if qc_asked:
            _qc_phrases = [
                "категорию отеля", "тип питания", "питание предпочитаете",
                "какой отель предпочитаете", "какую звёздность", "какую звездность",
                "сколько звёзд", "сколько звезд", "звёздность отел", "звездность отел",
            ]
            _last_qc_idx = -1
            for _qi in range(len(full_history) - 1, -1, -1):
                _qmsg = full_history[_qi]
                if _qmsg.get("role") == "assistant" and _qmsg.get("content"):
                    _qcontent = _qmsg.get("content", "").lower()
                    if any(_qp in _qcontent for _qp in _qc_phrases):
                        _last_qc_idx = _qi
                        break
            _user_after_qc = any(
                full_history[_uj].get("role") == "user"
                and full_history[_uj].get("content")
                and not full_history[_uj].get("content", "").startswith("Результаты")
                and not full_history[_uj].get("content", "").startswith("СИСТЕМНАЯ")
                for _uj in range(_last_qc_idx + 1, len(full_history))
            ) if _last_qc_idx >= 0 else False
            if not _user_after_qc:
                qc_asked = False
        if not qc_asked:
            # Точная подсказка: говорим модели, что именно осталось уточнить
            # ВАЖНО: НЕ включаем примеры вроде "4-5 звёзд" — модель интерпретирует
            # их как рекомендацию и предлагает upsell клиенту
            # Fix P3: бренд пропускает звёздность, но питание нужно уточнить
            if has_brand and not has_meal and not has_skip:
                missing.append("тип питания")
            elif has_stars and not has_meal:
                missing.append("тип питания")
            elif has_meal and not has_stars:
                missing.append("категорию отеля (звёздность)")
            else:
                missing.append("категорию отеля и тип питания")
    
    return len(missing) == 0, missing


def _safe_int(val, default: int = 0) -> int:
    """
    Безопасное преобразование значения API в int.
    TourVisor API возвращает числа как строки, float или int в разных контекстах.
    Обрабатывает: "45000", 45000, "45000.50", 45000.5, None, "", "N/A"
    """
    if val is None or val == "":
        return default
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return default


# ─── Маппинг кодов городов → названия (для tour_cards) ───
_DEPARTURE_CITIES = {
    1: "Москва", 2: "Пермь", 3: "Екатеринбург", 4: "Уфа",
    5: "Санкт-Петербург", 6: "Челябинск", 7: "Самара",
    8: "Нижний Новгород",  # Fix P4: добавлен Нижний Новгород (TourVisor ID=8)
    9: "Новосибирск", 10: "Казань", 11: "Краснодар",
    12: "Красноярск", 18: "Ростов-на-Дону", 56: "Сочи",
    99: "Без перелёта",  # Fix P3: departure=99 = туры без перелёта
}

# ─── Валидация departure: паттерн в контексте "из [город]" → правильный departure ID ───
# Используется для коррекции, если модель передала неверный ID города вылета
# Паттерны привязаны к контексту вылета ("из ...", "вылетаем из ...", "летим из ...")
_DEPARTURE_VALIDATION = [
    (r'(?:из|с)\s+москв\w*', 1),
    (r'москв\w*\s*[-—,]?\s*(?:вылет|аэропорт)', 1),
    (r'(?:из|с)\s+(?:санкт[\s-]*)?петербург\w*', 5),
    (r'(?:из|с)\s+(?:спб|питер\w*)', 5),
    (r'(?:из|с)\s+(?:екатеринбург\w*|еката|екб)', 3),
    (r'(?:из|с)\s+перм[иь]\w*', 2),
    (r'(?:из|с)\s+уф[аыуе]\w*', 4),
    (r'(?:из|с)\s+челябинск\w*', 6),
    (r'(?:из|с)\s+самар\w*', 7),
    (r'(?:из|с)\s+(?:нижн\w*\s*новгород\w*|н[\.\s]*новгород\w*|ннов\w*)', 8),  # Fix P4
    (r'(?:из|с)\s+новосибирск\w*', 9),
    (r'(?:из|с)\s+казан\w*', 10),
    (r'(?:из|с)\s+краснодар\w*', 11),
    (r'(?:из|с)\s+красноярск\w*', 12),
    (r'(?:из|с)\s+ростов\w*', 18),
    (r'(?:из|с)\s+сочи', 56),
    (r'без\s*перел[её]т\w*', 99),
]

# Паттерны для верификации смены города вылета (без обязательного "из/с").
# Используются ТОЛЬКО когда модель явно сменила departure по сравнению с кэшем.
_DEPARTURE_VERIFY = {
    1: r'москв', 2: r'перм[иь]', 3: r'екатеринбург|еката|екб',
    4: r'уф[аыуе]', 5: r'петербург|питер|спб',
    6: r'челябинск', 7: r'самар', 8: r'нижн.*новгород|ннов',
    9: r'новосибирск', 10: r'казан', 11: r'краснодар',
    12: r'красноярск', 18: r'ростов', 56: r'сочи', 99: r'без.*перел',
}


def _safe_float(val, default=None):
    """Безопасное преобразование в float (для hotelrating и т.п.)."""
    if val is None or val == "":
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _parse_tv_date(date_str: str):
    """Конвертирует TourVisor 'DD.MM.YYYY' → ISO 'YYYY-MM-DD' для фронтенда."""
    if not date_str:
        return None
    parts = date_str.split(".")
    if len(parts) == 3:
        return f"{parts[2]}-{parts[1]}-{parts[0]}"
    return None


def _calc_end_date(date_str: str, nights):
    """Рассчитать дату окончания: TourVisor 'DD.MM.YYYY' + nights → ISO 'YYYY-MM-DD'."""
    if not date_str or not nights:
        return None
    try:
        d = _dt.strptime(date_str, "%d.%m.%Y")
        d_end = d + _td(days=int(nights))
        return d_end.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def _nights_penalty(nights: int, nf: int = None, nt: int = None) -> float:
    """
    Штраф за отклонение от запрошенного диапазона ночей.
    Внутри диапазона: слегка предпочитаем верхнюю границу (nightsto),
    т.к. это число, которое назвал клиент ("10 дней" → nightsto=10).
    За пределами диапазона: штраф пропорционален расстоянию.
    """
    if nf is None and nt is None:
        return 0.0
    lo = nf if nf is not None else nt
    hi = nt if nt is not None else nf
    if lo <= nights <= hi:
        return (hi - nights) * 0.5
    return min(abs(nights - lo), abs(nights - hi)) * 2.0


def _pick_best_tour(tours: list, ideal_datefrom: str = None,
                    nightsfrom: int = None, nightsto: int = None) -> dict:
    """
    Выбрать тур из списка, максимально совпадающий с запросом клиента.
    Приоритет сортировки: ночи (tier) > дата > цена.
    Tier 0 = точно nightsto (то что клиент назвал), tier 1+ = ниже в диапазоне,
    tier 100+ = вне диапазона.
    """
    if not tours:
        return {}
    if not ideal_datefrom and nightsfrom is None and nightsto is None:
        return tours[0]

    ideal_dt = None
    if ideal_datefrom:
        try:
            ideal_dt = _dt.strptime(ideal_datefrom, "%d.%m.%Y")
        except (ValueError, TypeError):
            pass

    scored = []
    for t in tours:
        date_diff = 0
        if ideal_dt:
            try:
                fly_dt = _dt.strptime(t.get("flydate", ""), "%d.%m.%Y")
                date_diff = abs((fly_dt - ideal_dt).days)
            except (ValueError, TypeError):
                date_diff = 99

        nights = _safe_int(t.get("nights"), 0)
        if nightsfrom and nightsto and nightsfrom <= nights <= nightsto:
            nights_tier = nightsto - nights
        elif nightsfrom and nightsto:
            nights_tier = 100 + min(abs(nights - nightsfrom), abs(nights - nightsto))
        else:
            nights_tier = 0

        price = _safe_int(t.get("price"), 999999999)
        scored.append((nights_tier, date_diff, price, t))

    scored.sort(key=lambda x: (x[0], x[1], x[2]))
    return scored[0][3]


def _map_hotel_to_card(hotel: dict, departure_city: str = "Москва") -> dict:
    """
    Маппинг отеля из get_search_results → формат tour_card для фронтенда.
    Структура совпадает с ожиданиями createTourCardHTML в script.js.
    """
    tour = hotel.get("tour") or {}
    flydate_raw = tour.get("flydate", "")
    nights = _safe_int(tour.get("nights"), 7)
    tour_price = _safe_int(tour.get("price") or hotel.get("price"))

    # meal — в simplified data уже содержит mealrussian (русское описание)
    meal_desc = tour.get("meal") or ""

    # Fix P3: Если departure=99 ("Без перелёта"), TourVisor может не вернуть поле noflight
    # в результатах поиска. Определяем статус перелёта по departure_city:
    # "Без перелёта" = departure=99 → flight_included=False, is_hotel_only=True
    is_no_flight = (departure_city == "Без перелёта") or bool(tour.get("noflight"))

    return {
        "hotel_name": hotel.get("hotelname") or "Отель",
        "hotel_stars": _safe_int(hotel.get("hotelstars")),
        "hotel_rating": _safe_float(hotel.get("hotelrating")),
        "country": hotel.get("countryname") or "",
        "resort": hotel.get("regionname") or "",
        "region": hotel.get("regionname") or "",
        "date_from": _parse_tv_date(flydate_raw),
        "date_to": _calc_end_date(flydate_raw, nights),
        "nights": nights,
        "price": tour_price,
        "price_per_person": None,
        "food_type": "",                      # Код питания (для JS fallback)
        "meal_description": meal_desc,        # Русское описание питания
        "room_type": tour.get("room") or "Standard",
        "image_url": hotel.get("picturelink"),
        "hotel_link": hotel.get("fulldesclink") or "#",
        "id": str(tour.get("tourid") or ""),
        "departure_city": departure_city,
        "is_hotel_only": is_no_flight,
        "flight_included": not is_no_flight,
        "operator": tour.get("operatorname") or "",
    }


_MEAL_CODE_TO_RU = {
    "RO": "Без питания",
    "BB": "Только завтрак",
    "HB": "Завтрак и ужин",
    "HB+": "Полупансион+",
    "FB": "Полный пансион",
    "FB+": "Полный пансион+",
    "AI": "Всё включено",
    "UAI": "Ультра всё включено",
}


def _map_hot_tour_to_card(tour_data: dict) -> dict:
    """
    Маппинг горящего тура из get_hot_tours → формат tour_card для фронтенда.
    ⚠️ Цена горящих туров — ЗА ЧЕЛОВЕКА!
    """
    flydate_raw = tour_data.get("flydate", "")
    nights = _safe_int(tour_data.get("nights"), 7)
    price_pp = _safe_int(tour_data.get("price_per_person"))
    meal_code = tour_data.get("meal") or ""
    meal_ru = _MEAL_CODE_TO_RU.get(meal_code.strip(), meal_code)

    return {
        "hotel_name": tour_data.get("hotelname") or "Отель",
        "hotel_stars": _safe_int(tour_data.get("hotelstars")),
        "hotel_rating": _safe_float(tour_data.get("hotelrating")),
        "country": tour_data.get("countryname") or "",
        "resort": tour_data.get("regionname") or "",
        "region": tour_data.get("regionname") or "",
        "date_from": _parse_tv_date(flydate_raw),
        "date_to": _calc_end_date(flydate_raw, nights),
        "nights": nights,
        "price": price_pp,                   # За человека (как в API)
        "price_per_person": price_pp,         # Дубль для явного отображения
        "food_type": meal_code,               # Код питания для JS fallback
        "meal_description": meal_ru,          # Русское описание для фронтенда
        "room_type": "Standard",
        "image_url": tour_data.get("picturelink"),
        "hotel_link": tour_data.get("fulldesclink") or "#",
        "id": str(tour_data.get("tourid") or ""),
        "departure_city": tour_data.get("departurename") or "Москва",
        "is_hotel_only": False,
        "flight_included": True,
        "operator": tour_data.get("operatorname") or "",
    }


def _dedup_response(text: str) -> str:
    """
    Удаляет дублированный контент из ответа модели.
    Yandex GPT иногда генерирует повторы: текст обрывается на corrupted char (\\ufffd),
    затем перезапускается с начала. Эта функция обнаруживает и обрезает дубликат.
    """
    if not text or len(text) < 100:
        return text
    
    # Ищем первую строку
    first_newline = text.find('\n')
    if first_newline < 5:
        return text
    
    first_line = text[:first_newline].strip()
    if not first_line or len(first_line) < 10:
        return text
    
    # Ищем повторное вхождение первой строки
    second = text.find(first_line, first_newline + 1)
    if second > 0:
        # Обрезаем до повторного вхождения (убираем corrupted chars перед ним)
        clean = text[:second].rstrip('\ufffd\n \t')
        logger.debug("🧹 DEDUP: removed duplicate starting at char %d (saved %d → %d chars)",
                     second, len(text), len(clean))
        return clean
    
    return text


# ── Reasoning-leak sanitizer ──────────────────────────────────────────────
_RE_REASONING_JSON = re.compile(r'\{\s*"role"\s*:\s*"assistant"')
_RE_REASONING_MARKERS = re.compile(
    r'(?:'
    r'We need to|We have to|We must|We should'
    r'|Now I[\'m\s]|I should|I need to|I must'
    r'|Let me |The conversation|The user|The assistant|The last'
    r'|ChatGPT|GPT-\d|as an AI'
    r'|Мы have|Кажется the|Похоже the'
    r')',
    re.IGNORECASE
)

_MIN_VALID_PREFIX = 30
_MIN_CLEANED_LEN = 20


def _strip_reasoning_leak(text: str) -> str:
    """
    Strip leaked LLM reasoning / meta-commentary from the end of a response.

    Two passes:
      1. Mid-text JSON fragments  {"role":"assistant"...}
      2. English-language reasoning markers (We need to, ChatGPT, etc.)

    Safety constraints:
      - Marker must appear AFTER at least _MIN_VALID_PREFIX chars of valid text
      - Cleaned text must be at least _MIN_CLEANED_LEN chars
      - If no marker found → text returned unchanged
    """
    if not text or len(text) < _MIN_VALID_PREFIX + 10:
        return text

    original = text

    # Pass 1: mid-text JSON {"role":"assistant"...}
    m = _RE_REASONING_JSON.search(text)
    if m and m.start() > _MIN_VALID_PREFIX:
        candidate = text[:m.start()].rstrip()
        if len(candidate) >= _MIN_CLEANED_LEN:
            logger.warning(
                "🧹 REASONING-LEAK(json) stripped %d chars from pos %d",
                len(text) - len(candidate), m.start(),
            )
            text = candidate

    # Pass 2: English reasoning markers
    m = _RE_REASONING_MARKERS.search(text)
    if m and m.start() > _MIN_VALID_PREFIX:
        candidate = text[:m.start()].rstrip()
        if len(candidate) >= _MIN_CLEANED_LEN:
            logger.warning(
                "🧹 REASONING-LEAK(marker) stripped %d chars from pos %d: '%s'",
                len(text) - len(candidate), m.start(),
                text[m.start():m.start() + 60],
            )
            text = candidate

    return text


# ── Sentence-level deduplication ──────────────────────────────────────────
def _dedup_sentences(text: str) -> str:
    """
    Remove duplicated question sentences within a single response.
    Catches intra-paragraph repeats that _dedup_response (newline-based) misses.
    """
    if not text or len(text) < 60:
        return text

    questions = re.findall(r'[^.!?\n]*\?', text)
    for q in questions:
        q_stripped = q.strip()
        if len(q_stripped) < 20:
            continue
        first_pos = text.find(q_stripped)
        second_pos = text.find(q_stripped, first_pos + len(q_stripped))
        if second_pos > 0:
            cleaned = text[:first_pos + len(q_stripped)].rstrip()
            if len(cleaned) >= _MIN_CLEANED_LEN:
                logger.warning(
                    "🧹 DEDUP-SENTENCE: removed duplicate question at pos %d: '%s'",
                    second_pos, q_stripped[:60],
                )
                return cleaned
    return text


# ── Trailing dialogue-continuation fragment stripper ──────────────────────
_RE_ORPHAN_START = re.compile(
    r'\s*(?:Отлично|Хорошо|Давайте|Ладно|Замечательно|Прекрасно|'
    r'Жду|Конечно|Понятно|Спасибо|Итого|Итак)\b',
    re.IGNORECASE,
)


def _strip_trailing_fragment(text: str) -> str:
    """
    Remove orphaned dialogue-continuation fragments after the last '?'.
    LLM sometimes starts generating the next conversational turn
    (e.g. "Отлично," or "Хорошо,") after its final question.
    """
    if not text or len(text) < 50:
        return text

    last_q = text.rfind('?')
    if last_q < _MIN_VALID_PREFIX:
        return text

    trailing = text[last_q + 1:]
    if not trailing.strip():
        return text

    trailing_stripped = trailing.strip()
    if len(trailing_stripped) >= 60 or trailing_stripped[-1] in '.?!':
        return text

    if _RE_ORPHAN_START.match(trailing):
        cleaned = text[:last_q + 1].rstrip()
        if len(cleaned) >= _MIN_CLEANED_LEN:
            logger.warning(
                "🧹 TRAILING-FRAGMENT stripped: '%s'",
                trailing_stripped[:60],
            )
            return cleaned
    return text


class YandexGPTHandler:
    """Обработчик запросов к Yandex GPT с Function Calling (Responses API)"""
    
    def __init__(self):
        self.folder_id = os.getenv("YANDEX_FOLDER_ID")
        self.api_key = os.getenv("YANDEX_API_KEY")
        self.model = os.getenv("YANDEX_MODEL", "yandexgpt")
        
        # Используем Completion API (стабильный, работает с folder_id)
        self.completion_url = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
        self.headers = {
            "Authorization": f"Api-Key {self.api_key}",
            "Content-Type": "application/json"
        }
        
        self.model_uri = f"gpt://{self.folder_id}/{self.model}"
        
        self.tourvisor = TourVisorClient()
        self.tools = self._load_tools()
        
        # История сообщений для контекста (новый формат)
        # input_list содержит ТОЛЬКО новые элементы для следующего API-вызова
        self.input_list: List[Dict] = []
        
        # Полная история диалога — используется как fallback при ошибках
        self.full_history: List[Dict] = []
        
        # Максимальный размер full_history (в сообщениях).
        # При превышении — обрезаем старые сообщения, оставляя последние.
        # 24 сообщения ≈ 12 обменов user/assistant — достаточно для полного цикла.
        self._max_history_len = 24
        
        # Счётчик пустых итераций подряд (для детекции зависаний)
        self._empty_iterations = 0
        
        # ID последнего ответа для контекста
        self.previous_response_id: Optional[str] = None
        
        # Системный промпт (теперь это instructions)
        self.instructions = self._load_system_prompt()
        
        # Callback для записи в диалоговый лог (устанавливается из app.py)
        self._dialogue_log_callback = None
        
        # ── Для нового фронтенда: хранилище tour_cards ──
        # Заполняется в _dispatch_function при get_search_results / get_hot_tours
        # Считывается и очищается в /api/v1/chat после завершения chat()
        self._pending_tour_cards: List[Dict] = []
        self._last_departure_city: str = "Москва"
        
        # ── Идеальные параметры для пересортировки результатов ──
        self._ideal_datefrom: Optional[str] = None   # "DD.MM.YYYY"
        self._ideal_nightsfrom: Optional[int] = None
        self._ideal_nightsto: Optional[int] = None
        self._has_budget: bool = False                 # True если pricefrom/priceto заданы
        
        # ── P1/P13: Кэш requestid и tourid для валидации ──
        # Предотвращает placeholder hallucination (requestid_egypt, tourid_третьего_варианта)
        self._last_requestid: Optional[str] = None  # Последний реальный requestid из search_tours
        self._search_awaiting_results: bool = False   # True после search_tours, False после get_search_results
        self._tourid_map: Dict[int, Dict] = {}       # Позиция(1-based) → {tourid, hotelcode, hotelname}
        
        # ── Fix C2: Кэш параметров последнего поиска ──
        # При смене страны/направления ("а если Египет?") модель часто теряет
        # параметры из предыдущего поиска. Кэш заполняется после успешного search_tours
        # и используется как fallback для пропущенных параметров.
        self._last_search_params: Dict = {}
        self._user_stated_budget: Optional[int] = None
        
        # ── Метрики для мониторинга качества (Этап 3) ──
        self._metrics = {
            "promised_search_detections": 0,      # Детекции "обещанного поиска"
            "cascade_incomplete_detections": 0,   # Блокировки из-за неполного каскада
            "dateto_corrections": 0,              # Исправления dateto
            "total_searches": 0,                  # Всего вызовов search_tours
            "total_messages": 0,                  # Всего сообщений пользователя
        }
        
        logger.info("🤖 YandexGPTHandler INIT  model=%s  folder=%s  tools=%d",
                     self.model_uri, self.folder_id, len(self.tools))
    
    def get_metrics(self) -> Dict[str, int]:
        """Возвращает метрики сессии для мониторинга"""
        return self._metrics.copy()
    
    def _resolve_tourid_from_text(self, placeholder: str) -> Optional[str]:
        """
        P1/P13: Попытка resolve tourid из плейсхолдера типа 'tourid_третьего_варианта'.
        Используем _tourid_map (позиция → tourid) для поиска по ключевым словам.
        """
        if not self._tourid_map:
            return None
        
        placeholder_lower = placeholder.lower()
        
        # Маппинг порядковых слов → позиция
        ordinal_map = {
            "перв": 1, "1": 1,
            "втор": 2, "2": 2,
            "трет": 3, "третьего": 3, "3": 3,
            "четверт": 4, "четвёрт": 4, "4": 4,
            "пят": 5, "5": 5,
        }
        
        for keyword, pos in ordinal_map.items():
            if keyword in placeholder_lower:
                entry = self._tourid_map.get(pos)
                if entry:
                    logger.info("✅ TOURID-RESOLVE: '%s' → позиция %d → tourid=%s", placeholder, pos, entry["tourid"])
                    return entry["tourid"]
        
        # Если не нашли по порядковому — вернуть первый из кэша
        if 1 in self._tourid_map:
            first = self._tourid_map[1]["tourid"]
            logger.warning("⚠️ TOURID-RESOLVE: '%s' — не удалось определить позицию, возвращаем первый: %s", placeholder, first)
            return first
        
        return None
    
    def _append_history(self, role: str, content: str):
        """
        Fix P13: Добавляет сообщение в full_history с гарантией чередования ролей.
        Если последнее сообщение имеет ту же роль — вставляет placeholder.
        """
        if self.full_history and self.full_history[-1].get("role") == role:
            placeholder_role = "assistant" if role == "user" else "user"
            self.full_history.append({"role": placeholder_role, "content": "[продолжение обработки]"})
            logger.debug("🔄 ROLE-FIX: inserted %s placeholder before %s message", placeholder_role, role)
        self.full_history.append({"role": role, "content": content})
    
    def _trim_history(self):
        """
        Обрезает full_history если она превышает _max_history_len.
        Сохраняет первое сообщение (часто содержит контекст) + последние N.
        """
        if len(self.full_history) > self._max_history_len:
            old_len = len(self.full_history)
            # Оставляем первые 2 + последние (_max_history_len - 2)
            keep_start = 2
            keep_end = self._max_history_len - keep_start
            self.full_history = self.full_history[:keep_start] + self.full_history[-keep_end:]
            logger.info("✂️ TRIM full_history: %d → %d messages", old_len, len(self.full_history))
    
    def _dialogue_log(self, direction: str, content: str):
        """Запись в диалоговый лог через callback из app.py"""
        if self._dialogue_log_callback:
            try:
                self._dialogue_log_callback(direction, content)
            except Exception:
                pass
    
    def _load_tools(self) -> List[Dict]:
        """Загрузить описания функций из function_schemas.json"""
        schema_path = os.path.join(os.path.dirname(__file__), "..", "function_schemas.json")
        with open(schema_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        # Загружаем custom functions
        custom_tools = data.get("tools", [])
        
        # Добавляем встроенный web_search инструмент
        web_search_tool = {
            "type": "web_search",
            "search_context_size": "medium"  # low | medium | high
        }
        
        return custom_tools + [web_search_tool]
    
    def _load_system_prompt(self) -> str:
        """Загрузить системный промпт (теперь это instructions)"""
        prompt_path = os.path.join(os.path.dirname(__file__), "..", "system_prompt.md")
        try:
            with open(prompt_path, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return "Ты — AI-менеджер турагентства. Помогаешь клиентам найти и забронировать туры."
    
    async def _execute_function(self, name: str, arguments: str, call_id: str) -> Dict:
        """Выполнить функцию и вернуть результат в новом формате"""
        try:
            args = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError as e:
            logger.error(
                "⚠️ JSON PARSE ERROR for %s: %s (arg_len=%d)",
                name, e, len(arguments or "")
            )
            self._dialogue_log("ERROR", f"{name} -> malformed JSON: {str(e)[:200]}")
            return {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps({
                    "error": f"Ошибка: аргументы функции {name} содержат невалидный JSON. "
                             f"Попробуй вызвать функцию заново с корректными аргументами."
                }, ensure_ascii=False)
            }
        args_pretty = json.dumps(args, ensure_ascii=False)
        logger.info("🔧 FUNC CALL >> %s(%s)  call_id=%s", name, args_pretty[:300], call_id)
        t0 = time.perf_counter()
        
        # Пишем в диалоговый лог вызов функции
        self._dialogue_log("FUNC_CALL", f"{name}({args_pretty})")
        
        try:
            result = await self._dispatch_function(name, args)
            result_str = json.dumps(result, ensure_ascii=False, default=str)
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            logger.info("🔧 FUNC CALL << %s  OK  %dms  result_size=%d chars", name, elapsed_ms, len(result_str))
            logger.debug("🔧 FUNC RESULT [%s]: %s", name, result_str[:800] + ("…" if len(result_str) > 800 else ""))
            
            # Пишем в диалоговый лог результат функции (первые 2000 символов)
            self._dialogue_log("FUNC_RESULT", f"{name} -> {result_str[:2000]}{'…' if len(result_str) > 2000 else ''}")
            
            return {
                "type": "function_call_output",
                "call_id": call_id,
                "output": result_str
            }
        except (TourIdExpiredError, SearchNotFoundError, NoResultsError) as e:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            error_msg = f"Ошибка: {str(e)}"
            logger.warning("🔧 FUNC CALL << %s  BUSINESS_ERROR  %dms  %s", name, elapsed_ms, error_msg)
            self._dialogue_log("ERROR", f"{name} -> {error_msg}")
            return {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps({"error": error_msg}, ensure_ascii=False)
            }
        except Exception as e:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            error_msg = f"Неожиданная ошибка: {str(e)}"
            logger.error("🔧 FUNC CALL << %s  EXCEPTION  %dms  %s", name, elapsed_ms, error_msg, exc_info=True)
            self._dialogue_log("ERROR", f"{name} -> {error_msg}")
            return {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps({"error": error_msg}, ensure_ascii=False)
            }
    
    async def _dispatch_function(self, name: str, args: Dict) -> Any:
        """Маршрутизация вызовов функций к TourVisor клиенту"""
        
        if name == "get_current_date":
            from datetime import datetime
            now = datetime.now()
            return {
                "date": now.strftime("%d.%m.%Y"),
                "time": now.strftime("%H:%M"),
                "year": now.year,
                "month": now.month,
                "day": now.day,
                "weekday": ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"][now.weekday()],
                "hint": "Используй эту дату для datefrom/dateto. Формат: ДД.ММ.ГГГГ"
            }
        
        elif name == "search_tours":
            # ── Fix P4 + H1 + H2: Валидация departure code ──
            # Проверяем, что модель передала правильный ID города вылета
            # Fix H1: Исключаем результаты функций из текста для валидации
            # Fix H2: Обрабатываем массивы departure (модель может передать [5, 10])
            dep_raw = args.get("departure")
            
            if isinstance(dep_raw, list):
                # Fix P5+P6: Множественные города вылета — просим выбрать один
                # TourVisor API не поддерживает массив departure → 0 результатов
                city_names = [_DEPARTURE_CITIES.get(_safe_int(d), f"код {d}") for d in dep_raw]
                valid_names = [n for n in city_names if "код" not in n]
                logger.warning("⚠️ DEPARTURE-ARRAY REJECTED: %s → просим выбрать один город", dep_raw)
                return json.dumps({
                    "error": f"Клиент указал несколько городов вылета: {', '.join(valid_names or city_names)}. "
                             f"Уточни у клиента, из какого ОДНОГО города ему удобнее вылетать, "
                             f"и выполни поиск с одним городом."
                }, ensure_ascii=False)
            else:
                dep_code = _safe_int(dep_raw)
            
            if dep_code is not None and not isinstance(args.get("departure"), list):
                # ── Детекция смены города вылета ──
                # Если модель явно сменила departure по сравнению с кэшем И
                # новый город упоминается в недавних сообщениях — доверяем модели.
                _prev_dep = self._last_search_params.get("departure")
                _model_changed = (_prev_dep is not None
                                  and dep_code != _prev_dep
                                  and dep_code in _DEPARTURE_CITIES)
                _skip_validation = False

                if _model_changed:
                    _recent_user = " ".join([
                        msg.get("content", "").lower()
                        for msg in self.full_history[-6:]
                        if msg.get("role") == "user" and msg.get("content")
                        and not msg.get("content", "").startswith("Результаты")
                    ])
                    _verify = _DEPARTURE_VERIFY.get(dep_code)
                    if _verify and re.search(_verify, _recent_user):
                        logger.info(
                            "📋 DEPARTURE-CHANGE: %s(%d) → %s(%d), подтверждено текстом",
                            _DEPARTURE_CITIES.get(_prev_dep, "?"), _prev_dep,
                            _DEPARTURE_CITIES.get(dep_code, "?"), dep_code
                        )
                        _skip_validation = True

                if not _skip_validation:
                    user_text_for_dep = " ".join([
                        msg.get("content", "") for msg in self.full_history[-20:]
                        if msg.get("role") == "user" and msg.get("content")
                        and not msg.get("content", "").startswith("Результаты вызванных функций")
                        and not msg.get("content", "").startswith("Результаты запросов:")
                    ]).lower()
                    for dep_pattern, correct_dep_id in _DEPARTURE_VALIDATION:
                        if re.search(dep_pattern, user_text_for_dep):
                            if dep_code != correct_dep_id:
                                logger.warning(
                                    "⚠️ DEPARTURE-MISMATCH: departure=%s → %s (%s)",
                                    dep_code, correct_dep_id,
                                    _DEPARTURE_CITIES.get(correct_dep_id, "?")
                                )
                                args["departure"] = correct_dep_id
                                dep_code = correct_dep_id
                            break
            
            # Запоминаем город вылета для маппинга tour_cards
            if dep_code is not None:
                self._last_departure_city = _DEPARTURE_CITIES.get(
                    dep_code, self._last_departure_city
                )
            
            # ── Fix H4: Санитизация параметров — детекция галлюцинаций ──
            # Модель иногда вставляет вызовы функций ВНУТРЬ аргументов:
            # datefrom: "\"get_current_date(\"" (из Сценария 8)
            for _sanitize_key in ("datefrom", "dateto"):
                _sv = args.get(_sanitize_key, "")
                if isinstance(_sv, str) and re.search(r'get_\w+\(|search_\w+\(|"get_|function', _sv):
                    logger.warning(
                        "⚠️ HALLUCINATED-FUNC-IN-PARAM: %s='%s' — удаляем галлюцинацию",
                        _sanitize_key, _sv[:100]
                    )
                    args.pop(_sanitize_key, None)
            
            # ── Fix P2: Авто-дополнение года в датах DD.MM → DD.MM.YYYY ──
            for _dk in ("datefrom", "dateto"):
                _dv = args.get(_dk)
                if _dv and re.fullmatch(r'\d{1,2}\.\d{1,2}', str(_dv)):
                    _now = _dt.now()
                    _dv_with_year = f"{_dv}.{_now.year}"
                    try:
                        _parsed_d = _dt.strptime(_dv_with_year, "%d.%m.%Y")
                        if _parsed_d < _now:
                            _dv_with_year = f"{_dv}.{_now.year + 1}"
                        args[_dk] = _dv_with_year
                        logger.warning("🛡️ SAFETY-NET P2: %s авто-дополнен годом: '%s' → '%s'", _dk, _dv, args[_dk])
                    except ValueError:
                        logger.warning("⚠️ SAFETY-NET P2: не удалось дополнить год для %s='%s'", _dk, _dv)
            
            # ── Валидация и авто-коррекция dateto (Fix 1B) ──
            datefrom_str = args.get("datefrom")
            dateto_str = args.get("dateto")
            nightsfrom = args.get("nightsfrom")
            nightsto = args.get("nightsto")
            
            if datefrom_str:
                try:
                    datefrom_dt = _dt.strptime(datefrom_str, "%d.%m.%Y")
                    dateto_dt = _dt.strptime(dateto_str, "%d.%m.%Y") if dateto_str else None
                    
                    has_specific_nights = nightsfrom is not None or nightsto is not None
                    
                    # Случай 1: dateto не указан → авто-установка = datefrom (точная дата)
                    if dateto_dt is None:
                        dateto_dt = datefrom_dt
                        args["dateto"] = dateto_dt.strftime("%d.%m.%Y")
                        logger.warning("⚠️ dateto не указан, установлен = datefrom (%s)", args["dateto"])
                    
                    # Случай 2: dateto == datefrom — штатное поведение для точных дат, не трогаем
                    
                    # Случай 3: конкретная дата + длительность, но dateto слишком далеко
                    # Если nightsfrom/nightsto указаны и dateto - datefrom > nightsto,
                    # значит модель интерпретировала dateto как дату окончания тура,
                    # а не как последнюю дату вылета. Clamp до datefrom (точная дата).
                    # 
                    # ── P8: BYPASS если пользователь явно указал "с X по Y" ──
                    # Паттерн: "с 10 по 17 марта", "с 10.03 по 17.03" — НЕ clampить!
                    elif has_specific_nights and dateto_dt is not None:
                        # Проверяем, не указал ли пользователь явный диапазон дат
                        _user_date_text = " ".join([
                            msg.get("content", "") for msg in self.full_history[-20:]
                            if msg.get("role") == "user" and msg.get("content")
                        ])
                        _explicit_date_range = bool(re.search(
                            r'с\s+\d{1,2}[\s./-].*?(?:по|-)\s*\d{1,2}',
                            _user_date_text, re.IGNORECASE
                        ))
                        
                        if _explicit_date_range:
                            range_days = (dateto_dt - datefrom_dt).days
                            nightsfrom_val = nightsfrom or 7
                            if range_days > 2 and nightsfrom_val and abs(range_days - nightsfrom_val) <= 1:
                                corrected_dt = datefrom_dt
                                self._metrics["dateto_corrections"] = self._metrics.get("dateto_corrections", 0) + 1
                                logger.info(
                                    "✅ dateto clamp for explicit range: 'с %s по %s' (%d дней ≈ nights=%d). "
                                    "Сужаем dateto до %s (точная дата вылета, а не вся поездка)",
                                    datefrom_str, dateto_str, range_days, nightsfrom_val,
                                    corrected_dt.strftime("%d.%m.%Y")
                                )
                                args["dateto"] = corrected_dt.strftime("%d.%m.%Y")
                            else:
                                logger.info(
                                    "✅ dateto clamp BYPASSED: 'с X по Y' но range=%d != nights=%d — оставляем как есть. "
                                    "datefrom=%s, dateto=%s",
                                    range_days, nightsfrom_val, datefrom_str, dateto_str
                                )
                        else:
                            delta_days = (dateto_dt - datefrom_dt).days
                            effective_nights = nightsto or nightsfrom or 7
                            if delta_days >= 4 and abs(delta_days - effective_nights) <= 2:
                                corrected_dt = datefrom_dt
                                self._metrics["dateto_corrections"] += 1
                                logger.warning(
                                    "⚠️ dateto clamp: модель выставила dateto=%s (datefrom+%d дней ≈ nights=%d). "
                                    "Исправлено на datefrom = %s (точная дата вылета, не дата возвращения!)",
                                    dateto_str, delta_days, effective_nights,
                                    corrected_dt.strftime("%d.%m.%Y")
                                )
                                args["dateto"] = corrected_dt.strftime("%d.%m.%Y")
                    
                    # ── Fix P6: Проверка дат в прошлом ──
                    # Если datefrom уже в прошлом — сдвигаем на завтра
                    now_dt = _dt.now().replace(hour=0, minute=0, second=0, microsecond=0)
                    datefrom_dt = _dt.strptime(args["datefrom"], "%d.%m.%Y")  # Re-parse after possible clamp
                    dateto_dt = _dt.strptime(args["dateto"], "%d.%m.%Y")
                    
                    if datefrom_dt < now_dt:
                        new_datefrom = now_dt + _td(days=1)
                        logger.warning(
                            "⚠️ datefrom в прошлом (%s < %s), сдвинут на %s",
                            args["datefrom"], now_dt.strftime("%d.%m.%Y"),
                            new_datefrom.strftime("%d.%m.%Y")
                        )
                        args["datefrom"] = new_datefrom.strftime("%d.%m.%Y")
                        # Если dateto тоже в прошлом — сдвигаем и его
                        if dateto_dt < new_datefrom:
                            new_dateto = new_datefrom + _td(days=2)
                            args["dateto"] = new_dateto.strftime("%d.%m.%Y")
                            logger.warning("⚠️ dateto тоже сдвинут на %s", args["dateto"])
                    
                except (ValueError, TypeError) as e:
                    logger.warning("⚠️ Ошибка парсинга дат для валидации dateto: %s", e)
            
            # ── Fix P4: Safety-net для дат частей месяца ──
            # Модель часто путает "в конце мая" (ДИАПАЗОН 20.05-31.05)
            # с "конкретная дата 31.05" и ставит dateto = datefrom + 2.
            # Проверяем: если в user_text есть "начале/середине/конце + месяц",
            # а datefrom-dateto ≤ 5 дней — это ошибка, корректируем.
            if args.get("datefrom") and args.get("dateto"):
                user_msgs_for_dates = [
                    msg.get("content", "") for msg in self.full_history[-20:]
                    if msg.get("role") == "user" and msg.get("content")
                ]
                user_text_for_dates = " ".join(user_msgs_for_dates).lower()
                
                _MONTHS_MAP = {
                    'январ': (1, 31), 'феврал': (2, 28), 'март': (3, 31), 'апрел': (4, 30),
                    'ма': (5, 31), 'июн': (6, 30), 'июл': (7, 31), 'август': (8, 31),
                    'сентябр': (9, 30), 'октябр': (10, 31), 'ноябр': (11, 30), 'декабр': (12, 31),
                }
                
                # Паттерн: "в начале/середине/конце + месяц"
                month_part_match = re.search(
                    r'(?:в\s+)?(?P<part>начал\w*|середин\w*|конц\w*|перв\w+\s+половин\w*|втор\w+\s+половин\w*)'
                    r'\s+(?P<month>январ\w*|феврал\w*|март\w*|апрел\w*|ма[еяй]\w*|июн\w*|июл\w*|август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*)',
                    user_text_for_dates
                )
                
                if month_part_match:
                    part = month_part_match.group('part')
                    month_word = month_part_match.group('month')
                    
                    # Определяем месяц
                    detected_month = None
                    detected_last_day = 31
                    for prefix, (m_num, m_last) in _MONTHS_MAP.items():
                        if month_word.startswith(prefix):
                            detected_month = m_num
                            detected_last_day = m_last
                            break
                    
                    if detected_month:
                        try:
                            df = _dt.strptime(args["datefrom"], "%d.%m.%Y")
                            dt_val = _dt.strptime(args["dateto"], "%d.%m.%Y")
                            date_span = (dt_val - df).days
                            year = df.year if df.month == detected_month else (df.year if detected_month > df.month else df.year + 1)
                            
                            # Февраль високосного года
                            if detected_month == 2:
                                import calendar
                                detected_last_day = 29 if calendar.isleap(year) else 28
                            
                            corrected = False
                            
                            # Fix F3: Исправлены условия — safety-net срабатывает когда модель
                            # выставила НЕПРАВИЛЬНЫЙ диапазон (слишком узкий ИЛИ слишком широкий).
                            # «начало» = 01-04 (3 дня), «середина» = 10-20 (10 дней), «конец» = 20-end (10-11 дней)
                            if 'начал' in part and date_span != 3:
                                new_from = f"01.{detected_month:02d}.{year}"
                                new_to = f"04.{detected_month:02d}.{year}"
                                corrected = True
                            elif 'середин' in part and not (8 <= date_span <= 12):
                                new_from = f"10.{detected_month:02d}.{year}"
                                new_to = f"20.{detected_month:02d}.{year}"
                                corrected = True
                            elif 'конц' in part and not (8 <= date_span <= 14):
                                new_from = f"20.{detected_month:02d}.{year}"
                                new_to = f"{detected_last_day:02d}.{detected_month:02d}.{year}"
                                corrected = True
                            elif 'перв' in part and 'половин' in part and not (11 <= date_span <= 15):
                                new_from = f"01.{detected_month:02d}.{year}"
                                new_to = f"14.{detected_month:02d}.{year}"
                                corrected = True
                            elif 'втор' in part and 'половин' in part and not (11 <= date_span <= 15):
                                new_from = f"15.{detected_month:02d}.{year}"
                                new_to = f"28.{detected_month:02d}.{year}"
                                corrected = True
                            
                            if corrected:
                                logger.warning(
                                    "🛡️ SAFETY-NET P4: '%s %s' → даты скорректированы %s–%s → %s–%s (модель сузила диапазон)",
                                    part, month_word,
                                    args["datefrom"], args["dateto"],
                                    new_from, new_to
                                )
                                args["datefrom"] = new_from
                                args["dateto"] = new_to
                        except (ValueError, TypeError) as e:
                            logger.warning("⚠️ Ошибка коррекции дат частей месяца: %s", e)
            
            # ── Валидация: regions не должен совпадать с country code ──
            country_code = args.get("country")
            regions_val = args.get("regions", "")
            if regions_val and country_code:
                region_ids = [r.strip() for r in str(regions_val).split(",")]
                if len(region_ids) == 1 and region_ids[0] == str(country_code):
                    logger.warning(
                        "🛡️ SAFETY-NET: regions='%s' совпадает с country='%s' — убираем regions",
                        regions_val, country_code
                    )
                    args.pop("regions", None)
            
            # ── Fix P3: Проверка региона/курорта + авто-разрешение ──
            # Если клиент указал конкретный курорт, но модель НЕ передала regions —
            # пытаемся авто-разрешить (Tier 1: hardcoded ID, Tier 2: API lookup),
            # и только если не получилось — возвращаем ошибку
            if not args.get("regions") and not args.get("subregions") and not args.get("hotels"):
                user_messages_for_region = [
                    msg.get("content", "") for msg in self.full_history[-20:] 
                    if msg.get("role") == "user" 
                    and msg.get("content")
                    and not msg.get("content", "").startswith("Результаты вызванных функций")
                ]
                user_text_for_region = " ".join(user_messages_for_region).lower()
                
                # Универсальный список курортов по странам
                # Формат: (паттерн, страна_отображение, region_id | None, country_code, parent_region | None)
                #   region_id — если ID региона ИЗВЕСТЕН (популярные регионы)
                #   parent_region — если город является подрайоном известного региона (нужен API lookup)
                resort_patterns = [
                    # ═══ Россия (country=47) — hardcoded IDs из системного промпта ═══
                    # КМВ — города, входящие в регион "Кав. Мин. Воды" (Tier 1: hardcoded ID 424)
                    (r'\b(?:кисловодск\w*|пятигорск\w*|ессентуки\w*|железноводск\w*|минеральн\w*\s*вод\w*|кмв)\b', "России", "424", 47, None),
                    # Сочи (region=426) + Адлер входит в Сочи
                    (r'\b(?:сочи)\b', "России", "426", 47, None),
                    (r'\b(?:адлер\w*)\b', "России", "426", 47, None),
                    # Красная Поляна — отдельный регион (495)
                    (r'\b(?:красн\w*\s*полян\w*)\b', "России", "495", 47, None),
                    # Черноморское побережье
                    (r'\b(?:анап[аыуе]\w*)\b', "России", "427", 47, None),
                    (r'\b(?:геленджик\w*|новоросс\w*)\b', "России", "428", 47, None),
                    # Крым (region=423)
                    (r'\b(?:крым\w*)\b', "России", "423", 47, None),
                    (r'\b(?:ялт[аыуе]\w*|алушт[аыуе]\w*|севастопол\w*|феодоси\w*|судак\w*|евпатори\w*)\b', "России", "423", 47, None),
                    # Калининград (Tier 1: hardcoded ID 425)
                    (r'\b(?:калининград\w*)\b', "России", "425", 47, None),
                    (r'\b(?:светлогорск\w*|зеленоградск\w*)\b', "России", "425", 47, None),
                    # ═══ Турция (country=4) — hardcoded IDs ═══
                    (r'\b(?:алан[ьи]я|аланья)\b', "Турции", "19", 4, None),
                    (r'\b(?:анталь?я|анталия)\b', "Турции", "20", 4, None),
                    (r'\b(?:белек)\b', "Турции", "21", 4, None),
                    (r'\b(?:кемер)\b', "Турции", "22", 4, None),
                    (r'\b(?:сиде)\b', "Турции", "23", 4, None),
                    (r'\b(?:бодрум)\b', "Турции", "24", 4, None),
                    (r'\b(?:даламан)\b', "Турции", "25", 4, None),
                    (r'\b(?:мармарис)\b', "Турции", "26", 4, None),
                    (r'\b(?:фетхие|фетие)\b', "Турции", "27", 4, None),
                    (r'\b(?:кушадас\w*)\b', "Турции", "154", 4, None),
                    (r'\b(?:стамбул)\b', "Турции", "277", 4, None),
                    (r'\b(?:дидим)\b', "Турции", "155", 4, None),
                    # ═══ Египет (country=1) — hardcoded IDs из системного промпта ═══
                    (r'\b(?:шарм[\s-]*(?:эль[\s-]*)?шейх|шарм)\b', "Египта", "6", 1, None),
                    (r'\b(?:хургад[аыуе]\w*)\b', "Египта", "5", 1, None),
                    (r'\b(?:марса[\s-]*алам)\b', "Египта", "11", 1, None),
                    (r'\b(?:дахаб)\b', "Египта", None, 1, None),
                    # ═══ ОАЭ (country=9) — hardcoded IDs ═══
                    (r'\b(?:дубай|дубаи)\b', "ОАЭ", "45", 9, None),
                    (r'\b(?:абу[\s-]*даби)\b', "ОАЭ", "43", 9, None),
                    (r'\b(?:шардж[аеу]\w*)\b', "ОАЭ", "48", 9, None),
                    (r'\b(?:рас[\s-]*аль[\s-]*хайм\w*)\b', "ОАЭ", "46", 9, None),
                    # ═══ Таиланд (country=2) — hardcoded IDs ═══
                    (r'\b(?:пхукет|пукет)\b', "Таиланда", "8", 2, None),
                    (r'\b(?:паттай[яеу]\w*|паттая)\b', "Таиланда", "7", 2, None),
                    (r'\b(?:самуи)\b', "Таиланда", "9", 2, None),
                    (r'\b(?:краби)\b', "Таиланда", "60", 2, None),
                    (r'\b(?:хуа[\s-]*хин)\b', "Таиланда", None, 2, None),
                    # ═══ Вьетнам (country=16) ═══
                    (r'\b(?:фукуок|фу[\s-]*куок)\b', "Вьетнама", None, 16, None),
                    (r'\b(?:нячанг|ня[\s-]*чанг)\b', "Вьетнама", None, 16, None),
                    (r'\b(?:фантьет|фан[\s-]*тьет|муйне|муй[\s-]*не)\b', "Вьетнама", None, 16, None),
                    # ═══ Шри-Ланка (country=12) ═══
                    (r'\b(?:коломбо|бентот[аы]|хиккадув[аы]|унаватун[аы])\b', "Шри-Ланки", None, 12, None),
                    # ═══ Мальдивы (country=8) ═══
                    (r'\b(?:мале|маафуш\w*)\b', "Мальдив", None, 8, None),
                    # ═══ Куба (country=10) ═══
                    (r'\b(?:варадеро|гаван[аы])\b', "Кубы", None, 10, None),
                    # ═══ Доминикана (country=11) ═══
                    (r'\b(?:пунта[\s-]*кан[аы]|бока[\s-]*чик[аы])\b', "Доминиканы", None, 11, None),
                ]
                
                mentioned_resort = None
                for pattern, country_name, region_id, country_code, parent_region in resort_patterns:
                    if re.search(pattern, user_text_for_region):
                        resort_match = re.search(pattern, user_text_for_region).group()
                        mentioned_resort = (resort_match, country_name, region_id, country_code, parent_region)
                        break
                
                if mentioned_resort:
                    resort_name, country_name, region_id, country_code, parent_region = mentioned_resort
                    self._metrics.setdefault("resort_without_region_detections", 0)
                    self._metrics["resort_without_region_detections"] += 1
                    
                    # ── Fix P2: Корректируем country если модель передала не ту страну ──
                    # Курорт может принадлежать ТОЛЬКО одной стране — country_code из resort_patterns
                    # является единственным правильным значением.
                    # Пример: "Сочи" = Россия (47), даже если модель передала country=4 (Турция)
                    if country_code and int(args.get("country", 0)) != int(country_code):
                        logger.warning(
                            "⚠️ AUTO-RESOLVE: country мисмэтч %s→%s (курорт '%s' принадлежит %s), корректирую",
                            args.get("country"), country_code, resort_name, country_name
                        )
                        args["country"] = country_code
                    
                    resolved = False
                    
                    # Tier 1: Прямой ID региона известен (hardcoded для популярных)
                    if region_id:
                        args["regions"] = str(region_id)
                        logger.info(
                            "✅ AUTO-RESOLVE (Tier 1): курорт '%s' → regions=%s, country=%s (hardcoded)",
                            resort_name, region_id, args.get("country")
                        )
                        resolved = True
                    
                    # Tier 2: Знаем parent_region — ищем его ID через API
                    elif parent_region:
                        try:
                            api_country = country_code  # Fix P2: всегда используем country из resort_patterns
                            regions_list = await self.tourvisor.get_regions(int(api_country))
                            parent_lower = parent_region.lower().strip()
                            for r in regions_list:
                                rname = r.get("name", "").lower().strip()
                                # Fuzzy: exact match OR contains OR starts with same prefix
                                if rname == parent_lower or parent_lower in rname or rname in parent_lower or rname.startswith(parent_lower[:4]):
                                    args["regions"] = str(r.get("id"))
                                    logger.info(
                                        "✅ AUTO-RESOLVE (Tier 2): курорт '%s' → parent '%s' ≈ region '%s' → regions=%s (API fuzzy lookup)",
                                        resort_name, parent_region, r.get("name"), r.get("id")
                                    )
                                    resolved = True
                                    break
                            if not resolved:
                                logger.warning(
                                    "⚠️ AUTO-RESOLVE (Tier 2): parent '%s' не найден в API для country=%s",
                                    parent_region, api_country
                                )
                        except Exception as e:
                            logger.error("❌ AUTO-RESOLVE API error: %s", e)
                    
                    # Tier 3: ID неизвестен и нет parent — пробуем найти совпадение по имени через API
                    if not resolved and not region_id and not parent_region:
                        try:
                            api_country = country_code  # Fix P2: всегда используем country из resort_patterns
                            regions_list = await self.tourvisor.get_regions(int(api_country))
                            # Ищем регион, чьё имя содержит resort_name (или наоборот)
                            for r in regions_list:
                                rname = r.get("name", "").lower().strip()
                                if resort_name in rname or rname in resort_name or rname.startswith(resort_name[:4]):
                                    args["regions"] = str(r.get("id"))
                                    logger.info(
                                        "✅ AUTO-RESOLVE (Tier 3): курорт '%s' ≈ region '%s' → regions=%s (fuzzy API)",
                                        resort_name, r.get("name"), r.get("id")
                                    )
                                    resolved = True
                                    break
                        except Exception as e:
                            logger.error("❌ AUTO-RESOLVE (Tier 3) API error: %s", e)
                    
                    # Если не удалось авто-разрешить — fallback: ошибка для модели
                    if not resolved:
                        logger.warning(
                            "⚠️ RESORT-WITHOUT-REGION: курорт '%s' (%s) — не удалось авто-разрешить, блокируем",
                            resort_name, country_name
                        )
                        err_country_code = args.get("country", country_code)
                        return {
                            "status": "error",
                            "error": (
                                f"СИСТЕМНАЯ ОШИБКА: Клиент указал конкретный курорт '{resort_name}', "
                                f"но ты НЕ передал параметр regions в search_tours! "
                                f"ОБЯЗАТЕЛЬНО определи код региона: вызови get_dictionaries(type='region', regcountry={err_country_code}) "
                                f"и найди код для '{resort_name}'. Затем передай regions=КОД в search_tours. "
                                f"Без regions поиск вернёт туры по ВСЕЙ стране, а не по указанному курорту!"
                            ),
                            "_hint": f"Определи код региона '{resort_name}' через get_dictionaries и передай в regions."
                        }
            
            # ── Fix C2: Fallback из кэша предыдущего поиска ──
            # Если модель потеряла параметры при смене страны ("а если Египет?"),
            # восстанавливаем пропущенные из кэша. НИКОГДА не перезаписываем явно переданные.
            if self._last_search_params:
                _cache_keys = ("departure", "datefrom", "dateto", "nightsfrom", "nightsto",
                               "adults", "child", "childage1", "childage2", "childage3",
                               "stars", "starsbetter", "meal", "mealbetter")
                _restored = []
                for _ck in _cache_keys:
                    if (_ck not in args or args[_ck] is None) and _ck in self._last_search_params:
                        args[_ck] = self._last_search_params[_ck]
                        _restored.append(f"{_ck}={self._last_search_params[_ck]}")
                if _restored:
                    logger.info("📋 PARAM-CACHE: restored from previous search: %s", ", ".join(_restored))
                # Если страна изменилась — сбрасываем region из кэша (другая страна = другие регионы)
                if args.get("country") != self._last_search_params.get("_country"):
                    if "regions" in args and args.get("regions") == self._last_search_params.get("_regions"):
                        args.pop("regions", None)
                        logger.info("📋 PARAM-CACHE: cleared stale regions (country changed)")
            
            # ── Проверка полноты каскада (Fix 3B — блокирующая проверка) ──
            # Анализируем историю диалога, чтобы убедиться, что клиент ЯВНО указал критичные слоты
            is_cascade_complete, missing_slots = _check_cascade_slots(self.full_history, args, is_follow_up=bool(self._last_search_params))
            
            if not is_cascade_complete:
                self._metrics["cascade_incomplete_detections"] += 1
                logger.warning(
                    "⚠️ CASCADE-INCOMPLETE: клиент НЕ указал %s — блокируем search_tours и nudge модель",
                    ", ".join(missing_slots)
                )
                
                # Fix F5: Сохраняем параметры из заблокированного вызова для восстановления
                # при повторном search_tours после ответа клиента
                _cascade_saveable = ("departure", "datefrom", "dateto", "nightsfrom", "nightsto",
                                     "adults", "child", "childage1", "childage2", "childage3",
                                     "stars", "starsbetter", "meal", "mealbetter", "country", "regions", "hotels")
                _saved_count = 0
                for _sk in _cascade_saveable:
                    if args.get(_sk) is not None and _sk not in self._last_search_params:
                        self._last_search_params[_sk] = args[_sk]
                        _saved_count += 1
                if _saved_count > 0:
                    logger.info("📋 PARAM-CACHE (cascade-blocked): pre-saved %d params", _saved_count)
                
                # Возвращаем ошибку с ОДНИМ приоритетным вопросом (по порядку каскада: 2→3→4→5)
                # Правило § 0.3: "задавай ОДИН чёткий вопрос", не анкету
                first_missing = missing_slots[0]  # Берём первый по приоритету
                
                nudge_map = {
                    "город вылета": "'Из какого города планируете вылет?'",
                    "даты/месяц и длительность": "'Когда планируете поездку и на сколько ночей?'",
                    "даты/месяц вылета": "'В каком месяце планируете вылет?'",
                    "промежуток в месяце (начало/середина/конец)": "'В каком промежутке месяца планируете вылет — в начале, середине или конце?'",
                    "состав путешественников": "'Сколько взрослых едет и будут ли с вами дети?'",
                    "категорию отеля и тип питания": "'Какую категорию отеля и тип питания предпочитаете?'",
                    "категорию отеля (звёздность)": "'Какой категории отель вы рассматриваете?'",
                    "тип питания": "'Какой тип питания предпочитаете?'",
                }
                nudge = nudge_map.get(first_missing, f"Уточни у клиента: {first_missing}")
                
                return {
                    "status": "error",
                    "error": (
                        f"⛔ ПОИСК НЕ ЗАПУЩЕН! requestid НЕ создан! "
                        f"Причина: клиент НЕ указал {first_missing}. "
                        f"ОБЯЗАТЕЛЬНО спроси клиента ЯВНО: {nudge}. "
                        f"Задай ТОЛЬКО ОДИН вопрос, не перечисляй список! "
                        f"НЕ предлагай свои варианты и НЕ повышай категорию — только спроси! "
                        f"НЕ вызывай search_tours и НЕ вызывай get_search_status — нечего проверять, поиск НЕ был запущен!"
                    ),
                    "_hint": "ПОИСК НЕ ЗАПУЩЕН. requestid НЕ существует. Спроси ОДИН вопрос о недостающих данных. НЕ предлагай upsell! НЕ пытайся вызвать get_search_status!"
                }
            
            # ── Fix P5: Авто-коррекция nightsfrom (минимум 3 ночи) ──
            # По бизнес-логике nightsfrom < 3 бессмысленно (нет туров на 1-2 ночи)
            # Также если nightsfrom > nightsto — исправляем (nightsfrom = nightsto)
            nf = args.get("nightsfrom")
            nt = args.get("nightsto")
            if nf is not None and nf < 3:
                logger.warning("⚠️ nightsfrom=%d < 3, исправлено на 3 (минимум для туров)", nf)
                args["nightsfrom"] = 3
            if nf is not None and nt is not None and nf > nt:
                logger.warning("⚠️ nightsfrom=%d > nightsto=%d, исправлено nightsfrom=%d", nf, nt, nt)
                args["nightsfrom"] = nt
            
            # ── Fix P1: Safety-net для mealbetter ──
            # Если модель указала meal, но НЕ указала mealbetter → ставим mealbetter=0
            # (точное совпадение типа питания, а не "и лучше")
            # Дефолт API mealbetter=1 приводит к тому, что "полупансион" показывает "всё включено"
            if args.get("meal") is not None and args.get("mealbetter") is None:
                args["mealbetter"] = 0
                logger.info(
                    "🛡️ SAFETY-NET: mealbetter не указан при meal=%s → установлен mealbetter=0 (точное совпадение)",
                    args.get("meal")
                )
            
            # ── Fix F6 + C1: Safety-net для starsbetter ──
            # Сначала проверяем skip QC — если пользователь сказал "всё равно" / "без разницы",
            # удаляем stars/meal фильтры полностью (API вернёт все категории)
            _skip_qc_patterns = [
                r'(?:без\s*разницы|всё\s*равно|все\s*равно)',
                r'(?:не\s*важно|неважно|не\s*принципиально)',
                r'(?:на\s+(?:ваше?|твоё?|твое?)\s+усмотрени)',
                r'(?:рассмотрим\s+вариант|покажите?\s+что\s+есть|какие\s+есть)',
                r'(?:покажите?\s+что-нибудь|что\s+посоветуете)',
                r'(?:любой|любая|любое)\b',
            ]
            _last_user_msgs = [
                msg.get("content", "") for msg in self.full_history[-4:]
                if msg.get("role") == "user" and msg.get("content")
            ]
            _last_user_text = _last_user_msgs[-1].lower() if _last_user_msgs else ""
            _is_skip_qc = any(re.search(p, _last_user_text) for p in _skip_qc_patterns)

            if _is_skip_qc and args.get("stars") is not None:
                logger.info(
                    "🛡️ SAFETY-NET SKIP-QC: обнаружен skip quality check → удаляем stars=%s, starsbetter=%s, meal=%s, mealbetter=%s",
                    args.get("stars"), args.get("starsbetter"), args.get("meal"), args.get("mealbetter")
                )
                args.pop("stars", None)
                args.pop("starsbetter", None)
                args.pop("meal", None)
                args.pop("mealbetter", None)
            elif args.get("stars") is not None:
                if args.get("starsbetter") is None:
                    args["starsbetter"] = 0
                    logger.info(
                        "🛡️ SAFETY-NET F6: starsbetter не указан при stars=%s → starsbetter=0",
                        args.get("stars")
                    )
                elif args.get("starsbetter") == 1:
                    _user_stars_text = " ".join([
                        msg.get("content", "") for msg in self.full_history[-20:]
                        if msg.get("role") == "user" and msg.get("content")
                    ]).lower()
                    _wants_better = bool(re.search(
                        r'(?:от\s+\d|\d\s*[-–]\s*\d\s*(?:зв|★|\*)|не\s+ниже|минимум\s+\d|и\s+выше|выше)',
                        _user_stars_text
                    ))
                    if not _wants_better:
                        args["starsbetter"] = 0
                        logger.info(
                            "🛡️ SAFETY-NET C1: starsbetter=1 → 0 при stars=%s (нет 'от/диапазон/не ниже/минимум')",
                            args.get("stars")
                        )
            
            # ── Fix C2: Safety-net для nightsto при "дней" ──
            # Срабатывает ТОЛЬКО когда модель вообще не конвертировала дни→ночи
            # (nightsfrom == nightsto == raw_days). Если nightsfrom уже = days-1,
            # значит модель конвертировала корректно и nightsto = days — это верхний предел.
            if args.get("nightsto") is not None and args.get("nightsfrom") is not None:
                _user_dur_text = " ".join([
                    msg.get("content", "") for msg in self.full_history[-6:]
                    if msg.get("role") == "user" and msg.get("content")
                ]).lower()
                _days_match = re.search(r'(\d+)\s*(?:дней|дня|день)\b', _user_dur_text)
                if _days_match and 'ноч' not in _user_dur_text:
                    _max_days = int(_days_match.group(1))
                    _expected_nights = _max_days - 1
                    if (args["nightsto"] == _max_days
                            and args["nightsfrom"] == _max_days
                            and _expected_nights >= 3):
                        logger.info(
                            "🛡️ SAFETY-NET C2: nightsfrom=%d→%d, nightsto=%d (kept) (пользователь сказал '%d дней')",
                            _max_days, _expected_nights, _max_days, _max_days
                        )
                        args["nightsfrom"] = _expected_nights
            
            # ── Fix P7: Safety-net для "около N тыс" → диапазон ±20% ──
            if args.get("priceto") and not args.get("pricefrom"):
                _price_user_text = " ".join([
                    msg.get("content", "") for msg in self.full_history[-20:]
                    if msg.get("role") == "user" and msg.get("content")
                ]).lower()
                if re.search(r'(?:около|примерно|порядка|в\s+район[еу]|плюс.?минус)', _price_user_text):
                    _original_price = args["priceto"]
                    args["priceto"] = int(_original_price * 1.2)
                    args["pricefrom"] = int(_original_price * 0.8)
                    logger.info(
                        "💰 SAFETY-NET P7: 'около %s' → pricefrom=%s, priceto=%s",
                        _original_price, args["pricefrom"], args["priceto"]
                    )
            
            # ── Логирование пропущенных ключевых параметров (информационное) ──
            missing_params = []
            if not args.get("adults"):
                missing_params.append("adults")
            if not args.get("datefrom"):
                missing_params.append("datefrom")
            if not args.get("dateto"):
                missing_params.append("dateto")
            if not args.get("stars"):
                missing_params.append("stars")
            if not args.get("meal"):
                missing_params.append("meal")
            
            if missing_params:
                logger.info(
                    "ℹ️ search_tours вызван с дефолтными параметрами: %s",
                    ", ".join(missing_params)
                )
            
            self._metrics["total_searches"] += 1
            request_id = await self.tourvisor.search_tours(
                departure=args.get("departure"),
                country=args.get("country"),
                date_from=args.get("datefrom"),
                date_to=args.get("dateto"),
                nights_from=args.get("nightsfrom", 7),
                nights_to=args.get("nightsto", 10),
                adults=args.get("adults", 2),
                children=args.get("child", 0),
                child_ages=[args.get(f"childage{i}") for i in [1,2,3] if args.get(f"childage{i}")],
                stars=args.get("stars"),
                meal=args.get("meal"),
                rating=args.get("rating"),
                hotels=args.get("hotels"),
                regions=args.get("regions"),
                subregions=args.get("subregions"),
                operators=args.get("operators"),
                price_from=args.get("pricefrom"),
                price_to=args.get("priceto"),
                hotel_types=args.get("hoteltypes"),
                services=args.get("services"),
                onrequest=args.get("onrequest"),
                directflight=args.get("directflight"),
                flightclass=args.get("flightclass"),
                currency=args.get("currency"),
                pricetype=args.get("pricetype"),
                starsbetter=args.get("starsbetter"),
                mealbetter=args.get("mealbetter"),
                hideregular=args.get("hideregular")
            )
            
            # Проверка на ошибку (прошлые даты и т.п.)
            if request_id is None:
                return {
                    "error": "Не удалось создать поиск. Проверьте даты — они должны быть в будущем (2026 год или позже).",
                    "hint": "Используйте формат ДД.ММ.ГГГГ, например 01.03.2026"
                }
            
            # ── P13: Кэшируем requestid для валидации в get_search_status ──
            self._last_requestid = str(request_id)
            # Инвалидируем tourid_map — новый поиск, старые tourid недействительны
            self._tourid_map = {}
            if args.get("priceto"):
                self._user_stated_budget = int(args["priceto"])
            
            # ── Fix C2: Сохраняем параметры успешного поиска в кэш ──
            self._last_search_params = {
                k: v for k, v in args.items()
                if k in ("departure", "datefrom", "dateto", "nightsfrom", "nightsto",
                         "adults", "child", "childage1", "childage2", "childage3",
                         "stars", "starsbetter", "meal", "mealbetter")
                and v is not None
            }
            # Запоминаем страну и регион для детекции смены направления
            self._last_search_params["_country"] = args.get("country")
            self._last_search_params["_regions"] = args.get("regions")
            if args.get("hotels"):
                self._last_search_params["_hotels"] = args.get("hotels")
            logger.info("📋 PARAM-CACHE: saved %d params from search", len(self._last_search_params))
            
            # ── Сохраняем "идеальные" параметры для пересортировки результатов ──
            self._ideal_datefrom = args.get("datefrom")
            self._ideal_nightsfrom = _safe_int(args.get("nightsfrom"))
            self._ideal_nightsto = _safe_int(args.get("nightsto"))
            self._has_budget = bool(args.get("pricefrom") or args.get("priceto"))
            logger.info(
                "📋 RELEVANCE-PARAMS: datefrom=%s, nights=%s-%s, has_budget=%s",
                self._ideal_datefrom, self._ideal_nightsfrom, self._ideal_nightsto, self._has_budget
            )
            
            self._search_awaiting_results = True
            return {"requestid": str(request_id), "message": f"⛔ Поиск запущен (requestid={request_id}). ОБЯЗАТЕЛЬНО сейчас вызови get_search_status(requestid={request_id}). Ты ещё НЕ знаешь результатов — НЕ говори клиенту 'Нашёл' пока не вызовешь get_search_results!"}
        
        elif name == "get_search_status":
            # ── P1: Валидация requestid — отклоняем плейсхолдеры ──
            request_id = str(args.get("requestid", ""))
            if not request_id.replace(" ", "").isdigit():
                self._metrics.setdefault("placeholder_id_rejections", 0)
                self._metrics["placeholder_id_rejections"] += 1
                if self._last_requestid:
                    logger.warning(
                        "⚠️ PLACEHOLDER-REJECT: requestid='%s' содержит буквы → подставляем кэшированный %s",
                        request_id, self._last_requestid
                    )
                    request_id = self._last_requestid
                else:
                    logger.warning("⚠️ PLACEHOLDER-REJECT: requestid='%s' содержит буквы, кэш пуст", request_id)
                    return {
                        "status": "error",
                        "error": (
                            f"⛔ НЕВЕРНЫЙ requestid: '{request_id}' — это НЕ числовой ID! "
                            f"requestid — это ЧИСЛОВАЯ строка (например '11767315205'), "
                            f"которую возвращает search_tours. НЕ придумывай requestid! "
                            f"Если поиск не был запущен — сначала вызови search_tours."
                        )
                    }
            
            # ⚡ КРИТИЧЕСКИ ВАЖНО: Внутренний polling с ожиданием!
            # Без этого AI вызывает get_search_status в цикле и сжигает все итерации.
            # Теперь ОДНА итерация AI = полное ожидание завершения поиска.
            max_wait = 60  # Максимум ожидания в секундах
            poll_interval = 3  # Интервал опроса
            elapsed = 0
            last_status = {}
            
            while elapsed < max_wait:
                last_status = await self.tourvisor.get_search_status(request_id)
                state = last_status.get("state")
                
                if state == "finished":
                    # Проверяем есть ли результаты
                    hotels_found = last_status.get("hotelsfound", 0)
                    tours_found = last_status.get("toursfound", 0)

                    if hotels_found == 0 or tours_found == 0:
                        _dep_code = self._last_search_params.get("departure")
                        _dep_city = _DEPARTURE_CITIES.get(_dep_code, "") if _dep_code else ""
                        _major_cities = {1, 3, 5}  # Москва, Екатеринбург, СПб
                        _dep_hint = ""
                        if _dep_code and _dep_code not in _major_cities:
                            _dep_hint = (
                                f" ⚠️ Из города '{_dep_city}' (departure={_dep_code}) — ноль туров. "
                                f"Вероятно, из этого города нет рейсов в данную страну. "
                                f"Проверь через get_dictionaries(type=country, cndep={_dep_code}) "
                                f"какие направления доступны и предложи клиенту ближайшие "
                                f"альтернативные города вылета."
                            )
                        _hotel_hint = ""
                        _hotel_code_str = self._last_search_params.get("_hotels", "")
                        _meal_code = self._last_search_params.get("meal")
                        if _hotel_code_str and _meal_code:
                            _first_hotel = _hotel_code_str.split(",")[0].strip()
                            _hotel_hint = (
                                f" ⚠️ Поиск конкретного отеля (hotels={_hotel_code_str}) "
                                f"с meal={_meal_code} вернул 0 туров. "
                                f"Вызови get_hotel_info(hotelcode={_first_hotel}) — "
                                f"проверь поле meallist, чтобы узнать какие типы питания "
                                f"доступны в этом отеле, и предложи клиенту доступный вариант. "
                                f"НЕ предлагай другие отели, пока не проверил питание в этом!"
                            )
                        raise NoResultsError(
                            f"Поиск завершён: найдено {hotels_found} отелей, {tours_found} туров.{_dep_hint}{_hotel_hint}",
                            filters_hint="Попробуйте расширить даты, увеличить бюджет или убрать фильтры"
                        )

                    last_status["_hint"] = (
                        f"Поиск завершён! Найдено {hotels_found} отелей, {tours_found} туров. "
                        f"Вызови get_search_results с requestid для получения списка отелей."
                    )
                    if self._user_stated_budget:
                        _mp = int(last_status.get("minprice", 0))
                        if _mp > self._user_stated_budget:
                            last_status["_hint"] += (
                                f" ВНИМАНИЕ: минимальная цена ({_mp} руб.) ПРЕВЫШАЕТ бюджет клиента "
                                f"({self._user_stated_budget} руб.)! ОБЯЗАТЕЛЬНО предупреди клиента!"
                            )
                    return last_status
                
                if state == "no search results":
                    last_status["_hint"] = "Поиск не найден. requestid недействителен — нужен новый поиск."
                    return last_status
                
                # Если уже есть достаточно результатов — можно забирать частичные, не ждать 100%
                # Fix F1: Добавлено условие для поиска конкретного отеля (1 отель, но много туров)
                hotels_found = last_status.get("hotelsfound", 0)
                tours_found = last_status.get("toursfound", 0)
                progress = last_status.get("progress", 0)
                if (hotels_found >= 3 and progress >= 40) or \
                   (hotels_found >= 1 and tours_found >= 20 and progress >= 30) or \
                   (hotels_found >= 1 and elapsed >= 12):
                    logger.info("📊 SEARCH READY (partial)  requestid=%s  progress=%s%%  hotels=%s — returning early",
                                request_id, progress, hotels_found)
                    last_status["_hint"] = (
                        f"Поиск ещё идёт ({progress}%), но уже найдено {hotels_found} отелей. "
                        f"Вызови get_search_results с этим requestid для показа результатов."
                    )
                    if self._user_stated_budget:
                        _mp = int(last_status.get("minprice", 0))
                        if _mp > self._user_stated_budget:
                            last_status["_hint"] += (
                                f" ВНИМАНИЕ: минимальная цена ({_mp} руб.) ПРЕВЫШАЕТ бюджет клиента "
                                f"({self._user_stated_budget} руб.)! ОБЯЗАТЕЛЬНО предупреди клиента!"
                            )
                    return last_status
                
                # Ждём перед следующим опросом
                logger.debug("📊 SEARCH WAITING  requestid=%s  progress=%s%%  hotels=%s  elapsed=%ds  sleeping %ds…",
                            request_id, progress, hotels_found, elapsed, poll_interval)
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval
            
            # Timeout — возвращаем что есть
            hotels_found = last_status.get("hotelsfound", 0)
            if hotels_found > 0:
                last_status["_hint"] = (
                    f"Поиск не завершился за {max_wait}с, но найдено {hotels_found} отелей. "
                    f"Вызови get_search_results для показа частичных результатов."
                )
            else:
                last_status["_hint"] = (
                    f"Поиск не завершился за {max_wait}с и результатов нет. "
                    f"Предложи клиенту изменить параметры (даты, бюджет, направление)."
                )
            return last_status
        
        elif name == "get_search_results":
            self._search_awaiting_results = False
            # ── P1: Валидация requestid ──
            _rid = str(args.get("requestid", ""))
            if not _rid.replace(" ", "").isdigit():
                self._metrics.setdefault("placeholder_id_rejections", 0)
                self._metrics["placeholder_id_rejections"] += 1
                if self._last_requestid:
                    logger.warning("⚠️ PLACEHOLDER-REJECT get_search_results: '%s' → кэш %s", _rid, self._last_requestid)
                    args["requestid"] = self._last_requestid
                else:
                    return {"hotels_found": 0, "tours_found": 0, "hotels": [],
                            "error": f"⛔ НЕВЕРНЫЙ requestid: '{_rid}'. Сначала вызови search_tours для получения числового requestid."}
            
            _pool_size = 30 if self._ideal_datefrom else 10
            _actual_per_page = max(int(args.get("onpage", _pool_size)), _pool_size)
            full_results = await self.tourvisor.get_search_results(
                request_id=args["requestid"],
                page=args.get("page", 1),
                per_page=_actual_per_page,
                include_operators=args.get("operatorstatus") == 1,
                no_description=args.get("nodescription") == 1
            )
            
            # Сокращаем результаты для AI — формат карточек с картинками
            hotels = full_results.get("result", {}).get("hotel", [])
            
            # ── Уровень 1: для каждого отеля выбираем ЛУЧШИЙ тур по релевантности ──
            _scored_hotels = []
            for h in hotels:
                tours = h.get("tours", {}).get("tour", [])
                logger.debug(
                    "🏨 %s  tours_in_hotel=%d  nights_available=%s",
                    (h.get("hotelname") or "?")[:30],
                    len(tours),
                    sorted(set(int(t.get("nights", 0)) for t in tours if t.get("nights")))
                )
                best_tour = _pick_best_tour(
                    tours, self._ideal_datefrom,
                    self._ideal_nightsfrom, self._ideal_nightsto
                )
                
                picture = h.get("picturelink", "")
                has_real_photo = h.get("isphoto") == 1 and picture and "/reg-" not in picture
                
                entry = {
                    "hotelcode": h.get("hotelcode"),
                    "hotelname": h.get("hotelname"),
                    "hotelstars": h.get("hotelstars"),
                    "hotelrating": h.get("hotelrating"),
                    "regionname": h.get("regionname"),
                    "countryname": h.get("countryname"),
                    "price": h.get("price"),
                    "seadistance": h.get("seadistance"),
                    "picturelink": picture if has_real_photo else None,
                    "hoteldescription": h.get("hoteldescription"),
                    "fulldesclink": h.get("fulldesclink"),
                    "tour": {
                        "tourid": best_tour.get("tourid"),
                        "price": best_tour.get("price"),
                        "flydate": best_tour.get("flydate"),
                        "nights": best_tour.get("nights"),
                        "meal": best_tour.get("mealrussian"),
                        "room": best_tour.get("room"),
                        "placement": best_tour.get("placement"),
                        "operatorname": best_tour.get("operatorname"),
                        "tourname": best_tour.get("tourname"),
                        "promo": best_tour.get("promo"),
                        "regular": best_tour.get("regular"),
                        "onrequest": best_tour.get("onrequest"),
                        "flightstatus": best_tour.get("flightstatus"),
                        "hotelstatus": best_tour.get("hotelstatus"),
                        "nightflight": best_tour.get("nightflight"),
                        "noflight": best_tour.get("noflight"),
                        "notransfer": best_tour.get("notransfer"),
                        "nomedinsurance": best_tour.get("nomedinsurance"),
                        "nomeal": best_tour.get("nomeal")
                    } if best_tour else None
                }
                
                # Рассчитываем relevance score для сортировки
                # Ночи — основной фактор (вес 15), дата — вторичный (вес 1)
                _rel_score = 0.0
                if best_tour:
                    _rel_score += _nights_penalty(
                        _safe_int(best_tour.get("nights"), 0),
                        self._ideal_nightsfrom, self._ideal_nightsto
                    ) * 15
                if self._ideal_datefrom and best_tour:
                    try:
                        _fly = _dt.strptime(best_tour.get("flydate", ""), "%d.%m.%Y")
                        _ideal = _dt.strptime(self._ideal_datefrom, "%d.%m.%Y")
                        _rel_score += abs((_fly - _ideal).days)
                    except (ValueError, TypeError):
                        _rel_score += 99
                
                _scored_hotels.append((_rel_score, _safe_int(best_tour.get("price"), 999999999), entry))
                if best_tour:
                    logger.debug(
                        "🏨 %s  nights=%s  flydate=%s  price=%s  rel=%.1f",
                        h.get("hotelname", "?")[:30],
                        best_tour.get("nights"), best_tour.get("flydate"),
                        best_tour.get("price"), _rel_score
                    )
            
            # ── Уровень 2: сортировка отелей ──
            if not self._has_budget and self._ideal_datefrom:
                _scored_hotels.sort(key=lambda x: (x[0], x[1]))
                _top5 = _scored_hotels[:5]
                logger.info(
                    "🎯 RELEVANCE SORT: %d hotels re-ranked. Top5 nights: %s",
                    len(_scored_hotels),
                    [item[2].get("tour", {}).get("nights") for item in _top5]
                )
            else:
                _scored_hotels.sort(key=lambda x: x[1])
                logger.info("💰 PRICE SORT: %d hotels sorted by price (budget specified)", len(_scored_hotels))
            
            simplified = [item[2] for item in _scored_hotels[:5]]
            
            # ── Строим tour_cards для нового фронтенда ──
            self._pending_tour_cards = [
                _map_hotel_to_card(h, self._last_departure_city)
                for h in simplified
            ]
            logger.info("🎴 Built %d tour cards for frontend", len(self._pending_tour_cards))
            
            status = full_results.get("status", {})

            # ── Сокращённые данные для AI (без описаний/цен/дат — они на карточках) ──
            ai_hotels = []
            for h in simplified:
                tour = h.get("tour") or {}
                warnings = []
                if tour.get("nightflight"):
                    warnings.append("ночной перелёт")
                if tour.get("noflight"):
                    warnings.append("без перелёта")
                if tour.get("notransfer"):
                    warnings.append("без трансфера")
                if tour.get("nomedinsurance"):
                    warnings.append("без мед.страховки")
                if tour.get("nomeal"):
                    warnings.append("без питания")
                if tour.get("onrequest"):
                    warnings.append("под запрос")
                entry = {
                    "hotelcode": h.get("hotelcode"),
                    "hotelname": h.get("hotelname"),
                    "tourid": (h.get("tour") or {}).get("tourid"),
                }
                if warnings:
                    entry["warnings"] = warnings
                ai_hotels.append(entry)
            
            # ── P13: Кэшируем tourid по позиции для resolve "третий вариант" ──
            self._tourid_map = {}
            for idx, h_entry in enumerate(ai_hotels, 1):
                tid = h_entry.get("tourid")
                if tid:
                    self._tourid_map[idx] = {
                        "tourid": str(tid),
                        "hotelcode": h_entry.get("hotelcode"),
                        "hotelname": h_entry.get("hotelname"),
                    }
            if self._tourid_map:
                logger.info("🗂️ TOURID-CACHE: сохранено %d позиций: %s",
                            len(self._tourid_map),
                            {k: v["tourid"] for k, v in self._tourid_map.items()})

            if not ai_hotels and int(args.get("page", 1)) > 1:
                return {
                    "hotels_found": status.get("hotelsfound", 0),
                    "tours_found": status.get("toursfound", 0),
                    "hotels": [],
                    "_hint": (
                        "На этой странице больше нет вариантов — все доступные отели "
                        "уже были показаны ранее. Сообщи клиенту: «Все доступные варианты "
                        "по этим параметрам уже показаны. Хотите изменить фильтры или "
                        "посмотреть другое направление?»"
                    ),
                }

            return {
                "hotels_found": status.get("hotelsfound", len(hotels)),
                "tours_found": status.get("toursfound", 0),
                "hotels": ai_hotels,
                "_hint": "Карточки с фото, ценами, датами, питанием, звёздами УЖЕ отображены фронтендом. НЕ перечисляй отели, цены, описания, даты, питание, звёзды в тексте! Напиши ТОЛЬКО краткий комментарий (1-2 предложения) и спроси клиента."
            }
        
        elif name == "get_dictionaries":
            # Определяем какой справочник запрашивается
            dict_type = args.get("type", "")
            
            if "departure" in dict_type:
                return await self.tourvisor.get_departures()
            elif "country" in dict_type:
                return await self.tourvisor.get_countries(args.get("cndep"))
            elif "subregion" in dict_type:
                return await self.tourvisor.get_subregions(args.get("regcountry"))
            elif "region" in dict_type:
                regions = await self.tourvisor.get_regions(args.get("regcountry"))
                name_filter = args.get("name", "").lower().strip()
                if name_filter:
                    name_words = set(re.findall(r'\w+', name_filter))
                    filtered = [
                        r for r in regions
                        if name_filter in r.get("name", "").lower()
                        or r.get("name", "").lower() in name_filter
                        or any(w in r.get("name", "").lower() for w in name_words if len(w) > 3)
                    ]
                    if filtered:
                        regions = filtered
                return regions
            elif "meal" in dict_type:
                return await self.tourvisor.get_meals()
            elif "stars" in dict_type:
                return await self.tourvisor.get_stars()
            elif "operator" in dict_type:
                return await self.tourvisor.get_operators(
                    args.get("flydeparture"),
                    args.get("flycountry")
                )
            elif "services" in dict_type:
                return await self.tourvisor.get_services()
            elif "flydate" in dict_type:
                return await self.tourvisor.get_flydates(
                    args.get("flydeparture"),
                    args.get("flycountry")
                )
            elif "hotel" in dict_type:
                # Собираем типы отелей
                hotel_types = []
                for ht in ["active", "relax", "family", "health", "city", "beach", "deluxe"]:
                    if args.get(f"hot{ht}") == 1:
                        hotel_types.append(ht)
                
                hotels = await self.tourvisor.get_hotels(
                    country_id=args.get("hotcountry"),
                    region_id=args.get("hotregion"),
                    stars=args.get("hotstars"),
                    rating=args.get("hotrating"),
                    hotel_types=hotel_types if hotel_types else None
                )
                # ── Фильтруем по названию: exact substring → multi-variant fuzzy ──
                name_filter = re.sub(r'[^\w\s]', '', args.get("name", ""), flags=re.UNICODE).lower().strip()
                name_filter = re.sub(r'\s+', ' ', name_filter).strip()

                if name_filter:
                    matched = [h for h in hotels if name_filter in h.get("name", "").lower()]

                    if not matched and len(name_filter) >= 3:
                        has_cyrillic = any('\u0400' <= c <= '\u04ff' for c in name_filter)
                        if has_cyrillic:
                            variants = list(dict.fromkeys([
                                _transliterate(name_filter),
                                _transliterate(name_filter, _CYR_TO_LAT_ALT),
                            ]))
                        else:
                            variants = [name_filter]
                        matched = _fuzzy_hotel_match(variants, hotels)
                        logger.info("HOTEL-SEARCH fuzzy %s, found=%d", variants, len(matched))

                    hotels = matched
                return hotels[:20]
            elif "currency" in dict_type:
                # Курсы валют туроператоров
                return await self.tourvisor.get_currencies()
            else:
                return {"error": f"Неизвестный тип справочника: {dict_type}"}
        
        elif name == "actualize_tour":
            # ── P1: Валидация tourid — отклоняем плейсхолдеры, пробуем resolve из кэша ──
            _tid = str(args.get("tourid", ""))
            if not _tid.replace(" ", "").isdigit():
                self._metrics.setdefault("placeholder_id_rejections", 0)
                self._metrics["placeholder_id_rejections"] += 1
                resolved = self._resolve_tourid_from_text(_tid)
                if resolved:
                    logger.warning("⚠️ PLACEHOLDER-REJECT actualize_tour: '%s' → resolved tourid=%s", _tid, resolved)
                    args["tourid"] = resolved
                else:
                    return {"error": (
                        f"⛔ НЕВЕРНЫЙ tourid: '{_tid}'. tourid — это ЧИСЛОВАЯ строка (например '99195143679290'), "
                        f"которую возвращает get_search_results. Используй ТОЧНЫЙ tourid из результатов поиска."
                    )}
            return await self.tourvisor.actualize_tour(
                tour_id=args["tourid"],
                request_mode=args.get("request", 2),
                currency=args.get("currency", 0)
            )
        
        elif name == "get_tour_details":
            # ── P1: Валидация tourid ──
            _tid = str(args.get("tourid", ""))
            if not _tid.replace(" ", "").isdigit():
                self._metrics.setdefault("placeholder_id_rejections", 0)
                self._metrics["placeholder_id_rejections"] += 1
                resolved = self._resolve_tourid_from_text(_tid)
                if resolved:
                    logger.warning("⚠️ PLACEHOLDER-REJECT get_tour_details: '%s' → resolved tourid=%s", _tid, resolved)
                    args["tourid"] = resolved
                else:
                    return {"error": (
                        f"⛔ НЕВЕРНЫЙ tourid: '{_tid}'. Используй ЧИСЛОВОЙ tourid из результатов get_search_results."
                    )}
            result = await self.tourvisor.get_tour_details(
                tour_id=args["tourid"],
                currency=args.get("currency", 0)
            )
            
            # Fix F2 + C4: При iserror от actdetail — пробуем до 2 альтернативных tourid
            if isinstance(result, dict) and result.get("iserror") and self._tourid_map:
                current_tid = str(args["tourid"])
                _fallback_tries = 0
                for pos, entry in sorted(self._tourid_map.items()):
                    alt_tid = entry["tourid"]
                    if alt_tid != current_tid:
                        logger.warning(
                            "🔄 ACTDETAIL FALLBACK %d: tourid %s iserror → trying alt %s (pos %d, hotel=%s)",
                            _fallback_tries + 1, current_tid, alt_tid, pos, entry.get("hotelname", "?")
                        )
                        try:
                            alt_result = await self.tourvisor.get_tour_details(tour_id=alt_tid)
                            if isinstance(alt_result, dict) and not alt_result.get("iserror"):
                                logger.info("✅ ACTDETAIL FALLBACK SUCCESS: alt tourid %s returned flight data", alt_tid)
                                result = alt_result
                                break
                        except Exception as e:
                            logger.warning("⚠️ ACTDETAIL FALLBACK FAILED: alt tourid %s → %s", alt_tid, str(e)[:100])
                        _fallback_tries += 1
                        if _fallback_tries >= 2:
                            break
            
            # Fix C3: _hint для неполных данных о рейсе
            if isinstance(result, dict) and not result.get("iserror"):
                _flights = result.get("data", {}).get("flights", []) if "data" in result else result.get("flights", [])
                if _flights:
                    _fwd = _flights[0].get("forward", [{}]) if isinstance(_flights[0], dict) else [{}]
                    if _fwd and isinstance(_fwd[0], dict):
                        _has_time = bool(_fwd[0].get("departure", {}).get("time"))
                        _has_airline = bool(_fwd[0].get("company", {}).get("name"))
                        if not _has_time and not _has_airline:
                            result["_hint"] = (
                                "Данные о рейсе НЕПОЛНЫЕ — доступны только даты перелёта, "
                                "но НЕТ времени вылета и авиакомпании. "
                                "Сообщи клиенту даты и скажи, что время и авиакомпания "
                                "будут уточнены при бронировании. НЕ вызывай get_tour_details повторно."
                            )
                            logger.info("ℹ️ ACTDETAIL: неполные данные о рейсе (только даты) — добавлен _hint")
            
            return result
        
        elif name == "get_hotel_info":
            hotel = await self.tourvisor.get_hotel_info(
                hotel_code=args["hotelcode"],
                big_images=True,  # Всегда большие картинки
                remove_tags=True,  # Без HTML тегов
                include_reviews=args.get("reviews") == 1
            )
            
            # Форматируем для карточки с полным описанием
            images = hotel.get("images", {})
            if isinstance(images, dict):
                images = images.get("image", [])
            if isinstance(images, str):
                images = [images]
            
            reviews = hotel.get("reviews", {})
            if isinstance(reviews, dict):
                reviews = reviews.get("review", [])
            
            _info_fields = [hotel.get(f) for f in ("description", "territory", "beach", "child",
                            "services", "servicefree", "servicepay", "inroom", "roomtypes")]
            _null_count = sum(1 for v in _info_fields if v is None or v == "")
            _empty_warning = None
            if _null_count >= 7:
                _empty_warning = (
                    "Подробная информация по этому отелю временно недоступна. "
                    "Скажи клиенту: 'К сожалению, подробная информация по этому отелю "
                    "временно недоступна. Рекомендую уточнить детали у менеджера.' "
                    "НЕ говори 'у меня нет информации'."
                )
            
            return {
                "name": hotel.get("name"),
                "stars": hotel.get("stars"),
                "rating": hotel.get("rating"),
                "country": hotel.get("country"),
                "region": hotel.get("region"),
                "placement": hotel.get("placement"),
                "seadistance": hotel.get("seadistance"),
                "build": hotel.get("build"),
                "description": hotel.get("description"),
                "territory": hotel.get("territory"),
                "inroom": hotel.get("inroom"),
                "roomtypes": hotel.get("roomtypes"),
                "beach": hotel.get("beach"),
                "child": hotel.get("child"),
                "services": hotel.get("services"),
                "servicefree": hotel.get("servicefree"),
                "servicepay": hotel.get("servicepay"),
                "meallist": hotel.get("meallist"),
                "mealtypes": hotel.get("mealtypes"),
                "animation": hotel.get("animation"),
                "images": images[:5] if images else [],  # Первые 5 фото
                "images_count": hotel.get("imagescount"),
                "coordinates": {
                    "lat": hotel.get("coord1"),
                    "lon": hotel.get("coord2")
                },
                "reviews": [
                    {
                        "name": r.get("name"),
                        "rate": r.get("rate"),
                        "content": r.get("content", "")[:300] + "..." if len(r.get("content", "")) > 300 else r.get("content", ""),
                        "traveltime": r.get("traveltime"),
                        "sourcelink": r.get("sourcelink", "")  # ВАЖНО для указания источника!
                    } for r in (reviews[:3] if reviews else [])
                ] if args.get("reviews") == 1 else [],
                "_warning": _empty_warning,
            }
        
        elif name == "get_hot_tours":
            # Fix B4: модель может передать "country" (singular) вместо "countries" (plural)
            # Принимаем оба варианта через fallback
            
            # ── P14: tourtype=1 для "на море" если не указан ──
            if args.get("tourtype", 0) == 0:
                _hot_user_text = " ".join([
                    msg.get("content", "") for msg in self.full_history[-20:]
                    if msg.get("role") == "user" and msg.get("content")
                ]).lower()
                if re.search(r'(?:на\s+мор[еёюя]|пляж\w*|beach)', _hot_user_text):
                    args["tourtype"] = 1
                    logger.info("✅ P14: tourtype=1 авто-установлен для 'на море'")
            
            # ── Safety-net: проверка города вылета в тексте пользователя ──
            _hot_departure_text = " ".join([
                msg.get("content", "") for msg in self.full_history
                if msg.get("role") == "user" and msg.get("content")
                and not msg.get("content", "").startswith("Результаты")
            ]).lower()
            _has_departure = any(re.search(p, _hot_departure_text) for p in _DEPARTURE_PATTERNS)
            if not _has_departure:
                logger.warning("🛡️ HOT-TOURS-SAFETY: клиент не указал город вылета — блокируем")
                return {
                    "status": "error",
                    "error": (
                        "⛔ Для горящих туров ОБЯЗАТЕЛЕН город вылета. "
                        "Клиент НЕ указал город. Спроси: «Из какого города планируете вылет?»"
                    ),
                }

            tours = await self.tourvisor.get_hot_tours(
                city=args["city"],
                count=args.get("items", 10),
                city2=args.get("city2"),
                city3=args.get("city3"),
                uniq2=args.get("uniq2"),
                uniq3=args.get("uniq3"),
                countries=args.get("countries") or args.get("country"),
                regions=args.get("regions"),
                operators=args.get("operators"),
                datefrom=args.get("datefrom"),
                dateto=args.get("dateto"),
                stars=args.get("stars"),
                meal=args.get("meal"),
                rating=args.get("rating"),
                max_days=args.get("maxdays"),
                tour_type=args.get("tourtype", 0),
                visa_free=args.get("visa") == 1,
                sort_by_price=args.get("sort") == 1,
                picturetype=1,  # Fix R7: всегда 250px для качественных фото
                currency=args.get("currency", 0)
            )
            
            # ── Safety-net: 0 результатов — честный ответ ──
            if not tours:
                self._pending_tour_cards = []
                logger.info("🛡️ HOT-TOURS: 0 результатов — возвращаем честный ответ")
                _countries_name = args.get("countries") or args.get("country") or "указанном направлении"
                return {
                    "total_found": 0,
                    "tours": [],
                    "_hint": (
                        "⛔ НАЙДЕНО 0 ГОРЯЩИХ ТУРОВ. Честно скажи клиенту: "
                        "«К сожалению, горящих туров сейчас нет. "
                        "Хотите сделать обычный поиск с конкретными параметрами?» "
                        "НЕ говори «Нашёл!» если ничего не найдено."
                    ),
                }
            
            # Сокращаем результаты для AI — формат карточек с картинками
            simplified = []
            for t in tours[:7]:  # Максимум 7 горящих туров
                # Вычисляем скидку (безопасное преобразование — API отдаёт числа как строки)
                price = _safe_int(t.get("price"))
                price_old = _safe_int(t.get("priceold"))
                discount = round((price_old - price) / price_old * 100) if price_old > 0 else 0
                
                # Проверяем картинку — не показываем заглушки
                picture = t.get("hotelpicture", "")
                has_real_photo = picture and "/reg-" not in picture
                
                simplified.append({
                    "hotelcode": t.get("hotelcode"),
                    "hotelname": t.get("hotelname"),
                    "hotelstars": t.get("hotelstars"),
                    "hotelrating": t.get("hotelrating"),
                    "countryname": t.get("countryname"),
                    "regionname": t.get("hotelregionname"),
                    "departurename": t.get("departurename"),  # Город вылета
                    "departurenamefrom": t.get("departurenamefrom"),  # "из Москвы"
                    "operatorname": t.get("operatorname"),  # Туроператор
                    "price_per_person": price,
                    "price_old": price_old,
                    "discount_percent": discount,
                    "currency": t.get("currency", "RUB"),  # Валюта
                    "flydate": t.get("flydate"),
                    "nights": t.get("nights"),
                    "meal": t.get("meal"),
                    "tourid": t.get("tourid"),
                    "picturelink": picture if has_real_photo else None,  # Только реальные фото
                    "fulldesclink": t.get("fulldesclink")  # Ссылка
                })
            
            # ── Строим tour_cards для нового фронтенда ──
            self._pending_tour_cards = [
                _map_hot_tour_to_card(t) for t in simplified
            ]
            logger.info("🎴 Built %d hot tour cards for frontend", len(self._pending_tour_cards))
            
            # ── Сокращённые данные для AI (без цен/дат/звёзд — они на карточках) ──
            ai_tours = []
            for t in simplified:
                ai_tours.append({
                    "hotelcode": t.get("hotelcode"),
                    "hotelname": t.get("hotelname"),
                    "tourid": t.get("tourid"),
                })

            # ── P12: Динамическая формулировка цены с учётом группы ──
            _user_msgs = " ".join([
                msg.get("content", "") for msg in self.full_history[-20:]
                if msg.get("role") == "user" and msg.get("content")
            ]).lower()
            # Считаем путешественников из текста
            _total_travelers = 0
            _adults_match = re.search(r'(\d+)\s*(?:взр|в\b)', _user_msgs)
            _child_match = re.search(r'(\d+)\s*(?:реб|дет|р\b)', _user_msgs)
            if _adults_match:
                _total_travelers += int(_adults_match.group(1))
            if _child_match:
                _total_travelers += int(_child_match.group(1))
            # Ещё проверяем паттерн "ребёнок" / "с ребёнком" (1 ребёнок)
            if not _child_match and re.search(r'(?:ребен\w*|с\s+реб)', _user_msgs):
                _total_travelers += 1
            if _total_travelers < 1:
                _total_travelers = 2  # fallback
            
            if _total_travelers == 1:
                price_note = "ВАЖНО: Цены указаны ЗА ЧЕЛОВЕКА."
            else:
                price_note = f"ВАЖНО: Цены указаны ЗА ЧЕЛОВЕКА! Для {_total_travelers} путешественников умножай на {_total_travelers}."

            # ── Детекция параметров пользователя, которые hot tours НЕ поддерживает ──
            _ignored = []
            if re.search(r'\d+\s*(?:ноч|дн[еёяи]|недел)', _user_msgs):
                _ignored.append("количество ночей/дней")
            _has_children = bool(re.search(r'(?:реб[её]н|дет[еиясь])', _user_msgs))
            if _has_children:
                _ignored.append("состав семьи (дети)")
            if re.search(r'(?:бюджет|\d+\s*[-–]\s*\d+\s*[кКтТ]|(?:от|до)\s+\d+\s*[кКтТ])', _user_msgs):
                _ignored.append("бюджет")
            # ── Adults Only детекция ──
            _ao_names = [
                t.get("hotelname", "") for t in simplified
                if re.search(r'(?:adults?\s*only|16\+|18\+)', t.get("hotelname", ""), re.IGNORECASE)
            ]
            _adults_only_warning = None
            if _ao_names and _has_children:
                _adults_only_warning = (
                    f"⚠️ В выдаче есть отели «только для взрослых»: {', '.join(_ao_names)}. "
                    "Они НЕ подходят для семей с детьми! ОБЯЗАТЕЛЬНО предупреди клиента."
                )
                logger.info("⚠️ ADULTS-ONLY hotels detected for family: %s", _ao_names)

            _hot_warning = None
            if _ignored:
                _hot_warning = (
                    f"Горящие туры НЕ фильтруются по: {', '.join(_ignored)}. "
                    "ОБЯЗАТЕЛЬНО предупреди клиента, что показанные варианты "
                    "могут отличаться по этим параметрам. "
                    "Предложи обычный поиск (search_tours) для точных фильтров."
                )

            return {
                "total_found": len(tours),
                "note": price_note,
                "tours": ai_tours,
                "_hint": (
                    "Карточки с фото, ценами, датами, питанием, звёздами УЖЕ отображены фронтендом. "
                    "НЕ перечисляй отели, цены, описания, звёзды в тексте! "
                    "Напиши 3-4 коротких предложения: "
                    "1) Упомяни что цены за человека. "
                    "2) ОБЯЗАТЕЛЬНО добавь: «Горящие туры имеют фиксированные даты и длительность — "
                    "если нужны конкретные параметры, могу сделать обычный поиск.» "
                    "3) Спроси «Хотите подробнее о каком-то варианте?»"
                ),
                "_warning": _hot_warning,
                "_adults_only_warning": _adults_only_warning,
            }
        
        elif name == "continue_search":
            # ── P1: Валидация requestid ──
            _rid = str(args.get("requestid", ""))
            if not _rid.replace(" ", "").isdigit():
                if self._last_requestid:
                    args["requestid"] = self._last_requestid
                else:
                    return {"error": f"⛔ НЕВЕРНЫЙ requestid: '{_rid}'. Сначала вызови search_tours."}
            result = await self.tourvisor.continue_search(args["requestid"])
            page = result.get("page", "2")
            return {
                "page": page,
                "message": f"Продолжение поиска запущено (страница {page}). Вызови get_search_status для ожидания завершения, затем get_search_results."
            }
        
        else:
            return {"error": f"Неизвестная функция: {name}"}
    
    def _call_api_sync(self, stream: bool = False):
        """
        Синхронный вызов Completion API через прямой HTTP.
        Используется через asyncio.to_thread() для неблокирующего выполнения.
        
        ⚠️ Completion API НЕ поддерживает previous_response_id!
        Поэтому ВСЕГДА отправляем полную историю (full_history) + новые элементы из input_list.
        """
        if stream:
            raise NotImplementedError("Streaming через Completion API пока не поддерживается")
        
        # Строим полный список messages из full_history + свежие function results из input_list
        messages = []
        
        # 1. System message (instructions / промпт)
        if self.instructions:
            messages.append({"role": "system", "text": self.instructions})
        
        # 2. Полная история диалога (user/assistant)
        for item in self.full_history:
            role = item.get("role")
            content = item.get("content", "")
            if role == "user":
                messages.append({"role": "user", "text": content})
            elif role == "assistant":
                messages.append({"role": "assistant", "text": content})
        
        # 3. Свежие function results из input_list (они ещё не в full_history)
        for item in self.input_list:
            if item.get("type") == "function_call_output":
                output = item.get("output", "")
                call_id = item.get("call_id", "")
                # Function result отправляем как user-сообщение (модель лучше воспринимает)
                messages.append({
                    "role": "user",
                    "text": f"Результат вызова функции (call_id={call_id}):\n{output}\n\nТеперь проанализируй результат и ответь клиенту. Если нужно — вызови следующую функцию."
                })
        
        body = {
            "modelUri": self.model_uri,
            "completionOptions": {
                "stream": False,
                "temperature": 0.3,
                "maxTokens": 6000
            },
            "messages": messages
        }
        
        logger.debug("🌐 HTTP POST %s  messages=%d (history=%d + func_results)  body_size=%d",
                     self.completion_url, len(messages), len(self.full_history), len(json.dumps(body)))
        
        response = requests.post(
            self.completion_url,
            headers=self.headers,
            json=body,
            timeout=30
        )
        
        if response.status_code != 200:
            logger.error("🌐 HTTP ERROR %d: %s", response.status_code, response.text[:500])
            raise Exception(f"HTTP {response.status_code}: {response.text[:300]}")
        
        data = response.json()
        logger.debug("🌐 HTTP 200  response_size=%d", len(json.dumps(data)))
        
        # Извлекаем текст ответа и статус
        alternative = data.get("result", {}).get("alternatives", [{}])[0]
        text = alternative.get("message", {}).get("text", "")
        status = alternative.get("status", "")
        
        # Преобразуем в объект, похожий на Responses API
        class ResponseObject:
            def __init__(self, text_response, response_status=""):
                self.id = "completion_" + str(hash(text_response))[:16]
                self.output = []
                self.output_text = text_response
                self.status = response_status  # ALTERNATIVE_STATUS_CONTENT_FILTER etc.
                
                # Создаём message output
                msg_obj = type('obj', (object,), {
                    'type': 'message',
                    'content': [type('obj', (object,), {
                        'type': 'output_text',
                        'text': text_response
                    })()]
                })()
                self.output.append(msg_obj)
        
        return ResponseObject(text, status)
    
    async def _call_api(self, stream: bool = False):
        """
        Асинхронный вызов API через to_thread().
        Не блокирует event loop!
        """
        return await asyncio.to_thread(self._call_api_sync, stream)
    
    async def chat(self, user_message: str) -> str:
        """
        Отправить сообщение и получить ответ.
        Обрабатывает Function Calling автоматически (Responses API).
        
        ⚡ Двойной режим:
        - Основной: previous_response_id + только новые items в input
        - Fallback: full_history (при ошибках/пустых ответах)
        """
        # Сбрасываем tour_cards перед каждым новым сообщением
        self._pending_tour_cards = []
        
        # Инкрементируем счётчик сообщений
        self._metrics["total_messages"] += 1
        
        user_item = {"role": "user", "content": user_message}
        
        # Добавляем в полную историю и обрезаем если нужно
        self.full_history.append(user_item)
        self._trim_history()
        
        # input_list = только новое сообщение (контекст в previous_response_id)
        self.input_list = [user_item]
        
        logger.info("👤 USER >> \"%s\"  prev_response=%s  full_history=%d",
                     user_message[:150], self.previous_response_id or "none", len(self.full_history))
        
        max_iterations = 15
        iteration = 0
        chat_start = time.perf_counter()
        empty_retries = 0
        
        while iteration < max_iterations:
            iteration += 1
            logger.info("🔄 ITERATION %d/%d  (non-streaming)  input_items=%d  prev_id=%s",
                        iteration, max_iterations, len(self.input_list),
                        self.previous_response_id[:16] + "…" if self.previous_response_id else "none")
            
            try:
                t0 = time.perf_counter()
                response = await self._call_api(stream=False)
                api_ms = int((time.perf_counter() - t0) * 1000)
                
                output_types = [getattr(item, 'type', '?') for item in response.output]
                logger.info("🤖 YANDEX API << response_id=%s  %dms  output_items=%s  types=%s",
                            response.id, api_ms, len(response.output), output_types)
                
                # ⚡ Сохраняем ID ТОЛЬКО если ответ не пустой
                if len(response.output) > 0:
                    self.previous_response_id = response.id
                else:
                    logger.warning("⚠️ NOT saving response_id %s (empty output — would become 'failed')",
                                   response.id)
                
            except Exception as e:
                api_ms = int((time.perf_counter() - t0) * 1000)
                error_str = str(e)
                logger.error("🤖 YANDEX API !! ERROR  %dms  %s", api_ms, error_str[:300])
                
                if "403" in error_str or "Forbidden" in error_str:
                    logger.warning("⚠️ 403 Forbidden — content moderation or permissions issue")
                    self.previous_response_id = None
                    # Пробуем fallback через full_history
                    if empty_retries < 2:
                        empty_retries += 1
                        self.input_list = list(self.full_history) + [
                            {"role": "user", "content": "Пожалуйста, продолжи помогать с подбором тура."}
                        ]
                        continue
                    return "Извините, произошла техническая ошибка. Попробуйте переформулировать запрос или начните новый чат."
                
                if "429" in error_str or "Too Many" in error_str:
                    return "Сервис временно перегружен. Подождите несколько секунд и повторите."
                
                # Если previous response failed → fallback к full_history
                if "status failed" in error_str:
                    logger.warning("🔄 FALLBACK to full_history (%d items) after 'status failed'",
                                   len(self.full_history))
                    self.previous_response_id = None
                    self.input_list = list(self.full_history)
                    continue
                
                # Fix R9: HTTP 400 — token limit exceeded (32768 tokens)
                # Обрезаем историю: сохраняем первые 2 сообщения (system context) + последние 4
                if "400" in error_str and ("32768" in error_str or "number of input tokens" in error_str or "token" in error_str.lower()):
                    logger.warning(
                        "⚠️ TOKEN LIMIT EXCEEDED — trimming history from %d messages",
                        len(self.full_history)
                    )
                    if len(self.full_history) > 8:
                        trimmed_start = self.full_history[:2]  # первое сообщение пользователя + первый ответ
                        trimmed_end = self.full_history[-4:]    # последние 4 сообщения (актуальный контекст)
                        self.full_history = trimmed_start + trimmed_end
                        logger.info(
                            "✅ History trimmed to %d messages (2 start + 4 end)",
                            len(self.full_history)
                        )
                    self.previous_response_id = None
                    self.input_list = list(self.full_history)
                    if empty_retries < 2:
                        empty_retries += 1
                        continue
                    return "Извините, диалог стал слишком длинным. Пожалуйста, начните новый чат или кратко повторите ваш запрос."
                
                self.previous_response_id = None
                return "Произошла временная ошибка. Попробуйте ещё раз или начните новый чат."
            
            # Проверяем function calls
            has_function_calls = False
            function_results = []
            
            for item in response.output:
                if getattr(item, 'type', None) == "function_call":
                    has_function_calls = True
                    func_name = getattr(item, 'name', '')
                    func_args = getattr(item, 'arguments', '{}')
                    call_id = getattr(item, 'call_id', func_name)
                    result = await self._execute_function(func_name, func_args, call_id)
                    function_results.append(result)
            
            if has_function_calls:
                # Собираем summary функций для full_history
                func_summary_parts = []
                func_names = []
                for result in function_results:
                    call_id = result.get("call_id", "")
                    output = result.get("output", "")
                    for item in response.output:
                        if getattr(item, 'call_id', '') == call_id:
                            func_name = getattr(item, 'name', '?')
                            func_names.append(func_name)
                            limit = 2000 if func_name in ('get_search_results', 'get_hotel_info', 'get_hot_tours') else 1000
                            func_summary_parts.append(f"[{func_name}]: {output[:limit]}")
                            break
                
                # Сохраняем в full_history: assistant вызвал функции + user/результаты
                self._append_history("assistant", f"Вызываю функции: {', '.join(func_names)}")
                if func_summary_parts:
                    self._append_history("user", "Результаты вызванных функций:\n" + "\n".join(func_summary_parts) + "\n\nТеперь проанализируй результаты и ответь клиенту. Если нужно — вызови следующую функцию.")
                
                # input_list пуст — всё в full_history
                self.input_list = []
                logger.info("🔄 FUNC CALLS DONE  count=%d  continuing…", len(function_results))
            else:
                # Текстовый ответ
                final_text = getattr(response, 'output_text', '')
                
                if not final_text:
                    for item in response.output:
                        if getattr(item, 'type', None) == "message":
                            for c in getattr(item, 'content', []):
                                if getattr(c, 'type', None) == "output_text":
                                    final_text = getattr(c, 'text', '')
                                    break
                
                # ⚡ Пустой ответ → fallback к full_history + nudge
                if not final_text and len(response.output) == 0:
                    empty_retries += 1
                    logger.warning("⚠️ EMPTY RESPONSE #%d — falling back to full_history (%d items)",
                                   empty_retries, len(self.full_history))
                    if empty_retries >= 3:
                        logger.error("⚠️ GIVING UP after %d empty responses", empty_retries)
                        # ── P3: Если карточки уже есть — позитивный fallback вместо "Извините" ──
                        if self._pending_tour_cards:
                            return "Вот что нашёл по вашему запросу! Посмотрите варианты и скажите, какой заинтересовал — расскажу подробнее."
                        return "Извините, не удалось обработать запрос. Попробуйте переформулировать."
                    # Fallback: пересылаем всю историю + nudge сообщение
                    self.previous_response_id = None
                    nudge = {"role": "user", "content": "Продолжи обработку моего запроса на основе полученных данных."}
                    self.input_list = list(self.full_history) + [nudge]
                    continue
                
                # ⚡ Детект контент-фильтра Yandex API (ALTERNATIVE_STATUS_CONTENT_FILTER)
                # Фильтр срабатывает на коротких сообщениях при большом системном промпте.
                # Решение: вставляем приветствие ассистента перед первым сообщением пользователя.
                # Это даёт модели контекст «туристический чат» и фильтр пропускает запрос.
                is_content_filter = getattr(response, 'status', '') == 'ALTERNATIVE_STATUS_CONTENT_FILTER'
                is_self_mod = final_text and _is_self_moderation(final_text)
                
                if is_content_filter or is_self_mod:
                    empty_retries += 1
                    reason = "CONTENT_FILTER" if is_content_filter else "SELF-MODERATION"
                    logger.warning("⚠️ %s detected (#%d): \"%s\"", reason, empty_retries, (final_text or '')[:100])
                    
                    if empty_retries >= 3:
                        return "Извините, произошла ошибка. Попробуйте переформулировать запрос или начните новый чат."
                    
                    # Стратегия: вставляем контекстное приветствие ассистента ПЕРЕД первым
                    # сообщением пользователя. _call_api_sync строит messages из full_history.
                    # Поле _cf_greeting=True маркирует вставку для очистки после успеха.
                    _CF_GREETING = "Здравствуйте! Я помогу вам подобрать тур. Куда хотите поехать?"
                    
                    # Проверяем, не вставляли ли уже
                    has_greeting = any(item.get("_cf_greeting") for item in self.full_history)
                    
                    if not has_greeting:
                        # Ищем первое пользовательское сообщение и вставляем приветствие ПЕРЕД ним
                        for i, item in enumerate(self.full_history):
                            if item.get("role") == "user":
                                self.full_history.insert(i, {
                                    "role": "assistant",
                                    "content": _CF_GREETING,
                                    "_cf_greeting": True  # маркер для cleanup, не уходит в API
                                })
                                logger.info("🔄 CONTENT-FILTER BYPASS: inserted assistant greeting before user message")
                                break
                    
                    self.previous_response_id = None
                    self.input_list = []
                    continue
                
                # ⚡ Детект «обещанного, но не выполненного поиска»
                # Модель написала «сейчас поищу», но НЕ вызвала search_tours
                if final_text and _is_promised_search(final_text):
                    empty_retries += 1
                    self._metrics["promised_search_detections"] += 1
                    logger.warning("⚠️ PROMISED-SEARCH detected (#%d): \"%s\" — nudging model to call function",
                                   empty_retries, final_text[:150])
                    if empty_retries >= 2:
                        # Не зацикливаемся — отдаём текст как есть после 2 попыток
                        logger.warning("⚠️ PROMISED-SEARCH: giving up after %d retries, returning text", empty_retries)
                    else:
                        # Nudge: говорим модели ВЫПОЛНИТЬ поиск, а не описывать намерение
                        self.input_list = [
                            {
                                "type": "function_call_output",
                                "call_id": "_nudge_search",
                                "output": json.dumps({
                                    "error": "СИСТЕМНАЯ ОШИБКА: Ты ОПИСАЛ намерение поиска текстом, но НЕ вызвал функцию. "
                                             "НЕМЕДЛЕННО вызови get_current_date(), затем search_tours() с собранными параметрами. "
                                             "НИКОГДА не пиши 'сейчас поищу' — ВЫЗЫВАЙ функцию!"
                                }, ensure_ascii=False)
                            }
                        ]
                        continue
                
                # ── P7: JSON wrapper {"role":"assistant","message":"..."} → извлечь текст ──
                if final_text:
                    json_wrapper_text = _extract_json_wrapper_message(final_text)
                    if json_wrapper_text:
                        final_text = json_wrapper_text
                
                # ⚡ FIX B3 + P7: Safety-net для plaintext tool calls (yandexgpt/rc quirk)
                # Модель иногда возвращает function calls как текст вместо structured call
                plaintext_calls = _extract_plaintext_tool_calls(final_text) if final_text else []
                if plaintext_calls:
                    logger.warning("⚠️ PLAINTEXT-TOOL-CALL: found %d call(s) in text, executing as safety-net", len(plaintext_calls))
                    self._metrics.setdefault("plaintext_tool_call_recoveries", 0)
                    self._metrics["plaintext_tool_call_recoveries"] += 1
                    
                    pt_results = []
                    pt_summary_parts = []
                    for pt_name, pt_args_json in plaintext_calls:
                        pt_call_id = f"_plaintext_{pt_name}_{iteration}"
                        pt_result = await self._execute_function(pt_name, pt_args_json, pt_call_id)
                        pt_results.append(pt_result)
                        output = pt_result.get("output", "")
                        # Fix P12: Не добавлять ошибки KeyError/Timeout в summary — они путают модель
                        if '"KeyError"' in output or 'ReadTimeout' in output or '"Traceback' in output:
                            logger.warning("⚠️ SKIPPING error result from %s in summary", pt_name)
                            continue
                        limit = 2000 if pt_name in ('get_search_results', 'get_hotel_info', 'get_hot_tours') else 1000
                        pt_summary_parts.append(f"[{pt_name}]: {output[:limit]}")
                    
                    # Сохраняем вызов функции и результат в full_history
                    # (Completion API не имеет previous_response_id — нужен полный контекст)
                    called_funcs = ", ".join(f"{n}({a[:80]})" for n, a in plaintext_calls)
                    self._append_history("assistant", f"Вызываю функции: {called_funcs}")
                    if pt_summary_parts:
                        self._append_history("user", "Результаты вызванных функций:\n" + "\n".join(pt_summary_parts) + "\n\nТеперь проанализируй результаты и ответь клиенту. Если нужно — вызови следующую функцию.")
                    
                    # input_list пуст — всё уже в full_history
                    self.input_list = []
                    continue  # Продолжаем цикл — модель обработает результаты
                
                # ── Fix R6: Sanitize rejected plaintext function calls ──
                # Если модель вернула текст с вызовами несуществующих функций (например web_search),
                # plaintext_calls будет пуст, но текст содержит "Вызываю функции: web_search(...)"
                # Нельзя показывать такой текст клиенту!
                if final_text and not plaintext_calls:
                    _rejected_call_pattern = re.search(
                        r'(?:вызываю\s+функци[юи]|calling\s+function|tool_call).*?\w+\s*\(',
                        final_text, re.IGNORECASE
                    )
                    if _rejected_call_pattern:
                        logger.warning(
                            "⚠️ REJECTED-TOOL-CALL in text: model tried unknown function — nudging. Text: %s",
                            final_text[:200]
                        )
                        self._metrics.setdefault("rejected_tool_calls_sanitized", 0)
                        self._metrics["rejected_tool_calls_sanitized"] += 1
                        # Добавляем подсказку модели использовать только доступные функции
                        self.full_history.append({"role": "assistant", "content": final_text})
                        self.input_list = [{
                            "role": "user",
                            "content": (
                                "Эта функция недоступна. Используй ТОЛЬКО доступные функции: "
                                "get_current_date, search_tours, get_search_status, get_search_results, "
                                "get_dictionaries, get_hotel_info, get_hot_tours, actualize_tour, get_tour_details. "
                                "Если нужная информация недоступна через функции — скажи клиенту об этом вежливо "
                                "и предложи альтернативу."
                            )
                        }]
                        empty_retries += 1
                        if empty_retries < 3:
                            continue
                        # Если 3 попытки — отвечаем вежливо
                        final_text = "К сожалению, я не могу найти эту информацию в данный момент. Могу помочь с подбором туров, информацией об отелях или горящими предложениями. Чем ещё могу помочь?"
                
                # ── P6: Фильтрация "Результаты запросов:" из финального текста ──
                # Модель иногда эхо-повторяет записи из full_history
                if final_text and final_text.lstrip().startswith("Результаты запросов"):
                    logger.warning("⚠️ RESULT-LEAK detected: model echoed raw results → nudging")
                    self._metrics.setdefault("result_leak_filtered", 0)
                    self._metrics["result_leak_filtered"] += 1
                    if self._pending_tour_cards:
                        final_text = "Вот что нашёл по вашему запросу! Посмотрите варианты и скажите, какой заинтересовал — расскажу подробнее."
                    else:
                        # Nudge модель — не показываем сырые данные
                        empty_retries += 1
                        if empty_retries < 3:
                            self.full_history.append({"role": "assistant", "content": final_text})
                            self.input_list = [{"role": "user", "content": "Ответь клиенту нормальным текстом — НЕ показывай сырые данные функций. Если нужно вызвать ещё функцию — вызови."}]
                            continue
                        else:
                            final_text = "Я обработал ваш запрос. Чем могу помочь?"
                
                # Fix C3: Удаляем утёкшие вызовы функций из текста ответа
                # Модель иногда включает search_tours(...) или get_*(...) в текст
                _leaked_func_rx = re.compile(
                    r'(?:search_tours|get_(?:current_date|search_status|search_results|hotel_info|hot_tours|tour_details|dictionaries)|actualize_tour|continue_search)\s*\([^)]*\)',
                    re.DOTALL
                )
                if _leaked_func_rx.search(final_text):
                    _cleaned = _leaked_func_rx.sub('', final_text).strip()
                    if _cleaned and len(_cleaned) > 20:
                        logger.warning("⚠️ STRIPPED-LEAKED-FUNC from response: '%s' → '%s'",
                                       final_text[:200], _cleaned[:200])
                        final_text = _cleaned
                    # Если после удаления текст пустой — оставляем оригинал,
                    # так как plaintext extractor уже должен был обработать вызовы
                
                # Дедупликация ответа (Yandex GPT quirk)
                final_text = _dedup_response(final_text)
                
                # Strip leaked LLM reasoning / JSON fragments from end of response
                final_text = _strip_reasoning_leak(final_text)

                # Sentence-level dedup (catches intra-paragraph question repeats)
                final_text = _dedup_sentences(final_text)

                # Strip orphaned dialogue-continuation fragments after last '?'
                final_text = _strip_trailing_fragment(final_text)
                
                # Успешный ответ — сохраняем в историю
                self.full_history.append({"role": "assistant", "content": final_text})
                self.input_list = []
                
                # Очистка контент-фильтр приветствия из full_history (если было добавлено при retry)
                self.full_history = [
                    item for item in self.full_history
                    if not item.get("_cf_greeting")
                ]
                
                total_ms = int((time.perf_counter() - chat_start) * 1000)
                logger.info("🤖 ASSISTANT << %d chars  %d iterations  %dms total  \"%s\"",
                            len(final_text), iteration, total_ms,
                            final_text[:200] + ("…" if len(final_text) > 200 else ""))
                return final_text
        
        logger.error("🤖 MAX ITERATIONS REACHED (%d)", max_iterations)
        return "Ошибка: превышено количество итераций Function Calling"
    
    async def chat_stream(
        self, 
        user_message: str, 
        on_token: Optional[StreamCallback] = None
    ) -> str:
        """
        Отправить сообщение и получить ответ со STREAMING.
        Текст появляется по частям — как в ChatGPT.
        
        ⚠️ ВРЕМЕННО ОТКЛЮЧЕНО: Streaming не поддерживается через прямой HTTP к Responses API.
        Используется обычный chat() вместо этого.
        
        Args:
            user_message: Сообщение пользователя
            on_token: Callback функция, вызывается при получении каждого токена.
                      Пример: on_token=lambda text: print(text, end="", flush=True)
        
        Returns:
            Полный текст ответа
        
        Пример использования:
            # Простой вывод в консоль
            response = await handler.chat_stream(
                "Привет!",
                on_token=lambda t: print(t, end="", flush=True)
            )
            
            # Для веб-приложения (WebSocket/SSE)
            async def send_to_client(text):
                await websocket.send(text)
            
            response = await handler.chat_stream("Привет!", on_token=send_to_client)
        """
        # ⚠️ ВРЕМЕННЫЙ FALLBACK: используем обычный chat() вместо streaming
        logger.warning("⚠️ chat_stream() fallback to chat() — streaming не поддерживается через прямой HTTP")
        result = await self.chat(user_message)
        if on_token:
            on_token(result)
        return result
    
    async def _chat_stream_old(self, user_message: str, on_token: Optional[StreamCallback] = None) -> str:
        """Старый streaming код (не используется)"""
        # Сбрасываем tour_cards перед каждым новым сообщением
        self._pending_tour_cards = []
        
        user_item = {"role": "user", "content": user_message}
        
        # Добавляем в полную историю и обрезаем если нужно
        self.full_history.append(user_item)
        self._trim_history()
        
        # input_list = только новое сообщение (контекст в previous_response_id)
        self.input_list = [user_item]
        
        logger.info("👤 USER >> (stream) \"%s\"  prev_response=%s  full_history=%d",
                     user_message[:150], self.previous_response_id or "none", len(self.full_history))
        
        # Сбрасываем счётчик пустых итераций
        self._empty_iterations = 0
        
        # Цикл Function Calling со streaming
        max_iterations = 15
        iteration = 0
        chat_start = time.perf_counter()
        
        while iteration < max_iterations:
            iteration += 1
            logger.info("🔄 ITERATION %d/%d  (streaming)", iteration, max_iterations)
            
            try:
                # Вызываем API со streaming
                t0 = time.perf_counter()
                stream_response = await asyncio.to_thread(
                    lambda: self.client.responses.create(
                        model=self.model_uri,
                        input=self.input_list,
                        instructions=self.instructions,
                        tools=self.tools,
                        temperature=0.3,
                        max_output_tokens=4000,
                        previous_response_id=self.previous_response_id,
                        stream=True
                    )
                )
                api_ms = int((time.perf_counter() - t0) * 1000)
                logger.debug("🤖 YANDEX STREAM API << stream created in %dms", api_ms)
                
            except Exception as e:
                api_ms = int((time.perf_counter() - t0) * 1000)
                error_str = str(e)
                logger.error("🤖 YANDEX STREAM API !! ERROR  %dms  %s", api_ms, error_str[:300])
                
                # 403 Forbidden — content moderation или проблема с правами
                if "403" in error_str or "Forbidden" in error_str:
                    logger.warning("⚠️ STREAM 403 Forbidden — content moderation, retrying with full_history")
                    self.previous_response_id = None
                    self._empty_iterations += 1
                    if self._empty_iterations < 3:
                        self.input_list = list(self.full_history) + [
                            {"role": "user", "content": "Пожалуйста, продолжи помогать с подбором тура."}
                        ]
                        continue
                    return "Извините, произошла техническая ошибка. Попробуйте переформулировать запрос или начните новый чат."
                
                # 429 Too Many Requests — rate limiting
                if "429" in error_str or "Too Many" in error_str:
                    return "Сервис временно перегружен. Подождите несколько секунд и повторите."
                
                # Если response ещё in_progress — подождать и попробовать снова
                if "in_progress" in error_str:
                    logger.warning("🤖 YANDEX API !! prev response in_progress, waiting 2s…")
                    await asyncio.sleep(2)
                    continue
                
                # Если previous response failed → fallback к full_history
                if "status failed" in error_str:
                    logger.warning("🔄 STREAM FALLBACK to full_history (%d items) after 'status failed'",
                                   len(self.full_history))
                    self.previous_response_id = None
                    self.input_list = list(self.full_history)
                    continue
                
                # Fix R9: HTTP 400 — token limit exceeded (32768 tokens)
                if "400" in error_str and ("32768" in error_str or "number of input tokens" in error_str or "token" in error_str.lower()):
                    logger.warning(
                        "⚠️ STREAM TOKEN LIMIT EXCEEDED — trimming history from %d messages",
                        len(self.full_history)
                    )
                    if len(self.full_history) > 8:
                        self.full_history = self.full_history[:2] + self.full_history[-4:]
                        logger.info("✅ History trimmed to %d messages", len(self.full_history))
                    self.previous_response_id = None
                    self.input_list = list(self.full_history)
                    self._empty_iterations += 1
                    if self._empty_iterations < 3:
                        continue
                    return "Извините, диалог стал слишком длинным. Пожалуйста, начните новый чат или кратко повторите ваш запрос."
                
                self.previous_response_id = None
                return "Произошла временная ошибка связи. Попробуйте ещё раз или начните новый чат."
            
            # Обрабатываем streaming ответ
            full_text = ""
            has_function_calls = False
            function_calls_data = []
            output_items = []  # Собираем все output items
            response_id = None
            token_count = 0
            
            # Итерируем по событиям streaming
            for event in stream_response:
                event_type = getattr(event, 'type', None)
                
                # Сохраняем response_id
                if hasattr(event, 'response') and event.response:
                    response_id = getattr(event.response, 'id', None)
                
                # Текстовый контент (delta)
                if event_type == "response.output_text.delta":
                    delta_text = getattr(event, 'delta', '')
                    if delta_text:
                        full_text += delta_text
                        token_count += 1
                        # Вызываем callback для каждого токена
                        if on_token:
                            on_token(delta_text)
                
                # Output item - собираем все items (function_call, message, web_search, etc)
                elif event_type == "response.output_item.done":
                    event_data = event.model_dump() if hasattr(event, 'model_dump') else {}
                    item = event_data.get('item', {})
                    item_type = item.get('type', '')
                    
                    # Сохраняем item для истории
                    output_items.append(item)
                    logger.debug("📦 STREAM ITEM  type=%s", item_type)
                    
                    if item_type == 'function_call':
                        has_function_calls = True
                        fc_data = {
                            "name": item.get('name', ''),
                            "arguments": item.get('arguments', '{}'),
                            "call_id": item.get('call_id', item.get('id', ''))
                        }
                        function_calls_data.append(fc_data)
                        logger.info("📦 STREAM >> function_call: %s(%s)", fc_data["name"], fc_data["arguments"][:200])
                    elif item_type in ('web_search_call', 'web_search_result'):
                        logger.info("🌍 STREAM >> %s", item_type)
                
                # Завершение ответа
                elif event_type == "response.done":
                    if hasattr(event, 'response'):
                        response_id = getattr(event.response, 'id', None)
            
            # ⚡ Сохраняем ID ТОЛЬКО если ответ не пустой
            if response_id and (output_items or full_text):
                self.previous_response_id = response_id
            elif response_id:
                logger.warning("⚠️ NOT saving stream response_id %s (empty output)", response_id)
            
            stream_ms = int((time.perf_counter() - t0) * 1000)
            item_types = [i.get('type', '?') if isinstance(i, dict) else getattr(i, 'type', '?') for i in output_items]
            logger.info("📡 STREAM DONE  response_id=%s  %dms  tokens=%d  text=%d chars  items=%s  func_calls=%d  types=%s",
                         response_id, stream_ms, token_count, len(full_text), len(output_items),
                         len(function_calls_data), item_types)
            
            if has_function_calls:
                # Сбрасываем счётчик пустых итераций
                self._empty_iterations = 0
                
                # Выполняем функции
                function_results = []
                for fc in function_calls_data:
                    result = await self._execute_function(
                        fc["name"], 
                        fc["arguments"], 
                        fc["call_id"]
                    )
                    function_results.append(result)
                
                # Собираем summary для full_history (fallback)
                # ⚡ Увеличен лимит — при 500 терялся контекст карточек
                func_summary_parts = []
                for i, result in enumerate(function_results):
                    fc = function_calls_data[i] if i < len(function_calls_data) else {}
                    output = result.get("output", "")
                    func_name = fc.get('name', '?')
                    limit = 2000 if func_name in ('get_search_results', 'get_hotel_info', 'get_hot_tours') else 1000
                    func_summary_parts.append(f"[{func_name}]: {output[:limit]}")
                
                if func_summary_parts:
                    self.full_history.append({
                        "role": "assistant",
                        "content": "Результаты запросов:\n" + "\n".join(func_summary_parts)
                    })
                
                # input_list = только function results (output_items в previous_response_id)
                self.input_list = function_results
                logger.info("🔄 FUNC CALLS DONE  count=%d  continuing loop…",
                            len(function_results))
            elif full_text:
                # ⚡ Детект контент-фильтра / самомодерации модели (stream)
                if _is_self_moderation(full_text):
                    self._empty_iterations += 1
                    logger.warning("⚠️ STREAM CONTENT-FILTER/SELF-MODERATION detected (#%d): \"%s\"",
                                   self._empty_iterations, full_text[:100])
                    if self._empty_iterations >= 3:
                        self._empty_iterations = 0
                        return "Извините, произошла ошибка. Попробуйте переформулировать запрос или начните новый чат."
                    # Стратегия: вставляем контекстное приветствие ассистента
                    _CF_GREETING = "Здравствуйте! Я помогу вам подобрать тур. Куда хотите поехать?"
                    has_greeting = any(item.get("_cf_greeting") for item in self.full_history)
                    if not has_greeting:
                        for i, item in enumerate(self.full_history):
                            if item.get("role") == "user":
                                self.full_history.insert(i, {
                                    "role": "assistant",
                                    "content": _CF_GREETING,
                                    "_cf_greeting": True
                                })
                                logger.info("🔄 STREAM CONTENT-FILTER BYPASS: inserted assistant greeting")
                                break
                    self.previous_response_id = None
                    self.input_list = []
                    continue
                
                # ⚡ Детект «обещанного, но не выполненного поиска» (stream)
                if _is_promised_search(full_text):
                    self._empty_iterations += 1
                    self._metrics["promised_search_detections"] += 1
                    logger.warning("⚠️ STREAM PROMISED-SEARCH detected (#%d): \"%s\" — nudging model",
                                   self._empty_iterations, full_text[:150])
                    if self._empty_iterations >= 2:
                        logger.warning("⚠️ STREAM PROMISED-SEARCH: giving up after %d retries", self._empty_iterations)
                    else:
                        self.input_list = [
                            {
                                "type": "function_call_output",
                                "call_id": "_nudge_search",
                                "output": json.dumps({
                                    "error": "СИСТЕМНАЯ ОШИБКА: Ты ОПИСАЛ намерение поиска текстом, но НЕ вызвал функцию. "
                                             "НЕМЕДЛЕННО вызови get_current_date(), затем search_tours() с собранными параметрами. "
                                             "НИКОГДА не пиши 'сейчас поищу' — ВЫЗЫВАЙ функцию!"
                                }, ensure_ascii=False)
                            }
                        ]
                        continue
                
                # ⚡ FIX B3: Safety-net для plaintext tool calls (yandexgpt/rc quirk) — stream
                plaintext_calls = _extract_plaintext_tool_calls(full_text)
                if plaintext_calls:
                    logger.warning("⚠️ STREAM PLAINTEXT-TOOL-CALL: found %d call(s), executing", len(plaintext_calls))
                    self._metrics.setdefault("plaintext_tool_call_recoveries", 0)
                    self._metrics["plaintext_tool_call_recoveries"] += 1
                    self._empty_iterations = 0
                    
                    pt_results = []
                    pt_summary_parts = []
                    for pt_name, pt_args_json in plaintext_calls:
                        pt_call_id = f"_plaintext_{pt_name}_{iteration}"
                        pt_result = await self._execute_function(pt_name, pt_args_json, pt_call_id)
                        pt_results.append(pt_result)
                        output = pt_result.get("output", "")
                        # Fix P12: Не добавлять ошибки KeyError/Timeout в summary
                        if '"KeyError"' in output or 'ReadTimeout' in output or '"Traceback' in output:
                            logger.warning("⚠️ SKIPPING error result from %s in summary (stream)", pt_name)
                            continue
                        limit = 2000 if pt_name in ('get_search_results', 'get_hotel_info', 'get_hot_tours') else 1000
                        pt_summary_parts.append(f"[{pt_name}]: {output[:limit]}")
                    
                    # Fix P10: Согласование с non-streaming путём —
                    # assistant + user roles, chaining instruction, clear input_list
                    called_funcs = ", ".join(f"{n}({a[:80]})" for n, a in plaintext_calls)
                    self._append_history("assistant", f"Вызываю функции: {called_funcs}")
                    if pt_summary_parts:
                        self._append_history("user", "Результаты вызванных функций:\n" + "\n".join(pt_summary_parts) + "\n\nТеперь проанализируй результаты и ответь клиенту. Если нужно — вызови следующую функцию.")
                    
                    self.input_list = []
                    continue  # Продолжаем — модель обработает результаты
                
                # ── Fix R6 (stream): Sanitize rejected plaintext function calls ──
                if full_text and not plaintext_calls:
                    _rejected_call_pattern = re.search(
                        r'(?:вызываю\s+функци[юи]|calling\s+function|tool_call).*?\w+\s*\(',
                        full_text, re.IGNORECASE
                    )
                    if _rejected_call_pattern:
                        logger.warning(
                            "⚠️ STREAM REJECTED-TOOL-CALL: model tried unknown function — nudging. Text: %s",
                            full_text[:200]
                        )
                        self.full_history.append({"role": "assistant", "content": full_text})
                        self.input_list = [{
                            "role": "user",
                            "content": (
                                "Эта функция недоступна. Используй ТОЛЬКО доступные функции: "
                                "get_current_date, search_tours, get_search_status, get_search_results, "
                                "get_dictionaries, get_hotel_info, get_hot_tours, actualize_tour, get_tour_details. "
                                "Если нужная информация недоступна через функции — скажи клиенту об этом вежливо "
                                "и предложи альтернативу."
                            )
                        }]
                        self._empty_iterations += 1
                        if self._empty_iterations < 3:
                            continue
                        full_text = "К сожалению, я не могу найти эту информацию в данный момент. Могу помочь с подбором туров, информацией об отелях или горящими предложениями. Чем ещё могу помочь?"
                
                # Сбрасываем счётчик
                self._empty_iterations = 0
                
                # Дедупликация (Yandex GPT quirk)
                full_text = _dedup_response(full_text)
                
                # Сохраняем в full_history и чистим input_list
                self.full_history.append({"role": "assistant", "content": full_text})
                self.input_list = []
                
                # Очистка контент-фильтр приветствия из full_history (если было добавлено при retry)
                self.full_history = [
                    item for item in self.full_history
                    if not item.get("_cf_greeting")
                ]
                
                total_ms = int((time.perf_counter() - chat_start) * 1000)
                logger.info("🤖 ASSISTANT << (stream) %d chars  %d tokens  %d iterations  %dms total  \"%s\"",
                            len(full_text), token_count, iteration, total_ms,
                            full_text[:200] + ("…" if len(full_text) > 200 else ""))
                return full_text
            elif output_items:
                # Есть output_items (web_search, etc) но нет текста — продолжаем цикл
                has_text_message = any(
                    item.get('type') == 'message' and item.get('content')
                    for item in output_items
                )
                
                if has_text_message:
                    for item in output_items:
                        if item.get('type') == 'message':
                            content = item.get('content', [])
                            if isinstance(content, list):
                                for c in content:
                                    if c.get('type') == 'output_text':
                                        text = c.get('text', '')
                                        if text:
                                            self._empty_iterations = 0
                                            self.full_history.append({"role": "assistant", "content": text})
                                            self.input_list = []
                                            total_ms = int((time.perf_counter() - chat_start) * 1000)
                                            logger.info("🤖 ASSISTANT << (stream/msg) %d chars  %d iterations  %dms total  \"%s\"",
                                                        len(text), iteration, total_ms, text[:200] + ("…" if len(text) > 200 else ""))
                                            return text
                
                # Нет текста — проверяем что это за items
                has_web_search_call = any(
                    item.get('type') == 'web_search_call' 
                    for item in output_items
                )
                
                if has_web_search_call:
                    logger.info("🌍 WEB_SEARCH in progress, waiting 1s…")
                    await asyncio.sleep(1)
                else:
                    self._empty_iterations = 0
                    # output_items already tracked via previous_response_id
                    logger.info("📦 %d output_items tracked via prev_response_id (no text yet), continuing…", len(output_items))
            else:
                # Совсем пустой ответ
                self._empty_iterations += 1
                
                logger.warning("⚠️ EMPTY RESPONSE #%d (no text, no items, no func_calls) — fallback to full_history",
                               self._empty_iterations)
                
                # После 3 пустых итераций подряд — выходим
                if self._empty_iterations >= 3:
                    logger.error("⚠️ GIVING UP after %d empty responses", self._empty_iterations)
                    self._empty_iterations = 0
                    # ── P3: Если карточки уже есть — позитивный fallback ──
                    if self._pending_tour_cards:
                        return "Вот что нашёл по вашему запросу! Посмотрите варианты и скажите, какой заинтересовал — расскажу подробнее."
                    return "(Не удалось получить ответ. Попробуйте переформулировать вопрос.)"
                
                # Fallback: пересылаем всю историю + nudge без previous_response_id
                self.previous_response_id = None
                nudge = {"role": "user", "content": "Продолжи обработку моего запроса на основе полученных данных."}
                self.input_list = list(self.full_history) + [nudge]
        
        logger.error("🤖 MAX ITERATIONS REACHED (%d)", max_iterations)
        return "Ошибка: превышено количество итераций Function Calling"
    
    async def chat_stream_generator(self, user_message: str) -> AsyncIterator[str]:
        """
        Генератор для streaming ответа.
        Удобен для использования с async for.
        
        Пример:
            async for token in handler.chat_stream_generator("Привет!"):
                print(token, end="", flush=True)
        """
        # Очередь для передачи токенов из callback в генератор
        queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
        full_response = ""
        
        async def token_callback(token: str):
            await queue.put(token)
        
        # Запускаем chat_stream в фоне
        async def run_chat():
            nonlocal full_response
            try:
                # Для streaming используем синхронный callback
                # так как on_token не async
                tokens = []
                
                def sync_callback(token: str):
                    tokens.append(token)
                    # Синхронно добавляем в очередь через call_soon_threadsafe
                    asyncio.get_event_loop().call_soon_threadsafe(
                        lambda: queue.put_nowait(token)
                    )
                
                full_response = await self.chat_stream(user_message, on_token=sync_callback)
            finally:
                await queue.put(None)  # Сигнал завершения
        
        # Запускаем задачу
        task = asyncio.create_task(run_chat())
        
        # Читаем токены из очереди
        while True:
            token = await queue.get()
            if token is None:
                break
            yield token
        
        # Ждём завершения задачи
        await task
    
    async def close(self):
        """Закрыть соединения (async)"""
        await self.tourvisor.close()
        try:
            self.client.close()
        except Exception:
            pass

    def close_sync(self):
        """Синхронное закрытие ресурсов — используется при очистке сессий из Flask."""
        try:
            self.client.close()
        except Exception:
            pass
    
    def reset(self):
        """Сбросить историю диалога"""
        old_len = len(self.full_history)
        self.input_list = []
        self.full_history = []
        self.previous_response_id = None
        self._empty_iterations = 0
        self._pending_tour_cards = []
        self._last_departure_city = "Москва"
        logger.info("🔄 HANDLER RESET  cleared %d messages from full_history", old_len)


# ==================== ТЕСТ ====================

async def test_scenario_1():
    """Сценарий 1: Простой поиск тура (ГОТОВО)"""
    print("=" * 60)
    print("СЦЕНАРИЙ 1: Простой поиск тура")
    print("=" * 60)
    
    handler = YandexGPTHandler()
    try:
        response = await handler.chat(
            "Привет! Хотим с женой слетать в Турцию в марте, бюджет около 150 тысяч рублей. Вылет из Москвы."
        )
        print("\n✅ РЕЗУЛЬТАТ:\n" + response)
    finally:
        await handler.close()


async def test_scenario_2():
    """Сценарий 2: Горящие туры (ГОТОВО)"""
    print("=" * 60)
    print("СЦЕНАРИЙ 2: Горящие туры")
    print("=" * 60)
    
    handler = YandexGPTHandler()
    try:
        response = await handler.chat(
            "Покажи горящие туры из Москвы, желательно на море, 4-5 звёзд"
        )
        print("\n✅ РЕЗУЛЬТАТ:\n" + response)
    finally:
        await handler.close()


async def test_scenario_3():
    """Сценарий 3: Поиск с детьми + фильтры (питание, услуги)"""
    print("=" * 60)
    print("СЦЕНАРИЙ 3: Поиск с детьми + фильтры")
    print("=" * 60)
    
    handler = YandexGPTHandler()
    try:
        response = await handler.chat(
            "Хотим в Турцию из Москвы в марте, семья с ребёнком 5 лет. "
            "Обязательно всё включено, 4-5 звёзд. Бюджет до 200 тысяч."
        )
        print("\n✅ РЕЗУЛЬТАТ:\n" + response)
    finally:
        await handler.close()


async def test_scenario_4():
    """Сценарий 4: Справочники (города, страны)"""
    print("=" * 60)
    print("СЦЕНАРИЙ 4: Справочники")
    print("=" * 60)
    
    handler = YandexGPTHandler()
    try:
        response = await handler.chat(
            "Я из Казани. Куда можно полететь на море в марте? Какие страны доступны?"
        )
        print("\n✅ РЕЗУЛЬТАТ:\n" + response)
    finally:
        await handler.close()


async def test_scenario_5():
    """Сценарий 5: Подробная информация об отеле"""
    print("=" * 60)
    print("СЦЕНАРИЙ 5: Информация об отеле")
    print("=" * 60)
    
    handler = YandexGPTHandler()
    try:
        # Сначала поиск
        print("\n--- Поиск туров ---")
        await handler.chat("Найди туры в Турцию из Москвы в марте до 100 тысяч")
        
        # Потом подробности
        print("\n--- Запрос деталей ---")
        response = await handler.chat(
            "Расскажи подробнее про первый отель — что там есть, какой пляж, для детей"
        )
        print("\n✅ РЕЗУЛЬТАТ:\n" + response)
    finally:
        await handler.close()


async def test_scenario_6():
    """Сценарий 6: Актуализация цены и детали рейса"""
    print("=" * 60)
    print("СЦЕНАРИЙ 6: Актуализация + детали рейса")
    print("=" * 60)
    
    handler = YandexGPTHandler()
    try:
        # Сначала поиск
        print("\n--- Поиск туров ---")
        await handler.chat("Найди туры в Турцию из Москвы в марте до 100 тысяч")
        
        # Потом актуализация
        print("\n--- Запрос точной цены ---")
        response = await handler.chat(
            "Мне интересен первый вариант. Какая точная цена сейчас и какой рейс?"
        )
        print("\n✅ РЕЗУЛЬТАТ:\n" + response)
    finally:
        await handler.close()


async def test_scenario_7():
    """Сценарий 7: Продолжение поиска (ещё варианты)"""
    print("=" * 60)
    print("СЦЕНАРИЙ 7: Продолжение поиска")
    print("=" * 60)
    
    handler = YandexGPTHandler()
    try:
        # Сначала поиск
        print("\n--- Первый поиск ---")
        await handler.chat("Туры в Турцию из Москвы в марте до 150 тысяч")
        
        # Потом ещё
        print("\n--- Запрос ещё вариантов ---")
        response = await handler.chat("Покажи ещё варианты")
        print("\n✅ РЕЗУЛЬТАТ:\n" + response)
    finally:
        await handler.close()


async def test_scenario_8():
    """Сценарий 8: Веб-поиск (визы, погода) — теперь работает!"""
    print("=" * 60)
    print("СЦЕНАРИЙ 8: Вопросы про визы/погоду (web_search)")
    print("=" * 60)
    
    handler = YandexGPTHandler()
    try:
        response = await handler.chat(
            "Нужна ли виза в Египет для россиян? И какая погода там в феврале?"
        )
        print("\n✅ РЕЗУЛЬТАТ:\n" + response)
    finally:
        await handler.close()


async def test_scenario_9():
    """Сценарий 9: Поиск без результатов"""
    print("=" * 60)
    print("СЦЕНАРИЙ 9: Пустой результат поиска")
    print("=" * 60)
    
    handler = YandexGPTHandler()
    try:
        response = await handler.chat(
            "Найди тур на Мальдивы из Москвы на завтра, бюджет 50 тысяч, 5 звёзд, UAI"
        )
        print("\n✅ РЕЗУЛЬТАТ:\n" + response)
    finally:
        await handler.close()


async def test_scenario_10():
    """Сценарий 10: Полный диалог — от поиска до бронирования"""
    print("=" * 60)
    print("СЦЕНАРИЙ 10: Полный диалог")
    print("=" * 60)
    
    handler = YandexGPTHandler()
    try:
        print("\n--- Шаг 1: Начало диалога ---")
        await handler.chat("Привет! Хотим отдохнуть в Турции в марте, двое взрослых.")
        
        print("\n--- Шаг 2: Уточнение ---")
        await handler.chat("Бюджет около 100 тысяч, вылет из Москвы, 7-10 ночей, хотелось бы всё включено")
        
        print("\n--- Шаг 3: Выбор отеля ---")
        await handler.chat("Расскажи подробнее про второй вариант")
        
        print("\n--- Шаг 4: Бронирование ---")
        response = await handler.chat("Хотим забронировать этот тур. Какая точная цена?")
        
        print("\n✅ ФИНАЛЬНЫЙ РЕЗУЛЬТАТ:\n" + response)
    finally:
        await handler.close()


# ==================== НОВЫЕ ТЕСТЫ ДЛЯ ДОПОЛНИТЕЛЬНЫХ ПАРАМЕТРОВ ====================

async def test_scenario_11():
    """Сценарий 11: Тип отеля (hoteltypes) — только пляжные семейные"""
    print("=" * 60)
    print("СЦЕНАРИЙ 11: Фильтр по типу отеля (beach, family)")
    print("=" * 60)
    
    handler = YandexGPTHandler()
    try:
        response = await handler.chat(
            "Найди семейный пляжный отель в Турции из Москвы в марте. "
            "Важно чтобы отель был ориентирован на семьи с детьми и на пляже."
        )
        print("\n✅ РЕЗУЛЬТАТ:\n" + response)
    finally:
        await handler.close()


async def test_scenario_12():
    """Сценарий 12: Прямые рейсы (directflight)"""
    print("=" * 60)
    print("СЦЕНАРИЙ 12: Только прямые рейсы")
    print("=" * 60)
    
    handler = YandexGPTHandler()
    try:
        response = await handler.chat(
            "Хочу в Турцию из Москвы в марте, но обязательно прямой рейс без пересадок!"
        )
        print("\n✅ РЕЗУЛЬТАТ:\n" + response)
    finally:
        await handler.close()


async def test_scenario_13():
    """Сценарий 13: Фильтр по оператору"""
    print("=" * 60)
    print("СЦЕНАРИЙ 13: Конкретный туроператор")
    print("=" * 60)
    
    handler = YandexGPTHandler()
    try:
        response = await handler.chat(
            "Найди туры в Турцию из Москвы в марте, только от Anex Tour или Coral Travel."
        )
        print("\n✅ РЕЗУЛЬТАТ:\n" + response)
    finally:
        await handler.close()


async def test_scenario_14():
    """Сценарий 14: Конкретный отель"""
    print("=" * 60)
    print("СЦЕНАРИЙ 14: Поиск конкретного отеля")
    print("=" * 60)
    
    handler = YandexGPTHandler()
    try:
        response = await handler.chat(
            "Найди туры в отель Rixos в Турции из Москвы в марте."
        )
        print("\n✅ РЕЗУЛЬТАТ:\n" + response)
    finally:
        await handler.close()


async def test_scenario_15():
    """Сценарий 15: Только подтверждённые туры (onrequest=1)"""
    print("=" * 60)
    print("СЦЕНАРИЙ 15: Только подтверждённые туры (без 'под запрос')")
    print("=" * 60)
    
    handler = YandexGPTHandler()
    try:
        response = await handler.chat(
            "Найди туры в Турцию из Москвы в марте, "
            "но только те которые точно есть, без 'под запрос'."
        )
        print("\n✅ РЕЗУЛЬТАТ:\n" + response)
    finally:
        await handler.close()


async def test_scenario_16():
    """Сценарий 16: Бизнес-класс"""
    print("=" * 60)
    print("СЦЕНАРИЙ 16: Перелёт бизнес-классом")
    print("=" * 60)
    
    handler = YandexGPTHandler()
    try:
        response = await handler.chat(
            "Хочу в Турцию из Москвы в марте, перелёт бизнес-классом."
        )
        print("\n✅ РЕЗУЛЬТАТ:\n" + response)
    finally:
        await handler.close()


async def test_scenario_17():
    """Сценарий 17: Конкретный курорт (regions) — проверка правильных кодов"""
    print("=" * 60)
    print("СЦЕНАРИЙ 17: Конкретный курорт (Аланья)")
    print("=" * 60)
    
    handler = YandexGPTHandler()
    try:
        response = await handler.chat(
            "Найди туры в Аланью (Турция) из Москвы в марте."
        )
        print("\n✅ РЕЗУЛЬТАТ:\n" + response)
    finally:
        await handler.close()


async def test_scenario_18():
    """Сценарий 18: Получение текущей даты"""
    print("=" * 60)
    print("СЦЕНАРИЙ 18: Текущая дата")
    print("=" * 60)
    
    handler = YandexGPTHandler()
    try:
        response = await handler.chat(
            "Какая сейчас дата? Найди туры в Турцию на ближайшие выходные."
        )
        print("\n✅ РЕЗУЛЬТАТ:\n" + response)
    finally:
        await handler.close()


async def test_scenario_19():
    """Сценарий 19: Бизнес-класс перелёта"""
    print("=" * 60)
    print("СЦЕНАРИЙ 19: Бизнес-класс")
    print("=" * 60)
    
    handler = YandexGPTHandler()
    try:
        response = await handler.chat(
            "Найди тур в Турцию из Москвы в марте, перелёт бизнес-классом."
        )
        print("\n✅ РЕЗУЛЬТАТ:\n" + response)
    finally:
        await handler.close()


async def test_scenario_20():
    """Сценарий 20: Двое детей разного возраста"""
    print("=" * 60)
    print("СЦЕНАРИЙ 20: Двое детей")
    print("=" * 60)
    
    handler = YandexGPTHandler()
    try:
        response = await handler.chat(
            "Хотим в Турцию из Москвы в марте, двое взрослых и двое детей — 5 и 12 лет. Всё включено."
        )
        print("\n✅ РЕЗУЛЬТАТ:\n" + response)
    finally:
        await handler.close()


async def test_scenario_21():
    """Сценарий 21: Проверка visacharge — Египет"""
    print("=" * 60)
    print("СЦЕНАРИЙ 21: Визовые расходы (Египет)")
    print("=" * 60)
    
    handler = YandexGPTHandler()
    try:
        # Сначала поиск в Египет
        print("\n--- Поиск в Египет ---")
        await handler.chat("Найди тур в Египет из Москвы в марте, 4-5 звёзд")
        
        # Потом актуализация для проверки visacharge
        print("\n--- Актуализация для проверки визы ---")
        response = await handler.chat(
            "Какая точная цена первого варианта? И нужно ли доплачивать за визу?"
        )
        print("\n✅ РЕЗУЛЬТАТ:\n" + response)
    finally:
        await handler.close()


async def test_scenario_22():
    """Сценарий 22: Конкретный район курорта (subregions)"""
    print("=" * 60)
    print("СЦЕНАРИЙ 22: Подкурорт (subregions)")
    print("=" * 60)
    
    handler = YandexGPTHandler()
    try:
        response = await handler.chat(
            "Найди туры в Кемер, район Бельдиби, из Москвы в марте."
        )
        print("\n✅ РЕЗУЛЬТАТ:\n" + response)
    finally:
        await handler.close()


# ==================== ФИНАЛЬНЫЕ ТЕСТЫ ДЛЯ 100% ПОКРЫТИЯ ====================

async def test_scenario_23():
    """Сценарий 23: Трое детей (childage3)"""
    print("=" * 60)
    print("СЦЕНАРИЙ 23: Трое детей")
    print("=" * 60)
    
    handler = YandexGPTHandler()
    try:
        response = await handler.chat(
            "Хотим в Турцию из Москвы в марте, 2 взрослых и 3 детей — 3, 7 и 14 лет."
        )
        print("\n✅ РЕЗУЛЬТАТ:\n" + response)
    finally:
        await handler.close()


async def test_scenario_24():
    """Сценарий 24: Валюта (currency)"""
    print("=" * 60)
    print("СЦЕНАРИЙ 24: Цены в долларах")
    print("=" * 60)
    
    handler = YandexGPTHandler()
    try:
        response = await handler.chat(
            "Найди туры в Турцию из Москвы в марте. Цены покажи в долларах."
        )
        print("\n✅ РЕЗУЛЬТАТ:\n" + response)
    finally:
        await handler.close()


async def test_scenario_25():
    """Сценарий 25: 'А можно дешевле?'"""
    print("=" * 60)
    print("СЦЕНАРИЙ 25: Запрос на удешевление")
    print("=" * 60)
    
    handler = YandexGPTHandler()
    try:
        print("\n--- Первый поиск ---")
        await handler.chat("Туры в Турцию из Москвы в марте, 5 звёзд, UAI, бюджет 100 тысяч")
        
        print("\n--- Запрос дешевле ---")
        response = await handler.chat("Слишком дорого. А можно дешевле?")
        print("\n✅ РЕЗУЛЬТАТ:\n" + response)
    finally:
        await handler.close()


async def test_scenario_26():
    """Сценарий 26: Сравнить два отеля"""
    print("=" * 60)
    print("СЦЕНАРИЙ 26: Сравнение отелей")
    print("=" * 60)
    
    handler = YandexGPTHandler()
    try:
        print("\n--- Поиск ---")
        await handler.chat("Туры в Турцию из Москвы в марте до 150 тысяч")
        
        print("\n--- Сравнение ---")
        response = await handler.chat("Сравни первый и второй отель — какой лучше для семьи с детьми?")
        print("\n✅ РЕЗУЛЬТАТ:\n" + response)
    finally:
        await handler.close()


async def test_scenario_27():
    """Сценарий 27: Неизвестный город"""
    print("=" * 60)
    print("СЦЕНАРИЙ 27: Неизвестный город вылета")
    print("=" * 60)
    
    handler = YandexGPTHandler()
    try:
        response = await handler.chat(
            "Хочу в Турцию в марте из Владивостока"
        )
        print("\n✅ РЕЗУЛЬТАТ:\n" + response)
    finally:
        await handler.close()


async def test_scenario_28():
    """Сценарий 28: Диапазон дат > 14 дней"""
    print("=" * 60)
    print("СЦЕНАРИЙ 28: Большой диапазон дат")
    print("=" * 60)
    
    handler = YandexGPTHandler()
    try:
        response = await handler.chat(
            "Хочу в Турцию из Москвы в период с 1 марта по 30 апреля, гибкие даты."
        )
        print("\n✅ РЕЗУЛЬТАТ:\n" + response)
    finally:
        await handler.close()


async def test_scenario_29():
    """Сценарий 29: 6+ взрослых"""
    print("=" * 60)
    print("СЦЕНАРИЙ 29: Большая группа (7 взрослых)")
    print("=" * 60)
    
    handler = YandexGPTHandler()
    try:
        response = await handler.chat(
            "Хотим в Турцию из Москвы в марте, нас 7 человек взрослых."
        )
        print("\n✅ РЕЗУЛЬТАТ:\n" + response)
    finally:
        await handler.close()


async def test_scenario_30():
    """Сценарий 30: Ломаный русский"""
    print("=" * 60)
    print("СЦЕНАРИЙ 30: Ломаный русский")
    print("=" * 60)
    
    handler = YandexGPTHandler()
    try:
        response = await handler.chat(
            "хочу турция море дети март москва дешево"
        )
        print("\n✅ РЕЗУЛЬТАТ:\n" + response)
    finally:
        await handler.close()


async def test_scenario_31():
    """Сценарий 31: Стресс-тест — много требований"""
    print("=" * 60)
    print("СЦЕНАРИЙ 31: Стресс-тест (много требований)")
    print("=" * 60)
    
    handler = YandexGPTHandler()
    try:
        response = await handler.chat(
            "Хочу в Турцию из Москвы в марте, 2 взрослых и ребёнок 5 лет. "
            "Только 5 звёзд, UAI, первая линия, песчаный пляж, аквапарк, "
            "прямой рейс, без пересадок, бюджет до 200 тысяч, "
            "желательно Белек или Аланья."
        )
        print("\n✅ РЕЗУЛЬТАТ:\n" + response)
    finally:
        await handler.close()


async def test_scenario_32():
    """Сценарий 32: Вопрос про отмену (FAQ)"""
    print("=" * 60)
    print("СЦЕНАРИЙ 32: Вопрос про отмену")
    print("=" * 60)
    
    handler = YandexGPTHandler()
    try:
        response = await handler.chat(
            "Если я забронирую тур, можно ли потом отменить? Какие условия отмены?"
        )
        print("\n✅ РЕЗУЛЬТАТ:\n" + response)
    finally:
        await handler.close()


async def test_scenario_33():
    """Сценарий 33: STREAMING — ответ по частям"""
    print("=" * 60)
    print("СЦЕНАРИЙ 33: Streaming (ответ появляется по частям)")
    print("=" * 60)
    
    handler = YandexGPTHandler()
    try:
        print("\n🌊 Streaming ответ:")
        print("-" * 40)
        
        response = await handler.chat_stream(
            "Расскажи кратко про 3 популярных курорта Турции",
            on_token=lambda t: print(t, end="", flush=True)
        )
        
        print("\n" + "-" * 40)
        print(f"\n✅ Полный ответ получен ({len(response)} символов)")
    finally:
        await handler.close()


async def test_scenario_34():
    """Сценарий 34: STREAMING + Function Calling"""
    print("=" * 60)
    print("СЦЕНАРИЙ 34: Streaming с вызовом функций")
    print("=" * 60)
    
    handler = YandexGPTHandler()
    try:
        print("\n🌊 Streaming с функциями:")
        print("-" * 40)
        
        response = await handler.chat_stream(
            "Найди горящие туры из Москвы и расскажи о лучшем варианте",
            on_token=lambda t: print(t, end="", flush=True)
        )
        
        print("\n" + "-" * 40)
        print(f"\n✅ Ответ получен")
    finally:
        await handler.close()


async def run_all_scenarios():
    """Запустить все сценарии последовательно"""
    scenarios = [
        ("1", test_scenario_1),
        ("2", test_scenario_2),
        ("3", test_scenario_3),
        ("4", test_scenario_4),
        ("5", test_scenario_5),
        ("6", test_scenario_6),
        ("7", test_scenario_7),
        ("8", test_scenario_8),
        ("9", test_scenario_9),
        ("10", test_scenario_10),
        ("11", test_scenario_11),
        ("12", test_scenario_12),
        ("13", test_scenario_13),
        ("14", test_scenario_14),
        ("15", test_scenario_15),
        ("16", test_scenario_16),
        ("17", test_scenario_17),
        ("18", test_scenario_18),
        ("19", test_scenario_19),
        ("20", test_scenario_20),
        ("21", test_scenario_21),
        ("22", test_scenario_22),
        ("23", test_scenario_23),
        ("24", test_scenario_24),
        ("25", test_scenario_25),
        ("26", test_scenario_26),
        ("27", test_scenario_27),
        ("28", test_scenario_28),
        ("29", test_scenario_29),
        ("30", test_scenario_30),
        ("31", test_scenario_31),
        ("32", test_scenario_32),
    ]
    
    results = {}
    
    for name, func in scenarios:
        print(f"\n\n{'🚀' * 30}")
        print(f"ЗАПУСК СЦЕНАРИЯ {name}")
        print(f"{'🚀' * 30}\n")
        
        try:
            await func()
            results[name] = "✅ УСПЕХ"
        except Exception as e:
            results[name] = f"❌ ОШИБКА: {str(e)[:100]}"
            print(f"\n❌ ОШИБКА: {e}")
        
        print("\n" + "-" * 60)
        input("Нажмите Enter для следующего сценария...")
    
    # Итоги
    print("\n\n" + "=" * 60)
    print("ИТОГИ ТЕСТИРОВАНИЯ")
    print("=" * 60)
    for name, result in results.items():
        print(f"Сценарий {name}: {result}")


async def interactive_chat():
    """Интерактивный режим — реальный агент для общения"""
    print("=" * 60)
    print("🤖 AI МЕНЕДЖЕР ПО ТУРАМ (Responses API)")
    print("=" * 60)
    print("Напишите ваш запрос. Для выхода введите 'exit' или 'выход'.")
    print("Теперь работает поиск в интернете для вопросов о визах, погоде и т.д.")
    print("=" * 60)
    
    handler = YandexGPTHandler()
    
    try:
        while True:
            # Ввод от пользователя
            user_input = input("\n👤 Вы: ").strip()
            
            if not user_input:
                continue
            
            if user_input.lower() in ['exit', 'выход', 'quit', 'q']:
                print("\n👋 До свидания!")
                break
            
            # Ответ агента
            try:
                response = await handler.chat(user_input)
                print(f"\n🤖 Ассистент:\n{response}")
            except Exception as e:
                print(f"\n❌ Ошибка: {e}")
    
    finally:
        await handler.close()


async def interactive_chat_stream():
    """
    Интерактивный режим со STREAMING.
    Ответ появляется по частям — как в ChatGPT!
    """
    print("=" * 60)
    print("🌊 AI МЕНЕДЖЕР ПО ТУРАМ (STREAMING MODE)")
    print("=" * 60)
    print("Ответы появляются по частям — как в ChatGPT!")
    print("Напишите запрос. Для выхода: 'exit' или 'выход'.")
    print("=" * 60)
    
    handler = YandexGPTHandler()
    
    try:
        while True:
            # Ввод от пользователя
            user_input = input("\n👤 Вы: ").strip()
            
            if not user_input:
                continue
            
            if user_input.lower() in ['exit', 'выход', 'quit', 'q']:
                print("\n👋 До свидания!")
                break
            
            # Ответ агента со streaming
            try:
                print("\n🤖 Ассистент: ", end="", flush=True)
                response = await handler.chat_stream(
                    user_input,
                    on_token=lambda t: print(t, end="", flush=True)
                )
                print()  # Новая строка после ответа
            except Exception as e:
                print(f"\n❌ Ошибка: {e}")
    
    finally:
        await handler.close()


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        
        # Интерактивный режим
        if arg in ["chat", "run", "agent"]:
            asyncio.run(interactive_chat())
        elif arg in ["stream", "streaming"]:
            asyncio.run(interactive_chat_stream())
        # Тесты
        else:
            scenarios_map = {
                "1": test_scenario_1,
                "2": test_scenario_2,
                "3": test_scenario_3,
                "4": test_scenario_4,
                "5": test_scenario_5,
                "6": test_scenario_6,
                "7": test_scenario_7,
                "8": test_scenario_8,
                "9": test_scenario_9,
                "10": test_scenario_10,
                "11": test_scenario_11,
                "12": test_scenario_12,
                "13": test_scenario_13,
                "14": test_scenario_14,
                "15": test_scenario_15,
                "16": test_scenario_16,
                "17": test_scenario_17,
                "18": test_scenario_18,
                "19": test_scenario_19,
                "20": test_scenario_20,
                "21": test_scenario_21,
                "22": test_scenario_22,
                "23": test_scenario_23,
                "24": test_scenario_24,
                "25": test_scenario_25,
                "26": test_scenario_26,
                "27": test_scenario_27,
                "28": test_scenario_28,
                "29": test_scenario_29,
                "30": test_scenario_30,
                "31": test_scenario_31,
                "32": test_scenario_32,
                "33": test_scenario_33,
                "34": test_scenario_34,
                "all": run_all_scenarios,
            }
            if arg in scenarios_map:
                asyncio.run(scenarios_map[arg]())
            else:
                print(f"Неизвестная команда: {arg}")
                print("Доступные: chat, stream, 1-34, all")
    else:
        # По умолчанию — интерактивный режим со streaming
        asyncio.run(interactive_chat_stream())
