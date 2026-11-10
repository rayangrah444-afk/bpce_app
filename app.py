# -*- coding: utf-8 -*-
"""
==============================================================================
 BPCE INTERNATIONAL STRATEGY HUB
 Application Streamlit « Stratégie & Développement International » — Groupe BPCE
==============================================================================

 Lancement :
     pip install -r requirements.txt
     streamlit run app.py

 requirements.txt
 ----------------
     streamlit>=1.50
     plotly>=5.20
     pandas>=2.0
     numpy>=1.26
     requests>=2.31
     yfinance>=0.2.50
     reportlab>=4.0
     # Optionnel : wbdata>=1.0  (wrapper Banque Mondiale ; l'application appelle
     # directement l'API REST v2 de la Banque Mondiale, que wbdata encapsule,
     # afin de maîtriser finement timeouts et bascule en mode secours.)

 Architecture
 ------------
     0. Charte graphique prune Groupe BPCE : thème Streamlit auto-installé
        (.streamlit/config.toml), CSS anti « ton sur ton », thème Plotly dédié
     1. Référentiels : zones, pays, univers de cibles M&A, profils de secours
     2. Couche données « fail-safe » (World Bank, FMI, FRED, BCE, Yahoo Finance)
        -> chaque source : try/except spécifiques + timeout + cache 1 h
        -> en cas d'échec : bascule automatique sur un jeu simulé crédible
     3. Moteurs métier : scoring risques, simulation M&A, génération PDF
     4. Interface : sidebar + 5 modules (onglets contextuels)

 Avertissement : les données « mode secours » sont des estimations simulées à
 visée de démonstration. Elles ne constituent ni un conseil en investissement
 ni une donnée officielle et doivent être validées avant toute décision.
==============================================================================
"""

from __future__ import annotations

import io
import math
import zlib
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from xml.sax.saxutils import escape

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

# Dépendances optionnelles : leur absence ne doit jamais faire planter l'app.
try:
    import yfinance as yf
except ImportError:  # pragma: no cover
    yf = None

try:
    from reportlab.graphics.charts.barcharts import VerticalBarChart
    from reportlab.graphics.shapes import Drawing, String
    from reportlab.lib import colors as rl_colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.platypus import (KeepTogether, PageBreak, Paragraph,
                                    SimpleDocTemplate, Spacer, Table,
                                    TableStyle)
    REPORTLAB_OK = True
except ImportError:  # pragma: no cover
    REPORTLAB_OK = False


# =============================================================================
# 0. CONFIGURATION GÉNÉRALE & CHARTE GRAPHIQUE
# =============================================================================

APP_NAME = "BPCE International Strategy Hub"

# Palette institutionnelle Groupe BPCE (tons prune / violet)
PRUNE = "#4A2845"          # couleur emblématique : éléments forts, onglets actifs, boutons
PRUNE_DARK = "#351C32"     # barre latérale, bandeaux
PRUNE_DEEP = "#2C1A2B"     # titres
ACCENT = "#8E3B82"         # accent secondaire (projections, séries comparées)
MAUVE = "#B48AB0"          # séries tertiaires
TINT = "#F3EEF2"           # fonds teintés prune très clairs
GOLD = "#B8862B"           # surbrillance « terminal » (usage parcimonieux)
INK = "#1F2421"            # texte courant
MUTED = "#55505A"          # texte secondaire (contraste AA sur blanc)
SLATE = "#64748B"
MIST = "#CBD5E1"
BG = "#F8F9FA"
BORDER = "#E2E8F0"
WHITE = "#FFFFFF"

RISK_COLORS = {"Faible": "#2F855A", "Modéré": "#B7791F",
               "Élevé": "#C05621", "Critique": "#B42318"}

HTTP_TIMEOUT = 6           # secondes par requête HTTP
YF_TIMEOUT = 15            # secondes max pour l'ensemble des appels Yahoo
CACHE_TTL = 3600           # 1 heure
YEARS = list(range(2015, 2027))
USER_AGENT = {"User-Agent": "BPCE-Strategy-Hub/1.0 (python-requests)"}

WB_BASE = "https://api.worldbank.org/v2"
IMF_BASE = "https://www.imf.org/external/datamapper/api/v1"
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"
ECB_BASE = "https://data-api.ecb.europa.eu/service/data"

st.set_page_config(
    page_title=f"{APP_NAME} — Groupe BPCE",
    page_icon="🏦",
    layout="wide",
    initial_sidebar_state="expanded",
)


THEME_TOML = """# Généré automatiquement par BPCE International Strategy Hub
[theme]
base = "light"
primaryColor = "#4A2845"
backgroundColor = "#F8F9FA"
secondaryBackgroundColor = "#F3EEF2"
textColor = "#1F2421"
borderColor = "#E2E8F0"
linkColor = "#8E3B82"
font = "Inter, 'Segoe UI', Roboto, sans-serif"
baseRadius = "8px"
dataframeHeaderBackgroundColor = "#F3EEF2"
dataframeBorderColor = "#E2E8F0"
chartCategoricalColors = ["#4A2845", "#8E3B82", "#B48AB0", "#64748B", "#B8862B"]

[theme.sidebar]
backgroundColor = "#351C32"
secondaryBackgroundColor = "#4A2845"
textColor = "#FFFFFF"
borderColor = "#5E3A58"
"""


def ensure_theme_config() -> bool:
    """Installe le thème BPCE dans `.streamlit/config.toml` (s'il est absent).

    Pourquoi : en mode sombre (OS ou navigateur), Streamlit passe ses widgets en
    texte clair ; combiné à un fond clair, cela crée du « ton sur ton ». Le thème
    `base = "light"` force un rendu clair cohérent, y compris pour les éléments
    dessinés en canvas (tableaux) que le CSS ne peut pas atteindre. Streamlit
    recharge ce fichier à chaud : aucun redémarrage n'est nécessaire.
    Un fichier existant n'est jamais écrasé.
    """
    path = Path.cwd() / ".streamlit" / "config.toml"
    if path.exists():
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(THEME_TOML, encoding="utf-8")
        return True
    except OSError:
        return False


