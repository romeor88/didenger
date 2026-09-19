import os
import time
import secrets
import sqlite3
from datetime import datetime
from urllib.parse import quote
from functools import wraps

import requests
from dotenv import load_dotenv
from flask import (
    Flask, g, redirect, render_template, request,
    session, url_for, jsonify, flash
)

load_dotenv()

APP_NAME = "Didenger"
DATABASE = os.environ.get("DATABASE_PATH", "didenger.db")
SECRET_KEY = os.environ.get("SECRET_KEY", secrets.token_hex(16))
ADMIN_PIN = os.environ.get("ADMIN_PIN", "2468")
ADMIN_WA = os.environ.get("ADMIN_WA", "6281234567890")
XAI_API_KEY = os.environ.get("XAI_API_KEY", "")
XAI_MODEL = os.environ.get("XAI_MODEL", "grok-4-1-fast")
XAI_URL = os.environ.get("XAI_URL", "https://api.x.ai/v1/chat/completions")

PACKS = {
    "try": {
        "name": "Coba dulu",
        "base": 9900,
        "minutes": 60,
        "days": 2,
        "daily_cap": None,
        "label": "60 menit",
    },
    "plus": {
        "name": "Plong dulu",
        "base": 19900,
        "minutes": 135,
        "days": 7,
        "daily_cap": None,
        "label": "3 sesi x 45 menit",
    },
    "week": {
        "name": "Teman seminggu",
        "base": 29900,
        "minutes": 140,
        "days": 7,
        "daily_cap": 20,
        "label": "7 hari, max 20 menit/hari",
    },
}

SYSTEM_PROMPT = """Kamu adalah Didenger, teman curhat berbahasa Indonesia yang hangat, santai, dan tidak menghakimi.

Gaya:
- Bahasa gaul yang sopan. Boleh "aku-kamu".
- Jawaban pendek sampai sedang. Jangan ceramah.
- Dengar dulu. Jangan langsung kasih 10 solusi.
- Cerminin perasaan user dalam 1 kalimat, baru tanya 1 pertanyaan bagus.
- Ingat nama, masalah utama, dan orang yang disebut di sesi ini.

Dilarang:
- Mengaku sebagai psikolog, dokter, ustadz resmi, atau manusia.
- Mendiagnosis (depresi, bipolar, dll) atau meresepkan obat.
- Mendorong putus sekolah, resign mendadak, balas dendam, atau menyakiti orang.
- Roleplay pacar mesra / konten seksual. Kalau diminta, tolak halus dan balik ke curhat.
- Membuat janji "kamu pasti sembuh" atau "aku selalu ada selamanya".

Kalau user menyebut ingin mati, nyakitin diri, atau menyakiti orang lain:
1. Berhenti memberi saran biasa.
2. Bilang kamu khawatir dan ini di luar kapasitas bot.
3. Suruh segera hubungi 119 ext. 8, IGD, atau orang yang bisa menemani sekarang.
4. Tetap hangat, jangan panik dan jangan menguliahi.

Kalau user hanya butuh didengar, jangan memaksa langkah next.
Tutup sesi dengan lembut saat mereka bilang udahan.
Kalau sisa waktu tinggal sedikit, ingatkan pelan.
"""

app = Flask(__name__)
app.secret_key = SECRET_KEY


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DATABASE)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pack TEXT NOT NULL,
            amount INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'waiting',
            wa TEXT,
            token TEXT UNIQUE,
            minutes_left INTEGER NOT NULL,
            daily_cap INTEGER,
            minutes_today INTEGER DEFAULT 0,
            day_stamp TEXT,
            expires_at INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            paid_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            FOREIGN KEY(order_id) REFERENCES orders(id)
        );
        """
    )
    db.commit()
    db.close()


def now():
    return int(time.time())


def fmt_rp(n):
    return f"Rp {n:,}".replace(",", ".")


def next_amount(db, pack):
    base = PACKS[pack]["base"]
    cutoff = now() - 3 * 3600
    db.execute(
        "UPDATE orders SET status='expired' WHERE status='waiting' AND created_at < ?",
        (cutoff,),
    )
    used = {
        r["amount"]
        for r in db.execute(
            "SELECT amount FROM orders WHERE pack=? AND status='waiting'",
            (pack,),
        )
    }
    for suffix in range(1, 100):
        amount = base + suffix
        if amount not in used:
            return amount
    raise RuntimeError("Semua kode nominal sedang dipakai. Coba pack lain.")


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return fn(*args, **kwargs)
    return wrapper


@app.route("/")
def index():
    return render_template("index.html", packs=PACKS, wa=ADMIN_WA, app_name=APP_NAME)


@app.route("/order", methods=["POST"])
def create_order():
    pack = request.form.get("pack")
    if pack not in PACKS:
        return "Pack tidak valid", 400
    if request.form.get("age") != "yes":
        flash("Kamu harus 18+ untuk lanjut.")
        return redirect(url_for("index"))
    db = get_db()
    amount = next_amount(db, pack)
    token = secrets.token_urlsafe(16)
    p = PACKS[pack]
    db.execute(
        """
        INSERT INTO orders (pack, amount, wa, token, minutes_left, daily_cap, expires_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pack,
            amount,
            request.form.get("wa", "").strip(),
            token,
            p["minutes"],
            p["daily_cap"],
            now() + p["days"] * 86400,
            now(),
        ),
    )
    db.commit()
    return redirect(url_for("pay", amount=amount))


