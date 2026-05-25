import calendar
import csv
import hmac
import io
import os
import shutil
import sqlite3
import unicodedata
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import streamlit as st


BASE_DIR = Path(__file__).resolve().parent
LOCAL_DB_PATH = BASE_DIR / "shifts.db"
DB_PATH = Path(os.environ.get("SHIFT_DB_PATH", LOCAL_DB_PATH))
WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"]
MEMBER_START_OPTIONS = ["×", "17:00", "17:30", "18:00", "18:30", "19:00"]
STAFFING_TIME_SLOTS = ["17:00", "17:30", "18:00", "18:30", "19:00"]
DEFAULT_MEMBER_NAMES = ["田中", "佐藤", "鈴木", "高橋", "伊藤"]


DEFAULT_APP_PASSWORD = "0000"


def get_app_password() -> str:
    return os.environ.get("SHIFT_APP_PASSWORD", DEFAULT_APP_PASSWORD)


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


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH != LOCAL_DB_PATH and not DB_PATH.exists() and LOCAL_DB_PATH.exists():
        shutil.copy2(LOCAL_DB_PATH, DB_PATH)

    connection = sqlite3.connect(DB_PATH, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    with closing(get_connection()) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS shifts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                shift_date TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
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
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.commit()

    migrate_staffing_requirements_table()
    seed_members()


def migrate_staffing_requirements_table() -> None:
    with closing(get_connection()) as connection:
        columns = connection.execute("PRAGMA table_info(staffing_requirements)").fetchall()
        column_names = {column["name"] for column in columns}
        required_columns = {
            "shift_date",
            "staff_1700",
            "staff_1730",
            "staff_1800",
            "staff_1830",
            "staff_1900",
            "updated_at",
        }

        if not columns or required_columns.issubset(column_names):
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
                updated_at TEXT NOT NULL
            )
            """
        )

        if {"start_time", "end_time", "required_staff", "updated_at"}.issubset(column_names):
            connection.execute(
                """
                INSERT INTO staffing_requirements (
                    shift_date, staff_1700, staff_1730, staff_1800, staff_1830, staff_1900, updated_at
                )
                SELECT
                    shift_date,
                    required_staff,
                    required_staff,
                    required_staff,
                    0,
                    0,
                    updated_at
                FROM staffing_requirements_old
                """
            )

        connection.execute("DROP TABLE staffing_requirements_old")
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


def add_member(name: str) -> tuple[bool, str]:
    cleaned_name = name.strip()
    if not cleaned_name:
        return False, "名前を入力してください。"

    try:
        with closing(get_connection()) as connection:
            connection.execute(
                "INSERT INTO members (name, created_at) VALUES (?, ?)",
                (cleaned_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            connection.commit()
        return True, f"{cleaned_name} さんを登録しました。"
    except sqlite3.IntegrityError:
        return False, "その名前はすでに登録されています。"


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
            SELECT shift_date, start_time, end_time, note
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
                staff_1900
            FROM staffing_requirements
            WHERE shift_date BETWEEN ? AND ?
            ORDER BY shift_date ASC
            """,
            (month_start.isoformat(), month_end.isoformat()),
        ).fetchall()
    return {row["shift_date"]: row for row in rows}


