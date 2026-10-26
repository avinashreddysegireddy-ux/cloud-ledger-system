"""
Cloud Ledger System - simple base
---------------------------------
* Double-entry bookkeeping (debits must equal credits)
* Tamper-evident ledger (each transaction is hash-chained to the previous one)
* REST API + minimal web UI
* SQLite storage (swap DB_PATH / connection for PostgreSQL when deploying)

Run:  pip install -r requirements.txt && python app.py
Open: http://localhost:5000
"""
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone

from flask import Flask, g, jsonify, render_template_string, request

DB_PATH = os.environ.get("LEDGER_DB", "ledger.db")
ACCOUNT_TYPES = ("asset", "liability", "equity", "revenue", "expense")
DEBIT_NORMAL = ("asset", "expense")

app = Flask(__name__)


# ---------- database helpers ----------
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_):
    conn = g.pop("db", None)
    if conn:
        conn.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            type TEXT NOT NULL CHECK (type IN
                 ('asset','liability','equity','revenue','expense'))
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT NOT NULL,
            description TEXT NOT NULL,
            prev_hash   TEXT NOT NULL,
            hash        TEXT NOT NULL,
            created_at  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS entries (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            txn_id     INTEGER NOT NULL REFERENCES transactions(id),
            account_id INTEGER NOT NULL REFERENCES accounts(id),
            debit      INTEGER NOT NULL DEFAULT 0,   -- cents
            credit     INTEGER NOT NULL DEFAULT 0    -- cents
        );
        """
    )
    conn.commit()
    conn.close()


# ---------- hashing ----------
GENESIS = "0" * 64


def compute_hash(prev_hash, date, description, lines):
    payload = json.dumps(
        {"prev": prev_hash, "date": date, "desc": description, "lines": lines},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def txn_lines(txn_id):
    rows = db().execute(
        "SELECT account_id, debit, credit FROM entries WHERE txn_id=? ORDER BY id",
        (txn_id,),
    ).fetchall()
    return [[r["account_id"], r["debit"], r["credit"]] for r in rows]


# ---------- API: accounts ----------
@app.get("/api/accounts")
def list_accounts():
    rows = db().execute("SELECT * FROM accounts ORDER BY type, name").fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/accounts")
def create_account():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    typ = data.get("type")
    if not name or typ not in ACCOUNT_TYPES:
        return jsonify(error=f"name required; type must be one of {ACCOUNT_TYPES}"), 400
    try:
        cur = db().execute("INSERT INTO accounts(name, type) VALUES (?,?)", (name, typ))
        db().commit()
    except sqlite3.IntegrityError:
        return jsonify(error="account name already exists"), 409
    return jsonify(id=cur.lastrowid, name=name, type=typ), 201


# ---------- API: transactions ----------
@app.get("/api/transactions")
def list_transactions():
    txns = db().execute("SELECT * FROM transactions ORDER BY id DESC").fetchall()
    out = []
    for t in txns:
        lines = db().execute(
            """SELECT a.name, e.debit, e.credit FROM entries e
               JOIN accounts a ON a.id = e.account_id
               WHERE e.txn_id=? ORDER BY e.id""",
            (t["id"],),
        ).fetchall()
        out.append(
            {
                "id": t["id"],
                "date": t["date"],
                "description": t["description"],
                "hash": t["hash"][:12],
                "lines": [
                    {"account": l["name"], "debit": l["debit"] / 100, "credit": l["credit"] / 100}
                    for l in lines
                ],
            }
        )
    return jsonify(out)


@app.post("/api/transactions")
def create_transaction():
    """
    Body: {"date": "2026-10-26", "description": "...",
           "lines": [{"account_id": 1, "debit": 100.00},
                     {"account_id": 2, "credit": 100.00}]}
    """
    data = request.get_json(force=True)
    description = (data.get("description") or "").strip()
    date = data.get("date") or datetime.now(timezone.utc).date().isoformat()
    raw = data.get("lines") or []
    if not description or len(raw) < 2:
        return jsonify(error="description and at least 2 lines required"), 400

    lines = []
    try:
        for l in raw:
            debit = round(float(l.get("debit") or 0) * 100)
            credit = round(float(l.get("credit") or 0) * 100)
            if debit < 0 or credit < 0 or (debit and credit) or not (debit or credit):
                raise ValueError("each line needs exactly one positive debit or credit")
            lines.append([int(l["account_id"]), debit, credit])
    except (ValueError, KeyError, TypeError) as e:
        return jsonify(error=str(e)), 400

    if sum(l[1] for l in lines) != sum(l[2] for l in lines):
        return jsonify(error="transaction not balanced: debits must equal credits"), 400

    conn = db()
    ids = {r["id"] for r in conn.execute("SELECT id FROM accounts")}
    if any(l[0] not in ids for l in lines):
        return jsonify(error="unknown account_id"), 400

    last = conn.execute("SELECT hash FROM transactions ORDER BY id DESC LIMIT 1").fetchone()
    prev = last["hash"] if last else GENESIS
    h = compute_hash(prev, date, description, lines)

    cur = conn.execute(
        "INSERT INTO transactions(date, description, prev_hash, hash, created_at) VALUES (?,?,?,?,?)",
        (date, description, prev, h, datetime.now(timezone.utc).isoformat()),
    )
    txn_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO entries(txn_id, account_id, debit, credit) VALUES (?,?,?,?)",
        [(txn_id, *l) for l in lines],
    )
    conn.commit()
    return jsonify(id=txn_id, hash=h), 201


# ---------- API: balances & verification ----------
@app.get("/api/balances")
def balances():
    rows = db().execute(
        """SELECT a.id, a.name, a.type,
                  COALESCE(SUM(e.debit),0) AS d, COALESCE(SUM(e.credit),0) AS c
           FROM accounts a LEFT JOIN entries e ON e.account_id = a.id
           GROUP BY a.id ORDER BY a.type, a.name"""
    ).fetchall()
    out, total_d, total_c = [], 0, 0
    for r in rows:
        bal = (r["d"] - r["c"]) if r["type"] in DEBIT_NORMAL else (r["c"] - r["d"])
        out.append({"id": r["id"], "name": r["name"], "type": r["type"], "balance": bal / 100})
        total_d += r["d"]
        total_c += r["c"]
    return jsonify(accounts=out, total_debits=total_d / 100, total_credits=total_c / 100,
                   balanced=total_d == total_c)


@app.get("/api/verify")
def verify_chain():
    """Recompute every hash; report the first broken link, if any."""
    prev = GENESIS
    for t in db().execute("SELECT * FROM transactions ORDER BY id"):
        expected = compute_hash(prev, t["date"], t["description"], txn_lines(t["id"]))
        if t["prev_hash"] != prev or t["hash"] != expected:
            return jsonify(valid=False, broken_at_transaction=t["id"])
        prev = t["hash"]
    return jsonify(valid=True)


# ---------- minimal UI ----------
PAGE = """
<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cloud Ledger</title>
<style>
 body{font-family:system-ui,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;color:#222}
 h1{margin-bottom:0} section{margin:1.5rem 0;padding:1rem;border:1px solid #ddd;border-radius:8px}
 input,select,button{padding:.45rem;margin:.2rem;font:inherit}
 button{cursor:pointer} table{width:100%;border-collapse:collapse}
 td,th{padding:.35rem;border-bottom:1px solid #eee;text-align:left}
 .ok{color:#0a7d33}.bad{color:#c00}
</style></head><body>
<h1>Cloud Ledger</h1><p>Double-entry, tamper-evident ledger.</p>

<section><h3>New account</h3>
 <input id="an" placeholder="Account name">
 <select id="at"><option>asset</option><option>liability</option><option>equity</option>
 <option>revenue</option><option>expense</option></select>
 <button onclick="addAccount()">Add</button></section>

<section><h3>New transaction</h3>
 <input id="td" placeholder="Description">
 Debit <select id="dr"></select> Credit <select id="cr"></select>
 <input id="amt" type="number" step="0.01" min="0.01" placeholder="Amount">
 <button onclick="addTxn()">Post</button> <span id="msg" class="bad"></span></section>

<section><h3>Balances</h3><table id="bal"></table>
 <p id="chk"></p><button onclick="verify()">Verify ledger integrity</button></section>

<section><h3>Transactions</h3><table id="txn"></table></section>

<script>
const $=id=>document.getElementById(id);
async function api(url,opt){const r=await fetch(url,opt);return {ok:r.ok,data:await r.json()}}
const post=(u,b)=>api(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
async function load(){
 const a=(await api('/api/balances')).data;
 const opts=a.accounts.map(x=>`<option value="${x.id}">${x.name}</option>`).join('');
 $('dr').innerHTML=opts;$('cr').innerHTML=opts;
 $('bal').innerHTML='<tr><th>Account</th><th>Type</th><th>Balance</th></tr>'+
  a.accounts.map(x=>`<tr><td>${x.name}</td><td>${x.type}</td><td>${x.balance.toFixed(2)}</td></tr>`).join('');
 const t=(await api('/api/transactions')).data;
 $('txn').innerHTML='<tr><th>#</th><th>Date</th><th>Description</th><th>Lines</th><th>Hash</th></tr>'+
  t.map(x=>`<tr><td>${x.id}</td><td>${x.date}</td><td>${x.description}</td><td>`+
   x.lines.map(l=>`${l.account}: ${l.debit?'Dr '+l.debit.toFixed(2):'Cr '+l.credit.toFixed(2)}`).join('<br>')+
   `</td><td><code>${x.hash}</code></td></tr>`).join('');
}
async function addAccount(){
 const r=await post('/api/accounts',{name:$('an').value,type:$('at').value});
 if(!r.ok)alert(r.data.error);$('an').value='';load();}
async function addTxn(){
 const v=parseFloat($('amt').value);
 const r=await post('/api/transactions',{description:$('td').value,lines:[
  {account_id:+$('dr').value,debit:v},{account_id:+$('cr').value,credit:v}]});
 $('msg').textContent=r.ok?'':r.data.error;
 if(r.ok){$('td').value='';$('amt').value='';}load();}
async function verify(){
 const r=(await api('/api/verify')).data;
 $('chk').innerHTML=r.valid?'<span class="ok">Ledger intact ✔</span>'
  :`<span class="bad">Tampering detected at transaction ${r.broken_at_transaction}</span>`;}
load();
</script></body></html>
"""


@app.get("/")
def index():
    return render_template_string(PAGE)


if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5000)
