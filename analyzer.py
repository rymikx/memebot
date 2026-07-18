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
import aiohttp

DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/tokens/{address}"

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


async def fetch_json(session: aiohttp.ClientSession, url: str, timeout: int = 10):
    try:
        async with session.get(url, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return await resp.json()
    except Exception:
        return None


async def get_dexscreener_data(session: aiohttp.ClientSession, address: str):
    """Возвращает лучшую (по ликвидности) торговую пару для адреса токена."""
    data = await fetch_json(session, DEXSCREENER_URL.format(address=address))
    if not data or not data.get("pairs"):
        return None
    pairs = data["pairs"]
    # выбираем пару с максимальной ликвидностью в USD
    pairs = [p for p in pairs if p.get("liquidity", {}).get("usd") is not None]
    if not pairs:
        return None
    best = max(pairs, key=lambda p: p["liquidity"]["usd"])
    return best


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
