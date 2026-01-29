# app.py
import os, io, json, time, warnings
from datetime import datetime, timedelta

import requests
import pandas as pd
import urllib3
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from dotenv import load_dotenv

# ── Load env ──────────────────────────────
load_dotenv()

TERMINUS_BEARER = os.getenv("TERMINUS_BEARER")
PHPSESSID      = os.getenv("PHPSESSID")
PAGE_SIZE      = int(os.getenv("PAGE_SIZE", 50))

if not TERMINUS_BEARER:
    raise RuntimeError("❌ TERMINUS_BEARER not found")

BASE_URL = "https://api-v2.terminusfleet.com/api/servicerepairdevice"

app = FastAPI(title="GPS Lost API")

# ──────────────────────────────────────────
# Utils
# ──────────────────────────────────────────
def format_timedelta(td):
    days = td.days
    hours = td.seconds // 3600
    minutes = (td.seconds % 3600) // 60
    return f"{days} วัน {hours} ชั่วโมง {minutes} นาที"


# ──────────────────────────────────────────
# Terminus API
# ──────────────────────────────────────────
def fetch_terminus_data() -> pd.DataFrame:
    headers = {
        "Authorization": TERMINUS_BEARER,
        "Content-Type": "application/json",
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
        "company_id": 31
    }

    results = []
    page = 1

    while True:
        payload["page"] = page
        r = requests.post(BASE_URL, headers=headers, json=payload, timeout=30)

        if r.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"Terminus API error {r.status_code}"
            )

        data = r.json().get("data", [])
        if not data:
            break

        results.extend(data)
        page += 1
        time.sleep(0.3)

    df = pd.DataFrame(results)

    if df.empty:
        return df

    return df[["code", "plate_no", "gps_active_at"]]


# ──────────────────────────────────────────
# ATMS
# ──────────────────────────────────────────
def fetch_atms_data() -> pd.DataFrame:
    url = "https://www.mena-atms.com/report/print.out/print.excel/type/vehicle.daily.transaction"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Cookie": f"PHPSESSID={PHPSESSID}",
        "Referer": url,
    }

    yesterday = (datetime.today() - timedelta(days=1)).strftime("%d/%m/%Y")
    warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)

    dfs = []

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
            dfs.append(df)

    final_df = pd.concat(dfs, ignore_index=True)

    rename_map = {
        "Unnamed: 7": "เบอร์รถ",
        "Unnamed: 8": "ทะเบียน",
        "Unnamed: 13": "รหัส",
        "Unnamed: 14": "ชื่อ",
        "Unnamed: 15": "เบอร์โทร",
    }

    final_df = final_df.rename(columns=rename_map)

    if "ทะเบียน" not in final_df.columns:
        raise RuntimeError(f"ATMS format changed: {list(final_df.columns)}")

    final_df["ทะเบียน"] = final_df["ทะเบียน"].str.replace("สบ.", "", regex=False)

    return final_df[
        ["ฟลีท", "แพลนท์", "เบอร์รถ", "ทะเบียน", "รหัส", "ชื่อ", "เบอร์โทร", "สเตตัส"]
    ]


# ──────────────────────────────────────────
# API
# ──────────────────────────────────────────
@app.get("/")
def health():
    return {"status": "ok"}

@app.get("/download")
def download_excel():
    df_service = fetch_terminus_data()

    df_service["gps_active_at"] = pd.to_datetime(
        df_service["gps_active_at"], errors="coerce"
    )

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
        ["ฟลีท", "สเตตัส", "เบอร์รถ", "ทะเบียน", "รหัส", "ชื่อ",
         "เบอร์โทร", "gps_active_at", "diff_fmt", "over_2h"]
    ]

    output = io.BytesIO()
    merged.to_excel(output, index=False, engine="openpyxl")
    output.seek(0)

    fname = datetime.today().strftime("%d%m%Y") + "_gps_lost.xlsx"

    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={fname}"}
    )
