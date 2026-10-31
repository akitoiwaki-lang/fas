from dotenv import load_dotenv
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from io import BytesIO
import mimetypes
import os
import secrets
import sqlite3
import uuid

from flask import (
    Flask,
    abort,
    redirect,
    render_template_string,
    request,
    send_from_directory,
    session,
    url_for,
    send_file,
    jsonify
)

from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

# ------------------------------------------------------------
# My Closet
# - Save clothing items and photos
# - Share a closet with friends using a separate viewing password
# - Post an outfit for a date using clothes already in the closet
# - Let friends rate an outfit from 1 to 5 stars and leave a comment
# ------------------------------------------------------------

load_dotenv()

app = Flask(__name__)
app = Flask(__name__)
app.secret_key = os.environ.get("MY_CLOSET_SECRET") or secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB per request

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "closet.db"
PUBLIC_UPLOADS = BASE_DIR / "static" / "uploads"  # compatibility with older local photos
PRIVATE_UPLOADS = BASE_DIR / "private_uploads"
PUBLIC_UPLOADS.mkdir(parents=True, exist_ok=True)
PRIVATE_UPLOADS.mkdir(parents=True, exist_ok=True)

# If DATABASE_URL is set, use Supabase PostgreSQL and Supabase Storage.
# If it is not set, continue using local SQLite and local photo files.
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
SUPABASE_BUCKET = os.environ.get("SUPABASE_BUCKET", "closet-photos").strip()
USE_EXTERNAL_DB = bool(DATABASE_URL)
USE_EXTERNAL_STORAGE = bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY)

if USE_EXTERNAL_DB and not USE_EXTERNAL_STORAGE:
    raise RuntimeError(
        "DATABASE_URL を使う場合は、SUPABASE_URL と SUPABASE_SERVICE_ROLE_KEY も設定してください。"
    )
if USE_EXTERNAL_STORAGE and not USE_EXTERNAL_DB:
    raise RuntimeError(
        "SUPABASE_STORAGE を使う設定だけでは起動できません。DATABASE_URL も設定してください。"
    )

_supabase = None
if USE_EXTERNAL_STORAGE:
    from supabase import create_client
    _supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

STYLE = """
<style>
* { box-sizing: border-box; }
body { margin: 0; background: #f5f3ef; color: #292929; font-family: Arial, sans-serif; }
header { background: #292929; color: white; padding: 22px 16px; text-align: center; }
header h1 { margin: 0; letter-spacing: 3px; }
header p { margin-bottom: 0; }
main { max-width: 1050px; margin: 26px auto; padding: 0 16px; }
.panel, .card { background: white; padding: 18px; border-radius: 12px; margin-bottom: 18px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); gap: 16px; }
.card img { width: 100%; height: 230px; object-fit: contain; background: #fafafa; }
.outfit-items { display: grid; grid-template-columns: repeat(auto-fill, minmax(125px, 1fr)); gap: 10px; }
.outfit-items img { width: 100%; height: 155px; object-fit: contain; background: #fafafa; }
input, textarea, button, select { padding: 10px; margin: 6px 0; max-width: 100%; font: inherit; }
input[type=text], input[type=password], input[type=date], textarea, select { width: 100%; }
input[type=file] { width: 100%; }
textarea { min-height: 90px; resize: vertical; }
button, .button { display: inline-block; background: #292929; color: white; border: 0; border-radius: 7px; padding: 10px 14px; text-decoration: none; cursor: pointer; }
.delete { background: #a33; }
.error { color: #b00020; }
.muted { color: #666; font-size: .92em; }
.nav a { display: inline-block; margin: 3px 10px 3px 0; }
.item-picker { display: grid; grid-template-columns: repeat(auto-fill, minmax(145px, 1fr)); gap: 12px; }
.item-option { border: 1px solid #ddd; border-radius: 9px; padding: 10px; }
.item-option img { width: 100%; height: 150px; object-fit: contain; }
.item-option input { width: auto; }
.stars { color: #c78b00; font-size: 1.1em; }
hr { border: 0; border-top: 1px solid #e5e5e5; margin: 18px 0; }
@media (max-width: 560px) { main { margin-top: 14px; } .panel, .card { padding: 14px; } }
</style>
"""

def page(page_title, body, **context):
    """Render a complete page using a shared layout."""
    template = """
    <!doctype html>
    <html lang="ja">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>{{ page_title }}</title>
    """ + STYLE + """
    </head>
    <body>
      <header><h1>MY CLOSET</h1><p>{{ page_title }}</p></header>
      <main>{{ body|safe }}</main>
    </body>
    </html>
    """
    return render_template_string(template, page_title=page_title, body=render_template_string(body, **context))

class PostgresCursor:
    """Small compatibility wrapper for the SQLite cursor features this app uses."""
    def __init__(self, cursor, lastrowid=None):
        self._cursor = cursor
        self.lastrowid = lastrowid

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

class PostgresConnection:
    """Translate the app's SQLite-style placeholders to PostgreSQL placeholders."""
    def __init__(self, connection):
        self._connection = connection

    def execute(self, sql, params=()):
        import re
        query = sql.strip().rstrip(";")
        normalized = re.sub(r"\s+", " ", query).upper()
        needs_id = bool(re.match(r"INSERT\s+INTO\s+(USERS|OUTFITS)\b", normalized)) and " RETURNING " not in normalized
        if needs_id:
            query += " RETURNING id"
        query = query.replace("?", "%s")
        cursor = self._connection.cursor()
        cursor.execute(query, params)
        lastrowid = None
        if needs_id:
            row = cursor.fetchone()
            if row:
                lastrowid = row["id"]
        return PostgresCursor(cursor, lastrowid=lastrowid)

