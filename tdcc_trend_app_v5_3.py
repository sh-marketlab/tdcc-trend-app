"""
TDCC 集保戶股權分散趨勢查詢工具 v5.3

功能：
1. 抓取 TDCC 查詢頁不同資料日期資料
2. 輸入股票代號 + 起訖日期，自動逐週抓歷史資料
3. 以網頁呈現持股級距「人數」趨勢圖
4. 顯示模式：常用級距 / 全部級距 / 三大族群
5. 固定顏色對應，同一級距在不同模式下顏色一致
6. 本機 Streamlit 執行，後續可再部署成外部網站

執行：
streamlit run tdcc_trend_app_v5_3.py
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Iterable
from io import StringIO

import pandas as pd
import plotly.express as px
import streamlit as st

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait


TDCC_URL = "https://www.tdcc.com.tw/portal/zh/smWeb/qryStock"
CACHE_DIR = Path("tdcc_cache_v5_3")
CACHE_DIR.mkdir(exist_ok=True)

BUCKET_ORDER = [
    "1-999",
    "1,000-5,000",
    "5,001-10,000",
    "10,001-15,000",
    "15,001-20,000",
    "20,001-30,000",
    "30,001-40,000",
    "40,001-50,000",
    "50,001-100,000",
    "100,001-200,000",
    "200,001-400,000",
    "400,001-600,000",
    "600,001-800,000",
    "800,001-1,000,000",
    "1,000,001以上",
]

COMMON_BUCKETS = [
    "1-999",
    "1,000-5,000",
    "5,001-10,000",
    "10,001-15,000",
    "100,001-200,000",
    "400,001-600,000",
    "1,000,001以上",
]

COLOR_MAP = {
    "1-999": "#E74C3C",              # 散戶
    "1,000-5,000": "#3498DB",
    "5,001-10,000": "#5DADE2",
    "10,001-15,000": "#48C9B0",
    "15,001-20,000": "#1ABC9C",
    "20,001-30,000": "#27AE60",
    "30,001-40,000": "#F1C40F",
    "40,001-50,000": "#F39C12",
    "50,001-100,000": "#E67E22",
    "100,001-200,000": "#9B59B6",
    "200,001-400,000": "#8E44AD",
    "400,001-600,000": "#34495E",
    "600,001-800,000": "#2C3E50",
    "800,001-1,000,000": "#7F8C8D",
    "1,000,001以上": "#000000",
    "散戶 (1-999)": "#E74C3C",
    "中實戶 (1,000-50,000)": "#3498DB",
    "大戶 (50,001以上)": "#000000",
}


# =========================
# Page / CSS
# =========================
def inject_css() -> None:
    st.markdown(
        """
        <style>
        .block-container {
            padding-top: 2.2rem;
            padding-left: 4.2rem;
            padding-right: 4.2rem;
            max-width: 1500px;
        }
        [data-testid="stSidebar"] {
            min-width: 330px;
            max-width: 360px;
        }
        [data-testid="stSidebar"] * {
            font-size: 18px !important;
        }
        h1 {
            font-size: 42px !important;
            font-weight: 800 !important;
            color: #1f2a44 !important;
        }
        h2, h3 {
            color: #1f2a44 !important;
            font-weight: 800 !important;
        }
        .stButton > button {
            font-size: 20px !important;
            font-weight: 700 !important;
            height: 56px !important;
            border-radius: 14px !important;
            background: #ff4b4b !important;
            color: white !important;
            border: none !important;
        }
        div[data-testid="stMetric"] {
            background: #f4f7fb;
            border: 1px solid #dde5ef;
            padding: 18px 20px;
            border-radius: 16px;
        }
        div[data-testid="stMetricLabel"] { font-size: 17px !important; }
        div[data-testid="stMetricValue"] { font-size: 28px !important; }
        </style>
        """,
        unsafe_allow_html=True,
    )


# =========================
# Helpers
# =========================
def parse_roc_to_date(roc_str: str) -> pd.Timestamp:
    """TDCC scaDate 多為民國年月日，例如 1150410 -> 2026-04-10。"""
    text = str(roc_str).strip()
    if len(text) == 7 and text.isdigit():
        return pd.Timestamp(year=int(text[:3]) + 1911, month=int(text[3:5]), day=int(text[5:7]))
    return pd.to_datetime(text, errors="coerce")


def normalize_bucket(label: object) -> str | None:
    """將 TDCC 表格中可能出現的級距文字，標準化成 BUCKET_ORDER。"""
    if label is None:
        return None
    text = str(label).replace("\u3000", " ").replace("~", "-").strip()
    text = re.sub(r"\s+", "", text)
    text = text.replace("股", "").replace("以上", "以上")

    if "1,000,001" in text or "1000001" in text:
        return "1,000,001以上"

    compact_to_bucket = {re.sub(r"\D", "", b): b for b in BUCKET_ORDER if "以上" not in b}
    digits = re.sub(r"\D", "", text)
    if digits in compact_to_bucket:
        return compact_to_bucket[digits]

    for bucket in BUCKET_ORDER:
        if bucket in text:
            return bucket
    return None


def safe_number(value: object) -> float | None:
    text = str(value).replace(",", "").replace("%", "").strip()
    num = pd.to_numeric(text, errors="coerce")
    if pd.isna(num):
        return None
    return float(num)


def cache_path(stock_id: str, date_value: str) -> Path:
    clean_stock = re.sub(r"[^0-9A-Za-z_]", "", stock_id)
    return CACHE_DIR / f"{clean_stock}_{date_value}.csv"


# =========================
# Selenium / Data Fetch
# =========================
@st.cache_resource(show_spinner=False)
def init_driver():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1500,2200")
    options.add_experimental_option("excludeSwitches", ["enable-logging"])
    return webdriver.Chrome(options=options)


def get_available_dates(driver) -> list[tuple[str, pd.Timestamp]]:
    driver.get(TDCC_URL)
    wait = WebDriverWait(driver, 20)
    elem = wait.until(EC.presence_of_element_located((By.ID, "scaDate")))
    select = Select(elem)

    dates: list[tuple[str, pd.Timestamp]] = []
    for opt in select.options:
        val = opt.get_attribute("value") or ""
        if not val:
            continue
        ts = parse_roc_to_date(val)
        if pd.notna(ts):
            dates.append((val, ts.normalize()))
    return dates


def find_stock_input(driver):
    candidates = [
        (By.ID, "StockNo"),
        (By.NAME, "StockNo"),
        (By.ID, "stockNo"),
        (By.NAME, "stockNo"),
        (By.CSS_SELECTOR, "input[id*='Stock']"),
        (By.CSS_SELECTOR, "input[name*='Stock']"),
        (By.CSS_SELECTOR, "input[type='text']"),
    ]
    for by, selector in candidates:
        try:
            elems = driver.find_elements(by, selector)
            for elem in elems:
                if elem.is_displayed() and elem.is_enabled():
                    return elem
        except Exception:
            continue
    raise NoSuchElementException("找不到可輸入的股票代號欄位")


def click_query_button(driver):
    candidates = [
        (By.NAME, "sub"),
        (By.CSS_SELECTOR, "input[name='sub']"),
        (By.CSS_SELECTOR, "input[type='submit']"),
        (By.CSS_SELECTOR, "button[type='submit']"),
        (By.XPATH, "//input[contains(@value,'查詢')]"),
        (By.XPATH, "//button[contains(.,'查詢')]"),
    ]
    last_error = None
    for by, selector in candidates:
        try:
            btn = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((by, selector)))
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            time.sleep(0.2)
            driver.execute_script("arguments[0].click();", btn)
            return
        except Exception as exc:
            last_error = exc
            continue
    raise NoSuchElementException(f"找不到查詢按鈕：{last_error}")


def parse_result_table(driver, date_ts: pd.Timestamp) -> pd.DataFrame:
    html = driver.page_source
    tables = pd.read_html(StringIO(html))

    target = None
    for tbl in tables:
        tmp = tbl.copy()
        joined_cols = "|".join([str(c) for c in tmp.columns])
        joined_values = "|".join(tmp.astype(str).head(3).fillna("").values.ravel().tolist())
        joined = joined_cols + "|" + joined_values
        if ("人數" in joined and ("持股" in joined or "分級" in joined)) and len(tmp) >= 5:
            target = tmp
            break

    if target is None:
        raise ValueError("找不到含有『持股分級 / 人數』的結果表格")

    target.columns = [str(c).strip() for c in target.columns]

    # TDCC 表格欄位可能因 read_html 解析而變動，以下採彈性比對。
    bucket_col = None
    holder_col = None
    share_col = None
    ratio_col = None

    for c in target.columns:
        name = str(c)
        if bucket_col is None and ("持股" in name or "分級" in name):
            bucket_col = c
        if holder_col is None and "人數" in name:
            holder_col = c
        if share_col is None and "股數" in name:
            share_col = c
        if ratio_col is None and ("比例" in name or "%" in name):
            ratio_col = c

    if bucket_col is None:
        bucket_col = target.columns[0]
    if holder_col is None:
        # 通常第 2 欄為人數。
        holder_col = target.columns[1] if len(target.columns) > 1 else target.columns[0]
    if share_col is None and len(target.columns) > 2:
        share_col = target.columns[2]

    rows = []
    for _, row in target.iterrows():
        bucket = normalize_bucket(row.get(bucket_col))
        if not bucket:
            continue
        holders = safe_number(row.get(holder_col))
        if holders is None:
            continue
        rows.append(
            {
                "date": date_ts,
                "bucket": bucket,
                "holders": holders,
                "shares": safe_number(row.get(share_col)) if share_col else None,
                "ratio": safe_number(row.get(ratio_col)) if ratio_col else None,
            }
        )

    df = pd.DataFrame(rows)
    if df.empty:
        raise ValueError("表格解析後沒有有效級距資料")
    return df.groupby(["date", "bucket"], as_index=False).first()


def fetch_one_date(driver, stock_id: str, date_value: str, date_ts: pd.Timestamp) -> pd.DataFrame:
    cp = cache_path(stock_id, date_value)
    if cp.exists():
        df = pd.read_csv(cp)
        df["date"] = pd.to_datetime(df["date"])
        return df

    driver.get(TDCC_URL)
    wait = WebDriverWait(driver, 20)

    select_elem = wait.until(EC.presence_of_element_located((By.ID, "scaDate")))
    select = Select(select_elem)
    select.select_by_value(date_value)
    time.sleep(0.3)

    stock_input = find_stock_input(driver)
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", stock_input)
    time.sleep(0.2)
    stock_input.click()
    try:
        stock_input.clear()
    except Exception:
        # 若 clear 失敗，用 JS 清空。
        pass
    driver.execute_script("arguments[0].value = '';", stock_input)
    stock_input.send_keys(str(stock_id).strip())

    click_query_button(driver)
    time.sleep(1.2)

    # 等待表格刷新。若 TDCC 載入較慢，這裡會多等一下。
    WebDriverWait(driver, 20).until(lambda d: len(pd.read_html(StringIO(d.page_source))) > 0)
    df = parse_result_table(driver, date_ts)
    df.to_csv(cp, index=False, encoding="utf-8-sig")
    return df


def fetch_history(stock_id: str, start_date: pd.Timestamp, end_date: pd.Timestamp):
    driver = init_driver()
    logs: list[str] = []
    frames: list[pd.DataFrame] = []

    try:
        all_dates = get_available_dates(driver)
        target_dates = [(v, ts) for v, ts in all_dates if start_date <= ts <= end_date]
        target_dates = sorted(target_dates, key=lambda x: x[1])

        if not target_dates:
            return pd.DataFrame(), ["查詢區間內沒有 TDCC 可用資料日。"]

        progress = st.progress(0)
        status = st.empty()

        for i, (date_value, date_ts) in enumerate(target_dates, start=1):
            status.info(f"正在抓第 {i}/{len(target_dates)} 週：{date_ts.date()}")
            try:
                df = fetch_one_date(driver, stock_id, date_value, date_ts)
                frames.append(df)
                logs.append(f"✅ {date_ts.date()} 成功")
            except Exception as exc:
                logs.append(f"❌ {date_ts.date()} 失敗：{exc}")
            progress.progress(i / len(target_dates))

        status.empty()
        progress.empty()
    finally:
        # 使用 cache_resource 時不強制 quit，避免下次查詢重開瀏覽器太慢。
        # 若要完全釋放，可取消下方註解。
        # try:
        #     driver.quit()
        # except Exception:
        #     pass
        pass

    if not frames:
        return pd.DataFrame(), logs

    full = pd.concat(frames, ignore_index=True)
    full = full.drop_duplicates(subset=["date", "bucket"]).sort_values(["date", "bucket"])
    return full, logs


# =========================
# Data Transform
# =========================
def build_pivot(df: pd.DataFrame) -> pd.DataFrame:
    pivot = df.pivot(index="date", columns="bucket", values="holders").reset_index()
    for bucket in BUCKET_ORDER:
        if bucket not in pivot.columns:
            pivot[bucket] = pd.NA
    return pivot[["date"] + BUCKET_ORDER].sort_values("date")


def build_group_df(pivot: pd.DataFrame) -> pd.DataFrame:
    group_df = pd.DataFrame({"date": pivot["date"]})
    group_df["散戶 (1-999)"] = pivot["1-999"]
    group_df["中實戶 (1,000-50,000)"] = pivot[
        [
            "1,000-5,000",
            "5,001-10,000",
            "10,001-15,000",
            "15,001-20,000",
            "20,001-30,000",
            "30,001-40,000",
            "40,001-50,000",
        ]
    ].sum(axis=1, min_count=1)
    group_df["大戶 (50,001以上)"] = pivot[
        [
            "50,001-100,000",
            "100,001-200,000",
            "200,001-400,000",
            "400,001-600,000",
            "600,001-800,000",
            "800,001-1,000,000",
            "1,000,001以上",
        ]
    ].sum(axis=1, min_count=1)
    return group_df


def total_holder_df(pivot: pd.DataFrame) -> pd.DataFrame:
    out = pivot[["date"]].copy()
    out["總持有人數"] = pivot[BUCKET_ORDER].sum(axis=1, min_count=1)
    return out


# =========================
# Charts
# =========================
def make_bucket_chart(pivot: pd.DataFrame, buckets: Iterable[str], title: str):
    cols = ["date"] + [b for b in buckets if b in pivot.columns]
    chart_df = pivot[cols]
    melted = chart_df.melt(id_vars="date", var_name="級距", value_name="人數").dropna()

    fig = px.line(
        melted,
        x="date",
        y="人數",
        color="級距",
        color_discrete_map=COLOR_MAP,
        markers=True,
    )

    highlight = {"1-999", "1,000-5,000", "1,000,001以上"}
    for trace in fig.data:
        if trace.name in highlight:
            trace.line.width = 4
            trace.opacity = 1
        else:
            trace.line.width = 2.4
            trace.opacity = 0.78

    fig.update_layout(
        title=title,
        height=540,
        title_font_size=24,
        font=dict(size=16),
        xaxis_title="日期",
        yaxis_title="人數",
        legend_title_text="系列",
        hovermode="x unified",
        margin=dict(l=30, r=30, t=80, b=45),
    )
    return fig


def make_group_chart(group_df: pd.DataFrame):
    melted = group_df.melt(id_vars="date", var_name="族群", value_name="人數").dropna()
    fig = px.line(
        melted,
        x="date",
        y="人數",
        color="族群",
        color_discrete_map=COLOR_MAP,
        markers=True,
    )
    for trace in fig.data:
        trace.line.width = 4 if trace.name == "散戶 (1-999)" else 3.5
    fig.update_layout(
        title="各持股族群人數變化趨勢（三大族群）",
        height=520,
        title_font_size=24,
        font=dict(size=16),
        xaxis_title="日期",
        yaxis_title="人數",
        legend_title_text="系列",
        hovermode="x unified",
        margin=dict(l=30, r=30, t=80, b=45),
    )
    return fig


def make_total_chart(pivot: pd.DataFrame):
    df = total_holder_df(pivot)
    fig = px.line(df, x="date", y="總持有人數", markers=True)
    fig.update_traces(line=dict(width=3.5), marker=dict(size=7))
    fig.update_layout(
        title="總持有人數趨勢",
        height=380,
        title_font_size=22,
        font=dict(size=16),
        xaxis_title="日期",
        yaxis_title="總人數",
        hovermode="x unified",
        margin=dict(l=30, r=30, t=70, b=45),
    )
    return fig


# =========================
# App
# =========================
def app() -> None:
    st.set_page_config(page_title="TDCC 集保戶股權分散趨勢查詢", layout="wide")
    inject_css()

    with st.sidebar:
        st.header("查詢設定")
        stock_id = st.text_input("股票代號", "6693", help="輸入台股股票代號，例如 2330、6693")
        start_str = st.text_input("起始日期", "2025/01/01")
        end_str = st.text_input("結束日期", "2026/04/17")

        st.header("顯示方式")
        view_mode = st.radio(
            "主圖顯示模式",
            ["常用級距", "全部級距", "三大族群"],
            index=0,
            help="同一級距顏色固定，切換模式時不用重新比對顏色。",
        )
        show_total = st.toggle("顯示總持有人數趨勢", value=True)
        show_logs = st.toggle("顯示抓取紀錄", value=False)
        run = st.button("開始查詢", use_container_width=True)

    st.title("TDCC 集保戶股權分散趨勢查詢 v5.3")
    st.caption("輸入股票代號與起訖日期，自動逐週抓取 TDCC 歷史資料，並畫出各持股級距人數變化曲線。")

    with st.expander("這版做了什麼", expanded=False):
        st.markdown(
            """
            - 抓取 TDCC 查詢頁的歷史資料日
            - 逐週查詢單一股票的持股分級資料
            - 顯示模式包含：常用級距、全部級距、三大族群
            - 固定每個級距的顏色，切換模式時顏色不會亂跳
            - 加入本機快取，重複查詢同一股票同一週會更快
            """
        )

    if not run:
        st.info("請在左側輸入股票代號與日期區間，然後按「開始查詢」。")
        return

    try:
        start_date = pd.to_datetime(start_str).normalize()
        end_date = pd.to_datetime(end_str).normalize()
    except Exception:
        st.error("日期格式無法解析，請用 2025/01/01 或 2025-01-01 這種格式。")
        return

    if start_date > end_date:
        st.error("起始日期不能晚於結束日期。")
        return

    raw_df, logs = fetch_history(stock_id.strip(), start_date, end_date)
    if raw_df.empty:
        st.warning("這段期間抓不到資料。請確認股票代號、日期範圍，或打開『顯示抓取紀錄』查看失敗原因。")
        if show_logs:
            st.subheader("抓取紀錄")
            st.code("\n".join(logs), language="text")
        return

    pivot = build_pivot(raw_df)
    group_df = build_group_df(pivot)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("股票代號", stock_id.strip())
    c2.metric("查詢週數", f"{len(pivot)}")
    c3.metric("最新資料日", str(pivot["date"].max().date()))
    c4.metric("本機快取筆數", f"{len(list(CACHE_DIR.glob(f'{stock_id.strip()}_*.csv')))}")

    st.divider()

    if view_mode == "常用級距":
        st.plotly_chart(
            make_bucket_chart(pivot, COMMON_BUCKETS, "各持股分級人數變化趨勢（常用級距）"),
            use_container_width=True,
        )
    elif view_mode == "全部級距":
        st.plotly_chart(
            make_bucket_chart(pivot, BUCKET_ORDER, "各持股分級人數變化趨勢（全部級距）"),
            use_container_width=True,
        )
    else:
        st.plotly_chart(make_group_chart(group_df), use_container_width=True)

    if show_total:
        st.plotly_chart(make_total_chart(pivot), use_container_width=True)

    st.subheader("明細表（每週各持股級距人數）")
    st.dataframe(pivot, use_container_width=True, height=420)

    csv = pivot.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "下載目前查詢結果 CSV",
        data=csv,
        file_name=f"tdcc_{stock_id.strip()}_history.csv",
        mime="text/csv",
    )

    if show_logs:
        st.subheader("抓取紀錄")
        st.code("\n".join(logs), language="text")


if __name__ == "__main__":
    app()
