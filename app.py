# ============================================================
# IMPORTS
# ============================================================
import os, io, json, time, warnings
from datetime import datetime, timedelta
from pathlib import Path

import requests
import pandas as pd
import urllib3
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from dotenv import load_dotenv

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager

# ============================================================
# LOAD ENV (FIX PATH)
# ============================================================
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

TERMINUS_USER = os.getenv("TERMINUS_USER")
TERMINUS_PASS = os.getenv("TERMINUS_PASS")
PHPSESSID     = os.getenv("PHPSESSID")
PAGE_SIZE     = int(os.getenv("PAGE_SIZE", 50))

if not TERMINUS_USER or not TERMINUS_PASS:
    raise RuntimeError("❌ TERMINUS_USER / TERMINUS_PASS missing in .env")

# ============================================================
# CONFIG
# ============================================================
BASE_URL  = "https://api-v2.terminusfleet.com/api/servicerepairdevice"
LOGIN_URL = "https://app-v2.terminusfleet.com/"
COMPANY_ID = 31

app = FastAPI(title="data_lost_gps API")

# ============================================================
# TOKEN CACHE
# ============================================================
_cached_token = None
_token_timestamp = None

# ============================================================
# TERMINUS LOGIN (SELENIUM)
# ============================================================
def get_new_token(timeout=25) -> str:
    global _cached_token, _token_timestamp

    chrome_opts = Options()
    chrome_opts.add_argument("--headless=new")
    chrome_opts.add_argument("--no-sandbox")
    chrome_opts.add_argument("--disable-dev-shm-usage")
    chrome_opts.add_experimental_option("excludeSwitches", ["enable-logging"])
    chrome_opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=chrome_opts
    )

    driver.get(LOGIN_URL)

    driver.find_element(By.NAME, "username").send_keys(TERMINUS_USER)
    driver.find_element(By.NAME, "password").send_keys(TERMINUS_PASS)
    driver.find_element(By.XPATH, "//*[@id='root']//form/button").click()

    deadline = time.time() + timeout
    seen = set()
    token = None

    while time.time() < deadline:
        for entry in driver.get_log("performance"):
            msg = json.loads(entry["message"])["message"]
            if msg.get("method") != "Network.requestWillBeSent":
                continue

            req_id = msg["params"]["requestId"]
            if req_id in seen:
                continue
            seen.add(req_id)

            headers = msg["params"]["request"]["headers"]
            auth = headers.get("Authorization")
            if auth and auth.lower().startswith("bearer "):
                token = auth
                break

        if token:
            break
        time.sleep(0.2)

    driver.quit()

    if not token:
        raise RuntimeError("❌ Cannot obtain Terminus token")

    _cached_token = token
    _token_timestamp = datetime.now()
    return token


def get_token() -> str:
    global _cached_token, _token_timestamp

    if not _cached_token:
        return get_new_token()

    if datetime.now() - _token_timestamp > timedelta(hours=2):
        return get_new_token()

    return _cached_token

# ============================================================
# UTILS
# ============================================================
def format_timedelta(td: timedelta) -> str:
    days = td.days
    hours = td.seconds // 3600
    minutes = (td.seconds % 3600) // 60
    return f"{days} วัน {hours} ชั่วโมง {minutes} นาที"

# ============================================================
# TERMINUS DATA
# ============================================================
def fetch_terminus_data() -> pd.DataFrame:
    token = get_token()

    headers = {
        "Authorization": token,
        "Content-Type": "application/json"
    }

    payload = {
        "page": 1,
        "pageSize": PAGE_SIZE,
        "searchName": "",
        "orderBy": "id",
        "orderType": "asc",
        "filterObj": {
            "plate_no": "",
            "code": "",
            "device_user": "",
            "vehicle_type": "",
            "maintenance_status": "",
            "type": "",
            "location_code": "",
            "zone": "",
            "closed_in_30days": False,
            "searchInput": ""
        },
        "company_id": COMPANY_ID
    }
    print(token)
    all_results = []
    page = 1

    while True:
        payload["page"] = page
        resp = requests.post(BASE_URL, headers=headers, json=payload, timeout=30)

        if resp.status_code != 200:
            raise RuntimeError(f"❌ Terminus API error {resp.status_code}: {resp.text}")

        rows = resp.json().get("data", [])
        if not rows:
            break

        all_results.extend(rows)
        page += 1
        time.sleep(0.3)

    df = pd.DataFrame(all_results)
    return df[["code", "plate_no", "gps_active_at"]]

