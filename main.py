import logging
import re
import sys

from decimal import Decimal
from datetime import datetime
from automation_server_client import AutomationServer, Workqueue, WorkItemError, Credential, WorkItemStatus
from ky_client import KYClientManager
from ky_client.models import AfbrydType, Indtægter, IndtægterType, Ydelsesarter, RedigerOpgave
from odk_tools.tracking import Tracker
from sbsip import sbsip

ky: KYClientManager
tracker: Tracker


def _normalize_beloeb(value: str) -> str:
        text = re.sub(r"\s+", " ", str(value)).strip()
        text = re.sub(r"\s*kr\.?$", "", text, flags=re.IGNORECASE)
        return text.strip()

def _to_danish_decimal(val: str | float) -> Decimal:
    if isinstance(val, str):
        normalized = val.replace(".", "").replace(",", ".")
        return Decimal(normalized)

    return Decimal(str(val))


def match_opgave_detaljer(initierede_hændelser):
    feriepenge_hændelse = next(
        (
            hændelse
            for hændelse in initierede_hændelser
            if str(hændelse["Hændelsetype"]).startswith("Feriepenge udbetalt")
        ),
        None,
    )
    assert feriepenge_hændelse is not None, (
        "Feriepenge udbetalt hændelse blev ikke fundet"
    )

    pattern = re.compile(
        r"fra\s+(\d{2}-\d{2}-\d{4})\s+til\s+(\d{2}-\d{2}-\d{4})\.\s+([\d.]+,\d{2})\s+kr\.\s+med disposition\s+(\d{2}-\d{2}-\d{4})"
    )

    match = pattern.search(feriepenge_hændelse["Hændelsetype"])
    assert match is not None, (
        "Kunne ikke udlede ferieperiode, beløb og dispositionsdato fra hændelsetype"
    )

    detaljer = {
        "ferie_startdato": match.group(1),
        "ferie_enddato": match.group(2),
        "beløb": match.group(3),
        "dispositionsdato": match.group(4),
    }

    return detaljer


def match_ferieoplysninger(ferieoplysninger, opgave_detaljer) -> dict|None:
    ferieperioder = ferieoplysninger["Ferieperioder fra Feriekonto"]
    matching_row = next(
        (
            row
            for row in ferieperioder
            if str(row.get("Dispositionsdato", "")).strip() == opgave_detaljer["dispositionsdato"]            
            and str(row.get("Første feriedag", "")).strip() == opgave_detaljer["ferie_startdato"]
            and _normalize_beloeb(row.get("Udbetalte feriepenge", ""))
            == _normalize_beloeb(opgave_detaljer["beløb"])
        ),
        None,
    )

    return matching_row


def indenfor_nuværende_ferieår(date_to_check: datetime, today: datetime | None = None) -> bool:    
    if today is None:
        today = datetime.now()

    if today.month >= 9:
        ferieår_start = datetime(today.year, 9, 1)
        ferieår_end = datetime(today.year + 1, 12, 31)
    else:
        ferieår_start = datetime(today.year - 1, 9, 1)
        ferieår_end = datetime(today.year, 12, 31)

    return ferieår_start <= date_to_check <= ferieår_end


def har_angiv_ferieperioder_opgave(borgeroplysninger: dict) -> bool:
    ubehandlede_opgaver = borgeroplysninger.get("UbehandledeOpgaver", [])

    for opgave in ubehandlede_opgaver:
        if isinstance(opgave, dict):
            opgave_tekst = str(opgave.get("Opgave", ""))
        else:
            opgave_tekst = str(opgave)

        if "angiv ferieperioder" in opgave_tekst.casefold():
            return True

    return False


def populate_queue(workqueue: Workqueue):
    logger = logging.getLogger(__name__)
    opgaver = ky.opgaveindbakke.hent_opgaver("KH - 07. Feriepenge")
    opgaver = [opgave for opgave in opgaver if opgave['Opgavenavn'] == "Opfølgningsopgave - Feriekonto: Ferieperiode tilføjet"]
    
    for opgave in opgaver:
        eksisterende_kødata = workqueue.get_item_by_reference(opgave["Opgave-Id"])

        if len(eksisterende_kødata) > 0:
            continue

        workqueue.add_item(opgave, str(opgave["Opgave-Id"]))


