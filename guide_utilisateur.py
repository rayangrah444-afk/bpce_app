# -*- coding: utf-8 -*-
"""
Guide utilisateur — BPCE International Strategy Hub
====================================================

Génère un mémorandum PDF (3 pages) présentant l'application, ses cinq modules,
la lecture de leurs indicateurs et la méthodologie de données.

Utilisation autonome :
    python guide_utilisateur.py            -> écrit Guide_utilisateur_BPCE_Strategy_Hub.pdf

Depuis l'application Streamlit :
    from guide_utilisateur import generate_user_guide_pdf
    st.download_button("Guide utilisateur (PDF)", generate_user_guide_pdf(), "guide.pdf")

Dépendance : reportlab>=4.0
"""

from __future__ import annotations

import io
import re
from datetime import datetime
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (KeepTogether, Paragraph, SimpleDocTemplate,
                                Spacer, Table, TableStyle)

# --------------------------------------------------------------------------
# Charte Groupe BPCE (identique à app.py)
# --------------------------------------------------------------------------
PRUNE = colors.HexColor("#4A2845")
PRUNE_DARK = colors.HexColor("#351C32")
ACCENT = colors.HexColor("#8E3B82")
MAUVE = colors.HexColor("#B48AB0")
TINT = colors.HexColor("#F3EEF2")
INK = colors.HexColor("#1F2421")
MUTED = colors.HexColor("#55505A")
BORDER = colors.HexColor("#E2E8F0")
BG = colors.HexColor("#F8F9FA")
GREEN = colors.HexColor("#2F855A")
GREEN_BG = colors.HexColor("#EAF5EF")
RED = colors.HexColor("#B42318")
RED_BG = colors.HexColor("#FBECEA")
AMBER = colors.HexColor("#B7791F")
ORANGE = colors.HexColor("#C05621")

APP_NAME = "BPCE International Strategy Hub"
PAGE_W, PAGE_H = A4
MARGIN = 15 * mm
W = PAGE_W - 2 * MARGIN


# --------------------------------------------------------------------------
# Styles typographiques
# --------------------------------------------------------------------------
def _styles() -> dict[str, ParagraphStyle]:
    base = dict(fontName="Helvetica", textColor=INK, alignment=TA_LEFT)
    return {
        "title": ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=20, leading=24,
                                textColor=PRUNE_DARK, spaceAfter=2),
        "subtitle": ParagraphStyle("subtitle", fontName="Helvetica", fontSize=9.5, leading=13,
                                   textColor=MUTED, spaceAfter=8),
        "h1": ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=11.5, leading=14,
                             textColor=PRUNE, spaceBefore=7, spaceAfter=4),
        "body": ParagraphStyle("body", fontSize=8.8, leading=12.2, **base),
        "cell": ParagraphStyle("cell", fontSize=7.8, leading=9.9, **base),
        "cell_b": ParagraphStyle("cell_b", fontName="Helvetica-Bold", fontSize=7.9, leading=10.4,
                                 textColor=INK),
        "head": ParagraphStyle("head", fontName="Helvetica-Bold", fontSize=7.4, leading=9.5,
                               textColor=colors.white),
        "mod_title": ParagraphStyle("mod_title", fontName="Helvetica-Bold", fontSize=9.8, leading=12,
                                    textColor=colors.white),
        "mod_desc": ParagraphStyle("mod_desc", fontName="Helvetica", fontSize=8.1, leading=10.8,
                                   textColor=colors.HexColor("#EDE3EB")),
        "num": ParagraphStyle("num", fontName="Helvetica-Bold", fontSize=13, leading=15,
                              textColor=PRUNE, alignment=1),
        "box": ParagraphStyle("box", fontSize=8.8, leading=12.4, textColor=PRUNE_DARK,
                              fontName="Helvetica"),
        "small": ParagraphStyle("small", fontSize=7.2, leading=9.6, textColor=MUTED,
                                fontName="Helvetica"),
    }


S = _styles()


_AMP = re.compile(r"&(?!(?:amp|nbsp|lt|gt|quot|#\d+);)")


def P(text: str, style: str = "cell") -> Paragraph:
    """Paragraph avec balisage ReportLab autorisé (<b>, <i>, <br/>, <font>).

    Les « & » isolés (M&A, P&L) sont échappés automatiquement.
    """
    return Paragraph(_AMP.sub("&amp;", text), S[style])


