from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import sqlite3
import hashlib
import os
import re
import secrets
import time
from functools import wraps

app = Flask(__name__)
CORS(app)

DB           = os.environ.get('DB_PATH', 'ponto.db')
ADMIN_KEY    = os.environ.get('ADMIN_API_KEY', '')  # Defina esta var para proteger rotas admin
DATABASE_URL = os.environ.get('DATABASE_URL', '')   # Render injeta isto ao linkar um Postgres
USE_PG       = bool(DATABASE_URL)                    # Postgres em produção, SQLite local

# Rate limiting simples em memória: {ip: [timestamps]}
_login_attempts: dict = {}
LOGIN_MAX = 10
LOGIN_WINDOW = 60  # segundos

TOKEN_VALIDADE_DIAS = 90  # token expira após 90 dias sem uso

# Tipos de "abono" que SOMENTE o admin pode lançar (funcionário nunca grava estes).
# '' (vazio) = dia normal de trabalho, preenchido pelo próprio funcionário no app.
ABONO_TIPOS = ('falta', 'atestado', 'folga', 'feriado')

if USE_PG:
    import psycopg
    from psycopg.rows import dict_row
    INTEGRITY_ERRORS = (sqlite3.IntegrityError, psycopg.IntegrityError)
else:
    INTEGRITY_ERRORS = (sqlite3.IntegrityError,)

# Expressões de data/hora por backend — ambas produzem ISO 'YYYY-MM-DD HH:MM:SS' (UTC),
# garantindo que comparações por texto e o formato de saída sejam idênticos nos dois bancos.
if USE_PG:
    NOW_SQL    = "to_char(now() at time zone 'utc','YYYY-MM-DD HH24:MI:SS')"
    CUTOFF_SQL = ("to_char((now() - interval '%d days') at time zone 'utc',"
                  "'YYYY-MM-DD HH24:MI:SS')") % TOKEN_VALIDADE_DIAS
else:
    NOW_SQL    = "datetime('now')"
    CUTOFF_SQL = "datetime('now','-%d days')" % TOKEN_VALIDADE_DIAS

# ── BANCO DE DADOS ─────────────────────────────────────
class _Conn:
    """Adaptador fino: mesma API (.execute(...).fetchone()/fetchall(), .commit(),
    .rollback(), .close()) funcionando tanto em SQLite quanto em Postgres.
    Em Postgres, traduz os placeholders '?' para '%s' automaticamente."""
    def __init__(self):
        if USE_PG:
            self._c = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        else:
            self._c = sqlite3.connect(DB)
            self._c.row_factory = sqlite3.Row

    def execute(self, sql, params=()):
        if USE_PG:
            cur = self._c.cursor()
            cur.execute(sql.replace('?', '%s'), tuple(params) or None)
            return cur
        return self._c.execute(sql, params)

    def commit(self):
        self._c.commit()

    def rollback(self):
        self._c.rollback()

    def close(self):
        self._c.close()

def get_db():
    return _Conn()

