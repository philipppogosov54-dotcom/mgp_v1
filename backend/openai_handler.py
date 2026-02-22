"""
OpenAI GPT Handler — миграция с Yandex GPT на OpenAI (GPT-5 Mini)

Ключевые отличия от YandexGPTHandler:
- Нативный Function Calling (tool_calls) — НЕ нужен plaintext regex parsing
- Формат сообщений: role="tool" для результатов функций (а не role="user")
- OpenAI SDK вместо прямого HTTP к Yandex Completion API

Наследует ВСЮ бизнес-логику из YandexGPTHandler:
- _dispatch_function (~1200 строк маршрутизации TourVisor API)
- _execute_function (выполнение + логирование)
- _check_cascade_slots (проверка полноты каскада)
- Все safety-net правки (F1-F8, P1-P15, R6-R9, C2, H1-H2)
- _resolve_tourid_from_text, _dialogue_log, метрики
"""

import os
import json
import asyncio
import time
import logging
from typing import Optional, Dict, List
from openai import OpenAI
from dotenv import load_dotenv

try:
    from yandex_handler import (
        YandexGPTHandler,
        _is_promised_search,
        _dedup_response,
        _strip_reasoning_leak,
        _dedup_sentences,
        _strip_trailing_fragment,
        StreamCallback,
    )
except ImportError:
    from backend.yandex_handler import (
        YandexGPTHandler,
        _is_promised_search,
        _dedup_response,
        _strip_reasoning_leak,
        _dedup_sentences,
        _strip_trailing_fragment,
        StreamCallback,
    )

load_dotenv()

logger = logging.getLogger("mgp_bot")


