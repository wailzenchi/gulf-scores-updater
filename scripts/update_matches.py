import json
import os
import sys
from datetime import datetime, timedelta, timezone

import firebase_admin
import requests
from firebase_admin import credentials, firestore

# ---------------- الإعدادات ----------------
API_KEY = os.environ["API_FOOTBALL_KEY"]
FORCE = os.environ.get("FORCE_FETCH", "false").lower() == "true"

# الدوريات التي نأخذ منها قائمة الأندية الخليجية (مرة واحدة فقط ثم تُحفظ في Firestore)
CLUB_LEAGUES = [307, 305, 301]   # السعودية، قطر، الإمارات

# المنتخبات الخليجية: نطابق بالاسم، ويشمل ذلك الشباب والسيدات
# (مثل: Saudi Arabia U23 أو Qatar W) لأننا نبحث عن الاسم داخل اسم الفريق
NATIONAL_KEYWORDS = [
    "saudi arabia", "qatar", "united arab emirates", "uae",
    "kuwait", "bahrain", "oman" ,
]

DAILY_LIMIT = 90                # حد أمان أقل من 100
BEFORE_KICKOFF = timedelta(minutes=10)
AFTER_KICKOFF = timedelta(minutes=150)
FINISHED = {"FT", "AET", "PEN", "PST", "CANC", "ABD", "AWD", "WO"}

# نعتبر اليوم بتوقيت الرياض (UTC+3)
GULF_TZ = timezone(timedelta(hours=3))
BASE_URL = "https://v3.football.api-sports.io"


def init_db():
    info = json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT"])
    firebase_admin.initialize_app(credentials.Certificate(info))
    return firestore.client()


def day_range(now):
    local = now.astimezone(GULF_TZ)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc), start.strftime("%Y-%m-%d")


def should_fetch(db, now, today_str, start, end):
    """نقرر هل نحتاج طلب API الآن، اعتماداً على Firestore فقط (بلا طلبات API)."""
    if FORCE:
        print("تشغيل يدوي إجباري")
        return True

    daily = db.collection("meta").document("daily_fetch").get()
    if not daily.exists or daily.to_dict().get("date") != today_str:
        print("أول جلب لهذا اليوم")
        return True

    docs = (
        db.collection("matches")
        .where("kickoff", ">=", start)
        .where("kickoff", "<", end)
        .stream()
    )
    for d in docs:
        m = d.to_dict()
        kickoff = m.get("kickoff")
        if kickoff is None or m.get("status") in FINISHED:
            continue
        if kickoff - BEFORE_KICKOFF <= now <= kickoff + AFTER_KICKOFF:
            print(f"مباراة نشطة: {m.get('homeTeam')} × {m.get('awayTeam')}")
            return True

    print("لا توجد مباراة نشطة. لا حاجة لطلب API.")
    return False


def use_request_budget(db, today_str):
    """يزيد عداد الطلبات. يرجع False إذا وصلنا الحد اليومي."""
    ref = db.collection("meta").document("api_usage")
    snap = ref.get()
    data = snap.to_dict() if snap.exists else {}
    count = data.get("count", 0) if data.get("date") == today_str else 0
    if count >= DAILY_LIMIT:
        print(f"وصلنا الحد اليومي ({count}). توقف.")
        return False
    ref.set({"date": today_str, "count": count + 1})
    print(f"طلب API رقم {count + 1} اليوم")
    return True


def get_club_ids(db, today_str):
    """معرفات الأندية الخليجية: تُقرأ من Firestore، وإن لم توجد تُجلب مرة واحدة فقط."""
    ref = db.collection("meta").document("gulf_clubs")
    snap = ref.get()
    if snap.exists and snap.to_dict().get("ids"):
        ids = set(snap.to_dict()["ids"])
        print(f"عدد الأندية الخليجية (من الذاكرة): {len(ids)}")
        return ids

    ids = set()
    for league_id in CLUB_LEAGUES:
        teams = []
        for season in (2024,):               # الخطة المجانية تسمح بـ 2022 إلى 2024 فقط
            if not use_request_budget(db, today_str):
                break
            r = requests.get(
                f"{BASE_URL}/teams",
                headers={"x-apisports-key": API_KEY},
                params={"league": league_id, "season": season},
                timeout=30,
            )
            r.raise_for_status()
            body = r.json()
            if body.get("errors"):
                print(f"تنبيه من API (دوري {league_id}، موسم {season}):", body["errors"])
            teams = body.get("response", [])
            if teams:
                break
        for t in teams:
            ids.add(t["team"]["id"])

    print(f"عدد الأندية الخليجية المحفوظة: {len(ids)}")
    if ids:
        ref.set({"ids": sorted(ids)})
    return ids


def is_gulf_match(item, club_ids):
    home, away = item["teams"]["home"], item["teams"]["away"]
    if home["id"] in club_ids or away["id"] in club_ids:
        return True
    for name in (home["name"].lower(), away["name"].lower()):
        if any(keyword in name for keyword in NATIONAL_KEYWORDS):
            return True
    return False


def fetch_fixtures(today_str):
    resp = requests.get(
        f"{BASE_URL}/fixtures",
        headers={"x-apisports-key": API_KEY},
        params={"date": today_str, "timezone": "Asia/Riyadh"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("errors"):
        print("خطأ من API-Football:", data["errors"])
        sys.exit(1)
    print("الطلبات المتبقية حسب API:", resp.headers.get("x-ratelimit-requests-remaining"))
    return data.get("response", [])


def to_doc(item):
    f, l, t, g = item["fixture"], item["league"], item["teams"], item["goals"]
    doc = {
        "leagueName": l["name"],
        "homeTeam": t["home"]["name"],
        "awayTeam": t["away"]["name"],
        "homeLogo": t["home"].get("logo") or "",
        "awayLogo": t["away"].get("logo") or "",
        "homeScore": int(g["home"] or 0),
        "awayScore": int(g["away"] or 0),
        "status": f["status"]["short"],
        "kickoff": datetime.fromtimestamp(f["timestamp"], tz=timezone.utc),
    }
    second = (f.get("periods") or {}).get("second")
    if second:
        doc["secondHalfStart"] = datetime.fromtimestamp(second, tz=timezone.utc)
    return str(f["id"]), doc


def main():
    db = init_db()
    now = datetime.now(timezone.utc)
    start, end, today_str = day_range(now)

    if not should_fetch(db, now, today_str, start, end):
        return

    club_ids = get_club_ids(db, today_str)

    if not use_request_budget(db, today_str):
        return

    all_items = fetch_fixtures(today_str)
    print(f"إجمالي مباريات العالم اليوم: {len(all_items)}")

    items = all_items[:5]   # اختبار مؤقت: أول 5 مباريات مهما كانت
    print(f"عدد مباريات الفرق الخليجية اليوم: {len(items)}")
    for i in items:
        print(f" - {i['teams']['home']['name']} × {i['teams']['away']['name']} | {i['league']['name']}")

    batch = db.batch()
    for item in items:
        match_id, doc = to_doc(item)
        batch.set(db.collection("matches").document(match_id), doc, merge=True)
    batch.set(db.collection("meta").document("daily_fetch"), {"date": today_str})
    batch.commit()
    print("تم تحديث Firestore بنجاح")


if __name__ == "__main__":
    main()
