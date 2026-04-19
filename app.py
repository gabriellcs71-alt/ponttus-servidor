bash

cat /home/claude/ponto_app/servidor/app.py
Saída

from flask import Flask, request, jsonify
from flask_cors import CORS
import sqlite3
import hashlib
import os
from datetime import datetime

app = Flask(__name__)
CORS(app)

DB = "ponto.db"

def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS funcionarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            usuario TEXT UNIQUE NOT NULL,
            senha_hash TEXT NOT NULL,
            matricula TEXT DEFAULT '',
            cargo TEXT DEFAULT '',
            ativo INTEGER DEFAULT 1,
            criado_em TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS registros (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            funcionario_id INTEGER NOT NULL,
            data TEXT NOT NULL,
            cidade TEXT,
            entrada TEXT,
            almoco_inicio TEXT,
            almoco_fim TEXT,
            saida TEXT,
            observacao TEXT,
            assinatura TEXT,
            enviado_em TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (funcionario_id) REFERENCES funcionarios(id)
        );
        INSERT OR IGNORE INTO funcionarios (nome, usuario, senha_hash, ativo)
        VALUES ('Administrador', 'admin', '""" + hashlib.sha256('admin123'.encode()).hexdigest() + """', 1);
    """)
    conn.commit()
    conn.close()

def hash_senha(senha):
    return hashlib.sha256(senha.encode()).hexdigest()

# ── LOGIN ──────────────────────────────────────────────
@app.route('/login', methods=['POST'])
def login():
    data = request.json
    usuario = data.get('usuario','').strip()
    senha = data.get('senha','')
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM funcionarios WHERE usuario=? AND senha_hash=? AND ativo=1",
        (usuario, hash_senha(senha))
    ).fetchone()
    conn.close()
    if row:
        return jsonify({"ok": True, "id": row["id"], "nome": row["nome"], "usuario": row["usuario"], "matricula": row["matricula"], "cargo": row["cargo"]})
    return jsonify({"ok": False, "erro": "Usuário ou senha inválidos"}), 401

# ── REGISTROS ─────────────────────────────────────────
@app.route('/registros', methods=['POST'])
def salvar_registros():
    data = request.json
    funcionario_id = data.get('funcionario_id')
    registros = data.get('registros', [])
    if not funcionario_id or not registros:
        return jsonify({"ok": False, "erro": "Dados inválidos"}), 400
    conn = get_db()
    for reg in registros:
        # Verifica se já existe registro para essa data
        existe = conn.execute(
            "SELECT id FROM registros WHERE funcionario_id=? AND data=?",
            (funcionario_id, reg.get('data'))
        ).fetchone()
        if existe:
            conn.execute("""
                UPDATE registros SET cidade=?, entrada=?, almoco_inicio=?,
                almoco_fim=?, saida=?, observacao=?, assinatura=?, enviado_em=datetime('now')
                WHERE funcionario_id=? AND data=?
            """, (reg.get('cidade'), reg.get('entrada'), reg.get('almoco_inicio'),
                  reg.get('almoco_fim'), reg.get('saida'), reg.get('observacao'),
                  reg.get('assinatura'), funcionario_id, reg.get('data')))
        else:
            conn.execute("""
                INSERT INTO registros (funcionario_id, data, cidade, entrada,
                almoco_inicio, almoco_fim, saida, observacao, assinatura)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (funcionario_id, reg.get('data'), reg.get('cidade'),
                  reg.get('entrada'), reg.get('almoco_inicio'), reg.get('almoco_fim'),
                  reg.get('saida'), reg.get('observacao'), reg.get('assinatura')))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "salvos": len(registros)})

@app.route('/registros/<int:funcionario_id>', methods=['GET'])
def get_registros(funcionario_id):
    mes = request.args.get('mes')  # formato YYYY-MM
    conn = get_db()
    if mes:
        rows = conn.execute(
            "SELECT * FROM registros WHERE funcionario_id=? AND data LIKE ? ORDER BY data",
            (funcionario_id, f"{mes}%")
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM registros WHERE funcionario_id=? ORDER BY data DESC LIMIT 60",
            (funcionario_id,)
        ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

# ── ADMIN: FUNCIONÁRIOS ───────────────────────────────
@app.route('/admin/funcionarios', methods=['GET'])
def listar_funcionarios():
    conn = get_db()
    rows = conn.execute("SELECT id, nome, usuario, matricula, cargo, ativo, criado_em FROM funcionarios ORDER BY nome").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/admin/funcionarios', methods=['POST'])
def criar_funcionario():
    data = request.json
    nome = data.get('nome','').strip()
    usuario = data.get('usuario','').strip()
    senha = data.get('senha','')
    if not nome or not usuario or not senha:
        return jsonify({"ok": False, "erro": "Nome, usuário e senha são obrigatórios"}), 400
    conn = get_db()
    matricula = data.get('matricula','')
    cargo = data.get('cargo','')
    try:
        conn.execute(
            "INSERT INTO funcionarios (nome, usuario, senha_hash, matricula, cargo) VALUES (?,?,?,?,?)",
            (nome, usuario, hash_senha(senha), matricula, cargo)
        )
        conn.commit()
        return jsonify({"ok": True})
    except sqlite3.IntegrityError:
        return jsonify({"ok": False, "erro": "Usuário já existe"}), 409
    finally:
        conn.close()

@app.route('/admin/funcionarios/<int:fid>', methods=['PUT'])
def atualizar_funcionario(fid):
    data = request.json
    conn = get_db()
    if 'senha' in data and data['senha']:
        conn.execute("UPDATE funcionarios SET nome=?, usuario=?, senha_hash=?, matricula=?, cargo=?, ativo=? WHERE id=?",
            (data['nome'], data['usuario'], hash_senha(data['senha']), data.get('matricula',''), data.get('cargo',''), data.get('ativo',1), fid))
    else:
        conn.execute("UPDATE funcionarios SET nome=?, usuario=?, matricula=?, cargo=?, ativo=? WHERE id=?",
            (data['nome'], data['usuario'], data.get('matricula',''), data.get('cargo',''), data.get('ativo',1), fid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route('/admin/registros', methods=['GET'])
def admin_registros():
    mes = request.args.get('mes')
    conn = get_db()
    query = """
        SELECT r.*, f.nome as funcionario_nome
        FROM registros r
        JOIN funcionarios f ON f.id = r.funcionario_id
    """
    params = []
    if mes:
        query += " WHERE r.data LIKE ?"
        params.append(f"{mes}%")
    query += " ORDER BY f.nome, r.data"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/ping', methods=['GET'])
def ping():
    return jsonify({"ok": True, "msg": "Ponto App servidor online"})

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
