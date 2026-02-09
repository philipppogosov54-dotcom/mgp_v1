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
from typing import Optional, Dict, Any, List, Callable, AsyncIterator, Tuple
from openai import OpenAI
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
    ]
    return any(phrase in lower for phrase in promise_phrases)


def _check_cascade_slots(full_history: List[Dict], args: Dict) -> Tuple[bool, List[str]]:
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
    
    # Собираем последние 20 сообщений пользователя
    user_messages = [
        msg.get("content", "") for msg in full_history[-20:] 
        if msg.get("role") == "user" and msg.get("content")
    ]
    user_text = " ".join(user_messages).lower()
    
    # ─── Слот 2: Город вылета ───
    # Паттерны: названия городов вылета, "вылет из ...", "из москвы" и т.д.
    departure_patterns = [
        # Конкретные города вылета TourVisor
        r'\b(?:москв[аыуе]|мск)\b',
        r'\b(?:петербург\w*|питер\w*|спб|санкт-петербург\w*)\b',
        r'\b(?:екатеринбург\w*|еката)\b',
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
        # НЕ включаем Сочи — это чаще курорт (направление), а не город вылета
        # Обобщённые паттерны: "вылет из ...", "летим из ..."
        r'(?:вылет|вылетаем|летим|улетаем)\s+(?:из|с)\s+\w+',
        r'(?:из|с)\s+\w+\s+(?:вылет|вылетаем|улетаем)',
    ]
    has_departure_mention = any(re.search(p, user_text) for p in departure_patterns)
    
    if not has_departure_mention:
        missing.append("город вылета")
    
    # ─── Слот 3: Даты/месяц вылета ───
    # Паттерны для дат: числа с месяцами, названия месяцев, относительные даты
    date_patterns = [
        r'\d{1,2}\.\d{1,2}(?:\.\d{2,4})?',  # 21.03 или 21.03.2026
        r'\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)',
        r'(?:январ[еья]|феврал[еья]|март[еа]?|апрел[еья]|ма[еяй]|июн[еья]|июл[еья]|август[еа]?|сентябр[еья]|октябр[еья]|ноябр[еья]|декабр[еья])',
        r'(?:в\s+)?(?:начале|середине|конце)\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря|месяца)',
        r'(?:на\s+)?(?:майские|новогодние|новый год|8 марта|23 февраля)',
        r'(?:завтра|послезавтра|через\s+\w+\s+дн|через\s+неделю|через\s+месяц)',
        r'(?:в\s+)?(?:этом|следующем)\s+месяце',
        r'(?:в\s+)?ближайшее\s+время',
        r'(?:первой|второй)\s+половин[еы]',
    ]
    has_date_mention = any(re.search(p, user_text) for p in date_patterns)
    
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
        r'(?:взрослы[хй]|взр\.?|adults)',
        r'(?:дет(?:ей|и|ьми|ям)?|ребен(?:ок|ка)|child)',
        r'(?:я\s+)?(?:один|одна|сам|одиночк)',
        r'(?:двое|два|две)\s+(?:взрослы[хй]|человек|чел\.?)',  # "двое взрослых", "два человека"
        r'(?:трое|три|четыре|пять|шесть)\s+(?:взрослы[хй]|человек|чел\.?)',
        r'\d+\s*(?:взрослы[хй]|человек|чел\.?|взр)',  # "2 взрослых", "3 человека", "2в"
        r'\d+\s*в\s*\+',  # "2в+" — shorthand
        r'(?:с\s+)?(?:мужем|женой|парнем|девушкой|подругой|другом)',
        r'(?:вдво[её]м|втро[её]м|вчетвером|впятером)',
        r'(?:семь[её]й|компанией|группой)',
        r'(?:мы\s+с\s+)',
    ]
    has_travelers_mention = any(re.search(p, user_text) for p in travelers_patterns)
    
    if not has_travelers_mention:
        missing.append("состав путешественников")
    
    # ─── Слот 5: Quality Check (звёздность + питание) ───
    # Проверяем: клиент ЯВНО указал stars/meal ИЛИ явно "скипнул" (любой/не важно/и т.д.)
    # Также skip если клиент назвал конкретный отель/бренд (stars берётся из базы)
    
    stars_patterns = [
        r'\d\s*(?:звёзд|звезд|\*|⭐)',      # "5 звёзд", "4*", "5⭐"
        r'(?:пяти|четырёх|четырех|трёх|трех)звёзд',  # "пятизвёздочный"
    ]
    meal_patterns = [
        r'(?:всё?\s*включен|all\s*incl|[ауа]и|ai\b|uai\b)',   # "всё включено", "all inclusive", "AI", "UAI"
        r'(?:полупансион|half\s*board|hb\b)',
        r'(?:полный\s*пансион|full\s*board|fb\b)',
        r'(?:только\s*)?завтрак[аи]?\b',
        r'\b(?:bb|ro|ob)\b',  # bed&breakfast, room only, only bed
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
    
    # skip_quality ищем ТОЛЬКО по последнему сообщению пользователя
    # (чтобы "любой курорт" из раннего сообщения не пометил QC как пройденный)
    last_user_msg = user_messages[-1].lower() if user_messages else ""
    has_skip = any(re.search(p, last_user_msg) for p in skip_quality_patterns)
    
    # Quality Check пройден если:
    # - клиент указал хотя бы stars ИЛИ meal
    # - ИЛИ клиент явно скипнул ("любой", "не важно")
    # - ИЛИ клиент назвал конкретный бренд/отель
    quality_check_passed = has_stars or has_meal or has_skip or has_brand
    
    if not quality_check_passed:
        # Проверяем: может быть модель уже задала вопрос о QC, 
        # а клиент ответил чем-то неожиданным — не блокируем повторно
        # Ищем в истории ассистента вопрос про звёздность/питание
        assistant_messages = [
            msg.get("content", "") for msg in full_history[-10:] 
            if msg.get("role") == "assistant" and msg.get("content")
        ]
        assistant_text = " ".join(assistant_messages).lower()
        qc_asked = any(phrase in assistant_text for phrase in [
            "категорию отеля", "тип питания", "звёзд", "питание предпочитаете",
            "какой отель", "звёздность", "всё включено"
        ])
        # Если ассистент УЖЕ спрашивал QC и клиент ответил (есть следующее сообщение) — 
        # считаем что клиент явно или неявно скипнул
        if not qc_asked:
            missing.append("категорию отеля и тип питания (Quality Check)")
    
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
    9: "Новосибирск", 10: "Казань", 11: "Краснодар",
    12: "Красноярск", 18: "Ростов-на-Дону", 56: "Сочи",
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
        "is_hotel_only": bool(tour.get("noflight")),
        "flight_included": not bool(tour.get("noflight")),
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


class YandexGPTHandler:
    """Обработчик запросов к Yandex GPT с Function Calling (Responses API)"""
    
    def __init__(self):
        self.folder_id = os.getenv("YANDEX_FOLDER_ID")
        self.api_key = os.getenv("YANDEX_API_KEY")
        self.model = os.getenv("YANDEX_MODEL", "yandexgpt")
        
        # OpenAI-совместимый клиент для Responses API
        self.client = OpenAI(
            api_key=self.api_key,
            base_url="https://ai.api.cloud.yandex.net/v1",
            project=self.folder_id
        )
        
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
        # 40 сообщений ≈ 20 обменов user/assistant — достаточно для контекста.
        self._max_history_len = 40
        
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
        args = json.loads(arguments) if arguments else {}
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
            # Запоминаем город вылета для маппинга tour_cards
            dep_code = args.get("departure")
            if dep_code is not None:
                self._last_departure_city = _DEPARTURE_CITIES.get(
                    _safe_int(dep_code), self._last_departure_city
                )
            
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
                    
                    # Случай 1: dateto не указан → авто-установка datefrom + 2
                    if dateto_dt is None:
                        dateto_dt = datefrom_dt + _td(days=2)
                        args["dateto"] = dateto_dt.strftime("%d.%m.%Y")
                        logger.warning("⚠️ dateto не указан, установлен = datefrom+2 (%s)", args["dateto"])
                    
                    # Случай 2: dateto == datefrom (слишком узкий) → расширяем до +2
                    elif dateto_dt == datefrom_dt:
                        dateto_dt = datefrom_dt + _td(days=2)
                        args["dateto"] = dateto_dt.strftime("%d.%m.%Y")
                        logger.warning("⚠️ dateto == datefrom, расширен до datefrom+2 (%s)", args["dateto"])
                    
                    # Случай 3: конкретная дата + длительность, но dateto слишком далеко
                    # Если nightsfrom/nightsto указаны и dateto - datefrom > nightsto,
                    # значит модель интерпретировала dateto как дату окончания тура,
                    # а не как последнюю дату вылета. Clamp до datefrom + 2.
                    elif has_specific_nights and dateto_dt is not None:
                        delta_days = (dateto_dt - datefrom_dt).days
                        effective_nights = nightsto or nightsfrom or 7
                        # Если диапазон дат > 3 дней и при этом примерно равен длительности ночей —
                        # это ошибка модели (она посчитала dateto = datefrom + nights)
                        if delta_days >= 4 and abs(delta_days - effective_nights) <= 2:
                            corrected_dt = datefrom_dt + _td(days=2)
                            self._metrics["dateto_corrections"] += 1
                            logger.warning(
                                "⚠️ dateto clamp: модель выставила dateto=%s (datefrom+%d дней ≈ nights=%d). "
                                "Исправлено на datefrom+2 = %s (это окно дат ВЫЛЕТА, не дата возвращения!)",
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
            
            # ── Fix P3: Проверка региона/курорта ──
            # Если клиент указал конкретный курорт, но модель НЕ передала regions —
            # возвращаем ошибку с инструкцией определить регион
            if not args.get("regions") and not args.get("subregions") and not args.get("hotels"):
                user_messages_for_region = [
                    msg.get("content", "") for msg in self.full_history[-20:] 
                    if msg.get("role") == "user" and msg.get("content")
                ]
                user_text_for_region = " ".join(user_messages_for_region).lower()
                
                # Универсальный список курортов по странам
                # Формат: (паттерн, страна_для_подсказки)
                resort_patterns = [
                    # Россия (country=47)
                    (r'\b(?:кисловодск|пятигорск|ессентуки|железноводск|минеральн\w*\s*вод)\b', "России"),
                    (r'\b(?:сочи|адлер|красн\w*\s*полян)\b', "России"),
                    (r'\b(?:анап[аыуе]|геленджик|новоросс)\b', "России"),
                    (r'\b(?:крым|ялт[аыуе]|алушт[аыуе]|севастопол|феодоси|судак|евпатори)\b', "России"),
                    (r'\b(?:калининград|светлогорск|зеленоградск)\b', "России"),
                    # Таиланд (country=2)
                    (r'\b(?:пхукет|пукет)\b', "Таиланда"),
                    (r'\b(?:паттай[яеу]|паттая)\b', "Таиланда"),
                    (r'\b(?:самуи)\b', "Таиланда"),
                    (r'\b(?:краби)\b', "Таиланда"),
                    (r'\b(?:хуа\s*хин)\b', "Таиланда"),
                    # Турция (country=4) 
                    (r'\b(?:алан[ьи]я|аланья)\b', "Турции"),
                    (r'\b(?:анталь?я|анталия)\b', "Турции"),
                    (r'\b(?:кемер)\b', "Турции"),
                    (r'\b(?:сиде)\b', "Турции"),
                    (r'\b(?:белек)\b', "Турции"),
                    (r'\b(?:бодрум)\b', "Турции"),
                    (r'\b(?:мармарис)\b', "Турции"),
                    (r'\b(?:фетхие|фетие)\b', "Турции"),
                    (r'\b(?:кушадас)\b', "Турции"),
                    (r'\b(?:стамбул)\b', "Турции"),
                    # Египет (country=1)
                    (r'\b(?:шарм|шарм-эль-шейх|шарм\s*эль\s*шейх)\b', "Египта"),
                    (r'\b(?:хургад[аыуе])\b', "Египта"),
                    (r'\b(?:марса\s*алам)\b', "Египта"),
                    (r'\b(?:дахаб)\b', "Египта"),
                    # ОАЭ (country=9)
                    (r'\b(?:дубай|дубаи)\b', "ОАЭ"),
                    (r'\b(?:абу[\s-]*даби)\b', "ОАЭ"),
                    (r'\b(?:шардж[аеу])\b', "ОАЭ"),
                    (r'\b(?:рас[\s-]*аль[\s-]*хайм)\b', "ОАЭ"),
                    # Вьетнам (country=16)
                    (r'\b(?:фукуок|фу\s*куок)\b', "Вьетнама"),
                    (r'\b(?:нячанг|ня\s*чанг)\b', "Вьетнама"),
                    (r'\b(?:фантьет|фан\s*тьет|муйне|муй\s*не)\b', "Вьетнама"),
                    # Шри-Ланка
                    (r'\b(?:коломбо|бентот[аы]|хиккадув[аы]|унаватун[аы])\b', "Шри-Ланки"),
                    # Мальдивы
                    (r'\b(?:мале|маафуш)\b', "Мальдив"),
                    # Куба
                    (r'\b(?:варадеро|гаван[аы])\b', "Кубы"),
                    # Доминикана
                    (r'\b(?:пунта[\s-]*кан[аы]|бока[\s-]*чик[аы])\b', "Доминиканы"),
                ]
                
                mentioned_resort = None
                for pattern, country_name in resort_patterns:
                    if re.search(pattern, user_text_for_region):
                        mentioned_resort = (re.search(pattern, user_text_for_region).group(), country_name)
                        break
                
                if mentioned_resort:
                    resort_name, country_name = mentioned_resort
                    self._metrics.setdefault("resort_without_region_detections", 0)
                    self._metrics["resort_without_region_detections"] += 1
                    logger.warning(
                        "⚠️ RESORT-WITHOUT-REGION: клиент указал курорт '%s' (%s), но модель НЕ передала regions — блокируем",
                        resort_name, country_name
                    )
                    country_code = args.get("country", "")
                    return {
                        "status": "error",
                        "error": (
                            f"СИСТЕМНАЯ ОШИБКА: Клиент указал конкретный курорт '{resort_name}', "
                            f"но ты НЕ передал параметр regions в search_tours! "
                            f"ОБЯЗАТЕЛЬНО определи код региона: вызови get_dictionaries(type='region', regcountry={country_code}) "
                            f"и найди код для '{resort_name}'. Затем передай regions=КОД в search_tours. "
                            f"Без regions поиск вернёт туры по ВСЕЙ стране, а не по указанному курорту!"
                        ),
                        "_hint": f"Определи код региона '{resort_name}' через get_dictionaries и передай в regions."
                    }
            
            # ── Проверка полноты каскада (Fix 3B — блокирующая проверка) ──
            # Анализируем историю диалога, чтобы убедиться, что клиент ЯВНО указал критичные слоты
            is_cascade_complete, missing_slots = _check_cascade_slots(self.full_history, args)
            
            if not is_cascade_complete:
                self._metrics["cascade_incomplete_detections"] += 1
                logger.warning(
                    "⚠️ CASCADE-INCOMPLETE: клиент НЕ указал %s — блокируем search_tours и nudge модель",
                    ", ".join(missing_slots)
                )
                # Возвращаем ошибку с ОДНИМ приоритетным вопросом (по порядку каскада: 2→3→4→5)
                # Правило § 0.3: "задавай ОДИН чёткий вопрос", не анкету
                first_missing = missing_slots[0]  # Берём первый по приоритету
                
                nudge_map = {
                    "город вылета": "'Из какого города планируете вылет?'",
                    "даты/месяц и длительность": "'Когда планируете поездку и на сколько ночей?'",
                    "даты/месяц вылета": "'В каком месяце планируете вылет?'",
                    "состав путешественников": "'Сколько взрослых едет и будут ли с вами дети?'",
                    "категорию отеля и тип питания (Quality Check)": "'Какую категорию отеля и тип питания предпочитаете?'",
                }
                nudge = nudge_map.get(first_missing, f"Уточни у клиента: {first_missing}")
                
                return {
                    "status": "error",
                    "error": (
                        f"СИСТЕМНАЯ ОШИБКА ВАЛИДАЦИИ КАСКАДА: Клиент НЕ указал {first_missing}! "
                        f"ОБЯЗАТЕЛЬНО спроси клиента ЯВНО: {nudge}. "
                        f"Задай ТОЛЬКО ОДИН вопрос, не перечисляй список! "
                        f"НЕ вызывай search_tours пока клиент не ответит!"
                    ),
                    "_hint": "Это защита от пропуска слотов каскада. Спроси ОДИН вопрос о недостающих данных."
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
            
            return {"requestid": str(request_id), "message": "Поиск запущен. Вызови get_search_status — он автоматически дождётся результатов. Затем get_search_results."}
        
        elif name == "get_search_status":
            # ⚡ КРИТИЧЕСКИ ВАЖНО: Внутренний polling с ожиданием!
            # Без этого AI вызывает get_search_status в цикле и сжигает все итерации.
            # Теперь ОДНА итерация AI = полное ожидание завершения поиска.
            request_id = args["requestid"]
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
                        raise NoResultsError(
                            f"Поиск завершён: найдено {hotels_found} отелей, {tours_found} туров",
                            filters_hint="Попробуйте расширить даты, увеличить бюджет или убрать фильтры"
                        )

                    last_status["_hint"] = (
                        f"Поиск завершён! Найдено {hotels_found} отелей, {tours_found} туров. "
                        f"Вызови get_search_results с requestid для получения списка отелей."
                    )
                    return last_status
                
                if state == "no search results":
                    last_status["_hint"] = "Поиск не найден. requestid недействителен — нужен новый поиск."
                    return last_status
                
                # Если уже есть достаточно результатов (>5 отелей) и прогресс >40% —
                # можно забирать частичные результаты, не ждать 100%
                hotels_found = last_status.get("hotelsfound", 0)
                progress = last_status.get("progress", 0)
                if hotels_found >= 5 and progress >= 40:
                    logger.info("📊 SEARCH READY (partial)  requestid=%s  progress=%s%%  hotels=%s — returning early",
                                request_id, progress, hotels_found)
                    last_status["_hint"] = (
                        f"Поиск ещё идёт ({progress}%), но уже найдено {hotels_found} отелей. "
                        f"Вызови get_search_results с этим requestid для показа результатов."
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
            full_results = await self.tourvisor.get_search_results(
                request_id=args["requestid"],
                page=args.get("page", 1),
                per_page=args.get("onpage", 10),  # Ограничиваем до 10 отелей
                include_operators=args.get("operatorstatus") == 1,
                no_description=args.get("nodescription") == 1
            )
            
            # Сокращаем результаты для AI — формат карточек с картинками
            hotels = full_results.get("result", {}).get("hotel", [])
            simplified = []
            for h in hotels[:5]:  # Максимум 5 отелей для AI
                tours = h.get("tours", {}).get("tour", [])
                best_tour = tours[0] if tours else {}
                
                # Проверяем картинку — не показываем заглушки регионов
                picture = h.get("picturelink", "")
                has_real_photo = h.get("isphoto") == 1 and picture and "/reg-" not in picture
                
                simplified.append({
                    "hotelcode": h.get("hotelcode"),
                    "hotelname": h.get("hotelname"),
                    "hotelstars": h.get("hotelstars"),
                    "hotelrating": h.get("hotelrating"),
                    "regionname": h.get("regionname"),
                    "countryname": h.get("countryname"),
                    "price": h.get("price"),
                    "seadistance": h.get("seadistance"),
                    "picturelink": picture if has_real_photo else None,  # Только реальные фото
                    "hoteldescription": h.get("hoteldescription"),  # Описание
                    "fulldesclink": h.get("fulldesclink"),  # Ссылка на подробности
                    "tour": {
                        "tourid": best_tour.get("tourid"),
                        "price": best_tour.get("price"),  # Цена конкретного тура
                        "flydate": best_tour.get("flydate"),
                        "nights": best_tour.get("nights"),
                        "meal": best_tour.get("mealrussian"),
                        "room": best_tour.get("room"),
                        "placement": best_tour.get("placement"),
                        "operatorname": best_tour.get("operatorname"),
                        "tourname": best_tour.get("tourname"),  # Название тура
                        # ⚠️ Важные статусы для предупреждений клиенту:
                        "promo": best_tour.get("promo"),
                        "regular": best_tour.get("regular"),
                        "onrequest": best_tour.get("onrequest"),
                        "flightstatus": best_tour.get("flightstatus"),
                        "hotelstatus": best_tour.get("hotelstatus"),
                        "nightflight": best_tour.get("nightflight"),
                        # ⚠️ Важные флаги об исключениях из тура:
                        "noflight": best_tour.get("noflight"),
                        "notransfer": best_tour.get("notransfer"),
                        "nomedinsurance": best_tour.get("nomedinsurance"),
                        "nomeal": best_tour.get("nomeal")
                    } if best_tour else None
                })
            
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
                }
                if warnings:
                    entry["warnings"] = warnings
                ai_hotels.append(entry)

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
                return await self.tourvisor.get_regions(args.get("regcountry"))
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
                # Фильтруем по названию если указано
                name_filter = args.get("name", "").lower()
                if name_filter:
                    hotels = [h for h in hotels if name_filter in h.get("name", "").lower()]
                return hotels[:20]  # Максимум 20 отелей
            elif "currency" in dict_type:
                # Курсы валют туроператоров
                return await self.tourvisor.get_currencies()
            else:
                return {"error": f"Неизвестный тип справочника: {dict_type}"}
        
        elif name == "actualize_tour":
            return await self.tourvisor.actualize_tour(
                tour_id=args["tourid"],
                request_mode=args.get("request", 2),
                currency=args.get("currency", 0)
            )
        
        elif name == "get_tour_details":
            return await self.tourvisor.get_tour_details(
                tour_id=args["tourid"],
                currency=args.get("currency", 0)
            )
        
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
                ] if args.get("reviews") == 1 else []
            }
        
        elif name == "get_hot_tours":
            tours = await self.tourvisor.get_hot_tours(
                city=args["city"],
                count=args.get("items", 10),
                city2=args.get("city2"),
                city3=args.get("city3"),
                uniq2=args.get("uniq2"),
                uniq3=args.get("uniq3"),
                countries=args.get("countries"),
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
                picturetype=args.get("picturetype", 0),
                currency=args.get("currency", 0)
            )
            
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
                })

            return {
                "total_found": len(tours),
                "note": "ВАЖНО: Цены указаны ЗА ЧЕЛОВЕКА! Для двоих умножай на 2.",
                "tours": ai_tours,
                "_hint": "Карточки с фото, ценами, датами, питанием, звёздами УЖЕ отображены фронтендом. НЕ перечисляй отели, цены, описания, звёзды в тексте! Напиши ТОЛЬКО краткий комментарий и упомяни что цены за человека."
            }
        
        elif name == "continue_search":
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
        Синхронный вызов Responses API.
        Используется через asyncio.to_thread() для неблокирующего выполнения.
        """
        return self.client.responses.create(
            model=self.model_uri,
            input=self.input_list,
            instructions=self.instructions,
            tools=self.tools,
            temperature=0.3,
            max_output_tokens=4000,
            previous_response_id=self.previous_response_id,
            stream=stream
        )
    
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
                # Собираем summary функций для full_history (на случай fallback)
                # ⚡ Увеличен лимит до 1500 символов — при 500 терялся контекст
                #    (особенно данные отелей, цен и дат из search_results)
                func_summary_parts = []
                for result in function_results:
                    call_id = result.get("call_id", "")
                    output = result.get("output", "")
                    # Найдём имя функции по call_id
                    for item in response.output:
                        if getattr(item, 'call_id', '') == call_id:
                            func_name = getattr(item, 'name', '?')
                            # Для search_results и hotel_info — больше контекста (содержат карточки)
                            limit = 2000 if func_name in ('get_search_results', 'get_hotel_info', 'get_hot_tours') else 1000
                            func_summary_parts.append(f"[{func_name}]: {output[:limit]}")
                            break
                
                # В full_history сохраняем как assistant-сообщение (для fallback без previous_response_id)
                if func_summary_parts:
                    self.full_history.append({
                        "role": "assistant",
                        "content": "Результаты запросов:\n" + "\n".join(func_summary_parts)
                    })
                
                # input_list = только function results (function_calls в previous_response_id)
                self.input_list = function_results
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
                        return "Извините, не удалось обработать запрос. Попробуйте переформулировать."
                    # Fallback: пересылаем всю историю + nudge сообщение
                    self.previous_response_id = None
                    nudge = {"role": "user", "content": "Продолжи обработку моего запроса на основе полученных данных."}
                    self.input_list = list(self.full_history) + [nudge]
                    continue
                
                # ⚡ Детект самомодерации модели ("Я не могу обсуждать эту тему")
                if final_text and _is_self_moderation(final_text):
                    empty_retries += 1
                    logger.warning("⚠️ SELF-MODERATION detected (#%d): \"%s\"", empty_retries, final_text[:100])
                    if empty_retries >= 3:
                        return "Извините, произошла ошибка. Попробуйте переформулировать запрос или начните новый чат."
                    # Fallback: сбрасываем контекст и повторяем
                    self.previous_response_id = None
                    self.input_list = list(self.full_history) + [
                        {"role": "user", "content": "Пожалуйста, помоги с подбором тура. Продолжи с того места, где мы остановились."}
                    ]
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
                
                # Дедупликация ответа (Yandex GPT quirk)
                final_text = _dedup_response(final_text)
                
                # Успешный ответ — сохраняем в историю
                self.full_history.append({"role": "assistant", "content": final_text})
                self.input_list = []
                
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
                # ⚡ Детект самомодерации модели
                if _is_self_moderation(full_text):
                    self._empty_iterations += 1
                    logger.warning("⚠️ STREAM SELF-MODERATION detected (#%d): \"%s\"",
                                   self._empty_iterations, full_text[:100])
                    if self._empty_iterations >= 3:
                        self._empty_iterations = 0
                        return "Извините, произошла ошибка. Попробуйте переформулировать запрос или начните новый чат."
                    # Сбрасываем контекст и повторяем
                    self.previous_response_id = None
                    self.input_list = list(self.full_history) + [
                        {"role": "user", "content": "Пожалуйста, помоги с подбором тура. Продолжи с того места, где мы остановились."}
                    ]
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
                
                # Сбрасываем счётчик
                self._empty_iterations = 0
                
                # Дедупликация (Yandex GPT quirk)
                full_text = _dedup_response(full_text)
                
                # Сохраняем в full_history и чистим input_list
                self.full_history.append({"role": "assistant", "content": full_text})
                self.input_list = []
                
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