# ============================================================
# ATMS DATA (FOLLOW ORIGINAL LOGIC)
# ============================================================
def fetch_atms_data() -> pd.DataFrame:
    url = "https://www.mena-atms.com/report/print.out/print.excel/type/vehicle.daily.transaction"

    headers = {
        "Referer": url,
        "Content-Type": "application/x-www-form-urlencoded",
        "Cookie": f"PHPSESSID={PHPSESSID}",
    }

    yesterday = (datetime.today() - timedelta(days=1)).strftime("%d/%m/%Y")
    warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)

    all_results = []
    with requests.Session() as s:
        for fleet_group_id in ["1", "2"]:
            payload = {
                "fleet_group_id": fleet_group_id,
                "fleet_id": "",
                "t_date": yesterday,
                "num_of_day": "1",
                "submit": "พิมพ์",
                "display_type": "multiple-day",
                "report_type": "vehicle.daily.transaction",
            }

            r = s.post(url, data=payload, headers=headers, verify=False, timeout=60)
            r.raise_for_status()

            df = pd.read_excel(io.BytesIO(r.content), skiprows=1, dtype=str)
            df["fleet_group_id"] = fleet_group_id
            all_results.append(df)

    final_df = pd.concat(all_results, ignore_index=True)

    # normalize header (SAFETY)
    final_df.columns = final_df.columns.astype(str).str.strip()

    # 🔒 ORIGINAL MAPPING (DO NOT CHANGE)
    rename_map = {
        "หัว": "ยี่ห้อ",
        "Unnamed: 7": "เบอร์รถ",
        "Unnamed: 8": "ทะเบียน",
        "Unnamed: 13": "รหัส",
        "Unnamed: 14": "ชื่อ",
        "Unnamed: 15": "เบอร์โทร",
    }
    final_df = final_df.rename(columns=rename_map)

    final_df = final_df.dropna(subset=["ทะเบียน"])
    final_df["ทะเบียน"] = final_df["ทะเบียน"].str.replace("สบ.", "", regex=False)

    return final_df[
        ["วันที่", "ฟลีท", "แพลนท์", "ยี่ห้อ",
         "เบอร์รถ", "ทะเบียน", "รหัส", "ชื่อ", "เบอร์โทร", "สเตตัส"]
    ]

# ============================================================
# API
# ============================================================
@app.get("/")
def home():
    return {"message": "data_lost_gps API is running"}

@app.get("/download")
def download_excel():
    df_service = fetch_terminus_data()

    df_service["gps_active_at"] = pd.to_datetime(
        df_service["gps_active_at"],
        format="%d/%m/%Y %H:%M:%S",
        errors="coerce"
    )
    df_service = df_service.dropna(subset=["gps_active_at"])

    now = datetime.now()
    df_service["diff"] = now - df_service["gps_active_at"]
    df_service["diff_fmt"] = df_service["diff"].apply(format_timedelta)
    df_service["diff_hours"] = df_service["diff"].dt.total_seconds() / 3600
    df_service["over_2h"] = df_service["diff_hours"] > 2

    df_atms = fetch_atms_data()

    merged = df_service.merge(
        df_atms,
        left_on="plate_no",
        right_on="ทะเบียน",
        how="inner"
    )

    merged = merged[
        ["ฟลีท", "สเตตัส", "เบอร์รถ", "ทะเบียน",
         "รหัส", "ชื่อ", "เบอร์โทร",
         "gps_active_at", "diff", "over_2h", "diff_fmt"]
    ]

    output = io.BytesIO()
    merged.to_excel(output, index=False, engine="openpyxl")
    output.seek(0)

    filename = datetime.today().strftime("%d%m%Y") + "_data.xlsx"

    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