def inject_css() -> None:
    """Feuille de style « terminal bancaire privé » — charte prune Groupe BPCE.

    Le CSS force explicitement couleurs de fond ET de texte de chaque composant :
    le rendu reste lisible quel que soit le thème (clair/sombre) du poste client.
    """
    st.markdown(
        f"""
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap');

        /* ================= Base & typographie ================= */
        html, body, .stApp, button, input, select, textarea {{
            font-family: 'Inter', 'Segoe UI', 'Roboto', sans-serif;
        }}
        .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"] {{
            background: {BG} !important; color: {INK} !important;
        }}
        [data-testid="stHeader"] {{ background: transparent !important; height: 2.6rem; }}
        [data-testid="stHeader"] * {{ color: {PRUNE_DEEP} !important; }}
        .block-container {{ padding-top: 3.2rem; padding-bottom: 3rem; max-width: 1520px; }}

        [data-testid="stMain"] h1, [data-testid="stMain"] h2, [data-testid="stMain"] h3,
        [data-testid="stMain"] h4, [data-testid="stMain"] h5 {{
            color: {PRUNE_DEEP} !important; font-weight: 700; letter-spacing: -0.01em;
        }}
        [data-testid="stMain"] p, [data-testid="stMain"] li, [data-testid="stMain"] label,
        [data-testid="stMain"] [data-testid="stMarkdownContainer"],
        [data-testid="stMain"] [data-testid="stWidgetLabel"] *,
        [data-testid="stMain"] td, [data-testid="stMain"] th {{
            color: {INK};
        }}
        [data-testid="stMain"] [data-testid="stWidgetLabel"] p {{
            color: {PRUNE_DEEP} !important; font-weight: 600; font-size: .86rem;
        }}
        [data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] * {{ color: {MUTED} !important; }}
        [data-testid="stMain"] table {{ border-collapse: collapse; background: {WHITE}; }}
        [data-testid="stMain"] th {{ background: {TINT} !important; color: {PRUNE_DEEP} !important;
                                      border: 1px solid {BORDER} !important; }}
        [data-testid="stMain"] td {{ border: 1px solid {BORDER} !important; color: {INK} !important; }}

        /* ================= Champs de saisie (zone principale) ================= */
        [data-testid="stMain"] [data-baseweb="select"] > div,
        [data-testid="stMain"] [data-baseweb="input"],
        [data-testid="stMain"] [data-baseweb="base-input"],
        [data-testid="stMain"] input, [data-testid="stMain"] textarea {{
            background: {WHITE} !important; color: {INK} !important;
            border-color: {BORDER} !important; -webkit-text-fill-color: {INK} !important;
        }}
        [data-testid="stMain"] [data-baseweb="select"] * {{ color: {INK}; }}
        [data-testid="stMain"] [data-baseweb="select"] svg {{ fill: {PRUNE} !important; }}
        [data-testid="stMain"] input::placeholder {{ color: #8A8490 !important; -webkit-text-fill-color: #8A8490 !important; }}
        [data-testid="stNumberInput"] button {{ background: {TINT} !important; color: {PRUNE} !important; border: none !important; }}
        [data-testid="stNumberInput"] button:hover {{ background: {PRUNE} !important; color: {WHITE} !important; }}
        [data-baseweb="tag"] {{ background: {PRUNE} !important; }}
        [data-baseweb="tag"] * {{ color: {WHITE} !important; }}
        [data-testid="stSliderThumbValue"] p, [data-testid="stSliderThumbValue"] {{ color: {PRUNE} !important; font-weight: 700; }}
        [data-testid="stSliderTickBar"] p {{ color: {MUTED} !important; }}
        [data-testid="stMain"] [data-testid="stCheckbox"] p,
        [data-testid="stMain"] [data-testid="stRadioOption"] p {{ color: {INK} !important; }}

        /* Listes déroulantes (rendues hors du conteneur principal) */
        [data-baseweb="popover"] ul, [data-baseweb="popover"] [role="listbox"],
        [data-baseweb="menu"] {{ background: {WHITE} !important; border: 1px solid {BORDER}; }}
        [data-baseweb="popover"] li, [data-baseweb="popover"] [role="option"],
        [data-baseweb="popover"] li * {{ color: {INK} !important; background: {WHITE}; }}
        [data-baseweb="popover"] li:hover, [data-baseweb="popover"] [aria-selected="true"] {{
            background: {TINT} !important; }}

        /* Expanders, alertes, toasts, progress */
        [data-testid="stMain"] [data-testid="stExpander"] details {{
            background: {WHITE} !important; border: 1px solid {BORDER} !important; border-radius: 10px;
        }}
        [data-testid="stMain"] [data-testid="stExpander"] summary,
        [data-testid="stMain"] [data-testid="stExpander"] summary * {{ color: {PRUNE_DEEP} !important; font-weight: 600; }}
        [data-testid="stAlertContainer"] {{ background: {TINT} !important; border-left: 4px solid {PRUNE};
                                            color: {INK} !important; }}
        [data-testid="stAlertContainer"] * {{ color: {INK} !important; }}
        [data-testid="stToast"] {{ background: {WHITE} !important; border: 1px solid {BORDER};
                                   box-shadow: 0 8px 24px rgba(53,28,50,.18); }}
        [data-testid="stToast"] * {{ color: {INK} !important; }}
        [data-testid="stProgress"] p {{ color: {PRUNE_DEEP} !important; font-weight: 600; }}
        [data-testid="stDataFrame"], [data-testid="stTable"] {{
            border: 1px solid {BORDER}; border-radius: 10px; overflow: hidden; background: {WHITE};
            box-shadow: 0 1px 3px rgba(53,28,50,.06);
        }}

        /* ================= Boutons ================= */
        .stButton > button, .stDownloadButton > button {{
            background: {WHITE} !important; color: {PRUNE} !important; border: 1px solid {PRUNE} !important;
            font-weight: 600; border-radius: 8px; transition: all .18s ease;
        }}
        .stButton > button:hover, .stDownloadButton > button:hover {{
            background: {TINT} !important; transform: translateY(-1px);
            box-shadow: 0 6px 16px rgba(74,40,69,.18);
        }}
        .stButton > button[kind="primary"], .stDownloadButton > button {{
            background: linear-gradient(135deg, {PRUNE} 0%, {PRUNE_DARK} 100%) !important;
            color: {WHITE} !important; border: none !important;
        }}
        .stButton > button[kind="primary"] *, .stDownloadButton > button * {{ color: {WHITE} !important; }}
        .stButton > button[kind="primary"]:hover, .stDownloadButton > button:hover {{
            background: linear-gradient(135deg, {ACCENT} 0%, {PRUNE} 100%) !important;
            box-shadow: 0 8px 20px rgba(74,40,69,.35);
        }}

        /* ================= Onglets ================= */
        .stTabs [data-baseweb="tab-list"] {{ gap: 4px; border-bottom: 1px solid {BORDER}; }}
        .stTabs button[data-baseweb="tab"] {{
            color: {MUTED} !important; font-weight: 600; padding: 8px 18px !important;
            border-radius: 8px 8px 0 0; background: transparent; transition: all .18s;
        }}
        .stTabs [data-baseweb="tab"] *, .stTabs [data-baseweb="tab"] [data-testid="stMarkdownContainer"] {{
            color: inherit !important; }}
        .stTabs [data-baseweb="tab"]:hover {{ background: {TINT}; color: {PRUNE} !important; }}
        .stTabs [aria-selected="true"], .stTabs [aria-selected="true"] * {{
            color: {WHITE} !important; background: {PRUNE} !important; }}
        .stTabs [data-baseweb="tab-highlight"] {{ background-color: {PRUNE} !important; height: 3px; }}
        .stTabs [data-baseweb="tab-border"] {{ background-color: {BORDER} !important; }}

        /* ================= Barre latérale prune ================= */
        [data-testid="stSidebar"] {{
            background: linear-gradient(180deg, {PRUNE_DARK} 0%, #24121F 100%) !important;
            border-right: 1px solid #5E3A58;
        }}
        [data-testid="stSidebar"] * {{ color: {WHITE} !important; }}
        [data-testid="stSidebar"] [data-baseweb="select"] > div {{
            background: {PRUNE} !important; border: 1px solid #6E4A68 !important; border-radius: 8px;
        }}
        [data-testid="stSidebar"] [data-baseweb="select"] svg {{ fill: {WHITE} !important; }}
        [data-testid="stSidebar"] [data-testid="stWidgetLabel"] p {{
            font-size: .72rem; text-transform: uppercase; letter-spacing: .08em; font-weight: 600;
            color: #E9DDE7 !important;
        }}
        [data-testid="stSidebar"] [data-testid="stRadioOption"] {{
            padding: 7px 10px; border-radius: 8px; margin-bottom: 2px; transition: background .15s;
        }}
        [data-testid="stSidebar"] [data-testid="stRadioOption"]:hover {{ background: rgba(255,255,255,.08); }}
        [data-testid="stSidebar"] [data-testid="stRadioOption"][data-selected="true"] {{
            background: rgba(255,255,255,.14); box-shadow: inset 3px 0 0 {MAUVE};
        }}
        [data-testid="stSidebar"] [data-testid="stExpander"] details {{
            background: rgba(255,255,255,.05) !important; border: 1px solid rgba(255,255,255,.16) !important;
            border-radius: 10px;
        }}
        [data-testid="stSidebar"] .stButton > button, [data-testid="stSidebar"] .stDownloadButton > button {{
            background: rgba(255,255,255,.08) !important; border: 1px solid rgba(255,255,255,.3) !important;
        }}
        [data-testid="stSidebar"] .stButton > button:hover,
        [data-testid="stSidebar"] .stDownloadButton > button:hover {{ background: rgba(255,255,255,.16) !important; }}
        [data-testid="stSidebar"] hr {{ border-color: rgba(255,255,255,.14) !important; }}
        [data-testid="stSidebarCollapseButton"] svg, [data-testid="stSidebarHeader"] svg {{ fill: {WHITE} !important; }}

        .brand {{ padding: 6px 2px 14px 2px; border-bottom: 1px solid rgba(255,255,255,.14); margin-bottom: 14px; }}
        .brand .mark {{ display:inline-flex; align-items:center; justify-content:center; width:38px; height:38px;
                        border-radius:9px; background:{WHITE}; color:{PRUNE} !important; font-weight:800;
                        font-size:.66rem; letter-spacing:0; margin-right:10px; flex-shrink:0; }}
        .brand .mark * {{ color:{PRUNE} !important; }}
        .brand .row {{ display:flex; align-items:center; }}
        .brand .t1 {{ font-size:.68rem; letter-spacing:.2em; text-transform:uppercase; color:#E3CFE0 !important; font-weight:700; }}
        .brand .t2 {{ font-size:1.02rem; font-weight:800; line-height:1.15; }}
        .side-h {{ font-size:.7rem; letter-spacing:.16em; text-transform:uppercase; font-weight:700;
                   color:#D9C2D5 !important; margin: 8px 0 6px 0; }}
        .api-row {{ display:flex; justify-content:space-between; align-items:center; font-size:.8rem; padding:4px 0;
                    border-bottom: 1px dashed rgba(255,255,255,.1); }}
        .dot {{ display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:7px; }}

        /* ================= Bandeau de module & ruban « terminal » ================= */
        .hero {{
            background: {PRUNE_DARK}; border-radius: 12px 12px 0 0; padding: 20px 26px 18px 26px;
            position: relative; overflow: hidden; border: 1px solid #4E2C49; border-bottom: none;
        }}
        .hero:before {{ content:""; position:absolute; left:0; top:0; bottom:0; width:5px; background:{MAUVE}; }}
        .hero:after {{ content:""; position:absolute; right:-80px; top:-90px; width:260px; height:260px;
                       border-radius:50%; background: radial-gradient(circle, rgba(180,138,176,.28), transparent 70%); }}
        .hero .kicker {{ font-size:.7rem; letter-spacing:.22em; text-transform:uppercase; color:#E3CFE0 !important; font-weight:700; }}
        .hero .title {{ font-size:1.6rem; font-weight:800; margin: 4px 0 2px 0; color:{WHITE} !important; }}
        .hero .sub {{ font-size:.88rem; color:#E9DDE7 !important; }}
        .tape {{
            display:flex; flex-wrap:wrap; gap:0; background:#24121F; border-radius: 0 0 12px 12px;
            border: 1px solid #4E2C49; border-top: 1px solid #5E3A58; margin-bottom: 16px;
        }}
        .tape .cell {{ padding: 9px 18px; border-right: 1px solid #4E2C49; }}
        .tape .lbl {{ font-size:.62rem; letter-spacing:.14em; text-transform:uppercase; color:#C9B2C5 !important; font-weight:600; }}
        .tape .val {{ font-family:'JetBrains Mono', ui-monospace, monospace; font-size:.95rem; font-weight:700; color:{WHITE} !important; }}
        .tape .val.up {{ color:#7EE2A8 !important; }}
        .tape .val.down {{ color:#FF9C8F !important; }}
        .tape .val.hl {{ color:#F2C46D !important; }}

        /* ================= Cartes KPI ================= */
        .kpi-card {{
            background: {WHITE}; border-radius: 10px; padding: 15px 17px 13px 17px;
            border: 1px solid {BORDER}; border-top: 3px solid {PRUNE};
            box-shadow: 0 1px 3px rgba(53,28,50,.06), 0 4px 14px rgba(53,28,50,.04);
            transition: transform .2s ease, box-shadow .2s ease, border-color .2s;
            height: 100%; min-height: 122px;
        }}
        .kpi-card:hover {{ transform: translateY(-3px); border-color: #D6C6D3;
                           box-shadow: 0 12px 28px rgba(53,28,50,.12); }}
        .kpi-card.alt {{ border-top-color: {MAUVE}; }}
        .kpi-label {{ font-size: .68rem; color: {MUTED} !important; text-transform: uppercase;
                      letter-spacing: .1em; font-weight: 700; }}
        .kpi-value {{ font-size: 1.36rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: {PRUNE_DEEP} !important; font-weight: 800; margin-top: 5px;
                      line-height: 1.15; font-variant-numeric: tabular-nums; }}
        .kpi-sub {{ font-size: .75rem; color: {MUTED} !important; margin-top: 7px; line-height: 1.35; }}
        .kpi-delta.up {{ color: #2F855A !important; font-weight: 700; }}
        .kpi-delta.down {{ color: #B42318 !important; font-weight: 700; }}

        /* ================= Panneaux, badges, PESTEL ================= */
        .panel {{ background:{WHITE}; border-radius:10px; padding:16px 18px; border:1px solid {BORDER};
                  box-shadow: 0 1px 3px rgba(53,28,50,.05); margin-bottom: 12px; color:{INK}; }}
        .panel * {{ color: inherit; }}
        .section-title {{ font-size:.8rem; font-weight:800; color:{PRUNE_DEEP} !important; text-transform: uppercase;
                          letter-spacing:.12em; border-left:3px solid {PRUNE}; padding-left:10px; margin: 10px 0 12px 0; }}
        .sep {{ height:1px; background: linear-gradient(90deg, {PRUNE} 0%, {BORDER} 25%, transparent 100%);
                margin: 18px 0; border:none; }}
        .src-badge {{ display:inline-block; font-size:.7rem; font-weight:700; padding:3px 10px;
                      border-radius:4px; margin: 0 6px 8px 0; letter-spacing:.02em; }}
        .src-badge.live {{ background:#E8F5EE; color:#1F6B45 !important; border:1px solid #B9DFC9; }}
        .src-badge.partial {{ background:#FDF4E3; color:#7A5410 !important; border:1px solid #EBD3A1; }}
        .src-badge.fallback {{ background:{TINT}; color:{PRUNE} !important; border:1px solid #D9C6D5; }}
        .pestel {{ background:{WHITE}; border-radius:10px; padding:14px; height:100%; border:1px solid {BORDER};
                   border-top:3px solid {PRUNE}; transition: transform .2s, box-shadow .2s; }}
        .pestel:hover {{ transform: translateY(-2px); box-shadow: 0 10px 22px rgba(53,28,50,.10); }}
        .pestel .letter {{ font-size:1.5rem; font-weight:800; color:{PRUNE} !important; line-height:1; }}
        .pestel .name {{ font-size:.72rem; font-weight:800; color:{PRUNE_DEEP} !important; text-transform:uppercase; letter-spacing:.08em; margin-top:2px; }}
        .pestel .txt {{ font-size:.79rem; color:{INK} !important; margin-top:6px; line-height:1.4; }}
        .stars {{ color:{GOLD} !important; letter-spacing:2px; font-size:.9rem; margin-top:4px; }}
        .level-pill {{ display:inline-block; padding:2px 10px; border-radius:4px; color:{WHITE} !important;
                       font-size:.72rem; font-weight:700; letter-spacing:.03em; }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def style_fig(fig: go.Figure, height: int = 420, title: str | None = None) -> go.Figure:
    """Applique le thème Plotly « BPCE Pro » à une figure."""
    fig.update_layout(
        template="plotly_white",
        height=height,
        title=dict(text=title, font=dict(size=14, color=PRUNE_DEEP), x=0.01) if title else None,
        font=dict(family="Inter, Segoe UI, Roboto, sans-serif", size=12, color=INK),
        paper_bgcolor="#FFFFFF",
        plot_bgcolor="#FFFFFF",
        margin=dict(l=40, r=24, t=56 if title else 24, b=70),
        colorway=[PRUNE, ACCENT, MAUVE, SLATE, GOLD, MIST],
        legend=dict(orientation="h", yanchor="top", y=-0.12, xanchor="left", x=0,
                    bgcolor="rgba(0,0,0,0)", font=dict(size=11, color=INK)),
        hoverlabel=dict(bgcolor="#FFFFFF", font_size=12, bordercolor=PRUNE),
    )
    fig.update_xaxes(showgrid=False, linecolor=BORDER, tickfont=dict(color=MUTED))
    fig.update_yaxes(gridcolor="#EEF1F4", zerolinecolor=BORDER, tickfont=dict(color=MUTED),
                     title_font=dict(color=MUTED))
    return fig


PLOTLY_CONFIG = {"displaylogo": False, "modeBarButtonsToRemove": ["lasso2d", "select2d"]}


# =============================================================================
# 1. RÉFÉRENTIELS : ZONES, PAYS, PROFILS DE SECOURS, UNIVERS M&A
# =============================================================================

REGIONS: dict[str, list[str]] = {
    "Europe de l'Ouest / Benelux": ["FRA", "LUX", "BEL", "NLD", "DEU"],
    "Afrique de l'Ouest (UEMOA)": ["SEN", "CIV", "BEN", "TGO", "BFA"],
    "USMCA (Amérique du Nord)": ["USA", "CAN", "MEX"],
    "LATAM (Amérique Latine)": ["BRA", "CHL", "COL", "ARG", "PER"],
    "Asie de l'Est / ASEAN": ["SGP", "JPN", "KOR", "IDN", "VNM", "MYS"],
    "Moyen-Orient / MENA": ["ARE", "SAU", "QAT", "MAR", "EGY"],
}

# Profils pays de secours (valeurs indicatives ~2024, à des fins de simulation).
# name, iso2, devise, émergent, PIB Md$, croissance %, inflation %, chômage %,
# balance commerciale %PIB, WGI stabilité politique, WGI contrôle corruption,
# spread souverain estimé (pb), volatilité change vs EUR (%), base KYC (0-100),
# taux d'IS (%), exposition climatique (1-5), maturité digitale (1-5), superviseur
_PROFILE_FIELDS = ["name", "iso2", "currency", "em", "gdp", "g", "inf", "un", "tb",
                   "pv", "cc", "spread", "fxvol", "kyc", "tax", "climate", "digital",
                   "supervisor"]
_PROFILES_RAW = {
    "FRA": ("France", "FR", "EUR", False, 3160, 1.1, 2.0, 7.4, -1.5, 0.30, 1.10, 75, 0, 20, 25.0, 2, 4, "BCE (MSU) / ACPR"),
    "LUX": ("Luxembourg", "LU", "EUR", False, 93, 1.5, 2.2, 6.0, 30.0, 1.30, 1.90, 15, 0, 40, 24.9, 1, 5, "BCE (MSU) / CSSF"),
    "BEL": ("Belgique", "BE", "EUR", False, 660, 1.2, 3.0, 5.7, 0.5, 0.50, 1.40, 55, 0, 20, 25.0, 2, 4, "BCE (MSU) / BNB"),
    "NLD": ("Pays-Bas", "NL", "EUR", False, 1230, 1.0, 3.2, 3.7, 9.0, 0.80, 1.80, 20, 0, 25, 25.8, 3, 5, "BCE (MSU) / DNB"),
    "DEU": ("Allemagne", "DE", "EUR", False, 4660, 0.0, 2.4, 3.4, 4.5, 0.60, 1.80, 0, 0, 20, 30.0, 2, 4, "BCE (MSU) / BaFin"),
    "SEN": ("Sénégal", "SN", "XOF", True, 32, 6.0, 0.8, 3.0, -15.0, -0.20, -0.10, 700, 0.5, 55, 30.0, 4, 3, "Commission Bancaire UMOA / BCEAO"),
    "CIV": ("Côte d'Ivoire", "CI", "XOF", True, 87, 6.5, 3.5, 2.4, -2.0, -0.60, -0.40, 350, 0.5, 60, 25.0, 4, 3, "Commission Bancaire UMOA / BCEAO"),
    "BEN": ("Bénin", "BJ", "XOF", True, 21, 6.4, 1.5, 1.6, -6.0, -0.50, -0.10, 400, 0.5, 55, 30.0, 4, 2, "Commission Bancaire UMOA / BCEAO"),
    "TGO": ("Togo", "TG", "XOF", True, 9.7, 5.5, 3.0, 2.0, -10.0, -0.60, -0.70, 500, 0.5, 60, 27.0, 4, 2, "Commission Bancaire UMOA / BCEAO"),
    "BFA": ("Burkina Faso", "BF", "XOF", True, 21, 4.5, 4.0, 5.0, -3.0, -2.00, -0.40, 900, 0.5, 75, 27.5, 5, 2, "Commission Bancaire UMOA / BCEAO"),
    "USA": ("États-Unis", "US", "USD", False, 29180, 2.8, 3.0, 4.0, -3.0, 0.00, 1.10, 40, 7, 25, 21.0, 3, 5, "Federal Reserve / OCC / FDIC"),
    "CAN": ("Canada", "CA", "CAD", False, 2240, 1.5, 2.4, 6.4, -0.5, 0.90, 1.60, 30, 7, 20, 26.5, 2, 5, "BSIF (OSFI)"),
    "MEX": ("Mexique", "MX", "MXN", True, 1850, 1.4, 4.7, 2.7, -0.5, -0.70, -0.90, 300, 14, 60, 30.0, 4, 3, "CNBV / Banxico"),
    "BRA": ("Brésil", "BR", "BRL", True, 2170, 3.2, 4.4, 6.9, 2.0, -0.40, -0.40, 220, 16, 50, 34.0, 4, 4, "Banco Central do Brasil"),
    "CHL": ("Chili", "CL", "CLP", True, 330, 2.6, 3.9, 8.5, 4.0, 0.10, 0.90, 110, 13, 30, 27.0, 3, 4, "CMF"),
    "COL": ("Colombie", "CO", "COP", True, 420, 1.7, 6.6, 10.2, -3.0, -0.70, -0.40, 320, 14, 55, 35.0, 4, 3, "Superintendencia Financiera"),
    "ARG": ("Argentine", "AR", "ARS", True, 630, -1.7, 220.0, 7.5, 3.0, -0.10, -0.40, 700, 35, 50, 35.0, 3, 3, "BCRA"),
    "PER": ("Pérou", "PE", "PEN", True, 290, 3.3, 2.4, 6.5, 5.0, -0.50, -0.50, 150, 8, 45, 29.5, 4, 3, "SBS"),
    "SGP": ("Singapour", "SG", "SGD", False, 550, 4.4, 2.4, 2.0, 30.0, 1.50, 2.20, 15, 5, 35, 17.0, 3, 5, "MAS"),
    "JPN": ("Japon", "JP", "JPY", False, 4030, 0.1, 2.7, 2.5, -1.0, 1.00, 1.40, 25, 10, 20, 30.6, 3, 4, "FSA"),
    "KOR": ("Corée du Sud", "KR", "KRW", False, 1870, 2.0, 2.3, 2.8, 3.0, 0.60, 0.80, 35, 9, 25, 24.0, 2, 5, "FSC / FSS"),
    "IDN": ("Indonésie", "ID", "IDR", True, 1400, 5.0, 2.3, 4.9, 2.0, -0.40, -0.40, 100, 8, 50, 22.0, 4, 3, "OJK"),
    "VNM": ("Vietnam", "VN", "VND", True, 476, 7.1, 3.6, 2.1, 6.0, 0.00, -0.30, 180, 7, 60, 20.0, 5, 3, "State Bank of Vietnam"),
    "MYS": ("Malaisie", "MY", "MYR", True, 420, 5.1, 1.8, 3.3, 6.0, 0.20, 0.30, 60, 7, 40, 24.0, 3, 4, "Bank Negara Malaysia"),
    "ARE": ("Émirats Arabes Unis", "AE", "AED", True, 540, 3.8, 1.7, 2.5, 15.0, 0.70, 1.00, 60, 7, 55, 9.0, 4, 5, "CBUAE / DFSA / FSRA"),
    "SAU": ("Arabie Saoudite", "SA", "SAR", True, 1100, 1.3, 1.7, 3.7, 2.0, -0.40, 0.20, 90, 7, 50, 20.0, 4, 4, "SAMA"),
    "QAT": ("Qatar", "QA", "QAR", True, 220, 2.4, 1.1, 0.1, 25.0, 0.90, 0.80, 50, 7, 40, 10.0, 4, 5, "QCB / QFCRA"),
    "MAR": ("Maroc", "MA", "MAD", True, 155, 3.2, 1.0, 13.3, -12.0, -0.30, -0.30, 150, 3, 45, 37.0, 4, 3, "Bank Al-Maghrib"),
    "EGY": ("Égypte", "EG", "EGP", True, 390, 2.4, 33.0, 7.0, -7.0, -1.00, -0.80, 700, 25, 60, 22.5, 5, 3, "Central Bank of Egypt"),
}
PROFILES: dict[str, dict[str, Any]] = {
    k: dict(zip(_PROFILE_FIELDS, v)) for k, v in _PROFILES_RAW.items()
}
EURO_AREA = {"FRA", "LUX", "BEL", "NLD", "DEU"}
EU_MEMBERS = EURO_AREA

# Trajectoires d'inflation explicites pour les pays à régime inflationniste.
INFLATION_OVERRIDES = {
    "ARG": [26.5, 41.1, 26.0, 34.3, 53.5, 42.0, 48.4, 72.4, 133.5, 219.9, 45.0, 25.0],
    "EGY": [10.4, 13.8, 29.5, 14.4, 9.2, 5.0, 5.2, 13.9, 33.9, 28.3, 20.0, 13.0],
}

# Taux de change indicatifs (1 unité = x USD) pour normaliser les capitalisations.
FX_TO_USD = {
    "USD": 1.0, "EUR": 1.15, "GBP": 1.30, "GBp": 0.013, "CAD": 0.72, "MXN": 0.054,
    "BRL": 0.18, "CLP": 0.00105, "COP": 0.00025, "ARS": 0.0008, "PEN": 0.28,
    "SGD": 0.77, "JPY": 0.0067, "KRW": 0.00072, "IDR": 0.00006, "VND": 0.000038,
    "MYR": 0.23, "AED": 0.2723, "SAR": 0.2667, "QAR": 0.2747, "MAD": 0.11,
    "EGP": 0.020, "XOF": 0.00175,
}

# Univers de cibles : (ticker, nom, catégorie, taille L/M/S, disponible sur Yahoo)
TARGET_UNIVERSE: dict[str, list[tuple[str, str, str, str, bool]]] = {
    "FRA": [("BNP.PA", "BNP Paribas", "Banque", "L", True),
            ("GLE.PA", "Société Générale", "Banque", "L", True),
            ("ACA.PA", "Crédit Agricole SA", "Banque", "L", True),
            ("WLN.PA", "Worldline", "Fintech / Paiements", "M", True),
            ("COFA.PA", "Coface", "Assurance-crédit", "M", True)],
    "LUX": [("BIL-NC", "Banque Internationale à Luxembourg (non cotée)", "Banque", "M", False),
            ("QUINTET-NC", "Quintet Private Bank (non cotée)", "Banque privée", "M", False),
            ("SPUERKEESS-NC", "Spuerkeess / BCEE (non cotée)", "Banque", "M", False)],
    "BEL": [("KBC.BR", "KBC Group", "Banque", "L", True),
            ("AGS.BR", "Ageas", "Assurance", "M", True)],
    "NLD": [("INGA.AS", "ING Groep", "Banque", "L", True),
            ("ABN.AS", "ABN AMRO", "Banque", "L", True),
            ("ADYEN.AS", "Adyen", "Fintech / Paiements", "L", True),
            ("VLK.AS", "Van Lanschot Kempen", "Banque privée", "S", True)],
    "DEU": [("DBK.DE", "Deutsche Bank", "Banque", "L", True),
            ("CBK.DE", "Commerzbank", "Banque", "L", True),
            ("HYQ.DE", "Hypoport", "Fintech", "S", True)],
    "SEN": [("BOAS.BRVM", "Bank of Africa Sénégal", "Banque", "M", False),
            ("SNTS.BRVM", "Sonatel (Orange Money)", "Télécom / Mobile money", "L", False),
            ("ETIT.BRVM", "Ecobank Transnational (réseau régional)", "Banque", "L", False)],
    "CIV": [("SGBC.BRVM", "Société Générale Côte d'Ivoire", "Banque", "L", False),
            ("ECOC.BRVM", "Ecobank Côte d'Ivoire", "Banque", "M", False),
            ("SIBC.BRVM", "Société Ivoirienne de Banque", "Banque", "M", False),
            ("NSBC.BRVM", "NSIA Banque CI", "Banque", "M", False),
            ("BOAC.BRVM", "Bank of Africa Côte d'Ivoire", "Banque", "S", False)],
    "BEN": [("BOAB.BRVM", "Bank of Africa Bénin", "Banque", "M", False),
            ("ETIT.BRVM", "Ecobank Transnational (réseau régional)", "Banque", "L", False)],
    "TGO": [("ETIT.BRVM", "Ecobank Transnational", "Banque", "L", False),
            ("ORGT.BRVM", "Oragroup", "Banque", "M", False)],
    "BFA": [("CBIBF.BRVM", "Coris Bank International", "Banque", "M", False),
            ("BOABF.BRVM", "Bank of Africa Burkina Faso", "Banque", "S", False)],
    "USA": [("JPM", "JPMorgan Chase", "Banque", "L", True),
            ("BAC", "Bank of America", "Banque", "L", True),
            ("C", "Citigroup", "Banque", "L", True),
            ("WFC", "Wells Fargo", "Banque", "L", True),
            ("SOFI", "SoFi Technologies", "Fintech", "M", True),
            ("PYPL", "PayPal", "Fintech / Paiements", "L", True)],
    "CAN": [("RY.TO", "Royal Bank of Canada", "Banque", "L", True),
            ("TD.TO", "Toronto-Dominion Bank", "Banque", "L", True),
            ("BNS.TO", "Bank of Nova Scotia", "Banque", "L", True),
            ("NA.TO", "Banque Nationale du Canada", "Banque", "L", True)],
    "MEX": [("GFNORTEO.MX", "Grupo Financiero Banorte", "Banque", "L", True),
            ("BBAJIOO.MX", "Banco del Bajío", "Banque", "M", True),
            ("GENTERA.MX", "Gentera", "Microfinance", "M", True),
            ("Q.MX", "Quálitas", "Assurance", "M", True)],
    "BRA": [("ITUB", "Itaú Unibanco", "Banque", "L", True),
            ("BBD", "Banco Bradesco", "Banque", "L", True),
            ("NU", "Nu Holdings (Nubank)", "Fintech / Néobanque", "L", True),
            ("BBAS3.SA", "Banco do Brasil", "Banque", "L", True),
            ("BPAC11.SA", "BTG Pactual", "Banque d'investissement", "L", True),
            ("PAGS", "PagSeguro", "Fintech / Paiements", "M", True)],
    "CHL": [("BCH", "Banco de Chile", "Banque", "L", True),
            ("BSAC", "Banco Santander Chile", "Banque", "L", True)],
    "COL": [("CIB", "Grupo Cibest (Bancolombia)", "Banque", "L", True),
            ("AVAL", "Grupo Aval", "Banque", "M", True)],
    "ARG": [("GGAL", "Grupo Financiero Galicia", "Banque", "L", True),
            ("BMA", "Banco Macro", "Banque", "M", True),
            ("BBAR", "BBVA Argentina", "Banque", "M", True),
            ("SUPV", "Grupo Supervielle", "Banque", "S", True)],
    "PER": [("BAP", "Credicorp", "Banque", "L", True),
            ("IFS", "Intercorp Financial Services", "Banque", "M", True)],
    "SGP": [("D05.SI", "DBS Group", "Banque", "L", True),
            ("O39.SI", "OCBC", "Banque", "L", True),
            ("U11.SI", "United Overseas Bank", "Banque", "L", True)],
    "JPN": [("8306.T", "Mitsubishi UFJ Financial Group", "Banque", "L", True),
            ("8316.T", "Sumitomo Mitsui Financial Group", "Banque", "L", True),
            ("8411.T", "Mizuho Financial Group", "Banque", "L", True),
            ("8591.T", "ORIX", "Services financiers", "L", True)],
    "KOR": [("105560.KS", "KB Financial Group", "Banque", "L", True),
            ("055550.KS", "Shinhan Financial Group", "Banque", "L", True),
            ("086790.KS", "Hana Financial Group", "Banque", "L", True),
            ("323410.KS", "KakaoBank", "Fintech / Néobanque", "M", True)],
    "IDN": [("BBCA.JK", "Bank Central Asia", "Banque", "L", True),
            ("BBRI.JK", "Bank Rakyat Indonesia", "Banque", "L", True),
            ("BMRI.JK", "Bank Mandiri", "Banque", "L", True),
            ("BBNI.JK", "Bank Negara Indonesia", "Banque", "M", True)],
    "VNM": [("VCB.VN", "Vietcombank", "Banque", "L", True),
            ("BID.VN", "BIDV", "Banque", "L", True),
            ("TCB.VN", "Techcombank", "Banque", "M", True),
            ("MBB.VN", "MB Bank", "Banque", "M", True)],
    "MYS": [("1155.KL", "Maybank", "Banque", "L", True),
            ("1295.KL", "Public Bank", "Banque", "L", True),
            ("1023.KL", "CIMB Group", "Banque", "L", True),
            ("5819.KL", "Hong Leong Bank", "Banque", "M", True)],
    "ARE": [("FAB.ADX", "First Abu Dhabi Bank", "Banque", "L", False),
            ("ENBD.DFM", "Emirates NBD", "Banque", "L", False),
            ("ADCB.ADX", "Abu Dhabi Commercial Bank", "Banque", "L", False),
            ("DIB.DFM", "Dubai Islamic Bank", "Banque islamique", "M", False)],
    "SAU": [("1120.SR", "Al Rajhi Bank", "Banque islamique", "L", True),
            ("1180.SR", "Saudi National Bank", "Banque", "L", True),
            ("1010.SR", "Riyad Bank", "Banque", "M", True),
            ("1150.SR", "Alinma Bank", "Banque islamique", "M", True)],
    "QAT": [("QNBK.QA", "Qatar National Bank", "Banque", "L", True),
            ("CBQK.QA", "Commercial Bank of Qatar", "Banque", "M", True),
            ("MARK.QA", "Masraf Al Rayan", "Banque islamique", "M", True)],
    "MAR": [("ATW.CS", "Attijariwafa Bank", "Banque", "L", False),
            ("BCP.CS", "Banque Centrale Populaire", "Banque", "M", False),
            ("BOA.CS", "Bank of Africa (BMCE)", "Banque", "M", False),
            ("CIH.CS", "CIH Bank", "Banque", "S", False)],
    "EGY": [("COMI.CA", "Commercial International Bank", "Banque", "L", True),
            ("HRHO.CA", "EFG Holding", "Banque d'investissement", "M", True)],
}

# Métadonnées des indicateurs macro et codes API associés.
INDICATORS: dict[str, dict[str, Any]] = {
    "gdp_usd": dict(label="PIB nominal", unit="Md$", fmt="{:,.0f}", wb="NY.GDP.MKTP.CD",
                    wb_scale=1e-9, imf="NGDPD", higher_is_better=True),
    "gdp_growth": dict(label="Croissance du PIB réel", unit="%", fmt="{:+.1f}",
                       wb="NY.GDP.MKTP.KD.ZG", imf="NGDP_RPCH", higher_is_better=True),
    "inflation": dict(label="Inflation annuelle", unit="%", fmt="{:.1f}",
                      wb="FP.CPI.TOTL.ZG", imf="PCPIPCH", higher_is_better=False),
    "unemployment": dict(label="Taux de chômage", unit="%", fmt="{:.1f}",
                         wb="SL.UEM.TOTL.ZS", imf="LUR", higher_is_better=False),
    "trade_balance": dict(label="Balance commerciale", unit="% PIB", fmt="{:+.1f}",
                          wb="NE.RSB.GNFS.ZS", imf=None, higher_is_better=True),
    "wgi_pv": dict(label="Stabilité politique (WGI)", unit="indice", fmt="{:+.2f}",
                   wgi="PV.EST", higher_is_better=True),
    "wgi_cc": dict(label="Contrôle de la corruption (WGI)", unit="indice", fmt="{:+.2f}",
                   wgi="CC.EST", higher_is_better=True),
}

COMPLIANCE_CHECKLIST = [
    ("fatca_giin", "FATCA", "Vérification du GIIN et du statut FATCA de la cible (FFI participante / IGA)"),
    ("fatca_w8", "FATCA", "Revue des formulaires W-8 / W-9 et identification des US Persons"),
    ("crs_status", "CRS", "Statut de la juridiction dans l'échange automatique OCDE (CRS / AEOI)"),
    ("crs_selfcert", "CRS", "Auto-certifications de résidence fiscale de la clientèle"),
    ("lcb_kyc", "LCB-FT", "Procédures KYC / CDD et vigilance renforcée (EDD) sur clients à risque"),
    ("lcb_ubo", "LCB-FT", "Identification des bénéficiaires effectifs (UBO) et accès au registre local"),
    ("lcb_pep", "LCB-FT", "Filtrage des Personnes Politiquement Exposées (PPE)"),
    ("sanc_screen", "Sanctions", "Filtrage listes UE / ONU / OFAC / DG Trésor sur clients et contreparties"),
    ("sanc_expo", "Sanctions", "Analyse de l'exposition aux pays et secteurs sous sanctions (flux transfrontaliers)"),
    ("gafi_status", "GAFI", "Vérification du statut GAFI de la juridiction (listes grise / noire à jour)"),
    ("prudential", "Prudentiel", "Autorisations superviseurs (prise de participation qualifiante)"),
    ("data_priv", "Données", "Protection des données (RGPD / loi locale) et transferts hors UE"),
]


def seeded_rng(*keys: str) -> np.random.Generator:
    """Générateur pseudo-aléatoire déterministe (données simulées stables)."""
    return np.random.default_rng(zlib.crc32("|".join(keys).encode()))


def risk_level(score: float) -> str:
    """Traduit un score 0-100 en niveau qualitatif."""
    if score < 25:
        return "Faible"
    if score < 50:
        return "Modéré"
    if score < 75:
        return "Élevé"
    return "Critique"


def clip(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return float(max(lo, min(hi, x)))


# =============================================================================
# 2. COUCHE DONNÉES « FAIL-SAFE »
# =============================================================================

@st.cache_resource(show_spinner=False)
def get_executor() -> ThreadPoolExecutor:
    """Pool de threads unique (survit aux reruns Streamlit)."""
    return ThreadPoolExecutor(max_workers=8, thread_name_prefix="bpce-api")


def run_with_timeout(fn: Callable, timeout: float, *args, **kwargs):
    """Exécute `fn` avec un délai maximal ; lève FuturesTimeout au-delà."""
    return get_executor().submit(fn, *args, **kwargs).result(timeout=timeout)


def http_get(url: str, params: dict | None = None) -> requests.Response:
    """GET HTTP avec timeout et contrôle du statut (lève RequestException)."""
    resp = requests.get(url, params=params, timeout=HTTP_TIMEOUT, headers=USER_AGENT)
    resp.raise_for_status()
    return resp


def status_from(n_ok: int, n_total: int) -> str:
    if n_total == 0 or n_ok == 0:
        return "fallback"
    return "live" if n_ok == n_total else "partial"


# ---------- 2.1 Données macro simulées (mode secours) -------------------------

def mock_macro_series(iso3: str, indicator: str) -> dict[int, float]:
    """Série 2015-2026 crédible, déterministe, calée sur le profil du pays.

    Intègre les chocs historiques majeurs (Covid-19 2020, rebond 2021,
    choc inflationniste 2022-2023) pour rester réaliste.
    """
    p = PROFILES[iso3]
    rng = seeded_rng(iso3, indicator)
    noise = rng.normal(0, 1, len(YEARS))
    out: dict[int, float] = {}

    if indicator == "gdp_growth":
        shock = {2020: -(p["g"] + (2.5 if p["em"] else 5.0)), 2021: 4.0 if not p["em"] else 3.5,
                 2022: 0.8, 2023: -0.4}
        for i, y in enumerate(YEARS):
            out[y] = round(p["g"] + shock.get(y, 0.0) + 0.45 * noise[i], 2)
        out[2024] = p["g"]
    elif indicator == "inflation":
        if iso3 in INFLATION_OVERRIDES:
            return dict(zip(YEARS, INFLATION_OVERRIDES[iso3]))
        add = {2021: 1.6, 2022: 5.0 if not p["em"] else 4.0, 2023: 2.6 if not p["em"] else 3.0}
        for i, y in enumerate(YEARS):
            if y < 2021:
                base = p["inf"] * 0.65
            elif y <= 2024:
                base = p["inf"]
            else:
                base = p["inf"] * (0.92 if y == 2025 else 0.86) + 0.1
            out[y] = round(max(-0.8, base + add.get(y, 0.0) + 0.35 * noise[i]), 2)
        out[2024] = p["inf"]
    elif indicator == "unemployment":
        for i, y in enumerate(YEARS):
            trend = p["un"] * (1.12 - 0.012 * (y - 2015))
            covid = {2020: 1.4, 2021: 0.8}.get(y, 0.0)
            out[y] = round(max(0.1, trend + covid + 0.15 * noise[i]), 2)
        out[2024] = p["un"]
    elif indicator == "gdp_usd":
        growth = mock_macro_series(iso3, "gdp_growth")
        nominal_drift = 2.2
        levels = {2024: float(p["gdp"])}
        for y in range(2023, 2014, -1):
            levels[y] = levels[y + 1] / (1 + (growth[y + 1] + nominal_drift) / 100)
        for y in (2025, 2026):
            levels[y] = levels[y - 1] * (1 + (growth[y] + nominal_drift) / 100)
        out = {y: round(v, 1) for y, v in sorted(levels.items())}
    elif indicator == "trade_balance":
        for i, y in enumerate(YEARS):
            out[y] = round(p["tb"] + 0.8 * noise[i], 2)
    elif indicator in ("wgi_pv", "wgi_cc"):
        base = p["pv"] if indicator == "wgi_pv" else p["cc"]
        for i, y in enumerate(YEARS):
            if y <= 2024:
                out[y] = round(base + 0.08 * noise[i], 3)
    return out


# ---------- 2.2 Banque Mondiale & FMI ----------------------------------------

def _wb_indicator(code: str, iso3s: list[str], source: int | None = None) -> dict[str, dict[int, float]]:
    """Récupère un indicateur Banque Mondiale (API v2) pour plusieurs pays."""
    params = {"format": "json", "date": f"{YEARS[0]}:{YEARS[-1]}", "per_page": 2000}
    if source:
        params["source"] = source
    url = f"{WB_BASE}/country/{';'.join(iso3s)}/indicator/{code}"
    payload = http_get(url, params).json()
    if not isinstance(payload, list) or len(payload) < 2 or not payload[1]:
        raise ValueError(f"Réponse Banque Mondiale vide pour {code}")
    out: dict[str, dict[int, float]] = {}
    for row in payload[1]:
        iso = row.get("countryiso3code") or row["country"]["id"]
        if row.get("value") is None:
            continue
        out.setdefault(iso, {})[int(row["date"])] = float(row["value"])
    if not out:
        raise ValueError(f"Aucune valeur Banque Mondiale pour {code}")
    return out


def _wb_wgi(code: str, iso3s: list[str]) -> dict[str, dict[int, float]]:
    """Indicateurs WGI : plusieurs variantes de code/source selon les millésimes."""
    last_err: Exception | None = None
    for variant, source in ((f"GOV_WGI_{code}", 3), (code, 3), (code, None)):
        try:
            return _wb_indicator(variant, iso3s, source)
        except (requests.exceptions.RequestException, ValueError, KeyError, TypeError) as err:
            last_err = err
    raise ValueError(f"WGI indisponible : {last_err}")


def _imf_indicator(code: str, iso3s: list[str]) -> dict[str, dict[int, float]]:
    """Indicateur FMI (DataMapper / WEO), historique + projections."""
    url = f"{IMF_BASE}/{code}/{'/'.join(iso3s)}"
    payload = http_get(url, {"periods": ",".join(str(y) for y in YEARS)}).json()
    values = payload["values"][code]
    out = {iso: {int(y): float(v) for y, v in series.items() if v is not None}
           for iso, series in values.items()}
    if not out:
        raise ValueError(f"Réponse FMI vide pour {code}")
    return out


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def fetch_macro_data(iso3s: tuple[str, ...]) -> tuple[pd.DataFrame, dict[str, str]]:
    """Agrège les données macro d'une zone : Banque Mondiale + FMI + secours.

    Règles de fusion :
      * valeurs réalisées Banque Mondiale prioritaires ;
      * années manquantes / futures complétées par le FMI (WEO) ;
      * projections = années postérieures au dernier réalisé ;
      * pays/indicateur sans aucune donnée -> série simulée (« Simulé »).

    Returns:
        (DataFrame long [iso3, year, indicator, value, projection, source],
         dict statut par fournisseur : live / partial / fallback)
    """
    iso_list = list(iso3s)
    wb_ok = wb_total = imf_ok = imf_total = 0
    rows: list[dict[str, Any]] = []
    ex = get_executor()
    api_errors = (requests.exceptions.RequestException, FuturesTimeout, ValueError,
                  KeyError, TypeError, IndexError)

    # Lancement parallèle de tous les appels : les timeouts ne s'additionnent pas.
    wb_jobs, imf_jobs = {}, {}
    for ind, meta in INDICATORS.items():
        if "wgi" in meta:
            wb_jobs[ind] = ex.submit(_wb_wgi, meta["wgi"], iso_list)
        else:
            wb_jobs[ind] = ex.submit(_wb_indicator, meta["wb"], iso_list)
        if meta.get("imf"):
            imf_jobs[ind] = ex.submit(_imf_indicator, meta["imf"], iso_list)
    deadline = datetime.now().timestamp() + HTTP_TIMEOUT * 3 + 4

    def collect(job):
        return job.result(timeout=max(0.5, deadline - datetime.now().timestamp()))

    for ind, meta in INDICATORS.items():
        wb_data: dict[str, dict[int, float]] = {}
        imf_data: dict[str, dict[int, float]] = {}
        # --- Banque Mondiale -------------------------------------------------
        wb_total += 1
        try:
            wb_data = collect(wb_jobs[ind])
            scale = meta.get("wb_scale", 1.0)
            wb_data = {k: {y: v * scale for y, v in s.items()} for k, s in wb_data.items()}
            wb_ok += 1
        except api_errors:
            wb_data = {}
        # --- FMI --------------------------------------------------------------
        if ind in imf_jobs:
            imf_total += 1
            try:
                imf_data = collect(imf_jobs[ind])
                imf_ok += 1
            except api_errors:
                imf_data = {}
        # --- Fusion pays par pays ----------------------------------------------
        for iso in iso_list:
            wb_s = {y: v for y, v in wb_data.get(iso, {}).items() if y in YEARS}
            imf_s = {y: v for y, v in imf_data.get(iso, {}).items() if y in YEARS}
            if wb_s or imf_s:
                last_actual = max(wb_s) if wb_s else 2024
                for y in YEARS:
                    if y in wb_s:
                        rows.append(dict(iso3=iso, year=y, indicator=ind, value=wb_s[y],
                                         projection=False, source="Banque Mondiale"))
                    elif y in imf_s:
                        rows.append(dict(iso3=iso, year=y, indicator=ind, value=imf_s[y],
                                         projection=y > last_actual, source="FMI (WEO)"))
            else:
                for y, v in mock_macro_series(iso, ind).items():
                    rows.append(dict(iso3=iso, year=y, indicator=ind, value=v,
                                     projection=y >= 2025, source="Simulé"))

    df = pd.DataFrame(rows)
    df["country"] = df["iso3"].map(lambda i: PROFILES[i]["name"])
    status = {"Banque Mondiale": status_from(wb_ok, wb_total),
              "FMI": status_from(imf_ok, imf_total)}
    return df, status


def latest_values(df: pd.DataFrame, iso3: str) -> dict[str, dict[str, Any]]:
    """Dernière valeur réalisée par indicateur (+ précédente et projection 2026)."""
    out: dict[str, dict[str, Any]] = {}
    sub = df[df["iso3"] == iso3]
    for ind in INDICATORS:
        s = sub[sub["indicator"] == ind].sort_values("year")
        actual = s[~s["projection"]]
        if actual.empty:
            actual = s
        if actual.empty:
            p = PROFILES[iso3]
            fallback = {"gdp_usd": p["gdp"], "gdp_growth": p["g"], "inflation": p["inf"],
                        "unemployment": p["un"], "trade_balance": p["tb"],
                        "wgi_pv": p["pv"], "wgi_cc": p["cc"]}[ind]
            out[ind] = dict(value=fallback, year=2024, prev=None, proj=None, source="Simulé")
            continue
        last = actual.iloc[-1]
        prev = actual.iloc[-2]["value"] if len(actual) > 1 else None
        proj = s[(s["year"] == 2026)]["value"]
        out[ind] = dict(value=float(last["value"]), year=int(last["year"]),
                        prev=None if prev is None else float(prev),
                        proj=float(proj.iloc[0]) if not proj.empty else None,
                        source=last["source"])
    return out


# ---------- 2.3 FRED, BCE & marché des changes --------------------------------

def _fred_series(series_id: str) -> pd.Series:
    """Série FRED via l'export CSV public (aucune clé API requise)."""
    resp = http_get(FRED_CSV, {"id": series_id, "cosd": "2015-01-01"})
    df = pd.read_csv(io.StringIO(resp.text))
    if df.shape[1] < 2:
        raise ValueError(f"FRED : format inattendu pour {series_id}")
    s = pd.to_numeric(df.iloc[:, 1], errors="coerce")
    s.index = pd.to_datetime(df.iloc[:, 0], errors="coerce")
    s = s.dropna()
    if s.empty:
        raise ValueError(f"FRED : série vide {series_id}")
    return s


def _ecb_deposit_rate() -> float:
    """Taux de la facilité de dépôt BCE (flux SDW / Data Portal)."""
    url = f"{ECB_BASE}/FM/B.U2.EUR.4F.KR.DFR.LEV"
    resp = http_get(url, {"format": "csvdata", "lastNObservations": 1})
    df = pd.read_csv(io.StringIO(resp.text))
    return float(df["OBS_VALUE"].iloc[-1])


def _yf_fx_vol(currency: str) -> float:
    """Volatilité annualisée (%) EUR/devise sur 1 an via Yahoo Finance."""
    if yf is None:
        raise ImportError("yfinance non installé")
    hist = yf.Ticker(f"EUR{currency}=X").history(period="1y")
    close = hist["Close"].dropna()
    if len(close) < 60:
        raise ValueError("Historique FX insuffisant")
    rets = np.log(close).diff().dropna()
    return float(rets.std() * math.sqrt(252) * 100)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def fetch_market_risk_data(iso3: str) -> tuple[dict[str, Any], dict[str, str]]:
    """Indicateurs de marché pour le risque : spread souverain, stress EM,
    taux directeur de référence et volatilité de change.

    * Zone euro : spread 10 ans vs Bund (FRED / OCDE IRLTLT01) — mesure directe.
    * Autres : spread estimé, ajusté du stress EM courant (ICE BofA EM OAS, FRED).
    """
    p = PROFILES[iso3]
    status: dict[str, str] = {}
    out: dict[str, Any] = dict(spread_bps=float(p["spread"]), spread_src="Estimation (secours)",
                               em_oas=None, policy_rate=None, policy_label=None,
                               fx_vol=float(p["fxvol"]), fx_src="Estimation (secours)",
                               yield_hist=None)
    fred_ok = fred_total = 0
    ex = get_executor()
    deadline = datetime.now().timestamp() + max(HTTP_TIMEOUT + 4, YF_TIMEOUT)

    def collect(job):
        return job.result(timeout=max(0.5, deadline - datetime.now().timestamp()))

    # Lancement parallèle des requêtes nécessaires
    jobs: dict[str, Any] = {"oas": ex.submit(_fred_series, "BAMLEMCBPIOAS")}
    if iso3 in EURO_AREA and iso3 != "DEU":
        jobs["y_c"] = ex.submit(_fred_series, f"IRLTLT01{p['iso2']}M156N")
        jobs["y_b"] = ex.submit(_fred_series, "IRLTLT01DEM156N")
    if iso3 in EURO_AREA or p["currency"] == "XOF":
        jobs["ecb"] = ex.submit(_ecb_deposit_rate)
    elif p["currency"] == "USD" or iso3 in ("ARE", "SAU", "QAT"):
        jobs["ff"] = ex.submit(_fred_series, "FEDFUNDS")
    if p["currency"] not in ("EUR", "XOF"):
        jobs["fx"] = ex.submit(_yf_fx_vol, p["currency"])

    # Spread souverain zone euro
    if "y_c" in jobs:
        fred_total += 1
        try:
            c = collect(jobs["y_c"])
            b = collect(jobs["y_b"])
            spread = (c - b).dropna()
            out["spread_bps"] = float(spread.iloc[-1] * 100)
            out["spread_src"] = "FRED / OCDE (10 ans vs Bund)"
            out["yield_hist"] = pd.DataFrame({"Pays": c, "Bund": b}).dropna()
            fred_ok += 1
        except (requests.exceptions.RequestException, FuturesTimeout, ValueError, KeyError):
            pass
    elif iso3 == "DEU":
        out["spread_src"] = "Référence (Bund)"

    # Stress marchés émergents (ajuste les spreads hors zone euro)
    fred_total += 1
    try:
        oas = collect(jobs["oas"])
        out["em_oas"] = float(oas.iloc[-1] * 100)
        if iso3 not in EURO_AREA:
            factor = float(np.clip(out["em_oas"] / 300.0, 0.6, 1.6))
            out["spread_bps"] = float(p["spread"]) * factor
            out["spread_src"] = f"Estimation ajustée stress EM (OAS {out['em_oas']:.0f} pb, FRED)"
        fred_ok += 1
    except (requests.exceptions.RequestException, FuturesTimeout, ValueError, KeyError):
        pass

    # Taux directeur de référence
    if "ecb" in jobs:
        try:
            out["policy_rate"] = collect(jobs["ecb"])
            out["policy_label"] = "Taux de dépôt BCE (live)"
            status["BCE"] = "live"
        except (requests.exceptions.RequestException, FuturesTimeout, ValueError, KeyError):
            out["policy_rate"] = 2.00
            out["policy_label"] = "Taux de dépôt BCE (estimé)"
            status["BCE"] = "fallback"
    elif "ff" in jobs:
        fred_total += 1
        try:
            ff = collect(jobs["ff"])
            out["policy_rate"] = float(ff.iloc[-1])
            out["policy_label"] = "Fed Funds effectif (FRED)"
            fred_ok += 1
        except (requests.exceptions.RequestException, FuturesTimeout, ValueError, KeyError):
            out["policy_rate"] = 3.90
            out["policy_label"] = "Fed Funds (estimé)"
    status["FRED"] = status_from(fred_ok, fred_total)

    # Volatilité de change vs EUR
    if p["currency"] == "EUR":
        out["fx_vol"], out["fx_src"] = 0.0, "Devise domestique (EUR)"
    elif p["currency"] == "XOF":
        out["fx_vol"], out["fx_src"] = 0.5, "Parité fixe EUR/XOF (655,957)"
    else:
        try:
            out["fx_vol"] = collect(jobs["fx"])
            out["fx_src"] = f"Yahoo Finance EUR{p['currency']} (vol. 1 an)"
            status["Yahoo Finance"] = "live"
        except (FuturesTimeout, ImportError, ValueError, KeyError, IndexError, TypeError,
                requests.exceptions.RequestException, Exception):  # yfinance lève des types variés
            status["Yahoo Finance"] = "fallback"
    return out, status


# ---------- 2.4 Cibles M&A (Yahoo Finance) -------------------------------------

def _mock_target(ticker: str, name: str, category: str, tier: str, iso3: str,
                 listed: bool) -> dict[str, Any]:
    """Fiche cible simulée crédible (ratios cohérents entre eux)."""
    p = PROFILES[iso3]
    rng = seeded_rng(ticker, iso3)
    uemoa = p["currency"] == "XOF"
    fintech = "Fintech" in category or "Néobanque" in category or "Mobile" in category
    if uemoa:
        cap_rng = {"L": (1.0, 4.0), "M": (0.2, 1.0), "S": (0.05, 0.3)}[tier]
    elif p["em"]:
        cap_rng = {"L": (6, 45), "M": (1.0, 6.0), "S": (0.2, 1.2)}[tier]
    else:
        cap_rng = {"L": (25, 160), "M": (3, 20), "S": (0.4, 3)}[tier]
    cap_bn = float(rng.uniform(*cap_rng))
    roe = float(rng.uniform(-2, 18) if fintech else
                (rng.uniform(14, 24) if p["em"] else rng.uniform(7, 14)))
    ke = 13.0 if p["em"] else 9.5
    pb = max(0.3, roe / ke * float(rng.uniform(0.75, 1.25)) + (1.5 if fintech else 0))
    pe = pb / max(roe, 1.0) * 100 if roe > 1 else None
    div = 0.0 if fintech else float(rng.uniform(2.5, 7.0))
    net_income = cap_bn * 1000 / pe if pe else cap_bn * 1000 * 0.01
    return dict(ticker=ticker, name=name, category=category, country=p["name"],
                market_cap_usd_m=cap_bn * 1000, pe=pe, pb=pb, roe=roe, div_yield=div,
                perf_52w=float(rng.uniform(-15, 35)), beta=float(rng.uniform(0.7, 1.4)),
                revenue_usd_m=max(net_income / 0.27, cap_bn * 1000 * 0.12),
                net_income_usd_m=net_income, currency="USD",
                source="Simulé" + ("" if listed else " (non disponible sur Yahoo)"))


def _yf_target(ticker: str) -> dict[str, Any]:
    """Fiche cible en direct depuis Yahoo Finance (lève si données incomplètes)."""
    info = yf.Ticker(ticker).info
    if not info or info.get("marketCap") in (None, 0):
        raise ValueError(f"Données Yahoo incomplètes pour {ticker}")
    cur = info.get("financialCurrency") or info.get("currency") or "USD"
    px_cur = info.get("currency") or cur
    fx_px = FX_TO_USD.get(px_cur, 1.0)
    fx_fin = FX_TO_USD.get(cur, 1.0)
    price = info.get("currentPrice") or info.get("regularMarketPrice")
    div_rate = info.get("dividendRate") or info.get("trailingAnnualDividendRate")
    div_yield = (div_rate / price * 100) if (div_rate and price) else 0.0
    roe = info.get("returnOnEquity")
    perf = info.get("52WeekChange")
    return dict(
        ticker=ticker,
        name=info.get("longName") or info.get("shortName") or ticker,
        market_cap_usd_m=info["marketCap"] * fx_px / 1e6,
        pe=info.get("trailingPE") or info.get("forwardPE"),
        pb=info.get("priceToBook"),
        roe=roe * 100 if roe is not None else None,
        div_yield=div_yield,
        perf_52w=perf * 100 if perf is not None else None,
        beta=info.get("beta"),
        revenue_usd_m=(info.get("totalRevenue") or 0) * fx_fin / 1e6 or None,
        net_income_usd_m=(info.get("netIncomeToCommon") or 0) * fx_fin / 1e6 or None,
        currency=px_cur, source="Yahoo Finance (live)",
    )


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def fetch_ma_targets(iso3: str, extra_tickers: tuple[str, ...] = ()) -> tuple[pd.DataFrame, dict[str, str]]:
    """Univers de cibles du pays : appels Yahoo parallélisés + secours ligne à ligne.

    Chaque ticker est traité indépendamment : un échec ou un timeout sur l'un
    d'eux ne bloque jamais les autres et bascule sur une fiche simulée.
    """
    universe = list(TARGET_UNIVERSE.get(iso3, []))
    known = {u[0] for u in universe}
    universe += [(t, t, "Ajout manuel", "M", True) for t in extra_tickers if t not in known]

    futures = {}
    if yf is not None:
        for tk, *_rest, listed in universe:
            if listed:
                futures[tk] = get_executor().submit(_yf_target, tk)

    rows, n_live = [], 0
    deadline = datetime.now().timestamp() + YF_TIMEOUT
    for tk, name, cat, tier, listed in universe:
        row = None
        if tk in futures:
            try:
                remaining = max(0.5, deadline - datetime.now().timestamp())
                row = futures[tk].result(timeout=remaining)
                row.update(category=cat, country=PROFILES[iso3]["name"])
                if name != tk:
                    row["name"] = name
                n_live += 1
            except Exception:  # yfinance : erreurs réseau, JSON, rate-limit, timeout…
                row = None
        if row is None:
            row = _mock_target(tk, name, cat, tier, iso3, listed)
        rows.append(row)

    df = pd.DataFrame(rows)
    n_listed = sum(1 for u in universe if u[4])
    status = {"Yahoo Finance": "fallback" if yf is None else status_from(n_live, n_listed)}
    return df, status


def enrich_targets(df: pd.DataFrame, iso3: str) -> pd.DataFrame:
    """Ajoute P/B théorique (Gordon : P/B = ROE / Ke), décote et score d'attractivité."""
    ke = 13.0 if PROFILES[iso3]["em"] else 9.5
    d = df.copy()
    d["pb_fair"] = d["roe"].apply(lambda r: max(0.2, r / ke) if pd.notna(r) else np.nan)
    d["discount"] = (1 - d["pb"] / d["pb_fair"]) * 100
    roe_score = d["roe"].fillna(0).clip(0, 25) / 25 * 50
    disc_score = d["discount"].fillna(0).clip(-50, 50) / 50 * 30 + 30
    div_score = d["div_yield"].fillna(0).clip(0, 8) / 8 * 20
    d["attractiveness"] = (roe_score + disc_score.clip(0, 60) * 0.5 + div_score).round(1)
    return d


# ---------- 2.5 Registre des statuts API (sidebar + toasts) -------------------

def register_status(statuses: dict[str, str], context: str) -> None:
    """Mémorise le statut des sources et émet un toast discret à chaque changement."""
    reg = st.session_state.setdefault("api_status", {})
    toasted = st.session_state.setdefault("toasted", set())
    for src, stt in statuses.items():
        reg[src] = stt
        key = (src, context, stt)
        if key not in toasted:
            toasted.add(key)
            if stt == "live":
                st.toast(f"Données en direct — {src}", icon=":material/cloud_done:")
            elif stt == "partial":
                st.toast(f"{src} : données partielles, compléments simulés", icon=":material/cloud_sync:")
            else:
                st.toast(f"{src} indisponible — données estimées / mode secours", icon=":material/cloud_off:")


def source_badges(statuses: dict[str, str]) -> None:
    """Affiche les badges de provenance de la donnée."""
    labels = {"live": "Données en direct", "partial": "Données partielles + secours",
              "fallback": "Données estimées / Mode secours"}
    html = "".join(f'<span class="src-badge {s}">● {labels[s]} — {src}</span>'
                   for src, s in statuses.items())
    st.markdown(html, unsafe_allow_html=True)


# =============================================================================
# 3. MOTEURS MÉTIER
# =============================================================================

# ---------- 3.1 Scoring des risques -------------------------------------------

def compute_risk_scores(iso3: str, latest: dict, market: dict,
                        checklist_ratio: float = 0.0) -> dict[str, Any]:
    """Calcule les scores de risque (0-100, 100 = risque maximal).

    * Risque Pays   : stabilité politique 30 %, spread souverain 35 %,
                      gouvernance 15 %, déséquilibres macro 20 %.
    * Risque Change : volatilité EUR/devise 70 %, différentiel d'inflation 30 %.
    * Risque KYC    : base juridictionnelle indicative 60 %, corruption 40 %,
                      atténué par l'avancement de la checklist conformité.
    """
    p = PROFILES[iso3]
    pv = latest["wgi_pv"]["value"]
    cc = latest["wgi_cc"]["value"]
    infl = latest["inflation"]["value"]
    growth = latest["gdp_growth"]["value"]
    tb = latest["trade_balance"]["value"]

    pv_c = clip((2.5 - pv) / 5 * 100)
    cc_c = clip((2.5 - cc) / 5 * 100)
    spread_c = clip(market["spread_bps"] / 10)
    macro_c = clip(abs(infl - 2) * 6 + max(0, 1.5 - growth) * 8 + max(0, -tb - 3) * 2)
    country = 0.30 * pv_c + 0.35 * spread_c + 0.15 * cc_c + 0.20 * macro_c

    fx_c = clip(market["fx_vol"] * 3.3)
    infl_diff_c = clip(abs(infl - 2.0) * 6)
    fx = 0.7 * fx_c + 0.3 * infl_diff_c

    kyc_raw = 0.6 * p["kyc"] + 0.4 * cc_c
    kyc = kyc_raw * (1 - 0.25 * checklist_ratio)

    scores = {"Risque Pays": round(country, 1), "Risque de Change": round(fx, 1),
              "Risque KYC / Sanctions": round(kyc, 1)}

    # Matrice Impact x Probabilité (échelle 1-5)
    def scale(x: float) -> float:
        return round(1 + clip(x) / 100 * 4, 2)

    matrix = [
        ("Risque souverain", scale(spread_c), 4.6),
        ("Instabilité politique", scale(pv_c), 4.2),
        ("Risque de change", scale(fx_c), 3.4 if p["currency"] not in ("EUR", "XOF") else 1.8),
        ("Liquidité / refinancement", scale(0.5 * spread_c + 0.5 * fx_c), 3.8),
        ("LCB-FT", scale(kyc_raw), 4.4),
        ("Sanctions internationales", scale(kyc_raw * 0.8 + pv_c * 0.2), 4.9),
        ("Réglementaire / prudentiel", scale(35 + 0.3 * cc_c), 3.2),
        ("Inflation / macro", scale(macro_c), 2.8),
        ("Climat / ESG", scale((p["climate"] - 1) * 25), 2.6),
        ("Cyber / opérationnel", scale(30 + (5 - p["digital"]) * 8), 3.6),
    ]
    return dict(scores=scores, levels={k: risk_level(v) for k, v in scores.items()},
                components=dict(pv=pv_c, cc=cc_c, spread=spread_c, macro=macro_c,
                                fx=fx_c, infl_diff=infl_diff_c, kyc_raw=kyc_raw),
                matrix=matrix, composite=round(np.mean(list(scores.values())), 1))


# ---------- 3.2 Simulation financière post-acquisition -------------------------

def irr(cashflows: list[float]) -> float | None:
    """TRI par dichotomie (robuste, sans dépendance externe). None si indéfini."""
    def npv(rate: float) -> float:
        return sum(cf / (1 + rate) ** t for t, cf in enumerate(cashflows))
    lo, hi = -0.95, 5.0
    f_lo, f_hi = npv(lo), npv(hi)
    if np.isnan(f_lo) or np.isnan(f_hi) or f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = npv(mid)
        if abs(f_mid) < 1e-7:
            break
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


def payback_period(cashflows: list[float]) -> float | None:
    """Délai de récupération (années, interpolé) sur flux cumulés."""
    cum = 0.0
    for t, cf in enumerate(cashflows):
        prev = cum
        cum += cf
        if t > 0 and cum >= 0 > prev:
            return t - 1 + (-prev / cf if cf else 0)
    return None


def run_financial_simulation(params: dict[str, float]) -> dict[str, Any]:
    """Modèle de deal post-acquisition (P&L bancaire + flux actionnaire BPCE).

    Hypothèses :
      * PNB organique croissant au taux nominal g ; synergies de revenus et de
        coûts en montée en charge 35 % / 75 % / 100 % (années 1, 2, 3+) ;
      * coûts d'intégration one-off = 120 % des synergies de coûts annuelles
        en régime de croisière, étalés 60 % / 40 % sur les années 1-2 ;
      * dette d'acquisition in fine (intérêts annuels, remboursement à la sortie) ;
      * flux BPCE = dividendes remontés (taux de distribution) + valeur de sortie
        (multiple P/E x résultat net de l'année de sortie) - remboursement dette.
    """
    price = params["price"]
    equity = price * params["equity_pct"] / 100
    debt = price - equity
    n = int(max(5, params["horizon"]))
    ramp = [0.35, 0.75] + [1.0] * (n - 2)
    g = params["growth"] / 100
    cir = params["cir"] / 100
    rows, cashflows = [], [-equity]
    run_rate_cost_syn = params["pnb0"] * cir * params["cost_syn"] / 100

    for t in range(1, n + 1):
        pnb_org = params["pnb0"] * (1 + g) ** t
        rev_syn = pnb_org * params["rev_syn"] / 100 * ramp[t - 1]
        pnb = pnb_org + rev_syn
        costs_org = pnb_org * cir
        cost_syn = costs_org * params["cost_syn"] / 100 * ramp[t - 1]
        integration = run_rate_cost_syn * 1.2 * ({1: 0.6, 2: 0.4}.get(t, 0.0))
        opex = costs_org - cost_syn + integration + rev_syn * cir * 0.5
        rbe = pnb - opex
        cor = pnb * params["cor"] / 100
        interest = debt * params["rate"] / 100
        rai = rbe - cor - interest
        tax = max(0.0, rai * params["tax"] / 100)
        net = rai - tax
        standalone = (pnb_org - costs_org - pnb_org * params["cor"] / 100) * (1 - params["tax"] / 100)
        dividends = max(0.0, net) * params["payout"] / 100
        cf = dividends
        exit_value = 0.0
        if t == int(params["horizon"]):
            exit_value = max(0.0, net) * params["exit_pe"]
            cf += exit_value - debt
        if t <= int(params["horizon"]):
            cashflows.append(cf)
        rows.append(dict(Année=f"A{t}", annee=t, PNB_organique=pnb_org, Synergies_revenus=rev_syn,
                         PNB=pnb, Charges=opex, Synergies_couts=cost_syn,
                         Couts_integration=integration, RBE=rbe, Cout_du_risque=cor,
                         Frais_financiers=interest, Impot=tax, Resultat_net=net,
                         Resultat_net_standalone=standalone,
                         Coef_exploitation=opex / pnb * 100 if pnb else np.nan,
                         Flux_BPCE=cf, Valeur_sortie=exit_value))

    pl = pd.DataFrame(rows)
    rate = irr(cashflows)
    inflows = sum(cashflows[1:])
    roi = (inflows - equity) / equity * 100 if equity else None
    moic = inflows / equity if equity else None
    pb = payback_period(cashflows)
    roe_y3 = pl.loc[min(2, n - 1), "Resultat_net"] / equity * 100 if equity else None
    return dict(pl=pl, cashflows=cashflows, irr=None if rate is None else rate * 100,
                roi=roi, moic=moic, payback=pb, equity=equity, debt=debt,
                roe_y3=roe_y3, params=dict(params))


def sensitivity_grid(params: dict[str, float]) -> pd.DataFrame:
    """TRI selon synergies de coûts (lignes) et multiple de sortie (colonnes)."""
    cost_grid = [0, 5, 10, 15, 20, 25]
    base_pe = params["exit_pe"]
    pe_grid = sorted({round(max(4.0, base_pe + d), 1) for d in (-4, -2, 0, 2, 4, 6)})
    data = {}
    for pe in pe_grid:
        col = []
        for cs in cost_grid:
            r = run_financial_simulation({**params, "cost_syn": cs, "exit_pe": pe})["irr"]
            col.append(np.nan if r is None else r)
        data[f"P/E {pe:g}x"] = col
    return pd.DataFrame(data, index=[f"Syn. coûts {c}%" for c in cost_grid])


# ---------- 3.3 Générateur PDF (ReportLab) ------------------------------------

def _hex(c: str):
    return rl_colors.HexColor(c)


if REPORTLAB_OK:
    class NumberedCanvas(rl_canvas.Canvas):
        """Canvas à deux passes : header/footer corporate + « Page x / y »."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._saved_pages: list[dict] = []

        def showPage(self):  # noqa: N802 (API ReportLab)
            self._saved_pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._saved_pages)
            for state in self._saved_pages:
                self.__dict__.update(state)
                self._draw_frame(total)
                super().showPage()
            super().save()

        def _draw_frame(self, total: int) -> None:
            w, h = A4
            # Header : bandeau prune + liseré orchidée
            self.setFillColor(_hex(PRUNE))
            self.rect(0, h - 22 * mm, w, 22 * mm, stroke=0, fill=1)
            self.setFillColor(_hex(ACCENT))
            self.rect(0, h - 23.5 * mm, w, 1.5 * mm, stroke=0, fill=1)
            self.rect(15 * mm, h - 16 * mm, 3 * mm, 9 * mm, stroke=0, fill=1)
            self.setFillColor(rl_colors.white)
            self.setFont("Helvetica-Bold", 13)
            self.drawString(21 * mm, h - 11.5 * mm, "GROUPE BPCE")
            self.setFont("Helvetica", 8.5)
            self.drawString(21 * mm, h - 15.5 * mm, "Direction Stratégie & Développement International")
            self.setFont("Helvetica-Bold", 8.5)
            self.drawRightString(w - 15 * mm, h - 11.5 * mm, "NOTE DE SYNTHÈSE EXÉCUTIVE")
            self.setFont("Helvetica", 8)
            self.drawRightString(w - 15 * mm, h - 15.5 * mm, "Document de travail — Usage interne")
            # Footer
            self.setStrokeColor(_hex(PRUNE))
            self.setLineWidth(0.6)
            self.line(15 * mm, 14 * mm, w - 15 * mm, 14 * mm)
            self.setFillColor(_hex(SLATE))
            self.setFont("Helvetica", 7.5)
            self.drawString(15 * mm, 9.5 * mm, f"{APP_NAME} — Confidentiel — Diffusion restreinte")
            self.drawRightString(w - 38 * mm, 9.5 * mm, f"Édité le {datetime.now():%d/%m/%Y à %H:%M}")
            self.setFont("Helvetica-Bold", 7.5)
            self.setFillColor(_hex(ACCENT))
            self.drawRightString(w - 15 * mm, 9.5 * mm, f"Page {self._pageNumber} / {total}")


def _pdf_styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("t", parent=base["Title"], fontName="Helvetica-Bold", fontSize=19,
                                textColor=_hex(PRUNE), alignment=TA_LEFT, spaceAfter=2, leading=23),
        "subtitle": ParagraphStyle("st", parent=base["Normal"], fontName="Helvetica", fontSize=10,
                                   textColor=_hex(SLATE), spaceAfter=10),
        "h2": ParagraphStyle("h2", parent=base["Heading2"], fontName="Helvetica-Bold", fontSize=12.5,
                             textColor=_hex(PRUNE), spaceBefore=10, spaceAfter=6,
                             borderPadding=(0, 0, 2, 0)),
        "body": ParagraphStyle("b", parent=base["Normal"], fontName="Helvetica", fontSize=9,
                               leading=12.5, textColor=_hex(INK)),
        "small": ParagraphStyle("s", parent=base["Normal"], fontName="Helvetica", fontSize=7.5,
                                leading=10, textColor=_hex(SLATE)),
        "box": ParagraphStyle("bx", parent=base["Normal"], fontName="Helvetica", fontSize=9.5,
                              leading=13.5, textColor=_hex(PRUNE)),
    }


def _pdf_table(data: list[list[Any]], col_widths: list[float], header_color: str = PRUNE,
               zebra: bool = True, extra: list | None = None) -> Table:
    tbl = Table(data, colWidths=col_widths, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), _hex(header_color)),
        ("TEXTCOLOR", (0, 0), (-1, 0), rl_colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.2),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4.5),
        ("TOPPADDING", (0, 0), (-1, -1), 4.5),
        ("LINEBELOW", (0, -1), (-1, -1), 0.8, _hex(PRUNE)),
    ]
    if zebra:
        for r in range(1, len(data)):
            if r % 2 == 0:
                style.append(("BACKGROUND", (0, r), (-1, r), _hex(BG)))
    if extra:
        style.extend(extra)
    tbl.setStyle(TableStyle(style))
    return tbl


def _pdf_bar_chart(pl: pd.DataFrame) -> Drawing:
    """Histogramme PNB vs résultat net (ReportLab graphics, sans dépendance image)."""
    d = Drawing(170 * mm, 62 * mm)
    chart = VerticalBarChart()
    chart.x, chart.y = 14 * mm, 10 * mm
    chart.width, chart.height = 150 * mm, 44 * mm
    chart.data = [list(pl["PNB"].round(1)), list(pl["Resultat_net"].round(1))]
    chart.categoryAxis.categoryNames = list(pl["Année"])
    chart.categoryAxis.labels.fontName = "Helvetica"
    chart.categoryAxis.labels.fontSize = 7.5
    chart.valueAxis.labels.fontName = "Helvetica"
    chart.valueAxis.labels.fontSize = 7
    chart.valueAxis.valueMin = min(0, float(pl["Resultat_net"].min()) * 1.2)
    chart.valueAxis.gridStrokeColor = _hex(BORDER)
    chart.valueAxis.visibleGrid = True
    chart.bars[0].fillColor = _hex(PRUNE)
    chart.bars[1].fillColor = _hex(ACCENT)
    chart.bars.strokeColor = None
    chart.barSpacing = 1.5
    chart.groupSpacing = 8
    d.add(chart)
    d.add(String(14 * mm, 57 * mm, "■ PNB post-synergies", fontName="Helvetica", fontSize=7.5,
                 fillColor=_hex(PRUNE)))
    d.add(String(52 * mm, 57 * mm, "■ Résultat net", fontName="Helvetica", fontSize=7.5,
                 fillColor=_hex(ACCENT)))
    return d


def french_date(d: datetime | None = None) -> str:
    """Date en toutes lettres, en français, indépendamment de la locale système."""
    d = d or datetime.now()
    mois = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
            "septembre", "octobre", "novembre", "décembre"]
    return f"{d.day} {mois[d.month - 1]} {d.year}"


def _fmt(x: Any, pattern: str = "{:,.1f}", na: str = "n.d.") -> str:
    if x is None or (isinstance(x, float) and (np.isnan(x) or np.isinf(x))):
        return na
    return pattern.format(x).replace(",", " ")


def generate_pdf_report(payload: dict[str, Any]) -> bytes:
    """Génère la note de synthèse exécutive (2-3 pages) et renvoie les octets PDF.

    payload attendu : country, region, latest, region_avg, macro_status,
    risk, market, checklist_ratio, target (dict), sim (résultat simulation),
    currency_label, recommendation.
    """
    if not REPORTLAB_OK:
        raise RuntimeError("ReportLab n'est pas installé (pip install reportlab).")
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=31 * mm, bottomMargin=20 * mm,
                            title=f"Note de synthèse — {payload['country']}",
                            author="Groupe BPCE — Stratégie & Développement International")
    S = _pdf_styles()
    cur = payload["currency_label"]
    story: list[Any] = []
    W = A4[0] - 30 * mm

    # --- Titre & message clé -------------------------------------------------
    story.append(Paragraph(escape(f"Opportunité de développement — {payload['country']}"), S["title"]))
    story.append(Paragraph(escape(f"{payload['region']}  •  Cible étudiée : {payload['target']['name']}"
                                  f"  •  {french_date()}"), S["subtitle"]))
    reco = payload["recommendation"]
    box = Table([[Paragraph(f"<b>Message clé — {escape(reco['verdict'])}</b><br/>{escape(reco['text'])}",
                            S["box"])]], colWidths=[W])
    box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _hex(TINT)),
        ("LINEBEFORE", (0, 0), (0, -1), 4, _hex(ACCENT)),
        ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story += [box, Spacer(1, 8)]

    sim = payload["sim"]
    kpis = [["Score risque composite", "TRI actionnaire", "ROI", "Payback", "Prix d'acquisition"],
            [f"{payload['risk']['composite']:.0f}/100 ({risk_level(payload['risk']['composite'])})",
             _fmt(sim["irr"], "{:.1f} %"), _fmt(sim["roi"], "{:.0f} %"),
             _fmt(sim["payback"], "{:.1f} ans", "> horizon"),
             f"{_fmt(sim['params']['price'], '{:,.0f}')} {cur}M"]]
    kt = Table(kpis, colWidths=[W / 5] * 5)
    kt.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica"), ("FONTSIZE", (0, 0), (-1, 0), 7.5),
        ("TEXTCOLOR", (0, 0), (-1, 0), _hex(SLATE)),
        ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"), ("FONTSIZE", (0, 1), (-1, 1), 11),
        ("TEXTCOLOR", (0, 1), (-1, 1), _hex(PRUNE)),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("BOX", (0, 0), (-1, -1), 0.5, _hex(BORDER)),
        ("LINEAFTER", (0, 0), (-2, -1), 0.5, _hex(BORDER)),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story += [kt, Spacer(1, 6)]

    # --- 1. Synthèse macro -----------------------------------------------------
    story.append(Paragraph("1. Synthèse macroéconomique & PESTEL", S["h2"]))
    latest, avg = payload["latest"], payload["region_avg"]
    rows = [["Indicateur", "Valeur", "Année", "Moy. zone", "Proj. 2026", "Source"]]
    for ind, meta in INDICATORS.items():
        lv = latest[ind]
        rows.append([meta["label"] + f" ({meta['unit']})",
                     _fmt(lv["value"], meta["fmt"]), str(lv["year"]),
                     _fmt(avg.get(ind), meta["fmt"]), _fmt(lv["proj"], meta["fmt"], "—"),
                     lv["source"]])
    story.append(_pdf_table(rows, [58 * mm, 22 * mm, 14 * mm, 22 * mm, 22 * mm, W - 138 * mm],
                            extra=[("ALIGN", (5, 0), (5, -1), "LEFT")]))
    story.append(Spacer(1, 5))
    for line in payload["macro_comments"]:
        story.append(Paragraph("• " + escape(line), S["body"]))

    # --- 2. Compliance & risques ---------------------------------------------
    story.append(Paragraph("2. Compliance, KYC & Risk Index", S["h2"]))
    risk = payload["risk"]
    rrows = [["Dimension", "Score /100", "Niveau", "Principaux déterminants"]]
    comp, mk = risk["components"], payload["market"]
    drivers = {
        "Risque Pays": f"Spread {mk['spread_bps']:.0f} pb ; stabilité politique {latest['wgi_pv']['value']:+.2f}",
        "Risque de Change": f"Volatilité EUR/{PROFILES[payload['iso3']]['currency']} {mk['fx_vol']:.1f} % ; "
                            f"inflation {latest['inflation']['value']:.1f} %",
        "Risque KYC / Sanctions": f"Corruption (WGI) {latest['wgi_cc']['value']:+.2f} ; "
                                  f"checklist {payload['checklist_ratio'] * 100:.0f} % complétée",
    }
    extra = []
    for i, (k, v) in enumerate(risk["scores"].items(), start=1):
        lvl = risk["levels"][k]
        rrows.append([k, f"{v:.0f}", lvl, drivers[k]])
        extra += [("BACKGROUND", (2, i), (2, i), _hex(RISK_COLORS[lvl])),
                  ("TEXTCOLOR", (2, i), (2, i), rl_colors.white),
                  ("FONTNAME", (2, i), (2, i), "Helvetica-Bold"),
                  ("ALIGN", (2, i), (2, i), "CENTER")]
    extra.append(("ALIGN", (3, 0), (3, -1), "LEFT"))
    story.append(_pdf_table(rrows, [42 * mm, 20 * mm, 22 * mm, W - 84 * mm], header_color=ACCENT,
                            zebra=False, extra=extra))
    top = sorted(risk["matrix"], key=lambda r: r[1] * r[2], reverse=True)[:3]
    story.append(Spacer(1, 4))
    story.append(Paragraph("<b>Risques prioritaires (probabilité x impact) :</b> " +
                           escape(" ; ".join(f"{n} ({p * i:.1f}/25)" for n, p, i in top)), S["body"]))
    story.append(Paragraph(f"<b>Superviseur compétent :</b> {escape(PROFILES[payload['iso3']]['supervisor'])}",
                           S["body"]))

    story.append(PageBreak())

    # --- 3. Cible M&A ------------------------------------------------------------
    story.append(Paragraph("3. Cible M&amp;A sélectionnée", S["h2"]))
    t = payload["target"]
    trows = [["Société", "Catégorie", "Capi. (M$)", "P/E", "P/B", "ROE", "Rdt div.", "Source"],
             [Paragraph(escape(t["name"]), S["body"]), escape(str(t.get("category", ""))),
              _fmt(t.get("market_cap_usd_m"), "{:,.0f}"), _fmt(t.get("pe"), "{:.1f}x"),
              _fmt(t.get("pb"), "{:.2f}x"), _fmt(t.get("roe"), "{:.1f} %"),
              _fmt(t.get("div_yield"), "{:.1f} %"), Paragraph(escape(str(t.get("source", ""))), S["small"])]]
    story.append(_pdf_table(trows, [44 * mm, 26 * mm, 20 * mm, 14 * mm, 14 * mm, 15 * mm, 15 * mm,
                                    W - 148 * mm], extra=[("ALIGN", (7, 0), (7, -1), "LEFT")]))
    story.append(Spacer(1, 4))
    story.append(Paragraph(escape(payload["target_comment"]), S["body"]))

    # --- 4. Simulation -----------------------------------------------------------
    story.append(Paragraph("4. Projections financières post-acquisition", S["h2"]))
    prm = sim["params"]
    hyp = [["Hypothèse", "Valeur", "Hypothèse", "Valeur"],
           ["Prix d'acquisition", f"{_fmt(prm['price'], '{:,.0f}')} {cur}M", "Fonds propres / Dette",
            f"{prm['equity_pct']:.0f} % / {100 - prm['equity_pct']:.0f} %"],
           ["Synergies de revenus", f"{prm['rev_syn']:.1f} % du PNB", "Synergies de coûts",
            f"{prm['cost_syn']:.1f} % des charges"],
           ["Taux de financement", f"{prm['rate']:.2f} %", "Horizon", f"{prm['horizon']:.0f} ans"],
           ["PNB initial", f"{_fmt(prm['pnb0'], '{:,.0f}')} {cur}M", "Croissance organique",
            f"{prm['growth']:.1f} %"],
           ["Coef. d'exploitation", f"{prm['cir']:.0f} %", "Multiple de sortie", f"{prm['exit_pe']:.1f}x P/E"]]
    story.append(_pdf_table(hyp, [40 * mm, W / 2 - 40 * mm, 40 * mm, W / 2 - 40 * mm],
                            extra=[("ALIGN", (2, 1), (2, -1), "LEFT"),
                                   ("FONTNAME", (2, 1), (2, -1), "Helvetica-Bold"),
                                   ("FONTNAME", (0, 1), (0, -1), "Helvetica-Bold")]))
    story.append(Spacer(1, 6))
    pl = sim["pl"].head(max(5, int(prm["horizon"])))
    head = ["(" + cur + "M)"] + list(pl["Année"])
    lines = [("PNB post-synergies", "PNB"), ("dont synergies de revenus", "Synergies_revenus"),
             ("Charges d'exploitation", "Charges"), ("Coûts d'intégration", "Couts_integration"),
             ("Résultat brut d'exploitation", "RBE"), ("Coût du risque", "Cout_du_risque"),
             ("Frais financiers (dette)", "Frais_financiers"), ("Résultat net", "Resultat_net"),
             ("Coef. d'exploitation (%)", "Coef_exploitation"), ("Flux actionnaire BPCE", "Flux_BPCE")]
    prow = [head] + [[lbl] + [_fmt(v, "{:,.0f}") for v in pl[col]] for lbl, col in lines]
    ncol = len(head) - 1
    story.append(_pdf_table(prow, [52 * mm] + [(W - 52 * mm) / ncol] * ncol,
                            extra=[("FONTNAME", (0, 8), (-1, 8), "Helvetica-Bold"),
                                   ("TEXTCOLOR", (0, 8), (-1, 8), _hex(ACCENT)),
                                   ("FONTSIZE", (0, 0), (-1, -1), 7.6 if ncol > 6 else 8.2)]))
    story.append(Spacer(1, 4))
    story.append(KeepTogether([_pdf_bar_chart(pl), Paragraph(
        f"<b>Rendement :</b> TRI {_fmt(sim['irr'], '{:.1f} %')} — ROI {_fmt(sim['roi'], '{:.0f} %')} — "
        f"MOIC {_fmt(sim['moic'], '{:.2f}x')} — Payback {_fmt(sim['payback'], '{:.1f} ans', '> horizon')} — "
        f"Fonds propres investis {_fmt(sim['equity'], '{:,.0f}')} {cur}M, dette {_fmt(sim['debt'], '{:,.0f}')} {cur}M.",
        S["body"])]))

    # --- 5. Sources & avertissement -------------------------------------------------
    story.append(Paragraph("5. Provenance des données & avertissement", S["h2"]))
    prov = "  •  ".join(f"{k} : {v}" for k, v in payload["api_status"].items())
    story.append(Paragraph(escape(prov), S["small"]))
    story.append(Paragraph(
        "Sources mobilisées : Banque Mondiale (WDI, WGI), FMI (WEO DataMapper), FRED (Federal Reserve "
        "Bank of St. Louis), BCE (Data Portal), Yahoo Finance. « Simulé » désigne des estimations de "
        "secours générées localement lorsque la source officielle est indisponible. Ce document est un "
        "outil d'aide à la décision : il ne constitue ni un conseil en investissement ni une évaluation "
        "officielle et doit être validé par les équipes Finance, Risques et Conformité.", S["small"]))

    doc.build(story, canvasmaker=NumberedCanvas)
    return buf.getvalue()


# =============================================================================
# 4. INTERFACE UTILISATEUR
# =============================================================================

# Clés de widgets à conserver lors de la navigation entre modules.
PERSIST_KEYS = (["target_ticker", "ma_types", "ma_min_roe", "ma_extra", "sim_currency",
                 "sim_price", "sim_equity_pct", "sim_rev_syn", "sim_cost_syn", "sim_rate",
                 "sim_horizon", "sim_pnb0", "sim_growth", "sim_cir", "sim_cor", "sim_tax",
                 "sim_exit_pe", "sim_payout", "macro_ind", "macro_smooth"]
                + [f"chk_{c[0]}" for c in COMPLIANCE_CHECKLIST])


def persist_widget_state() -> None:
    """Empêche Streamlit d'effacer l'état des widgets des modules non affichés."""
    for k in PERSIST_KEYS:
        if k in st.session_state:
            st.session_state[k] = st.session_state[k]


def kpi_card(label: str, value: str, sub: str = "", delta: float | None = None,
             good_if_up: bool = True, variant: str = "") -> str:
    """Carte KPI HTML (ombre douce, survol animé, delta coloré)."""
    delta_html = ""
    if delta is not None and not np.isnan(delta):
        up = delta >= 0
        good = up == good_if_up
        arrow = "▲" if up else "▼"
        delta_html = (f'<span class="kpi-delta {"up" if good else "down"}">{arrow} '
                      f'{abs(delta):.2f}</span> ')
    return (f'<div class="kpi-card {variant}"><div class="kpi-label">{label}</div>'
            f'<div class="kpi-value">{value}</div><div class="kpi-sub">{delta_html}{sub}</div></div>')


def render_cards(cards: list[str]) -> None:
    cols = st.columns(len(cards))
    for col, html in zip(cols, cards):
        col.markdown(html, unsafe_allow_html=True)


def hero(kicker: str, title: str, subtitle: str, tape: list[tuple[str, str, str]] | None = None) -> None:
    """Bandeau de module + ruban « terminal » (libellé, valeur, style up/down/hl)."""
    html = (f'<div class="hero"><div class="kicker">{kicker}</div>'
            f'<div class="title">{title}</div><div class="sub">{subtitle}</div></div>')
    cells = "".join(f'<div class="cell"><div class="lbl">{lbl}</div><div class="val {cls}">{val}</div></div>'
                    for lbl, val, cls in (tape or []))
    html += f'<div class="tape">{cells}</div>' if cells else ""
    st.markdown(html, unsafe_allow_html=True)


def market_tape(ctx: dict) -> list[tuple[str, str, str]]:
    """Ruban de données clés du pays, commun à tous les modules."""
    lt, mk, p = ctx["latest"], ctx["market"], ctx["profile"]
    g = lt["gdp_growth"]["value"]
    tape = [
        ("Pays", f"{p['iso2']} · {p['currency']}", "hl"),
        ("PIB", f"{lt['gdp_usd']['value']:,.0f} Md$".replace(",", " "), ""),
        ("Croissance", f"{g:+.1f}%", "up" if g >= 0 else "down"),
        ("Inflation", f"{lt['inflation']['value']:.1f}%", "down" if lt["inflation"]["value"] > 4 else ""),
        ("Spread souv.", f"{mk['spread_bps']:.0f} pb", "down" if mk["spread_bps"] > 300 else ""),
        ("Vol. FX/EUR", f"{mk['fx_vol']:.1f}%", ""),
    ]
    if mk.get("policy_rate") is not None:
        tape.append(("Taux directeur", f"{mk['policy_rate']:.2f}%", ""))
    lvl = risk_level(ctx["risk"]["composite"])
    tape.append(("Risque composite", f"{ctx['risk']['composite']:.0f} · {lvl}",
                 "up" if lvl == "Faible" else "down" if lvl in ("Élevé", "Critique") else "hl"))
    return tape


def section(title: str) -> None:
    st.markdown(f'<div class="section-title">{title}</div>', unsafe_allow_html=True)


def separator() -> None:
    st.markdown('<div class="sep"></div>', unsafe_allow_html=True)


MODULES = ["Dashboard PESTEL & Macro", "Compliance, KYC & Risques",
           "Cartographie M&A", "Simulateur Post-Acquisition", "Note de Synthèse PDF"]


def render_sidebar() -> tuple[str, str, str]:
    """Sidebar : badge, sélecteur géographique hiérarchique, navigation, statut API."""
    with st.sidebar:
        st.markdown(
            '<div class="brand"><div class="row"><span class="mark">BPCE</span><div>'
            '<div class="t1">Groupe BPCE</div><div class="t2">International Strategy Hub</div>'
            '</div></div></div>', unsafe_allow_html=True)
        st.markdown('<div class="side-h">Périmètre géographique</div>', unsafe_allow_html=True)
        region = st.selectbox("Zone géographique / Bloc économique", list(REGIONS), key="region")
        iso_opts = REGIONS[region]
        iso3 = st.selectbox(
            "Pays cible", iso_opts, key=f"country_{region}",
            format_func=lambda i: f"{PROFILES[i]['name']}  ·  {PROFILES[i]['iso2']}")
        st.divider()
        st.markdown('<div class="side-h">Modules</div>', unsafe_allow_html=True)
        module = st.radio("Navigation", MODULES, key="module", label_visibility="collapsed")
        st.divider()

        with st.expander("Statut des connexions API", expanded=False):
            reg = st.session_state.get("api_status", {})
            colors = {"live": "#7EE2A8", "partial": "#F2C46D", "fallback": "#FF9C8F"}
            labels = {"live": "En direct", "partial": "Partiel", "fallback": "Mode secours"}
            for src in ["Banque Mondiale", "FMI", "FRED", "BCE", "Yahoo Finance"]:
                stt = reg.get(src)
                color = colors.get(stt, "#9C8A99")
                label = labels.get(stt, "Non sollicitée")
                st.markdown(f'<div class="api-row"><span><span class="dot" style="background:{color}">'
                            f'</span>{src}</span><span>{label}</span></div>', unsafe_allow_html=True)
            st.caption(f"Cache : {CACHE_TTL // 60} min • Timeout : {HTTP_TIMEOUT} s")
            if st.button("Rafraîchir les données", icon=":material/refresh:", width="stretch"):
                st.cache_data.clear()
                st.session_state["api_status"] = {}
                st.session_state["toasted"] = set()
                st.rerun()
        guide = user_guide_bytes()
        if guide:
            st.download_button("Guide utilisateur (PDF)", guide, icon=":material/menu_book:",
                               file_name="Guide_utilisateur_BPCE_Strategy_Hub.pdf",
                               mime="application/pdf", width="stretch")
        st.caption(f"© Groupe BPCE — {datetime.now():%Y} • Usage interne")
    return region, iso3, module


@st.cache_data(show_spinner=False)
def user_guide_bytes() -> bytes | None:
    """Mémorandum « Guide utilisateur » (module guide_utilisateur.py, facultatif)."""
    try:
        from guide_utilisateur import generate_user_guide_pdf
        return generate_user_guide_pdf()
    except Exception:  # module absent ou ReportLab indisponible : bouton masqué
        return None


# ---------- Module 1 : PESTEL & Macro -------------------------------------------

def region_average(df: pd.DataFrame, iso3s: list[str]) -> dict[str, float]:
    """Moyenne simple de la zone (dernière valeur réalisée de chaque pays)."""
    per_country = [latest_values(df, i) for i in iso3s]
    return {ind: float(np.nanmean([pc[ind]["value"] for pc in per_country])) if per_country else np.nan
            for ind in INDICATORS}


def pestel_assessment(iso3: str, latest: dict) -> list[tuple[str, str, int, str]]:
    """Évaluation PESTEL automatisée (note 1-5, 5 = environnement favorable)."""
    p = PROFILES[iso3]
    pv, cc = latest["wgi_pv"]["value"], latest["wgi_cc"]["value"]
    g, inf = latest["gdp_growth"]["value"], latest["inflation"]["value"]
    un = latest["unemployment"]["value"]

    def note(x: float, bounds: tuple[float, float, float, float]) -> int:
        return 1 + sum(x > b for b in bounds)

    pol = note(pv, (-1.0, -0.3, 0.3, 0.9))
    eco = max(1, min(5, round((note(g, (0.5, 1.5, 3.0, 5.0)) + note(-abs(inf - 2), (-10, -4, -2, -0.8))) / 2)))
    soc = note(-un, (-12, -8, -5.5, -3.5))
    tec = int(p["digital"])
    env = 6 - int(p["climate"])
    leg = note(cc, (-0.8, -0.2, 0.5, 1.3))
    return [
        ("P", "Politique", pol, f"Stabilité politique WGI {pv:+.2f} ({latest['wgi_pv']['year']})."),
        ("E", "Économique", eco, f"Croissance {g:+.1f} %, inflation {inf:.1f} % ({latest['inflation']['year']})."),
        ("S", "Social", soc, f"Chômage {un:.1f} % de la population active."),
        ("T", "Technologique", tec, f"Maturité digitale estimée {tec}/5 (paiements, néobanques)."),
        ("E", "Environnemental", env, f"Exposition climatique {p['climate']}/5 — risques physiques & transition."),
        ("L", "Légal", leg, f"Contrôle corruption WGI {cc:+.2f} ; superviseur : {p['supervisor']}."),
    ]


def macro_commentary(iso3: str, latest: dict, avg: dict) -> list[str]:
    """Commentaires automatiques à partir des écarts pays vs zone."""
    p = PROFILES[iso3]
    g, ga = latest["gdp_growth"]["value"], avg["gdp_growth"]
    i, ia = latest["inflation"]["value"], avg["inflation"]
    lines = [
        f"{p['name']} : PIB de {latest['gdp_usd']['value']:,.0f} Md$ ({latest['gdp_usd']['year']}), "
        f"croissance réelle de {g:+.1f} % vs {ga:+.1f} % en moyenne de zone "
        f"({'surperformance' if g > ga else 'sous-performance'} de {abs(g - ga):.1f} pt).".replace(",", " "),
        f"Inflation à {i:.1f} % ({'au-dessus' if i > ia else 'en dessous'} de la moyenne de zone, {ia:.1f} %) ; "
        f"chômage à {latest['unemployment']['value']:.1f} %.",
        f"Balance commerciale de {latest['trade_balance']['value']:+.1f} % du PIB ; stabilité politique WGI "
        f"{latest['wgi_pv']['value']:+.2f} et contrôle de la corruption {latest['wgi_cc']['value']:+.2f}.",
    ]
    if latest["gdp_growth"]["proj"] is not None:
        lines.append(f"Projection 2026 : croissance {latest['gdp_growth']['proj']:+.1f} %"
                     + (f", inflation {latest['inflation']['proj']:.1f} %." if latest["inflation"]["proj"]
                        is not None else "."))
    return lines


def timeseries_chart(df: pd.DataFrame, iso3: str, ind: str, smooth: bool) -> go.Figure:
    """Courbe lissée 2015-2026 avec zone d'incertitude (±1σ, élargie en projection)."""
    meta = INDICATORS[ind]
    s = df[(df["iso3"] == iso3) & (df["indicator"] == ind)].sort_values("year")
    fig = go.Figure()
    if s.empty:
        return style_fig(fig)
    x, y = s["year"].values, s["value"].values.astype(float)
    sigma = float(np.nanstd(np.diff(y))) if len(y) > 2 else abs(np.nanmean(y)) * 0.05
    sigma = max(sigma * 0.6, 1e-6)
    last_actual = s[~s["projection"]]["year"].max() if (~s["projection"]).any() else x.max()
    horizon = np.clip(x - last_actual, 0, None)
    band = sigma * (1 + 0.8 * horizon)
    fig.add_trace(go.Scatter(x=np.concatenate([x, x[::-1]]),
                             y=np.concatenate([y + band, (y - band)[::-1]]),
                             fill="toself", fillcolor="rgba(74,40,69,0.10)", line=dict(width=0),
                             hoverinfo="skip", name="Zone d'incertitude (±1σ)"))
    act = s[~s["projection"]]
    proj = s[s["projection"]]
    fig.add_trace(go.Scatter(x=act["year"], y=act["value"], mode="lines+markers", name="Réalisé",
                             line=dict(color=PRUNE, width=3, shape="spline" if smooth else "linear"),
                             marker=dict(size=7, color=PRUNE, line=dict(color="#fff", width=1.5)),
                             customdata=act["source"],
                             hovertemplate="%{x} : %{y:.2f} " + meta["unit"] + "<br>%{customdata}<extra></extra>"))
    if not proj.empty:
        link = pd.concat([act.tail(1), proj])
        fig.add_trace(go.Scatter(x=link["year"], y=link["value"], mode="lines+markers", name="Projection",
                                 line=dict(color=ACCENT, width=3, dash="dot",
                                           shape="spline" if smooth else "linear"),
                                 marker=dict(size=8, symbol="diamond", color=ACCENT),
                                 customdata=link["source"],
                                 hovertemplate="%{x} : %{y:.2f} " + meta["unit"] +
                                               "<br>%{customdata}<extra></extra>"))
        fig.add_vrect(x0=last_actual + 0.5, x1=x.max() + 0.5, fillcolor=ACCENT, opacity=0.04,
                      line_width=0, annotation_text="Projections", annotation_position="top left",
                      annotation_font=dict(color=ACCENT, size=11))
    if smooth and len(act) >= 3:
        ma = act["value"].rolling(3, center=True, min_periods=2).mean()
        fig.add_trace(go.Scatter(x=act["year"], y=ma, mode="lines", name="Moyenne mobile 3 ans",
                                 line=dict(color=SLATE, width=1.5, dash="dash")))
    fig.update_xaxes(dtick=1)
    fig.update_yaxes(title_text=meta["unit"])
    return style_fig(fig, 430, f"{meta['label']} — {PROFILES[iso3]['name']} ({YEARS[0]}-{YEARS[-1]})")


def module_macro(ctx: dict) -> None:
    iso3, p, df, latest, avg = ctx["iso3"], ctx["profile"], ctx["macro"], ctx["latest"], ctx["avg"]
    hero("Module 1 • Dashboard PESTEL & Macroéconomie",
         f"{p['name']} — Environnement macroéconomique",
         f"{ctx['region']} • Banque Mondiale (WDI/WGI), FMI (WEO), BCE • Horizon {YEARS[0]}-{YEARS[-1]}",
         tape=market_tape(ctx))
    source_badges(ctx["macro_status"])

    def delta(ind):
        lv = latest[ind]
        return None if lv["prev"] is None else lv["value"] - lv["prev"]

    def sub(ind):
        lv = latest[ind]
        return f"{lv['year']} • zone : {INDICATORS[ind]['fmt'].format(avg[ind])}"

    render_cards([
        kpi_card("PIB nominal", f"{latest['gdp_usd']['value']:,.0f} Md$".replace(",", " "),
                 sub("gdp_usd"), None),
        kpi_card("Croissance PIB", f"{latest['gdp_growth']['value']:+.1f} %", sub("gdp_growth"),
                 delta("gdp_growth")),
        kpi_card("Inflation", f"{latest['inflation']['value']:.1f} %", sub("inflation"),
                 delta("inflation"), good_if_up=False),
        kpi_card("Chômage", f"{latest['unemployment']['value']:.1f} %", sub("unemployment"),
                 delta("unemployment"), good_if_up=False),
        kpi_card("Balance commerciale", f"{latest['trade_balance']['value']:+.1f} % PIB",
                 sub("trade_balance"), delta("trade_balance"), variant="alt"),
        kpi_card("Stabilité politique", f"{latest['wgi_pv']['value']:+.2f}",
                 f"WGI {latest['wgi_pv']['year']} • échelle -2,5/+2,5", delta("wgi_pv"), variant="alt"),
    ])
    st.write("")
    mk = ctx["market"]
    extra_cards = [kpi_card("Spread souverain", f"{mk['spread_bps']:.0f} pb", mk["spread_src"], variant="alt"),
                   kpi_card("Volatilité change vs EUR", f"{mk['fx_vol']:.1f} %", mk["fx_src"], variant="alt")]
    if mk["policy_rate"] is not None:
        extra_cards.insert(0, kpi_card("Taux directeur de référence", f"{mk['policy_rate']:.2f} %",
                                       mk["policy_label"], variant="alt"))
    if latest["gdp_growth"]["proj"] is not None:
        extra_cards.append(kpi_card("Croissance projetée 2026", f"{latest['gdp_growth']['proj']:+.1f} %",
                                    "FMI WEO / estimation"))
    render_cards(extra_cards)
    separator()

    tabs = st.tabs(["Analyse PESTEL", "Évolution temporelle", "Comparatif régional", "Données brutes"])
    with tabs[0]:
        section("Grille PESTEL — notation automatisée (5 = environnement favorable)")
        cols = st.columns(6)
        for col, (letter, name, score, txt) in zip(cols, pestel_assessment(iso3, latest)):
            col.markdown(f'<div class="pestel"><div class="letter">{letter}</div><div class="name">{name}</div>'
                         f'<div class="stars">{"★" * score}{"☆" * (5 - score)}</div>'
                         f'<div class="txt">{txt}</div></div>', unsafe_allow_html=True)
        st.write("")
        section("Lecture stratégique")
        for line in macro_commentary(iso3, latest, avg):
            st.markdown(f"- {line}")

    with tabs[1]:
        c1, c2 = st.columns([3, 1])
        ind = c1.selectbox("Indicateur", list(INDICATORS), key="macro_ind",
                           format_func=lambda k: f"{INDICATORS[k]['label']} ({INDICATORS[k]['unit']})")
        smooth = c2.toggle("Lissage (spline + MM3)", value=True, key="macro_smooth")
        st.plotly_chart(timeseries_chart(df, iso3, ind, smooth), width="stretch", theme=None, config=PLOTLY_CONFIG)
        st.caption("Zone d'incertitude : ±1σ des variations annuelles historiques, élargie sur l'horizon de "
                   "projection. Les projections proviennent du FMI (WEO) ou du mode secours.")

    with tabs[2]:
        ind2 = st.selectbox("Indicateur comparé", list(INDICATORS), key="cmp_ind",
                            format_func=lambda k: INDICATORS[k]["label"])
        meta = INDICATORS[ind2]
        sub_df = df[df["indicator"] == ind2]
        pivot = sub_df.pivot_table(index="year", columns="iso3", values="value")
        zone_mean = pivot.mean(axis=1)
        fig = go.Figure()
        if iso3 in pivot:
            fig.add_bar(x=pivot.index, y=pivot[iso3], name=p["name"], marker_color=PRUNE,
                        hovertemplate="%{x} : %{y:.2f}<extra>" + p["name"] + "</extra>")
        fig.add_bar(x=zone_mean.index, y=zone_mean, name="Moyenne de la zone", marker_color=ACCENT,
                    opacity=0.85, hovertemplate="%{x} : %{y:.2f}<extra>Moyenne zone</extra>")
        fig.update_layout(barmode="group", bargap=0.25)
        fig.update_xaxes(dtick=1)
        st.plotly_chart(style_fig(fig, 400, f"{meta['label']} : {p['name']} vs moyenne {ctx['region']}"),
                        width="stretch", theme=None, config=PLOTLY_CONFIG)

        snap = pd.DataFrame([{"Pays": PROFILES[i]["name"], "Valeur": latest_values(df, i)[ind2]["value"],
                              "iso3": i} for i in ctx["region_isos"]]).sort_values("Valeur")
        fig2 = go.Figure(go.Bar(
            x=snap["Valeur"], y=snap["Pays"], orientation="h",
            marker_color=[ACCENT if i == iso3 else MIST for i in snap["iso3"]],
            text=[meta["fmt"].format(v) for v in snap["Valeur"]], textposition="outside"))
        fig2.add_vline(x=snap["Valeur"].mean(), line_dash="dash", line_color=PRUNE,
                       annotation_text="Moyenne zone", annotation_font_color=PRUNE)
        st.plotly_chart(style_fig(fig2, 320, f"Positionnement intra-zone — dernière valeur ({meta['unit']})"),
                        width="stretch", theme=None, config=PLOTLY_CONFIG)

    with tabs[3]:
        wide = (df[df["iso3"] == iso3]
                .assign(Indicateur=lambda d: d["indicator"].map(lambda k: INDICATORS[k]["label"]))
                .pivot_table(index="Indicateur", columns="year", values="value").round(2))
        st.dataframe(wide, width="stretch")
        st.download_button("Exporter les données (CSV)", df.to_csv(index=False).encode("utf-8"), icon=":material/download:",
                           file_name=f"macro_{iso3}.csv", mime="text/csv")


# ---------- Module 2 : Compliance & Risques -----------------------------------

def gauge(title: str, value: float) -> go.Figure:
    lvl = risk_level(value)
    fig = go.Figure(go.Indicator(
        mode="gauge+number", value=value,
        number=dict(suffix=" /100", valueformat=".0f", font=dict(size=26, color=PRUNE)),
        title=dict(text=f"<b>{title}</b><br><span style='font-size:13px;color:{RISK_COLORS[lvl]}'>"
                        f"Niveau : {lvl}</span>", font=dict(size=15, color=PRUNE)),
        gauge=dict(
            axis=dict(range=[0, 100], tickwidth=1, tickcolor=MIST),
            bar=dict(color=PRUNE, thickness=0.28),
            bgcolor="white", borderwidth=0,
            steps=[dict(range=[0, 25], color="#DDF0E5"), dict(range=[25, 50], color="#F7EBCF"),
                   dict(range=[50, 75], color="#F5DCCB"), dict(range=[75, 100], color="#F3D0CC")],
            threshold=dict(line=dict(color=RISK_COLORS[lvl], width=4), thickness=0.85, value=value))))
    fig = style_fig(fig, 270)
    fig.update_layout(margin=dict(l=38, r=38, t=80, b=10))
    return fig


def risk_matrix_chart(matrix: list[tuple[str, float, float]]) -> go.Figure:
    fig = go.Figure()
    zones = [(1, 1, 5, 5, "#F8E3E1"), (1, 1, 3.4, 3.4, "#F7EBCF"), (1, 1, 2.2, 2.2, "#DDF0E5")]
    for x0, y0, x1, y1, color in zones:
        fig.add_shape(type="rect", x0=x0, y0=y0, x1=x1, y1=y1, fillcolor=color, line_width=0, layer="below")
    names, probs, imps = zip(*matrix)
    crit = [p * i for p, i in zip(probs, imps)]
    fig.add_trace(go.Scatter(
        x=probs, y=imps, mode="markers+text", text=names, textposition="top center",
        textfont=dict(size=11, color=PRUNE),
        marker=dict(size=[12 + c * 1.4 for c in crit], color=crit,
                    colorscale=[[0, "#2F855A"], [0.45, "#B7791F"], [0.7, "#C05621"], [1, "#B42318"]],
                    cmin=1, cmax=25, line=dict(color="#fff", width=2),
                    colorbar=dict(title="Criticité", thickness=12)),
        hovertemplate="<b>%{text}</b><br>Probabilité : %{x:.1f}/5<br>Impact : %{y:.1f}/5<extra></extra>"))
    fig.update_xaxes(range=[1, 5.2], title_text="Probabilité d'occurrence (1-5)", dtick=1)
    fig.update_yaxes(range=[1, 5.3], title_text="Impact potentiel (1-5)", dtick=1)
    return style_fig(fig, 520, "Matrice des risques — Impact x Probabilité")


def module_risk(ctx: dict) -> None:
    iso3, p, latest, market = ctx["iso3"], ctx["profile"], ctx["latest"], ctx["market"]
    hero("Module 2 • Compliance, KYC & Risk Index",
         f"{p['name']} — Cartographie des risques",
         f"WGI (proxy CPI), FRED (spreads, stress EM), BCE, Yahoo Finance (change) • Superviseur : {p['supervisor']}",
         tape=market_tape(ctx))
    source_badges({**ctx["macro_status"], **ctx["market_status"]})
    risk = ctx["risk"]

    tabs = st.tabs(["Jauges de risque", "Matrice des risques", "Checklist conformité"])
    with tabs[0]:
        cols = st.columns(3)
        for col, (name, score) in zip(cols, risk["scores"].items()):
            col.plotly_chart(gauge(name, score), width="stretch", theme=None, config=PLOTLY_CONFIG)
        lvl = risk_level(risk["composite"])
        render_cards([
            kpi_card("Score composite", f"{risk['composite']:.0f} / 100",
                     f'<span class="level-pill" style="background:{RISK_COLORS[lvl]}">{lvl}</span>'),
            kpi_card("Spread souverain", f"{market['spread_bps']:.0f} pb", market["spread_src"], variant="alt"),
            kpi_card("Contrôle corruption (WGI)", f"{latest['wgi_cc']['value']:+.2f}",
                     "Proxy indice CPI Transparency International", variant="alt"),
            kpi_card("Stress EM (ICE BofA OAS)",
                     f"{market['em_oas']:.0f} pb" if market["em_oas"] is not None else "n.d.",
                     "FRED BAMLEMCBPIOAS" if market["em_oas"] is not None else "Source indisponible",
                     variant="alt"),
        ])
        if market.get("yield_hist") is not None:
            yh = market["yield_hist"]
            fig = go.Figure()
            fig.add_scatter(x=yh.index, y=yh["Pays"], name=f"OAT/10 ans {p['name']}", line=dict(color=PRUNE))
            fig.add_scatter(x=yh.index, y=yh["Bund"], name="Bund 10 ans", line=dict(color=ACCENT, dash="dot"))
            st.plotly_chart(style_fig(fig, 300, "Taux souverains 10 ans (FRED / OCDE)"), width="stretch",
                            theme=None, config=PLOTLY_CONFIG)
        with st.expander("Méthodologie de scoring", icon=":material/functions:"):
            c = risk["components"]
            st.markdown(
                f"""
| Dimension | Composante | Sous-score |
|---|---|---|
| Risque Pays | Stabilité politique (30 %) | {c['pv']:.0f} |
| | Spread souverain (35 %) | {c['spread']:.0f} |
| | Gouvernance / corruption (15 %) | {c['cc']:.0f} |
| | Déséquilibres macro (20 %) | {c['macro']:.0f} |
| Risque de Change | Volatilité EUR/devise (70 %) | {c['fx']:.0f} |
| | Différentiel d'inflation (30 %) | {c['infl_diff']:.0f} |
| Risque KYC / Sanctions | Base juridictionnelle indicative + corruption | {c['kyc_raw']:.0f} |

Le score KYC est atténué jusqu'à 25 % selon l'avancement de la checklist de conformité.
La base juridictionnelle KYC est **indicative** : vérifier systématiquement les listes GAFI,
UE, ONU et OFAC en vigueur.""")

    with tabs[1]:
        c1, c2 = st.columns([2.2, 1])
        c1.plotly_chart(risk_matrix_chart(risk["matrix"]), width="stretch", theme=None, config=PLOTLY_CONFIG)
        with c2:
            section("Top risques (criticité)")
            ranked = sorted(risk["matrix"], key=lambda r: r[1] * r[2], reverse=True)
            for name, prob, imp in ranked[:6]:
                crit = prob * imp
                lvl = "Critique" if crit >= 17 else "Élevé" if crit >= 11 else "Modéré" if crit >= 6 else "Faible"
                st.markdown(f'<div class="panel" style="padding:10px 14px;margin-bottom:8px">'
                            f'<b style="color:{PRUNE}">{name}</b><br/>'
                            f'<span class="level-pill" style="background:{RISK_COLORS[lvl]}">{lvl}</span> '
                            f'<small>P {prob:.1f} × I {imp:.1f} = {crit:.1f}/25</small></div>',
                            unsafe_allow_html=True)

    with tabs[2]:
        section("Checklist dynamique de conformité bancaire")
        notes = {
            "FATCA": ("Juridiction US : la cible est un établissement US — régime FATCA domestique."
                      if iso3 == "USA" else "Vérifier l'accord intergouvernemental (IGA) applicable."),
            "CRS": ("Les États-Unis ne participent pas au CRS (régime FATCA) : diligences spécifiques."
                    if iso3 == "USA" else "Vérifier le statut de juridiction participante sur le portail OCDE."),
            "LCB-FT": ("UE : Règlement AMLR (UE) 2024/1624 applicable à partir de juillet 2027, supervision AMLA."
                       if iso3 in EU_MEMBERS else
                       "UEMOA : loi uniforme LBC/FT/FP et contrôle de la CENTIF nationale."
                       if p["currency"] == "XOF" else
                       "Appliquer le standard Groupe BPCE (le plus exigeant entre local et France)."),
            "Sanctions": "Filtrage systématique ; documenter tout lien avec juridictions sous embargo.",
            "GAFI": "Consulter la dernière publication plénière GAFI (fatf-gafi.org).",
            "Prudentiel": f"Autorité(s) : {p['supervisor']}" + (" + BCE (MSU) pour le Groupe." if iso3 not in EURO_AREA else "."),
            "Données": "RGPD (UE) ; analyser les transferts et la localisation des données.",
        }
        regimes = list(dict.fromkeys(c[1] for c in COMPLIANCE_CHECKLIST))
        cols = st.columns(2)
        for idx, regime in enumerate(regimes):
            with cols[idx % 2]:
                st.markdown(f'<div class="panel" style="padding:12px 16px">'
                            f'<b style="color:{ACCENT}">{regime}</b> '
                            f'<small style="color:#55505A">— {notes.get(regime, "")}</small></div>',
                            unsafe_allow_html=True)
                for cid, reg, label in COMPLIANCE_CHECKLIST:
                    if reg == regime:
                        st.checkbox(label, key=f"chk_{cid}")
        done = ctx["checklist_ratio"]
        st.progress(done, text=f"Avancement conformité : {done * 100:.0f} % "
                               f"({round(done * len(COMPLIANCE_CHECKLIST))}/{len(COMPLIANCE_CHECKLIST)} points)")
        st.caption("L'avancement de la checklist réduit le score KYC résiduel (jusqu'à -25 %).")


# ---------- Module 3 : Cartographie M&A -----------------------------------------

def get_targets(ctx: dict) -> pd.DataFrame:
    extra = tuple(t.strip().upper() for t in st.session_state.get("ma_extra", "").split(",") if t.strip())
    with st.spinner("Interrogation de Yahoo Finance…"):
        df, status = fetch_ma_targets(ctx["iso3"], extra)
    register_status(status, f"ma_{ctx['iso3']}")
    ctx["ma_status"] = status
    return enrich_targets(df, ctx["iso3"])


def selected_target(ctx: dict, targets: pd.DataFrame) -> dict[str, Any]:
    """Cible retenue (sélection utilisateur, sinon meilleur score d'attractivité)."""
    tk = st.session_state.get("target_ticker")
    if tk not in set(targets["ticker"]):
        tk = targets.sort_values("attractiveness", ascending=False)["ticker"].iloc[0]
    return targets[targets["ticker"] == tk].iloc[0].to_dict()


def module_ma(ctx: dict) -> None:
    iso3, p = ctx["iso3"], ctx["profile"]
    hero("Module 3 • Cartographie M&A & Ciblage d'Opportunités",
         f"{p['name']} — Banques, institutions financières & Fintechs",
         "Yahoo Finance • Valorisation relative • Droite de Gordon P/B = ROE / Ke",
         tape=market_tape(ctx))
    targets = get_targets(ctx)
    source_badges(ctx["ma_status"])

    with st.container():
        c1, c2, c3 = st.columns([2, 1.2, 2])
        cats = sorted(targets["category"].dropna().unique())
        types = c1.multiselect("Catégories", cats, default=cats, key="ma_types")
        min_roe = c2.slider("ROE minimum (%)", -10, 25, -10, key="ma_min_roe")
        c3.text_input("Ajouter des tickers Yahoo (séparés par des virgules)", key="ma_extra",
                      placeholder="ex : SAN.MC, BBVA.MC")
    view = targets[targets["category"].isin(types) & (targets["roe"].fillna(-99) >= min_roe)]
    if view.empty:
        st.info("Aucune cible ne correspond aux filtres.")
        return

    total_cap = view["market_cap_usd_m"].sum() / 1000
    render_cards([
        kpi_card("Cibles analysées", f"{len(view)}", f"sur {len(targets)} dans l'univers"),
        kpi_card("Capitalisation cumulée", f"{total_cap:,.1f} Md$".replace(",", " "), "Conversion USD indicative"),
        kpi_card("ROE médian", f"{view['roe'].median():.1f} %", "Rentabilité des fonds propres", variant="alt"),
        kpi_card("P/B médian", f"{view['pb'].median():.2f}x", "Price-to-Book", variant="alt"),
    ])
    separator()
    tabs = st.tabs(["Tableau comparatif", "Valorisation vs Rentabilité", "Fiche cible"])

    with tabs[0]:
        show = view[["name", "ticker", "category", "market_cap_usd_m", "pe", "pb", "roe", "div_yield",
                     "perf_52w", "discount", "attractiveness", "source"]].sort_values(
            "attractiveness", ascending=False)
        st.dataframe(
            show, width="stretch", hide_index=True,
            column_config={
                "name": st.column_config.TextColumn("Société", width="large"),
                "ticker": st.column_config.TextColumn("Ticker"),
                "category": st.column_config.TextColumn("Catégorie"),
                "market_cap_usd_m": st.column_config.NumberColumn("Capitalisation (M$)", format="%,.0f"),
                "pe": st.column_config.NumberColumn("P/E", format="%.1fx"),
                "pb": st.column_config.NumberColumn("P/B", format="%.2fx"),
                "roe": st.column_config.ProgressColumn("ROE", format="%.1f%%", min_value=0, max_value=30),
                "div_yield": st.column_config.NumberColumn("Rdt dividende", format="%.2f%%"),
                "perf_52w": st.column_config.NumberColumn("Perf. 52 sem.", format="%+.1f%%"),
                "discount": st.column_config.NumberColumn("Décote vs P/B théorique", format="%+.0f%%",
                                                          help="Positif = valorisation inférieure à ROE/Ke"),
                "attractiveness": st.column_config.ProgressColumn("Score d'attractivité", format="%.0f",
                                                                  min_value=0, max_value=100),
                "source": st.column_config.TextColumn("Provenance"),
            })
        st.download_button("Exporter (CSV)", show.to_csv(index=False).encode("utf-8"), icon=":material/download:",
                           file_name=f"cibles_ma_{iso3}.csv", mime="text/csv")

    with tabs[1]:
        ke = 13.0 if p["em"] else 9.5
        v = view.dropna(subset=["pb", "roe"])
        fig = go.Figure()
        xs = np.linspace(min(0, v["roe"].min() - 2), max(25, v["roe"].max() + 2), 50)
        fig.add_scatter(x=xs, y=np.maximum(xs / ke, 0), mode="lines", name=f"P/B théorique (Ke = {ke:.1f} %)",
                        line=dict(color=SLATE, dash="dash"))
        palette = [PRUNE, ACCENT, MAUVE, SLATE, GOLD, "#2F855A", MIST]
        for k, (cat, grp) in enumerate(v.groupby("category")):
            fig.add_scatter(
                x=grp["roe"], y=grp["pb"], mode="markers+text", name=cat, text=grp["ticker"],
                textposition="top center", textfont=dict(size=10),
                marker=dict(size=np.sqrt(grp["market_cap_usd_m"].clip(lower=1)) / np.sqrt(v["market_cap_usd_m"].max()) * 60 + 10,
                            color=palette[k % len(palette)], opacity=0.8, line=dict(color="#fff", width=1.5)),
                customdata=np.stack([grp["name"], grp["market_cap_usd_m"]], axis=-1),
                hovertemplate="<b>%{customdata[0]}</b><br>ROE : %{x:.1f} %<br>P/B : %{y:.2f}x"
                              "<br>Capi : %{customdata[1]:,.0f} M$<extra></extra>")
        fig.update_xaxes(title_text="Return on Equity (%)")
        fig.update_yaxes(title_text="Price-to-Book (x)")
        st.plotly_chart(style_fig(fig, 540, "Valorisation vs Rentabilité — taille des bulles = capitalisation"),
                        width="stretch", theme=None, config=PLOTLY_CONFIG)
        st.caption("Les sociétés situées sous la droite théorique affichent une décote relative à leur "
                   "rentabilité : candidates prioritaires à une analyse approfondie.")

    with tabs[2]:
        opts = list(view.sort_values("attractiveness", ascending=False)["ticker"])
        if st.session_state.get("target_ticker") not in opts:
            st.session_state["target_ticker"] = opts[0]
        names = dict(zip(view["ticker"], view["name"]))
        st.selectbox("Cible retenue pour le simulateur et la note PDF", opts, key="target_ticker",
                     format_func=lambda t: f"{names[t]} ({t})")
        t = selected_target(ctx, view)
        render_cards([
            kpi_card("Capitalisation", f"{t['market_cap_usd_m']:,.0f} M$".replace(",", " "), t["source"]),
            kpi_card("P/E", _fmt(t["pe"], "{:.1f}x"), "Price / Earnings", variant="alt"),
            kpi_card("P/B", _fmt(t["pb"], "{:.2f}x"), f"Théorique : {_fmt(t['pb_fair'], '{:.2f}x')}", variant="alt"),
            kpi_card("ROE", _fmt(t["roe"], "{:.1f} %"), "Rentabilité des fonds propres"),
            kpi_card("Score d'attractivité", f"{t['attractiveness']:.0f}/100", "ROE, décote, rendement"),
        ])
        st.write("")
        st.info(target_comment(t), icon=":material/insights:")


def target_comment(t: dict) -> str:
    disc = t.get("discount")
    if disc is None or (isinstance(disc, float) and np.isnan(disc)):
        return f"{t['name']} : données de valorisation incomplètes, analyse qualitative à privilégier."
    pos = "décote" if disc > 0 else "prime"
    return (f"{t['name']} présente une {pos} de {abs(disc):.0f} % par rapport à son P/B théorique "
            f"(ROE {t['roe']:.1f} %). Une prime de contrôle de 25-35 % est usuelle dans le secteur bancaire ; "
            f"le simulateur retient par défaut 30 % au-dessus de la capitalisation.")


# ---------- Module 4 : Simulateur ----------------------------------------------

def default_sim_params(ctx: dict, t: dict) -> dict[str, float]:
    """Hypothèses par défaut cohérentes avec la cible et le pays."""
    p, latest = ctx["profile"], ctx["latest"]
    cap = float(t.get("market_cap_usd_m") or 500)
    rev = t.get("revenue_usd_m")
    rev = float(rev) if rev and not np.isnan(rev) else cap * 0.3
    pe = t.get("pe")
    pe = float(pe) if pe and not np.isnan(pe) else 10.0
    nominal = latest["gdp_growth"]["value"] + min(latest["inflation"]["value"], 8) * 0.6
    cor = 14 if p["em"] else 9
    tax = float(p["tax"])
    # Calibrage : coefficient d'exploitation implicite tel que le résultat net
    # stand-alone du modèle corresponde au résultat net publié de la cible.
    ni = t.get("net_income_usd_m")
    cir = 55 if p["em"] else 62
    if ni and not np.isnan(ni) and ni > 0 and rev > 0:
        cir = int(np.clip(round((1 - cor / 100 - ni / (rev * (1 - tax / 100))) * 100), 35, 85))
    return dict(price=round(cap * 1.30, -1), equity_pct=60, rev_syn=3.0, cost_syn=10.0,
                rate=round(4.5 + (2.0 if p["em"] else 0.0), 1), horizon=5,
                pnb0=round(rev, -1), growth=round(float(np.clip(nominal, 1.0, 12.0)), 1),
                cir=cir, cor=cor, tax=tax,
                exit_pe=round(float(np.clip(pe, 6, 18)), 1), payout=50)


SIM_KEYS = ["price", "equity_pct", "rev_syn", "cost_syn", "rate", "horizon", "pnb0", "growth",
            "cir", "cor", "tax", "exit_pe", "payout"]


def ensure_sim_state(ctx: dict, t: dict, force: bool = False) -> None:
    """Initialise / réinitialise les widgets du simulateur selon la cible."""
    if force or st.session_state.get("sim_target") != t["ticker"]:
        for k, v in default_sim_params(ctx, t).items():
            st.session_state[f"sim_{k}"] = v
        st.session_state["sim_target"] = t["ticker"]


def current_sim_params() -> dict[str, float]:
    return {k: float(st.session_state[f"sim_{k}"]) for k in SIM_KEYS}


def module_sim(ctx: dict) -> None:
    iso3, p = ctx["iso3"], ctx["profile"]
    targets = get_targets(ctx)
    t = selected_target(ctx, targets)
    ensure_sim_state(ctx, t)
    hero("Module 4 • Simulateur Financier Post-Acquisition",
         f"M&A Deal Modeler — {t['name']}",
         f"{p['name']} • Projections P&L, TRI, ROI & Payback • Hypothèses modifiables",
         tape=market_tape(ctx))

    left, right = st.columns([1, 2.4], gap="large")
    with left:
        section("Paramètres du deal")
        cur = st.radio("Devise d'affichage", ["$", "€"], horizontal=True, key="sim_currency")
        st.number_input(f"Prix d'acquisition ({cur}M)", min_value=10.0, step=50.0, format="%.0f", key="sim_price",
                        help="Défaut : capitalisation + prime de contrôle de 30 %")
        st.slider("Part financée en fonds propres BPCE (%)", 10, 100, step=5, key="sim_equity_pct",
                  help="Le solde est financé par dette d'acquisition in fine")
        st.slider("Synergies de revenus (% du PNB)", 0.0, 15.0, step=0.5, key="sim_rev_syn")
        st.slider("Synergies de coûts (% des charges)", 0.0, 30.0, step=0.5, key="sim_cost_syn")
        st.slider("Taux d'intérêt du financement (%)", 0.0, 15.0, step=0.1, key="sim_rate")
        st.slider("Horizon d'investissement (années)", 3, 10, key="sim_horizon")
        with st.expander("Hypothèses opérationnelles avancées", icon=":material/tune:"):
            st.number_input(f"PNB initial de la cible ({cur}M)", min_value=1.0, step=10.0, format="%.0f", key="sim_pnb0")
            st.slider("Croissance organique nominale (%)", 0.0, 15.0, step=0.1, key="sim_growth")
            st.slider("Coefficient d'exploitation (%)", 35, 85, key="sim_cir")
            st.slider("Coût du risque (% du PNB)", 0, 40, key="sim_cor")
            st.slider("Taux d'imposition (%)", 0.0, 45.0, step=0.5, key="sim_tax")
            st.slider("Multiple de sortie (P/E)", 4.0, 25.0, step=0.5, key="sim_exit_pe")
            st.slider("Taux de distribution à BPCE (%)", 0, 100, step=5, key="sim_payout")
        st.button("Réinitialiser selon la cible", icon=":material/restart_alt:", width="stretch",
                  on_click=ensure_sim_state, args=(ctx, t, True))

    params = current_sim_params()
    res = run_financial_simulation(params)
    pl = res["pl"]

    with right:
        hurdle = 10.0
        irr_v = res["irr"]
        render_cards([
            kpi_card("TRI actionnaire", _fmt(irr_v, "{:.1f} %"),
                     f"Hurdle rate {hurdle:.0f} % • " + ("créateur de valeur" if irr_v and irr_v >= hurdle
                                                        else "sous le seuil"),
                     None if irr_v is None else irr_v - hurdle),
            kpi_card("ROI", _fmt(res["roi"], "{:.0f} %"), f"MOIC {_fmt(res['moic'], '{:.2f}x')}"),
            kpi_card("Payback", _fmt(res["payback"], "{:.1f} ans", "> horizon"), "Flux cumulés actionnaire",
                     variant="alt"),
            kpi_card("Fonds propres investis", f"{res['equity']:,.0f} {cur}M".replace(",", " "),
                     f"Dette : {res['debt']:,.0f} {cur}M".replace(",", " "), variant="alt"),
        ])
        st.write("")
        tabs = st.tabs(["PNB & Résultat net", "P&L projeté", "Flux & création de valeur",
                        "Sensibilités"])
        with tabs[0]:
            fig = go.Figure()
            fig.add_bar(x=pl["Année"], y=pl["PNB_organique"], name="PNB organique", marker_color=PRUNE,
                        offsetgroup="pnb")
            fig.add_bar(x=pl["Année"], y=pl["Synergies_revenus"], name="Synergies de revenus",
                        marker_color=SLATE, offsetgroup="pnb", base=pl["PNB_organique"])
            rn_base = pl["Resultat_net_standalone"].clip(lower=0)
            uplift = pl["Resultat_net"] - pl["Resultat_net_standalone"]
            fig.add_bar(x=pl["Année"], y=pl["Resultat_net_standalone"], name="Résultat net stand-alone",
                        marker_color=MAUVE, offsetgroup="rn")
            fig.add_bar(x=pl["Année"], y=uplift, name="Effet deal (synergies − intégration − dette)",
                        marker_color=ACCENT, offsetgroup="rn", base=rn_base)
            fig.add_scatter(x=pl["Année"], y=pl["Resultat_net"], name="Résultat net post-synergies",
                            mode="lines+markers", line=dict(color=INK, width=2),
                            marker=dict(size=8))
            fig.update_layout(barmode="group", bargap=0.2)
            fig.update_yaxes(title_text=f"{cur}M")
            st.plotly_chart(style_fig(fig, 460, "Évolution du PNB et du résultat net post-synergies"),
                            width="stretch", theme=None, config=PLOTLY_CONFIG)

        with tabs[1]:
            table = pl.set_index("Année")[["PNB_organique", "Synergies_revenus", "PNB", "Charges",
                                           "Synergies_couts", "Couts_integration", "RBE", "Cout_du_risque",
                                           "Frais_financiers", "Impot", "Resultat_net", "Coef_exploitation"]].T
            table.index = ["PNB organique", "Synergies de revenus", "PNB total", "Charges d'exploitation",
                           "dont synergies de coûts", "Coûts d'intégration", "Résultat brut d'exploitation",
                           "Coût du risque", "Frais financiers", "Impôt", "Résultat net", "Coef. exploitation (%)"]
            st.dataframe(table.style.format("{:,.1f}"), width="stretch")
            st.download_button("Exporter le P&L (CSV)", pl.to_csv(index=False).encode("utf-8"), icon=":material/download:",
                               file_name=f"pl_{t['ticker']}.csv", mime="text/csv")

        with tabs[2]:
            cf = res["cashflows"]
            years = [f"A{i}" for i in range(len(cf))]
            fig = go.Figure()
            fig.add_bar(x=years, y=cf, name="Flux actionnaire BPCE",
                        marker_color=[ACCENT if v < 0 else PRUNE for v in cf])
            fig.add_scatter(x=years, y=np.cumsum(cf), name="Flux cumulés", mode="lines+markers",
                            line=dict(color=SLATE, width=3))
            fig.add_hline(y=0, line_color=MIST)
            fig.update_yaxes(title_text=f"{cur}M")
            st.plotly_chart(style_fig(fig, 420, "Flux de trésorerie actionnaire (dividendes + sortie − dette)"),
                            width="stretch", theme=None, config=PLOTLY_CONFIG)

        with tabs[3]:
            grid = sensitivity_grid(params)
            fig = go.Figure(go.Heatmap(
                z=grid.values, x=grid.columns, y=grid.index, text=np.round(grid.values, 1),
                texttemplate="%{text} %", colorscale=[[0, "#F3D0CC"], [0.5, "#FFFFFF"], [1, "#D9C2D5"]],
                zmid=hurdle, colorbar=dict(title="TRI %")))
            st.plotly_chart(style_fig(fig, 420, "Sensibilité du TRI : synergies de coûts × multiple de sortie"),
                            width="stretch", theme=None, config=PLOTLY_CONFIG)
    st.session_state["sim_result"] = res


# ---------- Module 5 : Note PDF -------------------------------------------------

def build_recommendation(risk: dict, sim: dict) -> dict[str, str]:
    comp, irr_v = risk["composite"], sim["irr"]
    if irr_v is not None and irr_v >= 12 and comp < 50:
        verdict = "Opportunité attractive"
        text = (f"TRI de {irr_v:.1f} % supérieur au seuil de 10 % avec un profil de risque contenu "
                f"({comp:.0f}/100). Recommandation : engager une due diligence approfondie.")
    elif irr_v is not None and irr_v >= 10:
        verdict = "Opportunité à approfondir sous conditions"
        text = (f"Rendement acceptable (TRI {irr_v:.1f} %) mais risque {risk_level(comp).lower()} "
                f"({comp:.0f}/100) : conditionner l'opération à des garanties (earn-out, clauses MAC, "
                f"couverture de change) et à une revue Conformité renforcée.")
    else:
        verdict = "Opportunité non prioritaire en l'état"
        text = (f"TRI de {_fmt(irr_v, '{:.1f} %')} insuffisant au regard d'un risque composite de "
                f"{comp:.0f}/100. Revoir le prix, la structure de financement ou le plan de synergies.")
    return dict(verdict=verdict, text=text)


def module_pdf(ctx: dict) -> None:
    iso3, p = ctx["iso3"], ctx["profile"]
    targets = get_targets(ctx)
    t = selected_target(ctx, targets)
    ensure_sim_state(ctx, t)
    sim = run_financial_simulation(current_sim_params())
    reco = build_recommendation(ctx["risk"], sim)
    cur = st.session_state.get("sim_currency", "$")

    hero("Module 5 • Générateur de Note de Synthèse",
         f"Executive Summary PDF — {p['name']}",
         "ReportLab • Charte Groupe BPCE • 2-3 pages • Header/Footer, pagination et date d'édition",
         tape=market_tape(ctx))

    c1, c2 = st.columns([1.6, 1], gap="large")
    with c1:
        section("Aperçu du contenu")
        st.markdown(f"""
<div class="panel">
<b style="color:{ACCENT}">{reco['verdict']}</b><br/><span style="color:#1F2421">{reco['text']}</span>
</div>""", unsafe_allow_html=True)
        render_cards([
            kpi_card("Pays", f"{p['name']}", ctx["region"]),
            kpi_card("Risque composite", f"{ctx['risk']['composite']:.0f}/100", risk_level(ctx["risk"]["composite"]),
                     variant="alt"),
            kpi_card("Cible", t["ticker"], t["name"][:38]),
            kpi_card("TRI simulé", _fmt(sim["irr"], "{:.1f} %"), f"Horizon {sim['params']['horizon']:.0f} ans",
                     variant="alt"),
        ])
        st.write("")
        st.markdown("""
**Sections du rapport**
1. Synthèse macroéconomique & PESTEL (indicateurs, moyenne de zone, projections 2026)
2. Compliance, KYC & Risk Index (scores, niveaux, risques prioritaires, superviseur)
3. Cible M&A sélectionnée (multiples, rentabilité, positionnement)
4. Projections financières post-acquisition (hypothèses, P&L, TRI / ROI / payback, graphique)
5. Provenance des données & avertissement
""")
    with c2:
        section("Édition")
        st.caption("Les paramètres repris sont ceux des modules 2 à 4 (checklist, cible, simulateur).")
        if not REPORTLAB_OK:
            st.error("ReportLab n'est pas installé : `pip install reportlab`.")
            return
        if st.button("Générer la note de synthèse", type="primary", icon=":material/picture_as_pdf:", width="stretch"):
            payload = dict(
                iso3=iso3, country=p["name"], region=ctx["region"], latest=ctx["latest"],
                region_avg=ctx["avg"], risk=ctx["risk"], market=ctx["market"],
                checklist_ratio=ctx["checklist_ratio"], target=t, sim=sim, currency_label=cur,
                recommendation=reco, macro_comments=macro_commentary(iso3, ctx["latest"], ctx["avg"]),
                target_comment=target_comment(t),
                api_status={k: {"live": "en direct", "partial": "partiel", "fallback": "mode secours"}[v]
                            for k, v in st.session_state.get("api_status", {}).items()},
            )
            try:
                with st.spinner("Composition du document…"):
                    st.session_state["pdf_bytes"] = generate_pdf_report(payload)
                    st.session_state["pdf_name"] = (f"BPCE_Note_Synthese_{iso3}_{t['ticker']}_"
                                                    f"{datetime.now():%Y%m%d}.pdf")
                st.toast("Note de synthèse générée", icon=":material/task_alt:")
            except Exception as err:  # robustesse : ne jamais casser l'interface
                st.error(f"Échec de génération du PDF : {err}")
        if st.session_state.get("pdf_bytes"):
            st.download_button("Télécharger le PDF", st.session_state["pdf_bytes"], icon=":material/download:",
                               file_name=st.session_state["pdf_name"], mime="application/pdf",
                               width="stretch")
            st.success(f"Document prêt ({len(st.session_state['pdf_bytes']) / 1024:.0f} Ko).")


# =============================================================================
# 5. ORCHESTRATION
# =============================================================================

def main() -> None:
    if ensure_theme_config():
        st.toast("Thème Groupe BPCE installé (.streamlit/config.toml)", icon=":material/palette:")
    inject_css()
    persist_widget_state()
    region, iso3, module = render_sidebar()

    with st.spinner("Chargement des données macroéconomiques…"):
        macro_df, macro_status = fetch_macro_data(tuple(REGIONS[region]))
    register_status(macro_status, f"macro_{region}")
    with st.spinner("Chargement des indicateurs de marché…"):
        market, market_status = fetch_market_risk_data(iso3)
    register_status(market_status, f"market_{iso3}")

    latest = latest_values(macro_df, iso3)
    checklist_ratio = sum(bool(st.session_state.get(f"chk_{c[0]}")) for c in COMPLIANCE_CHECKLIST) \
        / len(COMPLIANCE_CHECKLIST)
    ctx = dict(
        region=region, iso3=iso3, profile=PROFILES[iso3], region_isos=REGIONS[region],
        macro=macro_df, macro_status=macro_status, latest=latest,
        avg=region_average(macro_df, REGIONS[region]),
        market=market, market_status=market_status, checklist_ratio=checklist_ratio,
        risk=compute_risk_scores(iso3, latest, market, checklist_ratio),
    )

    {MODULES[0]: module_macro, MODULES[1]: module_risk, MODULES[2]: module_ma,
     MODULES[3]: module_sim, MODULES[4]: module_pdf}[module](ctx)


if __name__ == "__main__":
    main()
