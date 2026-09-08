# price-radar

Çok siteli fiyat ve stok takip platformu. Farklı e-ticaret sitelerindeki aynı ürünü
otomatik olarak eşleştirir, fiyat geçmişini tutar ve düşüşleri tespit eder.

```
Siteler → Adaptörler → Normalleştirme → Ürün eşleştirme → PostgreSQL → Rapor/API
```

> **Durum: Faz 2 tamamlandı.** Scraping çekirdeği, veri modeli, ürün eşleştirme, CLI,
> REST API, zamanlanmış işler ve Docker Compose çalışıyor. Dashboard, export ve
> e-posta bildirimi Faz 3'te.

## Neden bu tasarım

Çoğu "fiyat takip" projesi tek siteye gömülü bir scraper'dır ve o site HTML'ini
değiştirdiği gün ölür. Buradaki üç karar bunu önlüyor:

**Site tanımı kod değil, veri.** Yeni bir site eklemek için Python yazmıyorsun —
API'den `POST /sites` yapıyorsun ya da `config/sites/` altına bir YAML koyuyorsun:

```yaml
slug: benim-magazam
name: Benim Mağazam
base_url: https://ornek.com.tr
adapter: css
currency: TRY
start_urls:
  - https://ornek.com.tr/kategori/kulaklik
selectors:
  item: "div.urun-karti"
  title: "a.urun-adi@title"      # @ ile öznitelik okunur
  url: "a.urun-adi@href"
  price: "span.fiyat"
  availability: "div.stok-durumu"
pagination:
  next: "a.sonraki-sayfa"
```

**Product ile Offer ayrı.** Bir "ürün" gerçek dünyadaki nesnedir; bir "teklif" onun
belirli bir sitedeki listelenmesidir. Aynı kulaklık beş sitede beş farklı başlıkla
durur. Bu ayrımı yapmazsan siteler arası fiyat karşılaştırması yapamazsın — ki
projenin asıl değeri orada.

**Fiyat geçmişi sadece değişimde yazılır.** Saatte bir çalışan bir sistemde 1000 ürün
için günde 24.000 satır yerine yalnızca gerçekten değişenler kaydediliyor.

## Ürün eşleştirme

Aynı ürün farklı sitelerde farklı yazılır:

```
"Sony WH-1000XM5 Wireless Noise Cancelling Headphones - Black"
"SONY WH1000XM5 Kablosuz Kulaklık Siyah"
```

Üç aşamalı, açıklanabilir bir karar veriyor (makine öğrenmesi yok — 500 üründe
getirisi yok, "neden bu ikisi birleşti?" sorusuna cevap verebilmek daha değerli):

1. **Model kodu** — `WH-1000XM5` ↔ `WH1000XM5`. Varsa en güvenilir sinyal, kesin eşleşme.
2. **Sayısal varyant çelişkisi** — `iPhone 13` ile `iPhone 14` başlık olarak %96 benzer
   ama farklı ürünler. `256GB` ile `512GB` de öyle. Benzerliğe bakmadan ayrılırlar.
3. **Bulanık başlık** — Jaccard, kapsama ve karakter dizisi benzerliğinin birleşimi.
   Kapsama ölçütü `Wireless` ↔ `Kablosuz` gibi dil farklarını yakalıyor; 2. adım da
   onun fazla cömert davranmasını engelliyor.

## REST API

```bash
priceradar api                      # http://127.0.0.1:8000/docs
```

