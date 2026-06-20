import calendar
import csv
import hmac
import io
import os
import random
import shutil
import sqlite3
import unicodedata
from contextlib import closing
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path

import pandas as pd
import streamlit as st


BASE_DIR = Path(__file__).resolve().parent
LOCAL_DB_PATH = BASE_DIR / "shifts.db"
DB_PATH = Path(os.environ.get("SHIFT_DB_PATH", LOCAL_DB_PATH))
WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"]
MEMBER_START_OPTIONS = ["×", "17:00", "17:30", "18:00", "18:30", "19:00"]
STAFFING_TIME_SLOTS = ["17:00", "17:30", "18:00", "18:30", "19:00"]
TIME_DISPLAY_LABELS = {
    "17:00": "17",
    "17:30": "17b",
    "18:00": "18",
    "18:30": "18b",
    "19:00": "19",
}
MOTIVATION_OPTIONS = {
    "A": "A：めっちゃでたい",
    "B": "B：普通",
    "S": "S：少なめ",
    "C": "C：足りないとこだけでたい",
}
MOTIVATION_PRIORITY = {"A": 0, "B": 1, "S": 2, "C": 3}
MOTIVATION_RATIO_DIRECTION = {"A": 1, "B": 0, "S": -0.5, "C": -1}
BUSY_WEEKDAYS = {4, 5}
DEFAULT_MEMBER_NAMES = ["田中", "佐藤", "鈴木", "高橋", "伊藤"]


DEFAULT_APP_PASSWORD = "0000"


def get_app_password() -> str:
    return os.environ.get("SHIFT_APP_PASSWORD", DEFAULT_APP_PASSWORD)


def get_database_url() -> str | None:
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        return database_url

    try:
        return st.secrets.get("DATABASE_URL")
    except Exception:
        return None


class DatabaseConnection:
    def __init__(self, raw_connection, is_postgres: bool):
        self.raw_connection = raw_connection
        self.is_postgres = is_postgres

    def execute(self, query: str, params: tuple | list = ()):
        if self.is_postgres:
            query = query.replace("?", "%s")
        return self.raw_connection.execute(query, params)

    def commit(self) -> None:
        self.raw_connection.commit()

    def close(self) -> None:
        self.raw_connection.close()


def get_table_columns(connection: DatabaseConnection, table_name: str) -> set[str]:
    if connection.is_postgres:
        rows = connection.execute(
            """
            SELECT column_name AS name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = ?
            """,
            (table_name,),
        ).fetchall()
    else:
        rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row["name"] for row in rows}


def is_unique_constraint_error(error: Exception) -> bool:
    if isinstance(error, sqlite3.IntegrityError):
        return True
    return error.__class__.__name__ in {"UniqueViolation", "IntegrityError"}


def require_password() -> bool:
    if st.session_state.get("password_ok"):
        return True

    st.title("パスワード入力")
    password = st.text_input("パスワード", type="password")

    if st.button("ログイン", type="primary"):
        entered_password = unicodedata.normalize("NFKC", password).strip()
        saved_password = unicodedata.normalize("NFKC", get_app_password()).strip()
        if hmac.compare_digest(entered_password.encode("utf-8"), saved_password.encode("utf-8")):
            st.session_state["password_ok"] = True
            st.rerun()
        else:
            st.error("パスワードが違います。")

    return False


