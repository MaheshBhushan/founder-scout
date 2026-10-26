"""Places the automatic rotation searches, and the time zone each contact lives in.

The rotation gives six world regions an equal share of runs, and inside each region
it visits hubs in a fixed shuffled order, so no city is favoured. Time zones come from
the contact's location text, then the country code the extraction model reports, then
the email or website country domain, then the place the run searched.
"""

from __future__ import annotations

import random
import re

# Six regions with an equal share of runs; each lists its tech hubs and their time zones.
# Mainland China is left out of the rotation on purpose. Germany keeps its own region because German
# contacts get the working-student track.
REGIONS: dict[str, dict[str, str]] = {
    "Germany": {p: "Europe/Berlin" for p in (
        "Berlin", "Munich", "Hamburg", "Frankfurt", "Cologne", "Stuttgart", "Düsseldorf", "Leipzig", "Karlsruhe", "Dresden",
        "Aachen", "Nuremberg", "Bonn", "Mannheim", "Heidelberg", "Darmstadt", "Hanover", "Bremen", "Münster", "Essen",
        "Dortmund", "Bochum", "Freiburg", "Potsdam", "Kiel", "Jena", "Würzburg", "Regensburg", "Augsburg", "Ulm", "Mainz",
        "Wiesbaden", "Saarbrücken", "Erlangen", "Paderborn", "Göttingen", "Magdeburg", "Rostock",
    )},
    "Europe": {
        "London": "Europe/London", "Manchester": "Europe/London", "Edinburgh": "Europe/London", "Bristol": "Europe/London",
        "Oxford": "Europe/London", "Leeds": "Europe/London", "Glasgow": "Europe/London", "Belfast": "Europe/London",
        "Dublin": "Europe/Dublin", "Cork": "Europe/Dublin", "Galway": "Europe/Dublin",
        "Paris": "Europe/Paris", "Lyon": "Europe/Paris", "Toulouse": "Europe/Paris", "Bordeaux": "Europe/Paris",
        "Nantes": "Europe/Paris", "Lille": "Europe/Paris", "Marseille": "Europe/Paris", "Grenoble": "Europe/Paris",
        "Amsterdam": "Europe/Amsterdam", "Rotterdam": "Europe/Amsterdam", "Utrecht": "Europe/Amsterdam", "Eindhoven": "Europe/Amsterdam",
        "Delft": "Europe/Amsterdam", "Brussels": "Europe/Brussels", "Antwerp": "Europe/Brussels", "Ghent": "Europe/Brussels",
        "Leuven": "Europe/Brussels", "Luxembourg": "Europe/Luxembourg",
        "Zurich": "Europe/Zurich", "Geneva": "Europe/Zurich", "Lausanne": "Europe/Zurich", "Basel": "Europe/Zurich",
        "Vienna": "Europe/Vienna", "Graz": "Europe/Vienna", "Linz": "Europe/Vienna",
        "Madrid": "Europe/Madrid", "Barcelona": "Europe/Madrid", "Valencia": "Europe/Madrid", "Seville": "Europe/Madrid",
        "Málaga": "Europe/Madrid", "Bilbao": "Europe/Madrid", "Lisbon": "Europe/Lisbon", "Porto": "Europe/Lisbon", "Braga": "Europe/Lisbon",
        "Milan": "Europe/Rome", "Rome": "Europe/Rome", "Turin": "Europe/Rome", "Bologna": "Europe/Rome", "Florence": "Europe/Rome",
        "Valletta": "Europe/Malta", "Athens": "Europe/Athens", "Thessaloniki": "Europe/Athens", "Nicosia": "Asia/Nicosia",
        "Stockholm": "Europe/Stockholm", "Gothenburg": "Europe/Stockholm", "Malmö": "Europe/Stockholm", "Uppsala": "Europe/Stockholm",
        "Copenhagen": "Europe/Copenhagen", "Aarhus": "Europe/Copenhagen", "Oslo": "Europe/Oslo", "Bergen": "Europe/Oslo",
        "Trondheim": "Europe/Oslo", "Helsinki": "Europe/Helsinki", "Espoo": "Europe/Helsinki", "Tampere": "Europe/Helsinki",
        "Reykjavik": "Atlantic/Reykjavik", "Tallinn": "Europe/Tallinn", "Tartu": "Europe/Tallinn", "Riga": "Europe/Riga",
        "Vilnius": "Europe/Vilnius", "Kaunas": "Europe/Vilnius",
        "Warsaw": "Europe/Warsaw", "Kraków": "Europe/Warsaw", "Wrocław": "Europe/Warsaw", "Gdańsk": "Europe/Warsaw", "Poznań": "Europe/Warsaw",
        "Prague": "Europe/Prague", "Brno": "Europe/Prague", "Bratislava": "Europe/Bratislava", "Budapest": "Europe/Budapest",
        "Bucharest": "Europe/Bucharest", "Cluj-Napoca": "Europe/Bucharest", "Iași": "Europe/Bucharest", "Sofia": "Europe/Sofia",
        "Belgrade": "Europe/Belgrade", "Novi Sad": "Europe/Belgrade", "Zagreb": "Europe/Zagreb", "Ljubljana": "Europe/Ljubljana",
        "Kyiv": "Europe/Kyiv", "Lviv": "Europe/Kyiv", "Tbilisi": "Asia/Tbilisi", "Yerevan": "Asia/Yerevan",
    },
    "North America": {
        "San Francisco": "America/Los_Angeles", "Palo Alto": "America/Los_Angeles", "San Jose": "America/Los_Angeles",
        "Los Angeles": "America/Los_Angeles", "San Diego": "America/Los_Angeles", "Seattle": "America/Los_Angeles",
        "Portland": "America/Los_Angeles", "Salt Lake City": "America/Denver", "Denver": "America/Denver", "Boulder": "America/Denver",
        "Phoenix": "America/Phoenix", "Austin": "America/Chicago", "Dallas": "America/Chicago", "Houston": "America/Chicago",
        "Chicago": "America/Chicago", "Minneapolis": "America/Chicago", "Nashville": "America/Chicago", "St. Louis": "America/Chicago",
        "Detroit": "America/Detroit", "Ann Arbor": "America/Detroit", "Columbus": "America/New_York", "Pittsburgh": "America/New_York",
        "Philadelphia": "America/New_York", "New York": "America/New_York", "Boston": "America/New_York",
        "Washington DC": "America/New_York", "Baltimore": "America/New_York", "Raleigh": "America/New_York",
        "Atlanta": "America/New_York", "Miami": "America/New_York", "Toronto": "America/Toronto", "Waterloo": "America/Toronto",
        "Ottawa": "America/Toronto", "Montreal": "America/Toronto", "Quebec City": "America/Toronto", "Vancouver": "America/Vancouver",
        "Calgary": "America/Edmonton", "Edmonton": "America/Edmonton",
    },
    "Latin America": {
        "São Paulo": "America/Sao_Paulo", "Rio de Janeiro": "America/Sao_Paulo", "Campinas": "America/Sao_Paulo",
        "Belo Horizonte": "America/Sao_Paulo", "Curitiba": "America/Sao_Paulo", "Florianópolis": "America/Sao_Paulo",
        "Porto Alegre": "America/Sao_Paulo", "Recife": "America/Recife", "Mexico City": "America/Mexico_City",
        "Guadalajara": "America/Mexico_City", "Monterrey": "America/Monterrey", "Querétaro": "America/Mexico_City",
        "Buenos Aires": "America/Argentina/Buenos_Aires", "Rosario": "America/Argentina/Cordoba", "Montevideo": "America/Montevideo",
        "Santiago": "America/Santiago", "Valparaíso": "America/Santiago", "Lima": "America/Lima", "Bogotá": "America/Bogota",
        "Medellín": "America/Bogota", "Cali": "America/Bogota", "Barranquilla": "America/Bogota", "Quito": "America/Guayaquil",
        "Guayaquil": "America/Guayaquil", "La Paz": "America/La_Paz", "Asunción": "America/Asuncion",
        "Panama City": "America/Panama", "Guatemala City": "America/Guatemala", "Santo Domingo": "America/Santo_Domingo",
    },
    "Asia-Pacific": {
        "Bangalore": "Asia/Kolkata", "Mumbai": "Asia/Kolkata", "Delhi": "Asia/Kolkata", "Gurugram": "Asia/Kolkata",
        "Noida": "Asia/Kolkata", "Hyderabad": "Asia/Kolkata", "Pune": "Asia/Kolkata", "Chennai": "Asia/Kolkata",
        "Kolkata": "Asia/Kolkata", "Ahmedabad": "Asia/Kolkata", "Kochi": "Asia/Kolkata", "Jaipur": "Asia/Kolkata",
        "Chandigarh": "Asia/Kolkata", "Indore": "Asia/Kolkata", "Bhopal": "Asia/Kolkata", "Lucknow": "Asia/Kolkata",
        "Nagpur": "Asia/Kolkata", "Surat": "Asia/Kolkata", "Vadodara": "Asia/Kolkata", "Navi Mumbai": "Asia/Kolkata",
        "Thane": "Asia/Kolkata", "Nashik": "Asia/Kolkata", "Goa": "Asia/Kolkata", "Coimbatore": "Asia/Kolkata",
        "Madurai": "Asia/Kolkata", "Thiruvananthapuram": "Asia/Kolkata", "Mysuru": "Asia/Kolkata", "Mangaluru": "Asia/Kolkata",
        "Visakhapatnam": "Asia/Kolkata", "Vijayawada": "Asia/Kolkata", "Warangal": "Asia/Kolkata", "Bhubaneswar": "Asia/Kolkata",
        "Karachi": "Asia/Karachi", "Lahore": "Asia/Karachi", "Islamabad": "Asia/Karachi",
        "Dhaka": "Asia/Dhaka", "Colombo": "Asia/Colombo", "Kathmandu": "Asia/Kathmandu", "Almaty": "Asia/Almaty",
        "Tashkent": "Asia/Tashkent", "Singapore": "Asia/Singapore", "Kuala Lumpur": "Asia/Kuala_Lumpur", "Penang": "Asia/Kuala_Lumpur",
        "Jakarta": "Asia/Jakarta", "Bandung": "Asia/Jakarta", "Yogyakarta": "Asia/Jakarta", "Surabaya": "Asia/Jakarta",
        "Bangkok": "Asia/Bangkok", "Chiang Mai": "Asia/Bangkok", "Ho Chi Minh City": "Asia/Ho_Chi_Minh", "Hanoi": "Asia/Ho_Chi_Minh",
        "Da Nang": "Asia/Ho_Chi_Minh", "Manila": "Asia/Manila", "Cebu": "Asia/Manila", "Hong Kong": "Asia/Hong_Kong",
        "Taipei": "Asia/Taipei", "Tokyo": "Asia/Tokyo", "Osaka": "Asia/Tokyo", "Kyoto": "Asia/Tokyo", "Fukuoka": "Asia/Tokyo",
        "Seoul": "Asia/Seoul", "Busan": "Asia/Seoul", "Sydney": "Australia/Sydney", "Canberra": "Australia/Sydney",
        "Melbourne": "Australia/Melbourne", "Brisbane": "Australia/Brisbane", "Adelaide": "Australia/Adelaide",
        "Perth": "Australia/Perth", "Auckland": "Pacific/Auckland", "Wellington": "Pacific/Auckland", "Christchurch": "Pacific/Auckland",
    },
    "Middle East & Africa": {
        "Tel Aviv": "Asia/Jerusalem", "Jerusalem": "Asia/Jerusalem", "Haifa": "Asia/Jerusalem", "Beirut": "Asia/Beirut",
        "Amman": "Asia/Amman", "Dubai": "Asia/Dubai", "Abu Dhabi": "Asia/Dubai", "Riyadh": "Asia/Riyadh", "Jeddah": "Asia/Riyadh",
        "Doha": "Asia/Qatar", "Manama": "Asia/Bahrain", "Kuwait City": "Asia/Kuwait", "Muscat": "Asia/Muscat",
        "Istanbul": "Europe/Istanbul", "Ankara": "Europe/Istanbul", "Izmir": "Europe/Istanbul", "Cairo": "Africa/Cairo",
        "Alexandria": "Africa/Cairo", "Casablanca": "Africa/Casablanca", "Rabat": "Africa/Casablanca", "Tunis": "Africa/Tunis",
        "Algiers": "Africa/Algiers", "Lagos": "Africa/Lagos", "Abuja": "Africa/Lagos", "Accra": "Africa/Accra",
        "Dakar": "Africa/Dakar", "Abidjan": "Africa/Abidjan", "Nairobi": "Africa/Nairobi", "Kampala": "Africa/Kampala",
        "Kigali": "Africa/Kigali", "Dar es Salaam": "Africa/Dar_es_Salaam", "Addis Ababa": "Africa/Addis_Ababa",
        "Lusaka": "Africa/Lusaka", "Harare": "Africa/Harare", "Gaborone": "Africa/Gaborone", "Windhoek": "Africa/Windhoek",
        "Johannesburg": "Africa/Johannesburg", "Pretoria": "Africa/Johannesburg", "Cape Town": "Africa/Johannesburg",
        "Durban": "Africa/Johannesburg", "Port Louis": "Indian/Mauritius",
    },
}