def _schema_sql():
    """DDL específico do backend. Datas/timestamps são TEXT (ISO) em ambos."""
    if USE_PG:
        return [
            """CREATE TABLE IF NOT EXISTS funcionarios (
                id SERIAL PRIMARY KEY,
                nome TEXT NOT NULL,
                usuario TEXT UNIQUE NOT NULL,
                senha_hash TEXT NOT NULL,
                matricula TEXT DEFAULT '',
                cargo TEXT DEFAULT '',
                ativo INTEGER DEFAULT 1,
                criado_em TEXT DEFAULT (to_char(now() at time zone 'utc','YYYY-MM-DD HH24:MI:SS'))
            )""",
            """CREATE TABLE IF NOT EXISTS registros (
                id SERIAL PRIMARY KEY,
                funcionario_id INTEGER NOT NULL REFERENCES funcionarios(id),
                data TEXT NOT NULL,
                cidade TEXT,
                entrada TEXT,
                almoco_inicio TEXT,
                almoco_fim TEXT,
                saida TEXT,
                observacao TEXT,
                tipo TEXT DEFAULT '',
                enviado_em TEXT DEFAULT (to_char(now() at time zone 'utc','YYYY-MM-DD HH24:MI:SS'))
            )""",
            """CREATE TABLE IF NOT EXISTS tokens (
                token TEXT PRIMARY KEY,
                funcionario_id INTEGER NOT NULL REFERENCES funcionarios(id),
                criado_em TEXT DEFAULT (to_char(now() at time zone 'utc','YYYY-MM-DD HH24:MI:SS')),
                ultimo_uso TEXT DEFAULT (to_char(now() at time zone 'utc','YYYY-MM-DD HH24:MI:SS'))
            )""",
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_reg_func_data
                ON registros(funcionario_id, data)""",
        ]
    return [
        """CREATE TABLE IF NOT EXISTS funcionarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            usuario TEXT UNIQUE NOT NULL,
            senha_hash TEXT NOT NULL,
            matricula TEXT DEFAULT '',
            cargo TEXT DEFAULT '',
            ativo INTEGER DEFAULT 1,
            criado_em TEXT DEFAULT (datetime('now'))
        )""",
        """CREATE TABLE IF NOT EXISTS registros (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            funcionario_id INTEGER NOT NULL,
            data TEXT NOT NULL,
            cidade TEXT,
            entrada TEXT,
            almoco_inicio TEXT,
            almoco_fim TEXT,
            saida TEXT,
            observacao TEXT,
            tipo TEXT DEFAULT '',
            enviado_em TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (funcionario_id) REFERENCES funcionarios(id)
        )""",
        """CREATE TABLE IF NOT EXISTS tokens (
            token TEXT PRIMARY KEY,
            funcionario_id INTEGER NOT NULL,
            criado_em TEXT DEFAULT (datetime('now')),
            ultimo_uso TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (funcionario_id) REFERENCES funcionarios(id)
        )""",
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_reg_func_data
            ON registros(funcionario_id, data)""",
    ]

def init_db():
    conn = get_db()
    for stmt in _schema_sql():
        conn.execute(stmt)
    conn.commit()  # consolida o schema antes das migrações (evita rollback derrubar as tabelas no PG)

    # Migração (somente SQLite): converte datas antigas DD/MM/YYYY para ISO YYYY-MM-DD
    if not USE_PG:
        conn.execute("""
            UPDATE registros
            SET data = substr(data,7,4) || '-' || substr(data,4,2) || '-' || substr(data,1,2)
            WHERE data LIKE '__/__/____'
        """)

    # Migração: garante a coluna 'tipo' em bancos criados antes desta feature.
    try:
        conn.execute("ALTER TABLE registros ADD COLUMN tipo TEXT DEFAULT ''")
        conn.commit()
    except Exception:
        conn.rollback()  # coluna já existe

    # Migração: feriado deixou de ser marcado pelo funcionário (observacao='FERIADO')
    # e passou a ser um abono administrativo (tipo='feriado').
    conn.execute(
        "UPDATE registros SET tipo='feriado' WHERE observacao='FERIADO' AND (tipo='' OR tipo IS NULL)"
    )

    # Admin: usa env var se definida, senão gera senha aleatória na primeira execução
    senha_admin = os.environ.get('ADMIN_PASSWORD', '')
    existe = conn.execute("SELECT id FROM funcionarios WHERE usuario='admin'").fetchone()
    if not existe:
        if not senha_admin:
            senha_admin = secrets.token_urlsafe(12)
            print("\n" + "="*45)
            print("  ADMIN CRIADO — ANOTE ESTA SENHA:")
            print(f"  Usuário : admin")
            print(f"  Senha   : {senha_admin}")
            print("  (não será exibida novamente)")
            print("="*45 + "\n")
        conn.execute(
            "INSERT INTO funcionarios (nome, usuario, senha_hash, cargo, ativo) VALUES (?,?,?,?,1)",
            ('Administrador', 'admin', _hash_pbkdf2_full(senha_admin), 'ADMIN')
        )
    elif senha_admin:
        # Atualiza hash do admin se env var foi definida (ex: rotação de senha)
        conn.execute(
            "UPDATE funcionarios SET senha_hash=? WHERE usuario='admin'",
            (_hash_pbkdf2_full(senha_admin),)
        )

    conn.commit()
    conn.close()

