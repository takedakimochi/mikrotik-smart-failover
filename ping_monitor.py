#!/usr/bin/env python3
"""
Sistem Failover Monitoring Ping Smart untuk Mikrotik
- Ping paralel ke multi-monitor IP per ISP
- State Machine Real-time (Evaluasi per detik, bukan per siklus kaku)
- Perlindungan Anti-Flapping (Cooldown 180 detik)
- Logging interaktif
- Web Dashboard Glassmorphism Support
- Dibuat kocak biar gak pusing mikirin RTO!
"""

import subprocess
import time
import sys
import os
import logging
import collections
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import routeros_api
from dotenv import load_dotenv

# ──────────────────────────────────────────────────────────────
# Load konfigurasi dari .env (Wajib ada, gak ada fallback ya bos!)
# ──────────────────────────────────────────────────────────────
load_dotenv()

MIKROTIK_IP       = os.getenv("MIKROTIK_IP")
MIKROTIK_USER     = os.getenv("MIKROTIK_USER")
MIKROTIK_PASSWORD = os.getenv("MIKROTIK_PASSWORD")
API_PORT_STR      = os.getenv("MIKROTIK_API_PORT")
WEB_PORT_STR      = os.getenv("WEB_PORT")

if not MIKROTIK_IP or not MIKROTIK_USER or not MIKROTIK_PASSWORD or not API_PORT_STR:
    sys.exit("Error: Konfigurasi MIKROTIK_IP, MIKROTIK_USER, MIKROTIK_PASSWORD, dan MIKROTIK_API_PORT wajib diisi di file .env!")

try:
    API_PORT = int(API_PORT_STR)
except ValueError:
    sys.exit("Error: MIKROTIK_API_PORT di file .env kudu angka, bos!")

try:
    WEB_PORT = int(WEB_PORT_STR)
except ValueError:
    sys.exit("Error: WEB_PORT di file .env kudu angka, bos!")


# ──────────────────────────────────────────────────────────────
# Konfigurasi Target Multi-IP & Script Mikrotik
# ──────────────────────────────────────────────────────────────
TARGETS = {
    "target1": {
        "name": "ISP UTAMA",
        "ips": ["8.8.8.8", "1.1.1.1", "9.9.9.9"],
        "script_off": "JALUR_LDP_OFF",
        "script_on":  "JALUR_LDP_ON",
    },
    "target2": {
        "name": "ISP BACKUP",
        "ips": ["8.8.4.4", "1.0.0.1", "149.112.112.112"],
        "script_off": "JALUR_BACKUP_OFF",
        "script_on":  "JALUR_BACKUP_ON",
    },
}

PING_INTERVAL  = 1     # Detik antar iterasi ping
PING_TIMEOUT   = 2     # Timeout per ping (detik)

# Parametrisasi Logika Failover (Detik)
DOWN_DURATION_THRESHOLD = 90       # Durasi minimum kondisi buruk untuk down (60-90 detik)
STABILIZATION_DURATION  = 60        # Durasi minimum stabilisasi untuk recover
COOLDOWN_DURATION       = 300       # Cooldown setelah failover biar router gak meriang

# ──────────────────────────────────────────────────────────────
# State Monitoring (Dashboard API & Internal State Machine)
# ──────────────────────────────────────────────────────────────
MONITOR_STATE = {
    "status": "Initializing",
    "cycle": 0,
    "last_update": "",
    "targets": {
        key: {
            "ip": ", ".join(target["ips"]), # Biar dashboard nampilin semua IP
            "status": "Unknown",
            "success_count": 0,
            "fail_count": 0,
            "loss_pct": 0.0,
            "last_action": "Belum ada aksi"
        } for key, target in TARGETS.items()
    }
}

# State Machine Runtime Internal
runtime_states = {
    key: {
        "official_status": "UP",      # Status resmi: "UP" atau "DOWN"
        "down_since": None,           # Timestamp mulai terdeteksi down
        "stabilization_since": None,  # Timestamp mulai fase stabilisasi
        "ping_history": collections.deque(maxlen=180), # Moving average window (60 detik * 3 IP)
    } for key in TARGETS
}

# Global Cooldown Tracker
last_failover_time = 0.0

# Custom Logging Handler untuk dashboard web
class MemoryLogHandler(logging.Handler):
    def __init__(self, capacity=50):
        super().__init__()
        self.capacity = capacity
        self.log_queue = []

    def emit(self, record):
        log_entry = self.format(record)
        self.log_queue.append(log_entry)
        if len(self.log_queue) > self.capacity:
            self.log_queue.pop(0)

# ──────────────────────────────────────────────────────────────
# Setup Logging
# ──────────────────────────────────────────────────────────────
LOG_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, f"monitor_{datetime.now().strftime('%Y%m%d')}.log")