def hent_opgave_detaljer_og_ferieoplysninger(cpr: str, opgave_id: str) -> tuple[dict, dict | None]:
    initierede_hændelser = ky.borgere.åbn_opgave(cpr, opgave_id)
    opgave_detaljer = match_opgave_detaljer(initierede_hændelser)
    ky.borgere.afbryd_opgave(cpr, opgave_id, AfbrydType.AFBRYD)
    ferieoplysninger = ky.borgere.hent_ferie_oplysninger(cpr)
    matchede_ferieoplysninger = match_ferieoplysninger(ferieoplysninger, opgave_detaljer)

    return opgave_detaljer, matchede_ferieoplysninger


def skal_ignorere_opgave(
    logger: logging.Logger,
    data: dict,
    borgeroplysninger: dict,
    opgave_detaljer: dict,
    ferieoplysninger: dict | None,
) -> bool:
    if ferieoplysninger is None:
        logger.warning(
            f"Ferieoplysninger kunne ikke matches for borger med CPR {data['CPR-nummer']} og opgave {data['Opgave-Id']}. Opgave ignoreres."
        )
        return True

    dispositionsdato = datetime.strptime(opgave_detaljer["dispositionsdato"], "%d-%m-%Y")
    now = datetime.now()
    if dispositionsdato.year != now.year or dispositionsdato.month != now.month:
        return True

    # TODO: Afklar om borger er gift.

    htf_sag = next(
        (
            sag
            for sag in borgeroplysninger.get("Sagsoversigt", [])
            if str(sag.get("Sagsnøgle", "")).startswith("HTF-")
        ),
        None,
    )
    if htf_sag is not None:
        return True

    if ferieoplysninger["Årsagskode"] not in [1510, 1511, 1513, 1561, 1586, 1587]:
        return True

    if ferieoplysninger["Årsagskode"] in [1510, 1511, 1513, 1586, 1587]:
        if indenfor_nuværende_ferieår(dispositionsdato):
            return True

        if har_angiv_ferieperioder_opgave(borgeroplysninger):
            return True

        return True

    return False