SECTORS = ["AI", "fintech", "climate tech", "healthtech", "developer tools", "SaaS", "robotics", "edtech", "biotech", "e-commerce", "cybersecurity", "mobility"]


def build_rotation(seed: int = 7) -> list[tuple[str, str]]:
    """Every (sector, place) pair, one region per run in turn, in a fixed shuffled order inside each region."""
    per_region = []
    for i, places in enumerate(REGIONS.values()):
        pairs = [(sector, place) for sector in SECTORS for place in places]
        random.Random(seed + i).shuffle(pairs)
        per_region.append(pairs)
    # Smaller regions repeat until the largest has had a full pass.
    return [pairs[k % len(pairs)] for k in range(max(map(len, per_region))) for pairs in per_region]


ROTATION = build_rotation()
GERMAN_PLACES = [p.lower() for p in REGIONS["Germany"]] + ["germany", "deutschland", "münchen", "köln", "nürnberg"]

# Extra spellings and towns that page locations use, beyond the rotation places.
EXTRA_CITY_TZ = {
    # Areas and states that page locations name instead of a city.
    "mountain view": "America/Los_Angeles", "bay area": "America/Los_Angeles", "silicon valley": "America/Los_Angeles",
    "california": "America/Los_Angeles", "brooklyn": "America/New_York", "texas": "America/Chicago", "colorado": "America/Denver",
    "cambridge, uk": "Europe/London", "zug": "Europe/Zurich", "new delhi": "Asia/Kolkata", "gurgaon": "Asia/Kolkata",
    "cdmx": "America/Mexico_City",
    # Local spellings and spellings without accents.
    "münchen": "Europe/Berlin", "köln": "Europe/Berlin", "nürnberg": "Europe/Berlin", "hannover": "Europe/Berlin",
    "dusseldorf": "Europe/Berlin", "munster": "Europe/Berlin", "wurzburg": "Europe/Berlin", "saarbrucken": "Europe/Berlin",
    "gottingen": "Europe/Berlin", "zürich": "Europe/Zurich", "genève": "Europe/Zurich", "wien": "Europe/Vienna",
    "praha": "Europe/Prague", "milano": "Europe/Rome", "roma": "Europe/Rome", "torino": "Europe/Rome", "firenze": "Europe/Rome",
    "københavn": "Europe/Copenhagen", "warszawa": "Europe/Warsaw", "krakow": "Europe/Warsaw", "wroclaw": "Europe/Warsaw",
    "gdansk": "Europe/Warsaw", "poznan": "Europe/Warsaw", "lisboa": "Europe/Lisbon", "bruxelles": "Europe/Brussels",
    "brussel": "Europe/Brussels", "antwerpen": "Europe/Brussels", "gent": "Europe/Brussels", "sevilla": "Europe/Madrid",
    "malaga": "Europe/Madrid", "göteborg": "Europe/Stockholm", "malmo": "Europe/Stockholm", "reykjavík": "Atlantic/Reykjavik",
    "iasi": "Europe/Bucharest", "sao paulo": "America/Sao_Paulo", "florianopolis": "America/Sao_Paulo",
    "bogota": "America/Bogota", "medellin": "America/Bogota", "queretaro": "America/Mexico_City", "valparaiso": "America/Santiago",
    "asuncion": "America/Asuncion", "bengaluru": "Asia/Kolkata", "bombay": "Asia/Kolkata", "madras": "Asia/Kolkata", "calcutta": "Asia/Kolkata",
    "trivandrum": "Asia/Kolkata", "mysore": "Asia/Kolkata", "mangalore": "Asia/Kolkata", "vizag": "Asia/Kolkata",
    "secunderabad": "Asia/Kolkata", "saigon": "Asia/Ho_Chi_Minh",
}

