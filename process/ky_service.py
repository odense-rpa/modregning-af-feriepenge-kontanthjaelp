import httpx
import logging
import re
import json


from datafordeler import Datafordeler as DatafordelerClient
from datetime import datetime, timedelta
from decimal import Decimal
from ky_client import KYClientManager
from ky_client.models import (
    AfbrydType,
    Indtægter,
    IndtægterType,
    Ydelsesarter,
    RedigerOpgave,
    Journalnotat,
)
from odk_tools.reporting import report
from pathlib import Path
from process.config import get_excel_mapping
from sbsip import sbsip

# Set by main.py after initialization
ky: KYClientManager = None  # type: ignore[assignment]
datafordeler: DatafordelerClient = None  # type: ignore[assignment]


def _nettoficer_beløb(ferieoplysninger: dict, skatteoplysninger: dict) -> Decimal:
    tilladte_typer = {"Bikort", "Hovedkort", "Hovedkort med A-skat pct"}

    def _parse_dato(value: str) -> datetime:
        text = str(value).strip()
        for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        raise ValueError(f"Ugyldig Anvendelsesdato: {value}")

    def _normaliser_trækprocent(
        value: str | float | int | Decimal | None,
    ) -> Decimal | None:
        if value is None:
            return None

        text = str(value).strip()
        if not text or text == "-":
            return None

        text = text.replace("%", "")
        procent = _to_danish_decimal(text)

        if procent > Decimal("1"):
            procent = procent / Decimal("100")

        return procent

    bruttobeløb = _to_danish_decimal(ferieoplysninger["Udbetalte feriepenge"])
    if ferieoplysninger.get("Før Skat") != "Ja":
        return bruttobeløb.quantize(Decimal("0.01"))

    rows = []
    if isinstance(skatteoplysninger, list):
        rows = [row for row in skatteoplysninger if isinstance(row, dict)]
    elif isinstance(skatteoplysninger, dict):
        for value in skatteoplysninger.values():
            if isinstance(value, list):
                rows.extend(row for row in value if isinstance(row, dict))

    kandidat_rows = []
    for row in rows:
        if str(row.get("Kilde", "")).strip() != "ABONNEMENT":
            continue

        if str(row.get("Type", "")).strip() not in tilladte_typer:
            continue

        anvendelsesdato = row.get("Anvendelsesdato")
        if anvendelsesdato is None:
            continue

        try:
            dato = _parse_dato(str(anvendelsesdato))
        except ValueError:
            continue

        kandidat_rows.append((dato, row))

    if len(kandidat_rows) == 0:
        raise ValueError(
            "Ingen gyldig skatteoplysning fundet med Kilde=ABONNEMENT og gyldig Type"
        )

    kandidat_rows.sort(key=lambda item: item[0], reverse=True)
    nyeste_skatteoplysning = kandidat_rows[0][1]

    trækprocent = _normaliser_trækprocent(
        nyeste_skatteoplysning.get("A-skattetrækprocent")
    )
    if trækprocent is None:
        trækprocent = _normaliser_trækprocent(nyeste_skatteoplysning.get("Trækprocent"))

    if trækprocent is None:
        raise ValueError(
            "A-skattetrækprocent og Trækprocent mangler i nyeste skatteoplysning"
        )

    netto_beløb = bruttobeløb * (Decimal("1.00") - trækprocent)
    return netto_beløb.quantize(Decimal("0.01"))


def _normalize_beloeb(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value)).strip()
    text = re.sub(r"\s*kr\.?$", "", text, flags=re.IGNORECASE)
    return text.strip()


def _to_danish_decimal(val: str | float) -> Decimal:
    if isinstance(val, str):
        normalized = val.replace(".", "").replace(",", ".")
        return Decimal(normalized)

    return Decimal(str(val))