class OpenAIHandler(YandexGPTHandler):
    """
    OpenAI GPT Handler с нативным Function Calling.

    Наследует:
    - _dispatch_function (вся бизнес-логика TourVisor API)
    - _execute_function (выполнение + логирование)
    - Все safety-net правки (F1-F8, P1-P15, R6-R9, C2, H1-H2)
    - _resolve_tourid_from_text, _dialogue_log, метрики, tour_cards

    Переопределяет:
    - __init__ (OpenAI SDK вместо Yandex HTTP)
    - chat() (нативные tool_calls вместо plaintext parsing)
    - chat_stream() (делегирует в chat())
    - close_sync(), reset()
    """

    def __init__(self):
        # Initialize all shared state from parent (tourvisor, history, metrics, etc.)
        super().__init__()

        # Validate OpenAI API key
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError(
                "OPENAI_API_KEY не указан в .env! "
                "Добавьте OPENAI_API_KEY=sk-... в backend/.env"
            )

        # Override with OpenAI client
        # OPENAI_BASE_URL — для прокси (если OpenAI API недоступен напрямую, напр. из России)
        base_url = os.getenv("OPENAI_BASE_URL")
        client_kwargs = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
            logger.info("🌐 OpenAI proxy: %s", base_url)

        self.openai_client = OpenAI(timeout=120.0, **client_kwargs)
        self.model = os.getenv("OPENAI_MODEL", "gpt-5-mini")

        # Build OpenAI-formatted tools from function_schemas.json
        self.openai_tools = self._build_openai_tools()

        logger.info(
            "🤖 OpenAIHandler INIT  model=%s  tools=%d",
            self.model, len(self.openai_tools)
        )

    # ─── Tools ────────────────────────────────────────────────────────────

    def _build_openai_tools(self) -> List[Dict]:
        """
        Convert function_schemas.json to OpenAI tools format.

        Yandex format:  {"type": "function", "name": "...", "parameters": {...}}
        OpenAI format:  {"type": "function", "function": {"name": "...", "parameters": {...}}}
        """
        schema_path = os.path.join(
            os.path.dirname(__file__), "..", "function_schemas.json"
        )
        with open(schema_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        openai_tools = []
        for tool in data.get("tools", []):
            if tool.get("type") == "function":
                openai_tools.append({
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": tool.get("parameters", {}),
                    }
                })

        logger.info("🔧 Loaded %d OpenAI tools from function_schemas.json", len(openai_tools))
        return openai_tools

    # ─── Messages Builder ─────────────────────────────────────────────────

    def _build_openai_messages(self) -> List[Dict]:
        """
        Build messages array for OpenAI Chat Completions API.

        Format:
        - {"role": "system", "content": "..."}          — system prompt
        - {"role": "user", "content": "..."}             — user messages
        - {"role": "assistant", "content": "..."}        — text responses
        - {"role": "assistant", "tool_calls": [...]}     — function calls
        - {"role": "tool", "tool_call_id": "...", ...}   — function results
        """
        messages = []

        # System prompt
        if self.instructions:
            messages.append({"role": "system", "content": self.instructions})

        # Full history
        for item in self.full_history:
            role = item.get("role")

            if role == "user":
                messages.append({
                    "role": "user",
                    "content": item.get("content", "")
                })
            elif role == "assistant":
                msg = {"role": "assistant", "content": item.get("content")}
                if "tool_calls" in item:
                    msg["tool_calls"] = item["tool_calls"]
                messages.append(msg)
            elif role == "tool":
                messages.append({
                    "role": "tool",
                    "tool_call_id": item.get("tool_call_id", ""),
                    "content": item.get("content", "")
                })

        return messages

    # ─── History Trimming (tool_call-aware) ───────────────────────────────

    def _trim_history(self):
        """
        Override: trim history while preserving tool_call/tool_result pairs.

        OpenAI API requires that every assistant message with tool_calls
        is immediately followed by tool results for ALL those calls.
        Splitting them causes HTTP 400 errors.
        """
        if len(self.full_history) <= self._max_history_len:
            return

        old_len = len(self.full_history)
        keep_start = 2
        keep_end = self._max_history_len - keep_start
        tail = self.full_history[-keep_end:]

        # Remove orphaned tool messages at the start of tail
        while tail and tail[0].get("role") == "tool":
            tail.pop(0)

        # If tail starts with assistant + tool_calls without complete results, remove it
        if (tail
                and tail[0].get("role") == "assistant"
                and tail[0].get("tool_calls")):
            tc_ids = {tc["id"] for tc in tail[0].get("tool_calls", [])}
            found_ids = set()
            j = 1
            while j < len(tail) and tail[j].get("role") == "tool":
                found_ids.add(tail[j].get("tool_call_id"))
                j += 1
            if tc_ids != found_ids:
                tail = tail[j:]

        self.full_history = self.full_history[:keep_start] + tail
        logger.info(
            "✂️ TRIM full_history: %d → %d messages",
            old_len, len(self.full_history)
        )

    # ─── OpenAI API Call ──────────────────────────────────────────────────

    def _call_openai_sync(self, messages: List[Dict]):
        """
        Synchronous OpenAI API call.
        Run in thread via asyncio.to_thread() to avoid blocking the event loop.
        """
        return self.openai_client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=self.openai_tools,
            temperature=0.2,
            max_tokens=4096,
        )

    # ─── Main Chat Loop ──────────────────────────────────────────────────

    async def chat(self, user_message: str) -> str:
        """
        Send message and get response using OpenAI GPT with native tool calling.

        Key differences from YandexGPTHandler.chat():
        - No plaintext function call parsing (tool_calls are native JSON)
        - No content filter bypass (OpenAI doesn't have Yandex's content filter)
        - No role alternation hacks (_append_history not needed)
        - Tool results stored as role="tool" (not role="user")
        """
        # Reset tour cards for this message
        self._pending_tour_cards = []
        self._metrics["total_messages"] += 1

        # Add user message to history
        self.full_history.append({"role": "user", "content": user_message})
        self._trim_history()

        logger.info(
            "👤 USER >> \"%s\"  full_history=%d  model=%s",
            user_message[:150], len(self.full_history), self.model
        )

        max_iterations = 15
        iteration = 0
        chat_start = time.perf_counter()
        empty_retries = 0
        timeout_retries = 0

        while iteration < max_iterations:
            iteration += 1
            messages = self._build_openai_messages()

            logger.info(
                "🔄 ITERATION %d/%d  messages=%d  model=%s",
                iteration, max_iterations, len(messages), self.model
            )

            t0 = time.perf_counter()
            try:
                response = await asyncio.to_thread(
                    self._call_openai_sync, messages
                )
                api_ms = int((time.perf_counter() - t0) * 1000)

                choice = response.choices[0]
                message = choice.message
                finish_reason = choice.finish_reason

                # Token usage logging
                usage = response.usage
                if usage:
                    logger.info(
                        "🤖 OPENAI API <<  %dms  finish=%s  "
                        "tokens: prompt=%d completion=%d total=%d",
                        api_ms, finish_reason,
                        usage.prompt_tokens, usage.completion_tokens,
                        usage.total_tokens
                    )
                else:
                    logger.info(
                        "🤖 OPENAI API <<  %dms  finish=%s",
                        api_ms, finish_reason
                    )

            except Exception as e:
                api_ms = int((time.perf_counter() - t0) * 1000)
                error_str = str(e)
                logger.error(
                    "🤖 OPENAI API !! ERROR  %dms  %s",
                    api_ms, error_str[:300]
                )

                # Rate limit
                if "429" in error_str or "rate_limit" in error_str.lower():
                    return (
                        "Сервис временно перегружен. "
                        "Подождите несколько секунд и повторите."
                    )

                # Token limit exceeded
                if ("context_length_exceeded" in error_str
                        or "maximum context length" in error_str
                        or "max_tokens" in error_str.lower()):
                    logger.warning(
                        "⚠️ TOKEN LIMIT EXCEEDED — trimming history "
                        "from %d messages",
                        len(self.full_history)
                    )
                    if len(self.full_history) > 8:
                        head = self.full_history[:2]
                        tail = self.full_history[-4:]
                        # Remove orphaned tool messages at start of tail
                        while tail and tail[0].get("role") == "tool":
                            tail.pop(0)
                        self.full_history = head + tail
                        logger.info(
                            "✅ History trimmed to %d messages",
                            len(self.full_history)
                        )
                    empty_retries += 1
                    if empty_retries < 3:
                        continue
                    return (
                        "Извините, диалог стал слишком длинным. "
                        "Пожалуйста, начните новый чат."
                    )

                # Invalid request (orphaned tool message, malformed history)
                if "400" in error_str or "invalid" in error_str.lower():
                    logger.warning(
                        "⚠️ 400 ERROR — attempting history cleanup"
                    )
                    self._cleanup_history()
                    empty_retries += 1
                    if empty_retries < 3:
                        continue

                # Timeout (server-side, e.g. OpenRouter)
                if "timed out" in error_str.lower() or "timeout" in error_str.lower():
                    timeout_retries += 1
                    if timeout_retries < 2:
                        logger.warning(
                            "⏱️ TIMEOUT RETRY %d/2 — повтор через 2с",
                            timeout_retries
                        )
                        await asyncio.sleep(2)
                        continue

                return (
                    "Произошла временная ошибка. "
                    "Попробуйте ещё раз или начните новый чат."
                )

            # ── Handle tool calls (native) ──
            if message.tool_calls:
                # Store assistant message with tool_calls in history
                assistant_msg = {
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            }
                        }
                        for tc in message.tool_calls
                    ]
                }
                self.full_history.append(assistant_msg)

                # Log
                func_names = [tc.function.name for tc in message.tool_calls]
                logger.info(
                    "🔧 TOOL CALLS: %s", ", ".join(func_names)
                )

                # Оптимизация: параллельное выполнение tool calls
                if len(message.tool_calls) == 1:
                    # Один вызов — выполняем напрямую
                    tc = message.tool_calls[0]
                    arguments = tc.function.arguments or "{}"
                    result = await self._execute_function(
                        tc.function.name, arguments, tc.id
                    )
                    self.full_history.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result["output"]
                    })
                else:
                    # Несколько вызовов — выполняем параллельно через asyncio.gather
                    async def _exec_tool_call(tool_call):
                        args = tool_call.function.arguments or "{}"
                        return tool_call.id, await self._execute_function(
                            tool_call.function.name, args, tool_call.id
                        )
                    
                    results = await asyncio.gather(*[
                        _exec_tool_call(tc) for tc in message.tool_calls
                    ])
                    
                    # Добавляем результаты в историю в том же порядке
                    for tc_id, result in results:
                        self.full_history.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": result["output"]
                        })

                logger.info(
                    "🔄 TOOL CALLS DONE  count=%d  continuing…",
                    len(message.tool_calls)
                )
                continue

            # ── Handle text response ──
            final_text = message.content or ""

            # Content filter (OpenAI)
            if finish_reason == "content_filter":
                empty_retries += 1
                logger.warning(
                    "⚠️ CONTENT_FILTER detected (#%d): \"%s\"",
                    empty_retries, final_text[:100]
                )
                if empty_retries >= 3:
                    return (
                        "Извините, произошла ошибка. "
                        "Попробуйте переформулировать запрос."
                    )
                self.full_history.append({
                    "role": "user",
                    "content": (
                        "Пожалуйста, продолжи помогать "
                        "с подбором тура."
                    )
                })
                continue

            # Truncated response (max_tokens)
            if finish_reason == "length" and final_text:
                logger.warning(
                    "⚠️ Response truncated (max_tokens). "
                    "Length: %d chars", len(final_text)
                )

            # Empty response
            if not final_text:
                empty_retries += 1
                logger.warning(
                    "⚠️ EMPTY RESPONSE #%d", empty_retries
                )
                if empty_retries >= 3:
                    if self._pending_tour_cards:
                        return (
                            "Вот что нашёл по вашему запросу! "
                            "Посмотрите варианты и скажите, "
                            "какой заинтересовал — расскажу подробнее."
                        )
                    return (
                        "Извините, не удалось обработать запрос. "
                        "Попробуйте переформулировать."
                    )
                self.full_history.append({
                    "role": "user",
                    "content": (
                        "Продолжи обработку моего запроса "
                        "на основе полученных данных."
                    )
                })
                continue

            # Promised search detection (safety-net)
            if _is_promised_search(final_text):
                empty_retries += 1
                self._metrics["promised_search_detections"] = \
                    self._metrics.get("promised_search_detections", 0) + 1
                logger.warning(
                    "⚠️ PROMISED-SEARCH detected (#%d): \"%s\"",
                    empty_retries, final_text[:150]
                )
                if empty_retries < 2:
                    self.full_history.append({
                        "role": "assistant", "content": final_text
                    })
                    self.full_history.append({
                        "role": "user",
                        "content": (
                            "СИСТЕМНАЯ ОШИБКА: Ты ОПИСАЛ намерение "
                            "поиска текстом, но НЕ вызвал функцию. "
                            "НЕМЕДЛЕННО вызови get_current_date(), "
                            "затем search_tours() с собранными "
                            "параметрами. НИКОГДА не пиши "
                            "'сейчас поищу' — ВЫЗЫВАЙ функцию!"
                        )
                    })
                    continue

            # Search pipeline break detection (safety-net)
            if getattr(self, '_search_awaiting_results', False):
                logger.warning(
                    "⚠️ SEARCH-PIPELINE-BREAK: model stopped without get_search_results"
                )
                empty_retries += 1
                if empty_retries < 3:
                    self.full_history.append({
                        "role": "assistant", "content": final_text
                    })
                    self.full_history.append({
                        "role": "user",
                        "content": (
                            f"СИСТЕМНАЯ ОШИБКА: search_tours вернул requestid, "
                            f"но ты НЕ вызвал get_search_status и get_search_results. "
                            f"НЕМЕДЛЕННО вызови get_search_status(requestid="
                            f"{self._last_requestid}). НЕ отвечай клиенту пока "
                            f"не получишь результаты через get_search_results!"
                        )
                    })
                    continue
                else:
                    self._search_awaiting_results = False

            # Result leak detection (safety-net)
            if final_text.lstrip().startswith("Результаты запросов"):
                logger.warning("⚠️ RESULT-LEAK detected")
                self._metrics.setdefault("result_leak_filtered", 0)
                self._metrics["result_leak_filtered"] += 1
                if self._pending_tour_cards:
                    final_text = (
                        "Вот что нашёл по вашему запросу! "
                        "Посмотрите варианты и скажите, "
                        "какой заинтересовал — расскажу подробнее."
                    )
                else:
                    empty_retries += 1
                    if empty_retries < 3:
                        self.full_history.append({
                            "role": "assistant", "content": final_text
                        })
                        self.full_history.append({
                            "role": "user",
                            "content": (
                                "Ответь клиенту нормальным текстом — "
                                "НЕ показывай сырые данные функций. "
                                "Если нужно вызвать ещё функцию — вызови."
                            )
                        })
                        continue
                    final_text = "Я обработал ваш запрос. Чем могу помочь?"

            # Dedup (safety-net, unlikely with OpenAI but harmless)
            final_text = _dedup_response(final_text)

            # Strip leaked LLM reasoning / JSON fragments from end of response
            final_text = _strip_reasoning_leak(final_text)

            # Sentence-level dedup (catches intra-paragraph question repeats)
            final_text = _dedup_sentences(final_text)

            # Strip orphaned dialogue-continuation fragments after last '?'
            final_text = _strip_trailing_fragment(final_text)

            # Save to history
            self.full_history.append({
                "role": "assistant", "content": final_text
            })

            total_ms = int((time.perf_counter() - chat_start) * 1000)
            logger.info(
                "🤖 ASSISTANT << %d chars  %d iterations  %dms total  \"%s\"",
                len(final_text), iteration, total_ms,
                final_text[:200] + ("…" if len(final_text) > 200 else "")
            )
            return final_text

        logger.error("🤖 MAX ITERATIONS REACHED (%d)", max_iterations)
        return "Ошибка: превышено количество итераций Function Calling"

    # ─── History Cleanup ──────────────────────────────────────────────────

    def _cleanup_history(self):
        """
        Remove invalid message sequences from full_history.
        Fixes orphaned tool messages and incomplete tool_call groups.
        """
        cleaned = []
        i = 0
        while i < len(self.full_history):
            msg = self.full_history[i]

            if msg.get("role") == "tool":
                # Only keep if previous is assistant with matching tool_calls
                if (cleaned
                        and cleaned[-1].get("role") == "assistant"
                        and cleaned[-1].get("tool_calls")):
                    tc_ids = {
                        tc["id"]
                        for tc in cleaned[-1]["tool_calls"]
                    }
                    if msg.get("tool_call_id") in tc_ids:
                        cleaned.append(msg)
                        i += 1
                        continue
                # Orphaned tool message — skip
                logger.debug(
                    "🧹 CLEANUP: skipping orphaned tool message "
                    "tool_call_id=%s",
                    msg.get("tool_call_id", "?")
                )
                i += 1
                continue

            cleaned.append(msg)
            i += 1

        if len(cleaned) != len(self.full_history):
            logger.info(
                "🧹 CLEANUP: %d → %d messages (removed %d invalid)",
                len(self.full_history), len(cleaned),
                len(self.full_history) - len(cleaned)
            )
        self.full_history = cleaned

    # ─── Streaming (fallback to non-streaming) ────────────────────────────

    async def chat_stream(
        self,
        user_message: str,
        on_token: Optional[StreamCallback] = None
    ) -> str:
        """
        Streaming not yet implemented for OpenAI.
        Falls back to regular chat().
        """
        logger.warning(
            "⚠️ chat_stream() fallback to chat() — "
            "streaming не реализован для OpenAI"
        )
        result = await self.chat(user_message)
        if on_token:
            on_token(result)
        return result

    # ─── Lifecycle ────────────────────────────────────────────────────────

    def close_sync(self):
        """Close OpenAI client resources."""
        try:
            self.openai_client.close()
        except Exception:
            pass

    def reset(self):
        """Reset dialogue history and all caches."""
        old_len = len(self.full_history)
        self.full_history = []
        self.input_list = []
        self._pending_tour_cards = []
        self._last_departure_city = "Москва"
        self._last_requestid = None
        self._tourid_map = {}
        self._last_search_params = {}
        self._user_stated_budget = None
        self._empty_iterations = 0
        self.previous_response_id = None
        self._metrics = {
            "promised_search_detections": 0,
            "cascade_incomplete_detections": 0,
            "dateto_corrections": 0,
            "total_searches": 0,
            "total_messages": 0,
        }
        logger.info(
            "🔄 HANDLER RESET  cleared %d messages from full_history",
            old_len
        )
