# -*- coding: utf-8 -*-
"""
Media Screening App — Energy Sovereignty Framing & Cost of Delayed Transition
IESR internal tool. Periode analisis: 1 Januari – 31 Juli 2026.

Jalankan:
    pip install -r requirements.txt
    streamlit run media_screening_app.py

Data disimpan di SQLite lokal (media_screening.db) — aman untuk audit trail,
bisa dibackup dengan menyalin satu file.
"""

import json
import re
import sqlite3
import unicodedata
from datetime import datetime, date
from difflib import SequenceMatcher
from itertools import combinations
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import pandas as pd
import requests
import streamlit as st

DB_PATH = "media_screening.db"
CODEBOOK_VERSION = "v1.0 (2026-07)"
PERIOD_START = date(2026, 1, 1)
PERIOD_END = date(2026, 7, 31)

ACTOR_GROUPS = ["Public", "Media", "Stakeholder", "NGO-CSO/Ahli"]
CORE_STRATA = ["Public", "Media", "Stakeholder"]          # skema bobot asli, 3 strata
EXTENDED_STRATA = ["Public", "Media", "Stakeholder", "NGO-CSO/Ahli"]  # skema bobot 4 strata
DOC_TYPES = [
    "Berita", "Analisis berita", "Wawancara jurnalistik",
    "Op-ed / kolom / opini", "Siaran pers / press release",
    "Pernyataan pemerintah/parlemen/BUMN", "Regulasi / dokumen kebijakan",
    "Pernyataan organisasi (NGO/CSO/asosiasi)",
    "Lainnya",
]
SEARCH_METHODS = ["Manual Google Search", "SerpApi", "GDELT DOC API", "Database berlangganan", "Manual (lainnya)"]

SOV_FRAMES = [
    "Clean transition / reduced fossil dependence",
    "Domestic fossil expansion / continued fossil use",
    "Mixed / contested",
    "Neutral / descriptive",
]
SOV_PRESENCE = ["Explicit", "Implicit", "None"]
# Subkode khusus untuk "ketahanan energi" yang ambigu (keandalan pasokan vs kedaulatan)
KETAHANAN_SENSE = ["Tidak relevan", "Ketahanan = keandalan pasokan", "Ketahanan = kedaulatan/struktur energi", "Ambigu"]

COD_RECOGNITION = ["Explicit", "Implicit", "None"]
COD_DIRECTION = [
    "Cost of delay emphasized",
    "Short-term transition cost emphasized",
    "Both / contested",
    "No cost frame",
]
COST_TYPES = [
    "Pertumbuhan ekonomi", "Fiskal dan subsidi", "Ketergantungan impor & keamanan energi",
    "Lapangan kerja dan keterampilan", "Daya saing industri & perdagangan",
    "Investasi & stranded assets", "Kesehatan dan polusi", "Kerugian iklim & bencana",
    "Dampak distribusional & keadilan sosial",
]
EXCLUSION_REASONS = [
    "Duplikat / repost identik",
    "Iklan / sponsored content",
    "Hanya harga saham/komoditas harian",
    "Keyword hanya di menu/tag/rekomendasi",
    "Tidak dapat diakses / diverifikasi",
    "Tidak terkait pilihan jalur energi / cost of delay",
    "Di luar periode Jan–Jul 2026",
    "Tidak relevan dengan Indonesia",
    "Lainnya (catat di notes)",
]

DEFAULT_QUERIES = {
    "A. Sovereignty — broad": '("kedaulatan energi" OR "kemandirian energi" OR "swasembada energi" OR "ketahanan energi") AND ("transisi energi" OR "energi terbarukan" OR "energi bersih" OR batubara OR "batu bara" OR migas OR minyak OR gas) after:2025-12-31 before:2026-08-01',
    "B. Clean-energy sovereignty": '("kedaulatan energi" OR "kemandirian energi" OR "swasembada energi" OR "ketahanan energi") AND ("transisi energi" OR "energi terbarukan" OR "energi bersih" OR dekarbonisasi OR elektrifikasi OR "pengurangan impor BBM" OR "pengurangan ketergantungan fosil") after:2025-12-31 before:2026-08-01',
    "C. Fossil-based sovereignty": '("kedaulatan energi" OR "kemandirian energi" OR "swasembada energi" OR "ketahanan energi") AND ("eksplorasi migas" OR "lifting minyak" OR "lifting gas" OR "hilirisasi batubara" OR "gasifikasi batubara" OR DME OR "pembangunan PLTU" OR "batu bara domestik") after:2025-12-31 before:2026-08-01',
    "D. Cost of delayed transition": '("transisi energi" OR "energi terbarukan" OR "pensiun dini PLTU" OR "pengurangan bahan bakar fosil") AND (biaya OR kerugian OR risiko OR "aset terdampar" OR subsidi OR fiskal OR impor OR "daya saing" OR investasi OR "lapangan kerja" OR kesehatan OR polusi OR iklim) after:2025-12-31 before:2026-08-01',
}

TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
                   "fbclid", "gclid", "ref", "share", "amp"}


