# runpod-fleet

Sewa & jaga pod **RunPod** otomatis buat mining — Python murni (stdlib), tanpa `pip install`.  
Jalan di Linux / macOS / Windows (butuh `python3` + `curl`).

## Fitur

- **Multi-akun** — simpan beberapa API key di `config.json`, pilih pakai `--account`
- **Ganti template mudah** — `--template ID` atau `--pick` (pilih dari daftar interaktif)
- **Auto-replace** — pod yang gagal mining (mis. OpenCL mati di host community) langsung dibuang & diganti
- **Verifikasi nyata** — cek logs miner + GPU util via API, bukan cuma "pod RUNNING"
- **Kata kunci bisa diatur** — cocok bukan cuma WildRig, tapi juga miner lain

## Setup

```bash
git clone <repo-url> && cd runpod-fleet
cp config.example.json config.json
```

Edit `config.json`:

```json
{
  "accounts": { "akun1": "rpa_XXXX" },
  "default_account": "akun1",
  "template_id": "abc123xyz",
  "gpu_type_id": "NVIDIA GeForce RTX 5090",
  "cloud_type": "COMMUNITY",
  "count": 10,
  "name_prefix": "prl"
}
```

- **API key**: RunPod → Settings → API Keys  
- **Template ID**: lihat `python fleet.py templates` (template milik akun yang dipilih)

## Perintah

```bash
# MODE WIZARD (disarankan) — satu command, sisanya lewat menu interaktif
python3 fleet.py
#  1) Sewa pod baru (scan template dari akun kamu, pilih GPU/cloud/target)
#  2) Verifikasi semua pod (GOOD/BAD)
#  3) Buang semua pod jelek
#  4) Lihat daftar pod / 5) template
#  6) Setel GPU/cloud/prefix/target
#  7) Ganti akun (tampil label + email)
#  8) Tambah akun baru (tempel API key, otomatis disimpan ke config.json)
#  0) Keluar

# mode manual (tetap ada):
# daftar template milik akun
python3 fleet.py templates --account akun1

# pilih template interaktif, lalu sewa sampai 10 pod GOOD
python3 fleet.py rent --account akun1 --count 10 --pick

# sewa 5 pod sekarang (tanpa loop verifikasi)
python3 fleet.py deploy -n 5

# cek semua pod RUNNING (logs + GPU util)
python3 fleet.py verify

# buang semua pod prefix "prl-" yang jelek
python3 fleet.py terminate --prefix prl- --only-bad

# buang pod tertentu
python3 fleet.py terminate abc123xyz def456

# lihat logs 1 pod
python3 fleet.py logs abc123xyz

# jalanin rent di background (Unix), lalu stop
nohup python3 fleet.py rent --count 30 --account akun1 > fleet.log 2>&1 &
python3 fleet.py stop
```

## Cara kerja `rent`

1. Sewa pod (`podFindAndDeployOnDemand`) — kalau supply habis, auto-retry
2. Tiap ronde: cek logs semua pod (paralel)
3. Ada **GOOD** → dipertahankan  
   ada **BAD** (kata kunci), **LOW** (hashrate < ambang), atau GPU util <5% selama `stale_seconds` → di-terminate & diganti
4. Berhenti otomatis kalau jumlah GOOD ≥ `count`

## Pantau hashrate (auto-terminate pod lemah)

Script parse hashrate dari logs miner (SRBMiner `1 min NNN TH/s`, WildRig
`speed 10s/60s/15m ...`) dan normalisasi ke TH/s. Kalau GOOD tapi hashrate
di bawah ambang → verdict **LOW** → dibuang & diganti saat rent.

```json
"min_hashrate_ths": 300
```

```bash
python3 fleet.py verify            # tampil hr=350.2 per pod
python3 fleet.py rent --min-hashrate 300
python3 fleet.py rent --min-hashrate 0    # matikan pantauan
```

- Grace period: cek hanya jalan setelah uptime > `stale_seconds` (300s),
  pod yang lagi warm-up/download nggak salah dibunuh.
- **Sesuaikan ambang dengan GPU/miner**: 300 TH/s itu buat RTX 5090 +
  SRBMiner/WildRig pearlhash. Miner/GPU lain bisa beda jauh — kalau nggak
  yakin, `0` dulu, lihat angka riil di `verify`, baru set.

## Sesuaikan miner lain

Kalau pakai miner selain WildRig, ubah kata kuncinya:

```json
"good_keywords": ["Accepted", "accepted"],
"bad_keywords": ["No such file", "CUDA error"]
```

atau via CLI:

```bash
python3 fleet.py rent --good-kw "Sol: " --bad-kw "device not found"
```

## Catatan penting

- **Community cloud RunPod**: sebagian host punya OpenCL rusak (`Failed to find OpenCL platform`). Itu masalah host, bukan config — karena itu script ini wajib verifikasi & ganti pod.
- Miner berbasis **CUDA** (mis. krig-miner) jalan di semua host community; **OpenCL** (WildRig) cuma di host yang sehat.
- Biaya pod tetap dihitung selama RUNNING — pod jelek dibuang secepat mungkin biar nggak boros.
- API key jangan di-commit ke GitHub (pakai `.gitignore` buat `config.json`).

## License

Bebas dipakai. Risiko sewa/ billing di tangan kamu.
