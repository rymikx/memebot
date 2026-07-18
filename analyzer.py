"""
analyzer.py — сбор данных о мем-токене и расчёт инвестиционного скора.

Источники данных:
- DexScreener API (без ключа) — ликвидность, объём, цена, возраст пары
- GoPlus Security API (без ключа) — безопасность контракта, держатели

Все внешние вызовы обёрнуты в try/except: если какой-то источник недоступен
или не поддерживает сеть/токен — соответствующий блок скора помечается как
"N/A" и исключается из финального расчёта (среднее считается по доступным
блокам, а не по фиксированным 10 баллам).
"""

import time
import asyncio
import logging
import datetime
import aiohttp

logger = logging.getLogger(__name__)

# Основной способ — поиск по тексту без привязки к конкретной сети.
# Проверено по официальной документации DexScreener на июль 2026.
# Раньше использовался /latest/dex/tokens/{address} — DexScreener убрали его
# из документации, поэтому старая версия бота перестала находить токены.
DEXSCREENER_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search?q={address}"

# Запасной способ — /latest/dex/search индексирует в первую очередь текстовые
# запросы вида "SOL/USDC", а не гарантированно полные адреса контрактов.
# Если поиск ничего не нашёл, опрашиваем этот эндпоинт напрямую по каждой
# поддерживаемой сети — он принимает конкретный chainId и tokenAddress и
# работает надёжно, если сеть угадана верно. Возвращает JSON-массив пар
# напрямую (без обёртки {"pairs": [...]}), в отличие от /latest/dex/search.
DEXSCREENER_TOKEN_PAIRS_URL = "https://api.dexscreener.com/token-pairs/v1/{chain}/{address}"

# Второй, независимый источник (от команды CoinGecko) — у него отдельная
# инфраструктура, отдельный от DexScreener пул IP/серверов. Используем как
# финальный запасной вариант, если DexScreener стабильно отвечает 429
# (это бывает на бесплатном Render — общий IP "делится" с другими
# бесплатными сервисами, и это вне нашего контроля со стороны кода).
# Лимит ниже (30 запросов/мин), но нам за раз нужно 1-2 запроса — хватает.
GECKOTERMINAL_POOLS_URL = "https://api.geckoterminal.com/api/v2/networks/{network}/tokens/{address}/pools"

# Наши внутренние chain-слаги (совпадают с DexScreener) -> id сети в GeckoTerminal
GECKOTERMINAL_NETWORK_MAP = {
    "ethereum": "eth",
    "bsc": "bsc",
    "polygon": "polygon_pos",
    "arbitrum": "arbitrum",
    "base": "base",
    "avalanche": "avax",
    "optimism": "optimism",
    "fantom": "ftm",
    "solana": "solana",
    "ton": "ton",
    "robinhood": "robinhood",  # сеть очень новая — если GeckoTerminal ещё не
    # добавил поддержку, запрос просто вернёт пусто/404, ничего не сломается
}


# chainId-слаги DexScreener (не путать с GoPlus chain_id из карты ниже).
SUPPORTED_DEXSCREENER_CHAINS = [
    "ethereum", "bsc", "polygon", "arbitrum", "base", "avalanche",
    "optimism", "fantom", "robinhood", "solana", "ton",
]

# chainId (DexScreener) -> chain_id (GoPlus, для EVM-сетей)
# Примечание: "robinhood" (Robinhood Chain, chainId 4663, Arbitrum Orbit L2,
# запущена 01.07.2026) добавлена сюда на всякий случай — GoPlus может ещё не
# поддерживать такую молодую сеть. Если не поддерживает, get_goplus_security
# просто вернёт None, и блок безопасности/держателей корректно уйдёт в N/A —
# бот не упадёт и не соврёт о наличии данных.
GOPLUS_EVM_CHAIN_MAP = {
    "ethereum": "1",
    "bsc": "56",
    "polygon": "137",
    "arbitrum": "42161",
    "base": "8453",
    "avalanche": "43114",
    "optimism": "10",
    "fantom": "250",
    "robinhood": "4663",
}

