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


# ---------------------------------------------------------------- UI
st.set_page_config(page_title="Media Screening — Energy Sovereignty", layout="wide")
init_db()

st.title("Media Screening: Energy Sovereignty & Cost of Delayed Transition")
st.caption(f"Periode analisis: {PERIOD_START} s.d. {PERIOD_END} · Codebook {CODEBOOK_VERSION}")

with st.sidebar:
    st.header("Identitas")
    coder_name = st.text_input("Nama coder / analis", key="coder_name",
                               help="Wajib diisi — masuk ke audit trail")
    st.divider()
    d = docs_df()
    st.metric("Total dokumen", len(d))
    st.metric("Included", int((d["status"] == "included").sum()) if len(d) else 0)
    st.metric("Pending screening", int((d["status"] == "pending").sum()) if len(d) else 0)
    cds = codings_df()
    st.metric("Dokumen sudah dikoding", cds["doc_id"].nunique() if len(cds) else 0)

tab_import, tab_screen, tab_code, tab_dash, tab_rel, tab_export = st.tabs(
    ["📥 Import & GDELT", "🔍 Screening", "✍️ Coding", "📊 Dashboard", "✅ Reliability", "📤 Export & Audit"])

# ================================================================ IMPORT
with tab_import:
    col1, col2 = st.columns(2)

    with col1:
        st.subheader("Import CSV (hasil Manual Google Search)")
        st.caption("Kolom minimal: title, url, pub_date (YYYY-MM-DD), source, doc_type, actor_group, query_used")
        f = st.file_uploader("Upload CSV", type=["csv"])
        method_csv = st.selectbox("Search method", SEARCH_METHODS, index=0)
        if f is not None:
            try:
                imp = pd.read_csv(f)
            except Exception:
                f.seek(0)
                imp = pd.read_csv(f, sep=";")
            st.dataframe(imp.head(10), use_container_width=True)
            # pemetaan kolom fleksibel
            cols = list(imp.columns)
            mapping = {}
            for target in ["title", "url", "pub_date", "source", "doc_type", "actor_group", "query_used"]:
                guess = next((c for c in cols if c.strip().lower().replace(" ", "_") == target), None)
                mapping[target] = st.selectbox(f"Kolom untuk `{target}`", ["(kosong)"] + cols,
                                               index=(cols.index(guess) + 1) if guess in cols else 0,
                                               key=f"map_{target}")
            if st.button("Import ke database", type="primary", disabled=not coder_name):
                added, dup_flagged = 0, 0
                for _, r in imp.iterrows():
                    row = {t: (r[mapping[t]] if mapping[t] != "(kosong)" else None) for t in mapping}
                    row["search_method"] = method_csv
                    _, dups = add_document(row, actor=coder_name)
                    added += 1
                    dup_flagged += 1 if dups else 0
                st.success(f"{added} dokumen diimpor; {dup_flagged} ditandai kandidat duplikat (cek tab Screening).")
        if not coder_name:
            st.info("Isi nama coder di sidebar dulu sebelum import.")

        st.divider()
        st.subheader("Entri manual (media berlangganan / dokumen tunggal)")
        with st.form("manual_entry", clear_on_submit=True):
            m_title = st.text_input("Judul")
            m_url = st.text_input("URL")
            m_date = st.date_input("Tanggal publikasi", value=None,
                                   min_value=date(2025, 1, 1), max_value=date(2026, 12, 31))
            m_source = st.text_input("Media / institusi")
            m_type = st.selectbox("Jenis dokumen", DOC_TYPES)
            m_group = st.selectbox("Actor group", ACTOR_GROUPS)
            m_query = st.text_input("Query / cara ditemukan")
            m_method = st.selectbox("Search method", SEARCH_METHODS, index=3)
            if st.form_submit_button("Tambah dokumen") and m_title and coder_name:
                _, dups = add_document({
                    "title": m_title, "url": m_url,
                    "pub_date": str(m_date) if m_date else None,
                    "source": m_source, "doc_type": m_type, "actor_group": m_group,
                    "query_used": m_query, "search_method": m_method}, actor=coder_name)
                if dups:
                    st.warning(f"Ditambahkan, tapi mirip dengan dokumen id={dups[0][0]} ({dups[0][2]}).")
                else:
                    st.success("Dokumen ditambahkan.")

    with col2:
        st.subheader("GDELT DOC API (supplementary)")
        st.caption("GDELT DOC 2.0 paling andal untuk ±3 bulan terakhir. Gunakan untuk melengkapi "
                   "akhir April–Juli 2026, bukan sumber tunggal Januari–Juli.")
        qname = st.selectbox("Query template", list(DEFAULT_QUERIES.keys()))
        gq = st.text_area("Query GDELT (bisa diedit)", value=DEFAULT_QUERIES[qname].split(" after:")[0],
                          height=120,
                          help="GDELT tidak memakai after:/before: — rentang tanggal diatur di bawah. "
                               "Tambahkan sourcelang:ind untuk membatasi bahasa Indonesia.")
        gc1, gc2 = st.columns(2)
        g_start = gc1.date_input("Dari", value=date(2026, 4, 20))
        g_end = gc2.date_input("Sampai", value=date(2026, 7, 20))
        g_max = st.slider("Max records", 50, 250, 250, 50)
        g_id_only = st.checkbox(
            "Batasi ke Indonesia (tambahkan sourcecountry:indonesia sourcelang:ind)", value=True,
            help="sourcecountry memfilter berdasarkan NEGARA OUTLET (kompas.com, antaranews.com, dst.), "
                 "sourcelang berdasarkan bahasa artikel. Nonaktifkan sourcelang jika ingin ikut menangkap "
                 "outlet Indonesia berbahasa Inggris (The Jakarta Post) — lihat tombol di bawah.")
        g_en_pass = st.checkbox(
            "Pass tambahan: outlet Indonesia berbahasa Inggris (sourcecountry:indonesia sourcelang:eng)",
            value=False)
        if st.button("Cari di GDELT"):
            try:
                queries_to_run = []
                if g_id_only:
                    queries_to_run.append(gq + " sourcecountry:indonesia sourcelang:ind")
                else:
                    queries_to_run.append(gq)
                if g_en_pass:
                    queries_to_run.append(gq + " sourcecountry:indonesia sourcelang:eng")
                parts = [gdelt_search(q, g_start, g_end, g_max) for q in queries_to_run]
                res = pd.concat([p for p in parts if not p.empty], ignore_index=True) \
                    if any(not p.empty for p in parts) else pd.DataFrame()
                if res.empty:
                    st.info("Tidak ada hasil.")
                else:
                    res = res.drop_duplicates(subset=["url"]).reset_index(drop=True)
                    st.session_state["gdelt_results"] = res
                    st.success(f"{len(res)} artikel ditemukan (setelah dedup URL).")
            except Exception as e:
                st.error(f"Gagal memanggil GDELT: {e}")
        if "gdelt_results" in st.session_state:
            res = st.session_state["gdelt_results"]
            st.dataframe(res, use_container_width=True, height=300)
            g_group = st.selectbox("Actor group untuk hasil ini",
                                   ["Auto (dari URL/judul)"] + ACTOR_GROUPS, index=0)
            g_type = st.selectbox("Jenis dokumen default",
                                  ["Auto (dari URL/judul)"] + DOC_TYPES, index=0)
            sel = st.multiselect("Pilih baris untuk diimpor (index)", res.index.tolist())
            if st.button("Import baris terpilih", disabled=not coder_name):
                n_dup = 0
                for i in sel:
                    r = res.loc[i]
                    _, dups = add_document({
                        "title": r["title"], "url": r["url"], "pub_date": r["pub_date"],
                        "source": r["source"], "doc_type": g_type, "actor_group": g_group,
                        "query_used": gq, "search_method": "GDELT DOC API"}, actor=coder_name)
                    n_dup += 1 if dups else 0
                st.success(f"{len(sel)} diimpor; {n_dup} kandidat duplikat ditandai.")