memory_log_handler = MemoryLogHandler()
formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
memory_log_handler.setFormatter(formatter)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        memory_log_handler
    ],
)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# HTTP Server Handler
# ──────────────────────────────────────────────────────────────
class MonitorHTTPServer(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Mute default logs biar terminal gak penuh log request GET
        pass

    def do_GET(self):
        if self.path == "/api/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            
            state = dict(MONITOR_STATE)
            state["recent_logs"] = list(memory_log_handler.log_queue)
            
            self.wfile.write(json.dumps(state).encode("utf-8"))
        elif self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            
            index_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
            try:
                with open(index_path, "r", encoding="utf-8") as f:
                    html_content = f.read()
                self.wfile.write(html_content.encode("utf-8"))
            except Exception as e:
                self.wfile.write(f"<h3>Error loading index.html: {e}</h3>".encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")

def start_web_server(port):
    server_address = ("", port)
    try:
        httpd = HTTPServer(server_address, MonitorHTTPServer)
        log.info(f"🌐 HTTP Server dashboard jalan di http://localhost:{port} ya bos!")
        server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        server_thread.start()
    except Exception as e:
        log.error(f"✗ Gagal menjalankan HTTP Server di port {port}: {e}")

# ──────────────────────────────────────────────────────────────
# Fungsi Ping
# ──────────────────────────────────────────────────────────────
def ping_ip(ip: str) -> bool:
    """Ping satu IP, kembalikan True kalo sukses."""
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(PING_TIMEOUT), ip],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0
    except Exception:
        return False

# ──────────────────────────────────────────────────────────────
# Fungsi Ping Paralel semua target sekaligus
# ──────────────────────────────────────────────────────────────
def ping_all_ips(all_ips: list) -> dict:
    """Ping semua IP secara paralel."""
    results = {}
    with ThreadPoolExecutor(max_workers=len(all_ips)) as executor:
        future_to_ip = {
            executor.submit(ping_ip, ip): ip
            for ip in all_ips
        }
        for future in as_completed(future_to_ip):
            ip = future_to_ip[future]
            try:
                results[ip] = future.result()
            except Exception:
                results[ip] = False
    return results

# ──────────────────────────────────────────────────────────────
# Fungsi Eksekusi Script Mikrotik
# ──────────────────────────────────────────────────────────────
def run_mikrotik_script(script_name: str) -> bool:
    """Jalankan script di Mikrotik via API."""
    log.info(f"Menghubungkan ke router Mikrotik {MIKROTIK_IP}:{API_PORT}...")
    connection = None
    try:
        connection = routeros_api.RouterOsApiPool(
            MIKROTIK_IP,
            username=MIKROTIK_USER,
            password=MIKROTIK_PASSWORD,
            port=API_PORT,
            plaintext_login=True,
        )
        api = connection.get_api()
        log.info(f"Menjalankan script Mikrotik: '{script_name}'...")
        resource = api.get_resource("/system/script")
        resource.call("run", {"number": script_name})
        log.info(f"✓ Berhasil eksekusi script Mikrotik: '{script_name}'")
        return True
    except Exception as e:
        log.error(f"✗ Gagal eksekusi script Mikrotik '{script_name}': {e}")
        return False
    finally:
        if connection:
            try:
                connection.disconnect()
            except Exception:
                pass

# ──────────────────────────────────────────────────────────────
# State Machine & Evaluasi Failover Real-time
# ──────────────────────────────────────────────────────────────
def evaluate_and_trigger(ping_results: dict):
    """
    State machine inti buat deteksi failover dan recovery.
    Berjalan setiap detik untuk memantau status secara presisi.
    """
    global last_failover_time
    now = time.time()
    
    cooldown_elapsed = now - last_failover_time
    cooldown_active = cooldown_elapsed < COOLDOWN_DURATION
    cooldown_remaining = max(0, int(COOLDOWN_DURATION - cooldown_elapsed))

    for key, target in TARGETS.items():
        state = runtime_states[key]
        ips = target["ips"]
        
        # Dapatkan hasil ping instan untuk target ini
        target_results = {ip: ping_results.get(ip, False) for ip in ips}
        success_count = sum(1 for res in target_results.values() if res)
        fail_count = len(ips) - success_count
        
        # Masukkan hasil ke history ping (moving average window)
        for res in target_results.values():
            state["ping_history"].append(res)
            
        # Hitung statistik untuk dashboard
        total_history = len(state["ping_history"])
        failed_history = state["ping_history"].count(False)
        loss_pct = failed_history / total_history if total_history > 0 else 0.0
        
        MONITOR_STATE["targets"][key]["success_count"] = state["ping_history"].count(True)
        MONITOR_STATE["targets"][key]["fail_count"] = failed_history
        MONITOR_STATE["targets"][key]["loss_pct"] = loss_pct

        # ──────────────────────────────────────────────────────────
        # LOGIKA NEGARA (STATE MACHINE)
        # ──────────────────────────────────────────────────────────
        
        # JIKA JALUR SAAT INI DIANGGAP NORMAL (UP)
        if state["official_status"] == "UP":
            if fail_count >= 2:
                # Terjadi kondisi buruk (2 atau 3 monitor fail)
                state["stabilization_since"] = None  # Reset timer pemulihan jika ada
                if state["down_since"] is None:
                    state["down_since"] = now
                    log.warning(
                        f"⚠️ [{target['name']}] Terdeteksi kondisi buruk ({fail_count}/3 monitor DOWN). "
                        f"Mulai menghitung durasi down (Threshold: {DOWN_DURATION_THRESHOLD}s)..."
                    )
                else:
                    elapsed_down = now - state["down_since"]
                    # Log info setiap 5 detik biar gak terlalu menuhin terminal
                    if int(elapsed_down) % 5 == 0:
                        log.warning(
                            f"⏳ [{target['name']}] Kondisi buruk bertahan: {elapsed_down:.1f}s / {DOWN_DURATION_THRESHOLD}s "
                            f"({fail_count}/3 monitor DOWN)"
                        )
                    
                    if elapsed_down >= DOWN_DURATION_THRESHOLD:
                        # Udah lewat threshold durasi down, siap-siap switch!
                        if cooldown_active:
                            log.warning(
                                f"🚫 [{target['name']}] SEHARUSNYA FAILOVER KE DOWN! "
                                f"Tetapi DITUNDA karena Cooldown aktif ({cooldown_remaining}s tersisa)."
                            )
                            MONITOR_STATE["targets"][key]["status"] = "WARNING"
                        else:
                            log.error(
                                f"🔥 [{target['name']}] Kondisi DOWN bertahan >= {DOWN_DURATION_THRESHOLD}s! "
                                f"Mengeksekusi failover..."
                            )
                            state["official_status"] = "DOWN"
                            state["down_since"] = None
                            
                            # Jalankan script off
                            success = run_mikrotik_script(target["script_off"])
                            if success:
                                MONITOR_STATE["targets"][key]["last_action"] = f"Triggered {target['script_off']} (Loss: {loss_pct*100:.0f}%)"
                            else:
                                MONITOR_STATE["targets"][key]["last_action"] = f"Failed {target['script_off']}"
                            
                            last_failover_time = now
                            cooldown_active = True
                            cooldown_remaining = COOLDOWN_DURATION
            else:
                # 1 monitor fail = ignore (tetap UP), atau semua sukses
                if state["down_since"] is not None:
                    log.info(f"✅ [{target['name']}] Kondisi kembali normal (0 atau 1 monitor fail). Timer down direset.")
                    state["down_since"] = None

        # JIKA JALUR SAAT INI DIANGGAP MATI (DOWN)
        elif state["official_status"] == "DOWN":
            if success_count == 3:
                # Kasus: 3 monitor recover = ISP UP (Kembali normal secara instan)
                state["stabilization_since"] = None
                log.info(f"🎉 [{target['name']}] Semua 3 monitor sukses ping (Full Recovery).")
                
                if cooldown_active:
                    log.warning(
                        f"🚫 [{target['name']}] SEHARUSNYA RECOVERY KE UP! "
                        f"Tetapi DITUNDA karena Cooldown aktif ({cooldown_remaining}s tersisa)."
                    )
                    MONITOR_STATE["targets"][key]["status"] = "STABILISASI"
                else:
                    log.info(f"🟢 [{target['name']}] Mengeksekusi pemulihan ke UP...")
                    state["official_status"] = "UP"
                    
                    # Jalankan script on
                    success = run_mikrotik_script(target["script_on"])
                    if success:
                        MONITOR_STATE["targets"][key]["last_action"] = f"Triggered {target['script_on']} (Loss: {loss_pct*100:.0f}%)"
                    else:
                        MONITOR_STATE["targets"][key]["last_action"] = f"Failed {target['script_on']}"
                        
                    last_failover_time = now
                    cooldown_active = True
                    cooldown_remaining = COOLDOWN_DURATION

            elif success_count == 2:
                # Kasus: 2 monitor recover = stabilisasi (Tunggu durasi tertentu)
                if state["stabilization_since"] is None:
                    state["stabilization_since"] = now
                    log.info(
                        f"🔄 [{target['name']}] Mulai fase stabilisasi (2/3 monitor UP). "
                        f"Tunggu {STABILIZATION_DURATION}s..."
                    )
                else:
                    elapsed_stabilization = now - state["stabilization_since"]
                    if int(elapsed_stabilization) % 5 == 0:
                        log.info(
                            f"⏳ [{target['name']}] Fase stabilisasi berjalan: {elapsed_stabilization:.1f}s / {STABILIZATION_DURATION}s "
                            f"(2/3 monitor UP)"
                        )
                    
                    if elapsed_stabilization >= STABILIZATION_DURATION:
                        if cooldown_active:
                            log.warning(
                                f"🚫 [{target['name']}] STABILISASI SELESAI, SEHARUSNYA RECOVERY KE UP! "
                                f"Tetapi DITUNDA karena Cooldown aktif ({cooldown_remaining}s tersisa)."
                            )
                            MONITOR_STATE["targets"][key]["status"] = "STABILISASI"
                        else:
                            log.info(f"🟢 [{target['name']}] Stabilisasi sukses. Mengeksekusi pemulihan ke UP...")
                            state["official_status"] = "UP"
                            state["stabilization_since"] = None
                            
                            # Jalankan script on
                            success = run_mikrotik_script(target["script_on"])
                            if success:
                                MONITOR_STATE["targets"][key]["last_action"] = f"Triggered {target['script_on']} (Loss: {loss_pct*100:.0f}%)"
                            else:
                                MONITOR_STATE["targets"][key]["last_action"] = f"Failed {target['script_on']}"
                                
                            last_failover_time = now
                            cooldown_active = True
                            cooldown_remaining = COOLDOWN_DURATION
            else:
                # 1 monitor recover = ignore (tetap DOWN)
                if state["stabilization_since"] is not None:
                    log.warning(f"❌ [{target['name']}] Stabilisasi gagal (kembali buruk). Timer stabilisasi direset.")
                    state["stabilization_since"] = None

        # ──────────────────────────────────────────────────────────
        # UPDATE STATE VISUAL UNTUK DASHBOARD
        # ──────────────────────────────────────────────────────────
        if state["official_status"] == "UP":
            if state["down_since"] is not None:
                MONITOR_STATE["targets"][key]["status"] = "WARNING"
            else:
                MONITOR_STATE["targets"][key]["status"] = "OK"
        else: # DOWN
            if state["stabilization_since"] is not None:
                MONITOR_STATE["targets"][key]["status"] = "STABILISASI"
            else:
                MONITOR_STATE["targets"][key]["status"] = "RTO"

# ──────────────────────────────────────────────────────────────
# Main Loop
# ──────────────────────────────────────────────────────────────
def main():
    MONITOR_STATE["status"] = "Running"
    log.info("=" * 65)
    log.info("   ⚡ SISTEM SMART FAILOVER MIKROTIK - MONITORING DIMULAI ⚡")
    log.info(f"   📂 File Log disimpen di: {LOG_FILE}")
    log.info("=" * 65)

    # Jalankan HTTP server di thread terpisah
    start_web_server(WEB_PORT)

    # Kumpulkan semua IP monitor dari kedua ISP
    all_ips = []
    for target in TARGETS.values():
        all_ips.extend(target["ips"])

    cycle = 1

    while True:
        MONITOR_STATE["cycle"] = cycle
        MONITOR_STATE["last_update"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Ping semua IP monitor secara PARALEL
        ping_results = ping_all_ips(all_ips)

        # Evaluasi dengan State Machine
        evaluate_and_trigger(ping_results)

        # Format print log untuk ditampilkan di terminal
        parts = []
        for key, target in TARGETS.items():
            ips_status = []
            for ip in target["ips"]:
                status_str = "OK" if ping_results.get(ip, False) else "RTO"
                ips_status.append(f"{ip[-8:]}:{status_str}") # Ringkas IP biar log rapi
            
            loss_pct = MONITOR_STATE["targets"][key]["loss_pct"]
            status_visual = MONITOR_STATE["targets"][key]["status"]
            parts.append(
                f"{target['name']} ({status_visual}) => [{' | '.join(ips_status)}] "
                f"Loss: {loss_pct*100:.0f}%"
            )

        # Tambahkan indikator global cooldown di log terminal jika aktif
        cooldown_remaining = max(0, int(COOLDOWN_DURATION - (time.time() - last_failover_time)))
        cooldown_indicator = f" [🔒 COOLDOWN: {cooldown_remaining}s]" if cooldown_remaining > 0 else ""

        print(f"\r[{datetime.now().strftime('%H:%M:%S')}] {' | '.join(parts)}{cooldown_indicator}", end="", flush=True)

        time.sleep(PING_INTERVAL)
        cycle += 1


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n")
        log.info("[!] Monitoring dihentikan oleh admin ganteng (Ctrl+C). Bye!")
        MONITOR_STATE["status"] = "Stopped"
        sys.exit(0)