@app.route("/pay/<int:amount>")
def pay(amount):
    db = get_db()
    order = db.execute(
        "SELECT * FROM orders WHERE amount=? ORDER BY id DESC", (amount,)
    ).fetchone()
    if not order:
        return "Pesanan tidak ketemu", 404
    wa_link = "https://wa.me/" + ADMIN_WA + "?text=" + quote(
        f"Halo Didenger, saya sudah bayar {fmt_rp(order['amount'])}"
    )
    return render_template(
        "pay.html",
        order=order,
        pack=PACKS[order["pack"]],
        amount_fmt=fmt_rp(order["amount"]),
        wa_link=wa_link,
        app_name=APP_NAME,
    )


@app.route("/chat/<token>")
def chat_page(token):
    db = get_db()
    order = db.execute("SELECT * FROM orders WHERE token=?", (token,)).fetchone()
    if not order:
        return "Link tidak valid", 404
    if order["status"] != "paid":
        return redirect(url_for("pay", amount=order["amount"]))
    if order["expires_at"] < now() or order["minutes_left"] <= 0:
        return render_template("expired.html", app_name=APP_NAME)
    msgs = db.execute(
        "SELECT role, content FROM messages WHERE order_id=? ORDER BY id ASC",
        (order["id"],),
    ).fetchall()
    return render_template(
        "chat.html",
        order=order,
        messages=msgs,
        app_name=APP_NAME,
        token=token,
    )


@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.get_json(force=True)
    token = data.get("token")
    text = (data.get("message") or "").strip()
    if not token or not text:
        return jsonify({"error": "Pesan kosong"}), 400
    if len(text) > 4000:
        return jsonify({"error": "Pesan terlalu panjang"}), 400

    db = get_db()
    order = db.execute("SELECT * FROM orders WHERE token=?", (token,)).fetchone()
    if not order or order["status"] != "paid":
        return jsonify({"error": "Sesi belum aktif"}), 403
    if order["expires_at"] < now() or order["minutes_left"] <= 0:
        return jsonify({"error": "Sesi sudah habis"}), 403

    today = datetime.now().strftime("%Y-%m-%d")
    minutes_today = order["minutes_today"] or 0
    if order["day_stamp"] != today:
        minutes_today = 0
    if order["daily_cap"] and minutes_today >= order["daily_cap"]:
        return jsonify({"error": "Batas hari ini sudah habis. Balik besok ya."}), 403

    db.execute(
        "INSERT INTO messages (order_id, role, content, created_at) VALUES (?, 'user', ?, ?)",
        (order["id"], text, now()),
    )
    history = db.execute(
        "SELECT role, content FROM messages WHERE order_id=? ORDER BY id DESC LIMIT 20",
        (order["id"],),
    ).fetchall()
    history = list(reversed(history))
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for m in history:
        messages.append({"role": m["role"], "content": m["content"]})

    reply = call_grok(messages)
    db.execute(
        "INSERT INTO messages (order_id, role, content, created_at) VALUES (?, 'assistant', ?, ?)",
        (order["id"], reply, now()),
    )
    new_left = max(0, order["minutes_left"] - 1)
    db.execute(
        "UPDATE orders SET minutes_left=?, minutes_today=?, day_stamp=? WHERE id=?",
        (new_left, minutes_today + 1, today, order["id"]),
    )
    db.commit()
    return jsonify({"reply": reply, "minutes_left": new_left})


def call_grok(messages):
    if not XAI_API_KEY:
        last = messages[-1]["content"]
        return (
            "Aku dengerin. (Mode tes: API key Grok belum diisi.) "
            f"Kamu bilang: {last[:180]} "
            "Kalau berat banget dan ada pikiran nyakitin diri, hubungi 119 ext. 8 ya."
        )
    headers = {
        "Authorization": f"Bearer {XAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": XAI_MODEL,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 400,
    }
    try:
        r = requests.post(XAI_URL, headers=headers, json=payload, timeout=60)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return (
            "Aku masih di sini, tapi koneksi bot lagi gangguan sebentar. "
            "Coba kirim lagi. Kalau darurat, 119 ext. 8."
        )


@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if request.form.get("pin") == ADMIN_PIN:
            session["admin"] = True
            return redirect(url_for("admin_home"))
        flash("PIN salah")
    return render_template("admin_login.html", app_name=APP_NAME)


@app.route("/admin/home")
@admin_required
def admin_home():
    db = get_db()
    waiting = db.execute(
        "SELECT * FROM orders WHERE status='waiting' ORDER BY id DESC"
    ).fetchall()
    paid = db.execute(
        "SELECT * FROM orders WHERE status='paid' ORDER BY id DESC LIMIT 30"
    ).fetchall()
    return render_template(
        "admin.html",
        waiting=waiting,
        paid=paid,
        app_name=APP_NAME,
        fmt_rp=fmt_rp,
    )


@app.route("/admin/pay/<int:order_id>", methods=["POST"])
@admin_required
def admin_mark_paid(order_id):
    db = get_db()
    db.execute(
        "UPDATE orders SET status='paid', paid_at=? WHERE id=? AND status='waiting'",
        (now(), order_id),
    )
    db.commit()
    order = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    flash(f"Aktif. Link: {request.host_url}chat/{order['token']}")
    return redirect(url_for("admin_home"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)