# TON (The Open Network) — не EVM и не Solana, у GoPlus нет отдельного
# эндпоинта для TON на момент написания бота. DexScreener данные (chainId
# "ton") при этом доступны и обрабатываются как обычно. get_goplus_security
# для chain_id_dexscreener == "ton" автоматически вернёт None (не найдётся
# ни в GOPLUS_EVM_CHAIN_MAP, ни в ветке Solana) — контракт-секьюрити и
# держатели будут честно помечены N/A, без сетевого вызова впустую.

GOPLUS_EVM_URL = "https://api.gopluslabs.io/api/v1/token_security/{chain_id}?contract_addresses={address}"
GOPLUS_SOLANA_URL = "https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses={address}"


class AnalysisError(Exception):
    pass


async def fetch_json(session: aiohttp.ClientSession, url: str, timeout: int = 10, retries: int = 2):
    """Делает GET-запрос и парсит JSON. При 429 (слишком много запросов)
    делает паузу и повторяет — этого не было раньше, и именно поэтому бот
    массово падал на 429, когда DexScreener банил пачку из 11 одновременных
    запросов (это подтвердилось по логам с реальным адресом пользователя).
    """
    for attempt in range(retries + 1):
        try:
            async with session.get(url, timeout=timeout) as resp:
                if resp.status == 429:
                    wait = 1.5 * (attempt + 1)
                    logger.warning(
                        "fetch_json: 429 (rate limit) для %s, попытка %d/%d, жду %.1fс",
                        url, attempt + 1, retries + 1, wait,
                    )
                    if attempt < retries:
                        await asyncio.sleep(wait)
                        continue
                    return None
                if resp.status != 200:
                    try:
                        body_preview = (await resp.text())[:200]
                    except Exception:
                        body_preview = "<не удалось прочитать тело ответа>"
                    logger.warning(
                        "fetch_json: НЕ 200 статус %s для %s | тело: %s",
                        resp.status, url, body_preview,
                    )
                    return None
                data = await resp.json()
                logger.info(
                    "fetch_json: OK (200) для %s | ключи ответа: %s",
                    url,
                    list(data.keys()) if isinstance(data, dict) else f"list[{len(data)}]",
                )
                return data
        except Exception as e:
            logger.warning("fetch_json: ИСКЛЮЧЕНИЕ при запросе %s: %r", url, e)
            return None
    return None


def _addr_matches(candidate: str, address: str, is_evm: bool) -> bool:
    if not candidate:
        return False
    if is_evm:
        return candidate.lower() == address.lower()
    return candidate == address


def _pick_best_pair(pairs: list) -> dict | None:
    pairs = [p for p in pairs if p.get("liquidity", {}).get("usd") is not None]
    if not pairs:
        return None
    return max(pairs, key=lambda p: p["liquidity"]["usd"])


async def _search_by_text(session: aiohttp.ClientSession, address: str):
    """Основной способ: /latest/dex/search?q=<адрес>.

    Может не находить редкие/новые токены, если DexScreener не индексирует
    полный адрес как текст поиска — тогда возвращает None, и вызывающий
    код переходит к запасному способу (прямой опрос по сетям).
    """
    data = await fetch_json(session, DEXSCREENER_SEARCH_URL.format(address=address))
    if not data or not data.get("pairs"):
        return None

    is_evm = address.startswith("0x") or address.startswith("0X")
    pairs = [
        p
        for p in data["pairs"]
        if _addr_matches(p.get("baseToken", {}).get("address", ""), address, is_evm)
        or _addr_matches(p.get("quoteToken", {}).get("address", ""), address, is_evm)
    ]
    return _pick_best_pair(pairs)


