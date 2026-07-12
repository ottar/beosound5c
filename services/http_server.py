#!/usr/bin/env python3
"""BeoSound 5c HTTP server with no-cache headers.

Drop-in replacement for `python3 -m http.server 8000`.
Adds Cache-Control: no-store to every response so Chromium's
in-memory HTTP cache never serves stale files (playlist JSON,
JS, CSS, etc.).  This is appropriate for a local kiosk app.
"""

import http.server
import json
import sys
import threading
import time
import urllib.request
import urllib.error

sys.path.insert(0, __file__.rsplit('/', 1)[0])  # ensure services/ is on path
from lib.endpoints import input_url  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000

# Paths proxied to beo-input (port 8767)
_PROXY_PREFIXES = ('/config', '/update/', '/discover/')

# ── Weather proxy (MET Norway / yr.no) ──
#
# The kiosk fetches /weather/forecast from this server instead of calling
# api.met.no directly: browsers can't set the identifying User-Agent MET's
# terms require, and a server-side cache keeps us well under their rate
# limits (MET updates the forecast roughly every half hour anyway).
# Location comes from the `weather` config section (default: Bergen).

WEATHER_PATH = '/weather/forecast'
_MET_URL = ('https://api.met.no/weatherapi/locationforecast/2.0/complete'
            '?lat={lat:.4f}&lon={lon:.4f}&altitude={alt}')
_WEATHER_UA = 'BeoSound5c/1.0 github.com/ottar/beosound5c'
_WEATHER_TTL = 30 * 60  # seconds between upstream fetches

_weather_lock = threading.Lock()
_weather_cache = {'key': None, 'body': None, 'fetched': 0.0}


def _weather_forecast_body() -> bytes:
    """Return the cached forecast JSON, refreshing from MET when stale.
    On upstream failure a stale cache (any age) is served rather than an
    error — an old forecast beats an empty WEATHER page."""
    from lib.config import cfg
    lat = float(cfg('weather', 'lat', default=60.393))
    lon = float(cfg('weather', 'lon', default=5.3242))
    alt = int(cfg('weather', 'altitude', default=12))
    name = cfg('weather', 'name', default='Bergen')
    key = (lat, lon, alt)
    now = time.time()

    with _weather_lock:
        fresh = (_weather_cache['key'] == key
                 and now - _weather_cache['fetched'] < _WEATHER_TTL)
        if fresh:
            return _weather_cache['body']

    req = urllib.request.Request(
        _MET_URL.format(lat=lat, lon=lon, alt=alt),
        headers={'User-Agent': _WEATHER_UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            forecast = json.loads(resp.read())
        body = json.dumps({'name': name, 'lat': lat, 'lon': lon,
                           'fetched': int(now),
                           'forecast': forecast}).encode()
        with _weather_lock:
            _weather_cache.update(key=key, body=body, fetched=now)
        return body
    except Exception:
        with _weather_lock:
            if _weather_cache['key'] == key and _weather_cache['body']:
                return _weather_cache['body']
        raise


def _serve_weather(handler) -> bool:
    """Handle GET /weather/forecast. Returns True if handled."""
    if handler.path.split('?')[0] != WEATHER_PATH:
        return False
    try:
        body = _weather_forecast_body()
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Access-Control-Allow-Origin', '*')
        handler.end_headers()
        handler.wfile.write(body)
    except Exception as e:
        handler.send_response(502)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Access-Control-Allow-Origin', '*')
        handler.end_headers()
        handler.wfile.write(json.dumps({'error': str(e)}).encode())
    return True


def _proxy_to_input(handler, method: str) -> bool:
    """Forward request to beo-input on port 8767. Returns True if handled."""
    if not any(handler.path == p or handler.path.startswith(p)
               for p in _PROXY_PREFIXES):
        return False

    url = input_url(handler.path)
    body = None
    if method == 'POST':
        length = int(handler.headers.get('Content-Length', 0))
        body = handler.rfile.read(length) if length else b''
    ct = handler.headers.get('Content-Type', 'application/json')

    try:
        req = urllib.request.Request(url, data=body, method=method,
                                     headers={'Content-Type': ct})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
            handler.send_response(resp.status)
            handler.send_header('Content-Type',
                                 resp.headers.get('Content-Type', 'application/json'))
            handler.send_header('Access-Control-Allow-Origin', '*')
            handler.end_headers()
            handler.wfile.write(data)
    except urllib.error.HTTPError as e:
        data = e.read()
        handler.send_response(e.code)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Access-Control-Allow-Origin', '*')
        handler.end_headers()
        handler.wfile.write(data)
    except Exception as e:
        handler.send_response(502)
        handler.send_header('Content-Type', 'text/plain')
        handler.end_headers()
        handler.wfile.write(str(e).encode())
    return True


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    def _redirect_config(self):
        self.send_response(302)
        self.send_header('Location', '/softarc/config.html')
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        if self.path == '/config':
            self._redirect_config()
            return
        if _serve_weather(self):
            return
        if _proxy_to_input(self, 'GET'):
            return
        super().do_GET()

    def do_HEAD(self):
        if self.path == '/config':
            self._redirect_config()
            return
        super().do_HEAD()

    def do_POST(self):
        if _proxy_to_input(self, 'POST'):
            return
        self.send_response(404)
        self.end_headers()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()


if __name__ == "__main__":
    with http.server.ThreadingHTTPServer(("", PORT), NoCacheHandler) as httpd:
        print(f"Serving on port {PORT} (no-cache)")
        httpd.serve_forever()
