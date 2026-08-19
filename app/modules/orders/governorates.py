"""Egyptian governorates, and how to recognise the one a customer names.

Shopify will not accept a free-text governorate: an address needs the province *code* it
knows, or the order arrives with "Government is missing" and staff have to fill it in by
hand before anything can ship.

The codes below are Shopify's own, read from its ``countries.json`` for this store on
2026-08-18. The Arabic and English spellings are added here because customers write
"المنوفية", "Monufia" or "Menoufia" and mean the same place - Shopify only publishes one
label per province, so matching has to be ours.

If a name cannot be matched the order still goes through without a province rather than
failing; a missing field is a small annoyance for staff, a rejected order is a lost sale.
"""

import re
import unicodedata
from typing import Dict, List, Optional, Tuple

# code -> every spelling that should resolve to it. The first English entry is the label
# Shopify itself uses.
GOVERNORATES: Dict[str, Tuple[str, ...]] = {
    "C": ("Cairo", "القاهرة", "قاهرة", "masr", "مصر"),
    "GZ": ("Giza", "الجيزة", "جيزة", "El Giza"),
    "ALX": ("Alexandria", "الإسكندرية", "اسكندرية", "الاسكندرية", "Alex", "Iskandariyah"),
    "SU": ("6th of October", "السادس من أكتوبر", "٦ أكتوبر", "6 أكتوبر", "Sixth of October",
           "October City"),
    "HU": ("Helwan", "حلوان"),
    "KB": ("Qalyubia", "القليوبية", "قليوبية", "Qalyubiya", "Kalyoubia", "Banha", "بنها"),
    "SHR": ("Al Sharqia", "الشرقية", "شرقية", "Sharqia", "Sharkia", "Zagazig", "الزقازيق"),
    "MNF": ("Monufia", "المنوفية", "منوفية", "Menoufia", "Menofia", "Shibin El Kom",
            "شبين الكوم"),
    "GH": ("Gharbia", "الغربية", "غربية", "Tanta", "طنطا"),
    "BH": ("Beheira", "البحيرة", "بحيرة", "Behera", "Damanhur", "دمنهور"),
    "KFS": ("Kafr el-Sheikh", "كفر الشيخ", "Kafr El Sheikh", "Kafr Elsheikh"),
    "DK": ("Dakahlia", "الدقهلية", "دقهلية", "Daqahlia", "Mansoura", "المنصورة"),
    "DT": ("Damietta", "دمياط", "Dumyat"),
    "PTS": ("Port Said", "بورسعيد", "بور سعيد", "Portsaid"),
    "IS": ("Ismailia", "الإسماعيلية", "الاسماعيلية", "اسماعيلية", "Ismailiya"),
    "SUZ": ("Suez", "السويس", "سويس"),
    "BNS": ("Beni Suef", "بني سويف", "بنى سويف", "Bani Suwayf"),
    "FYM": ("Faiyum", "الفيوم", "فيوم", "Fayoum", "Fayyum"),
    "MN": ("Minya", "المنيا", "منيا", "Menia", "El Minya"),
    "AST": ("Asyut", "أسيوط", "اسيوط", "Assiut", "Assuit"),
    "SHG": ("Sohag", "سوهاج", "Suhag"),
    "KN": ("Qena", "قنا", "Qina"),
    "LX": ("Luxor", "الأقصر", "الاقصر", "Uqsur"),
    "ASN": ("Aswan", "أسوان", "اسوان"),
    "BA": ("Red Sea", "البحر الأحمر", "البحر الاحمر", "Hurghada", "الغردقة"),
    "MT": ("Matrouh", "مطروح", "مرسى مطروح", "Marsa Matrouh", "Matruh"),
    "WAD": ("New Valley", "الوادي الجديد", "Wadi Gadid", "Kharga"),
    "SIN": ("North Sinai", "شمال سيناء", "Shamal Sina", "Arish", "العريش"),
    "JS": ("South Sinai", "جنوب سيناء", "Janub Sina", "Sharm El Sheikh", "شرم الشيخ"),
}