def _match_opgave_detaljer(initierede_hændelser):
    feriepenge_hændelse = next(
        (
            hændelse
            for hændelse in initierede_hændelser
            if str(hændelse["Hændelsetype"]).startswith("Feriepenge udbetalt")
        ),
        None,
    )
    if feriepenge_hændelse is None:
        raise ValueError("Feriepenge udbetalt hændelse blev ikke fundet")

    pattern = re.compile(
        r"fra\s+(\d{2}-\d{2}-\d{4})\s+til\s+(\d{2}-\d{2}-\d{4})\.\s+([\d.]+,\d{2})\s+kr\.\s+med disposition\s+(\d{2}-\d{2}-\d{4})"
    )

    match = pattern.search(feriepenge_hændelse["Hændelsetype"])
    if match is None:
        raise ValueError(
            "Kunne ikke udlede ferieperiode, beløb og dispositionsdato fra hændelsetype"
        )

    detaljer = {
        "ferie_startdato": match.group(1),
        "ferie_enddato": match.group(2),
        "beløb": match.group(3),
        "dispositionsdato": match.group(4),
    }

    return detaljer


def _match_ferieoplysninger(ferieoplysninger, opgave_detaljer) -> dict | None:
    ferieperioder = ferieoplysninger["Ferieperioder fra Feriekonto"]
    matching_row = next(
        (
            row
            for row in ferieperioder
            if str(row.get("Dispositionsdato", "")).strip()
            == opgave_detaljer["dispositionsdato"]
            and str(row.get("Første feriedag", "")).strip()
            == opgave_detaljer["ferie_startdato"]
            and _normalize_beloeb(row.get("Udbetalte feriepenge", ""))
            == _normalize_beloeb(opgave_detaljer["beløb"])
        ),
        None,
    )

    return matching_row


def _indenfor_nuværende_ferieår(
    date_to_check: datetime, today: datetime | None = None
) -> bool:
    if today is None:
        today = datetime.now()

    if today.month >= 9:
        ferieår_start = datetime(today.year, 9, 1)
        ferieår_end = datetime(today.year + 1, 12, 31)
    else:
        ferieår_start = datetime(today.year - 1, 9, 1)
        ferieår_end = datetime(today.year, 12, 31)

    return ferieår_start <= date_to_check <= ferieår_end


def _har_feriedag_i_nuværende_ferieår(ferieperioder: list[dict]) -> bool:
    for row in ferieperioder:
        for felt in ["Første feriedag", "Sidste feriedag", "Sidsteferiedag"]:
            dato_text = str(row.get(felt, "")).strip()
            if not dato_text or dato_text == "-":
                continue

            try:
                dato = datetime.strptime(dato_text, "%d-%m-%Y")
            except ValueError:
                continue

            if _indenfor_nuværende_ferieår(dato):
                return True

    return False


def _har_angiv_ferieperioder_opgave(borgeroplysninger: dict) -> bool:
    ubehandlede_opgaver = borgeroplysninger.get("UbehandledeOpgaver", [])

    for opgave in ubehandlede_opgaver:
        if isinstance(opgave, dict):
            opgave_tekst = str(opgave.get("Opgave", ""))
        else:
            opgave_tekst = str(opgave)

        if "angiv ferieperioder" in opgave_tekst.casefold():
            return True

    return False


def _hent_nyeste_htf_sagsnøgle(borgeroplysninger: dict) -> str | None:
    sagsoversigt = borgeroplysninger.get("Sagsoversigt")
    if not isinstance(sagsoversigt, list) or len(sagsoversigt) == 0:
        return None

    htf_sager = []
    for sag in sagsoversigt:
        if not isinstance(sag, dict):
            continue

        sagsnøgle = str(sag.get("Sagsnøgle", "")).strip()
        if not sagsnøgle.startswith("HTF-"):
            continue

        startdato_text = str(sag.get("Startdato", "")).strip()
        try:
            startdato = datetime.strptime(startdato_text, "%d-%m-%Y")
        except ValueError:
            continue

        htf_sager.append((startdato, sagsnøgle))

    if len(htf_sager) == 0:
        return None

    htf_sager.sort(key=lambda item: item[0], reverse=True)
    return htf_sager[0][1]


def hent_opgave_detaljer_og_ferieoplysninger(
    cpr: str, opgave_id: str
) -> tuple[dict, dict | None]:
    initierede_hændelser = ky.borgere.åbn_opgave(cpr, opgave_id)
    opgave_detaljer = _match_opgave_detaljer(initierede_hændelser)
    ky.borgere.afbryd_opgave(cpr, opgave_id, AfbrydType.AFBRYD)
    ferieoplysninger = ky.borgere.hent_ferieoplysninger(cpr)
    matchede_ferieoplysninger = _match_ferieoplysninger(
        ferieoplysninger, opgave_detaljer
    )

    return opgave_detaljer, matchede_ferieoplysninger


