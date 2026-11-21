import os
import hmac
import sqlite3
from functools import wraps
from urllib.parse import urlencode

import requests
from flask import Flask, abort, redirect, render_template_string, request, session, url_for, flash

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "")
if not app.secret_key:
    raise RuntimeError("Configure FLASK_SECRET_KEY dans les variables d'environnement.")

DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "")
DISCORD_REDIRECT_URI = os.environ.get("DISCORD_REDIRECT_URI", "")
DISCORD_GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "")
ADMIN_ROLE_ID = os.environ.get("ADMIN_ROLE_ID", "")
BOT_API_TOKEN = os.environ.get("BOT_API_TOKEN", "")
DATABASE = os.environ.get("DATABASE_PATH", "/tmp/kayoux.db")

DISCORD_API = "https://discord.com/api/v10"

def db():
    os.makedirs(os.path.dirname(DATABASE) or ".", exist_ok=True)
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS transfers (
                id TEXT PRIMARY KEY,
                guild_id TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                recipient_id TEXT NOT NULL,
                player TEXT NOT NULL,
                operation TEXT NOT NULL,
                price TEXT,
                loan_type TEXT,
                duration TEXT,
                status TEXT NOT NULL,
                created_at TEXT,
                decision TEXT,
                decision_by TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_commands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                transfer_id TEXT NOT NULL,
                action TEXT NOT NULL,
                reason TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                delivered INTEGER DEFAULT 0
            )
        """)

def configured():
    return all([DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET, DISCORD_REDIRECT_URI,
                DISCORD_GUILD_ID, ADMIN_ROLE_ID, BOT_API_TOKEN])

def discord_get(path, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = requests.get(DISCORD_API + path, headers=headers, timeout=15)
    response.raise_for_status()
    return response.json()

def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper

@app.get("/")
def home():
    if not session.get("admin"):
        return redirect(url_for("login"))
    return redirect(url_for("dashboard"))

@app.get("/login")
def login():
    if not configured():
        return ("Le panel n'est pas encore configuré. Ajoute les variables d'environnement Render "
                "indiquées dans le guide avant de te connecter.", 503)
    state = os.urandom(24).hex()
    session["oauth_state"] = state
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify guilds.members.read",
        "state": state,
        "prompt": "consent"
    }
    return redirect("https://discord.com/oauth2/authorize?" + urlencode(params))

@app.get("/oauth/callback")
def oauth_callback():
    if request.args.get("state") != session.get("oauth_state"):
        abort(400, "État OAuth invalide.")
    code = request.args.get("code")
    if not code:
        abort(400, "Autorisation Discord manquante.")
    token_response = requests.post(
        DISCORD_API + "/oauth2/token",
        data={
            "client_id": DISCORD_CLIENT_ID,
            "client_secret": DISCORD_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": DISCORD_REDIRECT_URI,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    token_response.raise_for_status()
    access_token = token_response.json()["access_token"]
    user = discord_get("/users/@me", access_token)
    member = discord_get(f"/users/@me/guilds/{DISCORD_GUILD_ID}/member", access_token)
    roles = member.get("roles", [])
    if ADMIN_ROLE_ID not in roles:
        session.clear()
        return ("Accès refusé : le rôle adminkayoux est requis.", 403)
    session.clear()
    session["admin"] = {
        "id": user["id"],
        "username": user.get("global_name") or user["username"],
        "avatar": user.get("avatar"),
    }
    return redirect(url_for("dashboard"))

@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.get("/dashboard")
@admin_required
def dashboard():
    with db() as conn:
        transfers = conn.execute(
            "SELECT * FROM transfers ORDER BY created_at DESC LIMIT 100"
        ).fetchall()
    pending = sum(1 for t in transfers if t["status"] in ("attente_admin", "attente_coach"))
    return render_template_string(PAGE, admin=session["admin"], transfers=transfers, pending=pending)

@app.post("/transfer/<transfer_id>/<action>")
@admin_required
def decide_transfer(transfer_id, action):
    if action not in ("valider", "refuser"):
        abort(400)
    reason = request.form.get("reason", "").strip()[:500]
    with db() as conn:
        transfer = conn.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
        if not transfer:
            abort(404)
        if transfer["status"] not in ("attente_admin", "attente_coach"):
            flash("Cette proposition a déjà été traitée.")
            return redirect(url_for("dashboard"))
        new_status = "validee" if action == "valider" else "refusee_admin"
        conn.execute(
            "UPDATE transfers SET status=?, decision=?, decision_by=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (new_status, reason, session["admin"]["id"], transfer_id)
        )
        conn.execute(
            "INSERT INTO bot_commands (transfer_id, action, reason) VALUES (?, ?, ?)",
            (transfer_id, action, reason)
        )
    flash("Décision enregistrée. Elle sera transmise au bot Kayoux.")
    return redirect(url_for("dashboard"))

def bot_auth():
    supplied = request.headers.get("X-Kayoux-Token", "")
    if not BOT_API_TOKEN or not hmac.compare_digest(supplied, BOT_API_TOKEN):
        abort(401)

@app.post("/api/transfers/sync")
def api_sync_transfer():
    bot_auth()
    data = request.get_json(silent=True) or {}
    required = ["id", "guild_id", "sender_id", "recipient_id", "player", "operation", "status"]
    if any(not data.get(k) for k in required):
        abort(400, "Champs obligatoires manquants.")
    with db() as conn:
        conn.execute("""
            INSERT INTO transfers
            (id,guild_id,sender_id,recipient_id,player,operation,price,loan_type,duration,status,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
            status=excluded.status, price=excluded.price, loan_type=excluded.loan_type,
            duration=excluded.duration
        """, tuple(data.get(k) for k in [
            "id","guild_id","sender_id","recipient_id","player","operation",
            "price","loan_type","duration","status","created_at"
        ]))
    return {"ok": True}

@app.get("/api/bot/commands")
def api_bot_commands():
    bot_auth()
    with db() as conn:
        rows = conn.execute(
            "SELECT id, transfer_id, action, reason FROM bot_commands WHERE delivered=0 ORDER BY id"
        ).fetchall()
    return {"commands": [dict(r) for r in rows]}

@app.post("/api/bot/commands/<int:command_id>/ack")
def api_ack_command(command_id):
    bot_auth()
    with db() as conn:
        conn.execute("UPDATE bot_commands SET delivered=1 WHERE id=?", (command_id,))
    return {"ok": True}

PAGE = r"""
<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kayoux Admin</title>
<style>
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#10131b;color:#eef2ff;font:15px system-ui}
header{padding:22px 5%;background:#191e2b;border-bottom:1px solid #30384b;display:flex;justify-content:space-between;align-items:center}
main{max-width:1150px;margin:35px auto;padding:0 18px}.muted{color:#aab3c7}.stat{background:#1b2231;padding:18px;border:1px solid #30384b;border-radius:14px;margin-bottom:22px}
table{width:100%;border-collapse:collapse;background:#1b2231;border-radius:14px;overflow:hidden}th,td{text-align:left;padding:13px;border-bottom:1px solid #30384b}th{color:#aab3c7;font-size:12px;text-transform:uppercase}
button{border:0;border-radius:8px;padding:9px 13px;cursor:pointer;font-weight:700}.ok{background:#23865b;color:white}.no{background:#a43c4c;color:white}input{background:#10131b;color:white;border:1px solid #46516a;border-radius:7px;padding:8px;width:140px}
a{color:#c6d1ff}.empty{padding:30px;text-align:center}.actions{display:flex;gap:7px;flex-wrap:wrap}
@media(max-width:700px){table,tbody,tr,td{display:block}thead{display:none}tr{padding:10px;border-bottom:1px solid #30384b}td{border:0;padding:6px}}
</style></head><body>
<header><div><strong>🪨 KAYOUX ADMIN</strong><div class="muted">Panel de gestion</div></div>
<div>{{ admin.username }} · <a href="/logout">Déconnexion</a></div></header>
<main><div class="stat"><strong>{{ pending }}</strong> proposition(s) en attente de décision administrative</div>
<h2>Gestion des transferts</h2>
{% if transfers %}
<table><thead><tr><th>Joueur / opération</th><th>Coachs</th><th>Prix</th><th>Statut</th><th>Décision</th></tr></thead><tbody>
{% for t in transfers %}<tr>
<td><strong>{{ t.player }}</strong><br><span class="muted">{{ t.operation }}{% if t.loan_type %} · {{ t.loan_type }}{% endif %}</span></td>
<td><span class="muted">Demandeur :</span> {{ t.sender_id }}<br><span class="muted">Destinataire :</span> {{ t.recipient_id }}</td>
<td>{{ t.price or "—" }}{% if t.duration %}<br>{{ t.duration }}{% endif %}</td>
<td>{{ t.status }}</td><td>
{% if t.status in ("attente_admin","attente_coach") %}
<form method="post" action="/transfer/{{ t.id }}/valider" class="actions"><button class="ok">Valider</button></form>
<form method="post" action="/transfer/{{ t.id }}/refuser" class="actions"><input name="reason" placeholder="Motif (facultatif)"><button class="no">Refuser</button></form>
{% else %}<span class="muted">Traité</span>{% endif %}
</td></tr>{% endfor %}
</tbody></table>
{% else %}<div class="stat empty">Aucun transfert synchronisé pour le moment.<br><span class="muted">La liaison avec le bot sera activée à l'étape suivante.</span></div>{% endif %}
</main></body></html>
"""

init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