# ---------------------------------------------------------------- DB helpers
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with get_conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            url TEXT,
            url_canonical TEXT,
            pub_date TEXT,
            source TEXT,
            doc_type TEXT,
            actor_group TEXT,
            search_method TEXT,
            query_used TEXT,
            added_by TEXT,
            added_at TEXT,
            status TEXT DEFAULT 'pending',        -- pending / included / excluded
            exclusion_reason TEXT,
            dup_of INTEGER,
            screen_notes TEXT
        );
        CREATE TABLE IF NOT EXISTS codings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id INTEGER NOT NULL REFERENCES documents(id),
            coder TEXT NOT NULL,
            coded_at TEXT,
            codebook_version TEXT,
            sov_frame TEXT,
            sov_presence TEXT,
            ketahanan_sense TEXT,
            cod_recognition TEXT,
            cod_direction TEXT,
            cost_types TEXT,                       -- JSON list
            evidence_excerpt TEXT,
            notes TEXT,
            is_double_coding INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, actor TEXT, action TEXT, detail TEXT
        );
        """)
        try:
            c.execute("ALTER TABLE documents ADD COLUMN auto_rule TEXT")
        except sqlite3.OperationalError:
            pass  # kolom sudah ada


def log_action(actor, action, detail=""):
    with get_conn() as c:
        c.execute("INSERT INTO audit_log (ts, actor, action, detail) VALUES (?,?,?,?)",
                  (datetime.now().isoformat(timespec="seconds"), actor, action, detail))


def canonical_url(u):
    if not u or not isinstance(u, str):
        return ""
    u = u.strip()
    try:
        p = urlparse(u.lower())
        q = [(k, v) for k, v in parse_qsl(p.query) if k not in TRACKING_PARAMS]
        netloc = p.netloc.replace("www.", "").replace("amp.", "")
        path = re.sub(r"/amp/?$", "", p.path.rstrip("/"))
        return urlunparse((p.scheme or "https", netloc, path, "", urlencode(q), ""))
    except Exception:
        return u


def norm_title(t):
    t = unicodedata.normalize("NFKD", str(t or "")).lower()
    return re.sub(r"[^a-z0-9 ]+", " ", t).strip()


# ------------------------------------------------- auto-classification rules
# Saran otomatis actor group + jenis dokumen dari URL/judul.
# Ini SARAN, bukan keputusan final — coder mengonfirmasi/override saat screening.
STAKEHOLDER_SUFFIXES = (".go.id", ".mil.id")
STAKEHOLDER_DOMAINS = {
    # BUMN & lembaga yang tidak memakai .go.id — tambahkan sesuai kebutuhan
    "pln.co.id", "pertamina.com", "ptba.co.id", "bukitasam.co.id",
    "mind.id", "pgn.co.id", "geodipa.co.id",
}
REGULATION_HINTS = re.compile(r"(jdih|peraturan|permen|perpres|kepmen|undang-undang|uu[-_ ]?no)", re.I)
OPED_SUBDOMAIN = re.compile(r"^(kolom|opini)\.")
OPED_PATH = re.compile(r"/(opini|kolom|op-?ed|gagasan|pendapat|analisis-opini|forum)(/|$)")
OPED_TITLE = re.compile(r"^\s*(opini|kolom)\s*[:|–—-]", re.I)
PR_TITLE = re.compile(r"(siaran pers|press release|keterangan pers|pernyataan resmi)", re.I)
IG_SOURCE = re.compile(r"instagram\s*[·•|\-]\s*([a-z0-9_.]+)", re.I)

# Akun Instagram yang sudah diverifikasi manual — tambahkan seiring ditemukan akun baru.
# Kunci huruf kecil, tanpa @. Akun yang TIDAK ada di daftar ini ditandai untuk
# klasifikasi manual, bukan otomatis dianggap Media — supaya tidak salah kategori diam-diam.
IG_STAKEHOLDER_GOV = {
    "kesdm", "djebtke", "bpsdm.esdm", "dewanenergi", "indonesiago.id",
    "ditjenperbendaharaan", "bappenasri", "dinas_esdm_prov_ntt",
    "djpbdkijakarta", "kppn.garut", "beacukaimagelang", "bpskotasurabaya",
}
IG_STAKEHOLDER_BUMN = {
    "plnip.ubppelabuhanratu", "plnip.ubptello", "bukitasamptba",
    "ptwijayakarya", "pgnlngindonesia", "mkiofficial_",
}
# Pejabat yang memposting dalam kapasitas resmi — dihitung "pernyataan pejabat" (Stakeholder).
IG_OFFICIAL_PERSON = {"bahlillahadalia"}
IG_MEDIA = {
    "cnbcindonesia", "detikfinanceofficial", "pikiranrakyat", "katadatagreen",
    "idx_channel", "goodstats.id", "surabayaraya.info", "rriprograma3",
}
# NGO, think tank, asosiasi industri, lembaga riset non-pemerintah — strata NGO-CSO/Ahli.
IG_NGO_CSO = {
    "cerah_indonesiaku", "greenpeaceid", "tifafoundation_id", "celios_id",
    "tukindonesia", "yayasankehati", "metiires", "aesi_id", "energianusantara",
}
# Akun IESR sendiri — masuk strata NGO-CSO/Ahli TAPI ditandai self-citation untuk
# diputuskan manual saat screening (default: pertimbangkan dikeluarkan dari sampel).
IG_SELF_ORG = {"iesr.id"}


def suggest_classification(url, title="", source=""):
    """Return (actor_group, doc_type, rule)."""
    p = urlparse(str(url or "").lower())
    host = p.netloc.replace("www.", "")
    path = p.path or ""
    t = str(title or "")
    src = str(source or "")

    def is_stakeholder_domain(h):
        if h.endswith(STAKEHOLDER_SUFFIXES):
            return True
        return any(h == d or h.endswith("." + d) for d in STAKEHOLDER_DOMAINS)

    if "instagram.com" in host:
        m = IG_SOURCE.search(src)
        handle = m.group(1).lower() if m else None
        if not handle:
            return None, None, "Instagram tanpa nama akun terbaca — cek manual siapa pemilik akun"
        if handle in IG_SELF_ORG:
            return "NGO-CSO/Ahli", "Pernyataan organisasi (NGO/CSO/asosiasi)", \
                f"AKUN IESR SENDIRI (@{handle}) — pertimbangkan keluarkan (self-citation), putuskan saat screening"
        if handle in IG_STAKEHOLDER_GOV or handle in IG_STAKEHOLDER_BUMN:
            return "Stakeholder", "Siaran pers / press release", f"akun Instagram institusi terverifikasi (@{handle})"
        if handle in IG_OFFICIAL_PERSON:
            return "Stakeholder", "Pernyataan pemerintah/parlemen/BUMN", f"akun pejabat dalam kapasitas resmi (@{handle})"
        if handle in IG_MEDIA:
            return "Media", "Berita", f"akun media terverifikasi (@{handle})"
        if handle in IG_NGO_CSO:
            return "NGO-CSO/Ahli", "Pernyataan organisasi (NGO/CSO/asosiasi)", f"akun NGO/CSO/asosiasi terverifikasi (@{handle})"
        return None, None, f"Instagram @{handle} belum ada di daftar terverifikasi — KLASIFIKASI MANUAL"

    if host and is_stakeholder_domain(host):
        if REGULATION_HINTS.search(host + " " + path + " " + t):
            return "Stakeholder", "Regulasi / dokumen kebijakan", f"domain institusi ({host}) + pola regulasi"
        return "Stakeholder", "Siaran pers / press release", f"domain institusi ({host})"
    if OPED_SUBDOMAIN.match(host) or OPED_PATH.search(path) or OPED_TITLE.search(t):
        return "Public", "Op-ed / kolom / opini", "section/subdomain/judul opini"
    if PR_TITLE.search(t):
        # PR yang dimuat ulang di situs media — biasanya tetap dihitung Stakeholder,
        # tapi wajib dicek: bisa juga berita yang MELIPUT siaran pers (→ Media).
        return "Stakeholder", "Siaran pers / press release", "judul menyebut siaran pers di domain media — KONFIRMASI"
    if host:
        return "Media", "Berita", f"default: domain media ({host})"
    return None, None, "URL kosong — klasifikasi manual"


def find_duplicates(title, url, exclude_id=None):
    """Return list of (id, title, reason) kandidat duplikat."""
    hits = []
    cu = canonical_url(url)
    nt = norm_title(title)
    with get_conn() as c:
        rows = c.execute("SELECT id, title, url_canonical FROM documents").fetchall()
    for rid, rtitle, rcanon in rows:
        if exclude_id and rid == exclude_id:
            continue
        if cu and rcanon and cu == rcanon:
            hits.append((rid, rtitle, "URL identik"))
        elif nt and SequenceMatcher(None, nt, norm_title(rtitle)).ratio() >= 0.90:
            hits.append((rid, rtitle, "Judul sangat mirip (≥0.90)"))
    return hits


def _empty(v):
    return v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() in ("", "nan", "Auto (dari URL/judul)")


def find_duplicate_groups():
    """Kelompokkan dokumen yang saling duplikat (URL kanonik sama, atau judul ≥0.90 mirip).
    Return list of groups; tiap group = list of row dicts, diurutkan id menaik (yang tertua dulu)."""
    with get_conn() as c:
        rows = c.execute("SELECT id, title, url, url_canonical, source, pub_date, status "
                         "FROM documents ORDER BY id").fetchall()
    docs = [dict(id=r[0], title=r[1], url=r[2], canon=r[3], source=r[4],
                 pub_date=r[5], status=r[6]) for r in rows]
    parent = {doc["id"]: doc["id"] for doc in docs}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    n = len(docs)
    for i in range(n):
        for j in range(i + 1, n):
            a, b = docs[i], docs[j]
            same_url = a["canon"] and b["canon"] and a["canon"] == b["canon"]
            same_title = (norm_title(a["title"]) and
                          SequenceMatcher(None, norm_title(a["title"]), norm_title(b["title"])).ratio() >= 0.90)
            if same_url or same_title:
                union(a["id"], b["id"])

    groups_map = {}
    by_id = {doc["id"]: doc for doc in docs}
    for doc in docs:
        root = find(doc["id"])
        groups_map.setdefault(root, []).append(doc)
    # hanya kelompok dengan >1 anggota = benar-benar ada duplikat
    return [sorted(g, key=lambda x: x["id"]) for g in groups_map.values() if len(g) > 1]


def delete_documents(ids, actor="system"):
    """Hapus dokumen berdasarkan id. Aman: hanya hapus dokumen yang BELUM dikoding
    (menjaga integritas — dokumen yang sudah punya coding tidak dihapus)."""
    if not ids:
        return 0, 0
    deleted, skipped = 0, 0
    with get_conn() as c:
        for did in ids:
            has_coding = c.execute("SELECT COUNT(*) FROM codings WHERE doc_id=?", (did,)).fetchone()[0]
            if has_coding:
                skipped += 1
                continue
            c.execute("DELETE FROM documents WHERE id=?", (did,))
            deleted += 1
    log_action(actor, "delete_duplicates", f"deleted={deleted} skipped_coded={skipped}")
    return deleted, skipped


def reset_all(actor="system"):
    """Kosongkan seluruh data (documents + codings + audit). Tidak bisa dibatalkan."""
    with get_conn() as c:
        c.execute("DELETE FROM codings")
        c.execute("DELETE FROM documents")
        c.execute("DELETE FROM audit_log")
    # catat setelah dikosongkan supaya jadi baris pertama riwayat baru
    log_action(actor, "RESET_ALL", "seluruh data dikosongkan")


def add_document(row, actor="system"):
    dups = find_duplicates(row.get("title"), row.get("url"))
    ag, dt, rule = row.get("actor_group"), row.get("doc_type"), None
    if _empty(ag) or _empty(dt):
        s_ag, s_dt, rule = suggest_classification(row.get("url"), row.get("title"), row.get("source"))
        if _empty(ag):
            ag = s_ag
        if _empty(dt):
            dt = s_dt
        rule = f"auto: {rule}"
    with get_conn() as c:
        cur = c.execute("""INSERT INTO documents
            (title,url,url_canonical,pub_date,source,doc_type,actor_group,
             search_method,query_used,added_by,added_at,status,dup_of,auto_rule)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row.get("title"), row.get("url"), canonical_url(row.get("url")),
             row.get("pub_date"), row.get("source"), dt, ag,
             row.get("search_method"), row.get("query_used"),
             actor, datetime.now().isoformat(timespec="seconds"),
             "pending", dups[0][0] if dups else None, rule))
        new_id = cur.lastrowid
    log_action(actor, "add_document", f"id={new_id} title={row.get('title','')[:80]}")
    return new_id, dups