def skal_ignorere_opgave(
    logger: logging.Logger,
    data: dict,
    borgeroplysninger: dict,
    opgave_detaljer: dict,
    ferieoplysninger: dict | None,
) -> bool:
    if ferieoplysninger is None:
        report(
            "modregning_af_feriepenge_kontanthjaelp",
            "Manuel behandling",
            {
                "Cpr": data["CPR-nummer"],
                "Årsag": "Ferieoplysninger kunne ikke matches med opgave detaljer",
            },
        )
        return True

    dispositionsdato = datetime.strptime(
        opgave_detaljer["dispositionsdato"], "%d-%m-%Y"
    )
    now = datetime.now()
    if dispositionsdato.year != now.year or dispositionsdato.month != now.month:
        report(
            "modregning_af_feriepenge_kontanthjaelp",
            "Manuel behandling",
            {
                "Cpr": data["CPR-nummer"],
                "Årsag": "Dispositionsdato for feriepenge er ikke i indeværende måned",
            },
        )
        return True

    if borgeroplysninger.get("Personoplysninger", {}).get("Civilstand", "") == "Gift":
        report(
            "modregning_af_feriepenge_kontanthjaelp",
            "Manuel behandling",
            {"Cpr": data["CPR-nummer"], "Årsag": "Borger er gift"},
        )
        return True

    nyeste_htf_sagsnøgle = _hent_nyeste_htf_sagsnøgle(borgeroplysninger)
    if nyeste_htf_sagsnøgle is None:
        ky.borgere.godkend_opgave(cpr=data["CPR-nummer"], opgave_id=data["Opgave-Id"])
        report(
            "modregning_af_feriepenge_kontanthjaelp",
            "Godkendte opgaver",
            {
                "Cpr": data["CPR-nummer"],
                "Bemærkning": "Opgave godkendt automatisk, da ingen HTF sag blev fundet",
            },
        )
        return True

    if ferieoplysninger["Årsagskode"] not in [1510, 1511, 1513, 1561, 1586, 1587]:
        report(
            "modregning_af_feriepenge_kontanthjaelp",
            "Manuel behandling",
            {
                "Cpr": data["CPR-nummer"],
                "Årsag": "Årsagskode for feriepenge er udenfor scope for denne automatisering",
            },
        )
        return True

    if ferieoplysninger["Årsagskode"] in [1510, 1511, 1513, 1586, 1587]:
        ferieperioder = borgeroplysninger.get("Ferier")
        if isinstance(ferieperioder, list) and _har_feriedag_i_nuværende_ferieår(
            ferieperioder
        ):
            report(
                "modregning_af_feriepenge_kontanthjaelp",
                "Manuel behandling",
                {
                    "Cpr": data["CPR-nummer"],
                    "Årsag": "Ferieregistrering indenfor indeværende ferieår",
                },
            )
            return True

        if _har_angiv_ferieperioder_opgave(borgeroplysninger):
            report(
                "modregning_af_feriepenge_kontanthjaelp",
                "Manuel behandling",
                {
                    "Cpr": data["CPR-nummer"],
                    "Årsag": "Angiv ferieperioder opgave tilstede",
                },
            )
            return True

    return False


def indtast_indtægt(cpr: str, ferieoplysninger: dict, skatteoplysninger: dict) -> None:
    today = datetime.now()
    periode_fra = today.replace(day=1).strftime("%d-%m-%Y")
    next_month = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
    periode_til = (next_month - timedelta(days=1)).strftime("%d-%m-%Y")
    beløb = _nettoficer_beløb(ferieoplysninger, skatteoplysninger)

    template_path = (
        Path(__file__).resolve().parent.parent
        / "journalnotater"
        / (
            "1561.html"
            if ferieoplysninger.get("Årsagskode") == 1561
            else "Andre årsagskoder.html"
        )
    )
    journalnotat_indhold = template_path.read_text(encoding="utf-8")
    # TODO: Check om html behov overhovedet er til stede
    journalnotat_felter = {
        "Beløb": ferieoplysninger["Beløb"],
        "Dispositionsdato": ferieoplysninger["Dispositionsdato"],
        "Dato": datetime.now().strftime("%d-%m-%Y"),
        "Nettoficeret Beløb": str(beløb),
        "Måned": datetime.now().strftime("%B").lower(),
    }

    for nøgle, værdi in journalnotat_felter.items():
        journalnotat_indhold = journalnotat_indhold.replace(
            f"{{{{{nøgle}}}}}", str(værdi)
        )

    journalnotat = Journalnotat(
        indhold=journalnotat_indhold,
        sagstype="HTF",
        skabelongruppe="KH",
        skabelon="Agterskrivelse - feriepenge",
    )

    ky.borgere.indtast_indtægter(
        cpr=cpr,
        indtægter=Indtægter(
            indtaegtstype=IndtægterType.FERIEPENGE_SELVVALGT,
            beloeb=beløb,
            dispositionsdato=ferieoplysninger["Dispositionsdato"],
            periode_fra=periode_fra,
            periode_til=periode_til,
            timer_i_perioden=0,
            ydelsesarter=Ydelsesarter.HJAELP_TIL_FORSOERGGELSE,
        ),
        journalnotat=journalnotat,
    )