CITY_TZ = {p.lower(): tz for places in REGIONS.values() for p, tz in places.items()} | EXTRA_CITY_TZ

# ISO country code: (time zone for unknown towns, country names). Countries spanning
# several zones use a middle zone, so a morning send stays inside working hours on
# both coasts (America/Denver is 07:00-10:00 in California and 10:00-13:00 in New York).
COUNTRIES: dict[str, tuple[str, tuple[str, ...]]] = {
    "de": ("Europe/Berlin", ("germany", "deutschland")), "at": ("Europe/Vienna", ("austria", "österreich")),
    "ch": ("Europe/Zurich", ("switzerland", "schweiz", "suisse", "svizzera")), "gb": ("Europe/London", ("united kingdom", "uk", "england", "scotland", "wales")),
    "ie": ("Europe/Dublin", ("ireland",)), "fr": ("Europe/Paris", ("france",)), "nl": ("Europe/Amsterdam", ("netherlands", "the netherlands", "holland", "nederland")),
    "be": ("Europe/Brussels", ("belgium", "belgique", "belgië")), "lu": ("Europe/Luxembourg", ("luxembourg",)), "es": ("Europe/Madrid", ("spain", "españa")),
    "pt": ("Europe/Lisbon", ("portugal",)), "it": ("Europe/Rome", ("italy", "italia")), "se": ("Europe/Stockholm", ("sweden", "sverige")),
    "dk": ("Europe/Copenhagen", ("denmark", "danmark")), "no": ("Europe/Oslo", ("norway", "norge")), "fi": ("Europe/Helsinki", ("finland", "suomi")),
    "is": ("Atlantic/Reykjavik", ("iceland",)), "ee": ("Europe/Tallinn", ("estonia",)), "lv": ("Europe/Riga", ("latvia",)),
    "lt": ("Europe/Vilnius", ("lithuania",)), "pl": ("Europe/Warsaw", ("poland", "polska")), "cz": ("Europe/Prague", ("czech republic", "czechia", "česko", "česká republika")),
    "sk": ("Europe/Bratislava", ("slovakia",)), "hu": ("Europe/Budapest", ("hungary",)), "ro": ("Europe/Bucharest", ("romania",)),
    "bg": ("Europe/Sofia", ("bulgaria",)), "gr": ("Europe/Athens", ("greece",)), "rs": ("Europe/Belgrade", ("serbia",)),
    "hr": ("Europe/Zagreb", ("croatia",)), "si": ("Europe/Ljubljana", ("slovenia",)), "ua": ("Europe/Kyiv", ("ukraine",)),
    "cy": ("Asia/Nicosia", ("cyprus",)), "mt": ("Europe/Malta", ("malta",)), "tr": ("Europe/Istanbul", ("turkey", "türkiye")),
    "us": ("America/Denver", ("united states", "usa", "u.s.")), "ca": ("America/Toronto", ("canada",)),
    "mx": ("America/Mexico_City", ("mexico", "méxico")), "br": ("America/Sao_Paulo", ("brazil", "brasil")),
    "ar": ("America/Argentina/Buenos_Aires", ("argentina",)), "co": ("America/Bogota", ("colombia",)), "cl": ("America/Santiago", ("chile",)),
    "pe": ("America/Lima", ("peru",)), "uy": ("America/Montevideo", ("uruguay",)), "cr": ("America/Costa_Rica", ("costa rica",)),
    "in": ("Asia/Kolkata", ("india",)), "pk": ("Asia/Karachi", ("pakistan",)), "bd": ("Asia/Dhaka", ("bangladesh",)),
    "lk": ("Asia/Colombo", ("sri lanka",)), "sg": ("Asia/Singapore", ("singapore",)), "my": ("Asia/Kuala_Lumpur", ("malaysia",)),
    "id": ("Asia/Jakarta", ("indonesia",)), "th": ("Asia/Bangkok", ("thailand",)), "vn": ("Asia/Ho_Chi_Minh", ("vietnam", "viet nam")),
    "ph": ("Asia/Manila", ("philippines",)), "hk": ("Asia/Hong_Kong", ("hong kong",)), "tw": ("Asia/Taipei", ("taiwan",)),
    "cn": ("Asia/Shanghai", ("china",)), "jp": ("Asia/Tokyo", ("japan",)), "kr": ("Asia/Seoul", ("south korea", "korea")),
    "au": ("Australia/Sydney", ("australia",)), "nz": ("Pacific/Auckland", ("new zealand",)),
    "il": ("Asia/Jerusalem", ("israel",)), "ae": ("Asia/Dubai", ("united arab emirates", "uae")), "sa": ("Asia/Riyadh", ("saudi arabia",)),
    "qa": ("Asia/Qatar", ("qatar",)), "jo": ("Asia/Amman", ("jordan",)), "eg": ("Africa/Cairo", ("egypt",)),
    "ma": ("Africa/Casablanca", ("morocco",)), "tn": ("Africa/Tunis", ("tunisia",)), "ng": ("Africa/Lagos", ("nigeria",)),
    "gh": ("Africa/Accra", ("ghana",)), "ke": ("Africa/Nairobi", ("kenya",)), "rw": ("Africa/Kigali", ("rwanda",)),
    "np": ("Asia/Kathmandu", ("nepal",)), "kz": ("Asia/Almaty", ("kazakhstan",)), "uz": ("Asia/Tashkent", ("uzbekistan",)),
    "ge": ("Asia/Tbilisi", ()), "am": ("Asia/Yerevan", ("armenia",)), "lb": ("Asia/Beirut", ("lebanon",)),
    "bh": ("Asia/Bahrain", ("bahrain",)), "kw": ("Asia/Kuwait", ("kuwait",)), "om": ("Asia/Muscat", ("oman",)),
    "dz": ("Africa/Algiers", ("algeria",)), "sn": ("Africa/Dakar", ("senegal",)), "ci": ("Africa/Abidjan", ("ivory coast", "côte d'ivoire")),
    "ug": ("Africa/Kampala", ("uganda",)), "tz": ("Africa/Dar_es_Salaam", ("tanzania",)), "zm": ("Africa/Lusaka", ("zambia",)),
    "zw": ("Africa/Harare", ("zimbabwe",)), "bw": ("Africa/Gaborone", ("botswana",)), "na": ("Africa/Windhoek", ("namibia",)),
    "mu": ("Indian/Mauritius", ("mauritius",)), "ec": ("America/Guayaquil", ("ecuador",)), "bo": ("America/La_Paz", ("bolivia",)),
    "py": ("America/Asuncion", ("paraguay",)), "pa": ("America/Panama", ("panama",)), "gt": ("America/Guatemala", ("guatemala",)),
    "do": ("America/Santo_Domingo", ("dominican republic",)), "pr": ("America/Puerto_Rico", ("puerto rico",)),
    "et": ("Africa/Addis_Ababa", ("ethiopia",)), "za": ("Africa/Johannesburg", ("south africa",)),
}
# Country-code domains that differ from the ISO code.
TLD_ALIASES = {"uk": "gb"}


