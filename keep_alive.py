"""
keep_alive.py — крошечный HTTP-сервер для бесплатного тарифа Render.

Render free tier ожидает, что сервис отвечает на HTTP-запросы (это нужно
для health-check и чтобы сервис не "засыпал" от полного отсутствия
трафика). Наш бот работает через polling и сам по себе никаких HTTP-
запросов не принимает, поэтому поднимаем отдельный поток с простейшим
веб-сервером, который просто отвечает "OK" на любой GET-запрос.

Использование: см. bot.py — запускается автоматически, если задана
переменная окружения PORT (Render всегда её прокидывает).

Чтобы сервис реально не засыпал (у бесплатных Render-сервисов taймаут
неактивности — 15 минут), нужно ещё настроить внешний "пинг" каждые
5-10 минут — например, через бесплатный UptimeRobot (см. README).
"""

import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

logger = logging.getLogger(__name__)


class _HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("Meme Token Analyzer Bot is running.".encode("utf-8"))

    def log_message(self, format, *args):
        # заглушаем стандартный лог http.server, чтобы не засорять вывод
        pass


def start_keep_alive_server(port: int):
    def _run():
        server = HTTPServer(("0.0.0.0", port), _HealthCheckHandler)
        logger.info("Keep-alive HTTP server listening on port %s", port)
        server.serve_forever()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
