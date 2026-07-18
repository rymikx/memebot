"""
analyzer.py — сбор данных о мем-токене и расчёт инвестиционного скора.
"""

import time
import asyncio
import logging
import aiohttp

logger = logging.getLogger(__name__)

DEXSCREENER_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search?q={address}"
DEXSCREENER_TOKEN_PAIRS_URL = "https://api.dexscreener.com/token-pairs/v1/{chain}/{address}"

SUPPORTED_DEXSCREENER_CHAINS = [
    "ethereum", "bsc", "polygon", "arbitrum", "base", "avalanche",
    "optimism", "fantom", "robinhood", "solana", "ton",
]

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

GOPLUS_EVM_URL = "https://api.gopluslabs.io/api/v1/token_security/{chain_id}?contract_addresses={address}"
GOPLUS_SOLANA_URL = "https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses={address}"


class AnalysisError(Exception):
    pass


async def fetch_json(session: aiohttp.ClientSession, url: str, timeout: int = 10, retries: int = 2):
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


def _pick_best_pair(pairs):
    pairs = [p for p in pairs if p.get("liquidity", {}).get("usd") is not None]
    if not pairs:
        return None
    return max(pairs, key=lambda p: p["liquidity"]["usd"])


async def _search_by_text(session: aiohttp.ClientSession, address: str):
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


def _confident_single_chain(address: str):
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


def _guess_candidate_chains(address: str):
    confident = _confident_single_chain(address)
    if confident:
        return [confident]
    return ["ethereum", "base", "bsc", "arbitrum", "robinhood", "polygon", "avalanche", "optimism", "fantom"]


async def _fetch_pairs_for_chain(session: aiohttp.ClientSession, chain: str, address: str, retries: int = 2):
    url = DEXSCREENER_TOKEN_PAIRS_URL.format(chain=chain, address=address)
    data = await fetch_json(session, url, retries=retries)
    if not data or not isinstance(data, list):
        return []
    return data


async def _scan_candidate_chains(session: aiohttp.ClientSession, address: str, retries_per_chain: int = 2):
    candidates = _guess_candidate_chains(address)
    logger.info("Проверяю сети-кандидаты для %s: %s", address, candidates)
    for chain in candidates:
        pairs = await _fetch_pairs_for_chain(session, chain, address, retries=retries_per_chain)
        best = _pick_best_pair(pairs)
        if best:
            return best
    return None


async def get_dexscreener_data(session: aiohttp.ClientSession, address: str):
    confident_chain = _confident_single_chain(address)
    if confident_chain:
        logger.info(
            "get_dexscreener_data: сеть определена однозначно по формату (%s), "
            "пропускаю текстовый поиск, иду сразу в fallback с доп. попытками",
            confident_chain,
        )
        result = await _scan_candidate_chains(session, address, retries_per_chain=4)
        if result:
            logger.info("get_dexscreener_data: найдено: %s (сеть %s)", address, result.get("chainId"))
        else:
            logger.warning("get_dexscreener_data: НЕ найдено нигде: %s", address)
        return result

    pair = await _search_by_text(session, address)
    if pair:
        logger.info("get_dexscreener_data: найдено через текстовый поиск: %s", address)
        return pair
    logger.info("get_dexscreener_data: текстовый поиск ничего не дал для %s, пробую fallback по сетям", address)
    result = await _scan_candidate_chains(session, address)
    if result:
        logger.info("get_dexscreener_data: найдено через fallback: %s (сеть %s)", address, result.get("chainId"))
    else:
        logger.warning("get_dexscreener_data: НЕ найдено нигде: %s", address)
    return result


async def get_goplus_security(session: aiohttp.ClientSession, chain_id_dexscreener: str, address: str):
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