def docs_df():
    with get_conn() as c:
        return pd.read_sql_query("SELECT * FROM documents", c)


def codings_df():
    with get_conn() as c:
        return pd.read_sql_query(
            "SELECT cd.*, d.title, d.actor_group, d.pub_date, d.source "
            "FROM codings cd JOIN documents d ON d.id = cd.doc_id", c)


# ---------------------------------------------------------------- statistics
def wilson_ci(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0, 0.0)
    p = k / n
    denom = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z**2 / (4 * n**2)) ** 0.5) / denom
    return p, max(0.0, centre - half), min(1.0, centre + half)


def krippendorff_alpha_nominal(units):
    """units: list of list-of-values per unit (≥2 nilai per unit dihitung).
    Implementasi nominal sederhana (bootstrap tidak disertakan)."""
    units = [[v for v in u if v is not None and v == v] for u in units]
    units = [u for u in units if len(u) >= 2]
    if not units:
        return None
    n_total = sum(len(u) for u in units)
    # observed disagreement
    Do_num = 0.0
    for u in units:
        m = len(u)
        pairs = m * (m - 1)
        if pairs == 0:
            continue
        disagree = sum(1 for a, b in combinations(u, 2) if a != b) * 2
        Do_num += disagree / (m - 1)
    Do = Do_num / n_total
    # expected disagreement
    from collections import Counter
    counts = Counter(v for u in units for v in u)
    if n_total <= 1:
        return None
    De = 1 - sum(c * (c - 1) for c in counts.values()) / (n_total * (n_total - 1))
    if De == 0:
        return 1.0
    return 1 - Do / De