# ── HASHING ────────────────────────────────────────────
def _hash_pbkdf2_full(senha: str) -> str:
    """Retorna 'pbkdf2:<salt>:<hash>' usando PBKDF2-HMAC-SHA256 com 260k iterações."""
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac('sha256', senha.encode('utf-8'), salt.encode(), 260_000)
    return f"pbkdf2:{salt}:{dk.hex()}"

def verificar_senha(senha: str, stored: str) -> bool:
    """Suporta PBKDF2 (novo) e SHA256 puro (legado), usando compare_digest."""
    if stored.startswith('pbkdf2:'):
        parts = stored.split(':', 2)
        if len(parts) != 3:
            return False
        _, salt, expected = parts
        dk = hashlib.pbkdf2_hmac('sha256', senha.encode('utf-8'), salt.encode(), 260_000)
        return secrets.compare_digest(dk.hex(), expected)
    # Legado: SHA256 sem salt
    return secrets.compare_digest(hashlib.sha256(senha.encode()).hexdigest(), stored)

# ── DATAS ──────────────────────────────────────────────
_RE_ISO = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_RE_BR  = re.compile(r'^(\d{2})/(\d{2})/(\d{4})$')

def normalizar_data(d: str):
    """Aceita YYYY-MM-DD ou DD/MM/YYYY e retorna ISO, ou None se inválida."""
    d = str(d or '').strip()
    if _RE_ISO.match(d):
        return d
    m = _RE_BR.match(d)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return None

def _validar_mes(mes: str) -> bool:
    return bool(re.match(r'^\d{4}-(0[1-9]|1[0-2])$', mes))

# ── UTILITÁRIOS ────────────────────────────────────────
def _check_rate_limit(ip: str) -> bool:
    now = time.time()
    attempts = [t for t in _login_attempts.get(ip, []) if now - t < LOGIN_WINDOW]
    if len(attempts) >= LOGIN_MAX:
        _login_attempts[ip] = attempts
        return False
    attempts.append(now)
    _login_attempts[ip] = attempts
    return True