def _confident_single_chain(address: str) -> str | None:
    """Если по формату адреса сеть определяется однозначно (TON или Solana),
    возвращаем её — тогда можно сразу идти в fallback, без траты запросов
    на текстовый поиск, который для таких адресов, судя по логам, ни разу
    не сработал (только тратил впустую 3 попытки и время на retry).
    Для EVM-адресов (0x...) сеть неоднозначна — возвращаем None.
    """
    is_ton = (
        (len(address) == 48 and address[:2] in ("EQ", "UQ", "kQ", "0Q"))
        or address.count(":") == 1 and address.split(":")[0].lstrip("-").isdigit()
    )
    if is_ton:
        return "ton"
    is_evm = address.startswith("0x") or address.startswith("0X")
    if not is_evm:
        return "solana"
    return None


def _guess_candidate_chains(address: str) -> list:
    """Определяем, какие сети реально имеет смысл проверять, по формату
    адреса — вместо того чтобы долбить все 11 сетей подряд (именно это,
    судя по логам, стабильно вызывало бан по 429 на общем IP Render).
    """
    confident = _confident_single_chain(address)
    if confident:
        return [confident]
    # EVM: самые популярные для мемкоинов сначала
    return ["ethereum", "base", "bsc", "arbitrum", "robinhood", "polygon", "avalanche", "optimism", "fantom"]


async def _fetch_pairs_for_chain(session: aiohttp.ClientSession, chain: str, address: str, retries: int = 2):
    url = DEXSCREENER_TOKEN_PAIRS_URL.format(chain=chain, address=address)
    data = await fetch_json(session, url, retries=retries)
    # этот эндпоинт возвращает JSON-массив напрямую, а не {"pairs": [...]}
    if not data or not isinstance(data, list):
        return []
    return data


def _geckoterminal_item_to_pair(item: dict, chain: str) -> dict:
    """Приводит формат ответа GeckoTerminal к тому же виду dict, который
    возвращает DexScreener (liquidity.usd, volume.h24, pairCreatedAt в мс,
    fdv, priceChange.h24, chainId, ...) — чтобы все score_* функции ниже
    работали одинаково независимо от того, откуда пришли данные.
    """
    attrs = item.get("attributes", {}) or {}

    def _num(key, sub=None):
        try:
            v = attrs.get(key)
            if sub and isinstance(v, dict):
                v = v.get(sub)
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    created_ms = None
    created_str = attrs.get("pool_created_at")
    if created_str:
        try:
            dt = datetime.datetime.fromisoformat(created_str.replace("Z", "+00:00"))
            created_ms = dt.timestamp() * 1000
        except Exception:
            created_ms = None

    name = attrs.get("name") or ""
    symbol = name.split(" / ")[0].strip() if " / " in name else (name or None)

    return {
        "chainId": chain,
        "dexId": "geckoterminal",
        "baseToken": {"symbol": symbol, "name": symbol, "address": None},
        "priceUsd": attrs.get("base_token_price_usd"),
        "liquidity": {"usd": _num("reserve_in_usd")},
        "volume": {"h24": _num("volume_usd", "h24")},
        "pairCreatedAt": created_ms,
        "fdv": _num("fdv_usd") or _num("market_cap_usd"),
        "priceChange": {"h24": _num("price_change_percentage", "h24")},
        "url": attrs.get("address") and f"https://www.geckoterminal.com/{chain}/pools/{attrs.get('address')}",
    }


async def _fetch_geckoterminal_pools(session: aiohttp.ClientSession, chain: str, address: str, retries: int = 2):
    gt_network = GECKOTERMINAL_NETWORK_MAP.get(chain)
    if not gt_network:
        return []
    url = GECKOTERMINAL_POOLS_URL.format(network=gt_network, address=address)
    data = await fetch_json(session, url, retries=retries)
    if not data or "data" not in data or not isinstance(data["data"], list):
        return []
    return [_geckoterminal_item_to_pair(item, chain) for item in data["data"]]