Etkileşimli dokümantasyon `/docs` adresinde (OpenAPI'den otomatik üretiliyor).

| Uç nokta | Ne yapar |
|---|---|
| `GET /health` | Sağlık kontrolü — veritabanı, zamanlayıcı, sayaçlar |
| `GET /sites` · `POST /sites` | Site listele / kod yazmadan yeni site ekle |
| `PATCH /sites/{slug}` · `DELETE /sites/{slug}` | Güncelle / sil |
| `GET /products` | Ürünler, en düşük–en yüksek fiyatlarıyla |
| `GET /products?min_offers=2` | Yalnızca birden fazla sitede bulunanlar |
| `GET /products/{id}/history` | Fiyat geçmişi, siteye göre gruplanmış (grafik için hazır) |
| `GET /offers` | Teklifler; site ve stok durumuna göre filtrelenebilir |
| `POST /scrape` | Kazımayı hemen tetikle (202 döner, arka planda çalışır) |
| `GET /scheduler` · `GET /runs` | Zamanlayıcı durumu ve çalıştırma geçmişi |

Örnek — arayüzden site ekleme:

```bash
curl -X POST localhost:8000/sites -H 'Content-Type: application/json' -d '{
  "slug": "ornek-magaza",
  "name": "Örnek Mağaza",
  "base_url": "https://ornek.com.tr",
  "currency": "TRY",
  "start_urls": ["https://ornek.com.tr/kategori/kulaklik"],
  "selectors": {
    "item": "div.urun-karti",
    "title": "a.urun-adi@title",
    "url": "a.urun-adi@href",
    "price": "span.fiyat",
    "availability": "div.stok-durumu"
  }
}'
```

`selectors.item` eksikse istek 422 ile reddedilir — hata kayıt anında verilir,
kazıma sırasında sessizce boş sonuç dönmez.

## Zamanlanmış kazıma

APScheduler kullanılıyor, Celery değil. Bu iş yükü tek bir periyodik görev:
"her N dakikada bir siteleri kazı". Celery ayrı bir broker (Redis), ayrı worker
süreci ve dağıtım karmaşıklığı getiriyor; karşılığında verdiği dağıtık kuyruk bu
ölçekte kullanılmıyor. Birden çok worker makinesi gerektiğinde geçiş kolay:
`run_scrape` zaten bağımsız bir fonksiyon.

```bash
PRICERADAR_SCRAPE_INTERVAL_MINUTES=60   # 0 verirsen zamanlayıcı hiç başlamaz
```

Eşzamanlı çalıştırma kilitle engelleniyor: zamanlanmış iş sürerken elle tetiklersen
409 alırsın. İki kazıma çakışırsa aynı siteye iki kat istek gider ve hız sınırı
anlamsızlaşır.

## Şema göçleri

```bash
alembic upgrade head                          # göçleri uygula
alembic revision --autogenerate -m "açıklama" # model değişikliğinden göç üret
```

Uygulama açılışta şemayı doğruluyor. Eksik sütun veya tablo varsa hangisinin
eksik olduğunu ve ne yapılacağını söyleyen bir hata verip duruyor — eski şemayla
çalışıp ilk sorguda çökmüyor. Boş bir veritabanında tablolar otomatik oluşturulur,
yani ilk çalıştırma için göç komutuna gerek yok.

Alembic'ten önce oluşturulmuş bir veritabanın varsa (Faz 1'den kalma), göç uygulanamaz;
dosyayı silip yeniden oluştur.

Bağlantı adresi `alembic.ini`'de değil, uygulama ayarlarından okunuyor — tek yerde
tanımlı kalsın ve versiyon kontrolüne girmesin. SQLite'ta `render_as_batch` açık,
çünkü SQLite `ALTER TABLE`'ı sınırlı destekliyor.

## Kurulum

```bash
git clone https://github.com/Cagin-karatas/price-radar.git
cd price-radar

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -e .
priceradar init-db
priceradar scrape --site books-toscrape
```

Varsayılan veritabanı SQLite; hiçbir servis kurmadan çalışır. PostgreSQL için:

```bash
docker compose up -d db
export PRICERADAR_DATABASE_URL="postgresql+asyncpg://radar:radar@localhost:5432/priceradar"
priceradar init-db
```

Ya da her şeyi konteynerde:

```bash
docker compose up --build          # göçler + API (http://localhost:8000/docs)
docker compose run --rm scraper    # tek seferlik kazıma
```

Compose zinciri: `db` sağlıklı olunca `migrate` çalışır, o başarıyla bitince `api`
başlar. Böylece uygulama hiçbir zaman güncel olmayan bir şemaya bağlanmaz.

## Kullanım

```bash
priceradar sites                            # tanımlı siteler ve adaptörler
priceradar scrape --dry-run                 # kazı, göster, kaydetme
priceradar scrape -s books-toscrape -v      # tek site, ayrıntılı log
priceradar products --search kulaklık       # takip edilen ürünler
priceradar history 1                        # bir teklifin fiyat geçmişi
priceradar runs                             # geçmiş çalıştırmalar
priceradar robots                           # robots.txt teşhisi
priceradar api --reload                     # REST API (geliştirme)
```

Site tanımları veritabanından okunur. YAML dosyaları her `scrape` çalıştırmasında
veritabanına senkronize edilir; API'den eklenen ya da düzenlenen sitelere
dokunulmaz (`managed_by` alanı bunu ayırt eder).

## Scraping çekirdeği

`AsyncScraperClient` üretimde kullanılabilir olmayı sağlayan kısım:

- **Alan adı başına hız sınırı** — farklı sitelere eşzamanlı gidilir, aynı siteye
  sıraya girilir. Jitter var: sabit aralıkla istek atmak bot imzasıdır.
