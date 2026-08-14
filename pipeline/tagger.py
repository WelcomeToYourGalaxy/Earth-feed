"""
Region and topic tagging.

Two jobs:
  1. Assign each fact a region, so the imbalance counter can be honest.
  2. Assign meta-topic tags, which is how a fact gets matched to a context entry.

Deliberately rule-based. Tagging must not depend on a free tier being up.
"""

import re
import unicodedata

# ── Regions ────────────────────────────────────────────────────────────
# UN M49 subregions, plus polar / ocean / global which M49 has no slot for.

REGION_LABELS = {
    "northern_africa": "North Africa",
    "western_africa": "West Africa",
    "middle_africa": "Central Africa",
    "eastern_africa": "East Africa",
    "southern_africa": "Southern Africa",
    "northern_america": "North America",
    "central_america": "Central America",
    "caribbean": "Caribbean",
    "south_america": "South America",
    "central_asia": "Central Asia",
    "eastern_asia": "East Asia",
    "southern_asia": "South Asia",
    "south_eastern_asia": "Southeast Asia",
    "western_asia": "West Asia",
    "eastern_europe": "Eastern Europe",
    "northern_europe": "Northern Europe",
    "southern_europe": "Southern Europe",
    "western_europe": "Western Europe",
    "australia_nz": "Australia & NZ",
    "melanesia": "Melanesia",
    "micronesia": "Micronesia",
    "polynesia": "Polynesia",
    "polar": "Arctic & Antarctic",
    "ocean": "Ocean & high seas",
    "global": "Global / multi-region",
    "unlocated": "No place identified",
}

# Display order for the counter rail: geography, then the three non-M49 buckets.
REGION_ORDER = [
    "northern_africa", "western_africa", "middle_africa", "eastern_africa",
    "southern_africa", "northern_america", "central_america", "caribbean",
    "south_america", "central_asia", "eastern_asia", "southern_asia",
    "south_eastern_asia", "western_asia", "eastern_europe", "northern_europe",
    "southern_europe", "western_europe", "australia_nz", "melanesia",
    "micronesia", "polynesia", "polar", "ocean", "global", "unlocated",
]