async def _scan_candidate_chains(session: aiohttp.ClientSession, address: str, retries_per_chain: int = 2):
    """Запасной способ: проверяем только те сети, где адрес реально может
    существовать (по формату), а не все 11 подряд — резко меньше запросов
    к DexScreener, что и вызывало устойчивый бан по 429 на общем IP.

    Идём по очереди (не параллельно) и останавливаемся при первой находке —
    для TON/Solana это всего 1 сеть, для EVM — до 9, но обычно находится
    в первых 1-3 (Ethereum/Base/BSC — самые ходовые для мемов).
    """
    candidates = _guess_candidate_chains(address)
    logger.info("Проверяю сети-кандидаты для %s: %s", address, candidates)
    for chain in candidates:
        pairs = await _fetch_pairs_for_chain(session, chain, address, retries=retries_per_chain)
        best = _pick_best_pair(pairs)
        if best:
            return best
    return None


async def get_dexscreener_data(session: aiohttp.ClientSession, address: str):
    """Возвращает лучшую (по ликвидности) торговую пару для адреса токена.

    Порядок попыток:
    1. Если сеть определяется однозначно по формату адреса (TON/Solana) —
       сразу прямой запрос в DexScreener по этой сети, без траты запросов
       на текстовый поиск.
    2. Иначе (EVM) — сначала текстовый поиск DexScreener, потом перебор
       популярных EVM-сетей напрямую.
    3. Если DexScreener так и не ответил (стабильный 429 — известная
       проблема на бесплатном Render, где IP общий с другими сервисами) —
       пробуем GeckoTerminal: у него отдельная инфраструктура, отдельный
       пул IP, так что шанс получить ответ там выше.
    """
    confident_chain = _confident_single_chain(address)

    if confident_chain:
        logger.info(
            "get_dexscreener_data: сеть определена однозначно по формату (%s), "
            "пропускаю текстовый поиск, иду сразу в fallback с доп. попытками",
            confident_chain,
        )
        result = await _scan_candidate_chains(session, address, retries_per_chain=4)
        chains_to_try_gt = [confident_chain]
    else:
        result = await _search_by_text(session, address)
        if result:
            logger.info("get_dexscreener_data: найдено через текстовый поиск: %s", address)
            return result
        logger.info("get_dexscreener_data: текстовый поиск ничего не дал для %s, пробую fallback по сетям", address)
        result = await _scan_candidate_chains(session, address)
        chains_to_try_gt = _guess_candidate_chains(address)

    if result:
        logger.info("get_dexscreener_data: найдено через DexScreener: %s (сеть %s)", address, result.get("chainId"))
        return result

    logger.warning("get_dexscreener_data: DexScreener не нашёл (%s), пробую GeckoTerminal как запасной источник", address)
    for chain in chains_to_try_gt:
        pairs = await _fetch_geckoterminal_pools(session, chain, address)
        best = _pick_best_pair(pairs)
        if best:
            logger.info("get_dexscreener_data: найдено через GeckoTerminal: %s (сеть %s)", address, chain)
            return best

    logger.warning("get_dexscreener_data: НЕ найдено нигде (ни DexScreener, ни GeckoTerminal): %s", address)
    return None


async def get_goplus_security(session: aiohttp.ClientSession, chain_id_dexscreener: str, address: str):
    """Возвращает сырой ответ GoPlus для EVM или Solana, либо None, если сеть не поддержана."""
    address_lower = address.lower()
    if chain_id_dexscreener == "solana":
        data = await fetch_json(session, GOPLUS_SOLANA_URL.format(address=address))
        if not data or "result" not in data:
            return None
        result = data["result"].get(address) or data["result"].get(address_lower)
        return result
    goplus_chain = GOPLUS_EVM_CHAIN_MAP.get(chain_id_dexscreener)
    if not goplus_chain:
        return None
    data = await fetch_json(
        session, GOPLUS_EVM_URL.format(chain_id=goplus_chain, address=address_lower)
    )
    if not data or "result" not in data:
        return None
    result = data["result"].get(address_lower)
    return result


def score_liquidity(pair: dict):
    liq = pair.get("liquidity", {}).get("usd")
    if liq is None:
        return None, "Ликвидность неизвестна"
    if liq >= 100_000:
        s = 2.0
    elif liq >= 20_000:
        s = 1.5
    elif liq >= 5_000:
        s = 1.0
    elif liq >= 1_000:
        s = 0.5
    else:
        s = 0.0
    return s, f"Ликвидность в пуле: ${liq:,.0f}"


