import argparse
from datetime import date, datetime, timedelta
from asyncio.log import logger
import logging
import os
import sys
from venv import logger

from automation_server_client import (
    AutomationServer,
    Workqueue,
    Credential,
    WorkItemStatus,
)
from datafordeler import Datafordeler as DatafordelerClient
from ky_client import KYClientManager
from odk_tools.tracking import Tracker
from odk_tools.reporting import report
from process.config import load_excel_mapping
from sbsip import sbsip
import process.ky_service as ky_service
from process.ky_service import (
    hent_opgave_detaljer_og_ferieoplysninger,
    skal_ignorere_opgave,
    indtast_indtægt,
    afsend_brev_og_upload_til_ky,
    rediger_opgave,
)

datafordeler: DatafordelerClient
ky: KYClientManager
tracker: Tracker
proces_navn = "Modregning af feriepenge - Kontanthjælp"


def populate_expired_queue(workqueue: Workqueue) -> None:
    opgaver = ky.opgaveindbakke.hent_opgaver("KH - 34. Tyra ferie")

    def parse_forfaldsdato(opgave: dict) -> date | None:
        forfaldsdato = opgave.get("Forfaldsdato")
        if not forfaldsdato:
            return None

        if isinstance(forfaldsdato, date):
            return forfaldsdato

        if not isinstance(forfaldsdato, str):
            return None

        try:
            return datetime.strptime(forfaldsdato.strip(), "%d-%m-%Y").date()
        except ValueError:
            return None

    cutoff = datetime.now().date() - timedelta(days=1)
    opgaver = [
        opgave
        for opgave in opgaver
        if (forfaldsdato := parse_forfaldsdato(opgave)) is not None
        and forfaldsdato <= cutoff
    ]

    for opgave in opgaver:
        workqueue.add_item(opgave, str(opgave["Opgave-Id"]))


def process_expired_queue(workqueue: Workqueue):
    for item in workqueue:
        with item:
            data = item.data  # Item data deserialized from json as dict

            try:
                # TODO: Test
                ky.borgere.godkend_opgave(data["CPR-nummer"], data["Opgave-Id"])

                tracker.track_task(proces_navn)

            except Exception as e:
                report(
                    "modregning_af_feriepenge_kontanthjaelp",
                    "Fejl",
                    {"Cpr": data["CPR-nummer"], "Fejl": str(e)},
                )
                logger.error(f"Error processing expired item: {data}. Error: {e}")
                item.fail(str(e))


def populate_queue(workqueue: Workqueue) -> None:
    logger = logging.getLogger(__name__)

    opgaver = ky.opgaveindbakke.hent_opgaver("KH - 07. Feriepenge")
    opgaver = [
        opgave
        for opgave in opgaver
        if opgave["Opgavenavn"]
        == "Opfølgningsopgave - Feriekonto: Ferieperiode tilføjet"
    ]

    for opgave in opgaver:
        eksisterende_kødata = workqueue.get_item_by_reference(opgave["Opgave-Id"])

        if len(eksisterende_kødata) > 0:
            continue

        workqueue.add_item(opgave, str(opgave["Opgave-Id"]))


def process_workqueue(workqueue: Workqueue):
    logger = logging.getLogger(__name__)

    for item in workqueue:
        with item:
            data = item.data  # Item data deserialized from json as dict
            borgeroplysninger = None

            try:
                # Hent oplysninger
                borgeroplysninger = ky.borgere.hent_borgersag(data["CPR-nummer"])
                opgave_detaljer, ferieoplysninger = (
                    hent_opgave_detaljer_og_ferieoplysninger(
                        data["CPR-nummer"], data["Opgave-Id"]
                    )
                )

                # Kontroller oplysninger
                if (
                    skal_ignorere_opgave(
                        logger,
                        data,
                        borgeroplysninger,
                        opgave_detaljer,
                        ferieoplysninger,
                    )
                    or ferieoplysninger is None
                ):
                    continue

                skatteoplysninger = ky.borgere.hent_skatteoplysninger(
                    data["CPR-nummer"]
                )

                # Udfør handlinger
                indtast_indtægt(data["CPR-nummer"], ferieoplysninger, skatteoplysninger)
                afsend_brev_og_upload_til_ky(
                    data,
                    borgeroplysninger,
                    ferieoplysninger,
                    skatteoplysninger,
                    args.word_template_path,
                )
                rediger_opgave(data, borgeroplysninger)

                report(
                    "modregning_af_feriepenge_kontanthjaelp",
                    "Afsendt agterskrivelse",
                    {"Cpr": data["CPR-nummer"]},
                )

                tracker.track_task(proces_navn)

            except Exception as e:
                report(
                    "modregning_af_feriepenge_kontanthjaelp",
                    "Fejl",
                    {"Cpr": data["CPR-nummer"], "Fejl": str(e)},
                )
                logger.error(f"Error processing item: {data}. Error: {e}")
                item.fail(str(e))
            finally:
                if borgeroplysninger is not None:
                    ky.borgere.luk_borgersag(borgeroplysninger["pId"])


if __name__ == "__main__":
    ats = AutomationServer.from_environment()
    workqueue = ats.workqueue()

    roboa = Credential.get_credential("RoboA")
    tracking_credential = Credential.get_credential("Odense SQL Server")
    SBSip_credential = Credential.get_credential("SBSip - produktion")

    # Overskriv stien i forbindelse med udvikling
    certifikat_sti = os.getenv(
        "CERTIFIKATER", "/certifikater"
    )  # TODO: ./certifikater ved test
    datafordeler = DatafordelerClient(
        certifikat_sti=os.path.join(certifikat_sti, "datafordeler.crt"),
        certifikat_nøglefil=os.path.join(certifikat_sti, "datafordeler.key"),
    )

    tracker = Tracker(
        username=tracking_credential.username, password=tracking_credential.password
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

    ky_service.ky = ky
    ky_service.datafordeler = datafordeler

    # Parse command line arguments
    parser = argparse.ArgumentParser(description=proces_navn)
    parser.add_argument(
        "--excel-file",
        default="Regelsæt.xlsx",
        help="Path to the Excel file containing mapping data (default: Regelsæt.xlsx)",
    )
    parser.add_argument(
        "--queue",
        action="store_true",
        help="Populate the queue with test data and exit",
    )
    parser.add_argument("--word-template-path", default="./agterskrivelser")
    parser.add_argument(
        "--queue-expired",
        action="store_true",
        help="Populate only expired items in a seperate queue",
    )
    parser.add_argument(
        "--process-expired",
        action="store_true",
        help="Process only expired items in a seperate queue",
    )

    args = parser.parse_args()

    if not os.path.isfile(args.excel_file):
        raise FileNotFoundError(f"Excel file not found: {args.excel_file}")

    load_excel_mapping(args.excel_file)

    # Queue management
    if "--queue" in sys.argv:
        workqueue.clear_workqueue(WorkItemStatus.NEW)
        populate_queue(workqueue)
        exit(0)

    if args.queue_expired:
        oprydningskø = Workqueue.get_workqueue(80)
        populate_expired_queue(oprydningskø)
        exit(0)

    if args.process_expired:
        oprydningskø = Workqueue.get_workqueue(80)
        process_expired_queue(oprydningskø)
        exit(0)

    # Process workqueue
    process_workqueue(workqueue)
