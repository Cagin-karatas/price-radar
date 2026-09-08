"""Ürün eşleştirme ve duplicate tespiti.

Problem: aynı kulaklık beş sitede beş farklı başlıkla listelenir.

    "Sony WH-1000XM5 Wireless Noise Cancelling Headphones - Black"
    "SONY WH1000XM5 Kablosuz Kulaklık Siyah"
    "Sony WH-1000XM5 (Black) — Over-Ear Bluetooth"

Bunları aynı ürün saymazsan siteler arası fiyat karşılaştırması yapamazsın.

İki aşamalı çözüm:
  1. **Model kodu** — varsa en güvenilir sinyal (WH-1000XM5). Kesin eşleşme.
  2. **Bulanık başlık** — model kodu yoksa normalleştirilmiş başlıklar
     token benzerliğiyle karşılaştırılır.

Bilinçli olarak makine öğrenmesi kullanmıyoruz: 500 üründe ML'in getirisi
yok, açıklanabilirliğin kaybı büyük. Müşteri "neden bu iki ürün birleşti"
diye sorduğunda cevap verebilmek gerekiyor.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

# Başlıkta anlam taşımayan kelimeler
STOPWORDS = {
    "the", "and", "for", "with", "new", "original", "genuine", "official",
    "ve", "ile", "yeni", "orijinal", "adet", "ürün", "urun",
    "free", "shipping", "kargo", "bedava", "ücretsiz", "ucretsiz",
    "black", "white", "siyah", "beyaz",  # renk çoğu zaman ayrı varyant değil
}

# WH-1000XM5, A2342, RTX4080, SM-G991B gibi model kodları:
# harf ve rakam içeren, en az 4 karakterlik belirteçler
MODEL_CODE_RE = re.compile(r"\b(?=[a-z0-9-]*[a-z])(?=[a-z0-9-]*\d)[a-z0-9]+(?:-[a-z0-9]+)*\b")

TR_MAP = str.maketrans({
    "ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g",
    "ü": "u", "Ü": "u", "ö": "o", "Ö": "o", "ç": "c", "Ç": "c",
})


def normalize_title(title: str) -> str:
    """Başlığı karşılaştırılabilir hale getirir."""
    text = (title or "").translate(TR_MAP).lower()
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9\s-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def tokenize(title: str) -> list[str]:
    """Anlamlı belirteçleri döndürür."""
    return [
        token
        for token in normalize_title(title).split()
        if token not in STOPWORDS and len(token) > 1
    ]


def extract_model_code(title: str) -> str | None:
    """Başlıktan model kodunu çıkarır.

    Harf+rakam karışımı, en az 4 karakter olan ilk belirteç. Tireler
    kaldırılır: "WH-1000XM5" ile "WH1000XM5" aynı koddur.
    """
    normalized = normalize_title(title)

    candidates = []
    for match in MODEL_CODE_RE.finditer(normalized):
        token = match.group(0).replace("-", "")
        if len(token) < 4:
            continue
        # Sadece rakam+tek harf olanları ele: "5g", "128gb" model kodu değil
        if re.fullmatch(r"\d+[a-z]{1,2}", token):
            continue
        candidates.append(token)

    if not candidates:
        return None

    # En uzun aday genelde asıl model kodudur
    return max(candidates, key=len)


def spec_tokens(title: str) -> set[str]:
    """Ürünü diğer varyantlarından ayıran sayısal belirteçler.

    "iPhone 13 128GB" -> {"13", "128gb"}

    Bunlar ürün kimliğinin can alıcı kısmı: "iPhone 13" ile "iPhone 14"
    başlık olarak %96 benzer ama farklı ürünler. Ayrımı yapan tek şey
    bu belirteçler.
    """
    found = set()
    for token in normalize_title(title).split():
        if token.isdigit():
            found.add(token)
        elif re.fullmatch(r"\d+[a-z]{1,3}", token):  # 128gb, 3s, 15w
            found.add(token)
    return found


def has_spec_conflict(a: str, b: str) -> bool:
    """İki başlıkta çelişen sayısal belirteç var mı?

    Her iki tarafta da diğerinde bulunmayan sayısal belirteç varsa,
    bunlar farklı varyantlardır. Tek tarafta fazladan belirteç olması
    çelişki sayılmaz ("Kitap 2020 Baskı" ile "Kitap" aynı olabilir).
    """
    tokens_a, tokens_b = spec_tokens(a), spec_tokens(b)
    if not tokens_a or not tokens_b:
        return False
    return bool(tokens_a - tokens_b) and bool(tokens_b - tokens_a)


def title_similarity(a: str, b: str) -> float:
    """0-1 arası benzerlik.

    Üç ölçüt birleştiriliyor:
      - Jaccard: token kümesi kesişimi / birleşimi
      - Kapsama: kesişim / küçük kümenin boyutu — farklı dillerde yazılmış
        aynı ürünü yakalar ("Wireless Mouse" ↔ "Kablosuz Mouse")
      - Karakter dizisi benzerliği

    Kapsama tek başına fazla cömert ("iPhone" ⊂ "iPhone 15 Pro"), ama
    `has_spec_conflict` sayısal varyantları zaten eliyor.
    """
    tokens_a, tokens_b = set(tokenize(a)), set(tokenize(b))
    if not tokens_a or not tokens_b:
        return 0.0

    intersection = len(tokens_a & tokens_b)
    jaccard = intersection / len(tokens_a | tokens_b)
    containment = intersection / min(len(tokens_a), len(tokens_b))
    sequence = SequenceMatcher(None, normalize_title(a), normalize_title(b)).ratio()

    return round((max(jaccard, containment) + sequence) / 2, 4)


def fingerprint(title: str, brand: str | None = None) -> str:
    """Ürün için kararlı kimlik.

    Model kodu varsa ona dayanır (en güvenilir). Yoksa normalleştirilmiş
    başlığın anlamlı belirteçleri alfabetik sıraya konur — kelime sırası
    değişse bile aynı kimlik üretilsin.
    """
    model = extract_model_code(title)
    brand_part = normalize_title(brand or "")

    if model:
        basis = f"{brand_part}|{model}"
    else:
        basis = f"{brand_part}|{' '.join(sorted(tokenize(title)))}"

    return hashlib.sha1(basis.encode()).hexdigest()[:32]


@dataclass
class MatchResult:
    """Eşleştirme kararı ve gerekçesi.

    Gerekçeyi saklıyoruz: "bu iki ürün neden birleşti?" sorusuna cevap
    verebilmek, otomatik birleştirmeyi güvenilir kılan şey.
    """

    matched: bool
    score: float
    reason: str


def match(
    title_a: str,
    title_b: str,
    *,
    brand_a: str | None = None,
    brand_b: str | None = None,
    threshold: float = 0.82,
) -> MatchResult:
    """İki listelemenin aynı ürün olup olmadığına karar verir."""
    model_a = extract_model_code(title_a)
    model_b = extract_model_code(title_b)

    if model_a and model_b:
        if model_a == model_b:
            return MatchResult(True, 1.0, f"model kodu eşleşti: {model_a}")
        # İki tarafın da model kodu var ama farklı: kesinlikle farklı ürünler.
        # Başlık benzerliğine bakmaya gerek yok, "iphone 13" ve "iphone 14"
        # aksi halde eşleşirdi.
        return MatchResult(False, 0.0, f"model kodları farklı: {model_a} ≠ {model_b}")

    if brand_a and brand_b and normalize_title(brand_a) != normalize_title(brand_b):
        return MatchResult(False, 0.0, "marka farklı")

    # Sayısal varyant çelişkisi: "iPhone 13" ve "iPhone 14" başlık olarak
    # neredeyse aynı, ama farklı ürünler. Benzerliğe bakmadan ayır.
    if has_spec_conflict(title_a, title_b):
        conflict_a = spec_tokens(title_a) - spec_tokens(title_b)
        conflict_b = spec_tokens(title_b) - spec_tokens(title_a)
        return MatchResult(
            False,
            0.0,
            f"model varyantı farklı: {sorted(conflict_a)} ≠ {sorted(conflict_b)}",
        )

    score = title_similarity(title_a, title_b)
    if score >= threshold:
        return MatchResult(True, score, f"başlık benzerliği {score:.2f}")

    return MatchResult(False, score, f"başlık benzerliği yetersiz ({score:.2f})")


def find_match(
    title: str,
    candidates: list[tuple[int, str]],
    *,
    brand: str | None = None,
    threshold: float = 0.82,
) -> tuple[int, MatchResult] | None:
    """Aday listesinde en iyi eşleşmeyi bulur.

    `candidates`: (product_id, title) çiftleri.
    """
    best_id: int | None = None
    best: MatchResult | None = None

    for candidate_id, candidate_title in candidates:
        result = match(title, candidate_title, brand_a=brand, threshold=threshold)
        if not result.matched:
            continue
        if best is None or result.score > best.score:
            best_id, best = candidate_id, result

    if best_id is None or best is None:
        return None
    return best_id, best
