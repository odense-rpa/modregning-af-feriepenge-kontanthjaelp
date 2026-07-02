# Modregning af feriepenge – kontanthjælp

Automatiserer modregning af feriepenge for borgere på kontanthjælp ved at hente opgaver fra KY, vurdere om de kan behandles automatisk, beregne nettobeløb, registrere indkomst og afsende digital agterskrivelse via SBSip.

## Hvad gør robotten?

1. Henter opgaver af typen *Opfølgningsopgave - Feriekonto: Ferieperiode tilføjet* fra KY-opgaveindbakken (KH - 07. Feriepenge) og fylder dem i arbejdskøen.
2. For hvert køelement hentes borgeroplysninger og ferieoplysninger fra KY og matches med opgavedetaljerne.
3. Opgaven sendes til manuel behandling hvis borgeren er låst, er gift, dispositionsdatoen ikke er i indeværende måned, ingen HTF-sag findes, årsagskoden er uden for scope, eller der allerede er ferieregistreringer eller en *angiv-ferieperioder*-opgave i indeværende ferieår.
4. Beregner nettobeløbet ved at fratrække skat (trækprocent fra SKAT-abonnementsoplysninger) fra bruttobeløbet, hvis udbetalingen er sket før skat.
5. Registrerer feriepenge som indkomst i KY med journalnotat udfyldt fra HTML-skabelon (mappe: `journalnotater/`).
6. Genererer agterskrivelse som Word-dokument (mappe: `agterskrivelser/`) udfyldt med borgerdata og sender det til PDF-rendering via intern HTTP-tjeneste på `rpa-ats.odknet.dk:8331/render`.
7. Sender den renderede PDF som digital post via SBSip til borgerens adresse, opslået via Datafordeler.
8. Uploader PDF-dokumentet til borgerens HTF-sag i KY.
9. Opdaterer opgaven i KY med en opfølgningsopgave (KH - Tyra ferie) med forfaldsdato 11 dage frem.
10. Håndterer separat en udløbskø (workqueue 80): henter opgaver fra KH - 34. Tyra ferie med overskredet forfaldsdato og godkender dem automatisk.

## Forudsætninger

- Python ≥ 3.13
- [`uv`](https://docs.astral.sh/uv/) til pakkehåndtering
- Adgang til **Automation Server** (arbejdskø)
- Adgang til **KY** (borgersager, opgaver, ferie- og skatteoplysninger)
- Adgang til **SBSip** (afsendelse af digital post)
- Adgang til **Datafordeler** (adresseoplysninger) inkl. gyldigt certifikat
- `Regelsæt.xlsx` med årsagskode-til-brevskabelon-mapping placeret i roden af projektet

## Installation

```sh
uv sync
```

## Konfiguration

Credentials registreres i Automation Server:
- `RoboA`
- `Odense SQL Server`
- `SBSip - produktion`

Miljøvariabler:

| Variabel | Beskrivelse |
|---|---|
| `ATS_URL` | URL til Automation Server API |
| `ATS_TOKEN` | Adgangstoken til Automation Server |
| `ATS_WORKQUEUE_OVERRIDE` | Overskriver arbejdskø-ID (bruges til test) |
| `CERTIFIKATER` | Sti til certifikater til Datafordeler-klienten (standard: `./certifikater`) |

## Kørsel

```sh
uv run python main.py --queue   # Fyld arbejdskøen
uv run python main.py           # Behandl arbejdskøen
```

## Afhængigheder

| Pakke | Formål |
|---|---|
| `automation-server-client` | Kommunikation med Automation Server – arbejdskø og credentials |
| `datafordeler` | Opslag af borgerens adresse til SBSip-afsendelse |
| `ky-client` | Al interaktion med KY: borgersager, opgaver, ferieoplysninger, skatteoplysninger, indkomstregistrering og dokumentupload |
| `odk-tools` | Aktivitetssporing (Tracker) og rapportering af hændelser |
| `openpyxl` | Indlæsning af `Regelsæt.xlsx` med årsagskode-til-brevskabelon-mapping |
| `ruff` | Python-linter (udviklingsværktøj) |
| `sbsip` | Afsendelse af digital post til borgere |

## GDPR og sikkerhed

Processen behandler følgende personoplysninger for borgere på kontanthjælp: CPR-numre, civilstand, ferieoplysninger (ferieperioder, udbetalte beløb, årsagskoder, dispositionsdatoer), skatteoplysninger (trækprocent) samt sags- og adresseoplysninger. Oplysningerne hentes fra KY og Datafordeler, anvendes til beregning og afsendelse af agterskrivelse, og gemmes ikke lokalt ud over under den aktive kørsels varighed.