def score_volume(pair: dict):
    liq = pair.get("liquidity", {}).get("usd")
    vol24 = pair.get("volume", {}).get("h24")
    if liq is None or vol24 is None or liq == 0:
        return None, "Объём торгов неизвестен"
    ratio = vol24 / liq
    if 0.5 <= ratio <= 5:
        s = 2.0
        note = "здоровое соотношение объём/ликвидность"
    elif 0.1 <= ratio < 0.5 or 5 < ratio <= 10:
        s = 1.0
        note = "объём слегка низкий или подозрительно высокий"
    elif ratio > 10:
        s = 0.5
        note = "аномально высокий оборот — возможен wash trading"
    else:
        s = 0.3
        note = "очень низкая активность торгов"
    return s, f"Объём 24ч: ${vol24:,.0f} ({note})"


def score_age_and_timing(pair: dict):
    created_ms = pair.get("pairCreatedAt")
    fdv = pair.get("fdv") or pair.get("marketCap")
    price_change_24h = pair.get("priceChange", {}).get("h24")

    notes = []
    if created_ms:
        age_days = (time.time() * 1000 - created_ms) / (1000 * 60 * 60 * 24)
        if age_days < 1:
            age_score = 1.0
            notes.append(f"токену меньше суток ({age_days*24:.1f}ч) — экстремально ранняя и рискованная стадия")
        elif age_days < 7:
            age_score = 1.5
            notes.append(f"возраст {age_days:.1f} дн. — ранняя стадия")
        elif age_days < 30:
            age_score = 2.0
            notes.append(f"возраст {age_days:.0f} дн. — уже пережил первую волну хайпа/дампа")
        elif age_days < 180:
            age_score = 1.5
            notes.append(f"возраст {age_days:.0f} дн. — зрелый мем, взрывной рост маловероятен")
        else:
            age_score = 1.0
            notes.append(f"возраст {age_days/30:.0f} мес. — старый токен, апсайд обычно ограничен")
    else:
        age_score = None
        notes.append("возраст токена неизвестен")

    timing_note = "недостаточно данных для оценки тайминга"
    if fdv is not None:
        if fdv < 100_000:
            timing_note = "капитализация < $100k — очень ранняя стадия, огромный риск, но и потенциал x100 не исключён"
        elif fdv < 1_000_000:
            timing_note = "капитализация $100k–1M — ранняя стадия"
        elif fdv < 10_000_000:
            timing_note = "капитализация $1M–10M — средняя стадия, для кратного роста нужен уже серьёзный приток денег"
        elif fdv < 100_000_000:
            timing_note = "капитализация $10M–100M — поздняя стадия, x10+ маловероятен без крупного нарратива"
        else:
            timing_note = "капитализация > $100M — вход на пике зрелости, апсайд для мем-коина обычно ограничен"

    if price_change_24h is not None and price_change_24h > 300:
        timing_note += f". Внимание: цена уже выросла на {price_change_24h:.0f}% за 24ч — высокий риск захода на пике перед коррекцией"

    return age_score, " / ".join(notes), timing_note