_DIACRITICS = re.compile(r"[ً-ٰٟ]")
# Districts, because almost nobody writes their governorate: an address arrives as
# "القاهرة - المعادي - شارع ٩٠", or just "المعادي". Only well-known, unambiguous ones are
# listed - a district that could be in two governorates is better left unresolved than
# guessed. Helwan and 6th of October are deliberately absent: this store treats them as
# governorates in their own right, which is how Shopify lists them.
DISTRICTS: Dict[str, Tuple[str, ...]] = {
    "C": ("المعادي", "Maadi", "مدينة نصر", "Nasr City", "مصر الجديدة", "Heliopolis",
          "الزمالك", "Zamalek", "المقطم", "Mokattam", "وسط البلد", "Downtown",
          "التجمع الخامس", "New Cairo", "القاهرة الجديدة", "مدينتي", "Madinaty",
          "الرحاب", "Rehab", "شبرا", "Shubra", "عين شمس", "Ain Shams",
          "العباسية", "Abbassia", "المرج", "السيدة زينب", "الدرب الأحمر"),
    "GZ": ("الدقي", "Dokki", "المهندسين", "Mohandessin", "العجوزة", "Agouza",
           "الهرم", "Haram", "فيصل", "Faisal", "إمبابة", "Imbaba", "بولاق الدكرور",
           "الشيخ زايد", "Sheikh Zayed", "حدائق الأهرام", "البدرشين"),
    "ALX": ("سموحة", "Smouha", "ستانلي", "Stanley", "المنتزه", "Montaza",
            "العجمي", "Agami", "سيدي بشر", "Sidi Bishr", "محرم بك", "لوران", "Louran",
            "سيدي جابر", "Sidi Gaber", "برج العرب", "Borg El Arab"),
}

# Words that carry no identifying information: "محافظة المنوفية" is just "المنوفية".
_NOISE = ("governorate", "government", "محافظة", "محافظه", "el", "al", "the")


def _normalise(text: str) -> str:
    """Fold spelling differences so "المنوفية" and "Menoufia " compare usefully."""
    text = unicodedata.normalize("NFKC", (text or "").strip().casefold())
    text = _DIACRITICS.sub("", text)
    # Arabic letters customers spell interchangeably.
    for source, target in (("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ى", "ي"), ("ة", "ه"),
                           ("ؤ", "و"), ("ئ", "ي")):
        text = text.replace(source, target)
    text = re.sub(r"[^\w؀-ۿ]+", " ", text)
    words = [word for word in text.split() if word not in _NOISE]
    return " ".join(words)


# Built once: every known spelling, normalised, pointing at its code. Governorate names
# are registered first so a district can never shadow one.
_LOOKUP: Dict[str, str] = {}
for _code, _names in GOVERNORATES.items():
    _LOOKUP[_normalise(_code)] = _code
    for _name in _names:
        _LOOKUP.setdefault(_normalise(_name), _code)
for _code, _districts in DISTRICTS.items():
    for _district in _districts:
        _LOOKUP.setdefault(_normalise(_district), _code)


def resolve(name: Optional[str]) -> Optional[str]:
    """The Shopify province code for a governorate a customer named, or None.

    None is not a failure to shout about - the order proceeds without the province, and
    the caller logs it so a spelling worth adding shows up.
    """
    key = _normalise(name or "")
    if not key:
        return None
    if key in _LOOKUP:
        return _LOOKUP[key]

    # "شبين الكوم المنوفية", or "Cairo, Egypt" - find a known name inside what was given.
    for spelling, code in _LOOKUP.items():
        if len(spelling) >= 4 and (spelling in key or key in spelling):
            return code
    return None


def names() -> List[str]:
    """The English label for each governorate, for prompting and for error messages."""
    return [names_[0] for names_ in GOVERNORATES.values()]