def bullets(items: list[str], style: str = "cell") -> Paragraph:
    return P("<br/>".join(f"•&nbsp;{i}" for i in items), style)


# --------------------------------------------------------------------------
# Header / footer corporate + pagination « Page x / y »
# --------------------------------------------------------------------------
class _MemoCanvas(rl_canvas.Canvas):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pages: list[dict] = []

    def showPage(self):  # noqa: N802 (API ReportLab)
        self._pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._pages)
        for state in self._pages:
            self.__dict__.update(state)
            self._frame(total)
            super().showPage()
        super().save()

    def _frame(self, total: int) -> None:
        # Bandeau prune + liseré orchidée
        self.setFillColor(PRUNE_DARK)
        self.rect(0, PAGE_H - 20 * mm, PAGE_W, 20 * mm, stroke=0, fill=1)
        self.setFillColor(MAUVE)
        self.rect(0, PAGE_H - 21.2 * mm, PAGE_W, 1.2 * mm, stroke=0, fill=1)
        self.setFillColor(colors.white)
        self.roundRect(MARGIN, PAGE_H - 15.5 * mm, 11 * mm, 11 * mm, 2 * mm, stroke=0, fill=1)
        self.setFillColor(PRUNE)
        self.setFont("Helvetica-Bold", 6.6)
        self.drawCentredString(MARGIN + 5.5 * mm, PAGE_H - 10.9 * mm, "BPCE")
        self.setFillColor(colors.white)
        self.setFont("Helvetica-Bold", 11)
        self.drawString(MARGIN + 14 * mm, PAGE_H - 9.6 * mm, "GROUPE BPCE")
        self.setFont("Helvetica", 7.8)
        self.drawString(MARGIN + 14 * mm, PAGE_H - 13.6 * mm, "Direction Stratégie & Développement International")
        self.setFont("Helvetica-Bold", 8)
        self.drawRightString(PAGE_W - MARGIN, PAGE_H - 9.6 * mm, "MÉMORANDUM — GUIDE UTILISATEUR")
        self.setFont("Helvetica", 7.6)
        self.drawRightString(PAGE_W - MARGIN, PAGE_H - 13.6 * mm, APP_NAME)
        # Pied de page
        self.setStrokeColor(PRUNE)
        self.setLineWidth(0.5)
        self.line(MARGIN, 13 * mm, PAGE_W - MARGIN, 13 * mm)
        self.setFont("Helvetica", 7)
        self.setFillColor(MUTED)
        self.drawString(MARGIN, 9 * mm, "Usage interne — Document de référence utilisateur")
        self.drawRightString(PAGE_W - MARGIN - 22 * mm, 9 * mm, f"Version du {datetime.now():%d/%m/%Y}")
        self.setFont("Helvetica-Bold", 7)
        self.setFillColor(ACCENT)
        self.drawRightString(PAGE_W - MARGIN, 9 * mm, f"Page {self._pageNumber} / {total}")


# --------------------------------------------------------------------------
# Blocs de mise en page
# --------------------------------------------------------------------------
def callout(title: str, body: str) -> Table:
    """Encadré « message clé » : fond teinté, filet prune à gauche."""
    t = Table([[P(f"<b>{title}</b><br/>{body}", "box")]], colWidths=[W])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), TINT),
        ("LINEBEFORE", (0, 0), (0, -1), 3.5, PRUNE),
        ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    return t


def steps_strip(steps: list[tuple[str, str]]) -> Table:
    """Parcours utilisateur en étapes numérotées, sur une ligne."""
    cells = [[P(f"<font color='#8E3B82'><b>{i}</b></font>&nbsp;&nbsp;<b>{t}</b><br/>{d}", "cell")
              for i, (t, d) in enumerate(steps, start=1)]]
    tbl = Table(cells, colWidths=[W / len(steps)] * len(steps))
    tbl.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
        ("LINEAFTER", (0, 0), (-2, -1), 0.5, BORDER),
        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))
    return tbl


