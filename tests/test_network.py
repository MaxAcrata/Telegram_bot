"""Сетевые повторы, классификация ошибок и TelegramCallResult (блок A5/A6)."""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

from bot import (
    _network_kind,
    _safe_answer,
    _safe_edit,
    _safe_reply,
    _tg_call,
)


def _with_cause(exc: BaseException, cause: BaseException) -> BaseException:
    """Устанавливает первопричину как PTB делает через `raise ... from err`."""
    exc.__cause__ = cause
    return exc


def make_update():
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    update.effective_user.id = 111
    update.effective_chat.id = 222
    return update


def make_query():
    query = MagicMock()
    query.edit_message_text = AsyncMock()
    query.answer = AsyncMock()
    query.from_user.id = 111
    return query


# ===== Требование 9: первый вызов TimedOut, следующая попытка успешна =====
@pytest.mark.asyncio
async def test_safe_reply_timedout_connect_then_success():
    update = make_update()
    err = _with_cause(TimedOut("timed out"), httpx.ConnectTimeout("connect"))
    update.message.reply_text.side_effect = [err, "MSG"]

    result = await _safe_reply(update, "текст")

    assert result.success is True
    assert result.delivery_uncertain is False
    assert result.value == "MSG"
    assert update.message.reply_text.call_count == 2


# ===== Требование 10: все попытки TimedOut — явный провал без зависаний =====
@pytest.mark.asyncio
async def test_safe_reply_all_timedout_fails_after_bounded_retries():
    update = make_update()
    err = _with_cause(TimedOut("timed out"), httpx.ConnectTimeout("connect"))
    update.message.reply_text.side_effect = err

    result = await _safe_reply(update, "текст")

    assert result.success is False
    # connect: доставка гарантированно не состоялась — исход определён
    assert result.delivery_uncertain is False
    assert isinstance(result.error, TimedOut)
    # 1 попытка + ровно 1 повтор (без бесконечных повторов)
    assert update.message.reply_text.call_count == 2


@pytest.mark.asyncio
async def test_safe_reply_read_timeout_no_retry_and_uncertain():
    """read timeout: Telegram мог принять сообщение — повтор запрещён."""
    update = make_update()
    err = _with_cause(TimedOut("timed out"), httpx.ReadTimeout("read"))
    update.message.reply_text.side_effect = err

    result = await _safe_reply(update, "текст")

    assert result.success is False
    assert result.delivery_uncertain is True
    assert update.message.reply_text.call_count == 1


# ===== Требование 11: RetryAfter выдерживает retry_after =====
@pytest.mark.asyncio
async def test_safe_reply_retryafter_honors_delay():
    update = make_update()
    update.message.reply_text.side_effect = [RetryAfter(1), "MSG"]

    with patch("bot.asyncio.sleep", new=AsyncMock()) as sleep_mock:
        result = await _safe_reply(update, "текст")

    sleep_mock.assert_awaited_once_with(1.0)
    assert result.success is True
    assert update.message.reply_text.call_count == 2


@pytest.mark.asyncio
async def test_safe_reply_retryafter_budget_exhausted():
    """RetryAfter не повторяется бесконечно."""
    update = make_update()
    update.message.reply_text.side_effect = RetryAfter(1)

    with patch("bot.asyncio.sleep", new=AsyncMock()) as sleep_mock:
        result = await _safe_reply(update, "текст")

    assert result.success is False
    # 1 исходная попытка + 2 разрешённых повтора с ожиданием
    assert sleep_mock.await_count == 2
    assert update.message.reply_text.call_count == 3


# ===== Требование 12: BadRequest не повторяется =====
@pytest.mark.asyncio
async def test_safe_edit_badrequest_no_retry():
    query = make_query()
    query.edit_message_text.side_effect = BadRequest("Bad Request: broken")

    result = await _safe_edit(query, "текст")

    assert result.success is False
    assert result.delivery_uncertain is False
    assert query.edit_message_text.call_count == 1


@pytest.mark.asyncio
async def test_safe_reply_badrequest_no_retry():
    update = make_update()
    update.message.reply_text.side_effect = BadRequest("Bad Request: chat not found")

    result = await _safe_reply(update, "текст")

    assert result.success is False
    assert update.message.reply_text.call_count == 1