COUNTRY_REGION = {
    # North Africa
    "algeria": "northern_africa", "egypt": "northern_africa", "libya": "northern_africa",
    "morocco": "northern_africa", "sudan": "northern_africa", "tunisia": "northern_africa",
    "western sahara": "northern_africa",
    # West Africa
    "benin": "western_africa", "burkina faso": "western_africa", "cape verde": "western_africa",
    "cabo verde": "western_africa", "ivory coast": "western_africa", "côte d'ivoire": "western_africa",
    "cote d'ivoire": "western_africa", "gambia": "western_africa", "ghana": "western_africa",
    "guinea": "western_africa", "guinea-bissau": "western_africa", "liberia": "western_africa",
    "mali": "western_africa", "mauritania": "western_africa", "niger": "western_africa",
    "nigeria": "western_africa", "senegal": "western_africa", "sierra leone": "western_africa",
    "togo": "western_africa",
    # Central Africa
    "angola": "middle_africa", "cameroon": "middle_africa",
    "central african republic": "middle_africa", "chad": "middle_africa",
    "republic of the congo": "middle_africa", "congo-brazzaville": "middle_africa",
    "democratic republic of the congo": "middle_africa", "dr congo": "middle_africa",
    "drc": "middle_africa", "congo-kinshasa": "middle_africa",
    "equatorial guinea": "middle_africa", "gabon": "middle_africa",
    "são tomé": "middle_africa", "sao tome": "middle_africa",
    # East Africa
    "burundi": "eastern_africa", "comoros": "eastern_africa", "djibouti": "eastern_africa",
    "eritrea": "eastern_africa", "ethiopia": "eastern_africa", "kenya": "eastern_africa",
    "madagascar": "eastern_africa", "malawi": "eastern_africa", "mauritius": "eastern_africa",
    "mozambique": "eastern_africa", "rwanda": "eastern_africa", "seychelles": "eastern_africa",
    "somalia": "eastern_africa", "south sudan": "eastern_africa", "tanzania": "eastern_africa",
    "uganda": "eastern_africa", "zambia": "eastern_africa", "zimbabwe": "eastern_africa",
    # Southern Africa
    "botswana": "southern_africa", "eswatini": "southern_africa", "swaziland": "southern_africa",
    "lesotho": "southern_africa", "namibia": "southern_africa", "south africa": "southern_africa",
    # Americas
    "canada": "northern_america", "united states": "northern_america", "usa": "northern_america",
    "u.s.": "northern_america", "mexico": "central_america", "belize": "central_america",
    "costa rica": "central_america", "el salvador": "central_america", "guatemala": "central_america",
    "honduras": "central_america", "nicaragua": "central_america", "panama": "central_america",
    "cuba": "caribbean", "dominican republic": "caribbean", "haiti": "caribbean",
    "jamaica": "caribbean", "puerto rico": "caribbean", "trinidad": "caribbean",
    "bahamas": "caribbean", "barbados": "caribbean", "guadeloupe": "caribbean",
    "martinique": "caribbean", "saint lucia": "caribbean", "grenada": "caribbean",
    "dominica": "caribbean", "antigua": "caribbean", "curacao": "caribbean",
    "curaçao": "caribbean", "aruba": "caribbean", "caribbean": "caribbean",
    "caraïbes": "caribbean", "caribe": "caribbean",
    "argentina": "south_america", "bolivia": "south_america", "brazil": "south_america",
    "chile": "south_america", "colombia": "south_america", "ecuador": "south_america",
    "guyana": "south_america", "paraguay": "south_america", "peru": "south_america",
    "suriname": "south_america", "uruguay": "south_america", "venezuela": "south_america",
    "french guiana": "south_america",
    # Asia
    "kazakhstan": "central_asia", "kyrgyzstan": "central_asia", "tajikistan": "central_asia",
    "turkmenistan": "central_asia", "uzbekistan": "central_asia",
    "china": "eastern_asia", "japan": "eastern_asia", "mongolia": "eastern_asia",
    "north korea": "eastern_asia", "south korea": "eastern_asia", "taiwan": "eastern_asia",
    "afghanistan": "southern_asia", "bangladesh": "southern_asia", "bhutan": "southern_asia",
    "india": "southern_asia", "iran": "southern_asia", "maldives": "southern_asia",
    "nepal": "southern_asia", "pakistan": "southern_asia", "sri lanka": "southern_asia",
    "brunei": "south_eastern_asia", "cambodia": "south_eastern_asia",
    "indonesia": "south_eastern_asia", "laos": "south_eastern_asia",
    "malaysia": "south_eastern_asia", "myanmar": "south_eastern_asia",
    "burma": "south_eastern_asia", "philippines": "south_eastern_asia",
    "singapore": "south_eastern_asia", "thailand": "south_eastern_asia",
    "timor-leste": "south_eastern_asia", "east timor": "south_eastern_asia",
    "vietnam": "south_eastern_asia",
    "armenia": "western_asia", "azerbaijan": "western_asia", "bahrain": "western_asia",
    "cyprus": "western_asia", "georgia": "western_asia", "iraq": "western_asia",
    "israel": "western_asia", "jordan": "western_asia", "kuwait": "western_asia",
    "lebanon": "western_asia", "oman": "western_asia", "palestine": "western_asia",
    "qatar": "western_asia", "saudi arabia": "western_asia", "syria": "western_asia",
    "turkey": "western_asia", "türkiye": "western_asia",
    "united arab emirates": "western_asia", "yemen": "western_asia",
    # Europe
    "belarus": "eastern_europe", "bulgaria": "eastern_europe", "czechia": "eastern_europe",
    "czech republic": "eastern_europe", "hungary": "eastern_europe", "moldova": "eastern_europe",
    "poland": "eastern_europe", "romania": "eastern_europe", "russia": "eastern_europe",
    "slovakia": "eastern_europe", "ukraine": "eastern_europe",
    "denmark": "northern_europe", "estonia": "northern_europe", "finland": "northern_europe",
    "iceland": "northern_europe", "ireland": "northern_europe", "latvia": "northern_europe",
    "lithuania": "northern_europe", "norway": "northern_europe", "sweden": "northern_europe",
    "united kingdom": "northern_europe", "uk": "northern_europe", "scotland": "northern_europe",
    "wales": "northern_europe", "england": "northern_europe",
    "albania": "southern_europe", "bosnia": "southern_europe", "croatia": "southern_europe",
    "greece": "southern_europe", "italy": "southern_europe", "kosovo": "southern_europe",
    "malta": "southern_europe", "montenegro": "southern_europe",
    "north macedonia": "southern_europe", "portugal": "southern_europe",
    "serbia": "southern_europe", "slovenia": "southern_europe", "spain": "southern_europe",
    "austria": "western_europe", "belgium": "western_europe", "france": "western_europe",
    "germany": "western_europe", "luxembourg": "western_europe",
    "netherlands": "western_europe", "switzerland": "western_europe",
    # Oceania
    "australia": "australia_nz", "new zealand": "australia_nz",
    "fiji": "melanesia", "new caledonia": "melanesia", "papua new guinea": "melanesia",
    "solomon islands": "melanesia", "vanuatu": "melanesia",
    "guam": "micronesia", "kiribati": "micronesia", "marshall islands": "micronesia",
    "micronesia": "micronesia", "nauru": "micronesia", "palau": "micronesia",
    "cook islands": "polynesia", "french polynesia": "polynesia", "samoa": "polynesia",
    "tonga": "polynesia", "tuvalu": "polynesia", "niue": "polynesia",
}

# Non-English names for countries the roster actually covers, mapped to the
# same regions. Added after Central Africa read zero despite thirty French
# items being harvested from Kinshasa and Mongabay Afrique.
COUNTRY_REGION.update({
    # French
    "rdc": "middle_africa", "congo-kinshasa": "middle_africa",
    "république démocratique du congo": "middle_africa",
    "republique democratique du congo": "middle_africa",
    "congo brazzaville": "middle_africa", "cameroun": "middle_africa",
    "tchad": "middle_africa", "gabon": "middle_africa",
    "centrafrique": "middle_africa", "république centrafricaine": "middle_africa",
    "côte d ivoire": "western_africa", "sénégal": "western_africa",
    "mauritanie": "western_africa", "guinée": "western_africa",
    "burkina": "western_africa", "bénin": "western_africa",
    "algérie": "northern_africa", "maroc": "northern_africa",
    "tunisie": "northern_africa", "égypte": "northern_africa",
    "madagascar": "eastern_africa", "éthiopie": "eastern_africa",
    "tanzanie": "eastern_africa", "ouganda": "eastern_africa",
    "afrique du sud": "southern_africa", "france": "western_europe",
    "belgique": "western_europe", "suisse": "western_europe",
    "allemagne": "western_europe", "espagne": "southern_europe",
    "brésil": "south_america", "brésilien": "south_america",
    "pérou": "south_america", "colombie": "south_america",
    "guyane": "south_america", "amazonie": "south_america",
    # Spanish / Portuguese
    "brasil": "south_america", "perú": "south_america", "peru": "south_america",
    "méxico": "central_america", "colômbia": "south_america",
    "amazônia": "south_america", "amazonía": "south_america",
    "bolívia": "south_america", "equador": "south_america",
    "españa": "southern_europe", "portugal": "southern_europe",
    # Russian
    "казахстан": "central_asia", "кыргызстан": "central_asia",
    "киргизия": "central_asia", "узбекистан": "central_asia",
    "таджикистан": "central_asia", "туркменистан": "central_asia",
    "россия": "eastern_europe", "украина": "eastern_europe",
    "аральское море": "central_asia", "казахстана": "central_asia",
})