def module_block(num: int, title: str, purpose: str, indicators: list[str],
                 opportunities: list[str], alerts: list[str]) -> KeepTogether:
    """Fiche module : bandeau titre + grille Indicateurs / Opportunités / Alertes."""
    head = Table([[P(str(num), "num"),
                   P(f"{title}<br/><font name='Helvetica' size='8' color='#EDE3EB'>{purpose}</font>", "mod_title")]],
                 colWidths=[10 * mm, W - 10 * mm])
    head.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), PRUNE),
        ("BACKGROUND", (0, 0), (0, 0), colors.white),
        ("BOX", (0, 0), (0, 0), 1.2, PRUNE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (1, 0), (1, 0), 8),
    ]))
    cw = [W * 0.36, W * 0.32, W * 0.32]
    grid = Table([
        [P("INDICATEURS AFFICHÉS", "head"), P("SIGNAUX D'OPPORTUNITÉ", "head"), P("SIGNAUX D'ALERTE", "head")],
        [bullets(indicators), bullets(opportunities), bullets(alerts)],
    ], colWidths=cw)
    grid.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), PRUNE_DARK),
        ("BACKGROUND", (1, 0), (1, 0), GREEN),
        ("BACKGROUND", (2, 0), (2, 0), RED),
        ("BACKGROUND", (0, 1), (0, 1), colors.white),
        ("BACKGROUND", (1, 1), (1, 1), GREEN_BG),
        ("BACKGROUND", (2, 1), (2, 1), RED_BG),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
        ("LINEAFTER", (0, 0), (-2, -1), 0.5, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))
    return KeepTogether([head, grid, Spacer(1, 6)])


def simple_table(rows: list[list], col_widths: list[float], extra: list | None = None) -> Table:
    data = [[P(c, "head") if isinstance(c, str) else c for c in rows[0]]]
    data += [[P(c) if isinstance(c, str) else c for c in r] for r in rows[1:]]
    tbl = Table(data, colWidths=col_widths, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), PRUNE),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
        ("INNERGRID", (0, 1), (-1, -1), 0.4, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]
    for r in range(2, len(data), 2):
        style.append(("BACKGROUND", (0, r), (-1, r), BG))
    tbl.setStyle(TableStyle(style + (extra or [])))
    return tbl