def get_connection() -> DatabaseConnection:
    database_url = get_database_url()
    if database_url:
        from psycopg import connect
        from psycopg.rows import dict_row

        return DatabaseConnection(connect(database_url, row_factory=dict_row, prepare_threshold=None), is_postgres=True)

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH != LOCAL_DB_PATH and not DB_PATH.exists() and LOCAL_DB_PATH.exists():
        shutil.copy2(LOCAL_DB_PATH, DB_PATH)

    connection = sqlite3.connect(DB_PATH, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    return DatabaseConnection(connection, is_postgres=False)


def init_db() -> None:
    with closing(get_connection()) as connection:
        id_column = "SERIAL PRIMARY KEY" if connection.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS members (
                id {id_column},
                name TEXT NOT NULL UNIQUE,
                is_newcomer INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS shifts (
                id {id_column},
                name TEXT NOT NULL,
                shift_date TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                motivation_level TEXT NOT NULL DEFAULT 'B',
                note TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS staffing_requirements (
                shift_date TEXT PRIMARY KEY,
                staff_1700 INTEGER NOT NULL,
                staff_1730 INTEGER NOT NULL,
                staff_1800 INTEGER NOT NULL,
                staff_1830 INTEGER NOT NULL,
                staff_1900 INTEGER NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.commit()

    migrate_staffing_requirements_table()
    migrate_shifts_table()
    migrate_members_table()
    seed_members()


def migrate_members_table() -> None:
    with closing(get_connection()) as connection:
        column_names = get_table_columns(connection, "members")
        if "is_newcomer" not in column_names:
            connection.execute("ALTER TABLE members ADD COLUMN is_newcomer INTEGER NOT NULL DEFAULT 0")
            connection.commit()


def migrate_shifts_table() -> None:
    with closing(get_connection()) as connection:
        column_names = get_table_columns(connection, "shifts")
        if "motivation_level" not in column_names:
            connection.execute("ALTER TABLE shifts ADD COLUMN motivation_level TEXT NOT NULL DEFAULT 'B'")
            connection.commit()


def migrate_staffing_requirements_table() -> None:
    with closing(get_connection()) as connection:
        column_names = get_table_columns(connection, "staffing_requirements")
        required_columns = {
            "shift_date",
            "staff_1700",
            "staff_1730",
            "staff_1800",
            "staff_1830",
            "staff_1900",
            "updated_at",
        }

        if not column_names:
            return

        if required_columns.issubset(column_names):
            if "note" not in column_names:
                connection.execute("ALTER TABLE staffing_requirements ADD COLUMN note TEXT NOT NULL DEFAULT ''")
                connection.commit()
            return

        connection.execute("ALTER TABLE staffing_requirements RENAME TO staffing_requirements_old")
        connection.execute(
            """
            CREATE TABLE staffing_requirements (
                shift_date TEXT PRIMARY KEY,
                staff_1700 INTEGER NOT NULL,
                staff_1730 INTEGER NOT NULL,
                staff_1800 INTEGER NOT NULL,
                staff_1830 INTEGER NOT NULL,
                staff_1900 INTEGER NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )
            """
        )

        if {"start_time", "end_time", "required_staff", "updated_at"}.issubset(column_names):
            connection.execute(
                """
                INSERT INTO staffing_requirements (
                    shift_date, staff_1700, staff_1730, staff_1800, staff_1830, staff_1900, note, updated_at
                )
                SELECT
                    shift_date,
                    required_staff,
                    required_staff,
                    required_staff,
                    0,
                    0,
                    '',
                    updated_at
                FROM staffing_requirements_old
                """
            )

        connection.execute("DROP TABLE staffing_requirements_old")
        connection.commit()

        column_names = get_table_columns(connection, "staffing_requirements")
        if "note" not in column_names:
            connection.execute("ALTER TABLE staffing_requirements ADD COLUMN note TEXT NOT NULL DEFAULT ''")
            connection.commit()


def seed_members() -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with closing(get_connection()) as connection:
        existing_count = connection.execute(
            "SELECT COUNT(*) AS count FROM members"
        ).fetchone()["count"]
        if existing_count > 0:
            return

        for name in DEFAULT_MEMBER_NAMES:
            connection.execute(
                """
                INSERT INTO members (name, created_at)
                VALUES (?, ?)
                ON CONFLICT(name) DO NOTHING
                """,
                (name, timestamp),
            )
        connection.commit()


def add_member(name: str, is_newcomer: bool = False) -> tuple[bool, str]:
    cleaned_name = name.strip()
    if not cleaned_name:
        return False, "名前を入力してください。"

    try:
        with closing(get_connection()) as connection:
            connection.execute(
                "INSERT INTO members (name, is_newcomer, created_at) VALUES (?, ?, ?)",
                (cleaned_name, int(is_newcomer), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            connection.commit()
        return True, f"{cleaned_name} さんを登録しました。"
    except Exception as error:
        if is_unique_constraint_error(error):
            return False, "その名前はすでに登録されています。"
        raise


def delete_member(name: str) -> tuple[bool, str]:
    cleaned_name = name.strip()
    if not cleaned_name:
        return False, "削除する名前を選択してください。"

    with closing(get_connection()) as connection:
        existing_member = connection.execute(
            "SELECT 1 FROM members WHERE name = ?",
            (cleaned_name,),
        ).fetchone()
        if not existing_member:
            return False, "選択した名前は登録されていません。"

        deleted_shifts = connection.execute(
            "SELECT COUNT(*) AS count FROM shifts WHERE name = ?",
            (cleaned_name,),
        ).fetchone()["count"]
        connection.execute("DELETE FROM shifts WHERE name = ?", (cleaned_name,))
        deleted_members = connection.execute(
            "DELETE FROM members WHERE name = ?",
            (cleaned_name,),
        ).rowcount
        connection.commit()

    if deleted_members == 0:
        return False, "削除に失敗しました。もう一度お試しください。"

    return True, f"{cleaned_name} さんを削除しました。削除した希望シフト件数: {deleted_shifts} 件"


def load_members() -> list[str]:
    with closing(get_connection()) as connection:
        rows = connection.execute(
            "SELECT name FROM members ORDER BY name COLLATE NOCASE ASC"
        ).fetchall()
    return [row["name"] for row in rows]


def load_newcomer_members() -> set[str]:
    with closing(get_connection()) as connection:
        rows = connection.execute(
            "SELECT name FROM members WHERE is_newcomer = 1"
        ).fetchall()
    return {row["name"] for row in rows}


def load_member_roster() -> pd.DataFrame:
    with closing(get_connection()) as connection:
        rows = connection.execute(
            """
            SELECT id, name, is_newcomer
            FROM members
            ORDER BY name COLLATE NOCASE ASC
            """
        ).fetchall()
    return pd.DataFrame(
        [
            {
                "id": row["id"],
                "名前": row["name"],
                "新人": bool(row["is_newcomer"]),
            }
            for row in rows
        ]
    )


def save_member_roster(roster_df: pd.DataFrame) -> None:
    with closing(get_connection()) as connection:
        for _, row in roster_df.iterrows():
            connection.execute(
                "UPDATE members SET is_newcomer = ? WHERE id = ?",
                (int(bool(row["新人"])), int(row["id"])),
            )
        connection.commit()


def month_range(target_month: date) -> tuple[date, date]:
    month_start = target_month.replace(day=1)
    _, last_day = calendar.monthrange(month_start.year, month_start.month)
    return month_start, month_start.replace(day=last_day)


def half_month_range(target_month: date, half_label: str) -> tuple[date, date]:
    month_start, month_end = month_range(target_month)
    if half_label == "前半":
        return month_start, month_start.replace(day=15)
    return month_start.replace(day=16), month_end


def get_half_month_days(target_month: date, half_label: str) -> list[date]:
    period_start, period_end = half_month_range(target_month, half_label)
    return [
        date(target_month.year, target_month.month, day)
        for day in range(period_start.day, period_end.day + 1)
    ]


def build_member_widget_prefix(name: str, target_month: date, half_label: str) -> str:
    return f"{name}_{target_month.strftime('%Y%m')}_{half_label}"


def render_year_month_half_selector(year_label: str, month_label: str, half_label: str, key_prefix: str) -> tuple[date, str]:
    today = date.today()
    year_options = list(range(today.year, today.year + 4))
    columns = st.columns(3)

    selected_year = columns[0].selectbox(
        year_label,
        options=year_options,
        index=0,
        format_func=lambda year: f"{year}年度",
        key=f"{key_prefix}_year",
    )
    selected_month = columns[1].selectbox(
        month_label,
        options=list(range(1, 13)),
        index=today.month - 1,
        format_func=lambda month: f"{month}月",
        key=f"{key_prefix}_month",
    )
    selected_half = columns[2].selectbox(
        half_label,
        options=["前半", "後半"],
        index=0 if today.day <= 15 else 1,
        key=f"{key_prefix}_half",
    )
    return date(selected_year, selected_month, 1), selected_half


def load_member_shifts(name: str, month_start: date, month_end: date) -> dict[str, sqlite3.Row]:
    with closing(get_connection()) as connection:
        rows = connection.execute(
            """
            SELECT shift_date, start_time, end_time, motivation_level, note
            FROM shifts
            WHERE name = ? AND shift_date BETWEEN ? AND ?
            ORDER BY shift_date ASC
            """,
            (name, month_start.isoformat(), month_end.isoformat()),
        ).fetchall()
    return {row["shift_date"]: row for row in rows}


def load_staffing_for_month(month_start: date, month_end: date) -> dict[str, sqlite3.Row]:
    with closing(get_connection()) as connection:
        rows = connection.execute(
            """
            SELECT
                shift_date,
                staff_1700,
                staff_1730,
                staff_1800,
                staff_1830,
                staff_1900,
                note
            FROM staffing_requirements
            WHERE shift_date BETWEEN ? AND ?
            ORDER BY shift_date ASC
            """,
            (month_start.isoformat(), month_end.isoformat()),
        ).fetchall()
    return {row["shift_date"]: row for row in rows}


def save_shift_entries(
    name: str,
    month_start: date,
    month_end: date,
    entries: list[dict[str, str]],
    motivation_level: str,
) -> None:
    with closing(get_connection()) as connection:
        connection.execute(
            "DELETE FROM shifts WHERE name = ? AND shift_date BETWEEN ? AND ?",
            (name, month_start.isoformat(), month_end.isoformat()),
        )
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for entry in entries:
            connection.execute(
                """
                INSERT INTO shifts (
                    name, shift_date, start_time, end_time, motivation_level, note, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    entry["shift_date"],
                    entry["start_time"],
                    entry["end_time"],
                    motivation_level,
                    entry["note"],
                    now,
                ),
            )
        connection.commit()


def save_staffing_entries(month_start: date, month_end: date, entries: list[dict[str, int | str]]) -> None:
    with closing(get_connection()) as connection:
        connection.execute(
            "DELETE FROM staffing_requirements WHERE shift_date BETWEEN ? AND ?",
            (month_start.isoformat(), month_end.isoformat()),
        )

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for entry in entries:
            connection.execute(
                """
                INSERT INTO staffing_requirements (
                    shift_date, staff_1700, staff_1730, staff_1800, staff_1830, staff_1900, note, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry["shift_date"],
                    int(entry["staff_1700"]),
                    int(entry["staff_1730"]),
                    int(entry["staff_1800"]),
                    int(entry["staff_1830"]),
                    int(entry["staff_1900"]),
                    str(entry.get("note", "")),
                    now,
                ),
            )
        connection.commit()


def load_shifts() -> pd.DataFrame:
    with closing(get_connection()) as connection:
        query = """
            SELECT
                s.id,
                s.name,
                s.shift_date,
                s.start_time,
                s.end_time,
                s.motivation_level,
                s.note,
                s.created_at,
                COALESCE(r.staff_1700, '') AS staff_1700,
                COALESCE(r.staff_1730, '') AS staff_1730,
                COALESCE(r.staff_1800, '') AS staff_1800,
                COALESCE(r.staff_1830, '') AS staff_1830,
                COALESCE(r.staff_1900, '') AS staff_1900
            FROM shifts AS s
            LEFT JOIN staffing_requirements AS r
                ON s.shift_date = r.shift_date
            ORDER BY s.shift_date ASC, s.start_time ASC, s.name ASC
        """
        rows = connection.execute(query).fetchall()
    columns = [
        "id",
        "name",
        "shift_date",
        "start_time",
        "end_time",
        "motivation_level",
        "note",
        "created_at",
        "staff_1700",
        "staff_1730",
        "staff_1800",
        "staff_1830",
        "staff_1900",
    ]
    return pd.DataFrame([dict(row) for row in rows], columns=columns)


def build_csv(dataframe: pd.DataFrame) -> bytes:
    output = io.StringIO()
    dataframe.to_csv(output, index=False, encoding="utf-8-sig", quoting=csv.QUOTE_MINIMAL)
    return output.getvalue().encode("utf-8-sig")


def load_table_font(size: int):
    from PIL import ImageFont

    font_candidates = [
        "C:/Windows/Fonts/meiryo.ttc",
        "C:/Windows/Fonts/YuGothM.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for font_path in font_candidates:
        if Path(font_path).exists():
            return ImageFont.truetype(font_path, size)
    return ImageFont.load_default()


def draw_centered_text(draw, box: tuple[int, int, int, int], text: str, font, fill: str) -> None:
    left, top, right, bottom = box
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    x = left + (right - left - text_width) / 2
    y = top + (bottom - top - text_height) / 2 - 1
    draw.text((x, y), text, font=font, fill=fill)


def build_schedule_png(dataframe: pd.DataFrame, title: str) -> bytes | None:
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    title_font = load_table_font(24)
    header_font = load_table_font(15)
    body_font = load_table_font(15)
    small_font = load_table_font(11)
    strong_font = load_table_font(17)

    columns = list(dataframe.columns)
    column_widths = []
    for column in columns:
        column_name = str(column)
        if column_name == "名前":
            column_widths.append(105)
        elif is_schedule_day_column(column_name):
            column_widths.append(68)
        else:
            column_widths.append(112)

    header_height = 34
    row_heights = []
    for _, row in dataframe.iterrows():
        max_lines = max(len(str(value).splitlines()) for value in row)
        row_heights.append(max(38, 18 + (max_lines * 19)))

    margin = 16
    title_height = 36
    table_width = sum(column_widths)
    table_height = header_height + sum(row_heights)
    image_width = table_width + (margin * 2)
    image_height = title_height + table_height + (margin * 2)

    image = Image.new("RGB", (image_width, image_height), "white")
    draw = ImageDraw.Draw(image)
    draw.text((margin, margin - 2), title, font=title_font, fill="#0f172a")

    x = margin
    y = margin + title_height
    for column, width in zip(columns, column_widths):
        draw.rectangle((x, y, x + width, y + header_height), fill="#f3f4f6", outline="#d1d5db")
        draw_centered_text(draw, (x + 4, y, x + width - 4, y + header_height), str(column), header_font, "#0f172a")
        x += width

    y += header_height
    for row_index, (_, row) in enumerate(dataframe.iterrows()):
        row_height = row_heights[row_index]
        is_shortage_row = row_index == len(dataframe) - 1
        x = margin
        for column, width in zip(columns, column_widths):
            value = row[column]
            cell_text = "" if pd.isna(value) else str(value)
            draw.rectangle((x, y, x + width, y + row_height), fill="#ffffff", outline="#d1d5db")

            if is_shortage_row and is_schedule_day_column(column):
                shortage_count = int(cell_text) if cell_text.isdigit() else 0
                if shortage_count > 0:
                    pill_width = 34
                    pill_height = 22
                    pill_left = x + (width - pill_width) / 2
                    pill_top = y + (row_height - pill_height) / 2
                    draw.rounded_rectangle(
                        (pill_left, pill_top, pill_left + pill_width, pill_top + pill_height),
                        radius=11,
                        fill="#fecaca",
                    )
                    draw_centered_text(
                        draw,
                        (int(pill_left), int(pill_top), int(pill_left + pill_width), int(pill_top + pill_height)),
                        str(shortage_count),
                        strong_font,
                        "#991b1b",
                    )
                else:
                    draw_centered_text(draw, (x, y, x + width, y + row_height), "0", body_font, "#9ca3af")
            elif is_schedule_day_column(column) and cell_text:
                lines = cell_text.splitlines()
                current_y = y + 5
                for line in lines:
                    if line.startswith("req:"):
                        draw_centered_text(
                            draw,
                            (x, current_y, x + width, current_y + 14),
                            line.removeprefix("req:"),
                            small_font,
                            "#64748b",
                        )
                        current_y += 15
                    elif line:
                        pill_width = min(width - 8, 38)
                        pill_height = 22
                        pill_left = x + (width - pill_width) / 2
                        draw.rounded_rectangle(
                            (pill_left, current_y, pill_left + pill_width, current_y + pill_height),
                            radius=11,
                            fill="#e0f2fe",
                        )
                        draw_centered_text(
                            draw,
                            (int(pill_left), current_y, int(pill_left + pill_width), current_y + pill_height),
                            line,
                            strong_font,
                            "#075985",
                        )
                        current_y += 23
            else:
                draw_centered_text(draw, (x + 4, y, x + width - 4, y + row_height), cell_text, body_font, "#111827")

            x += width
        y += row_height

    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def slot_field_name(slot: str) -> str:
    return f"staff_{slot.replace(':', '')}"


def display_time(value: str) -> str:
    return TIME_DISPLAY_LABELS.get(value, value)


def later_start_time(first: str, second: str) -> str:
    return max(first, second, key=STAFFING_TIME_SLOTS.index)


def motivation_adjusted_assignment_ratio(
    name: str,
    motivation_level: str,
    assignment_counts: dict[str, int],
    request_day_counts: dict[str, int],
) -> float:
    request_count = max(request_day_counts.get(name, 0), 1)
    current_ratio = assignment_counts.get(name, 0) / request_count
    one_shift_ratio = 1 / request_count
    motivation_offset = MOTIVATION_RATIO_DIRECTION.get(motivation_level, 0) * one_shift_ratio
    return current_ratio - motivation_offset


def busy_day_balance_score(
    name: str,
    is_busy_day: bool,
    busy_assignment_counts: dict[str, int],
    regular_assignment_counts: dict[str, int],
    busy_request_counts: dict[str, int],
    regular_request_counts: dict[str, int],
) -> float:
    busy_ratio = busy_assignment_counts.get(name, 0) / max(busy_request_counts.get(name, 0), 1)
    regular_ratio = regular_assignment_counts.get(name, 0) / max(regular_request_counts.get(name, 0), 1)
    if is_busy_day:
        return busy_ratio - regular_ratio
    return regular_ratio - busy_ratio


def load_shift_requests_for_month(month_start: date, month_end: date) -> list[sqlite3.Row]:
    with closing(get_connection()) as connection:
        rows = connection.execute(
            """
            SELECT name, shift_date, start_time, motivation_level
            FROM shifts
            WHERE shift_date BETWEEN ? AND ?
            ORDER BY shift_date ASC, start_time ASC, motivation_level ASC, name ASC
            """,
            (month_start.isoformat(), month_end.isoformat()),
        ).fetchall()
    return rows


def build_schedule_for_month(target_month: date, half_label: str) -> tuple[pd.DataFrame, list[str], dict[str, int]]:
    month_start, month_end = half_month_range(target_month, half_label)
    members = load_members()
    newcomer_members = load_newcomer_members()
    shift_requests = load_shift_requests_for_month(month_start, month_end)
    staffing_map = load_staffing_for_month(month_start, month_end)
    assignment_counts = {member: 0 for member in members}
    request_day_counts = {member: 0 for member in members}
    busy_assignment_counts = {member: 0 for member in members}
    regular_assignment_counts = {member: 0 for member in members}
    busy_request_counts = {member: 0 for member in members}
    regular_request_counts = {member: 0 for member in members}
    shortage_counts_by_date: dict[str, int] = {}
    visible_days = [
        current_day
        for current_day in calendar.Calendar(firstweekday=0).itermonthdates(target_month.year, target_month.month)
        if current_day.month == target_month.month and month_start <= current_day <= month_end
    ]
    schedule_entries = {
        member: {
            current_day.isoformat(): ""
            for current_day in visible_days
        }
        for member in members
    }
    warnings: list[str] = []

    requests_by_date: dict[str, list[sqlite3.Row]] = {}
    requested_dates_by_member: dict[str, set[str]] = {member: set() for member in members}
    requested_start_by_member_date: dict[tuple[str, str], str] = {}
    for row in shift_requests:
        requests_by_date.setdefault(row["shift_date"], []).append(row)
        requested_dates_by_member.setdefault(row["name"], set()).add(row["shift_date"])
        requested_start_by_member_date[(row["name"], row["shift_date"])] = row["start_time"]
        request_day_counts[row["name"]] = request_day_counts.get(row["name"], 0) + 1
        request_date = datetime.strptime(row["shift_date"], "%Y-%m-%d").date()
        if request_date.weekday() in BUSY_WEEKDAYS:
            busy_request_counts[row["name"]] = busy_request_counts.get(row["name"], 0) + 1
        else:
            regular_request_counts[row["name"]] = regular_request_counts.get(row["name"], 0) + 1

    for current_day in visible_days:
        current_key = current_day.isoformat()
        daily_requests = requests_by_date.get(current_key, [])
        is_busy_day = current_day.weekday() in BUSY_WEEKDAYS

        staffing_row = staffing_map.get(current_key)
        default_values = default_staffing_values(current_day, staffing_row)
        requirements = {
            slot: int(default_values[slot_field_name(slot)])
            for slot in STAFFING_TIME_SLOTS
        }

        request_map = {row["name"]: row["start_time"] for row in daily_requests}
        motivation_map = {row["name"]: row["motivation_level"] for row in daily_requests}
        random_tiebreakers = {name: random.random() for name in request_map}
        assigned_start_times: dict[str, str] = {}
        assigned_members: set[str] = set()

        def consecutive_assignment_streak(name: str) -> int:
            streak = 0
            previous_day = current_day - timedelta(days=1)
            while schedule_entries.get(name, {}).get(previous_day.isoformat()):
                streak += 1
                previous_day -= timedelta(days=1)
            return streak

        def candidate_sort_key(name: str) -> tuple[float, float, int, int, int, int, int, float]:
            return (
                motivation_adjusted_assignment_ratio(
                    name,
                    motivation_map.get(name, "B"),
                    assignment_counts,
                    request_day_counts,
                ),
                busy_day_balance_score(
                    name,
                    is_busy_day,
                    busy_assignment_counts,
                    regular_assignment_counts,
                    busy_request_counts,
                    regular_request_counts,
                ),
                MOTIVATION_PRIORITY.get(motivation_map.get(name, "B"), MOTIVATION_PRIORITY["B"]),
                consecutive_assignment_streak(name),
                assignment_counts.get(name, 0),
                request_day_counts.get(name, 0),
                -STAFFING_TIME_SLOTS.index(request_map[name]),
                random_tiebreakers[name],
            )

        def candidate_score(name: str, assigned_start: str) -> tuple[float, float, int, int, int, int, int, int, float]:
            sort_key = candidate_sort_key(name)
            start_gap = STAFFING_TIME_SLOTS.index(assigned_start) - STAFFING_TIME_SLOTS.index(request_map[name])
            return (*sort_key[:7], start_gap, sort_key[7])

        def add_scores(
            first: tuple[float, float, int, int, int, int, int, int, float],
            second: tuple[float, float, int, int, int, int, int, int, float],
        ) -> tuple[float, float, int, int, int, int, int, int, float]:
            return tuple(left + right for left, right in zip(first, second))  # type: ignore[return-value]

        def find_best_assignments(
            slot_demands: list[str],
            candidate_names: list[str],
            can_cover,
            assigned_start_for,
        ) -> dict[str, str] | None:
            if not slot_demands:
                return {}

            ordered_candidates = sorted(candidate_names, key=candidate_sort_key)
            slot_demands = sorted(
                slot_demands,
                key=lambda slot: sum(1 for name in ordered_candidates if can_cover(name, slot)),
            )
            zero_score: tuple[float, float, int, int, int, int, int, int, float] = (0, 0, 0, 0, 0, 0, 0, 0, 0)
            cache = {}

            def search(
                demand_index: int,
                used_members: tuple[str, ...],
            ) -> tuple[tuple[float, float, int, int, int, int, int, int, float], tuple[tuple[str, str], ...]] | None:
                if demand_index == len(slot_demands):
                    return zero_score, tuple()

                cache_key = (demand_index, used_members)
                if cache_key in cache:
                    return cache[cache_key]

                used_set = set(used_members)
                used_newcomer_count = sum(1 for name in used_set if name in newcomer_members)
                slot = slot_demands[demand_index]
                remaining_slots = len(slot_demands) - demand_index
                available_count = sum(1 for name in ordered_candidates if name not in used_set)
                if available_count < remaining_slots:
                    cache[cache_key] = None
                    return None

                best_result = None
                best_compare_key = None
                options = [
                    name
                    for name in ordered_candidates
                    if name not in used_set
                    and can_cover(name, slot)
                    and (name not in newcomer_members or used_newcomer_count == 0)
                ]
                for name in options:
                    assigned_start = assigned_start_for(name, slot)
                    next_used = tuple(sorted((*used_members, name)))
                    suffix = search(demand_index + 1, next_used)
                    if suffix is None:
                        continue

                    suffix_score, suffix_assignments = suffix
                    score = add_scores(candidate_score(name, assigned_start), suffix_score)
                    assignments = ((name, assigned_start), *suffix_assignments)
                    compare_key = score
                    if best_compare_key is None or compare_key < best_compare_key:
                        best_compare_key = compare_key
                        best_result = (score, assignments)

                cache[cache_key] = best_result
                return best_result

            result = search(0, tuple())
            if result is None:
                return None

            _, assignments = result
            return dict(assignments)

        def assign_member(name: str, start_time: str) -> None:
            assigned_members.add(name)
            assigned_start_times[name] = start_time
            assignment_counts[name] = assignment_counts.get(name, 0) + 1
            if is_busy_day:
                busy_assignment_counts[name] = busy_assignment_counts.get(name, 0) + 1
            else:
                regular_assignment_counts[name] = regular_assignment_counts.get(name, 0) + 1

        def build_exact_assignments() -> dict[str, str] | None:
            slot_demands = [
                slot
                for slot in STAFFING_TIME_SLOTS
                for _ in range(requirements[slot])
            ]
            return find_best_assignments(
                slot_demands,
                list(request_map),
                lambda name, slot: request_map[name] <= slot,
                lambda _name, slot: slot,
            )

        exact_assignments = build_exact_assignments()
        if exact_assignments is not None:
            for name, start_time in exact_assignments.items():
                assign_member(name, start_time)
        else:
            required_1700_count = requirements["17:00"]
            total_required_count = sum(requirements.values())
            candidates_1700 = [
                name
                for name, preferred_start in request_map.items()
                if preferred_start == "17:00"
            ]
            selected_1700_count = min(required_1700_count, len(candidates_1700))
            selected_1700_assignments = find_best_assignments(
                ["17:00"] * selected_1700_count,
                candidates_1700,
                lambda _name, _slot: True,
                lambda _name, slot: slot,
            ) or {}
            for name, start_time in selected_1700_assignments.items():
                assign_member(name, start_time)

            remaining_1700_deficit = required_1700_count - len(selected_1700_assignments)
            if remaining_1700_deficit > 0:
                warnings.append(
                    f"{current_day.strftime('%Y-%m-%d')} {display_time('17:00')} は {remaining_1700_deficit} 人不足しています。"
                )

            remaining_total_count = total_required_count - len(assigned_members)
            flexible_candidates = [
                name
                for name in request_map
                if name not in assigned_members
            ]
            flexible_target_slots = [
                slot
                for slot in STAFFING_TIME_SLOTS
                if slot != "17:00"
                for _ in range(requirements[slot])
            ]
            fallback_flexible_slot = flexible_target_slots[-1] if flexible_target_slots else "17:30"
            selected_flexible_count = min(remaining_total_count, len(flexible_candidates))
            flexible_slot_demands = [
                flexible_target_slots[index] if index < len(flexible_target_slots) else fallback_flexible_slot
                for index in range(selected_flexible_count)
            ]
            selected_flexible_assignments = find_best_assignments(
                flexible_slot_demands,
                flexible_candidates,
                lambda _name, _slot: True,
                lambda name, slot: later_start_time(request_map[name], slot),
            ) or {}

            for name, start_time in selected_flexible_assignments.items():
                assign_member(name, start_time)

            remaining_total_deficit = total_required_count - len(assigned_members)
            if remaining_total_deficit > 0:
                warnings.append(
                    f"{current_day.strftime('%Y-%m-%d')} 合計人数は {remaining_total_deficit} 人不足しています。"
                )
            shortage_count = max(remaining_1700_deficit, remaining_total_deficit)
            if shortage_count > 0:
                shortage_counts_by_date[current_key] = shortage_count

        for name, start_time in assigned_start_times.items():
            schedule_entries.setdefault(name, {})
            schedule_entries[name][current_key] = display_time(start_time)

    column_labels = [f"{current_day.day}日（{WEEKDAYS[current_day.weekday()]}）" for current_day in visible_days]
    column_keys = [current_day.isoformat() for current_day in visible_days]

    table_rows = []
    for member in members:
        row = {"名前": member}
        for label, key in zip(column_labels, column_keys):
            assigned_time = schedule_entries.get(member, {}).get(key, "")
            if key in requested_dates_by_member.get(member, set()):
                requested_start = display_time(requested_start_by_member_date[(member, key)])
                row[label] = f"req:{requested_start}\n{assigned_time}" if assigned_time else f"req:{requested_start}"
            else:
                row[label] = assigned_time
        row["出勤日数/希望日数"] = f"{assignment_counts.get(member, 0)}/{request_day_counts.get(member, 0)}"
        row["金土/日〜木"] = f"{busy_assignment_counts.get(member, 0)}/{regular_assignment_counts.get(member, 0)}"
        table_rows.append(row)

    schedule_df = pd.DataFrame(table_rows)
    return schedule_df, warnings, shortage_counts_by_date


def mark_shortage_columns(
    schedule_df: pd.DataFrame,
    shortage_counts_by_date: dict[str, int],
    target_month: date,
) -> pd.DataFrame:
    display_df = schedule_df.copy()
    shortage_row = {"名前": "不足人数"}
    for column in display_df.columns:
        if column in ("名前", "出勤日数/希望日数", "金土/日〜木"):
            continue
        day_number = str(column).split("日")[0]
        if day_number.isdigit():
            current_key = date(target_month.year, target_month.month, int(day_number)).isoformat()
            shortage_row[column] = shortage_counts_by_date.get(current_key, 0)
    shortage_row["出勤日数/希望日数"] = ""
    shortage_row["金土/日〜木"] = ""
    display_df = pd.concat([display_df, pd.DataFrame([shortage_row])], ignore_index=True)
    return display_df


def is_schedule_day_column(column: object) -> bool:
    return "日（" in str(column)


def schedule_cell_to_html(value: object, column: object, is_shortage_row: bool = False) -> str:
    if pd.isna(value):
        return ""

    if is_shortage_row and is_schedule_day_column(column):
        shortage_count = int(value) if str(value).isdigit() else 0
        if shortage_count > 0:
            return f"<span class='shortage-count'>{shortage_count}</span>"
        return "<span class='shortage-zero'>0</span>"

    lines = str(value).splitlines()
    rendered_lines = []
    for line in lines:
        escaped_line = escape(line)
        if line.startswith("req:"):
            rendered_lines.append(f"<small class='request-time'>{escape(line.removeprefix('req:'))}</small>")
        elif is_schedule_day_column(column) and line:
            rendered_lines.append(f"<span class='shift-time'>{escaped_line}</span>")
        else:
            rendered_lines.append(f"<span>{escaped_line}</span>")
    return "<br>".join(rendered_lines)


def build_schedule_table_html(dataframe: pd.DataFrame) -> str:
    headers = "".join(f"<th>{escape(str(column))}</th>" for column in dataframe.columns)
    rows = []
    for row_index, row in dataframe.iterrows():
        is_shortage_row = row_index == len(dataframe) - 1
        cells = "".join(
            f"<td>{schedule_cell_to_html(value, column, is_shortage_row)}</td>"
            for column, value in row.items()
        )
        row_class = " class='shortage-row'" if is_shortage_row else ""
        rows.append(f"<tr{row_class}>{cells}</tr>")
    return f"""
        <div class="schedule-table-wrap">
            <table class="schedule-table">
                <thead><tr>{headers}</tr></thead>
                <tbody>{''.join(rows)}</tbody>
            </table>
        </div>
    """


def render_schedule_table(dataframe: pd.DataFrame, enable_fullscreen: bool = False) -> None:
    table_html = build_schedule_table_html(dataframe)
    fullscreen_id = f"schedule_fullscreen_{abs(hash(tuple(dataframe.columns)))}"
    fullscreen_html = ""
    if enable_fullscreen:
        fullscreen_html = f"""
        <input type="checkbox" id="{fullscreen_id}" class="fullscreen-toggle">
        <label for="{fullscreen_id}" class="fullscreen-open">フルスクリーン表示</label>
        <div class="schedule-fullscreen">
            <div class="schedule-fullscreen-toolbar">
                <strong>完成シフト</strong>
                <label for="{fullscreen_id}" class="fullscreen-close">閉じる</label>
            </div>
            {table_html}
        </div>
        """

    st.markdown(
        f"""
        <style>
        .schedule-table-wrap {{
            overflow-x: auto;
            margin-top: 0.5rem;
        }}
        .schedule-table {{
            border-collapse: collapse;
            width: max-content;
            min-width: 100%;
            font-size: 0.78rem;
        }}
        .schedule-table th,
        .schedule-table td {{
            border: 1px solid #d1d5db;
            padding: 0.22rem 0.32rem;
            text-align: center;
            vertical-align: middle;
            white-space: nowrap;
        }}
        .schedule-table th {{
            background: #f3f4f6;
            font-weight: 600;
        }}
        .schedule-table td:first-child,
        .schedule-table th:first-child {{
            position: sticky;
            left: 0;
            background: #ffffff;
            text-align: left;
            z-index: 1;
        }}
        .schedule-table th:first-child {{
            background: #f3f4f6;
            z-index: 2;
        }}
        .schedule-table .request-time {{
            color: #64748b;
            font-size: 0.62rem;
            line-height: 1.1;
        }}
        .schedule-table .shift-time {{
            display: inline-block;
            min-width: 1.65rem;
            margin-top: 0.05rem;
            padding: 0.04rem 0.24rem;
            border-radius: 999px;
            background: #e0f2fe;
            color: #075985;
            font-size: 0.82rem;
            font-weight: 700;
            line-height: 1.2;
        }}
        .schedule-table .shortage-row td {{
            font-weight: 600;
        }}
        .schedule-table .shortage-count {{
            display: inline-block;
            min-width: 1.65rem;
            padding: 0.04rem 0.24rem;
            border-radius: 999px;
            background: #fecaca;
            color: #991b1b;
            font-size: 0.82rem;
            font-weight: 800;
            line-height: 1.2;
        }}
        .schedule-table .shortage-zero {{
            color: #9ca3af;
            font-weight: 500;
        }}
        .fullscreen-toggle {{
            position: absolute;
            opacity: 0;
            pointer-events: none;
        }}
        .fullscreen-open,
        .fullscreen-close {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            border: 1px solid #cbd5e1;
            border-radius: 6px;
            background: #ffffff;
            color: #0f172a;
            cursor: pointer;
            font-size: 0.9rem;
            font-weight: 600;
            line-height: 1.2;
        }}
        .fullscreen-open {{
            margin: 0.25rem 0 0.5rem;
            padding: 0.45rem 0.7rem;
        }}
        .fullscreen-close {{
            padding: 0.4rem 0.65rem;
        }}
        .schedule-fullscreen {{
            display: none;
        }}
        .fullscreen-toggle:checked ~ .schedule-fullscreen {{
            display: block;
            position: fixed;
            inset: 0;
            z-index: 999999;
            background: #ffffff;
            padding: 0.5rem;
            overflow: auto;
        }}
        .schedule-fullscreen-toolbar {{
            position: sticky;
            top: 0;
            z-index: 3;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 1rem;
            margin: -0.5rem -0.5rem 0.4rem;
            padding: 0.45rem 0.6rem;
            border-bottom: 1px solid #e5e7eb;
            background: #ffffff;
        }}
        .schedule-fullscreen .schedule-table {{
            font-size: 0.86rem;
        }}
        .schedule-fullscreen .schedule-table th,
        .schedule-fullscreen .schedule-table td {{
            padding: 0.26rem 0.36rem;
        }}
        </style>
        {fullscreen_html}
        {table_html}
        """,
        unsafe_allow_html=True,
    )


def render_weekday_header() -> None:
    columns = st.columns(7)
    for index, label in enumerate(WEEKDAYS):
        if index == 4:
            columns[index].markdown(
                f"<span style='display:inline-block;border-left:4px solid #f59e0b;padding-left:8px;'><strong>{label}</strong></span>",
                unsafe_allow_html=True,
            )
        elif index == 5:
            columns[index].markdown(
                f"<span style='display:inline-block;border-left:4px solid #3b82f6;padding-left:8px;'><strong>{label}</strong></span>",
                unsafe_allow_html=True,
            )
        else:
            columns[index].markdown(f"**{label}**")


def render_calendar_accent(day_index: int) -> None:
    if day_index == 4:
        st.markdown(
            "<div style='border-left:4px solid #f59e0b;padding-left:8px;'>",
            unsafe_allow_html=True,
        )
    elif day_index == 5:
        st.markdown(
            "<div style='border-left:4px solid #3b82f6;padding-left:8px;'>",
            unsafe_allow_html=True,
        )


def close_calendar_accent(day_index: int) -> None:
    if day_index in (4, 5):
        st.markdown("</div>", unsafe_allow_html=True)


def default_staffing_values(current_day: date, current_existing: sqlite3.Row | None) -> dict[str, int]:
    if current_existing:
        return {
            "staff_1700": int(current_existing["staff_1700"]),
            "staff_1730": int(current_existing["staff_1730"]),
            "staff_1800": int(current_existing["staff_1800"]),
            "staff_1830": int(current_existing["staff_1830"]),
            "staff_1900": int(current_existing["staff_1900"]),
        }

    if current_day.weekday() in (4, 5):
        return {
            "staff_1700": 2,
            "staff_1730": 1,
            "staff_1800": 2,
            "staff_1830": 1,
            "staff_1900": 0,
        }

    return {
        "staff_1700": 1,
        "staff_1730": 1,
        "staff_1800": 1,
        "staff_1830": 0,
        "staff_1900": 0,
    }


def render_member_calendar(name: str, target_month: date, half_label: str, widget_prefix: str) -> list[dict[str, str]]:
    month_start, month_end = half_month_range(target_month, half_label)
    existing = load_member_shifts(name, month_start, month_end)
    cal = calendar.Calendar(firstweekday=0)
    entries: list[dict[str, str]] = []

    st.caption("各日付で開始時間を選んでください。`×` は出勤不可です。")
    render_weekday_header()

    for week in cal.monthdatescalendar(month_start.year, month_start.month):
        columns = st.columns(7)
        for day_index, current_day in enumerate(week):
            with columns[day_index]:
                if current_day.month != month_start.month:
                    st.markdown("&nbsp;", unsafe_allow_html=True)
                    continue
                if current_day < month_start or current_day > month_end:
                    st.markdown("&nbsp;", unsafe_allow_html=True)
                    continue

                render_calendar_accent(day_index)
                current_key = current_day.isoformat()
                current_existing = existing.get(current_key)
                default_option = current_existing["start_time"] if current_existing else MEMBER_START_OPTIONS[0]
                if default_option not in MEMBER_START_OPTIONS:
                    default_option = MEMBER_START_OPTIONS[0]

                st.markdown(f"**{current_day.day}日**")
                selected_start = st.selectbox(
                    "開始時間",
                    MEMBER_START_OPTIONS,
                    index=MEMBER_START_OPTIONS.index(default_option),
                    format_func=display_time,
                    key=f"member_start_{widget_prefix}_{current_key}",
                    label_visibility="collapsed",
                )

                if selected_start != "×":
                    entries.append(
                        {
                            "shift_date": current_key,
                            "start_time": selected_start,
                            "end_time": "",
                            "note": "",
                        }
                    )

                close_calendar_accent(day_index)

    return entries


def render_staffing_calendar(target_month: date, half_label: str) -> list[dict[str, int | str]]:
    month_start, month_end = half_month_range(target_month, half_label)
    existing = load_staffing_for_month(month_start, month_end)
    rows = []
    for current_day in get_half_month_days(target_month, half_label):
        current_key = current_day.isoformat()
        current_existing = existing.get(current_key)
        defaults = default_staffing_values(current_day, current_existing)
        rows.append(
            {
                "shift_date": current_key,
                "日付": f"{current_day.day}日",
                "曜日": WEEKDAYS[current_day.weekday()],
                "17": defaults["staff_1700"],
                "17b": defaults["staff_1730"],
                "18": defaults["staff_1800"],
                "18b": defaults["staff_1830"],
                "19": defaults["staff_1900"],
                "備考": current_existing["note"] if current_existing else "",
            }
        )

    staffing_df = pd.DataFrame(rows)
    edited_df = st.data_editor(
        staffing_df,
        hide_index=True,
        num_rows="fixed",
        disabled=["日付", "曜日"],
        use_container_width=True,
        key=f"staffing_editor_{target_month.strftime('%Y%m')}_{half_label}",
        column_config={
            "shift_date": None,
            "日付": st.column_config.TextColumn("日付", width="small"),
            "曜日": st.column_config.TextColumn("曜日", width="small"),
            "17": st.column_config.NumberColumn("17", min_value=0, step=1, width="small"),
            "17b": st.column_config.NumberColumn("17b", min_value=0, step=1, width="small"),
            "18": st.column_config.NumberColumn("18", min_value=0, step=1, width="small"),
            "18b": st.column_config.NumberColumn("18b", min_value=0, step=1, width="small"),
            "19": st.column_config.NumberColumn("19", min_value=0, step=1, width="small"),
            "備考": st.column_config.TextColumn("備考", width="large"),
        },
    )

    entries: list[dict[str, int | str]] = []
    for _, row in edited_df.iterrows():
        def staffing_count(column_name: str) -> int:
            value = row[column_name]
            return 0 if pd.isna(value) else int(value)

        entries.append(
            {
                "shift_date": str(row["shift_date"]),
                "staff_1700": staffing_count("17"),
                "staff_1730": staffing_count("17b"),
                "staff_1800": staffing_count("18"),
                "staff_1830": staffing_count("18b"),
                "staff_1900": staffing_count("19"),
                "note": "" if pd.isna(row["備考"]) else str(row["備考"]),
            }
        )

    return entries

def render_member_page() -> None:
    st.subheader("希望シフト提出")
    members = load_members()
    if not members:
        st.warning("登録済みの名前がありません。先に管理者画面で名前を登録してください。")
        return

    selected_name = st.selectbox("名前を選択", members)
    target_month, selected_half = render_year_month_half_selector("年度を選択", "月を選択", "前半 or 後半", "member_selector")
    widget_prefix = build_member_widget_prefix(selected_name, target_month, selected_half)
    month_start, month_end = half_month_range(target_month, selected_half)
    existing_shifts = load_member_shifts(selected_name, month_start, month_end)
    existing_motivation = next(
        (
            row["motivation_level"]
            for row in existing_shifts.values()
            if row["motivation_level"] in MOTIVATION_OPTIONS
        ),
        "B",
    )
    reset_col, _ = st.columns([1, 4])
    if reset_col.button("リセット"):
        for current_day in get_half_month_days(target_month, selected_half):
            st.session_state[f"member_start_{widget_prefix}_{current_day.isoformat()}"] = MEMBER_START_OPTIONS[0]
        st.session_state[f"motivation_{widget_prefix}"] = "B"
        st.rerun()

    with st.form("member_calendar_form"):
        st.write(f"{target_month.strftime('%Y年%m月')} {selected_half} の希望シフト")
        motivation_level = st.selectbox(
            "やる気度",
            options=list(MOTIVATION_OPTIONS.keys()),
            index=list(MOTIVATION_OPTIONS.keys()).index(existing_motivation),
            format_func=lambda level: MOTIVATION_OPTIONS[level],
            key=f"motivation_{widget_prefix}",
        )
        entries = render_member_calendar(selected_name, target_month, selected_half, widget_prefix)
        submitted = st.form_submit_button("希望シフトを保存", type="primary")

    if submitted:
        save_shift_entries(selected_name, month_start, month_end, entries, motivation_level)
        st.success(f"{selected_name} さんの {target_month.strftime('%Y年%m月')} {selected_half} の希望を保存しました。")


def render_member_management() -> None:
    st.markdown("### バイト名簿")
    roster_df = load_member_roster()
    if roster_df.empty:
        st.info("登録済みの名前がありません。")
    else:
        with st.form("member_roster_form"):
            edited_roster_df = st.data_editor(
                roster_df,
                hide_index=True,
                num_rows="fixed",
                disabled=["id", "名前"],
                use_container_width=True,
                key="member_roster_editor",
                column_config={
                    "id": None,
                    "名前": st.column_config.TextColumn("名前", width="medium"),
                    "新人": st.column_config.CheckboxColumn("新人", width="small"),
                },
            )
            roster_submitted = st.form_submit_button("名簿を保存", type="primary")
        if roster_submitted:
            save_member_roster(edited_roster_df)
            st.success("名簿を保存しました。")

    st.markdown("### 名前の登録")
    with st.form("member_register_form"):
        new_member_name = st.text_input("登録する名前")
        new_member_is_newcomer = st.checkbox("新人")
        member_submitted = st.form_submit_button("名前を登録")
    if member_submitted:
        success, message = add_member(new_member_name, new_member_is_newcomer)
        if success:
            st.success(message)
            st.rerun()
        else:
            st.error(message)

    st.markdown("### 名前の削除")
    members = load_members()
    if members:
        with st.form("member_delete_form"):
            member_to_delete = st.selectbox("削除する名前", members)
            delete_submitted = st.form_submit_button("名前を削除")
        if delete_submitted:
            success, message = delete_member(member_to_delete)
            if success:
                st.success(message)
                st.rerun()
            else:
                st.error(message)
    else:
        st.info("削除できる名前がありません。")


def render_admin_page() -> None:
    st.subheader("管理者画面")

    st.markdown("### シフト作成")
    target_month, selected_half = render_year_month_half_selector("年度を選択", "管理する月を選択", "前半 or 後半", "admin_selector")
    if st.button("シフトを作成", type="primary"):
        generated_schedule_df, schedule_warnings, shortage_counts_by_date = build_schedule_for_month(target_month, selected_half)
        st.session_state["generated_schedule_df"] = generated_schedule_df
        st.session_state["generated_schedule_warnings"] = schedule_warnings
        st.session_state["generated_schedule_shortage_counts_by_date"] = shortage_counts_by_date
        st.session_state["generated_schedule_period"] = f"{target_month.strftime('%Y年%m月')} {selected_half}"

    generated_schedule_df = st.session_state.get("generated_schedule_df")
    generated_schedule_warnings = st.session_state.get("generated_schedule_warnings", [])
    generated_schedule_shortage_counts_by_date = st.session_state.get("generated_schedule_shortage_counts_by_date", {})
    generated_schedule_period = st.session_state.get("generated_schedule_period")
    current_period = f"{target_month.strftime('%Y年%m月')} {selected_half}"
    if generated_schedule_df is not None and generated_schedule_period == current_period:
        display_schedule_df = mark_shortage_columns(
            generated_schedule_df,
            generated_schedule_shortage_counts_by_date,
            target_month,
        )
        st.write(f"{generated_schedule_period} の自動作成シフト")
        st.caption("各セルは上段の小さい文字が希望時間、下段が出勤時間です。不足人数がある日は下段の不足人数行に赤いマーカーが付きます。")
        render_schedule_table(display_schedule_df, enable_fullscreen=True)
        download_csv_col, download_png_col = st.columns(2)
        with download_csv_col:
            st.download_button(
                label="作成したシフトをCSVダウンロード",
                data=build_csv(display_schedule_df),
                file_name="generated_shift_schedule.csv",
                mime="text/csv",
            )
        with download_png_col:
            schedule_png = build_schedule_png(display_schedule_df, generated_schedule_period)
            if schedule_png is None:
                st.warning("画像保存には pillow が必要です。requirements.txt を再インストールしてください。")
            else:
                st.download_button(
                    label="完成シフトを画像でダウンロード",
                    data=schedule_png,
                    file_name="generated_shift_schedule.png",
                    mime="image/png",
                )
        if generated_schedule_warnings:
            for warning in generated_schedule_warnings:
                st.warning(warning)
        else:
            st.success("必要人員を満たすシフトを作成しました。")

    st.markdown("### 必要人員カレンダー")

    with st.form("staffing_calendar_form"):
        st.write(f"{target_month.strftime('%Y年%m月')} {selected_half} の必要人員")
        staffing_entries = render_staffing_calendar(target_month, selected_half)
        staffing_submitted = st.form_submit_button("必要人員カレンダーを保存", type="primary")

    if staffing_submitted:
        month_start, month_end = half_month_range(target_month, selected_half)
        save_staffing_entries(month_start, month_end, staffing_entries)
        st.success(f"{target_month.strftime('%Y年%m月')} {selected_half} の必要人員設定を保存しました。")

    st.markdown("### 提出された希望一覧")
    shifts_df = load_shifts()
    if shifts_df.empty:
        st.info("まだ希望シフトは提出されていません。")
        render_member_management()
        return

    half_start, half_end = half_month_range(target_month, selected_half)
    period_filtered_df = shifts_df[
        (shifts_df["shift_date"] >= half_start.isoformat())
        & (shifts_df["shift_date"] <= half_end.isoformat())
    ]
    if period_filtered_df.empty:
        st.info("選択した期間の希望シフトはまだ提出されていません。")
        render_member_management()
        return

    filter_col1, filter_col2 = st.columns(2)
    all_dates = ["すべて"] + sorted(period_filtered_df["shift_date"].unique().tolist())
    all_names = ["すべて"] + sorted(period_filtered_df["name"].unique().tolist())
    selected_date = filter_col1.selectbox("日付で絞り込み", all_dates)
    selected_name = filter_col2.selectbox("メンバーで絞り込み", all_names)

    filtered_df = period_filtered_df.copy()
    if selected_date != "すべて":
        filtered_df = filtered_df[filtered_df["shift_date"] == selected_date]
    if selected_name != "すべて":
        filtered_df = filtered_df[filtered_df["name"] == selected_name]

    display_source_df = filtered_df.copy()
    display_source_df["start_time"] = display_source_df["start_time"].map(display_time)
    display_source_df["motivation_level"] = display_source_df["motivation_level"].map(
        lambda level: MOTIVATION_OPTIONS.get(level, "B：普通")
    )
    display_df = display_source_df.rename(
        columns={
            "id": "ID",
            "name": "名前",
            "shift_date": "日付",
            "start_time": "希望開始",
            "end_time": "希望終了",
            "motivation_level": "やる気度",
            "note": "備考",
            "created_at": "提出日時",
            "staff_1700": "17必要人数",
            "staff_1730": "17b必要人数",
            "staff_1800": "18必要人数",
            "staff_1830": "18b必要人数",
            "staff_1900": "19必要人数",
        }
    )
    st.dataframe(display_df, use_container_width=True, hide_index=True)
    st.download_button(
        label="CSVをダウンロード",
        data=build_csv(filtered_df),
        file_name="shift_requests.csv",
        mime="text/csv",
    )

    render_member_management()


def main() -> None:
    st.set_page_config(page_title="しょうの助シフト", page_icon="📅", layout="wide")
    if not require_password():
        return

    init_db()

    st.title("しょうの助シフト")
    page = st.sidebar.radio("画面を選択", ("希望シフト提出", "管理者画面"))
    if page == "希望シフト提出":
        render_member_page()
    else:
        render_admin_page()


if __name__ == "__main__":
    main()