FEATURE_REGION_EXTRA = {
    "cuvette centrale": "middle_africa", "bassin du congo": "middle_africa",
    "fleuve congo": "middle_africa", "kivu": "middle_africa",
    "katanga": "middle_africa", "kinshasa": "middle_africa",
    "lubumbashi": "middle_africa", "goma": "middle_africa",
    "sahel": "western_africa", "lac tchad": "western_africa",
    "delta du niger": "western_africa", "dakar": "western_africa",
    "cerrado": "south_america", "pantanal": "south_america",
    "чуй": "central_asia", "иссык-куль": "central_asia",
    "балхаш": "central_asia", "тянь-шань": "central_asia",
}

# Named features and subnational places. Longest match wins, so entries here
# beat country names when both appear.
FEATURE_REGION = {
    # Forest basins and biomes
    "amazon": "south_america", "amazonia": "south_america", "amazônia": "south_america",
    "cerrado": "south_america", "gran chaco": "south_america", "pantanal": "south_america",
    "mato grosso": "south_america", "pará": "south_america", "rondônia": "south_america",
    "acre state": "south_america", "yasuní": "south_america", "yasuni": "south_america",
    "orinoco": "south_america", "atlantic forest": "south_america",
    "congo basin": "middle_africa", "cuvette centrale": "middle_africa",
    "salonga": "middle_africa", "sangha trinational": "middle_africa",
    "virunga": "middle_africa", "ituri": "middle_africa", "okapi": "middle_africa",
    "kahuzi-biega": "middle_africa", "lomami": "middle_africa",
    "borneo": "south_eastern_asia", "sumatra": "south_eastern_asia",
    "kalimantan": "south_eastern_asia", "sulawesi": "south_eastern_asia",
    "leuser": "south_eastern_asia", "mekong": "south_eastern_asia",
    "tonle sap": "south_eastern_asia", "irrawaddy": "south_eastern_asia",
    "ayeyarwady": "south_eastern_asia", "tanintharyi": "south_eastern_asia",
    "salween": "south_eastern_asia", "hukawng": "south_eastern_asia",
    "new guinea": "melanesia", "west papua": "melanesia", "papua province": "melanesia",
    "sepik": "melanesia", "lorentz": "melanesia", "merauke": "melanesia",
    # Water
    "aral sea": "central_asia", "amu darya": "central_asia", "syr darya": "central_asia",
    "tien shan": "central_asia", "tian shan": "central_asia", "balkhash": "central_asia",
    "caspian": "central_asia", "lake chad": "western_africa", "niger delta": "western_africa",
    "volta basin": "western_africa", "lake victoria": "eastern_africa",
    "lake turkana": "eastern_africa", "okavango": "southern_africa",
    "zambezi": "eastern_africa", "nile": "northern_africa", "white nile": "eastern_africa",
    "ganges": "southern_asia", "brahmaputra": "southern_asia", "indus": "southern_asia",
    "yangtze": "eastern_asia", "yellow river": "eastern_asia",
    "colorado river": "northern_america", "ogallala": "northern_america",
    "high plains aquifer": "northern_america", "great lakes": "northern_america",
    "murray-darling": "australia_nz", "great barrier reef": "australia_nz",
    # Polar
    "arctic": "polar", "antarctic": "polar", "antarctica": "polar",
    "greenland": "polar", "svalbard": "polar", "barents sea": "polar",
    "beaufort sea": "polar", "thwaites": "polar", "permafrost": "polar",
    "siberia": "polar", "yamal": "polar", "chukchi": "polar",
    # Ocean
    "high seas": "ocean", "clarion-clipperton": "ocean", "sargasso": "ocean",
    "pacific garbage": "ocean", "mid-atlantic ridge": "ocean",
    "abyssal plain": "ocean", "seamount": "ocean", "deep sea": "ocean",
}

