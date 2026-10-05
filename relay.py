#!/usr/bin/env python3
"""
Relay de buffering pour Zabbix Webhook.
Ne reçoit QUE les échecs déjà détectés par le script JS Zabbix.
Rejoue vers une VIP fixe avec backoff exponentiel.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 9191

VIP_ENDPOINT = "https://vip.example.com/webhook"   # <-- endpoint fixe de rejeu
VIP_TIMEOUT = 10  # secondes

QUEUE_DIR = "/var/lib/zbx-relay"
QUEUE_FILE = os.path.join(QUEUE_DIR, "queue.jsonl")
DEAD_LETTER_FILE = os.path.join(QUEUE_DIR, "dead-letter.jsonl")

LOG_DIR = "/var/log/zbx-relay"
LOG_FILE = os.path.join(LOG_DIR, "relay.log")

# Paliers de backoff exponentiel (secondes), plafonné au dernier palier
BACKOFF_STEPS = [5, 30, 120, 600, 1800, 3600]  # 5s,30s,2min,10min,30min,1h

WORKER_POLL_INTERVAL = 5  # fréquence de scan de la queue

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(QUEUE_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("zbx-relay")

# Verrou pour protéger les accès concurrents au fichier de queue
queue_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Gestion de la queue (fichier JSONL)
# ---------------------------------------------------------------------------
def append_to_queue(item: dict):
    """Ajoute un item à la queue (append-only, thread-safe)."""
    with queue_lock:
        with open(QUEUE_FILE, "a") as f:
            f.write(json.dumps(item) + "\n")


def read_queue() -> list:
    """Lit tous les items de la queue."""
    if not os.path.exists(QUEUE_FILE):
        return []
    with queue_lock:
        with open(QUEUE_FILE, "r") as f:
            lines = [line.strip() for line in f if line.strip()]
    items = []
    for line in lines:
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            log.warning("Ligne de queue invalide ignorée: %s", line[:200])
    return items


def rewrite_queue(items: list):
    """Réécrit intégralement la queue (utilisé après traitement d'un lot)."""
    with queue_lock:
        tmp_file = QUEUE_FILE + ".tmp"
        with open(tmp_file, "w") as f:
            for item in items:
                f.write(json.dumps(item) + "\n")
        os.replace(tmp_file, QUEUE_FILE)


def append_to_dead_letter(item: dict, reason: str):
    item["dead_letter_reason"] = reason
    item["dead_letter_at"] = datetime.now(timezone.utc).isoformat()
    with open(DEAD_LETTER_FILE, "a") as f:
        f.write(json.dumps(item) + "\n")
    log.warning("Item envoyé en dead-letter (%s): %s", reason, item.get("endpoint"))


# ---------------------------------------------------------------------------
# Worker de rejeu
# ---------------------------------------------------------------------------
def compute_next_retry(attempt_count: int) -> int:
    """Retourne le délai (en secondes) avant le prochain essai."""
    idx = min(attempt_count, len(BACKOFF_STEPS) - 1)
    return BACKOFF_STEPS[idx]


def try_replay(item: dict) -> str:
    """
    Tente de rejouer un item vers la VIP fixe.
    Retourne: "success", "retry", ou "dead-letter"
    """
    try:
        method = item.get("method", "POST").upper()
        headers = item.get("headers", {"Content-Type": "application/json"})
        payload = item.get("payload", {})

        resp = requests.request(
            method,
            VIP_ENDPOINT,
            json=payload,
            headers=headers,
            timeout=VIP_TIMEOUT,
        )

        if 200 <= resp.status_code < 300:
            log.info("Rejeu réussi vers %s (status %s)", VIP_ENDPOINT, resp.status_code)
            return "success"

        if 400 <= resp.status_code < 500:
            log.warning("Rejeu refusé définitivement (status %s)", resp.status_code)
            return "dead-letter"

        # 5xx ou autre -> on continue à retenter
        log.info("Rejeu échoué (status %s), nouvelle tentative plus tard", resp.status_code)
        return "retry"

    except requests.exceptions.RequestException as e:
        log.info("Rejeu impossible (contact échoué: %s), nouvelle tentative plus tard", e)
        return "retry"


def worker_loop():
    log.info("Worker de rejeu démarré.")
    while True:
        time.sleep(WORKER_POLL_INTERVAL)
        items = read_queue()
        if not items:
            continue

        now = time.time()
        remaining_items = []
        changed = False

        for item in items:
            next_retry_at = item.get("next_retry_at", 0)
            if now < next_retry_at:
                remaining_items.append(item)
                continue

            result = try_replay(item)
            changed = True

            if result == "success":
                continue  # retiré de la queue

            elif result == "dead-letter":
                append_to_dead_letter(item, reason="4xx au rejeu")
                continue  # retiré de la queue

            else:  # retry
                item["attempt_count"] = item.get("attempt_count", 0) + 1
                delay = compute_next_retry(item["attempt_count"])
                item["next_retry_at"] = now + delay
                item["last_attempt_at"] = datetime.now(timezone.utc).isoformat()
                remaining_items.append(item)

        if changed:
            rewrite_queue(remaining_items)
            log.info("Queue mise à jour: %d item(s) restant(s).", len(remaining_items))


# ---------------------------------------------------------------------------
# Serveur HTTP — réception des échecs
# ---------------------------------------------------------------------------
class BufferHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Redirige les logs HTTP par défaut vers notre logger
        log.debug("%s - %s", self.address_string(), format % args)

    def do_POST(self):
        if self.path != "/buffer":
            self.send_response(404)
            self.end_headers()
            return

        try:
            content_length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(content_length)
            data = json.loads(raw_body)
        except (ValueError, json.JSONDecodeError) as e:
            log.warning("Requête /buffer invalide: %s", e)
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "error", "detail": str(e)}).encode())
            return

        # Validation minimale
        if "payload" not in data:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "error", "detail": "champ 'payload' manquant"}).encode())
            return

        item = {
            "endpoint": data.get("endpoint", VIP_ENDPOINT),
            "payload": data["payload"],
            "method": data.get("method", "POST"),
            "headers": data.get("headers", {"Content-Type": "application/json"}),
            "failed_at": data.get("failed_at", datetime.now(timezone.utc).isoformat()),
            "failure_reason": data.get("failure_reason", "unknown"),
            "attempt_count": 0,
            "next_retry_at": 0,  # éligible immédiatement au prochain cycle worker
        }

        append_to_queue(item)
        log.info("Item bufferisé (raison: %s)", item["failure_reason"])

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "buffered"}).encode())


def main():
    server = HTTPServer((LISTEN_HOST, LISTEN_PORT), BufferHandler)
    log.info("Relay en écoute sur %s:%s", LISTEN_HOST, LISTEN_PORT)

    worker_thread = threading.Thread(target=worker_loop, daemon=True)
    worker_thread.start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Arrêt du relay.")
        server.shutdown()


if __name__ == "__main__":
    main()