# ================================================================ SCREENING
with tab_screen:
    st.subheader("Screening: inklusi / eksklusi")
    d = docs_df()
    if d.empty:
        st.info("Belum ada dokumen.")
    else:
        fstatus = st.multiselect("Filter status", ["pending", "included", "excluded"], default=["pending"])
        view = d[d["status"].isin(fstatus)].copy()
        st.caption(f"{len(view)} dokumen. Kolom **dup_of** berisi id dokumen yang terdeteksi mirip.")
        st.dataframe(view[["id", "title", "source", "pub_date", "actor_group", "doc_type",
                           "auto_rule", "search_method", "status", "dup_of"]],
                     use_container_width=True, height=320)
        if len(view):
            sel_id = st.number_input("ID dokumen untuk di-screen", min_value=int(d["id"].min()),
                                     max_value=int(d["id"].max()), step=1)
            row = d[d["id"] == sel_id]
            if len(row):
                r = row.iloc[0]
                st.markdown(f"**{r['title']}**  \n{r['source'] or '-'} · {r['pub_date'] or '-'} · "
                            f"[{r['url']}]({r['url']})")
                if r.get("auto_rule"):
                    st.info(f"Klasifikasi otomatis → **{r['actor_group']} / {r['doc_type']}** "
                            f"({r['auto_rule']}). Konfirmasi atau override di bawah.")
                oc1, oc2, oc3 = st.columns([1, 2, 1])
                new_group = oc1.selectbox("Actor group", ACTOR_GROUPS,
                                          index=ACTOR_GROUPS.index(r["actor_group"])
                                          if r["actor_group"] in ACTOR_GROUPS else 1,
                                          key=f"ovr_g_{sel_id}")
                new_type = oc2.selectbox("Jenis dokumen", DOC_TYPES,
                                         index=DOC_TYPES.index(r["doc_type"])
                                         if r["doc_type"] in DOC_TYPES else 0,
                                         key=f"ovr_t_{sel_id}")
                if oc3.button("Simpan klasifikasi", disabled=not coder_name):
                    with get_conn() as c:
                        c.execute("UPDATE documents SET actor_group=?, doc_type=?, "
                                  "auto_rule=COALESCE(auto_rule,'') || ' | dikonfirmasi ' || ? WHERE id=?",
                                  (new_group, new_type, coder_name, int(sel_id)))
                    log_action(coder_name, "reclassify",
                               f"id={sel_id} → {new_group}/{new_type}")
                    st.success("Klasifikasi diperbarui."); st.rerun()
                # cek kriteria otomatis
                warns = []
                try:
                    pd_ = pd.to_datetime(r["pub_date"]).date()
                    if not (PERIOD_START <= pd_ <= PERIOD_END):
                        warns.append("Tanggal publikasi di luar Jan–Jul 2026")
                except Exception:
                    warns.append("Tanggal publikasi kosong / tidak valid")
                if r["dup_of"] and not pd.isna(r["dup_of"]):
                    warns.append(f"Kandidat duplikat dari dokumen id={int(r['dup_of'])} — "
                                 "hitung satu, kecuali framing editorial berbeda")
                for w in warns:
                    st.warning(w)
                cA, cB = st.columns(2)
                with cA:
                    st.markdown("**Checklist inklusi** — semua harus terpenuhi:")
                    ok1 = st.checkbox("Terbit 1 Jan – 31 Jul 2026")
                    ok2 = st.checkbox("Relevan dengan Indonesia / kebijakan energi Indonesia")
                    ok3 = st.checkbox("Membahas ≥1 objective secara substantif (bukan incidental)")
                    ok4 = st.checkbox("Open access / langganan resmi IESR, bisa diverifikasi")
                    note = st.text_input("Catatan screening", key="scr_note")
                    if st.button("✔ Include", type="primary",
                                 disabled=not (ok1 and ok2 and ok3 and ok4 and coder_name)):
                        with get_conn() as c:
                            c.execute("UPDATE documents SET status='included', screen_notes=? WHERE id=?",
                                      (note, int(sel_id)))
                        log_action(coder_name, "include", f"id={sel_id}")
                        st.success("Included."); st.rerun()
                with cB:
                    reason = st.selectbox("Alasan eksklusi", EXCLUSION_REASONS)
                    if st.button("✘ Exclude", disabled=not coder_name):
                        with get_conn() as c:
                            c.execute("UPDATE documents SET status='excluded', exclusion_reason=?, "
                                      "screen_notes=? WHERE id=?", (reason, note if 'note' in dir() else "", int(sel_id)))
                        log_action(coder_name, "exclude", f"id={sel_id} reason={reason}")
                        st.success("Excluded."); st.rerun()