SUBNATIONAL = {
    # US states and territories most likely to appear in these sources
    "alabama": "northern_america", "alaska": "northern_america",
    "arizona": "northern_america", "arkansas": "northern_america",
    "california": "northern_america", "colorado": "northern_america",
    "connecticut": "northern_america", "delaware": "northern_america",
    "florida": "northern_america", "georgia state": "northern_america",
    "hawaii": "northern_america", "idaho": "northern_america",
    "illinois": "northern_america", "indiana": "northern_america",
    "iowa": "northern_america", "kansas": "northern_america",
    "kentucky": "northern_america", "louisiana": "northern_america",
    "maine": "northern_america", "maryland": "northern_america",
    "massachusetts": "northern_america", "michigan": "northern_america",
    "minnesota": "northern_america", "mississippi": "northern_america",
    "missouri": "northern_america", "montana": "northern_america",
    "nebraska": "northern_america", "nevada": "northern_america",
    "new hampshire": "northern_america", "new jersey": "northern_america",
    "new mexico": "northern_america", "new york": "northern_america",
    "north carolina": "northern_america", "north dakota": "northern_america",
    "ohio": "northern_america", "oklahoma": "northern_america",
    "oregon": "northern_america", "pennsylvania": "northern_america",
    "rhode island": "northern_america", "south carolina": "northern_america",
    "south dakota": "northern_america", "tennessee": "northern_america",
    "texas": "northern_america", "utah": "northern_america",
    "vermont": "northern_america", "virginia": "northern_america",
    "washington state": "northern_america", "west virginia": "northern_america",
    "wisconsin": "northern_america", "wyoming": "northern_america",
    "alberta": "northern_america", "british columbia": "northern_america",
    "ontario": "northern_america", "quebec": "northern_america",
    "québec": "northern_america", "saskatchewan": "northern_america",
    "manitoba": "northern_america", "nunavut": "polar",
    "northwest territories": "polar", "yukon": "polar",
    # Elsewhere
    "maharashtra": "southern_asia", "kerala": "southern_asia",
    "tamil nadu": "southern_asia", "odisha": "southern_asia",
    "jharkhand": "southern_asia", "chhattisgarh": "southern_asia",
    "assam": "southern_asia", "gujarat": "southern_asia",
    "karnataka": "southern_asia", "punjab": "southern_asia",
    "west bengal": "southern_asia", "uttar pradesh": "southern_asia",
    "kachin": "south_eastern_asia", "shan state": "south_eastern_asia",
    "rakhine": "south_eastern_asia", "sarawak": "south_eastern_asia",
    "sabah": "south_eastern_asia", "papua barat": "melanesia",
    "bahia": "south_america", "minas gerais": "south_america",
    "goiás": "south_america", "maranhão": "south_america",
    "tocantins": "south_america", "amapá": "south_america",
    "roraima": "south_america", "amazonas": "south_america",
    "loreto": "south_america", "madre de dios": "south_america",
    "antioquia": "south_america", "putumayo": "south_america",
    "kwazulu-natal": "southern_africa", "mpumalanga": "southern_africa",
    "limpopo": "southern_africa", "gauteng": "southern_africa",
    "north kivu": "middle_africa", "south kivu": "middle_africa",
    "nord-kivu": "middle_africa", "sud-kivu": "middle_africa",
    "equateur": "middle_africa", "tshopo": "middle_africa",
}
COUNTRY_REGION.update(SUBNATIONAL)


# Country names in the languages the roster now harvests. The tagger reads the
# English fact first and falls back to the original-language source text, so a
# missing name here means a whole country lands in "No place identified".
COUNTRY_REGION.update({
    # German
    "deutschland": "western_europe", "österreich": "western_europe",
    "schweiz": "western_europe", "frankreich": "western_europe",
    "italien": "southern_europe", "spanien": "southern_europe",
    "griechenland": "southern_europe", "polen": "eastern_europe",
    "russland": "eastern_europe", "türkei": "western_asia",
    "brasilien": "south_america", "indien": "southern_asia",
    "china": "eastern_asia", "japan": "eastern_asia",
    "indonesien": "south_eastern_asia", "kongo": "middle_africa",
    "nigeria": "western_africa", "südafrika": "southern_africa",
    "ägypten": "northern_africa", "kenia": "eastern_africa",
    # Italian / Dutch / Polish
    "italia": "southern_europe", "grecia": "southern_europe",
    "germania": "western_europe", "francia": "western_europe",
    "duitsland": "western_europe", "nederland": "western_europe",
    "belgië": "western_europe", "niemcy": "western_europe",
    "polska": "eastern_europe", "rosja": "eastern_europe",
    "ukraina": "eastern_europe", "україна": "eastern_europe",
    "білорусь": "eastern_europe",
    # Turkish
    "türkiye": "western_asia", "suriye": "western_asia",
    "irak": "western_asia", "iran": "southern_asia",
    "yunanistan": "southern_europe", "rusya": "eastern_europe",
    "karadeniz": "western_asia", "akdeniz": "southern_europe",
    # Arabic
    "مصر": "northern_africa", "المغرب": "northern_africa",
    "الجزائر": "northern_africa", "تونس": "northern_africa",
    "ليبيا": "northern_africa", "السودان": "northern_africa",
    "العراق": "western_asia", "سوريا": "western_asia",
    "لبنان": "western_asia", "الأردن": "western_asia",
    "اليمن": "western_asia", "السعودية": "western_asia",
    "فلسطين": "western_asia", "الصومال": "eastern_africa",
    "النيل": "northern_africa", "الخليج": "western_asia",
    # Persian
    "ایران": "southern_asia", "افغانستان": "southern_asia",
    "دریاچه ارومیه": "southern_asia", "خوزستان": "southern_asia",
    # Hindi / Bengali / Urdu
    "भारत": "southern_asia", "गंगा": "southern_asia",
    "हिमालय": "southern_asia", "বাংলাদেশ": "southern_asia",
    "সুন্দরবন": "southern_asia", "پاکستان": "southern_asia",
    # Indonesian / Malay
    "indonesia": "south_eastern_asia", "kalimantan": "south_eastern_asia",
    "sumatera": "south_eastern_asia", "sulawesi": "south_eastern_asia",
    "malaysia": "south_eastern_asia", "hutan": "south_eastern_asia",
    "papua": "melanesia",
    # Vietnamese / Thai / Khmer
    "việt nam": "south_eastern_asia", "mê kông": "south_eastern_asia",
    "ประเทศไทย": "south_eastern_asia", "แม่น้ำโขง": "south_eastern_asia",
    "កម្ពុជា": "south_eastern_asia",
    # Chinese / Japanese / Korean
    "中国": "eastern_asia", "长江": "eastern_asia", "黄河": "eastern_asia",
    "内蒙古": "eastern_asia", "西藏": "eastern_asia", "云南": "eastern_asia",
    "日本": "eastern_asia", "福島": "eastern_asia",
    "한국": "eastern_asia", "대한민국": "eastern_asia",
    "몽골": "eastern_asia", "モンゴル": "eastern_asia",
    # Swahili / Amharic / Hausa
    "tanzania": "eastern_africa", "kenya": "eastern_africa",
    "uganda": "eastern_africa", "msitu": "eastern_africa",
    "ኢትዮጵያ": "eastern_africa", "አባይ": "eastern_africa",
    "najeriya": "western_africa",
    # More Spanish / Portuguese
    "argentina": "south_america", "chile": "south_america",
    "paraguai": "south_america", "uruguai": "south_america",
    "venezuela": "south_america", "guatemala": "central_america",
    "honduras": "central_america", "nicarágua": "central_america",
    "costa rica": "central_america", "panamá": "central_america",
    "el salvador": "central_america", "república dominicana": "caribbean",
    "moçambique": "eastern_africa", "angola": "middle_africa",
    "cabo verde": "western_africa", "guiné-bissau": "western_africa",
    "timor-leste": "south_eastern_asia",
})