def risk_scale() -> Table:
    """Échelle de lecture des scores de risque (0-100)."""
    levels = [("0 – 25", "Faible", GREEN, "Instruction standard"),
              ("25 – 50", "Modéré", AMBER, "Vigilance, suivi renforcé"),
              ("50 – 75", "Élevé", ORANGE, "Opération sous conditions"),
              ("75 – 100", "Critique", RED, "Go / no-go Conformité préalable")]
    row = [P(f"<font color='white'><b>{lbl}</b>&nbsp;&nbsp;{rng}</font><br/>"
             f"<font color='white' size='7.2'>{act}</font>", "cell") for rng, lbl, _, act in levels]
    tbl = Table([row], colWidths=[W / 4] * 4)
    style = [("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
             ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
             ("LEFTPADDING", (0, 0), (-1, -1), 7)]
    for i, (_, _, col, _) in enumerate(levels):
        style.append(("BACKGROUND", (i, 0), (i, 0), col))
    tbl.setStyle(TableStyle(style))
    return tbl


# --------------------------------------------------------------------------
# Contenu du mémorandum
# --------------------------------------------------------------------------
MODULES = [
    dict(
        title="Dashboard PESTEL & Macroéconomie",
        purpose="Positionne le pays cible dans sa zone sur 2015-2026 (réalisé + projections FMI) "
                "et note son environnement PESTEL de 1 à 5.",
        indicators=[
            "PIB nominal (Md$), croissance réelle, inflation, chômage",
            "Balance commerciale (% du PIB)",
            "Stabilité politique et contrôle de la corruption (WGI, échelle -2,5 / +2,5)",
            "Taux directeur, spread souverain, volatilité du change vs EUR",
            "Grille PESTEL notée de 1 à 5 étoiles (5 = favorable)",
            "Comparatif pays vs moyenne de zone ; zone d'incertitude (±1 écart-type)",
        ],
        opportunities=[
            "Croissance durablement supérieure à la moyenne de zone",
            "Inflation maîtrisée (2 à 4 %) : préservation des marges et des fonds propres",
            "WGI positifs ; 4 étoiles et plus sur P, E et L",
            "Projections 2026 orientées à la hausse avec une zone d'incertitude étroite",
        ],
        alerts=[
            "Inflation supérieure à 8-10 % : érosion du capital converti en euros",
            "Stabilité politique WGI inférieure à -1",
            "Déficit commercial au-delà de -5 % du PIB : pression sur le change et la liquidité",
            "Zone d'incertitude large : scénario central peu fiable",
        ],
    ),
    dict(
        title="Compliance, KYC & Risk Index",
        purpose="Synthétise le risque d'implantation en trois scores de 0 à 100 (100 = risque maximal), "
                "une matrice des risques et une checklist réglementaire.",
        indicators=[
            "<b>Risque Pays</b> : stabilité politique 30 %, spread souverain 35 %, corruption 15 %, "
            "déséquilibres macro 20 %",
            "<b>Risque de Change</b> : volatilité EUR/devise 70 %, écart d'inflation 30 %",
            "<b>Risque KYC / Sanctions</b> : base juridictionnelle 60 %, corruption 40 % ; "
            "réduit jusqu'à 25 % selon l'avancement de la checklist",
            "Matrice Impact x Probabilité de 10 risques (criticité sur 25)",
            "Checklist FATCA, CRS, LCB-FT, Sanctions, GAFI, Prudentiel, Données",
        ],
        opportunities=[
            "Score composite inférieur à 50 : instruction possible au standard Groupe",
            "Devise ancrée à l'euro (zone euro, franc CFA) : risque de change quasi nul",
            "Checklist complétée : risque KYC résiduel abaissé et documenté",
        ],
        alerts=[
            "Un score « Critique » (75 et plus) : arbitrage Conformité avant toute étape",
            "Spread souverain supérieur à 300 pb",
            "LCB-FT ou Sanctions en zone rouge de la matrice (criticité de 17/25 et plus)",
            "Base KYC indicative : toujours vérifier les listes GAFI, UE, ONU et OFAC en vigueur",
        ],
    ),
    dict(
        title="Cartographie M&A & Ciblage d'opportunités",
        purpose="Compare les banques, institutions financières et fintechs du pays et repère "
                "celles dont la valorisation est inférieure à leur rentabilité.",
        indicators=[
            "Capitalisation (M$), P/E, P/B, ROE, rendement du dividende, performance 52 semaines",
            "<b>P/B théorique = ROE / Ke</b> (Ke de 9,5 % sur les marchés développés, 13 % sur les "
            "émergents) et décote correspondante",
            "Score d'attractivité de 0 à 100 : ROE (50 pts), décote (30 pts), rendement (20 pts)",
            "Nuage Valorisation vs Rentabilité (taille des bulles = capitalisation)",
        ],
        opportunities=[
            "Cible située sous la droite théorique : rentabilité achetée avec une décote",
            "ROE supérieur au coût des fonds propres (Ke) : création de valeur",
            "Score d'attractivité supérieur à 60, avec un dividende soutenable",
        ],
        alerts=[
            "P/B élevé avec un ROE faible : prime de marché non justifiée",
            "ROE inférieur à Ke : la cible détruit de la valeur",
            "Ligne marquée « Simulé » : chiffres illustratifs, à ne pas utiliser en comité",
        ],
    ),
    dict(
        title="Simulateur financier post-acquisition",
        purpose="Projette le P&L de la cible après intégration et mesure le rendement pour l'actionnaire "
                "BPCE selon le prix, le financement et les synergies.",
        indicators=[
            "Hypothèses : prix (par défaut capitalisation + 30 % de prime), part de fonds propres et de "
            "dette in fine, synergies de revenus et de coûts (montée en charge 35 / 75 / 100 %), taux, "
            "horizon, multiple de sortie",
            "PNB, charges, coût du risque, résultat net, coefficient d'exploitation",
            "<b>TRI actionnaire</b> (seuil de rentabilité exigé : 10 %), ROI, MOIC, payback",
            "Carte de sensibilité : TRI selon les synergies de coûts et le multiple de sortie",
        ],
        opportunities=[
            "TRI de 12 % et plus avec un risque composite inférieur à 50",
            "TRI au-dessus de 10 % même à synergies réduites (robustesse)",
            "Payback atteint avant l'horizon de sortie",
        ],
        alerts=[
            "TRI inférieur à 10 % : prix, structure ou plan de synergies à revoir",
            "Valeur dépendante d'un multiple de sortie élevé : thèse fragile",
            "Levier élevé : le TRI augmente, mais aussi le risque de refinancement",
        ],
    ),
    dict(
        title="Note de synthèse exécutive (PDF)",
        purpose="Assemble en 3 pages les conclusions des modules 1 à 4 pour un comité ou une revue de "
                "direction.",
        indicators=[
            "Message clé automatique : <i>attractive</i>, <i>à approfondir sous conditions</i> ou "
            "<i>non prioritaire</i>",
            "Synthèse macro et PESTEL, scores de risque, fiche cible, hypothèses, P&L et rendement",
            "Provenance de chaque donnée (en direct, partielle ou mode secours)",
        ],
        opportunities=[
            "Support prêt à diffuser, homogène d'un pays à l'autre",
            "Comparaison rapide de plusieurs scénarios (une note par jeu d'hypothèses)",
        ],
        alerts=[
            "Vérifier la section « Provenance » avant toute diffusion",
            "La note est un outil d'aide à la décision, pas une évaluation officielle",
        ],
    ),
]

SOURCES = [
    ["Source", "Données mobilisées", "Usage dans l'outil"],
    ["<b>Banque Mondiale</b> (WDI, WGI)", "PIB, croissance, inflation, chômage, balance commerciale, "
     "stabilité politique, contrôle de la corruption", "Séries réalisées 2015-2024 (prioritaires)"],
    ["<b>FMI</b> (WEO DataMapper)", "Croissance, inflation, chômage, PIB", "Projections 2025-2026 et années "
     "manquantes"],
    ["<b>FRED</b> (Fed de St. Louis)", "Taux 10 ans OCDE vs Bund, spread émergents ICE BofA, Fed Funds",
     "Spread souverain, stress de marché, taux directeur"],
    ["<b>BCE</b> (Data Portal)", "Taux de la facilité de dépôt", "Taux de référence zone euro et UEMOA"],
    ["<b>Yahoo Finance</b>", "Capitalisations, multiples, ROE, dividendes, cours de change",
     "Cartographie M&A, volatilité EUR/devise sur 1 an"],
]


def _build_story() -> list:
    story: list = []

    # ---- Titre & message clé -------------------------------------------------
    story.append(P("Guide utilisateur — International Strategy Hub", "title"))
    story.append(P("Mémorandum à l'attention des dirigeants et analystes • Direction Stratégie & "
                   "Développement International", "subtitle"))
    story.append(callout(
        "En bref",
        "L'application réunit dans un seul outil les quatre questions d'un projet de développement "
        "international : <b>le pays est-il porteur ?</b> (macro et PESTEL), <b>est-il sûr ?</b> "
        "(risques et conformité), <b>quelle cible acquérir ?</b> (cartographie M&A) et "
        "<b>à quel prix l'opération crée-t-elle de la valeur ?</b> (simulateur). "
        "Elle produit ensuite une note exécutive prête pour un comité."))

    # ---- 1. Introduction -----------------------------------------------------
    story.append(P("1. Objectif de l'application", "h1"))
    story.append(P(
        "Le <b>BPCE International Strategy Hub</b> accélère la phase de pré-instruction des projets "
        "d'implantation, de partenariat ou d'acquisition hors de France. Il couvre 29 pays répartis en six "
        "zones (Europe de l'Ouest et Benelux, UEMOA, USMCA, LATAM, Asie de l'Est et ASEAN, MENA) et "
        "relie, pour chaque pays, des données publiques de référence à un cadre d'analyse homogène. "
        "Objectif : écarter rapidement les dossiers non prioritaires et concentrer l'expertise des "
        "équipes Finance, Risques et Conformité sur les opportunités qui le justifient.", "body"))
    story.append(Spacer(1, 6))
    story.append(steps_strip([
        ("Cadrer", "Choisir la zone puis le pays dans la barre latérale."),
        ("Diagnostiquer", "Lire le ruban de données et le module macro."),
        ("Sécuriser", "Contrôler les scores de risque et remplir la checklist."),
        ("Cibler", "Retenir une cible dans la cartographie M&A."),
        ("Décider", "Simuler le deal, puis éditer la note PDF."),
    ]))
    story.append(Spacer(1, 4))
    story.append(P("Le <b>ruban de données</b> en tête de chaque module (PIB, croissance, inflation, spread, "
                   "change, taux directeur, risque composite) donne une lecture instantanée du pays : "
                   "<font color='#2F855A'><b>vert</b></font> = signal favorable, "
                   "<font color='#B42318'><b>rouge</b></font> = point de vigilance.", "body"))

    # ---- 2. Modules ----------------------------------------------------------
    story.append(P("2. Les cinq modules et la lecture de leurs indicateurs", "h1"))
    for i, m in enumerate(MODULES, start=1):
        story.append(module_block(i, m["title"], m["purpose"], m["indicators"],
                                  m["opportunities"], m["alerts"]))

    # ---- Grilles de décision -------------------------------------------------
    story.append(KeepTogether([
        P("Grilles de décision de référence", "h1"),
        P("<b>Scores de risque (module 2)</b> — même échelle pour le Risque Pays, le Risque de Change, "
          "le Risque KYC et le score composite (moyenne des trois) :", "body"),
        Spacer(1, 4), risk_scale(), Spacer(1, 6),
        simple_table([
            ["Message clé de la note (module 5)", "Condition", "Recommandation"],
            ["<font color='#2F855A'><b>Opportunité attractive</b></font>",
             "TRI de 12 % et plus, risque composite inférieur à 50",
             "Engager une due diligence approfondie"],
            ["<font color='#B7791F'><b>À approfondir sous conditions</b></font>",
             "TRI entre 10 et 12 %, ou risque composite de 50 et plus",
             "Garanties (earn-out, clauses MAC, couverture de change) et revue Conformité renforcée"],
            ["<font color='#B42318'><b>Non prioritaire en l'état</b></font>", "TRI inférieur à 10 %",
             "Revoir le prix, le financement ou le plan de synergies"],
        ], [W * 0.30, W * 0.33, W * 0.37]),
    ]))

    # ---- 3. Méthodologie -----------------------------------------------------
    story.append(KeepTogether([P("3. Méthodologie et sources de données", "h1"),
                               simple_table(SOURCES, [W * 0.22, W * 0.44, W * 0.34])]))
    story.append(Spacer(1, 6))
    robust = Table([[
        bullets([
            "<b>Continuité de service</b> : chaque source est interrogée avec un délai maximal "
            "(6 s par requête) et en parallèle ; l'application ne se bloque jamais.",
            "<b>Mode secours automatique</b> : si une source ne répond pas, l'indicateur ou le titre "
            "concerné bascule sur une estimation simulée calibrée, sans affecter le reste.",
            "<b>Traçabilité</b> : badges « Données en direct », « Données partielles » ou « Mode secours » "
            "sur chaque écran, et statut des connexions dans la barre latérale.",
        ]),
        bullets([
            "<b>Fraîcheur</b> : les données sont mises en cache 1 heure ; bouton « Rafraîchir les données » "
            "dans la barre latérale.",
            "<b>Limites</b> : les estimations de secours (ordres de grandeur ~2024), la base KYC par pays "
            "et les taux de conversion en dollars sont <b>indicatifs</b>.",
            "<b>Hors Yahoo Finance</b> : BRVM (UEMOA), Bourse de Casablanca, Émirats et banques "
            "non cotées sont présentées en données simulées.",
        ]),
    ]], colWidths=[W / 2, W / 2])
    robust.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), TINT),
        ("LINEBEFORE", (0, 0), (0, -1), 3.5, PRUNE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 9),
    ]))
    story.append(robust)
    story.append(Spacer(1, 6))
    story.append(P(
        "Avertissement : l'application est un outil d'aide à la décision. Ses résultats ne constituent "
        "ni un conseil en investissement ni une évaluation officielle et doivent être validés par les "
        "équipes Finance, Risques et Conformité avant tout engagement du Groupe.", "small"))
    return story


def generate_user_guide_pdf() -> bytes:
    """Construit le mémorandum et renvoie les octets du PDF."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=25 * mm, bottomMargin=16 * mm,
        title="Guide utilisateur — BPCE International Strategy Hub",
        author="Groupe BPCE — Stratégie & Développement International",
    )
    doc.build(_build_story(), canvasmaker=_MemoCanvas)
    return buf.getvalue()


if __name__ == "__main__":
    out = "Guide_utilisateur_BPCE_Strategy_Hub.pdf"
    with open(out, "wb") as fh:
        fh.write(generate_user_guide_pdf())
    print(f"PDF généré : {out}")