@pytest.mark.asyncio
async def test_edit_message_not_modified_is_success():
    """BadRequest: Message is not modified считается успешным edit."""
    query = make_query()
    query.edit_message_text.side_effect = BadRequest(
        "Message is not modified: not modified"
    )

    result = await _safe_edit(query, "текст")

    assert result.success is True
    assert query.edit_message_text.call_count == 1


# ===== Классификация первопричин (isinstance, не .type) =====
@pytest.mark.parametrize(
    "cause,expected",
    [
        (httpx.ConnectTimeout("x"), "connect"),
        (httpx.ReadTimeout("x"), "read"),
        (httpx.WriteTimeout("x"), "write"),
        (httpx.PoolTimeout("x"), "pool"),
        (httpx.ConnectError("x"), "connect"),
        (httpx.ReadError("x"), "read"),
        (httpx.WriteError("x"), "write"),
    ],
)
def test_network_kind_classification(cause, expected):
    exc = TimedOut("timed out")
    exc.__cause__ = cause
    assert _network_kind(exc) == expected


def test_network_kind_unknown_without_cause():
    assert _network_kind(TimedOut("timed out")) == "unknown"
    assert _network_kind(NetworkError("boom")) == "unknown"


@pytest.mark.asyncio
async def test_network_kind_used_for_state_policy():
    """NetworkError с read-первопричиной: без повтора, доставка неизвестна."""
    update = make_update()
    err = NetworkError("httpx.ReadError: x")
    err.__cause__ = httpx.ReadError("x")
    update.message.reply_text.side_effect = err

    result = await _safe_reply(update, "текст")

    assert result.success is False
    assert result.delivery_uncertain is True
    assert update.message.reply_text.call_count == 1


@pytest.mark.asyncio
async def test_networkerror_connect_retries_once():
    update = make_update()
    err = NetworkError("httpx.ConnectError: x")
    err.__cause__ = httpx.ConnectError("x")
    update.message.reply_text.side_effect = [err, "MSG"]

    result = await _safe_reply(update, "текст")

    assert result.success is True
    assert update.message.reply_text.call_count == 2


# ===== best-effort ответ на callback =====
@pytest.mark.asyncio
async def test_safe_answer_connect_retries_once():
    query = make_query()
    err = _with_cause(TimedOut("timed out"), httpx.ConnectTimeout("connect"))
    query.answer.side_effect = [err, True]

    result = await _safe_answer(query)

    assert result.success is True
    assert query.answer.call_count == 2


@pytest.mark.asyncio
async def test_safe_answer_read_timeout_no_retry():
    query = make_query()
    err = _with_cause(TimedOut("timed out"), httpx.ReadTimeout("read"))
    query.answer.side_effect = err

    result = await _safe_answer(query)

    assert result.success is False
    assert result.delivery_uncertain is True
    assert query.answer.call_count == 1


# ===== A6: TimedOut логируется с первопричиной =====
@pytest.mark.asyncio
async def test_timedout_logs_cause_kind_and_attempt(caplog):
    update = make_update()
    err = _with_cause(TimedOut("timed out"), httpx.ConnectTimeout("connect"))
    update.message.reply_text.side_effect = err

    with caplog.at_level(logging.DEBUG, logger="bot"):
        await _safe_reply(update, "текст")

    text = caplog.text
    assert "op=_safe_reply" in text
    assert "error=TimedOut" in text
    assert "cause=ConnectTimeout" in text
    assert "kind=connect" in text
    assert "attempt=" in text


# ===== Движок: идемпотентный успешный вызов и неизвестная первопричина =====
@pytest.mark.asyncio
async def test_tg_call_unknown_kind_is_conservative():
    """Нераспознанная первопричина — без повтора (консервативная политика)."""
    factory = AsyncMock(side_effect=TimedOut("no cause attached"))

    result = await _tg_call("op", factory, kind="send")

    assert result.success is False
    assert result.delivery_uncertain is True
    assert factory.await_count == 1