def save_shift_entries(name: str, month_start: date, month_end: date, entries: list[dict[str, str]]) -> None:
    with closing(get_connection()) as connection:
        connection.execute(
            "DELETE FROM shifts WHERE name = ? AND shift_date BETWEEN ? AND ?",
            (name, month_start.isoformat(), month_end.isoformat()),
        )
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for entry in entries:
            connection.execute(
                """
                INSERT INTO shifts (name, shift_date, start_time, end_time, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    entry["shift_date"],
                    entry["start_time"],
                    entry["end_time"],
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
                    shift_date, staff_1700, staff_1730, staff_1800, staff_1830, staff_1900, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry["shift_date"],
                    int(entry["staff_1700"]),
                    int(entry["staff_1730"]),
                    int(entry["staff_1800"]),
                    int(entry["staff_1830"]),
                    int(entry["staff_1900"]),
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
        return pd.read_sql_query(query, connection)


def build_csv(dataframe: pd.DataFrame) -> bytes:
    output = io.StringIO()
    dataframe.to_csv(output, index=False, encoding="utf-8-sig", quoting=csv.QUOTE_MINIMAL)
    return output.getvalue().encode("utf-8-sig")


def slot_field_name(slot: str) -> str:
    return f"staff_{slot.replace(':', '')}"


def load_shift_requests_for_month(month_start: date, month_end: date) -> list[sqlite3.Row]:
    with closing(get_connection()) as connection:
        rows = connection.execute(
            """
            SELECT name, shift_date, start_time
            FROM shifts
            WHERE shift_date BETWEEN ? AND ?
            ORDER BY shift_date ASC, start_time ASC, name ASC
            """,
            (month_start.isoformat(), month_end.isoformat()),
        ).fetchall()
    return rows


def build_schedule_for_month(target_month: date, half_label: str) -> tuple[pd.DataFrame, list[str], dict[str, int]]:
    month_start, month_end = half_month_range(target_month, half_label)
    members = load_members()
    shift_requests = load_shift_requests_for_month(month_start, month_end)
    staffing_map = load_staffing_for_month(month_start, month_end)
    assignment_counts = {member: 0 for member in members}
    request_day_counts = {member: 0 for member in members}
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
    for row in shift_requests:
        requests_by_date.setdefault(row["shift_date"], []).append(row)
        request_day_counts[row["name"]] = request_day_counts.get(row["name"], 0) + 1

    for current_day in visible_days:
        current_key = current_day.isoformat()
        daily_requests = requests_by_date.get(current_key, [])

        staffing_row = staffing_map.get(current_key)
        default_values = default_staffing_values(current_day, staffing_row)
        requirements = {
            slot: int(default_values[slot_field_name(slot)])
            for slot in STAFFING_TIME_SLOTS
        }

        request_map = {row["name"]: row["start_time"] for row in daily_requests}
        assigned_start_times: dict[str, str] = {}
        assigned_members: set[str] = set()

        for slot in STAFFING_TIME_SLOTS:
            required_count = requirements[slot]
            if required_count <= 0:
                continue

            candidates = [
                name
                for name, preferred_start in request_map.items()
                if name not in assigned_members and preferred_start <= slot
            ]
            candidates.sort(
                key=lambda name: (
                    assignment_counts.get(name, 0) / max(request_day_counts.get(name, 0), 1),
                    assignment_counts.get(name, 0),
                    request_day_counts.get(name, 0),
                    -STAFFING_TIME_SLOTS.index(request_map[name]),
                    name,
                )
            )

            selected_members = candidates[:required_count]
            for name in selected_members:
                assigned_members.add(name)
                assigned_start_times[name] = slot
                assignment_counts[name] = assignment_counts.get(name, 0) + 1

            remaining_deficit = required_count - len(selected_members)
            if remaining_deficit > 0:
                shortage_counts_by_date[current_key] = shortage_counts_by_date.get(current_key, 0) + remaining_deficit
                warnings.append(
                    f"{current_day.strftime('%Y-%m-%d')} {slot} は {remaining_deficit} 人不足しています。"
                )

        for name, start_time in assigned_start_times.items():
            schedule_entries.setdefault(name, {})
            schedule_entries[name][current_key] = f"{start_time}〜"

    column_labels = [f"{current_day.day}日（{WEEKDAYS[current_day.weekday()]}）" for current_day in visible_days]
    column_keys = [current_day.isoformat() for current_day in visible_days]

    table_rows = []
    for member in members:
        row = {"名前": member}
        for label, key in zip(column_labels, column_keys):
            row[label] = schedule_entries.get(member, {}).get(key, "")
        row["出勤日数/希望日数"] = f"{assignment_counts.get(member, 0)}/{request_day_counts.get(member, 0)}"
        table_rows.append(row)

    schedule_df = pd.DataFrame(table_rows)
    return schedule_df, warnings, shortage_counts_by_date


def mark_shortage_columns(
    schedule_df: pd.DataFrame,
    shortage_counts_by_date: dict[str, int],
    target_month: date,
) -> pd.DataFrame:
    display_df = schedule_df.copy()
    rename_map: dict[str, str] = {}
    for shortage_date in sorted(shortage_counts_by_date):
        shortage_day = datetime.strptime(shortage_date, "%Y-%m-%d").date()
        if shortage_day.year == target_month.year and shortage_day.month == target_month.month:
            column_name = f"{shortage_day.day}日（{WEEKDAYS[shortage_day.weekday()]}）"
            if column_name in display_df.columns:
                rename_map[column_name] = f"{column_name} *"

    if rename_map:
        display_df = display_df.rename(columns=rename_map)

    shortage_row = {"名前": "不足人数"}
    for column in display_df.columns:
        if column in ("名前", "出勤日数/希望日数"):
            continue
        base_column = column.replace(" *", "")
        day_number = base_column.split("日")[0]
        if day_number.isdigit():
            current_key = date(target_month.year, target_month.month, int(day_number)).isoformat()
            shortage_row[column] = shortage_counts_by_date.get(current_key, 0)
    shortage_row["出勤日数/希望日数"] = ""
    display_df = pd.concat([display_df, pd.DataFrame([shortage_row])], ignore_index=True)
    return display_df


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
    cal = calendar.Calendar(firstweekday=0)
    entries: list[dict[str, int | str]] = []

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
                defaults = default_staffing_values(current_day, current_existing)

                st.markdown(f"**{current_day.day}日**")
                slot_values = {}
                for slot in STAFFING_TIME_SLOTS:
                    field_name = f"staff_{slot.replace(':', '')}"
                    slot_values[field_name] = st.number_input(
                        slot,
                        min_value=0,
                        step=1,
                        value=defaults[field_name],
                        key=f"{field_name}_{current_key}",
                    )

                entries.append(
                    {
                        "shift_date": current_key,
                        "staff_1700": int(slot_values["staff_1700"]),
                        "staff_1730": int(slot_values["staff_1730"]),
                        "staff_1800": int(slot_values["staff_1800"]),
                        "staff_1830": int(slot_values["staff_1830"]),
                        "staff_1900": int(slot_values["staff_1900"]),
                    }
                )
                close_calendar_accent(day_index)

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
    reset_col, _ = st.columns([1, 4])
    if reset_col.button("リセット"):
        for current_day in get_half_month_days(target_month, selected_half):
            st.session_state[f"member_start_{widget_prefix}_{current_day.isoformat()}"] = MEMBER_START_OPTIONS[0]
        st.rerun()

    with st.form("member_calendar_form"):
        st.write(f"{target_month.strftime('%Y年%m月')} {selected_half} の希望シフト")
        entries = render_member_calendar(selected_name, target_month, selected_half, widget_prefix)
        submitted = st.form_submit_button("希望シフトを保存", type="primary")

    if submitted:
        month_start, month_end = half_month_range(target_month, selected_half)
        save_shift_entries(selected_name, month_start, month_end, entries)
        st.success(f"{selected_name} さんの {target_month.strftime('%Y年%m月')} {selected_half} の希望を保存しました。")


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
        st.caption("* が付いた日は必要人数を満たせていない日です。")
        st.dataframe(display_schedule_df, use_container_width=True, hide_index=True)
        st.download_button(
            label="作成したシフトをCSVダウンロード",
            data=build_csv(display_schedule_df),
            file_name="generated_shift_schedule.csv",
            mime="text/csv",
        )
        if generated_schedule_warnings:
            for warning in generated_schedule_warnings:
                st.warning(warning)
        else:
            st.success("必要人員を満たすシフトを作成しました。")

    st.markdown("### 名前の登録")
    with st.form("member_register_form"):
        new_member_name = st.text_input("登録する名前")
        member_submitted = st.form_submit_button("名前を登録")
    if member_submitted:
        success, message = add_member(new_member_name)
        if success:
            st.success(message)
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
        return

    half_start, half_end = half_month_range(target_month, selected_half)
    period_filtered_df = shifts_df[
        (shifts_df["shift_date"] >= half_start.isoformat())
        & (shifts_df["shift_date"] <= half_end.isoformat())
    ]
    if period_filtered_df.empty:
        st.info("選択した期間の希望シフトはまだ提出されていません。")
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

    display_df = filtered_df.rename(
        columns={
            "id": "ID",
            "name": "名前",
            "shift_date": "日付",
            "start_time": "希望開始",
            "end_time": "希望終了",
            "note": "備考",
            "created_at": "提出日時",
            "staff_1700": "17:00必要人数",
            "staff_1730": "17:30必要人数",
            "staff_1800": "18:00必要人数",
            "staff_1830": "18:30必要人数",
            "staff_1900": "19:00必要人数",
        }
    )
    st.dataframe(display_df, use_container_width=True, hide_index=True)
    st.download_button(
        label="CSVをダウンロード",
        data=build_csv(filtered_df),
        file_name="shift_requests.csv",
        mime="text/csv",
    )


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