# Completing the country list, then the territories and dependencies that show
# up in environmental reporting far more often than their size suggests —
# small islands carry outsized coverage of sea level, reefs and mining.
COUNTRY_REGION.update({
    "sao tome and principe": "middle_africa", "são tomé and príncipe": "middle_africa",
    "antigua and barbuda": "caribbean", "saint kitts": "caribbean",
    "nevis": "caribbean", "saint vincent": "caribbean", "grenadines": "caribbean",
    "trinidad and tobago": "caribbean", "tobago": "caribbean",
    "andorra": "southern_europe", "bosnia and herzegovina": "southern_europe",
    "herzegovina": "southern_europe", "san marino": "southern_europe",
    "liechtenstein": "western_europe", "monaco": "western_europe",
    "american samoa": "polynesia", "tokelau": "polynesia",
    "wallis and futuna": "polynesia", "futuna": "polynesia",
    # Territories and dependencies
    "greenland": "polar", "kalaallit nunaat": "polar", "faroe": "northern_europe",
    "åland": "northern_europe", "aland": "northern_europe",
    "isle of man": "northern_europe", "jersey": "northern_europe",
    "guernsey": "northern_europe", "gibraltar": "southern_europe",
    "azores": "southern_europe", "açores": "southern_europe",
    "madeira": "southern_europe", "canary islands": "northern_africa",
    "islas canarias": "northern_africa", "ceuta": "northern_africa",
    "melilla": "northern_africa", "réunion": "eastern_africa",
    "reunion": "eastern_africa", "mayotte": "eastern_africa",
    "saint helena": "middle_africa", "ascension island": "middle_africa",
    "hong kong": "eastern_asia", "macau": "eastern_asia", "macao": "eastern_asia",
    "bermuda": "northern_america", "greenlandic": "polar",
    "cayman islands": "caribbean", "turks and caicos": "caribbean",
    "british virgin islands": "caribbean", "us virgin islands": "caribbean",
    "anguilla": "caribbean", "montserrat": "caribbean",
    "sint maarten": "caribbean", "saint barthélemy": "caribbean",
    "saint martin": "caribbean", "bonaire": "caribbean",
    "saint pierre": "northern_america", "miquelon": "northern_america",
    "falkland islands": "south_america", "islas malvinas": "south_america",
    "south georgia": "polar", "galápagos": "south_america",
    "galapagos": "south_america", "easter island": "polynesia",
    "rapa nui": "polynesia", "pitcairn": "polynesia",
    "norfolk island": "australia_nz", "christmas island": "australia_nz",
    "cocos islands": "australia_nz", "lord howe": "australia_nz",
    "chatham islands": "australia_nz", "northern mariana": "micronesia",
    "wake island": "micronesia", "johnston atoll": "micronesia",
    "chagos": "eastern_africa", "diego garcia": "eastern_africa",
    "kerguelen": "polar", "south orkney": "polar", "ross sea": "polar",
    "weddell sea": "polar", "antarctic peninsula": "polar",
})