- **Crawl-delay'e uyum** — site robots.txt'te süre belirtmişse o uygulanır.
- **Üstel geri çekilme** — 429/5xx'te `Retry-After` başlığına uyulur.
- **robots.txt (RFC 9309)** — dosya 4xx dönüyorsa "kural yok" sayılır. Python'un
  yerleşik `RobotFileParser`'ı 403'ü "her şey yasak" kabul ediyor ve Cloudflare
  arkasındaki siteler bot'lara robots.txt için bile 403 döndürdüğünden, izin veren
  siteler erişilmez görünüyordu.
- **Proxy havuzu** — sırayla seçim, başarısız proxy'yi işaretleme. Havuz boşsa
  doğrudan bağlanılır, yani proxy olmadan da çalışır.

## JavaScript ile üretilen sayfalar

Playwright isteğe bağlı bağımlılık (tarayıcı indirmesi 300 MB'ın üzerinde):

```bash
pip install "price-radar[browser]"
playwright install chromium
```

Sonra site YAML'ında `adapter: browser` yeterli. Ayrıştırma mantığı CSS adaptörüyle
aynı — tarayıcı yalnızca HTML'i üretmek için kullanılıyor, bu sayede ayrıştırma
testleri tarayıcı olmadan koşuyor.

## Etik ve yasal not

Demo siteleri (`books.toscrape.com`, `webscraper.io`) kazımaya açıkça izin veren test
siteleridir. Kendi hedefini eklemeden önce o sitenin kullanım şartlarını ve
robots.txt'ini kontrol et. `PRICERADAR_RESPECT_ROBOTS=false` ayarı var ama kullanmadan
önce ne yaptığını bil.

Amazon, Trendyol gibi büyük pazaryerleri kullanım şartlarında kazımayı yasaklar.

## Proje yapısı

```
src/priceradar/
├── normalize.py          # fiyat/para birimi/stok ayrıştırma (1.299,99 ↔ 1,299.99)
├── matching.py           # ürün eşleştirme, duplicate tespiti, parmak izi
├── pipeline.py           # kazı → eşleştir → kaydet → değişim tespiti
├── config.py             # ortam ayarları + YAML site tanımları
├── cli.py
├── scheduler.py          # APScheduler işleri, eşzamanlı çalıştırma kilidi
├── sites_repo.py         # YAML ↔ veritabanı senkronizasyonu
├── api/
│   ├── main.py           # FastAPI uygulaması, lifespan, sağlık kontrolü
│   ├── schemas.py        # Pydantic istek/yanıt modelleri
│   ├── deps.py           # oturum ve ayar bağımlılıkları
│   └── routers/          # sites, products, offers, jobs
├── db/
│   ├── models.py         # Site, Product, Offer, PriceSnapshot, ScrapeRun, PriceAlert
│   └── session.py        # async motor, SQLite/PostgreSQL
└── scraping/
    ├── client.py         # hız sınırı, retry, robots, proxy
    ├── base.py           # Adapter arayüzü, ScrapedItem, kayıt defteri
    ├── css_adapter.py    # YAML/CSS seçici tabanlı (statik HTML)
    └── playwright_adapter.py
config/sites/             # site tanımları (YAML)
migrations/               # Alembic göçleri
tests/                    # 111 test, fixture tabanlı (ağ erişimi gerekmez)
```

## Geliştirme

```bash
pip install -r requirements-dev.txt
pytest -v
```

Testler ağa çıkmaz: kaydedilmiş HTML fixture'ları ve sahte istemci kullanılır.
Veritabanı testleri gerçek async SQLAlchemy ile geçici SQLite üzerinde koşar —
şema aynı olduğu için PostgreSQL davranışını temsil eder.

## Yol haritası

**Faz 2 — API ve otomasyon** ✅
- [x] FastAPI REST (ürünler, teklifler, fiyat geçmişi, site CRUD)
- [x] Background jobs (APScheduler)
- [x] Alembic ile şema göçleri

**Faz 3 — Arayüz ve bildirim**
- [ ] Dashboard (fiyat grafiği, siteler arası karşılaştırma)
- [ ] CSV/Excel export
- [ ] Fiyat düşünce e-posta bildirimi (`PriceAlert` modeli hazır)

**Sonrası**
- [ ] Kimlik doğrulama (API anahtarı veya JWT)
- [ ] Celery + Redis'e geçiş (birden çok worker gerekirse)
- [ ] Ekran görüntüsü geçmişi
- [ ] Webhook desteği
- [ ] Prometheus metrikleri

## Lisans

MIT