def require_admin_key(f):
    """Decorator: exige header X-Admin-Key quando ADMIN_API_KEY está configurado."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if ADMIN_KEY:
            provided = request.headers.get('X-Admin-Key', '')
            if not secrets.compare_digest(provided, ADMIN_KEY):
                return jsonify({"ok": False, "erro": "Não autorizado"}), 401
        return f(*args, **kwargs)
    return decorated

def _funcionario_do_token():
    """Valida o header Authorization: Bearer <token>. Retorna funcionario_id ou None."""
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return None
    token = auth[7:].strip()
    if not token:
        return None
    conn = get_db()
    row = conn.execute(
        f"""SELECT funcionario_id FROM tokens
           WHERE token=? AND ultimo_uso > {CUTOFF_SQL}""",
        (token,)
    ).fetchone()
    if row:
        conn.execute(f"UPDATE tokens SET ultimo_uso={NOW_SQL} WHERE token=?", (token,))
        conn.commit()
    conn.close()
    return row['funcionario_id'] if row else None

def require_token(f):
    """Decorator: exige token válido e injeta funcionario_id autenticado."""
    @wraps(f)
    def decorated(*args, **kwargs):
        fid = _funcionario_do_token()
        if fid is None:
            return jsonify({"ok": False, "erro": "Sessão expirada. Faça login novamente."}), 401
        return f(fid, *args, **kwargs)
    return decorated

# ── LOGIN ──────────────────────────────────────────────
@app.route('/login', methods=['POST'])
def login():
    ip = request.remote_addr or '0.0.0.0'
    if not _check_rate_limit(ip):
        return jsonify({"ok": False, "erro": "Muitas tentativas. Aguarde 1 minuto."}), 429

    data = request.get_json(silent=True) or {}
    usuario = str(data.get('usuario', '')).strip()[:50]
    senha   = str(data.get('senha', ''))[:100]

    if not usuario or not senha:
        return jsonify({"ok": False, "erro": "Usuário e senha obrigatórios"}), 400

    conn = get_db()
    row = conn.execute(
        "SELECT * FROM funcionarios WHERE usuario=? AND ativo=1", (usuario,)
    ).fetchone()

    if row and verificar_senha(senha, row['senha_hash']):
        token = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO tokens (token, funcionario_id) VALUES (?,?)",
            (token, row['id'])
        )
        # Limpa tokens expirados do funcionário
        conn.execute(
            f"DELETE FROM tokens WHERE funcionario_id=? AND ultimo_uso < {CUTOFF_SQL}",
            (row['id'],)
        )
        conn.commit()
        conn.close()
        return jsonify({
            "ok": True, "id": row["id"], "nome": row["nome"],
            "usuario": row["usuario"], "matricula": row["matricula"],
            "cargo": row["cargo"], "token": token
        })
    conn.close()
    return jsonify({"ok": False, "erro": "Usuário ou senha inválidos"}), 401

# ── TROCAR SENHA (funcionário) ────────────────────────
@app.route('/senha', methods=['POST'])
@require_token
def trocar_senha(auth_fid):
    """O funcionário autenticado troca a própria senha (exige a senha atual)."""
    data = request.get_json(silent=True) or {}
    atual = str(data.get('senha_atual', '') or '')[:100]
    nova  = str(data.get('nova_senha', '') or '')[:100]
    if len(nova) < 6:
        return jsonify({"ok": False, "erro": "A nova senha deve ter no mínimo 6 caracteres"}), 400

    conn = get_db()
    row = conn.execute("SELECT senha_hash FROM funcionarios WHERE id=?", (auth_fid,)).fetchone()
    if not row or not verificar_senha(atual, row['senha_hash']):
        conn.close()
        return jsonify({"ok": False, "erro": "Senha atual incorreta"}), 401
    conn.execute("UPDATE funcionarios SET senha_hash=? WHERE id=?",
                 (_hash_pbkdf2_full(nova), auth_fid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# ── REGISTROS ─────────────────────────────────────────
@app.route('/registros', methods=['POST'])
@require_token
def salvar_registros(auth_fid):
    data = request.get_json(silent=True) or {}
    registros = data.get('registros', [])

    if not isinstance(registros, list) or not registros or len(registros) > 500:
        return jsonify({"ok": False, "erro": "Dados inválidos"}), 400

    funcionario_id = auth_fid  # ignora id do payload: usa sempre o do token

    conn = get_db()
    salvos = 0
    for reg in registros:
        data_iso = normalizar_data(reg.get('data', ''))
        if not data_iso:
            continue

        campos = (
            str(reg.get('cidade')    or '')[:100],
            str(reg.get('entrada')   or '')[:5],
            str(reg.get('almoco_inicio') or '')[:5],
            str(reg.get('almoco_fim')    or '')[:5],
            str(reg.get('saida')     or '')[:5],
            str(reg.get('observacao')or '')[:500],
        )

        # Funcionário só grava dia de trabalho (tipo=''); se o dia já é um abono
        # lançado pelo admin, o WHERE impede a sobrescrita — o abono prevalece.
        conn.execute(f"""
            INSERT INTO registros (funcionario_id, data, cidade, entrada,
                almoco_inicio, almoco_fim, saida, observacao, tipo)
            VALUES (?,?,?,?,?,?,?,?,'')
            ON CONFLICT(funcionario_id, data) DO UPDATE SET
                cidade=excluded.cidade, entrada=excluded.entrada,
                almoco_inicio=excluded.almoco_inicio, almoco_fim=excluded.almoco_fim,
                saida=excluded.saida, observacao=excluded.observacao,
                enviado_em={NOW_SQL}
            WHERE registros.tipo='' OR registros.tipo IS NULL
        """, (funcionario_id, data_iso, *campos))
        salvos += 1

    conn.commit()
    conn.close()
    return jsonify({"ok": True, "salvos": salvos})

@app.route('/registros', methods=['GET'])
@require_token
def get_registros(auth_fid):
    mes = request.args.get('mes', '')
    conn = get_db()
    if mes:
        if not _validar_mes(mes):
            conn.close()
            return jsonify({"ok": False, "erro": "Formato de mês inválido (use YYYY-MM)"}), 400
        rows = conn.execute(
            "SELECT * FROM registros WHERE funcionario_id=? AND data LIKE ? ORDER BY data",
            (auth_fid, f"{mes}%")
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM registros WHERE funcionario_id=? ORDER BY data DESC LIMIT 120",
            (auth_fid,)
        ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

# ── ADMIN: FUNCIONÁRIOS ───────────────────────────────
@app.route('/admin/funcionarios', methods=['GET'])
@require_admin_key
def listar_funcionarios():
    conn = get_db()
    try:
        limit  = min(int(request.args.get('limit', 100)), 500)
        offset = max(int(request.args.get('offset', 0)), 0)
    except ValueError:
        conn.close()
        return jsonify({"ok": False, "erro": "Parâmetros inválidos"}), 400
    rows = conn.execute(
        "SELECT id, nome, usuario, matricula, cargo, ativo, criado_em FROM funcionarios ORDER BY nome LIMIT ? OFFSET ?",
        (limit, offset)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/admin/funcionarios', methods=['POST'])
@require_admin_key
def criar_funcionario():
    data    = request.get_json(silent=True) or {}
    nome    = str(data.get('nome',    '') or '').strip()[:100]
    usuario = str(data.get('usuario', '') or '').strip().lower()[:50]
    senha   = str(data.get('senha',   '') or '')[:100]

    if not nome or not usuario or not senha:
        return jsonify({"ok": False, "erro": "Nome, usuário e senha são obrigatórios"}), 400
    if not re.match(r'^[a-z0-9._-]{3,50}$', usuario):
        return jsonify({"ok": False, "erro": "Usuário deve ter 3-50 caracteres minúsculos alfanuméricos (. _ - permitidos)"}), 400
    if len(senha) < 6:
        return jsonify({"ok": False, "erro": "Senha deve ter no mínimo 6 caracteres"}), 400

    matricula = str(data.get('matricula', '') or '')[:20]
    cargo     = str(data.get('cargo',     '') or '')[:50]

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO funcionarios (nome, usuario, senha_hash, matricula, cargo) VALUES (?,?,?,?,?)",
            (nome, usuario, _hash_pbkdf2_full(senha), matricula, cargo)
        )
        conn.commit()
        return jsonify({"ok": True})
    except INTEGRITY_ERRORS:
        conn.rollback()
        return jsonify({"ok": False, "erro": "Usuário já existe"}), 409
    finally:
        conn.close()

@app.route('/admin/funcionarios/<int:fid>', methods=['PUT'])
@require_admin_key
def atualizar_funcionario(fid):
    data    = request.get_json(silent=True) or {}
    nome    = str(data.get('nome',    '') or '').strip()[:100]
    usuario = str(data.get('usuario', '') or '').strip().lower()[:50]

    if not nome or not usuario:
        return jsonify({"ok": False, "erro": "Nome e usuário são obrigatórios"}), 400

    matricula = str(data.get('matricula', '') or '')[:20]
    cargo     = str(data.get('cargo',     '') or '')[:50]
    ativo     = int(bool(data.get('ativo', 1)))

    conn = get_db()
    if data.get('senha'):
        senha = str(data['senha'])[:100]
        if len(senha) < 6:
            conn.close()
            return jsonify({"ok": False, "erro": "Senha deve ter no mínimo 6 caracteres"}), 400
        conn.execute(
            "UPDATE funcionarios SET nome=?, usuario=?, senha_hash=?, matricula=?, cargo=?, ativo=? WHERE id=?",
            (nome, usuario, _hash_pbkdf2_full(senha), matricula, cargo, ativo, fid)
        )
    else:
        conn.execute(
            "UPDATE funcionarios SET nome=?, usuario=?, matricula=?, cargo=?, ativo=? WHERE id=?",
            (nome, usuario, matricula, cargo, ativo, fid)
        )
    if not ativo:
        conn.execute("DELETE FROM tokens WHERE funcionario_id=?", (fid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route('/admin/registros', methods=['GET'])
@require_admin_key
def admin_registros():
    mes = request.args.get('mes', '')
    conn = get_db()
    query = """
        SELECT r.*, f.nome as funcionario_nome, f.matricula as funcionario_matricula
        FROM registros r
        JOIN funcionarios f ON f.id = r.funcionario_id
    """
    params = []
    if mes:
        if not _validar_mes(mes):
            conn.close()
            return jsonify({"ok": False, "erro": "Formato de mês inválido (use YYYY-MM)"}), 400
        query += " WHERE r.data LIKE ?"
        params.append(f"{mes}%")
    query += " ORDER BY f.nome, r.data"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/admin/abono', methods=['POST'])
@require_admin_key
def admin_abono():
    """Lança ou remove um abono (falta/atestado/folga/feriado) num dia.
    Somente o admin. O abono prevalece: zera as horas trabalhadas do dia.
    Enviar tipo='' remove o abono (o dia volta a ficar vazio)."""
    data = request.get_json(silent=True) or {}
    try:
        fid = int(data.get('funcionario_id'))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "erro": "funcionario_id inválido"}), 400

    data_iso = normalizar_data(data.get('data', ''))
    if not data_iso:
        return jsonify({"ok": False, "erro": "Data inválida (use YYYY-MM-DD)"}), 400

    tipo = str(data.get('tipo', '') or '').strip().lower()
    if tipo and tipo not in ABONO_TIPOS:
        return jsonify({"ok": False, "erro": "Tipo inválido"}), 400

    conn = get_db()
    func = conn.execute("SELECT id FROM funcionarios WHERE id=?", (fid,)).fetchone()
    if not func:
        conn.close()
        return jsonify({"ok": False, "erro": "Funcionário não encontrado"}), 404

    if tipo == '':
        # Remove o abono: apaga o registro do dia (volta a ficar vazio).
        conn.execute("DELETE FROM registros WHERE funcionario_id=? AND data=?", (fid, data_iso))
    else:
        # Marca o abono zerando horas/cidade — o abono prevalece sobre o trabalho.
        conn.execute(f"""
            INSERT INTO registros (funcionario_id, data, cidade, entrada,
                almoco_inicio, almoco_fim, saida, observacao, tipo)
            VALUES (?,?,'','','','','','',?)
            ON CONFLICT(funcionario_id, data) DO UPDATE SET
                tipo=excluded.tipo, cidade='', entrada='', almoco_inicio='',
                almoco_fim='', saida='', enviado_em={NOW_SQL}
        """, (fid, data_iso, tipo))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "tipo": tipo})

# ── PAINEL ADMIN (WEB) ────────────────────────────────
@app.route('/')
def raiz():
    return render_template('painel.html')

@app.route('/painel')
def painel():
    return render_template('painel.html')

@app.route('/ping', methods=['GET'])
def ping():
    return jsonify({"ok": True, "msg": "Ponttus servidor online", "db": "postgres" if USE_PG else "sqlite"})

# Garante schema também sob gunicorn (Render), não só em execução direta
init_db()

if __name__ == '__main__':
    port  = int(os.environ.get('PORT', 5000))
    host  = os.environ.get('HOST', '0.0.0.0')
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(host=host, port=port, debug=debug)

# teste auto-deploy 1781400366