# The long tail of languages. A name missing here sends an entire country to
# "No place identified" whenever the extracted fact does not repeat the place.
COUNTRY_REGION.update({
    # Nordic
    "sverige": "northern_europe", "norge": "northern_europe",
    "danmark": "northern_europe", "suomi": "northern_europe",
    "ísland": "northern_europe", "grønland": "polar",
    # Greek, Romanian, Hungarian, Czech, Bulgarian, Albanian
    "ελλάδα": "southern_europe", "κύπρος": "western_asia",
    "românia": "eastern_europe", "dunărea": "eastern_europe",
    "magyarország": "eastern_europe", "česko": "eastern_europe",
    "българия": "eastern_europe", "shqipëri": "southern_europe",
    "srbija": "southern_europe", "србија": "southern_europe",
    "hrvatska": "southern_europe", "slovenija": "southern_europe",
    "makedonija": "southern_europe", "crna gora": "southern_europe",
    # Caucasus & Central Asia
    "საქართველო": "western_asia", "հայաստան": "western_asia",
    "azərbaycan": "western_asia", "qazaqstan": "central_asia",
    "қазақстан": "central_asia", "oʻzbekiston": "central_asia",
    "ўзбекистон": "central_asia", "тоҷикистон": "central_asia",
    "кыргызстан": "central_asia", "монгол": "eastern_asia",
    # South Asian languages
    "இந்தியா": "southern_asia", "தமிழ்நாடு": "southern_asia",
    "భారత": "southern_asia", "महाराष्ट्र": "southern_asia",
    "ਪੰਜਾਬ": "southern_asia", "नेपाल": "southern_asia",
    "ශ්‍රී ලංකා": "southern_asia", "இலங்கை": "southern_asia",
    "ভারত": "southern_asia", "পদ্মা": "southern_asia",
    "بھارت": "southern_asia", "سندھ": "southern_asia",
    "افغان": "southern_asia", "پښتونخوا": "southern_asia",
    # Southeast Asian
    "မြန်မာ": "south_eastern_asia", "ဧရာဝတီ": "south_eastern_asia",
    "ລາວ": "south_eastern_asia", "ຂອງ": "south_eastern_asia",
    "pilipinas": "south_eastern_asia", "luzon": "south_eastern_asia",
    "mindanao": "south_eastern_asia", "visayas": "south_eastern_asia",
    "kampuchea": "south_eastern_asia", "sabah": "south_eastern_asia",
    "borneo": "south_eastern_asia", "jawa": "south_eastern_asia",
    "sungai": "south_eastern_asia",
    # African languages
    "soomaaliya": "eastern_africa", "ኤርትራ": "eastern_africa",
    "ትግራይ": "eastern_africa", "afrika": "eastern_africa",
    "nàìjíríà": "western_africa", "yorùbá": "western_africa",
    "ìgbò": "western_africa", "sénégal": "western_africa",
    "madagasikara": "eastern_africa", "suid-afrika": "southern_africa",
    "ingane": "southern_africa", "congo-brazzaville": "middle_africa",
    "lubumbashi": "middle_africa",
    # Hebrew
    "ישראל": "western_asia", "הכנרת": "western_asia",
    # Kurdish
    "kurdistan": "western_asia", "کوردستان": "western_asia",
})

FEATURE_REGION.update(FEATURE_REGION_EXTRA)

# Normalised copies used for matching. The originals stay readable.
_FEATURES = {}
_COUNTRIES = {}

GLOBAL_MARKERS = [
    "global", "worldwide", "planetary", "across the world", "every continent",
    "international", "transboundary", "world's", "earth's",
]

# ── Meta-topics ────────────────────────────────────────────────────────
# These are the join keys into the context library. Keep them coarse:
# a fact should usually match one or two, never six.

TOPIC_PATTERNS = {
    "forest": [
        r"\bdeforest", r"\bforest\b", r"\blogging\b", r"\btimber\b", r"\bclear-?cut",
        r"\bcanopy\b", r"\bmangrove", r"\bwoodland\b", r"\bafforest", r"\bREDD\b",
    ],
    "freshwater": [
        r"\baquifer\b", r"\bgroundwater\b", r"\bwatershed\b", r"\briver\b", r"\bdam\b",
        r"\blake\b", r"\bbasin\b", r"\bwater table\b", r"\bopen water\b", r"\breservoir\b",
        r"\bwater extent\b", r"\bfreshwater\b", r"\bspring\b", r"\bborehole\b",
        r"\bwetland\b", r"\birrigat", r"\bdrought\b", r"\bwater table\b", r"\bglacier\b",
        r"\bsnowpack\b", r"\bstreamflow\b",
    ],
    "soil": [
        r"\bsoil\b", r"\berosion\b", r"\bdesertific", r"\btopsoil\b", r"\bsalinis",
        r"\bsaliniz", r"\bcompaction\b", r"\bland degradation\b",
    ],
    "ocean": [
        r"\bocean\b", r"\bmarine\b", r"\bcoral\b", r"\breef\b", r"\bfisher",
        r"\bcoastal\b", r"\bsea level\b", r"\bacidific", r"\bplankton\b",
        r"\btrawl", r"\bbycatch\b", r"\bseagrass\b", r"\bsea ice\b",
    ],
    "carbon": [
        r"\bcarbon\b", r"\bCO2\b", r"\bemissions?\b", r"\bmethane\b", r"\bgreenhouse\b",
        r"\bfossil fuel\b", r"\bwarming\b", r"\bclimate\b", r"\bsink\b", r"\bpermafrost\b",
    ],
    "extinction": [
        r"\bextinct", r"\bbiodiversity\b", r"\bspecies\b", r"\bpopulation decline\b",
        r"\bendangered\b", r"\bhabitat\b", r"\bRed List\b", r"\bpollinator",
        r"\bcorridor", r"\breserve\b", r"\bprotected area", r"\bnational park\b",
        r"\belephant\b", r"\btiger\b", r"\bprimate\b", r"\bmigrat",
        r"\binsect\b", r"\bwildlife\b", r"\bpoach",
    ],
    "toxics": [
        r"\bPFAS\b", r"\bpesticide", r"\bherbicide", r"\bheavy metal", r"\bmercury\b",
        r"\bcadmium\b", r"\blead\b", r"\barsenic\b", r"\bdioxin", r"\bmicroplastic",
        r"\bcontaminat", r"\btoxic", r"\bpollut", r"\bglyphosate\b", r"\bneonicotinoid",
        r"\bforever chemical",
    ],
    "extraction": [
        r"\bmining\b", r"\bmine\b", r"\bdrilling\b", r"\boil\b", r"\bgas\b", r"\bcoal\b",
        r"\bquarr", r"\btailings\b", r"\bconcession", r"\bpipeline\b", r"\bfracking\b",
        r"\bcobalt\b", r"\blithium\b", r"\bnickel\b", r"\bbauxite\b",
    ],
    "land_tenure": [
        r"\bindigenous\b", r"\bland rights\b", r"\bcustomary\b", r"\bdisplace",
        r"\bland grab\b", r"\bevict", r"\bland defender", r"\bconsultation\b",
        r"\bFPIC\b", r"\bfree, prior",
    ],
    "food": [
        r"\bcrop\b", r"\byield\b", r"\bagricultur", r"\bfarm", r"\bharvest\b",
        r"\blivestock\b", r"\bfood securit", r"\bmaize\b", r"\bwheat\b", r"\brice\b",
        r"\bsoy\b", r"\bpalm oil\b", r"\bcattle\b",
    ],
    "law": [
        r"\bcourt\b", r"\bruling\b", r"\blawsuit\b", r"\btribunal\b", r"\bregulat",
        r"\bpermit\b", r"\blicence\b", r"\blicense\b", r"\btreaty\b", r"\bconvention\b",
        r"\bmoratorium\b", r"\bban\b", r"\benforcement\b", r"\bfined?\b", r"\bliab",
    ],
    "air": [
        r"\bair qualit", r"\bPM2\.5\b", r"\bparticulate", r"\bsmog\b", r"\bNO2\b",
        r"\bozone\b", r"\bhaze\b", r"\bwildfire smoke\b",
    ],
    "fire": [
        r"\bwildfire\b", r"\bfire season\b", r"\bburn(ed|ing|t)?\b", r"\bblaze\b",
        r"\bslash-and-burn\b",
    ],
}