def score_contract_safety(goplus_result: dict, is_solana: bool):
    if not goplus_result:
        return None, "Данные о безопасности контракта недоступны"

    score = 2.0
    flags = []

    def is_true(v):
        return str(v) in ("1", "true", "True")

    if not is_solana:
        if is_true(goplus_result.get("is_honeypot")):
            score -= 2.0
            flags.append("🚨 ПОХОЖЕ НА HONEYPOT (нельзя продать)")
        if is_true(goplus_result.get("cannot_sell_all")):
            score -= 0.7
            flags.append("нельзя продать весь баланс")
        if is_true(goplus_result.get("is_mintable")):
            score -= 0.4
            flags.append("владелец может допечатать токены (mint)")
        if is_true(goplus_result.get("transfer_pausable")):
            score -= 0.4
            flags.append("владелец может приостановить переводы")
        if is_true(goplus_result.get("is_blacklisted")):
            score -= 0.3
            flags.append("есть функция blacklist")
        if goplus_result.get("is_open_source") is not None and not is_true(goplus_result.get("is_open_source")):
            score -= 0.3
            flags.append("контракт не верифицирован (не open source)")
        try:
            buy_tax = float(goplus_result.get("buy_tax", 0) or 0)
            sell_tax = float(goplus_result.get("sell_tax", 0) or 0)
            if buy_tax > 0.1 or sell_tax > 0.1:
                score -= 0.5
                flags.append(f"высокие налоги: buy {buy_tax*100:.0f}% / sell {sell_tax*100:.0f}%")
        except (TypeError, ValueError):
            pass
        owner_addr = (goplus_result.get("owner_address") or "").lower()
        renounced_addresses = {
            "0x0000000000000000000000000000000000000000",
            "0x000000000000000000000000000000000000dead",
            "",
        }
        if owner_addr and owner_addr not in renounced_addresses:
            flags.append("ownership не отречён (owner всё ещё имеет права)")
    else:
        if goplus_result.get("mintable", {}).get("status") == "1":
            score -= 0.6
            flags.append("mint authority не отозван")
        if goplus_result.get("freezable", {}).get("status") == "1":
            score -= 0.6
            flags.append("есть freeze authority")

    score = max(0.0, min(2.0, score))
    summary = "; ".join(flags) if flags else "явных красных флагов не обнаружено"
    return score, summary


def score_holders(goplus_result: dict, is_solana: bool):
    if not goplus_result:
        return None, "Данные о держателях недоступны"
    try:
        holders = goplus_result.get("holders")
        if not holders:
            return None, "Данные о держателях недоступны"
        top10_pct = sum(float(h.get("percent", 0)) for h in holders[:10]) * 100
    except Exception:
        return None, "Не удалось разобрать данные о держателях"

    if top10_pct < 20:
        s = 2.0
    elif top10_pct < 40:
        s = 1.5
    elif top10_pct < 60:
        s = 1.0
    elif top10_pct < 80:
        s = 0.5
    else:
        s = 0.0
    return s, f"Топ-10 держателей владеют {top10_pct:.1f}% предложения"


async def analyze_token(address: str) -> dict:
    address = address.strip()
    async with aiohttp.ClientSession() as session:
        pair = await get_dexscreener_data(session, address)
        if not pair:
            raise AnalysisError(
                "Не нашёл этот адрес ни в одной паре на DexScreener. "
                "Проверь адрес или подожди — возможно, токен ещё не листингован ни на одной DEX."
            )

        chain_id = pair.get("chainId")
        is_solana = chain_id == "solana"
        goplus_result = await get_goplus_security(session, chain_id, address)

        liq_score, liq_note = score_liquidity(pair)
        vol_score, vol_note = score_volume(pair)
        age_score, age_note, timing_note = score_age_and_timing(pair)
        safety_score, safety_note = score_contract_safety(goplus_result, is_solana)
        holders_score, holders_note = score_holders(goplus_result, is_solana)

        components = {
            "Ликвидность": (liq_score, liq_note),
            "Объём торгов": (vol_score, vol_note),
            "Возраст/стадия": (age_score, age_note),
            "Безопасность контракта": (safety_score, safety_note),
            "Держатели": (holders_score, holders_note),
        }

        available = [v[0] for v in components.values() if v[0] is not None]
        if available:
            # нормируем к шкале 0-10 (каждый компонент максимум 2 балла)
            final_score = round(sum(available) / len(available) * 5, 1)
        else:
            final_score = None

        return {
            "address": address,
            "chain": chain_id,
            "dex": pair.get("dexId"),
            "symbol": pair.get("baseToken", {}).get("symbol"),
            "name": pair.get("baseToken", {}).get("name"),
            "price_usd": pair.get("priceUsd"),
            "fdv": pair.get("fdv") or pair.get("marketCap"),
            "url": pair.get("url"),
            "components": components,
            "timing_note": timing_note,
            "final_score": final_score,
            "unavailable_count": sum(1 for v in components.values() if v[0] is None),
        }
