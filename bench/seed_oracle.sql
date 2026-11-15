-- The benchmark's fixture data. Small and deterministic on purpose: the point
-- is whether a query is *right*, which a handful of rows shows as well as a
-- million, and a fixed dataset makes the gold answers reproducible.
BEGIN EXECUTE IMMEDIATE 'DROP TABLE islemler';   EXCEPTION WHEN OTHERS THEN NULL; END;
/
BEGIN EXECUTE IMMEDIATE 'DROP TABLE musteriler'; EXCEPTION WHEN OTHERS THEN NULL; END;
/
CREATE TABLE musteriler (
  musteri_id    NUMBER(10) PRIMARY KEY,
  ad_soyad      VARCHAR2(120) NOT NULL,
  eposta        VARCHAR2(160),
  sube_kodu     VARCHAR2(10) NOT NULL,
  segment       VARCHAR2(20) NOT NULL,
  acilis_tarihi DATE NOT NULL
)
/
CREATE TABLE islemler (
  islem_id     NUMBER(10) PRIMARY KEY,
  musteri_id   NUMBER(10) NOT NULL REFERENCES musteriler (musteri_id),
  tutar        NUMBER(14,2) NOT NULL,
  para_birimi  VARCHAR2(3) NOT NULL,
  islem_turu   VARCHAR2(20) NOT NULL,
  durum        VARCHAR2(20) NOT NULL,
  islem_tarihi DATE NOT NULL,
  kanal        VARCHAR2(20) NOT NULL
)
/
INSERT ALL
  INTO musteriler VALUES (1,'Ayse Yilmaz','ayse@example.com','IST01','premium',DATE '2022-01-15')
  INTO musteriler VALUES (2,'Mehmet Demir','mehmet@example.com','IST01','standart',DATE '2022-03-02')
  INTO musteriler VALUES (3,'Zeynep Kaya',NULL,'ANK02','premium',DATE '2023-05-20')
  INTO musteriler VALUES (4,'Ali Sahin','ali@example.com','ANK02','temel',DATE '2023-07-11')
  INTO musteriler VALUES (5,'Fatma Celik',NULL,'IZM03','standart',DATE '2024-02-01')
SELECT * FROM dual
/
INSERT ALL
  INTO islemler VALUES (1,1,1500.00,'TRY','havale','basarili',DATE '2024-01-10','mobil')
  INTO islemler VALUES (2,1, 250.50,'TRY','odeme','basarili',DATE '2024-01-25','web')
  INTO islemler VALUES (3,2, 900.00,'TRY','havale','iptal',  DATE '2024-02-05','mobil')
  INTO islemler VALUES (4,2,3200.75,'USD','transfer','basarili',DATE '2024-02-18','sube')
  INTO islemler VALUES (5,3, 120.00,'TRY','odeme','basarili',DATE '2024-04-03','mobil')
  INTO islemler VALUES (6,3,4500.00,'TRY','transfer','basarili',DATE '2024-04-22','web')
  INTO islemler VALUES (7,4,  75.25,'EUR','odeme','iptal',   DATE '2024-05-09','mobil')
  INTO islemler VALUES (8,4,2100.00,'TRY','havale','basarili',DATE '2024-05-30','sube')
  INTO islemler VALUES (9,5, 640.00,'TRY','odeme','basarili',DATE '2024-06-14','web')
  INTO islemler VALUES (10,5,1800.00,'TRY','transfer','basarili',DATE '2024-06-28','mobil')
SELECT * FROM dual
/
COMMIT
/