CYRILLIC = re.compile(r"[\u0400-\u04FF]")
# Scripts that inflect with affixes, and scripts with no spaces between words.
INFLECTING = re.compile(
    r"[\u0400-\u04FF"      # Cyrillic
    r"\u0590-\u05FF"       # Hebrew
    r"\u0600-\u06FF"       # Arabic, Persian, Urdu, Pashto, Kurdish
    r"\u0900-\u097F"       # Devanagari — Hindi, Marathi, Nepali
    r"\u0980-\u09FF"       # Bengali
    r"\u0A00-\u0A7F"       # Gurmukhi — Punjabi
    r"\u0B80-\u0BFF"       # Tamil
    r"\u0C00-\u0C7F"       # Telugu
    r"\u0D80-\u0DFF"       # Sinhala
    r"\u0E00-\u0E7F"       # Thai
    r"\u0E80-\u0EFF"       # Lao
    r"\u1000-\u109F"       # Burmese
    r"\u1200-\u137F"       # Ge'ez — Amharic, Tigrinya
    r"\u10A0-\u10FF"       # Georgian
    r"\u0530-\u058F"       # Armenian
    r"\u1780-\u17FF]")     # Khmer
NO_BOUNDARY = re.compile(r"[\u4E00-\u9FFF\u3040-\u30FF\uAC00-\uD7AF]")


