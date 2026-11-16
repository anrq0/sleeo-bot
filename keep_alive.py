"""Petit serveur web pour Render (service « Web Service »). Il répond sur le port demandé par Render,
ce qui supprime l'avertissement « No open ports detected » et permet à UptimeRobot de garder le bot éveillé."""
import os, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    def _ok(self, body=True):
        self.send_response(200); self.send_header("Content-Type", "text/plain; charset=utf-8"); self.end_headers()
        if body:
            self.wfile.write(b"Sleeo Watch OK")
    def do_GET(self): self._ok()
    def do_HEAD(self): self._ok(False)
    def log_message(self, *args): pass

def keep_alive():
    port = int(os.environ.get("PORT", "10000"))
    threading.Thread(target=lambda: HTTPServer(("0.0.0.0", port), Handler).serve_forever(), daemon=True).start()