# ================================================================ CODING
with tab_code:
    st.subheader("Coding dokumen (status: included)")
    d = docs_df()
    cds = codings_df()
    inc = d[d["status"] == "included"]
    if inc.empty:
        st.info("Belum ada dokumen berstatus included.")
    else:
        coded_ids = set(cds["doc_id"]) if len(cds) else set()
        mode = st.radio("Mode", ["Belum dikoding", "Double-coding (dokumen sudah dikoding coder lain)", "Semua"],
                        horizontal=True)
        if mode == "Belum dikoding":
            pool = inc[~inc["id"].isin(coded_ids)]
        elif mode.startswith("Double"):
            pool = inc[inc["id"].isin(coded_ids)]
        else:
            pool = inc
        st.dataframe(pool[["id", "title", "source", "pub_date", "actor_group"]],
                     use_container_width=True, height=240)
        if len(pool):
            cid = st.number_input("ID dokumen untuk dikoding", min_value=int(inc["id"].min()),
                                  max_value=int(inc["id"].max()), step=1, key="code_id")
            row = inc[inc["id"] == cid]
            if len(row):
                r = row.iloc[0]
                prev = cds[cds["doc_id"] == cid] if len(cds) else pd.DataFrame()
                st.markdown(f"**{r['title']}** — {r['actor_group']} · {r['source'] or '-'} · "
                            f"[{r['url']}]({r['url']})")
                if len(prev):
                    st.caption(f"Sudah dikoding oleh: {', '.join(prev['coder'].unique())} "
                               "(isi coding kamu secara independen — jangan lihat hasil mereka dulu).")
                with st.form("coding_form", clear_on_submit=True):
                    st.markdown("**Objective 1 — Energy sovereignty framing**")
                    sp = st.radio("Keberadaan konsep sovereignty", SOV_PRESENCE, horizontal=True)
                    sf = st.radio("Kategori framing", SOV_FRAMES)
                    ks = st.selectbox("Subkode 'ketahanan energi' (jika istilah ini yang dipakai)", KETAHANAN_SENSE)
                    st.markdown("**Objective 2 — Cost of delayed transition**")
                    cr = st.radio("Recognition", COD_RECOGNITION, horizontal=True)
                    cdir = st.radio("Direction of cost narrative", COD_DIRECTION)
                    ct = st.multiselect("Jenis biaya / risiko yang disebut", COST_TYPES)
                    ev = st.text_area("Evidence excerpt (kutipan pendek, maks ±2 kalimat)",
                                      help="Simpan kutipan pendek saja, bukan seluruh artikel.")
                    nt = st.text_area("Notes / kasus ambigu untuk dibahas di kalibrasi")
                    submitted = st.form_submit_button("Simpan coding", type="primary")
                    if submitted:
                        if not coder_name:
                            st.error("Isi nama coder di sidebar.")
                        elif not ev.strip():
                            st.error("Evidence excerpt wajib diisi (audit trail).")
                        else:
                            with get_conn() as c:
                                c.execute("""INSERT INTO codings
                                    (doc_id, coder, coded_at, codebook_version, sov_frame, sov_presence,
                                     ketahanan_sense, cod_recognition, cod_direction, cost_types,
                                     evidence_excerpt, notes, is_double_coding)
                                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                    (int(cid), coder_name, datetime.now().isoformat(timespec="seconds"),
                                     CODEBOOK_VERSION, sf, sp, ks, cr, cdir, json.dumps(ct, ensure_ascii=False),
                                     ev.strip(), nt.strip(), 1 if len(prev) else 0))
                            log_action(coder_name, "code", f"doc_id={cid}")
                            st.success("Coding tersimpan.")

# ================================================================ DASHBOARD
with tab_dash:
    st.subheader("Metrics")
    cds = codings_df()
    if cds.empty:
        st.info("Belum ada coding.")
    else:
        # satu baris per dokumen: pakai coding pertama (primary); double-coding hanya untuk reliability
        primary = cds.sort_values("coded_at").groupby("doc_id", as_index=False).first()
        primary["month"] = pd.to_datetime(primary["pub_date"], errors="coerce").dt.to_period("M").astype(str)

        c1, c2, c3 = st.columns(3)
        c1.metric("Dokumen dikoding", len(primary))
        c2.metric("Sovereignty explicit",
                  f"{(primary['sov_presence']=='Explicit').mean():.0%}")
        c3.metric("Mengenali cost of delay (expl+impl)",
                  f"{(primary['cod_recognition'].isin(['Explicit','Implicit'])).mean():.0%}")

        st.markdown("#### Jumlah dokumen per bulan × actor group")
        pv = primary.pivot_table(index="month", columns="actor_group", values="doc_id",
                                 aggfunc="count", fill_value=0)
        st.bar_chart(pv)

        small = [g for g in EXTENDED_STRATA if (primary["actor_group"] == g).sum() < 30]
        if small:
            st.warning(f"Strata dengan n < 30: {', '.join(small)}. Proporsi equal-weighted untuk strata "
                       "kecil sangat noisy — laporkan n per strata dan interval kepercayaan, jangan hanya angka gabungan.")

        st.caption("Dua skema bobot ditampilkan berdampingan: **(A) 4 strata setara** — Public, Media, "
                   "Stakeholder, NGO-CSO/Ahli masing-masing 1/4 — dan **(B) NGO-CSO digabung ke Public** "
                   "— kembali ke bobot 3 strata seperti desain semula, dengan NGO-CSO/Ahli dihitung sebagai "
                   "bagian dari suara publik/pakar.")
        primary_merged = merge_ngo_into_public(primary)

        def show_two_schemes(var, categories, header):
            st.markdown(f"#### {header}")
            cA, cB = st.columns(2)
            with cA:
                st.caption("Skema A — 4 strata setara")
                dA = strata_distribution(primary, var, categories, groups=EXTENDED_STRATA)
                rowsA = [{"Strata": f"{g} (n={v['n']})", "Kategori": cat, "Proporsi": round(p, 3)}
                        for g, v in dA.items() for cat, p in v["props"].items()]
                st.dataframe(pd.DataFrame(rowsA).pivot(index="Kategori", columns="Strata", values="Proporsi"),
                             use_container_width=True)
            with cB:
                st.caption("Skema B — NGO-CSO digabung ke Public (3 strata)")
                dB = strata_distribution(primary_merged, var, categories, groups=CORE_STRATA)
                rowsB = [{"Strata": f"{g} (n={v['n']})", "Kategori": cat, "Proporsi": round(p, 3)}
                        for g, v in dB.items() for cat, p in v["props"].items()]
                st.dataframe(pd.DataFrame(rowsB).pivot(index="Kategori", columns="Strata", values="Proporsi"),
                             use_container_width=True)

        show_two_schemes("sov_frame", SOV_FRAMES, "Objective 1 — distribusi framing sovereignty per strata")
        show_two_schemes("cod_direction", COD_DIRECTION, "Objective 2 — direction of cost narrative per strata")

        st.markdown("#### Jenis biaya paling sering disebut")
        all_costs = []
        for x in primary["cost_types"].dropna():
            try:
                all_costs += json.loads(x)
            except Exception:
                pass
        if all_costs:
            st.bar_chart(pd.Series(all_costs).value_counts())

        st.markdown("#### Proporsi kunci + Wilson 95% CI")
        key_rows = []
        for label, mask in [
            ("Sovereignty explicit", primary["sov_presence"] == "Explicit"),
            ("Clean-transition framing", primary["sov_frame"] == SOV_FRAMES[0]),
            ("Fossil-expansion framing", primary["sov_frame"] == SOV_FRAMES[1]),
            ("Cost-of-delay recognized (expl+impl)",
             primary["cod_recognition"].isin(["Explicit", "Implicit"])),
        ]:
            p, lo, hi = wilson_ci(int(mask.sum()), len(primary))
            key_rows.append({"Indikator": label, "k/n": f"{int(mask.sum())}/{len(primary)}",
                             "Proporsi": f"{p:.1%}", "95% CI": f"[{lo:.1%}, {hi:.1%}]"})
        st.dataframe(pd.DataFrame(key_rows), use_container_width=True, hide_index=True)

# ================================================================ RELIABILITY
with tab_rel:
    st.subheader("Intercoder reliability (dokumen dengan ≥2 coding)")
    cds = codings_df()
    multi = cds.groupby("doc_id").filter(lambda g: g["coder"].nunique() >= 2) if len(cds) else pd.DataFrame()
    if multi.empty:
        st.info("Belum ada dokumen yang di-double-code oleh ≥2 coder berbeda. "
                "Target pilot: 50–100 dokumen, double-code 15–20% terstratifikasi per actor group.")
    else:
        n_units = multi["doc_id"].nunique()
        st.caption(f"{n_units} dokumen double-coded.")
        results = []
        for var, label in [("sov_frame", "Sovereignty framing"),
                           ("sov_presence", "Sovereignty presence"),
                           ("cod_recognition", "Cost-of-delay recognition"),
                           ("cod_direction", "Cost direction")]:
            units = [g[var].tolist() for _, g in multi.groupby("doc_id")]
            alpha = krippendorff_alpha_nominal(units)
            agree = sum(1 for u in units if len(set(u)) == 1) / len(units)
            verdict = ("✅ baik (≥0.80)" if alpha is not None and alpha >= 0.80 else
                       "🟡 dapat diterima (0.667–0.80)" if alpha is not None and alpha >= 0.667 else
                       "🔴 revisi codebook (<0.667)")
            results.append({"Variabel": label, "Krippendorff α": f"{alpha:.3f}" if alpha is not None else "-",
                            "% full agreement": f"{agree:.0%}", "Interpretasi": verdict})
        st.dataframe(pd.DataFrame(results), use_container_width=True, hide_index=True)
        st.markdown("#### Dokumen dengan disagreement (bahas di sesi kalibrasi)")
        dis = []
        for doc_id, g in multi.groupby("doc_id"):
            for var in ["sov_frame", "cod_direction"]:
                if g[var].nunique() > 1:
                    dis.append({"doc_id": doc_id, "title": g["title"].iloc[0], "variabel": var,
                                "nilai": " | ".join(f"{c}: {v}" for c, v in zip(g["coder"], g[var]))})
        if dis:
            st.dataframe(pd.DataFrame(dis), use_container_width=True, hide_index=True)
        else:
            st.success("Tidak ada disagreement pada variabel utama.")

# ================================================================ EXPORT
with tab_export:
    st.subheader("Export & audit trail")
    d = docs_df(); cds = codings_df()
    with get_conn() as c:
        audit = pd.read_sql_query("SELECT * FROM audit_log ORDER BY ts DESC", c)
    colx, coly, colz = st.columns(3)
    colx.download_button("⬇ documents.csv", d.to_csv(index=False).encode("utf-8-sig"),
                         "documents.csv", "text/csv")
    coly.download_button("⬇ codings.csv", cds.to_csv(index=False).encode("utf-8-sig"),
                         "codings.csv", "text/csv")
    colz.download_button("⬇ audit_log.csv", audit.to_csv(index=False).encode("utf-8-sig"),
                         "audit_log.csv", "text/csv")
    st.dataframe(audit.head(200), use_container_width=True, height=300)
    st.caption("Backup penuh: salin file media_screening.db. Codings.csv bisa dianalisis lanjutan "
               "di notebook Colab (analysis_colab.ipynb).")