# Subject vocabulary in the harvest languages. Enough to answer "is this item
# about the environment at all", which is all the harvest gate needs; the
# fine-grained English patterns still do the topic tagging after translation.
MULTILINGUAL_TOPIC = re.compile(
    "|".join([
    # French
    r"pollution|déforestation|deforestation|forêt|environnement|climat|sécheresse",
    r"inondation|barrage|mine\b|minier|pétrole|charbon|déchets|pesticide|espèce",
    r"biodiversité|écolog|eau potable|nappe|glacier|incendie|émission",
    # Spanish / Portuguese
    r"contaminación|contaminação|deforestación|desmatamento|desflorestação",
    r"medio ambiente|meio ambiente|clima|sequía|seca\b|inundac|represa|barragem",
    r"miner|miner[ií]a|mina\b|minas\b|minério|mineração|garimp|petróleo|petrolero",
    r"carbón|carvão|residuos|resíduos|plaguicida|agrotóxico|tierras raras",
    r"terras raras|acuífer|manglar|mangue|selva|bosque|floresta|desmonte",
    r"biodiversidad|biodiversidade|ecolog|acuífero|aquífero|glaciar|geleira",
    r"incendio|incêndio|emision|emissõ|vertido|derrame|garimpo",
    # German / Dutch / Nordic
    r"umwelt|verschmutzung|abholzung|klima|dürre|hochwasser|bergbau|kohle",
    r"artenvielfalt|emission|milieu|vervuiling|ontbossing|droogte|mijnbouw",
    r"förorening|miljö|avskogning|forurensning|forurening|saastuminen|ympäristö",
    # Slavic
    r"загрязн|экологи|вырубк|засух|наводнен|добыч|уголь|выброс|отход",
    r"забруднен|екологі|довкілл|вирубк|zanieczyszcz|środowisk|wylesian",
    r"poluare|mediu|defrișă|замърсяван|околна среда|znečišt|szennyez|környezet",
    # Turkish / Greek / Italian
    r"kirlilik|çevre|ormansızlaş|maden|kuraklık|ρύπανση|περιβάλλον|αποψίλωση",
    r"inquinamento|ambiente|deforestazione|siccità|rifiuti",
    # Indonesian / Malay / Vietnamese / Thai / Khmer / Burmese
    r"pencemaran|lingkungan|deforestasi|hutan|tambang|kekeringan|banjir|limbah",
    r"alam sekitar|penyahutanan|perlombongan|kemarau|sisa",
    r"ô nhiễm|môi trường|phá rừng|khai khoáng|hạn hán|lũ lụt|chất thải",
    r"มลพิษ|สิ่งแวดล้อม|ป่า|เหมือง|ภัยแล้ง|น้ำท่วม|ขยะ",
    r"បរិស្ថាន|ព្រៃឈើ|ពុល|ပတ်ဝန်းကျင်|သစ်တော|ညစ်ညမ်း",
    # Arabic / Persian / Hebrew / Urdu
    r"تلوث|بيئة|إزالة الغابات|جفاف|فيضان|تعدين|نفايات|مبيدات|انبعاث",
    r"محیط زیست|آلودگی|خشکسالی|جنگل|معدن|زباله",
    r"זיהום|סביבה|בצורת|יער|כרייה|פסולת|آلودگی|ماحول|جنگلات",
    # Hindi / Bengali / Chinese / Japanese / Korean / Swahili / Amharic
    r"प्रदूषण|पर्यावरण|वन|खनन|सूखा|बाढ़|कचरा|अपशिष्ट",
    r"দূষণ|পরিবেশ|বন|খনি|খরা|বন্যা|বর্জ্য",
    r"污染|环境|毁林|森林|采矿|干旱|洪水|废物|排放|生态|環境|汚染|森林|鉱山",
    r"환경|오염|산림|광산|가뭄|홍수|폐기물|생태",
    r"uchafuzi|mazingira|misitu|madini|ukame|mafuriko|taka",
    r"ብክለት|አካባቢ|ደን|ማዕድን|ድርቅ|ጎርፍ",
    ]), re.I)


def is_environmental(text):
    """
    Coarse yes/no for the harvest gate, in any harvested language.

    detect_topics is English-only, so gating relied on it and non-English
    general-news sources were passed through ungated entirely.
    """
    return bool(detect_topics(text) or MULTILINGUAL_TOPIC.search(text or ""))


def _norm(text):
    text = unicodedata.normalize("NFKD", text or "")
    # Drop combining marks so "Amazônia" and "Amazonia" are the same string.
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.lower()


def _key(name):
    """Normalise a gazetteer key the same way text is normalised."""
    return _norm(name)


def _build(mapping):
    return {_key(k): v for k, v in mapping.items()}


def detect_region(text):
    """
    Return (region_key, matched_place or None).

    Features beat countries; longest match wins within each set. Falls back to
    'global' only when a global marker is present, otherwise returns
    (None, None) so the caller can decide what to do with an untagged fact.
    """
    low = _norm(text)

    if not _FEATURES:
        _FEATURES.update({_key(k): v for k, v in FEATURE_REGION.items()})
        _COUNTRIES.update({_key(k): v for k, v in COUNTRY_REGION.items()})

    def hit(name):
        # Suffix tolerance is language-specific. Slavic languages decline place
        # names, so "Казахстане" has to reach "Казахстан" — but allowing a free
        # suffix on Latin keys lets "Indus" match "industrial", which is how a
        # European soil study got tagged South Asia once already.
        if NO_BOUNDARY.search(name):
            # Chinese, Japanese and Korean write without spaces, so \w
            # boundaries never match. Plain containment is correct there.
            return name in low
        if INFLECTING.search(name):
            tail = r"\w*"          # full affixation
            head = ""
        else:
            tail = r"(?:s|es)?\b"  # plurals only
            head = r"(?<!\w)"
        return re.search(head + re.escape(name) + tail, low)

    best = None
    for place, region in _FEATURES.items():
        if hit(place) and (best is None or len(place) > len(best[0])):
            best = (place, region)
    if best:
        return best[1], best[0]

    for country, region in _COUNTRIES.items():
        if hit(country) and (best is None or len(country) > len(best[0])):
            best = (country, region)
    if best:
        return best[1], best[0]

    for marker in GLOBAL_MARKERS:
        if marker in low:
            return "global", None

    return None, None


def detect_topics(text, limit=3):
    """Return meta-topic tags ordered by match count, strongest first."""
    low = _norm(text)
    scored = []
    for topic, patterns in TOPIC_PATTERNS.items():
        hits = sum(1 for p in patterns if re.search(p, low, re.I))
        if hits:
            scored.append((hits, topic))
    scored.sort(reverse=True)
    return [t for _, t in scored[:limit]]


def tag(fact_text, source_text=""):
    """
    Tag a fact. Region is read from the fact first — if the fact names no place,
    fall back to the source text, because a paper about the Congo Basin whose
    key sentence says only 'the basin' is still a Congo Basin fact.
    """
    region, place = detect_region(fact_text)
    if region is None and source_text:
        region, place = detect_region(source_text)
    topics = detect_topics(fact_text) or detect_topics(source_text)
    region = region or "unlocated"
    return {
        "region": region,
        "region_label": REGION_LABELS.get(region),
        "place": place,
        "topics": topics,
    }