def _pattern(names) -> re.Pattern:
    # Longest names first, whole words only, so "india" does not match "indiana" and "oman" not "romania".
    alts = sorted({n.lower() for n in names}, key=len, reverse=True)
    return re.compile(r"(?<![\w.])(" + "|".join(re.escape(a) for a in alts) + r")(?![\w])")


_CITY_RE = _pattern(CITY_TZ)
_COUNTRY_NAME = {n: cc for cc, (_, names) in COUNTRIES.items() for n in names}
_COUNTRY_RE = _pattern(_COUNTRY_NAME)


def tz_for(location: str = "", country: str = "", hosts: tuple[str, ...] = (), searched: str = "") -> str | None:
    """IANA time zone for a contact, or None when nothing identifies one."""
    loc = (location or "").lower()
    if m := _CITY_RE.search(loc):
        return CITY_TZ[m.group(1)]
    if m := _COUNTRY_RE.search(loc):
        return COUNTRIES[_COUNTRY_NAME[m.group(1)]][0]
    cc = (country or "").strip().lower()
    if cc in COUNTRIES:
        return COUNTRIES[cc][0]
    for host in hosts:
        tld = host.rsplit(".", 1)[-1].lower() if host and "." in host else ""
        tld = TLD_ALIASES.get(tld, tld)
        if tld in COUNTRIES and tld != "co":  # .co is used as a generic domain far more than for Colombia
            return COUNTRIES[tld][0]
    if searched:
        return CITY_TZ.get(searched.lower())
    return None