def strata_distribution(df, var, categories, groups=None):
    """Distribusi per strata + equal-weighted P_c = rata-rata proporsi tiap strata di `groups`."""
    groups = groups or CORE_STRATA
    out = {}
    for g in groups:
        sub = df[df["actor_group"] == g]
        n = len(sub)
        out[g] = {"n": n, "props": {c: (sub[var] == c).sum() / n if n else 0.0 for c in categories}}
    out["Equal-weighted"] = {
        "n": sum(out[g]["n"] for g in groups),
        "props": {c: sum(out[g]["props"][c] for g in groups) / len(groups) for c in categories},
    }
    return out


def merge_ngo_into_public(df):
    """Versi alternatif: gabungkan strata NGO-CSO/Ahli ke dalam Public sebelum dihitung,
    lalu bobot setara 3 strata seperti skema semula (Public gabungan / Media / Stakeholder)."""
    df2 = df.copy()
    df2["actor_group"] = df2["actor_group"].replace({"NGO-CSO/Ahli": "Public"})
    return df2


# ---------------------------------------------------------------- GDELT
def gdelt_search(query, start, end, max_records=250):
    """GDELT DOC 2.0 API — supplementary discovery. Realistis untuk ±3 bulan terakhir.
    GDELT membatasi ±1 request/5 detik per IP → retry dengan jeda saat kena 429."""
    import time
    url = "https://api.gdeltproject.org/api/v2/doc/doc"
    params = {
        "query": query,
        "mode": "ArtList",
        "maxrecords": max_records,
        "format": "json",
        "startdatetime": start.strftime("%Y%m%d") + "000000",
        "enddatetime": end.strftime("%Y%m%d") + "235959",
        "sort": "DateDesc",
    }
    headers = {"User-Agent": "IESR-media-screening/1.0 (research)"}
    r = None
    for attempt in range(4):
        r = requests.get(url, params=params, headers=headers, timeout=60)
        if r.status_code == 429:
            time.sleep(15 * (attempt + 1))
            continue
        break
    r.raise_for_status()
    try:
        arts = r.json().get("articles", [])
    except Exception:
        arts = []
    rows = []
    for a in arts:
        rows.append({
            "title": a.get("title"),
            "url": a.get("url"),
            "pub_date": (a.get("seendate") or "")[:8],
            "source": a.get("domain"),
            "language": a.get("language"),
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df["pub_date"] = pd.to_datetime(df["pub_date"], format="%Y%m%d", errors="coerce").dt.date.astype(str)
    return df


# ================================================================ UI (guided)
st.set_page_config(page_title="Pemantau Framing Media", layout="centered")
init_db()

# ---- CSS ringan untuk merapikan tampilan ----
st.markdown("""
<style>
  .block-container {max-width: 900px; padding-top: 2rem;}
  div[data-testid="stMetricValue"] {font-size: 1.4rem;}
  .step-done {color: #1D9E75;}
  .doc-card {border: 1px solid #E0E0E0; border-radius: 12px; padding: 1rem 1.25rem; margin: .5rem 0;}
  .muted {color: #6b7280; font-size: 0.9rem;}
</style>
""", unsafe_allow_html=True)


def _stats():
    d = docs_df()
    cds = codings_df()
    total = len(d)
    pending = int((d["status"] == "pending").sum()) if total else 0
    included = int((d["status"] == "included").sum()) if total else 0
    coded = cds["doc_id"].nunique() if len(cds) else 0
    return d, cds, total, pending, included, coded


d, cds, total, pending, included, coded = _stats()

# ---- Sidebar: identitas + progres ringkas ----
with st.sidebar:
    st.markdown("### Siapa Anda?")
    coder_name = st.text_input("Nama Anda", key="coder_name",
                               placeholder="mis. Eva",
                               help="Dipakai untuk mencatat siapa mengerjakan apa")
    if not coder_name:
        st.info("Isi nama dulu untuk mulai.")
    st.divider()
    st.markdown("### Progres")
    if total == 0:
        st.caption("Belum ada dokumen. Mulai dari langkah 1.")
    else:
        st.progress(min(1.0, (included + (total - pending - included)) / total),
                    text=f"{total - pending}/{total} sudah disaring")
        if included:
            st.progress(min(1.0, coded / included),
                        text=f"{coded}/{included} sudah dibaca & ditandai")
    st.divider()
    st.caption(f"Periode: {PERIOD_START.strftime('%b %Y')}–{PERIOD_END.strftime('%b %Y')}")

# ---- Header + navigasi langkah (bukan tab penuh istilah) ----
st.title("Pemantau Framing Media Energi")

STEPS = {
    "1. Masukkan data": "import",
    "2. Saring dokumen": "screen",
    "3. Baca & tandai": "code",
    "4. Lihat hasil": "dash",
    "Lainnya (audit, keandalan)": "more",
}
labels = list(STEPS.keys())
# tandai langkah yang sudah ada isinya
if total: labels[0] += "  ✓"
if (total - pending) > 0: labels[1] += "  ✓"
if coded > 0: labels[2] += "  ✓"

choice = st.radio("Langkah", labels, horizontal=False, label_visibility="collapsed")
page = STEPS[choice.replace("  ✓", "")]
st.divider()

# ============================================================ 1. IMPORT
if page == "import":
    st.header("Langkah 1 — Masukkan data")
    st.markdown("Punya file hasil pencarian (CSV dari SerpApi/Google)? Upload di sini. "
                "Kolom jenis dokumen & kelompok aktor boleh kosong — sistem menebaknya otomatis, "
                "Anda tinggal cek di langkah 2.")

    if not coder_name:
        st.warning("Isi nama Anda di panel kiri dulu.")
    else:
        f = st.file_uploader("Pilih file CSV", type=["csv"])
        if f is not None:
            try:
                imp = pd.read_csv(f)
            except Exception:
                f.seek(0); imp = pd.read_csv(f, sep=";")
            st.success(f"File terbaca: {len(imp)} baris.")
            with st.expander("Lihat cuplikan data"):
                st.dataframe(imp.head(8), use_container_width=True)
            cols = list(imp.columns)

            def guess(name):
                g = next((c for c in cols if c.strip().lower().replace(" ", "_") == name), None)
                return (cols.index(g) + 1) if g in cols else 0

            with st.expander("Cocokkan kolom (biasanya sudah benar otomatis)", expanded=False):
                mapping = {}
                for tgt in ["title", "url", "pub_date", "source", "doc_type", "actor_group", "query_used"]:
                    mapping[tgt] = st.selectbox(tgt, ["(kosong)"] + cols, index=guess(tgt), key=f"map_{tgt}")
            method = st.selectbox("Sumber data ini dari mana?", SEARCH_METHODS, index=1)
            skip_existing = st.checkbox("Lewati dokumen yang link-nya sudah ada di sistem "
                                        "(cegah dobel kalau tak sengaja import 2×)", value=True)
            if st.button("Masukkan ke sistem", type="primary"):
                existing = set()
                if skip_existing:
                    with get_conn() as c:
                        existing = {row[0] for row in c.execute(
                            "SELECT url_canonical FROM documents WHERE url_canonical != ''").fetchall()}
                added, dup, skipped = 0, 0, 0
                for _, r in imp.iterrows():
                    row = {t: (r[mapping[t]] if mapping[t] != "(kosong)" else None) for t in mapping}
                    if skip_existing and canonical_url(row.get("url")) in existing:
                        skipped += 1
                        continue
                    row["search_method"] = method
                    _, dups = add_document(row, actor=coder_name)
                    added += 1; dup += 1 if dups else 0
                msg = f"{added} dokumen masuk."
                if skipped:
                    msg += f" {skipped} dilewati karena sudah ada di sistem."
                if dup:
                    msg += f" {dup} kemungkinan kembar dengan dokumen lain — cek di tab Bersihkan data."
                st.success(msg)
                if added:
                    st.balloons()

        st.divider()
        with st.expander("Atau tambah satu dokumen manual (mis. dari media langganan)"):
            with st.form("manual", clear_on_submit=True):
                mt = st.text_input("Judul")
                mu = st.text_input("Link (URL)")
                mdt = st.date_input("Tanggal terbit", value=None,
                                    min_value=date(2025, 1, 1), max_value=date(2026, 12, 31))
                msrc = st.text_input("Nama media / lembaga")
                if st.form_submit_button("Tambah") and mt and coder_name:
                    add_document({"title": mt, "url": mu, "pub_date": str(mdt) if mdt else None,
                                  "source": msrc, "doc_type": None, "actor_group": None,
                                  "query_used": "manual", "search_method": "Manual (lainnya)"}, actor=coder_name)
                    st.success("Ditambahkan.")

# ============================================================ 2. SCREEN
elif page == "screen":
    st.header("Langkah 2 — Saring dokumen")
    st.markdown("Untuk tiap dokumen: apakah layak masuk analisis? Cek juga apakah kelompok "
                "aktornya sudah benar (yang ditebak sistem).")

    if pending == 0 and total > 0:
        st.success("Semua dokumen sudah disaring. Lanjut ke langkah 3.")
    elif total == 0:
        st.info("Belum ada dokumen. Kembali ke langkah 1.")
    elif not coder_name:
        st.warning("Isi nama Anda di panel kiri dulu.")
    else:
        pend = d[d["status"] == "pending"].sort_values("id")
        st.caption(f"Sisa {len(pend)} dokumen untuk disaring.")
        r = pend.iloc[0]  # tangani satu per satu, paling atas
        st.markdown(f"#### {r['title']}")
        meta = f"{r['source'] or '—'} · {r['pub_date'] or 'tanggal?'}"
        st.markdown(f"<span class='muted'>{meta}</span>", unsafe_allow_html=True)
        if r["url"] and str(r["url"]) != "nan":
            st.markdown(f"[Buka artikel untuk dibaca →]({r['url']})")

        # peringatan otomatis
        warns = []
        try:
            pdd = pd.to_datetime(r["pub_date"]).date()
            if not (PERIOD_START <= pdd <= PERIOD_END):
                warns.append("Tanggal di luar periode Jan–Jul 2026")
        except Exception:
            warns.append("Tanggal kosong / tak jelas — cek manual saat buka artikel")
        if r["dup_of"] and not pd.isna(r["dup_of"]):
            warns.append(f"Mirip dengan dokumen #{int(r['dup_of'])} — hitung satu saja kecuali isinya beda")
        for w in warns:
            st.warning(w, icon="⚠️")

        st.markdown("**Kelompok aktor** (tebakan sistem — betulkan bila salah):")
        if r.get("auto_rule"):
            st.caption(f"↳ {r['auto_rule']}")
        c1, c2 = st.columns(2)
        ng = c1.selectbox("Kelompok", ACTOR_GROUPS,
                          index=ACTOR_GROUPS.index(r["actor_group"]) if r["actor_group"] in ACTOR_GROUPS else 1)
        nt = c2.selectbox("Jenis dokumen", DOC_TYPES,
                          index=DOC_TYPES.index(r["doc_type"]) if r["doc_type"] in DOC_TYPES else 0)

        st.markdown("**Layak masuk analisis?**")
        st.caption("Layak jika: terbit Jan–Jul 2026 · soal energi Indonesia · membahas kedaulatan "
                   "energi atau biaya penundaan secara serius (bukan sekadar menyebut).")
        b1, b2, _ = st.columns([1, 1, 2])
        if b1.button("✓ Ya, masukkan", type="primary"):
            with get_conn() as c:
                c.execute("UPDATE documents SET status='included', actor_group=?, doc_type=? WHERE id=?",
                          (ng, nt, int(r["id"])))
            log_action(coder_name, "include", f"id={r['id']} {ng}/{nt}")
            st.rerun()
        with b2.popover("✗ Tidak"):
            reason = st.selectbox("Alasan", EXCLUSION_REASONS, key=f"excl_{r['id']}")
            if st.button("Keluarkan", key=f"exb_{r['id']}"):
                with get_conn() as c:
                    c.execute("UPDATE documents SET status='excluded', exclusion_reason=? WHERE id=?",
                              (reason, int(r["id"])))
                log_action(coder_name, "exclude", f"id={r['id']} {reason}")
                st.rerun()

# ============================================================ 3. CODE
elif page == "code":
    st.header("Langkah 3 — Baca & tandai framing")
    inc = d[d["status"] == "included"]
    coded_ids = set(cds["doc_id"]) if len(cds) else set()
    todo = inc[~inc["id"].isin(coded_ids)].sort_values("id")

    if len(inc) == 0:
        st.info("Belum ada dokumen yang lolos saringan. Selesaikan langkah 2 dulu.")
    elif len(todo) == 0:
        st.success("Semua dokumen sudah ditandai. Lihat hasilnya di langkah 4.")
    elif not coder_name:
        st.warning("Isi nama Anda di panel kiri dulu.")
    else:
        st.caption(f"Sisa {len(todo)} dokumen untuk dibaca & ditandai.")
        r = todo.iloc[0]
        st.markdown(f"#### {r['title']}")
        st.markdown(f"<span class='muted'>{r['source'] or '—'} · {r['actor_group']} · {r['pub_date'] or '—'}</span>",
                    unsafe_allow_html=True)
        if r["url"] and str(r["url"]) != "nan":
            st.markdown(f"[Buka & baca artikel →]({r['url']})")
        st.divider()

        with st.form("code_form", clear_on_submit=True):
            st.markdown("**1. Soal kedaulatan energi — arah cerita ke mana?**")
            sf = st.radio("framing", SOV_FRAMES, label_visibility="collapsed",
                          captions=["Kedaulatan = energi bersih / kurangi fosil",
                                    "Kedaulatan = perkuat fosil domestik",
                                    "Dua-duanya / saling bertentangan",
                                    "Disebut tapi tak jelas arahnya"])
            sp = st.radio("Istilah kedaulatan/kemandirian disebut langsung?", SOV_PRESENCE,
                          horizontal=True,
                          captions=["Pakai kata itu", "Maknanya ada, katanya tidak", "Tidak dibahas"])
            with st.expander("Catatan khusus 'ketahanan energi' (bila istilah itu yang dipakai)"):
                ks = st.selectbox("Maksudnya", KETAHANAN_SENSE, label_visibility="collapsed")

            st.markdown("**2. Biaya kalau transisi ditunda — diakui atau tidak?**")
            cr = st.radio("recog", COD_RECOGNITION, horizontal=True, label_visibility="collapsed",
                          captions=["Disebut tegas", "Tersirat", "Tidak ada"])
            cdir = st.radio("Cerita soal biaya lebih menekankan apa?", COD_DIRECTION,
                            captions=["Rugi kalau transisi lambat",
                                      "Beban transisi (tarif, PHK, investasi)",
                                      "Dua-duanya", "Tak ada bahasan biaya"])
            ct = st.multiselect("Jenis biaya/risiko yang disebut (boleh lebih dari satu)", COST_TYPES)

            st.markdown("**3. Bukti**")
            ev = st.text_area("Kutipan pendek dari artikel (maks ~2 kalimat) sebagai bukti",
                              placeholder="Salin 1–2 kalimat kunci dari artikel...")
            note = st.text_input("Catatan (opsional) — bila ragu, tulis di sini untuk dibahas nanti")

            if st.form_submit_button("Simpan & lanjut ke berikutnya", type="primary"):
                if not ev.strip():
                    st.error("Kutipan bukti wajib diisi.")
                else:
                    with get_conn() as c:
                        c.execute("""INSERT INTO codings
                            (doc_id, coder, coded_at, codebook_version, sov_frame, sov_presence,
                             ketahanan_sense, cod_recognition, cod_direction, cost_types,
                             evidence_excerpt, notes, is_double_coding)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)""",
                            (int(r["id"]), coder_name, datetime.now().isoformat(timespec="seconds"),
                             CODEBOOK_VERSION, sf, sp, ks, cr, cdir, json.dumps(ct, ensure_ascii=False),
                             ev.strip(), note.strip()))
                    log_action(coder_name, "code", f"doc_id={r['id']}")
                    st.rerun()

# ============================================================ 4. DASHBOARD
elif page == "dash":
    st.header("Langkah 4 — Hasil")
    if len(cds) == 0:
        st.info("Belum ada dokumen yang ditandai. Kembali ke langkah 3.")
    else:
        primary = cds.sort_values("coded_at").groupby("doc_id", as_index=False).first()
        primary["month"] = pd.to_datetime(primary["pub_date"], errors="coerce").dt.to_period("M").astype(str)

        m1, m2, m3 = st.columns(3)
        m1.metric("Dokumen ditandai", len(primary))
        m2.metric("Kedaulatan disebut tegas", f"{(primary['sov_presence']=='Explicit').mean():.0%}")
        m3.metric("Akui biaya penundaan",
                  f"{(primary['cod_recognition'].isin(['Explicit','Implicit'])).mean():.0%}")

        st.markdown("#### Dokumen per bulan × kelompok aktor")
        pv = primary.pivot_table(index="month", columns="actor_group", values="doc_id",
                                 aggfunc="count", fill_value=0)
        st.bar_chart(pv)

        small = [g for g in EXTENDED_STRATA if (primary["actor_group"] == g).sum() < 30]
        if small:
            st.info(f"Kelompok dengan data < 30 dokumen: {', '.join(small)}. Angka gabungannya masih goyah — "
                    "sertakan jumlah per kelompok saat melapor.", icon="ℹ️")

        primary_merged = merge_ngo_into_public(primary)

        def two_schemes(var, cats, header):
            st.markdown(f"#### {header}")
            cA, cB = st.columns(2)
            with cA:
                st.caption("A — 4 kelompok setara")
                dA = strata_distribution(primary, var, cats, groups=EXTENDED_STRATA)
                rows = [{"Kategori": c, f"{g} (n={v['n']})": round(v["props"][c], 3)}
                        for g, v in dA.items() for c in cats]
                st.dataframe(pd.DataFrame([{**{"Kategori": c},
                              **{f"{g} (n={dA[g]['n']})": round(dA[g]['props'][c], 3) for g in dA}}
                              for c in cats]).set_index("Kategori"), use_container_width=True)
            with cB:
                st.caption("B — NGO digabung ke Publik (3 kelompok)")
                dB = strata_distribution(primary_merged, var, cats, groups=CORE_STRATA)
                st.dataframe(pd.DataFrame([{**{"Kategori": c},
                              **{f"{g} (n={dB[g]['n']})": round(dB[g]['props'][c], 3) for g in dB}}
                              for c in cats]).set_index("Kategori"), use_container_width=True)

        two_schemes("sov_frame", SOV_FRAMES, "Framing kedaulatan energi per kelompok")
        two_schemes("cod_direction", COD_DIRECTION, "Arah cerita biaya per kelompok")

        st.markdown("#### Jenis biaya paling sering disebut")
        costs = []
        for x in primary["cost_types"].dropna():
            try: costs += json.loads(x)
            except Exception: pass
        if costs:
            st.bar_chart(pd.Series(costs).value_counts())

        st.markdown("#### Proporsi kunci (dengan rentang ketidakpastian 95%)")
        rows = []
        for lab, mask in [("Kedaulatan disebut tegas", primary["sov_presence"] == "Explicit"),
                          ("Framing energi bersih", primary["sov_frame"] == SOV_FRAMES[0]),
                          ("Framing perluasan fosil", primary["sov_frame"] == SOV_FRAMES[1]),
                          ("Akui biaya penundaan", primary["cod_recognition"].isin(["Explicit", "Implicit"]))]:
            p, lo, hi = wilson_ci(int(mask.sum()), len(primary))
            rows.append({"Indikator": lab, "Proporsi": f"{p:.0%}", "Rentang 95%": f"{lo:.0%}–{hi:.0%}"})
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

# ============================================================ MORE (audit + reliability + export)
elif page == "more":
    st.header("Lainnya")
    tc, t1, t2, t3 = st.tabs(["🧹 Bersihkan data", "Keandalan antar-coder", "Ekspor data", "Riwayat (audit)"])

    with tc:
        st.markdown("### Cek & bersihkan duplikat")
        st.markdown("Kalau kamu tidak sengaja meng-import file yang sama dua kali, dokumennya jadi dobel. "
                    "Tombol di bawah mencari dokumen yang benar-benar kembar (link sama, atau judul nyaris sama) "
                    "dan menyisakan **satu** dari tiap kelompok.")

        dall = docs_df()
        st.caption(f"Saat ini ada {len(dall)} dokumen di sistem.")

        if st.button("🔍 Cari duplikat", key="scan_dup"):
            st.session_state["dup_groups"] = find_duplicate_groups()

        if "dup_groups" in st.session_state:
            groups = st.session_state["dup_groups"]
            if not groups:
                st.success("Tidak ada duplikat. Data bersih — langsung lanjut mengerjakan Langkah 2.")
            else:
                n_extra = sum(len(g) - 1 for g in groups)
                st.warning(f"Ditemukan {len(groups)} kelompok kembar, berisi {n_extra} dokumen berlebih "
                           f"yang bisa dihapus (menyisakan 1 per kelompok).")
                with st.expander(f"Lihat {len(groups)} kelompok duplikat"):
                    for gi, g in enumerate(groups[:40], 1):
                        st.markdown(f"**Kelompok {gi}** — menyimpan #{g[0]['id']}, menghapus "
                                    f"{', '.join('#'+str(x['id']) for x in g[1:])}")
                        for x in g:
                            keep = "✅ simpan" if x["id"] == g[0]["id"] else "🗑 hapus"
                            st.caption(f"{keep} · #{x['id']} · {str(x['title'])[:70]} · {x['source'] or '—'}")
                    if len(groups) > 40:
                        st.caption(f"...dan {len(groups)-40} kelompok lain.")

                st.info("Yang dihapus hanya salinan berlebih yang **belum kamu tandai** di Langkah 3. "
                        "Kalau ada salinan yang sudah terlanjur dikoding, itu tidak dihapus (biar hasil kerjamu aman).")
                if st.button("🗑 Hapus duplikat berlebih sekarang", type="primary", key="do_dedup",
                             disabled=not coder_name):
                    to_delete = [x["id"] for g in groups for x in g[1:]]
                    deleted, skipped = delete_documents(to_delete, actor=coder_name or "system")
                    msg = f"{deleted} duplikat dihapus."
                    if skipped:
                        msg += f" {skipped} tidak dihapus karena sudah dikoding."
                    st.success(msg)
                    del st.session_state["dup_groups"]
                    st.rerun()
                if not coder_name:
                    st.caption("Isi nama di panel kiri untuk mengaktifkan tombol hapus.")

        st.divider()
        st.markdown("### Mulai dari nol (reset)")
        st.markdown("Menghapus **semua** dokumen, hasil coding, dan riwayat. Pakai ini kalau mau import ulang "
                    "dari awal yang bersih. Tidak bisa dibatalkan — pastikan sudah ekspor/backup dulu bila perlu.")
        with st.expander("Buka opsi reset (hati-hati)"):
            confirm = st.text_input('Ketik persis kata **HAPUS** untuk mengonfirmasi', key="reset_confirm")
            if st.button("Reset semua data", type="secondary", key="do_reset",
                         disabled=(confirm != "HAPUS" or not coder_name)):
                reset_all(actor=coder_name or "system")
                st.session_state.pop("dup_groups", None)
                st.success("Semua data dikosongkan. Silakan mulai lagi dari Langkah 1.")
                st.rerun()
            if confirm != "HAPUS":
                st.caption("Tombol aktif setelah kamu mengetik HAPUS (huruf besar).")

    with t1:
        st.markdown("Untuk cek apakah dua coder menilai dokumen yang sama secara konsisten. "
                    "Butuh dokumen yang dinilai ≥2 orang.")
        multi = cds.groupby("doc_id").filter(lambda g: g["coder"].nunique() >= 2) if len(cds) else pd.DataFrame()
        if multi.empty:
            st.info("Belum ada dokumen yang dinilai 2 coder berbeda. Target: 15–20% dari sampel, "
                    "dinilai ulang oleh orang kedua secara independen.")
        else:
            res = []
            for var, lab in [("sov_frame", "Framing kedaulatan"), ("sov_presence", "Penyebutan kedaulatan"),
                             ("cod_recognition", "Pengakuan biaya"), ("cod_direction", "Arah biaya")]:
                units = [g[var].tolist() for _, g in multi.groupby("doc_id")]
                a = krippendorff_alpha_nominal(units)
                agree = sum(1 for u in units if len(set(u)) == 1) / len(units)
                verdict = "baik" if a and a >= 0.80 else "cukup" if a and a >= 0.667 else "perlu perbaikan"
                res.append({"Variabel": lab, "Skor α": f"{a:.2f}" if a is not None else "—",
                            "Sepakat penuh": f"{agree:.0%}", "Nilai": verdict})
            st.dataframe(pd.DataFrame(res), use_container_width=True, hide_index=True)

    with t2:
        d2 = docs_df(); c2 = codings_df()
        with get_conn() as c:
            audit = pd.read_sql_query("SELECT * FROM audit_log ORDER BY ts DESC", c)
        x, y, z = st.columns(3)
        x.download_button("⬇ Dokumen", d2.to_csv(index=False).encode("utf-8-sig"), "documents.csv")
        y.download_button("⬇ Hasil coding", c2.to_csv(index=False).encode("utf-8-sig"), "codings.csv")
        z.download_button("⬇ Riwayat", audit.to_csv(index=False).encode("utf-8-sig"), "audit_log.csv")
        st.caption("Backup penuh: salin file media_screening.db.")

    with t3:
        with get_conn() as c:
            audit = pd.read_sql_query("SELECT ts, actor, action, detail FROM audit_log ORDER BY ts DESC LIMIT 100", c)
        st.dataframe(audit, use_container_width=True, height=360, hide_index=True)