def afsend_brev_og_upload_til_ky(
    data: dict,
    borgeroplysninger: dict,
    ferieoplysninger: dict,
    skatteoplysninger: dict,
    word_template_path: str,
) -> None:
    regler = get_excel_mapping()

    felter = {
        "Beløb": ferieoplysninger["Beløb"],
        "Dispositionsdato": ferieoplysninger["Dispositionsdato"],
        "DD+11 dage": (datetime.now() + timedelta(days=11)).strftime("%d-%m-%Y"),
        "DD+12 dage": (datetime.now() + timedelta(days=12)).strftime("%d-%m-%Y"),
        "DD+40 dage": (datetime.now() + timedelta(days=40)).strftime("%d-%m-%Y"),
        "Netto beløb": _nettoficer_beløb(ferieoplysninger, skatteoplysninger),
        "Indeværende måned": str(datetime.now().strftime("%B")).lower(),
    }

    regel = next(
        (
            r
            for r in regler
            if isinstance(ferieoplysninger["Årsagskode"], int)
            and isinstance(r.get("Årsagskode"), int)
            and r["Årsagskode"] == ferieoplysninger["Årsagskode"]
        ),
        None,
    )

    if regel is None:
        raise ValueError(
            f"Ingen regel fundet for årsagskode: {ferieoplysninger['Årsagskode']}"
        )

    with open(f"{word_template_path}/{regel['Brevskabelon']}.docx", "rb") as f:
        response = httpx.post(
            "http://rpa-ats.odknet.dk:8331/render",
            files={"file": (f"{word_template_path}/{regel['Brevskabelon']}.docx", f)},
            data={"fields": json.dumps(felter)},
        )

    pdf_path = Path(
        f"{regel['Brevskabelon']} {datetime.now().strftime('%d-%m-%Y')}.pdf"  # TODO: Verify
    )

    pdf_path.write_bytes(response.content)

    adresse, post_nr = datafordeler.hent_adresse_til_sbsip(cpr=data["CPR-nummer"])
    sbsip.send_digital_post(
        cpr=data["CPR-nummer"],
        overskrift="Agterskrivelse - feriepenge",
        beskrivelse="",
        vedhæftet_fil=pdf_path,
        adresse=adresse,
        post_nr=post_nr,
    )

    sagsnøgle = _hent_nyeste_htf_sagsnøgle(borgeroplysninger)
    if sagsnøgle is None:
        raise ValueError("Ingen HTF sag fundet i Sagsoversigt")

    # Upload til KY
    ky.borgere.upload_dokument(
        cpr=data["CPR-nummer"],
        sagsnøgle=sagsnøgle,
        file_path=pdf_path,
    )

    pdf_path.unlink(missing_ok=True)


def rediger_opgave(data: dict, borgeroplysninger: dict) -> None:
    # Gå til borger
    ky.borgere.hent_borgersag(data["CPR-nummer"])

    # Rediger opgave
    ky.borgere.rediger_opgave(
        cpr=data["CPR-nummer"],
        opgave_id=data["Opgave-Id"],
        ændringer=RedigerOpgave(
            opfølgningsopgavetype="KH - Tyra ferie",
            forfalds_dato=datetime.fromordinal(
                datetime.now().toordinal() + 11
            ).strftime("%d-%m-%Y"),
        ),
    )

    ky.borgere.luk_borgersag(borgeroplysninger["pId"])