def process_workqueue(workqueue: Workqueue):
    logger = logging.getLogger(__name__)

    for item in workqueue:
        with item:
            data = item.data  # Item data deserialized from json as dict
 
            try:
                # Hent oplysninger                
                borgeroplysninger = ky.borgere.hent_borgersag(data["CPR-nummer"])
                opgave_detaljer, ferieoplysninger = hent_opgave_detaljer_og_ferieoplysninger(
                    data["CPR-nummer"], data["Opgave-Id"]
                )

                # Kontroller oplysninger
                # TODO: verificer try.
                try:
                    if skal_ignorere_opgave(
                        logger,
                        data,
                        borgeroplysninger,
                        opgave_detaljer,
                        ferieoplysninger,
                    ) or ferieoplysninger is None:
                        continue
                    
                    # Udfør handling
                    # Agterskrivelsesflow:                    
                    beløb = _to_danish_decimal(ferieoplysninger["Udbetalte feriepenge"])

                    # Er det bruttoferiepenge? Ja - omregn indtægt til netto
                    if ferieoplysninger["Før Skat"] == "Ja":
                        beløb = 123.1 # TODO: Hent skatteoplysninger for seneste trækprocent på hovedkort? Skattekort fra eIndkomst -> A-skattetrækprocent/Trækprocent, sort by Anvendelsesdato desc
                        pass

                    # Indberet indtægt - TODO: Evt. journalnotat.
                    ky.borgere.indtast_indtægter(
                            cpr=data["CPR-nummer"],
                            indtægter=Indtægter(
                                indtaegtstype=IndtægterType.FERIEPENGE_SELVVALGT,
                                beloeb=float(beløb),
                                dispositionsdato=ferieoplysninger["Dispositionsdato"],
                                periode_fra="", #TODO: First date of the current month as string
                                periode_til="", #TODO: Last date of the current month as string
                                timer_i_perioden=0,
                                ydelsesarter=Ydelsesarter.HJAELP_TIL_FORSOERGGELSE
                            )
                    )                        

                    # TODO: Identificer brevskabelon + felter ud fra årsagskode
                    # TODO: Indlæs brevskabelon + udfyld felter
                    # Example:
                    # regel = next(
                    #         (
                    #             r
                    #             for r in regler
                    #             if isinstance(skema_navn, str)
                    #             and isinstance(r.get("Henvendelsestype"), str)
                    #             and r["Henvendelsestype"].strip().lower() == skema_navn.strip().lower()
                    #         ),
                    #         None,
                    #     )
                    
                    # with open(f"{word_template_path}/{regel['Brevskabelon']}.docx", "rb") as f:
                    #     response = httpx.post(
                    #         "http://rpa-ats.odknet.dk:8331/convert",
                    #         files={"file": (f"{word_template_path}/{regel['Brevskabelon']}.docx", f)},
                    #     )
                    # TODO: Handle naming
                    # pdf_path = Path(
                    #     f"{regel['Brevskabelon']} {datetime.now().strftime('%d-%m-%Y')}.pdf"
                    # )
                    # TODO: Gem fil lokalt i Linux
                    # pdf_path.write_bytes(response.content)


                    # Afsend via SBSIP                    
                    # adresse, post_nr = datafordeler.hent_adresse_til_sbsip(cpr=cpr)
                    # sbsip.send_digital_post(
                    #     cpr=cpr,
                    #     overskrift="Sådan behandler vi dine personoplysninger",
                    #     beskrivelse="",
                    #     vedhæftet_fil=pdf_path,
                    #     adresse=adresse,
                    #     post_nr=post_nr
                    # )
                    
                    
                    # Upload til KY
                    ky.borgere.upload_dokument(
                        cpr=data["CPR-nummer"],
                        sagsnøgle=data["Sagsnøgle"], # TODO: Afklar hvilken sag
                        file_path="path/to/file", #TODO: Path til genereret brev
                    )

                    # Slet fil i Linux
                    # pdf_path.unlink(missing_ok=True)

                    # Gå til borger
                    ky.borgere.hent_borgersag(data["CPR-nummer"])

                    # Rediger opgave
                    ky.borgere.rediger_opgave(
                        cpr=data["CPR-nummer"],
                        opgave_id=data["Opgave-Id"],
                        ændringer=RedigerOpgave(
                            opfølgningsopgavetype="KH - Tyra ferie", #TODO: verificer den præcise opgavetype
                            forfalds_dato="" #TODO: TODO: today + 11 dage som string
                        )
                    )

                    # TODO: Safeguard logik imod igangværende opgaver + Opfølgningsopgaver/Manuel opgave
                    ky.borgere.luk_borgersag(borgeroplysninger["pId"])

                    
                    # Proces 2 - opfølgningsproces
                    # Læs opgavepakke med navn "KH - 37.Tyra ferie", Evt. filtrer
                    # Kontroller om forfaldsdato er overskredet - Hvis nej luk opgave, hvis ja slet opgave.


                except Exception:
                    continue
                
            except Exception as e:
                # TODO: Reporting
                logger.error(f"Error processing item: {data}. Error: {e}")
                item.fail(str(e))


if __name__ == "__main__":
    ats = AutomationServer.from_environment()
    workqueue = ats.workqueue()

    roboa = Credential.get_credential("RoboA")
    tracking_credential = Credential.get_credential("Odense SQL Server")
    SBSip_credential = Credential.get_credential("SBSip - produktion")
        
    tracker = Tracker(
        username=tracking_credential.username, 
        password=tracking_credential.password
    )

    sbsip.start_sbsip(
        brugernavn=SBSip_credential.username,
        adgangskode=SBSip_credential.password,
    )
    
    ky = KYClientManager(
        username=f"{roboa.username}@odense.dk",
        password=roboa.password,
        idp=roboa.data["idp"],
    )

    # TODO: Indlæs Excel regelsæt
    # TODO: Reporting

    # Queue management
    if "--queue" in sys.argv:
        workqueue.clear_workqueue(WorkItemStatus.NEW)
        populate_queue(workqueue)
        exit(0)

    # Process workqueue
    process_workqueue(workqueue)