@contextmanager
def connect_db():
    """Use SQLite locally, or external PostgreSQL when DATABASE_URL is configured."""
    if not USE_EXTERNAL_DB:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return

    import psycopg
    from psycopg.rows import dict_row

    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if "sslmode=" not in url:
        url += "&sslmode=require" if "?" in url else "?sslmode=require"
    conn = psycopg.connect(url, row_factory=dict_row)
    try:
        yield PostgresConnection(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    """Create tables in the selected database. Local SQLite supports old schemas too."""
    if USE_EXTERNAL_DB:
        with connect_db() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS users (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                user_id TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                view_password_hash TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS clothing_items (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                image TEXT NOT NULL,
                owner_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS outfits (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                owner_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                outfit_date TEXT NOT NULL,
                caption TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(owner_id, outfit_date)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS outfit_items (
                outfit_id BIGINT NOT NULL REFERENCES outfits(id) ON DELETE CASCADE,
                item_id TEXT NOT NULL REFERENCES clothing_items(id) ON DELETE CASCADE,
                position INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(outfit_id, item_id)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS outfit_ratings (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                outfit_id BIGINT NOT NULL REFERENCES outfits(id) ON DELETE CASCADE,
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                stars INTEGER NOT NULL CHECK(stars BETWEEN 1 AND 5),
                comment TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(outfit_id, user_id)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS rooms (
            room_code TEXT PRIMARY KEY,
            room_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            owner_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
             )""")

            conn.execute("""CREATE TABLE IF NOT EXISTS room_members (
            room_code TEXT NOT NULL REFERENCES rooms(room_code) ON DELETE CASCADE,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(room_code, user_id)
            )""")

            conn.execute("""CREATE TABLE IF NOT EXISTS room_messages (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            room_code TEXT NOT NULL REFERENCES rooms(room_code) ON DELETE CASCADE,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            message TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""") 
            return

    with connect_db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS clothing_items (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            image TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        item_columns = {row["name"] for row in conn.execute("PRAGMA table_info(clothing_items)").fetchall()}
        if "owner_id" not in item_columns:
            conn.execute("ALTER TABLE clothing_items ADD COLUMN owner_id INTEGER")
        user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "view_password_hash" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN view_password_hash TEXT")
        first_user = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        if first_user:
            conn.execute("UPDATE clothing_items SET owner_id=? WHERE owner_id IS NULL", (first_user["id"],))
        conn.execute("""CREATE TABLE IF NOT EXISTS outfits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            outfit_date TEXT NOT NULL,
            caption TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(owner_id, outfit_date),
            FOREIGN KEY(owner_id) REFERENCES users(id) ON DELETE CASCADE
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS outfit_items (
            outfit_id INTEGER NOT NULL,
            item_id TEXT NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(outfit_id, item_id),
            FOREIGN KEY(outfit_id) REFERENCES outfits(id) ON DELETE CASCADE
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS outfit_ratings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            outfit_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            stars INTEGER NOT NULL CHECK(stars BETWEEN 1 AND 5),
            comment TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(outfit_id, user_id),
            FOREIGN KEY(outfit_id) REFERENCES outfits(id) ON DELETE CASCADE,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )""")
        images = conn.execute("SELECT DISTINCT image FROM clothing_items").fetchall()
        for row in images:
            filename = Path(row["image"]).name
            old_path = PUBLIC_UPLOADS / filename
            new_path = PRIVATE_UPLOADS / filename
            if old_path.is_file() and not new_path.exists():
                old_path.replace(new_path)

def is_integrity_error(error):
    if not USE_EXTERNAL_DB:
        return isinstance(error, sqlite3.IntegrityError)
    try:
        import psycopg
        return isinstance(error, psycopg.IntegrityError)
    except ImportError:
        return False

def current_user():
    user_pk = session.get("user_pk")
    if user_pk is None:
        return None
    with connect_db() as conn:
        return conn.execute("SELECT * FROM users WHERE id=?", (user_pk,)).fetchone()

def allowed_file(filename):
    return Path(secure_filename(filename)).suffix.lower() in ALLOWED_EXTENSIONS

def save_photo(file_storage):
    extension = Path(secure_filename(file_storage.filename)).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise ValueError("JPG、PNG、WEBP、GIF形式の写真を選んでください。")
    filename = uuid.uuid4().hex + extension
    if USE_EXTERNAL_STORAGE:
        content_type = file_storage.mimetype or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        _supabase.storage.from_(SUPABASE_BUCKET).upload(
            path=filename,
            file=file_storage.read(),
            file_options={"content-type": content_type, "upsert": "false"},
        )
    else:
        file_storage.save(PRIVATE_UPLOADS / filename)
    return filename

def delete_photo(filename):
    """Delete a saved photo from the active storage provider."""
    safe_name = Path(filename).name
    if USE_EXTERNAL_STORAGE:
        _supabase.storage.from_(SUPABASE_BUCKET).remove([safe_name])
    else:
        (PRIVATE_UPLOADS / safe_name).unlink(missing_ok=True)
        (PUBLIC_UPLOADS / safe_name).unlink(missing_ok=True)

def owned_item(item_id, owner_id):
    with connect_db() as conn:
        return conn.execute(
            "SELECT * FROM clothing_items WHERE id=? AND owner_id=?",
            (item_id, owner_id),
        ).fetchone()

def has_view_access(owner_id):
    if owner_id is None:
        return False
    allowed = {str(value) for value in session.get("view_access", [])}
    return str(owner_id) in allowed
def is_room_member(room_code, user_id):
    with connect_db() as conn:
        member = conn.execute(
            """
            SELECT 1
            FROM room_members
            WHERE room_code=? AND user_id=?
            """,
            (room_code, user_id),
        ).fetchone()

    return member is not None

def can_view_outfit(outfit, user):
    return (
        outfit is not None
        and user is not None
        and can_view_owner(outfit["owner_id"], user["id"])
    )

def get_outfit_items(outfit_id):
    with connect_db() as conn:
        return conn.execute(
            """SELECT ci.id, ci.title
               FROM outfit_items oi
               JOIN clothing_items ci ON ci.id=oi.item_id
               WHERE oi.outfit_id=?
               ORDER BY oi.position, ci.created_at""",
            (outfit_id,),
        ).fetchall()

def build_outfit_cards(outfits, viewer_id):
    cards = []
    for outfit in outfits:
        items = get_outfit_items(outfit["id"])
        with connect_db() as conn:
            rating_info = conn.execute(
                "SELECT AVG(stars) AS average, COUNT(*) AS count FROM outfit_ratings WHERE outfit_id=?",
                (outfit["id"],),
            ).fetchone()
            my_rating = conn.execute(
                "SELECT stars, comment FROM outfit_ratings WHERE outfit_id=? AND user_id=?",
                (outfit["id"], viewer_id),
            ).fetchone()
        cards.append({
            "outfit": outfit,
            "items": items,
            "average": rating_info["average"],
            "rating_count": rating_info["count"],
            "my_rating": my_rating,
        })
    return cards

AUTH_BODY = """
<section class="panel">
  <h2>{{ title }}</h2>
  {% if error %}<p class="error">{{ error }}</p>{% endif %}
  <form method="post">
    <label>ユーザーID（3〜30文字、英数字・_・-）</label>
    <input name="user_id" type="text" minlength="3" maxlength="30" pattern="[A-Za-z0-9_-]+" required>
    <label>ログインパスワード（8文字以上）</label>
    <input name="password" type="password" minlength="8" required>
    <button type="submit">{{ title }}</button>
  </form>
  <p>{{ other_text }} <a href="{{ other_url }}">{{ other_link }}</a></p>
</section>
"""

@app.route("/")
def index():
    user = current_user()
    if user is None:
        return redirect(url_for("login"))
    with connect_db() as conn:
        items = conn.execute(
            "SELECT * FROM clothing_items WHERE owner_id=? ORDER BY created_at DESC",
            (user["id"],),
        ).fetchall()
        outfits = conn.execute(
            "SELECT * FROM outfits WHERE owner_id=? ORDER BY outfit_date DESC LIMIT 6",
            (user["id"],),
        ).fetchall()
    cards = build_outfit_cards(outfits, user["id"])
    body = """
    <section class="panel">
      <p>ログイン中：<b>{{ user_id }}</b></p>
      <div class="nav">
        <a href="{{ url_for('settings') }}">閲覧パスワード設定</a>
        <a href="{{ url_for('search_user') }}">友達を探す</a>
        <a href="{{ url_for('outfit_feed') }}">コーデ一覧・評価</a>
        <a href="/rooms">ルーム一覧・作成</a>
        <a href="{{ url_for('logout') }}">ログアウト</a>
      </div>
    </section>
    <section class="panel">
      <h2>今日のコーデを投稿</h2>
      <p>登録した服を選んで、その日のコーデを投稿できます。日付ごとに1つ保存でき、同じ日付なら編集できます。</p>
      <a class="button" href="{{ url_for('outfit_new') }}">コーデを作成・編集</a>
    </section>
    <section class="panel">
      <h2>服を追加する</h2>
      <form action="{{ url_for('add') }}" method="post" enctype="multipart/form-data">
        <label>服の名前</label><input type="text" name="title" maxlength="100" required>
        <label>写真（10MB以下）</label><input type="file" name="photo" accept=".jpg,.jpeg,.png,.webp,.gif" required>
        <button type="submit">服を登録</button>
      </form>
    </section>
    <h2>登録した服（{{ items|length }}着）</h2>
    <section class="grid">
      {% for item in items %}
      <article class="card">
        <img src="{{ url_for('photo', item_id=item['id']) }}" alt="{{ item['title'] }}">
        <h3>{{ item['title'] }}</h3>
        <a href="{{ url_for('edit', item_id=item['id']) }}">編集</a>
        <form action="{{ url_for('delete', item_id=item['id']) }}" method="post" onsubmit="return confirm('この服を削除しますか？')">
          <button class="delete">削除</button>
        </form>
      </article>
      {% else %}<p>まだ服がありません。写真を追加してみよう。</p>{% endfor %}
    </section>
    <h2>自分の最近のコーデ</h2>
    <section class="grid">
      {% for card in cards %}
      <article class="card">
        <h3>{{ card.outfit['outfit_date'] }}</h3>
        {% if card.outfit['caption'] %}<p>{{ card.outfit['caption'] }}</p>{% endif %}
        <p>{{ card['items']|length }}点の服</p>
        {% if card.average is not none %}<p>平均評価：{{ '%.1f'|format(card.average) }} / 5（{{ card.rating_count }}件）</p>{% else %}<p>まだ評価はありません。</p>{% endif %}
        <a href="{{ url_for('outfit_detail', outfit_id=card.outfit['id']) }}">コーデを見る・編集する</a>
      </article>
      {% else %}<p>まだコーデを投稿していません。</p>{% endfor %}
    </section>
    """
    return page("マイページ", body, user_id=user["user_id"], items=items, cards=cards)

@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user():
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        user_id = request.form.get("user_id", "").strip()
        password = request.form.get("password", "")
        valid_id = 3 <= len(user_id) <= 30 and all(
            char.isascii() and (char.isalnum() or char in "_-") for char in user_id
        )
        if not valid_id:
            error = "ユーザーIDは3〜30文字の英数字、_、- にしてください。"
        elif len(password) < 8:
            error = "パスワードは8文字以上にしてください。"
        else:
            try:
                with connect_db() as conn:
                    cursor = conn.execute(
                        "INSERT INTO users(user_id,password_hash) VALUES(?,?)",
                        (user_id, generate_password_hash(password)),
                    )
                    new_id = cursor.lastrowid
                    # Keep support for legacy items with no owner, if any remain.
                    conn.execute("UPDATE clothing_items SET owner_id=? WHERE owner_id IS NULL", (new_id,))
                session.clear()
                session["user_pk"] = new_id
                return redirect(url_for("index"))
            except Exception as exc:
                if is_integrity_error(exc):
                    error = "そのユーザーIDはすでに使われています。"
                else:
                    raise
    return page(
        "新規登録",
        AUTH_BODY,
        title="新規登録",
        error=error,
        other_text="登録済みなら",
        other_url=url_for("login"),
        other_link="ログイン",
    )

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        user_id = request.form.get("user_id", "").strip()
        password = request.form.get("password", "")
        with connect_db() as conn:
            user = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_pk"] = user["id"]
            return redirect(url_for("index"))
        error = "ユーザーIDまたはパスワードが違います。"
    return page(
        "ログイン",
        AUTH_BODY,
        title="ログイン",
        error=error,
        other_text="初めて使うなら",
        other_url=url_for("register"),
        other_link="新規登録",
    )

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/settings", methods=["GET", "POST"])
def settings():
    user = current_user()
    if user is None:
        return redirect(url_for("login"))
    message = ""
    if request.method == "POST":
        password = request.form.get("view_password", "")
        if len(password) < 8:
            message = "閲覧パスワードは8文字以上にしてください。"
        else:
            with connect_db() as conn:
                conn.execute(
                    "UPDATE users SET view_password_hash=? WHERE id=?",
                    (generate_password_hash(password), user["id"]),
                )
            message = "閲覧パスワードを設定しました。友達に教えるのはこのパスワードです。"
    body = """
    <section class="panel">
      <h2>閲覧パスワードの設定</h2>
      <p>友達に教えるためのパスワードです。ログイン用とは別のものにしてください。</p>
      {% if message %}<p>{{ message }}</p>{% endif %}
      <form method="post"><label>閲覧パスワード（8文字以上）</label>
      <input type="password" name="view_password" minlength="8" required>
      <button>設定・変更する</button></form>
      <p><a href="{{ url_for('index') }}">マイページへ戻る</a></p>
    </section>
    """
    return page("閲覧設定", body, message=message)

@app.route("/search", methods=["GET", "POST"])
def search_user():
    if current_user() is None:
        return redirect(url_for("login"))
    error = None
    if request.method == "POST":
        target_id = request.form.get("user_id", "").strip()
        with connect_db() as conn:
            target = conn.execute(
                "SELECT id,user_id,view_password_hash FROM users WHERE user_id=?",
                (target_id,),
            ).fetchone()
        if target is None:
            error = "そのユーザーIDは見つかりませんでした。"
        elif target["id"] == session["user_pk"]:
            return redirect(url_for("index"))
        elif not target["view_password_hash"]:
            error = "このユーザーはまだ閲覧パスワードを設定していません。"
        else:
            return redirect(url_for("view_password", target_id=target["id"]))
    body = """
    <section class="panel"><h2>友達を探す</h2>
      {% if error %}<p class="error">{{ error }}</p>{% endif %}
      <form method="post"><label>友達のユーザーID</label>
      <input type="text" name="user_id" placeholder="友達のユーザーID" required>
      <button>検索</button></form>
      <p><a href="{{ url_for('index') }}">戻る</a></p>
    </section>
    """
    return page("友達を探す", body, error=error)

@app.route("/view-password/<int:target_id>", methods=["GET", "POST"])
def view_password(target_id):
    if current_user() is None:
        return redirect(url_for("login"))
    with connect_db() as conn:
        target = conn.execute(
            "SELECT id,user_id,view_password_hash FROM users WHERE id=?", (target_id,)
        ).fetchone()
    if target is None or target_id == session["user_pk"]:
        abort(404)
    if not target["view_password_hash"]:
        return redirect(url_for("search_user"))
    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        if check_password_hash(target["view_password_hash"], password):
            allowed = {str(value) for value in session.get("view_access", [])}
            allowed.add(str(target_id))
            session["view_access"] = list(allowed)
            return redirect(url_for("friend_closet", target_id=target_id))
        error = "閲覧パスワードが違います。"
    body = """
    <section class="panel"><h2>{{ target_id }} さんのクローゼット</h2>
      {% if error %}<p class="error">{{ error }}</p>{% endif %}
      <form method="post"><label>閲覧パスワード</label>
      <input type="password" name="password" required><button>クローゼットを見る</button></form>
      <p><a href="{{ url_for('search_user') }}">戻る</a></p>
    </section>
    """
    return page("閲覧パスワード", body, target_id=target["user_id"], error=error)

@app.route("/friend/<int:target_id>")
def friend_closet(target_id):
    if current_user() is None:
        return redirect(url_for("login"))
    if not has_view_access(target_id):
        return redirect(url_for("view_password", target_id=target_id))
    with connect_db() as conn:
        target = conn.execute("SELECT id,user_id FROM users WHERE id=?", (target_id,)).fetchone()
        if target is None:
            abort(404)
        items = conn.execute(
            "SELECT id,title FROM clothing_items WHERE owner_id=? ORDER BY created_at DESC",
            (target_id,),
        ).fetchall()
        outfits = conn.execute(
            "SELECT * FROM outfits WHERE owner_id=? ORDER BY outfit_date DESC",
            (target_id,),
        ).fetchall()
    cards = build_outfit_cards(outfits, session["user_pk"])
    body = """
    <section class="panel">
      <h2>{{ target_id }} さんのクローゼット</h2>
      <div class="nav"><a href="{{ url_for('outfit_feed') }}">コーデ一覧・評価</a>
      <a href="{{ url_for('search_user') }}">別の友達を探す</a>
      <a href="{{ url_for('index') }}">自分のクローゼットへ</a></div>
    </section>
    <h2>登録した服（{{ items|length }}着）</h2>
    <section class="grid">
      {% for item in items %}<article class="card">
        <img src="{{ url_for('photo', item_id=item['id']) }}" alt="{{ item['title'] }}"><h3>{{ item['title'] }}</h3>
      </article>{% else %}<p>このクローゼットにはまだ服がありません。</p>{% endfor %}
    </section>
    <h2>{{ target_id }} さんのコーデ</h2>
    <section class="grid">
      {% for card in cards %}<article class="card">
        <h3>{{ card.outfit['outfit_date'] }}</h3>
        {% if card.outfit['caption'] %}<p>{{ card.outfit['caption'] }}</p>{% endif %}
        <p>{{ card['items']|length }}点の服</p>
        {% if card.average is not none %}<p>平均評価：{{ '%.1f'|format(card.average) }} / 5（{{ card.rating_count }}件）</p>{% else %}<p>まだ評価はありません。</p>{% endif %}
        <a href="{{ url_for('outfit_detail', outfit_id=card.outfit['id']) }}">コーデを見る・評価する</a>
      </article>{% else %}<p>まだコーデを投稿していません。</p>{% endfor %}
    </section>
    """
    return page("友達のクローゼット", body, target_id=target["user_id"], items=items, cards=cards)

@app.route("/photo/<item_id>")
def photo(item_id):
    user = current_user()
    if user is None:
        abort(403)
    with connect_db() as conn:
        item = conn.execute(
            "SELECT image,owner_id FROM clothing_items WHERE id=?", (item_id,)
        ).fetchone()
    if item is None:
        abort(404)
    is_owner = str(item["owner_id"]) == str(user["id"])
    filename = Path(item["image"]).name
    if USE_EXTERNAL_STORAGE:
        try:
            image_data = _supabase.storage.from_(SUPABASE_BUCKET).download(filename)
        except Exception:
            abort(404)
        mimetype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        response = send_file(BytesIO(image_data), mimetype=mimetype, download_name=filename, max_age=0)
    else:
        private_path = PRIVATE_UPLOADS / filename
        old_path = PUBLIC_UPLOADS / filename
        if not private_path.is_file() and old_path.is_file():
            old_path.replace(private_path)
        if not private_path.is_file():
            abort(404)
        response = send_from_directory(PRIVATE_UPLOADS, filename, max_age=0)
    response.headers["Cache-Control"] = "no-store"
    return response

@app.route("/add", methods=["POST"])
def add():
    user = current_user()
    if user is None:
        return redirect(url_for("login"))
    title = request.form.get("title", "").strip()
    photo_file = request.files.get("photo")
    if not title or len(title) > 100 or not photo_file or not photo_file.filename:
        return "服の名前と写真を指定してください。", 400
    if not allowed_file(photo_file.filename):
        return "JPG、PNG、WEBP、GIF形式の写真を選んでください。", 400
    image_name = save_photo(photo_file)
    item_id = uuid.uuid4().hex
    try:
        with connect_db() as conn:
            conn.execute(
                "INSERT INTO clothing_items(id,title,image,owner_id) VALUES(?,?,?,?)",
                (item_id, title, image_name, user["id"]),
            )
    except Exception:
        try:
            delete_photo(image_name)
        except Exception:
            app.logger.exception("Could not clean up an uploaded photo after a database error")
        raise
    return redirect(url_for("index"))

@app.route("/edit/<item_id>", methods=["GET", "POST"])
def edit(item_id):
    user = current_user()
    if user is None:
        return redirect(url_for("login"))
    item = owned_item(item_id, user["id"])
    if item is None:
        abort(404)
    error = None
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        photo_file = request.files.get("photo")
        if not title or len(title) > 100:
            error = "服の名前を1〜100文字で入力してください。"
        elif photo_file and photo_file.filename and not allowed_file(photo_file.filename):
            error = "JPG、PNG、WEBP、GIF形式の写真を選んでください。"
        else:
            new_image = item["image"]
            if photo_file and photo_file.filename:
                new_image = save_photo(photo_file)
            try:
                with connect_db() as conn:
                    conn.execute(
                        "UPDATE clothing_items SET title=?,image=? WHERE id=? AND owner_id=?",
                        (title, new_image, item_id, user["id"]),
                    )
            except Exception:
                if new_image != item["image"]:
                    try:
                        delete_photo(new_image)
                    except Exception:
                        app.logger.exception("Could not clean up the new photo after an edit error")
                raise
            if new_image != item["image"]:
                try:
                    delete_photo(item["image"])
                except Exception:
                    app.logger.exception("Could not delete the old photo after an edit")
            return redirect(url_for("index"))
    body = """
    <section class="panel">
      {% if error %}<p class="error">{{ error }}</p>{% endif %}
      <img style="width:100%;max-height:330px;object-fit:contain" src="{{ url_for('photo', item_id=item['id']) }}" alt="{{ item['title'] }}">
      <form method="post" enctype="multipart/form-data">
        <label>服の名前</label><input type="text" name="title" value="{{ item['title'] }}" maxlength="100" required>
        <label>写真（変更する場合のみ選択）</label><input type="file" name="photo" accept=".jpg,.jpeg,.png,.webp,.gif">
        <button type="submit">変更を保存</button>
      </form><a href="{{ url_for('index') }}">戻る</a>
    </section>
    """
    return page("服を編集", body, item=item, error=error)

@app.route("/delete/<item_id>", methods=["POST"])
def delete(item_id):
    user = current_user()
    if user is None:
        return redirect(url_for("login"))
    with connect_db() as conn:
        item = conn.execute(
            "SELECT image FROM clothing_items WHERE id=? AND owner_id=?",
            (item_id, user["id"]),
        ).fetchone()
        if item is None:
            abort(404)
        # Remove the selected item from any saved outfits before deleting it.
        conn.execute("DELETE FROM outfit_items WHERE item_id=?", (item_id,))
        conn.execute("DELETE FROM clothing_items WHERE id=? AND owner_id=?", (item_id, user["id"]))
    try:
        delete_photo(item["image"])
    except Exception:
        app.logger.exception("Could not delete photo from storage")
    return redirect(url_for("index"))

@app.route("/outfit/new", methods=["GET", "POST"])
def outfit_new():
    user = current_user()
    if user is None:
        return redirect(url_for("login"))
    today = date.today().isoformat()
    outfit_date = request.values.get("outfit_date", today)
    try:
        date.fromisoformat(outfit_date)
    except (TypeError, ValueError):
        outfit_date = today
    with connect_db() as conn:
        items = conn.execute(
            "SELECT id,title FROM clothing_items WHERE owner_id=? ORDER BY created_at DESC",
            (user["id"],),
        ).fetchall()
        existing = conn.execute(
            "SELECT * FROM outfits WHERE owner_id=? AND outfit_date=?",
            (user["id"], outfit_date),
        ).fetchone()
        selected_ids = set()
        if existing:
            selected_ids = {
                row["item_id"]
                for row in conn.execute("SELECT item_id FROM outfit_items WHERE outfit_id=?", (existing["id"],)).fetchall()
            }
    error = None
    if request.method == "POST":
        outfit_date = request.form.get("outfit_date", today)
        try:
            date.fromisoformat(outfit_date)
        except (TypeError, ValueError):
            error = "正しい日付を選んでください。"
        if error is None:
            caption = request.form.get("caption", "").strip()[:300]
            submitted_ids = list(dict.fromkeys(request.form.getlist("item_ids")))
            # Only keep clothing IDs that belong to the logged-in user.
            with connect_db() as conn:
                if submitted_ids:
                    placeholders = ",".join("?" for _ in submitted_ids)
                    valid_rows = conn.execute(
                        f"SELECT id FROM clothing_items WHERE owner_id=? AND id IN ({placeholders})",
                        [user["id"], *submitted_ids],
                    ).fetchall()
                    valid_ids = [row["id"] for row in valid_rows]
                else:
                    valid_ids = []
                if not valid_ids:
                    error = "コーデに使う服を1着以上選んでください。"
                else:
                    existing = conn.execute(
                        "SELECT id FROM outfits WHERE owner_id=? AND outfit_date=?",
                        (user["id"], outfit_date),
                    ).fetchone()
                    if existing:
                        outfit_id = existing["id"]
                        conn.execute(
                            "UPDATE outfits SET caption=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                            (caption, outfit_id),
                        )
                        conn.execute("DELETE FROM outfit_items WHERE outfit_id=?", (outfit_id,))
                    else:
                        cursor = conn.execute(
                            "INSERT INTO outfits(owner_id,outfit_date,caption) VALUES(?,?,?)",
                            (user["id"], outfit_date, caption),
                        )
                        outfit_id = cursor.lastrowid
                    for position, selected_id in enumerate(valid_ids):
                        conn.execute(
                            "INSERT INTO outfit_items(outfit_id,item_id,position) VALUES(?,?,?)",
                            (outfit_id, selected_id, position),
                        )
            if error is None:
                return redirect(url_for("outfit_detail", outfit_id=outfit_id))
        # Keep user selections if validation failed.
        selected_ids = set(request.form.getlist("item_ids"))
        with connect_db() as conn:
            items = conn.execute(
                "SELECT id,title FROM clothing_items WHERE owner_id=? ORDER BY created_at DESC",
                (user["id"],),
            ).fetchall()
        existing = None
    body = """
    <section class="panel">
      <p>登録済みの服から、その日に着たコーデを選んでください。</p>
      {% if error %}<p class="error">{{ error }}</p>{% endif %}
      <form method="post">
        <label>日付</label><input type="date" name="outfit_date" value="{{ outfit_date }}" required>
        <label>ひとこと（任意・300文字まで）</label><textarea name="caption" maxlength="300" placeholder="今日のコーデのポイントなど">{{ existing['caption'] if existing else (posted_caption or '') }}</textarea>
        <h3>着た服を選ぶ</h3>
        <section class="item-picker">
        {% for item in items %}
          <label class="item-option">
            <input type="checkbox" name="item_ids" value="{{ item['id'] }}" {% if item['id'] in selected_ids %}checked{% endif %}>
            <img src="{{ url_for('photo', item_id=item['id']) }}" alt="{{ item['title'] }}">
            <div>{{ item['title'] }}</div>
          </label>
        {% else %}<p>先にマイページで服を登録してください。</p>{% endfor %}
        </section>
        <button type="submit">コーデを保存する</button>
      </form>
      <p class="muted">同じ日付で再度保存すると、その日のコーデを更新します。</p>
      <a href="{{ url_for('index') }}">マイページへ戻る</a>
    </section>
    """
    posted_caption = request.form.get("caption", "") if request.method == "POST" else ""
    return page("コーデを作成・編集", body, items=items, selected_ids=selected_ids,
                outfit_date=outfit_date, existing=existing, error=error, posted_caption=posted_caption)

@app.route("/outfits")
def outfit_feed():
    user = current_user()
    if user is None:
        return redirect(url_for("login"))
    visible_ids = {str(user["id"])}
    visible_ids.update(str(value) for value in session.get("view_access", []))

    with connect_db() as conn:
        room_members = conn.execute(
        """SELECT DISTINCT rm2.user_id
           FROM room_members rm1
           JOIN room_members rm2
             ON rm1.room_code = rm2.room_code
           WHERE rm1.user_id=?""",
        (user["id"],),
    ).fetchall()

    visible_ids.update(str(row["user_id"]) for row in room_members)
    placeholders = ",".join("?" for _ in visible_ids)
    with connect_db() as conn:
        outfits = conn.execute(
            f"""SELECT outfits.*, users.user_id AS owner_user_id
                FROM outfits JOIN users ON users.id=outfits.owner_id
                WHERE outfits.owner_id IN ({placeholders})
                ORDER BY outfits.outfit_date DESC, outfits.updated_at DESC""",
            list(visible_ids),
        ).fetchall()
        cards = build_outfit_cards(outfits, user["id"])
    body = """
    <section class="panel">
      <div class="nav"><a href="{{ url_for('outfit_new') }}">コーデを投稿する</a>
      <a href="{{ url_for('search_user') }}">友達を探す</a>
      <a href="{{ url_for('index') }}">マイページ</a></div>
      <p class="muted">自分のコーデと、閲覧パスワードを入力した友達のコーデが表示されます。</p>
    </section>
    <section class="grid">
      {% for card in cards %}
      <article class="card">
        <h3>{{ card.outfit['owner_user_id'] }} さん · {{ card.outfit['outfit_date'] }}</h3>
        {% if card.outfit['caption'] %}<p>{{ card.outfit['caption'] }}</p>{% endif %}
        <div class="outfit-items">
          {% for item in card['items'] %}<div><img src="{{ url_for('photo', item_id=item['id']) }}" alt="{{ item['title'] }}"><p>{{ item['title'] }}</p></div>{% endfor %}
        </div>
        {% if card.average is not none %}<p><span class="stars">★</span> 平均 {{ '%.1f'|format(card.average) }} / 5（{{ card.rating_count }}件）</p>{% else %}<p>まだ評価はありません。</p>{% endif %}
        {% if card.my_rating %}<p class="muted">あなたの評価：{{ card.my_rating['stars'] }} / 5</p>{% endif %}
        <a class="button" href="{{ url_for('outfit_detail', outfit_id=card.outfit['id']) }}">詳細・評価・コメント</a>
      </article>
      {% else %}<p>まだコーデがありません。自分のコーデを投稿するか、友達の閲覧パスワードを入力してください。</p>{% endfor %}
    </section>
    """
    return page("コーデ一覧・評価", body, cards=cards)

@app.route("/outfit/<int:outfit_id>", methods=["GET", "POST"])
def outfit_detail(outfit_id):
    user = current_user()
    if user is None:
        return redirect(url_for("login"))
    with connect_db() as conn:
        outfit = conn.execute(
            """SELECT outfits.*, users.user_id AS owner_user_id
               FROM outfits JOIN users ON users.id=outfits.owner_id
               WHERE outfits.id=?""",
            (outfit_id,),
        ).fetchone()
    if outfit is None:
        abort(404)
    if not can_view_outfit(outfit, user):
        abort(403)

    error = None
    if request.method == "POST":
        if outfit["owner_id"] == user["id"]:
            error = "自分のコーデには評価できません。友達に評価してもらいましょう。"
        else:
            try:
                stars = int(request.form.get("stars", "5"))
            except (TypeError, ValueError):
                stars = 0
            comment = request.form.get("comment", "").strip()
            if stars < 1 or stars > 5:
                error = "星1〜5の中から選んでください。"
            elif len(comment) > 500:
                error = "コメントは500文字以内にしてください。"
            else:
                with connect_db() as conn:
                    conn.execute(
                        """INSERT INTO outfit_ratings(outfit_id,user_id,stars,comment)
                           VALUES(?,?,?,?)
                           ON CONFLICT(outfit_id,user_id) DO UPDATE SET
                             stars=excluded.stars,
                             comment=excluded.comment,
                             updated_at=CURRENT_TIMESTAMP""",
                        (outfit_id, user["id"], stars, comment),
                    )
                return redirect(url_for("outfit_detail", outfit_id=outfit_id))

    items = get_outfit_items(outfit_id)
    with connect_db() as conn:
        reviews = conn.execute(
            """SELECT r.stars,r.comment,r.created_at,r.updated_at,u.user_id
               FROM outfit_ratings r JOIN users u ON u.id=r.user_id
               WHERE r.outfit_id=? ORDER BY r.updated_at DESC""",
            (outfit_id,),
        ).fetchall()
        my_rating = conn.execute(
            "SELECT stars,comment FROM outfit_ratings WHERE outfit_id=? AND user_id=?",
            (outfit_id, user["id"]),
        ).fetchone()
        rating_info = conn.execute(
            "SELECT AVG(stars) AS average,COUNT(*) AS count FROM outfit_ratings WHERE outfit_id=?",
            (outfit_id,),
        ).fetchone()
    is_owner = outfit["owner_id"] == user["id"]
    body = """
    <section class="panel">
      <h2>{{ outfit['owner_user_id'] }} さんのコーデ</h2>
      <p><b>日付：</b>{{ outfit['outfit_date'] }}</p>
      {% if outfit['caption'] %}<p>{{ outfit['caption'] }}</p>{% endif %}
      {% if rating_info.average is not none %}<p><span class="stars">★</span> 平均評価：{{ '%.1f'|format(rating_info.average) }} / 5（{{ rating_info.count }}件）</p>{% else %}<p>まだ評価はありません。</p>{% endif %}
      <div class="outfit-items">
        {% for item in items %}<div><img src="{{ url_for('photo', item_id=item['id']) }}" alt="{{ item['title'] }}"><p>{{ item['title'] }}</p></div>{% endfor %}
      </div>
      {% if is_owner %}<p><a class="button" href="{{ url_for('outfit_new', outfit_date=outfit['outfit_date']) }}">この日のコーデを編集</a></p>{% endif %}
      <p><a href="{{ url_for('outfit_feed') }}">コーデ一覧へ戻る</a>　<a href="{{ url_for('index') }}">マイページ</a></p>
    </section>
    {% if not is_owner %}
    <section class="panel">
      <h2>このコーデを評価する</h2>
      {% if error %}<p class="error">{{ error }}</p>{% endif %}
      <form method="post">
        <label>評価（1〜5）</label>
        <select name="stars" required>
          {% for n in range(5,0,-1) %}<option value="{{ n }}" {% if my_rating and my_rating['stars']==n %}selected{% elif not my_rating and n==5 %}selected{% endif %}>{{ n }} 星</option>{% endfor %}
        </select>
        <label>コメント（任意・500文字以内）</label>
        <textarea name="comment" maxlength="500" placeholder="コーデの感想を書こう">{{ my_rating['comment'] if my_rating else '' }}</textarea>
        <button type="submit">評価・コメントを保存</button>
      </form>
      <p class="muted">同じ人がもう一度送信すると、自分の評価・コメントが更新されます。</p>
    </section>
    {% elif error %}<section class="panel"><p class="error">{{ error }}</p></section>{% endif %}
    <section class="panel">
      <h2>みんなの評価・コメント</h2>
      {% for review in reviews %}
        <hr><p><b>{{ review['user_id'] }}</b>　<span class="stars">{{ '★' * review['stars'] }}{{ '☆' * (5-review['stars']) }}</span>　{{ review['stars'] }}/5</p>
        {% if review['comment'] %}<p>{{ review['comment'] }}</p>{% else %}<p class="muted">コメントなし</p>{% endif %}
        <p class="muted">更新：{{ review['updated_at'] }}</p>
      {% else %}<p>まだ評価・コメントはありません。</p>{% endfor %}
    </section>
    """
    return page("コーデの詳細", body, outfit=outfit, items=items, reviews=reviews,
                my_rating=my_rating, rating_info=rating_info, is_owner=is_owner, error=error)
@app.route("/rooms")
def rooms():
    user = current_user()
    if user is None:
        return redirect(url_for("login"))

    with connect_db() as conn:
        my_rooms = conn.execute(
            """SELECT r.*
               FROM rooms r
               JOIN room_members m ON m.room_code=r.room_code
               WHERE m.user_id=?
               ORDER BY r.created_at DESC""",
            (user["id"],),
        ).fetchall()

    body = """
    <section class="panel">
      <h2>ルームを作成する</h2>
      <form action="{{ url_for('room_create') }}" method="post">
        <label>ルーム名</label>
        <input type="text" name="room_name" maxlength="50" required>

        <label>ルームパスワード（8文字以上）</label>
        <input type="password" name="password" minlength="8" required>

        <button>ルームを作成</button>
      </form>
    </section>

    <section class="panel">
      <h2>友達のルームに参加する</h2>
      <form action="{{ url_for('room_join') }}" method="post">
        <label>招待コード</label>
        <input type="text" name="room_code" required>

        <label>ルームパスワード</label>
        <input type="password" name="password" required>

        <button>ルームに参加</button>
      </form>
    </section>

    <section class="panel">
      <h2>参加中のルーム</h2>
      {% for room in my_rooms %}
        <p>
          <b>{{ room['room_name'] }}</b>
          <a href="{{ url_for('room_detail', room_code=room['room_code']) }}">
            ルームを開く
          </a>
        </p>
      {% else %}
        <p>まだ参加しているルームはありません。</p>
      {% endfor %}
    </section>

    <a href="{{ url_for('index') }}">マイページへ戻る</a>
    """

    return page("ルーム", body, my_rooms=my_rooms)

@app.route("/room/create", methods=["POST"])
def room_create():
    user = current_user()
    if user is None:
        return redirect(url_for("login"))

    room_name = request.form.get("room_name", "").strip()
    password = request.form.get("password", "")

    if not room_name or len(room_name) > 50:
        return "ルーム名は1〜50文字にしてください。", 400

    if len(password) < 8:
        return "パスワードは8文字以上にしてください。", 400

    room_code = secrets.token_urlsafe(9)

    with connect_db() as conn:
        conn.execute(
            """INSERT INTO rooms
               (room_code, room_name, password_hash, owner_id)
               VALUES (?, ?, ?, ?)""",
            (
                room_code,
                room_name,
                generate_password_hash(password),
                user["id"],
            ),
        )

        conn.execute(
            """INSERT INTO room_members(room_code, user_id)
               VALUES (?, ?)""",
            (room_code, user["id"]),
        )

    return redirect(url_for("room_detail", room_code=room_code))

@app.route("/room/join", methods=["POST"])
def room_join():
    user = current_user()
    if user is None:
        return redirect(url_for("login"))

    room_code = request.form.get("room_code", "").strip()
    password = request.form.get("password", "")

    with connect_db() as conn:
        room = conn.execute(
            "SELECT * FROM rooms WHERE room_code=?",
            (room_code,),
        ).fetchone()

    if room is None:
        return "ルームが見つかりません。", 404

    if not check_password_hash(room["password_hash"], password):
        return "ルームパスワードが違います。", 403

    with connect_db() as conn:
        conn.execute(
            """INSERT INTO room_members(room_code, user_id)
               VALUES (?, ?)
               ON CONFLICT(room_code, user_id) DO NOTHING""",
            (room_code, user["id"]),
        )

    return redirect(url_for("room_detail", room_code=room_code))
@app.route("/manage_rooms")
def manage_rooms():
    user = current_user()
    if user is None:
        return redirect(url_for("login"))

    with connect_db() as conn:
        my_rooms = conn.execute(
            """SELECT r.*
               FROM rooms r
               JOIN room_members m ON m.room_code=r.room_code
               WHERE m.user_id=?
               ORDER BY r.created_at DESC""",
            (user["id"],),
        ).fetchall()

    body = """
        <section class="panel">
        <h2>ルームを作成する</h2>
        <form action="{{ url_for('room_create') }}" method="post">
            <p>
                <label>ルーム名</label><br>
                <input type="text" name="room_name" maxlength="50" required style="width: 100%; max-width: 300px; padding: 5px;">
            </p>
            <p>
                <label>ルームパスワード（8文字以上）</label><br>
                <input type="password" name="password" minlength="8" required style="width: 100%; max-width: 300px; padding: 5px;">
            </p>
            <button style="padding: 5px 15px; background-color: #333; color: #fff; border: none; cursor: pointer;">ルームを作成</button>
        </form>
    </section>

    <section class="panel">
      <h2>友達のルームに参加する</h2>
      <form action="{{ url_for('room_join') }}" method="post">
        <label>招待コード</label>
        <input type="text" name="room_code" required>

        <label>ルームパスワード</label>
        <input type="password" name="password" required>

        <button>ルームに参加</button>
      </form>
    </section>

    <section class="panel">
      <h2>参加中のルーム</h2>
      {% for room in my_rooms %}
        <p>
          <b>{{ room['room_name'] }}</b>
          <a href="{{ url_for('room_detail', room_code=room['room_code']) }}">
            ルームを開く
          </a>
        </p>
      {% else %}
        <p>まだ参加しているルームはありません。</p>
      {% endfor %}
    </section>

    <a href="{{ url_for('index') }}">マイページへ戻る</a>
    """

    return page("ルーム", body, my_rooms=my_rooms)

@app.route("/Aroom/create", methods=["POST"])
def Aroom_create():
    user = current_user()
    if user is None:
        return redirect(url_for("login"))

    room_name = request.form.get("room_name", "").strip()
    password = request.form.get("password", "")

    if not room_name or len(room_name) > 50:
        return "ルーム名は1〜50文字にしてください。", 400

    if len(password) < 8:
        return "パスワードは8文字以上にしてください。", 400

    room_code = secrets.token_urlsafe(9)

    with connect_db() as conn:
        conn.execute(
            """INSERT INTO rooms
               (room_code, room_name, password_hash, owner_id)
               VALUES (?, ?, ?, ?)""",
            (
                room_code,
                room_name,
                generate_password_hash(password),
                user["id"],
            ),
        )

        conn.execute(
            """INSERT INTO room_members(room_code, user_id)
               VALUES (?, ?)""",
            (room_code, user["id"]),
        )

    return redirect(url_for("room_detail", room_code=room_code))

@app.route("/room/join", methods=["POST"])
def room_join_post():
    user = current_user()
    if user is None:
        return redirect(url_for("login"))

    room_code = request.form.get("room_code", "").strip()
    password = request.form.get("password", "")

    with connect_db() as conn:
        room = conn.execute(
            "SELECT * FROM rooms WHERE room_code=?",
            (room_code,),
        ).fetchone()

    if room is None:
        return "ルームが見つかりません。", 404

    if not check_password_hash(room["password_hash"], password):
        return "ルームパスワードが違います。", 403

    with connect_db() as conn:
        conn.execute(
            """INSERT INTO room_members(room_code, user_id)
               VALUES (?, ?)
               ON CONFLICT(room_code, user_id) DO NOTHING""",
            (room_code, user["id"]),
        )

    return redirect(url_for("room_detail", room_code=room_code))
@app.route("/room/<room_code>")
def room_detail(room_code):
    user = current_user()
    if user is None:
        return redirect(url_for("login"))

    if not is_room_member(room_code, user["id"]):
        abort(403)

    with connect_db() as conn:
        room = conn.execute(
            "SELECT * FROM rooms WHERE room_code=?",
            (room_code,),
        ).fetchone()

        members = conn.execute(
            """SELECT u.id, u.user_id
               FROM room_members m
               JOIN users u ON u.id=m.user_id
               WHERE m.room_code=?
               ORDER BY m.joined_at""",
            (room_code,),
        ).fetchall()

        messages = conn.execute(
            """SELECT m.message, m.created_at, u.user_id
               FROM room_messages m
               JOIN users u ON u.id=m.user_id
               WHERE m.room_code=?
               ORDER BY m.id DESC
               LIMIT 100""",
            (room_code,),
        ).fetchall()

    if room is None:
        abort(404)

    messages = list(reversed(messages))

    # ルームメンバー全員の服を取得
    with connect_db() as conn:
        items = conn.execute(
            """SELECT ci.*, u.user_id AS owner_name
               FROM clothing_items ci
               JOIN room_members rm ON rm.user_id=ci.owner_id
               JOIN users u ON u.id=ci.owner_id
               WHERE rm.room_code=?
               ORDER BY ci.created_at DESC""",
            (room_code,),
        ).fetchall()

    body = """
    <section class="panel">
      <h2>{{ room['room_name'] }}</h2>

      <p>招待コード：<b>{{ room['room_code'] }}</b></p>

      <h3>メンバー</h3>
      {% for member in members %}
        <span>{{ member['user_id'] }}</span>　
      {% endfor %}
    </section>

    <section class="panel">
      <h2>みんなのクローゼット</h2>
      <section class="grid">
        {% for item in items %}
          <article class="card">
            <img src="{{ url_for('photo', item_id=item['id']) }}"
                 alt="{{ item['title'] }}">
            <h3>{{ item['title'] }}</h3>
            <p class="muted">登録者：{{ item['owner_name'] }}</p>
          </article>
        {% else %}
          <p>まだ服が登録されていません。</p>
        {% endfor %}
      </section>
    </section>

    <section class="panel">
      <h2>ルームチャット</h2>

      <div id="chat-messages"
           style="height:350px;overflow-y:auto;border:1px solid #ddd;
                  padding:12px;border-radius:8px;background:#fafafa;">

        {% for msg in messages %}
          <div style="margin-bottom:12px;">
            <b>{{ msg['user_id'] }}</b>
            <span class="muted">{{ msg['created_at'] }}</span>
            <div style="overflow-wrap:anywhere;">
              {{ msg['message'] }}
            </div>
          </div>
        {% endfor %}

      </div>

      <form id="chat-form">
        <input id="chat-input" type="text"
               maxlength="1000" placeholder="メッセージを入力..." required>
        <button type="submit">送信</button>
      </form>
      <p id="chat-status" class="muted"></p>
    </section>

    <p><a href="{{ url_for('rooms') }}">ルーム一覧へ戻る</a></p>
    <p><a href="{{ url_for('index') }}">マイページへ戻る</a></p>

    <script>
    const chatBox = document.getElementById("chat-messages");
    const chatForm = document.getElementById("chat-form");
    const chatInput = document.getElementById("chat-input");
    const chatStatus = document.getElementById("chat-status");

    let latestMessageId = 0;

    async function loadMessages() {
      try {
        const response = await fetch(
          "{{ url_for('room_messages', room_code=room['room_code']) }}"
        );

        if (!response.ok) return;

        const data = await response.json();

        chatBox.replaceChildren();

        for (const msg of data.messages) {
          const wrapper = document.createElement("div");
          wrapper.style.marginBottom = "12px";

          const name = document.createElement("b");
          name.textContent = msg.user_id + " ";

          const time = document.createElement("span");
          time.className = "muted";
          time.textContent = msg.created_at;

          const content = document.createElement("div");
          content.style.overflowWrap = "anywhere";
          content.textContent = msg.message;

          wrapper.append(name, time, content);
          chatBox.appendChild(wrapper);
        }

        chatBox.scrollTop = chatBox.scrollHeight;
      } catch (error) {
        chatStatus.textContent = "メッセージを取得できませんでした。";
      }
    }

    chatForm.addEventListener("submit", async function(event) {
      event.preventDefault();

      const message = chatInput.value.trim();
      if (!message) return;

      const formData = new FormData();
      formData.append("message", message);

      try {
        const response = await fetch(
          "{{ url_for('room_send_message', room_code=room['room_code']) }}",
          { method: "POST", body: formData }
        );

        if (!response.ok) {
          chatStatus.textContent = "送信できませんでした。";
          return;
        }

        chatInput.value = "";
        chatStatus.textContent = "";
        await loadMessages();

      } catch (error) {
        chatStatus.textContent = "送信に失敗しました。";
      }
    });

    loadMessages();
    setInterval(loadMessages, 3000);
    </script>
    """

    return page(
        "ルーム",
        body,
        room=room,
        members=members,
        messages=messages,
        items=items,
    )
@app.route("/room/<room_code>/send", methods=["POST"])
def room_send_message(room_code):
    user = current_user()
    if user is None:
        abort(403)

    if not is_room_member(room_code, user["id"]):
        abort(403)

    message = request.form.get("message", "").strip()

    if not message or len(message) > 1000:
        return jsonify({"error": "メッセージは1〜1000文字です。"}), 400

    with connect_db() as conn:
        conn.execute(
            """INSERT INTO room_messages
               (room_code, user_id, message)
               VALUES (?, ?, ?)""",
            (room_code, user["id"], message),
        )

    return jsonify({"success": True})

@app.route("/room/<room_code>/messages")
def room_messages(room_code):
    user = current_user()
    if user is None:
        abort(403)

    if not is_room_member(room_code, user["id"]):
        abort(403)

    with connect_db() as conn:
        messages = conn.execute(
            """SELECT m.id, m.message, m.created_at, u.user_id
               FROM room_messages m
               JOIN users u ON u.id=m.user_id
               WHERE m.room_code=?
               ORDER BY m.id DESC
               LIMIT 100""",
            (room_code,),
        ).fetchall()

    return jsonify({
        "messages": [
            {
                "id": msg["id"],
                "message": msg["message"],
                "user_id": msg["user_id"],
                "created_at": str(msg["created_at"]),
            }
            for msg in reversed(messages)
        ]
    })
@app.errorhandler(413)
def too_large(_error):
    return "写真は10MB以下にしてください。", 413

# Create tables when the app starts.
init_db()
with connect_db() as conn:
    conn.execute("""CREATE TABLE IF NOT EXISTS rooms (
    room_code TEXT PRIMARY KEY,
    room_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    owner_id INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(owner_id) REFERENCES users(id) ON DELETE CASCADE
)""")

    conn.execute("""CREATE TABLE IF NOT EXISTS room_members (
    room_code TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(room_code, user_id),
    FOREIGN KEY(room_code) REFERENCES rooms(room_code) ON DELETE CASCADE,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
)""")

    conn.execute("""CREATE TABLE IF NOT EXISTS room_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_code TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    message TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(room_code) REFERENCES rooms(room_code) ON DELETE CASCADE,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
)""")

if __name__ == "__main__":
    # Local development only. For public hosting, use a production WSGI server.
    app.run(debug=False)

