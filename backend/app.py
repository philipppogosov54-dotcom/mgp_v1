"""
Web UI для чата с AI-менеджером турагентства
Flask + Server-Sent Events для streaming
"""

import asyncio
import os
import time
import uuid
import logging
from flask import Flask, render_template, request, Response, jsonify, stream_with_context, g, send_from_directory
from flask_cors import CORS
from werkzeug.exceptions import HTTPException
try:
    # если запускают из папки backend (python app.py)
    from yandex_handler import YandexGPTHandler
except ImportError:
    # если запускают из корня (python -m backend.app)
    from backend.yandex_handler import YandexGPTHandler
import json
import queue
import threading

app = Flask(__name__, template_folder='templates', static_folder='static')
CORS(app)

# === ЛОГИРОВАНИЕ ===
from datetime import datetime as _dt

# Директория для логов
_LOGS_DIR = os.path.join(os.path.dirname(__file__), "..", "logs")
os.makedirs(_LOGS_DIR, exist_ok=True)

# Файл диалогового лога (человекочитаемый markdown)
_DIALOGUE_LOG_PATH = os.path.join(
    _LOGS_DIR,
    f"dialogue_{_dt.now().strftime('%Y%m%d_%H%M%S')}.md"
)


def _write_dialogue_log(session_id: str, direction: str, content: str):
    """
    Пишет в человекочитаемый диалоговый лог (markdown).
    direction: 'USER', 'ASSISTANT', 'FUNC_CALL', 'FUNC_RESULT', 'API_RAW', 'ERROR', 'SYSTEM'
    """
    ts = _dt.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    sid = session_id[:8] if session_id else "--------"
    icons = {
        "USER": "👤", "ASSISTANT": "🤖", "FUNC_CALL": "🔧",
        "FUNC_RESULT": "📦", "API_RAW": "🌐", "ERROR": "❌", "SYSTEM": "⚙️",
        "TOUR_CARDS": "🎴"
    }
    icon = icons.get(direction, "📝")
    try:
        with open(_DIALOGUE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n### [{ts}] {icon} {direction} (session: {sid})\n")
            f.write(f"```\n{content}\n```\n")
    except Exception:
        pass  # лог не должен ломать приложение


def _setup_logging() -> logging.Logger:
    """
    Единая настройка логирования в консоль + файл.
    Управление:
      - LOG_LEVEL=DEBUG|INFO|WARNING|ERROR (по умолчанию INFO)
    """
    logger = logging.getLogger("mgp_bot")

    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logger.setLevel(level)

    formatter = logging.Formatter(
        fmt="%(asctime)s.%(msecs)03d %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # --- Console handler ---
    if logger.handlers:
        handler = logger.handlers[0]
    else:
        handler = logging.StreamHandler()
        logger.addHandler(handler)

    handler.setLevel(level)
    handler.setFormatter(formatter)

    # --- File handler (полный лог с DEBUG) ---
    file_log_path = os.path.join(
        _LOGS_DIR,
        f"server_{_dt.now().strftime('%Y%m%d_%H%M%S')}.log"
    )
    file_handler = logging.FileHandler(file_log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s.%(msecs)03d %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(file_handler)

    # WerkZeug: по умолчанию скрываем access-логи (они дублируют наши -> / <-).
    # При необходимости можно включить обратно через WERKZEUG_LOG_LEVEL=INFO.
    werk_logger = logging.getLogger("werkzeug")
    werk_level_name = os.getenv("WERKZEUG_LOG_LEVEL", "WARNING").upper()
    werk_level = getattr(logging, werk_level_name, logging.WARNING)
    werk_logger.setLevel(werk_level)
    if not werk_logger.handlers:
        werk_logger.addHandler(handler)
        werk_logger.addHandler(file_handler)
    else:
        # на случай, если handler уже был, приведём его к одному формату
        for h in werk_logger.handlers:
            h.setLevel(werk_level)
            h.setFormatter(formatter)

    logger.info("📁 Server log: %s", file_log_path)
    logger.info("📁 Dialogue log: %s", _DIALOGUE_LOG_PATH)

    return logger


logger = _setup_logging()

# Записываем заголовок диалогового лога
with open(_DIALOGUE_LOG_PATH, "w", encoding="utf-8") as _f:
    _f.write(f"# 📝 Диалоговый лог AI-Турменеджера МГП\n")
    _f.write(f"**Дата:** {_dt.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
    _f.write(f"---\n")


def log(msg: str, level: str = "INFO"):
    """Совместимость со старым логгером (level=INFO/OK/WARN/ERROR/MSG/FUNC)."""
    level_map = {
        "INFO": logging.INFO,
        "OK": logging.INFO,
        "WARN": logging.WARNING,
        "ERROR": logging.ERROR,
        "MSG": logging.INFO,
        "FUNC": logging.DEBUG,
    }
    py_level = level_map.get(level, logging.INFO)
    logger.log(py_level, f"[{level}] {msg}")

# === УПРАВЛЕНИЕ СЕССИЯМИ ===
# Thread-safe хранилище сессий с автоочисткой
_handlers_lock = threading.Lock()
_handlers: dict[str, dict] = {}  # session_id → {"handler": YandexGPTHandler, "last_active": float}
SESSION_TTL_SECONDS = 30 * 60  # 30 минут неактивности → удаление


def get_handler(session_id: str) -> YandexGPTHandler:
    """Получить или создать handler для сессии (thread-safe)"""
    with _handlers_lock:
        if session_id in _handlers:
            _handlers[session_id]["last_active"] = time.time()
            return _handlers[session_id]["handler"]
        handler = YandexGPTHandler()
        # Подключаем диалоговый лог
        handler._dialogue_log_callback = lambda direction, content: _write_dialogue_log(session_id, direction, content)
        _handlers[session_id] = {"handler": handler, "last_active": time.time()}
        logger.info("🆕 New session %s  (total sessions: %d)", session_id[:8], len(_handlers))
        _write_dialogue_log(session_id, "SYSTEM", f"New session created (model: {handler.model})")
        return handler


def _cleanup_stale_sessions():
    """Удалить сессии, неактивные дольше SESSION_TTL_SECONDS"""
    now = time.time()
    with _handlers_lock:
        stale = [sid for sid, info in _handlers.items()
                 if now - info["last_active"] > SESSION_TTL_SECONDS]
        for sid in stale:
            handler = _handlers[sid]["handler"]
            try:
                handler.close_sync()
            except Exception:
                logger.debug("close_sync failed for session %s", sid[:8], exc_info=True)
            del _handlers[sid]
        if stale:
            logger.info("🧹 Cleaned up %d stale sessions (remaining: %d)", len(stale), len(_handlers))


# Путь к новому фронтенду (frontend/) — абсолютный путь для корректной работы send_from_directory
_FRONTEND_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend"))


@app.route('/')
def index():
    """Главная страница — новый чат-виджет"""
    return send_from_directory(_FRONTEND_DIR, 'index.html')


@app.route('/widget')
def widget():
    """Новый чат-виджет с визуальными карточками туров"""
    return send_from_directory(_FRONTEND_DIR, 'index.html')


@app.route('/frontend/<path:filename>')
def frontend_static(filename):
    """Статические файлы нового фронтенда (CSS, JS)"""
    return send_from_directory(_FRONTEND_DIR, filename)


@app.route('/favicon.ico')
def favicon():
    """Чтобы не засорять логи 404-ками от браузера."""
    return ("", 204)

_last_cleanup = [0.0]  # mutable container for nonlocal access

@app.before_request
def _log_request_start():
    g._req_start = time.perf_counter()
    g.request_id = uuid.uuid4().hex[:8]
    logger.info("-> %s %s rid=%s ip=%s", request.method, request.path, g.request_id, request.remote_addr)
    # Периодическая очистка устаревших сессий (каждые 5 минут)
    now = time.time()
    if now - _last_cleanup[0] > 300:
        _last_cleanup[0] = now
        _cleanup_stale_sessions()


@app.after_request
def _log_request_end(response):
    try:
        duration_ms = int((time.perf_counter() - getattr(g, "_req_start", time.perf_counter())) * 1000)
    except Exception:
        duration_ms = -1
    rid = getattr(g, "request_id", "-")
    logger.info("<- %s %s %s %dms rid=%s", request.method, request.path, response.status_code, duration_ms, rid)
    # удобно дергать request-id из фронта при разборе багов
    response.headers["X-Request-Id"] = rid
    return response


@app.errorhandler(Exception)
def _handle_unexpected_error(e: Exception):
    # не ломаем штатные HTTP ошибки (404/405 и т.п.)
    if isinstance(e, HTTPException):
        return e

    rid = getattr(g, "request_id", "-")
    logger.exception("Unhandled exception rid=%s path=%s", rid, request.path)
    if request.path.startswith("/api/"):
        return jsonify({"error": str(e), "request_id": rid}), 500
    return "Internal Server Error", 500


@app.route('/api/chat', methods=['POST'])
def chat():
    """Обычный chat без streaming"""
    data = request.json
    message = data.get('message', '')
    session_id = data.get('session_id', 'default')
    
    if not message:
        return jsonify({'error': 'Empty message'}), 400
    
    handler = get_handler(session_id)
    
    try:
        # Запускаем async функцию
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        response = loop.run_until_complete(handler.chat(message))
        loop.close()
        
        return jsonify({'response': response})
    except Exception as e:
        logger.exception("chat error session_id=%s", session_id)
        return jsonify({'error': str(e)}), 500


@app.route('/api/v1/chat', methods=['POST'])
def chat_v1():
    """
    Новый API для чат-виджета.
    Принимает: { message, conversation_id }
    Возвращает: { reply, tour_cards, conversation_id }
    
    Ключевое отличие от /api/chat/stream:
    - Один JSON-ответ (не SSE stream)
    - tour_cards — массив структурированных объектов для визуальных карточек
    - reply — текстовый ответ ассистента (без Markdown-карточек)
    """
    data = request.json
    message = data.get('message', '')
    conversation_id = data.get('conversation_id', str(uuid.uuid4()))

    if not message:
        return jsonify({
            'error': 'Empty message',
            'reply': '',
            'tour_cards': [],
            'conversation_id': conversation_id
        }), 400

    # conversation_id → session_id
    session_id = conversation_id

    log(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", "INFO")
    log(f"📨 [v1] Новое сообщение от {session_id[:8]}...", "MSG")
    log(f"   └─ \"{message[:100]}{'...' if len(message) > 100 else ''}\"", "MSG")

    _write_dialogue_log(session_id, "USER", message)

    handler = get_handler(session_id)

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        reply = loop.run_until_complete(handler.chat(message))
        loop.close()

        # Забираем накопленные tour_cards
        tour_cards = list(handler._pending_tour_cards)
        handler._pending_tour_cards = []

        _write_dialogue_log(session_id, "ASSISTANT", reply)

        # Логируем карточки туров (полные данные: цены, даты, отели, питание)
        if tour_cards:
            cards_summary_lines = []
            for i, card in enumerate(tour_cards, 1):
                cards_summary_lines.append(
                    f"  {i}. {card.get('hotel_name', '?')} {'⭐' * card.get('hotel_stars', 0)}\n"
                    f"     📍 {card.get('country', '')} / {card.get('resort', '')}\n"
                    f"     💰 {card.get('price', '?'):,} ₽ {'(за чел.)' if card.get('price_per_person') else '(за тур)'}\n"
                    f"     📅 {card.get('date_from', '?')} → {card.get('date_to', '?')} ({card.get('nights', '?')} ночей)\n"
                    f"     🍽 {card.get('meal_description', card.get('food_type', '?'))}\n"
                    f"     🏨 {card.get('room_type', '?')}\n"
                    f"     ✈️ Из: {card.get('departure_city', '?')} | Перелёт: {'Да' if card.get('flight_included') else 'Нет'}\n"
                    f"     🏢 Оператор: {card.get('operator', '?')}\n"
                    f"     🔗 {card.get('hotel_link', '')}"
                )
            cards_text = f"Показано {len(tour_cards)} карточек:\n" + "\n".join(cards_summary_lines)
            _write_dialogue_log(session_id, "TOUR_CARDS", cards_text)

        log(f"✅ [v1] Ответ: {len(reply)} символов, {len(tour_cards)} карточек", "OK")

        return jsonify({
            'reply': reply,
            'tour_cards': tour_cards,
            'conversation_id': conversation_id
        })

    except Exception as e:
        logger.exception("[v1] chat error session_id=%s", session_id)
        _write_dialogue_log(session_id, "ERROR", str(e))
        return jsonify({
            'error': str(e),
            'reply': 'Извините, произошла техническая ошибка. Попробуйте ещё раз.',
            'tour_cards': [],
            'conversation_id': conversation_id
        }), 500


@app.route('/api/chat/stream', methods=['POST'])
def chat_stream():
    """Chat со streaming через SSE"""
    data = request.json
    message = data.get('message', '')
    session_id = data.get('session_id', 'default')
    
    log(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", "INFO")
    log(f"📨 Новое сообщение от {session_id[:8]}...", "MSG")
    log(f"   └─ \"{message[:100]}{'...' if len(message) > 100 else ''}\"", "MSG")
    
    # Логируем входящее сообщение пользователя
    _write_dialogue_log(session_id, "USER", message)
    
    if not message:
        log("❌ Пустое сообщение!", "ERROR")
        return jsonify({'error': 'Empty message'}), 400
    
    handler = get_handler(session_id)
    log(f"📊 Модель: {handler.model}", "INFO")
    log(f"📊 История: {len(handler.input_list)} сообщений", "INFO")
    
    def generate():
        token_queue = queue.Queue()
        result = {'response': '', 'error': None}
        token_count = [0]  # Счётчик токенов
        accumulated_text = ['']  # Накопленный текст для dedup
        first_line = [None]  # Первая строка ответа
        dedup_active = [False]  # Флаг: обнаружен дубликат, прекращаем отправку
        
        def on_token(token):
            accumulated_text[0] += token
            
            # Определяем первую строку (после первого \n)
            if first_line[0] is None:
                nl_idx = accumulated_text[0].find('\n')
                if nl_idx > 10:
                    first_line[0] = accumulated_text[0][:nl_idx].strip()
            
            # Проверяем дубликат: если первая строка повторилась
            if first_line[0] and len(accumulated_text[0]) > len(first_line[0]) + 50:
                second = accumulated_text[0].find(first_line[0], len(first_line[0]) + 1)
                if second > 0 and not dedup_active[0]:
                    dedup_active[0] = True
                    logger.debug("🧹 STREAM DEDUP: duplicate detected at char %d, stopping token emission", second)
            
            if not dedup_active[0]:
                token_queue.put(('token', token))
                token_count[0] += 1
        
        def run_chat():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                log("🚀 Отправляю запрос в YandexGPT...", "INFO")
                response = loop.run_until_complete(
                    handler.chat_stream(message, on_token=on_token)
                )
                loop.close()
                result['response'] = response
                log(f"✅ Ответ получен: {len(response)} символов, {token_count[0]} токенов", "OK")
                log(f"   └─ \"{response[:150]}{'...' if len(response) > 150 else ''}\"", "OK")
                # Логируем полный ответ ассистента
                _write_dialogue_log(session_id, "ASSISTANT", response)
                token_queue.put(('done', response))
            except Exception as e:
                result['error'] = str(e)
                logger.exception("stream chat error session_id=%s", session_id)
                log(f"❌ ОШИБКА: {e}", "ERROR")
                _write_dialogue_log(session_id, "ERROR", str(e))
                token_queue.put(('error', str(e)))
        
        # Запускаем в отдельном потоке
        thread = threading.Thread(target=run_chat)
        thread.start()
        
        # Стримим токены
        while True:
            try:
                event_type, data = token_queue.get(timeout=60)
                
                if event_type == 'token':
                    yield f"data: {json.dumps({'type': 'token', 'content': data})}\n\n"
                elif event_type == 'done':
                    yield f"data: {json.dumps({'type': 'done', 'content': data})}\n\n"
                    break
                elif event_type == 'error':
                    yield f"data: {json.dumps({'type': 'error', 'content': data})}\n\n"
                    break
            except queue.Empty:
                log("⏳ Таймаут ожидания...", "WARN")
                yield f"data: {json.dumps({'type': 'ping'})}\n\n"
        
        thread.join()
    
    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no'
        }
    )


@app.route('/api/reset', methods=['POST'])
def reset():
    """Сбросить историю диалога"""
    data = request.json or {}
    session_id = data.get('session_id', 'default')
    
    with _handlers_lock:
        if session_id in _handlers:
            _handlers[session_id]["handler"].reset()
            log(f"🔄 Сессия {session_id[:8]}... сброшена", "WARN")
            _write_dialogue_log(session_id, "SYSTEM", "=== SESSION RESET ===")
    
    return jsonify({'status': 'ok'})


@app.route('/api/status')
def status():
    """Статус сервера"""
    with _handlers_lock:
        session_count = len(_handlers)
    return jsonify({
        'status': 'running',
        'sessions': session_count
    })


@app.route('/api/metrics')
def get_metrics():
    """
    Возвращает агрегированные метрики по всем активным сессиям.
    Используется для мониторинга качества работы AI-ассистента.
    """
    with _handlers_lock:
        aggregated = {
            "total_sessions": len(_handlers),
            "promised_search_detections": 0,
            "cascade_incomplete_detections": 0,
            "dateto_corrections": 0,
            "total_searches": 0,
            "total_messages": 0,
        }
        
        for session_data in _handlers.values():
            handler = session_data["handler"]
            metrics = handler.get_metrics()
            for key in ["promised_search_detections", "cascade_incomplete_detections", 
                        "dateto_corrections", "total_searches", "total_messages"]:
                aggregated[key] += metrics.get(key, 0)
        
        return jsonify(aggregated)


if __name__ == '__main__':
    import socket
    from werkzeug.serving import run_simple

    from dotenv import load_dotenv
    load_dotenv()
    
    model = os.getenv("YANDEX_MODEL", "yandexgpt")
    folder = os.getenv("YANDEX_FOLDER_ID", "???")
    
    print("\n" + "="*50)
    print("🚀 AI ТУРМЕНЕДЖЕР - Web UI")
    print("="*50)
    print(f"📍 URL: http://localhost:8080")
    print(f"🤖 Модель: {model}")
    print(f"📁 Folder: {folder[:8]}...")
    print(f"📝 Dialogue log: {_DIALOGUE_LOG_PATH}")
    print(f"📋 Server log: {os.path.join(_LOGS_DIR, 'server_*.log')}")
    print("="*50 + "\n")
    
    # Привязываемся к '::' (IPv6 dual-stack) — принимает и IPv4, и IPv6 соединения.
    # На macOS localhost -> ::1, поэтому без IPv6 браузер получает ERR_CONNECTION_RESET.
    run_simple('::', 8080, app, use_reloader=False, use_debugger=False, threaded=True)
