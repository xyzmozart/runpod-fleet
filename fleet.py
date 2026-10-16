#!/usr/bin/env python3
"""
runpod-fleet — sewa & jaga pod RunPod otomatis (stdlib only, no deps).

Commands:
  wizard      mode interaktif (default kalau tanpa subcommand)
  templates   daftar template milik akun
  pods        daftar pod
  deploy      sewa N pod sekarang (sekali jalan)
  rent        sewa + verifikasi + ganti pod jelek, sampai target GOOD
  verify      cek semua pod RUNNING (logs + GPU util)
  terminate   buang pod (by id / prefix / --bad)
  logs        lihat logs 1 pod
  stop        hentikan proses rent yang jalan di background

Contoh:
  python fleet.py                        # wizard interaktif
  python fleet.py templates --account main
  python fleet.py rent --account main --count 10 --pick
  python fleet.py stop

Semua opsi ditaruh DI BELAKANG subcommand.
"""
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

GRAPHQL = "https://api.runpod.io/graphql"
DEFAULT_CONFIG = "config.json"

# ---------------------------------------------------------------- config


def load_config(path):
    if not os.path.exists(path):
        sys.exit(
            "Config tidak ditemukan: %s\n"
            "Salin config.example.json -> config.json lalu isi API key." % path
        )
    with open(path) as f:
        return json.load(f)


def save_config(path, cfg):
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")


def myself_email(key):
    d = gql(key, "{ myself { email } }")
    try:
        return d["data"]["myself"]["email"] or ""
    except Exception:
        return ""


def pick_template(cfg, key):
    tmpls = user_templates(key)
    if not tmpls:
        sys.exit("Tidak ada template custom di akun ini.")
    for i, t in enumerate(tmpls, 1):
        print("%3d. %-45s %s  (%s)" % (i, t["name"], t["id"], t["imageName"]))
    ans = input("Pilih nomor template: ").strip()
    try:
        return tmpls[int(ans) - 1]["id"]
    except (ValueError, IndexError):
        sys.exit("Pilihan tidak valid.")


# ---------------------------------------------------------------- api


def gql(key, query, timeout=30):
    req = urllib.request.Request(
        GRAPHQL + "?api_key=" + key,
        data=json.dumps({"query": query}).encode(),
        headers={
            "Content-Type": "application/json",
            # RunPod menolak User-Agent default Python
            "User-Agent": "runpod-fleet/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"errors": [{"message": "HTTP %s: %s" % (e.code, e.read()[:200])}]}
    if body.get("errors"):
        # supply constraint dsb tetap dikembalikan sebagai data None
        pass
    return body


def my_pods(key):
    d = gql(key, "{ myself { pods { id name desiredStatus templateId } } }")
    return d.get("data", {}).get("myself", {}).get("pods", []) or []


def pod_runtime(key, pid):
    d = gql(key, '{ pod(input:{podId:"%s"}) { runtime { uptimeInSeconds'
            ' gpus { gpuUtilPercent } } } }' % pid)
    try:
        return d["data"]["pod"]["runtime"] or {}
    except Exception:
        return {}


def pod_logs(key, pid, wait=15):
    try:
        out = subprocess.run(
            ["curl", "-s", "-N", "--max-time", str(wait),
             "-H", "Authorization: Bearer " + key,
             "https://api.runpod.io/v2/pods/%s/logs" % pid],
            capture_output=True, text=True, timeout=wait + 15).stdout
    except Exception:
        return []
    lines = []
    for raw in out.splitlines():
        if raw.startswith("data: ") and '"line"' in raw:
            try:
                lines.append(json.loads(raw[6:]).get("line", ""))
            except Exception:
                pass
    return lines


def deploy(key, tpl, args, name):
    q = ('mutation { podFindAndDeployOnDemand(input: {'
         'cloudType: %s, gpuCount: 1, gpuTypeId: "%s", name: "%s", '
         'templateId: "%s", volumeInGb: 0, containerDiskInGb: %d, '
         'minVcpuCount: %d, minMemoryInGb: %d, ports: "22/tcp"})'
         ' { id desiredStatus costPerHr } }' % (
             args.cloud, args.gpu, name, tpl,
             args.disk, args.min_vcpu, args.min_mem))
    d = gql(key, q)
    p = (d.get("data") or {}).get("podFindAndDeployOnDemand")
    if p:
        return p["id"], p.get("costPerHr")
    errs = d.get("errors") or [{}]
    return None, errs[0].get("extensions", {}).get("code", errs[0].get("message", "?"))


def terminate(key, pid):
    d = gql(key, 'mutation { podTerminate(input:{podId:"%s"}) }' % pid)
    return not d.get("errors")


# ---------------------------------------------------------------- verdict

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_UNIT_TH = {"kh": 1e-9, "mh": 1e-6, "gh": 1e-3, "th": 1.0, "ph": 1e3,
            "h": 1e-12}
# SRBMiner: "1 min    350.21 TH/s" / "Hashrate    350.39 TH/s"
_SRB_1MIN = re.compile(r"\b1 min\s+([0-9]+(?:\.[0-9]+)?)\s*([kMGT]?H)/s", re.I)
_SRB_HASH = re.compile(r"\bHashrate\s+([0-9]+(?:\.[0-9]+)?)\s*([kMGT]?H)/s", re.I)
# WildRig: "speed 10s/60s/15m 384 385 n/a TH/s max: 390 TH/s"
_WRG_SPEED = re.compile(
    r"\bspeed 10s/60s/15m\s+(\S+)\s+(\S+)\s+(\S+)\s+([kMGT]?H)/s", re.I)
# fallback generik: angka + satuan H/s terakhir di log
_ANY_HR = re.compile(r"\b([0-9]+(?:\.[0-9]+)?)\s*([kMGT]?H)/s\b", re.I)


def _to_th(value, unit):
    return value * _UNIT_TH.get(unit.lower(), 1.0)


def parse_hashrate_ths(lines):
    """Ambil hashrate (dalam TH/s) dari logs miner. None = tak terbaca.
    Utamakan rata-rata (1 min / 60s) biar nggak false-positive gara-gara
    drop sekejap."""
    txt = [_ANSI_RE.sub("", l) for l in lines]
    # 1) SRBMiner "1 min"
    for l in reversed(txt):
        m = _SRB_1MIN.search(l)
        if m:
            return _to_th(float(m.group(1)), m.group(2))
    # 2) WildRig "speed 10s/60s/15m" -> pakai nilai 60s (fallback 10s/15m)
    for l in reversed(txt):
        m = _WRG_SPEED.search(l)
        if m:
            for tok in (m.group(2), m.group(1), m.group(3)):
                if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", tok):
                    return _to_th(float(tok), m.group(4))
    # 3) SRBMiner baris "Hashrate NNN TH/s"
    for l in reversed(txt):
        m = _SRB_HASH.search(l)
        if m:
            return _to_th(float(m.group(1)), m.group(2))
    # 4) fallback: angka+satuan terakhir yang terbaca
    hr = None
    for l in txt:
        for m in _ANY_HR.finditer(l):
            hr = _to_th(float(m.group(1)), m.group(2))
    return hr


def classify(key, pid, args):
    """-> (verdict, hashrate_ths). verdict: GOOD | BAD | LOW | PENDING"""
    lines = pod_logs(key, pid, wait=args.log_wait)
    txt = "\n".join(lines)
    hr = parse_hashrate_ths(lines)
    verdict = "PENDING"
    for kw in args.bad_keywords:
        if kw in txt:
            verdict = "BAD"
            break
    if verdict == "PENDING":
        for kw in args.good_keywords:
            if kw in txt:
                verdict = "GOOD"
                break
    if verdict == "PENDING" and txt.count("exit code") >= 2:
        verdict = "BAD"
    # pantau hashrate: GOOD tapi di bawah ambang -> LOW (dibuang saat rent)
    if verdict == "GOOD" and args.min_hashrate and hr is not None \
            and hr < args.min_hashrate:
        up = (pod_runtime(key, pid) or {}).get("uptimeInSeconds") or 0
        if up > args.stale:
            verdict = "LOW"
    return verdict, hr


def hr_str(hr):
    return "-" if hr is None else "%.1f" % hr


def is_stale(key, pid, stale_seconds):
    rt = pod_runtime(key, pid)
    up = rt.get("uptimeInSeconds") or 0
    utils = [g.get("gpuUtilPercent") for g in (rt.get("gpus") or [])]
    return up > stale_seconds and utils and all((u or 0) < 5 for u in utils)


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


# ---------------------------------------------------------------- commands


def user_templates(key, show_all=False):
    """Template di akun. Default: sembunyikan template bawaan RunPod
    (image runpod/* dan filebrowser/*), sisanya = punya user sendiri."""
    d = gql(key, "{ myself { podTemplates { id name imageName containerDiskInGb } } }")
    tmpls = (d.get("data") or {}).get("myself", {}).get("podTemplates") or []
    if not show_all:
        tmpls = [t for t in tmpls
                 if not (t.get("imageName") or "").startswith(("runpod/", "filebrowser/"))]
    return tmpls


def cmd_templates(args, key, cfg):
    tmpls = user_templates(key, show_all=getattr(args, "all_templates", False))
    if not tmpls:
        print("(tidak ada template custom; pakai --all untuk lihat bawaan RunPod)")
        return
    for t in tmpls:
        print("%-12s %-45s %s" % (t["id"], t["name"], t["imageName"]))


def cmd_pods(args, key, cfg):
    pods = my_pods(key)
    if not pods:
        print("(tidak ada pod)")
        return
    for p in sorted(pods, key=lambda x: x["name"]):
        print("%-24s %-18s %-9s %s" % (
            p["name"], p["id"], p["desiredStatus"], p.get("templateId", "")))
    from collections import Counter
    print(dict(Counter(p["desiredStatus"] for p in pods)))


def cmd_deploy(args, key, cfg):
    tpl = args.template or cfg.get("template_id")
    if not tpl:
        sys.exit("Template belum di-set. Pakai --template ID, --pick, "
                 "atau isi template_id di config.json.")
    n = args.n or 1
    ok = 0
    for i in range(n):
        name = "%s-%s" % (args.prefix, int(time.time()) % 100000 + i)
        pid, info = deploy(key, tpl, args, name)
        if pid:
            ok += 1
            log("DEPLOY %s (%s/hr)" % (pid, info))
        else:
            log("GAGAL: %s" % info)
            if info == "SUPPLY_CONSTRAINT":
                time.sleep(args.supply_wait)
                # coba lagi
                pid, info = deploy(key, tpl, args, name)
                if pid:
                    ok += 1
                    log("DEPLOY %s (%s/hr)" % (pid, info))
    log("selesai: %d/%d pod baru" % (ok, n))


def cmd_verify(args, key, cfg):
    pods = [p for p in my_pods(key) if p["desiredStatus"] == "RUNNING"]
    if args.prefix:
        pods = [p for p in pods if p["name"].startswith(args.prefix)]
    results = {"GOOD": [], "BAD": [], "LOW": [], "PENDING": []}

    def check(p):
        st, hr = classify(key, p["id"], args)
        rt = pod_runtime(key, p["id"])
        util = [g.get("gpuUtilPercent") for g in (rt.get("gpus") or [])]
        return p, st, hr, util

    with ThreadPoolExecutor(args.concurrency) as ex:
        for p, st, hr, util in ex.map(check, pods):
            print("%-24s %-18s %-8s hr=%-7s util=%s" % (
                p["name"], p["id"], st, hr_str(hr), util))
            results[st].append(p)
    print("\nGOOD=%d LOW=%d BAD=%d PENDING=%d TOTAL=%d" % (
        len(results["GOOD"]), len(results["LOW"]), len(results["BAD"]),
        len(results["PENDING"]), len(pods)))
    if args.min_hashrate:
        print("(ambang hashrate: %g TH/s — LOW = di bawah ambang, "
              "akan dibuang saat rent)" % args.min_hashrate)


def cmd_terminate(args, key, cfg):
    pods = my_pods(key)
    targets = []
    if args.ids:
        targets = [{"id": i, "name": i} for i in args.ids]
    else:
        for p in pods:
            if args.prefix and not p["name"].startswith(args.prefix):
                continue
            if args.only_bad and p["desiredStatus"] == "RUNNING":
                st, hr = classify(key, p["id"], args)
                if st not in ("BAD", "LOW"):
                    continue
            targets.append(p)
    for p in targets:
        if terminate(key, p["id"]):
            log("TERMINATE %s %s" % (p["id"], p["name"]))
        else:
            log("GAGAL terminate %s" % p["id"])
    log("total dibuang: %d" % len(targets))


def cmd_logs(args, key, cfg):
    for l in pod_logs(key, args.pod_id, wait=args.log_wait):
        print(l)


def cmd_rent(args, key, cfg):
    tpl = args.template or cfg.get("template_id")
    if not tpl:
        sys.exit("Template belum di-set. Pakai --template ID, --pick, "
                 "atau isi template_id di config.json.")
    target = args.count
    prefix = args.prefix
    counter = 0
    log("rent dimulai: target=%d GOOD, template=%s, prefix=%s-%s, "
        "min_hr=%g TH/s" % (
            target, tpl, prefix, args.tag, args.min_hashrate or 0))
    write_pidfile(args)
    try:
        while True:
            pods = [p for p in my_pods(key)
                    if p["desiredStatus"] == "RUNNING"
                    and p["name"].startswith(prefix + "-")]
            good, bad, pending = [], [], []

            def check(p):
                st, hr = classify(key, p["id"], args)
                return p, st, hr

            with ThreadPoolExecutor(args.concurrency) as ex:
                for p, st, hr in ex.map(check, pods):
                    log("CHECK %s %s -> %s hr=%s" % (
                        p["id"], p["name"], st, hr_str(hr)))
                    if st == "GOOD":
                        good.append(p)
                    elif st in ("BAD", "LOW"):
                        if st == "LOW":
                            log("  LOW hashrate %s TH/s < %g -> dibuang" % (
                                hr_str(hr), args.min_hashrate))
                        bad.append(p)
                    else:
                        if is_stale(key, p["id"], args.stale):
                            log("  STALE -> BAD %s" % p["name"])
                            bad.append(p)
                        else:
                            pending.append(p)
            for p in bad:
                terminate(key, p["id"])
                log("TERMINATE %s %s" % (p["id"], p["name"]))
            log("SUMMARY good=%d pending=%d bad=%d" % (
                len(good), len(pending), len(bad)))
            if len(good) >= target:
                log("TARGET_REACHED (%d GOOD)" % len(good))
                break
            while len(good) + len(pending) < target and len(pending) < args.batch:
                counter += 1
                name = "%s-%s-%d" % (prefix, args.tag, counter)
                pid, info = deploy(key, tpl, args, name)
                if pid:
                    log("DEPLOY %s %s (%s/hr)" % (pid, name, info))
                    pending.append({"id": pid, "name": name})
                else:
                    log("wait[%s] %s" % (args.tag, info))
                    if info != "SUPPLY_CONSTRAINT":
                        log("  pesan: %s" % info)
                    time.sleep(args.supply_wait)
                    break
            time.sleep(args.round_pause)
    except KeyboardInterrupt:
        log("dihentikan manual (Ctrl+C)")
    finally:
        remove_pidfile()


# ---------------------------------------------------------------- pidfile


def pidfile_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".fleet.pid")


def write_pidfile(args):
    with open(pidfile_path(), "w") as f:
        f.write(str(os.getpid()))


def remove_pidfile():
    try:
        os.remove(pidfile_path())
    except OSError:
        pass


def cmd_stop(args, key, cfg):
    path = pidfile_path()
    if not os.path.exists(path):
        print("tidak ada proses rent yang terdaftar")
        return
    pid = int(open(path).read().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        print("dihentikan pid %d" % pid)
    except OSError as e:
        print("gagal: %s" % e)
    remove_pidfile()


# ---------------------------------------------------------------- wizard


def apply_defaults(args, cfg):
    for attr, ckey, dflt in [
        ("gpu", "gpu_type_id", "NVIDIA GeForce RTX 5090"),
        ("cloud", "cloud_type", "COMMUNITY"),
        ("count", "count", 10),
        ("prefix", "name_prefix", "prl"),
        ("stale", "stale_seconds", 300),
        ("min_hashrate", "min_hashrate_ths", 300),
        ("batch", "deploys_per_round", 3),
        ("disk", "container_disk_gb", 20),
        ("min_vcpu", "min_vcpu", 4),
        ("min_mem", "min_memory_gb", 16),
        ("good_kw", "good_keywords", ["TH/s", "Accepted"]),
        ("bad_kw", "bad_keywords",
         ["Failed to find OpenCL platform", "clGetPlatformIDs"]),
    ]:
        if getattr(args, attr, None) is None:
            setattr(args, attr, cfg.get(ckey, dflt))
        elif isinstance(getattr(args, attr), str) and ckey.endswith("keywords"):
            setattr(args, attr, [x.strip() for x in getattr(args, attr).split(",")])
    args.good_keywords = args.good_kw
    args.bad_keywords = args.bad_kw
    return args


def ask(prompt, default=None):
    try:
        s = input("%s%s: " % (prompt, " [%s]" % default if default is not None else "")).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit("dibatalkan")
    return s or ("" if default is None else str(default))


def ask_int(prompt, default, lo=None, hi=None):
    while True:
        s = ask(prompt, default)
        try:
            v = int(s)
        except ValueError:
            print("  harus angka")
            continue
        if lo is not None and v < lo:
            print("  minimal %d" % lo)
            continue
        if hi is not None and v > hi:
            print("  maksimal %d" % hi)
            continue
        return v


def ask_choice(prompt, choices, default=None):
    """choices: list of (value, label). default = value."""
    for i, (val, label) in enumerate(choices, 1):
        print("  %d. %s" % (i, label))
    idx = {val: i for i, (val, _) in enumerate(choices, 1)}
    dflt = idx.get(default)
    while True:
        s = ask(prompt, dflt)
        try:
            v = int(s)
        except ValueError:
            if s in idx:
                return s
            print("  pilihan tidak valid")
            continue
        if 1 <= v <= len(choices):
            return choices[v - 1][0]
        print("  pilihan tidak valid")


def wizard_subargs(args, cfg, **overrides):
    ns = argparse.Namespace(**vars(args))
    for k, v in overrides.items():
        setattr(ns, k, v)
    return apply_defaults(ns, cfg)


def cmd_wizard(args, key, cfg):
    accounts = cfg.get("accounts") or {}
    cur_account = args.account or cfg.get("default_account", "")
    if not key and accounts:
        cur_account = list(accounts)[0]
        key = accounts[cur_account]
    gpu = args.gpu or cfg.get("gpu_type_id", "NVIDIA GeForce RTX 5090")
    cloud = args.cloud or cfg.get("cloud_type", "COMMUNITY")
    prefix = args.prefix or cfg.get("name_prefix", "prl")
    count = args.count or cfg.get("count", 10)
    min_hr = args.min_hashrate if getattr(args, "min_hashrate", None) is not None \
        else cfg.get("min_hashrate_ths", 300)
    email_cache = {}

    def acct_label(name):
        if name not in email_cache:
            email_cache[name] = myself_email(accounts[name]) or "?"
        return "%s  <%s>" % (name, email_cache[name])

    def clone(**ov):
        ov.setdefault("gpu", gpu)
        ov.setdefault("cloud", cloud)
        ov.setdefault("prefix", prefix)
        ov.setdefault("count", count)
        ov.setdefault("min_hashrate", min_hr)
        ov.setdefault("template", args.template)
        ov.setdefault("tag", args.tag)
        ov.setdefault("key", key)
        ov.setdefault("account", cur_account)
        return wizard_subargs(args, cfg, **ov)

    while True:
        from collections import Counter
        pods = my_pods(key) if key else []
        st = dict(Counter(p["desiredStatus"] for p in pods)) or {}
        print()
        print("=" * 56)
        print(" runpod-fleet wizard")
        print(" akun   : %s" % (acct_label(cur_account) if cur_account in accounts else (cur_account or "(tanpa config)")))
        print(" pod    : %s" % (", ".join("%s=%d" % kv for kv in st.items()) or "kosong"))
        print(" GPU    : %s (%s)" % (gpu, cloud))
        print(" prefix : %s | target: %d GOOD" % (prefix, count))
        print(" min HR : %g TH/s (0 = mati)" % min_hr)
        print("=" * 56)
        print("1) Sewa pod baru (rent otomatis + ganti yang rusak)")
        print("2) Verifikasi semua pod (GOOD/LOW/BAD)")
        print("3) Buang semua pod jelek (BAD/LOW/stale) sesuai prefix")
        print("4) Lihat daftar pod")
        print("5) Lihat daftar template")
        print("6) Setel GPU / cloud / prefix / target / min hashrate")
        print("7) Ganti akun")
        print("8) Tambah akun baru (tempel API key)")
        print("0) Keluar")
        c = ask("Pilih", "0")

        if c == "1":
            tmpls = user_templates(key)
            if not tmpls:
                print("Tidak ada template custom di akun ini — buat dulu di dashboard RunPod.")
                continue
            print("\nTemplate tersedia di akun kamu:")
            for i, t in enumerate(tmpls, 1):
                print("  %2d. %-42s %s (disk %sGB)" % (
                    i, t["name"], t["id"], t.get("containerDiskInGb") or "?"))
            ti = ask_int("Nomor template", 1, 1, len(tmpls))
            tpl = tmpls[ti - 1]["id"]
            g = ask("GPU type id", gpu)
            cld = ask_choice("Cloud type", [
                ("COMMUNITY", "COMMUNITY — murah, host kadang OpenCL rusak"),
                ("SECURE", "SECURE — lebih mahal, host lebih stabil"),
                ("ALL", "ALL — bebas"),
            ], cloud)
            n = ask_int("Target jumlah pod GOOD", count, 1, 500)
            p = ask("Prefix nama pod", prefix)
            mh = ask_int("Min hashrate TH/s (0 = mati)", min_hr, 0, 10 ** 7)
            print("\nRencana sewa:")
            print("  template : %s (%s)" % (tpl, tmpls[ti - 1]["name"]))
            print("  GPU      : %s | cloud: %s" % (g, cld))
            print("  target   : %d GOOD | prefix: %s-%s" % (n, p, args.tag))
            print("  min HR   : %s TH/s" % (mh if mh else "mati"))
            if ask("Mulai sekarang? (y/n)", "y").lower() != "y":
                continue
            gpu, cloud, prefix, count, min_hr = g, cld, p, n, mh
            cmd_rent(clone(template=tpl), key, cfg)
        elif c == "2":
            cmd_verify(clone(), key, cfg)
        elif c == "3":
            cmd_terminate(clone(only_bad=True), key, cfg)
        elif c == "4":
            cmd_pods(clone(), key, cfg)
        elif c == "5":
            cmd_templates(clone(), key, cfg)
        elif c == "6":
            gpu = ask("GPU type id", gpu)
            cloud = ask_choice("Cloud type", [
                ("COMMUNITY", "COMMUNITY — murah"),
                ("SECURE", "SECURE — stabil"),
                ("ALL", "ALL — bebas"),
            ], cloud)
            prefix = ask("Prefix nama pod", prefix)
            count = ask_int("Target GOOD", count, 1, 500)
            min_hr = ask_int("Min hashrate TH/s (0 = mati)", min_hr, 0, 10 ** 7)
        elif c == "7":
            if not accounts:
                print("Belum ada akun di config.json.")
                continue
            names = list(accounts)
            for i, n in enumerate(names, 1):
                mark = " (aktif)" if n == cur_account else ""
                print("  %d. %s%s" % (i, acct_label(n), mark))
            ai = ask_int("Pilih akun", names.index(cur_account) + 1 if cur_account in names else 1,
                         1, len(names))
            cur_account = names[ai - 1]
            key = accounts[cur_account]
            cfg["default_account"] = cur_account  # biar konsisten
            print("akun -> %s" % acct_label(cur_account))
        elif c == "8":
            print("Tempel API key RunPod (rpa_...).")
            new_key = ask("API key").strip()
            if not new_key:
                print("dibatalkan")
                continue
            print("memvalidasi...")
            em = myself_email(new_key)
            if not em:
                print("API key tidak valid atau jaringan bermasalah — tidak disimpan.")
                continue
            default_name = em.split("@")[0]
            new_name = ask("Nama akun (label)", default_name).strip() or default_name
            accounts[new_name] = new_key
            cfg.setdefault("accounts", {})[new_name] = new_key
            cfg["default_account"] = new_name
            save_config(args.config, cfg)
            cur_account = new_name
            key = new_key
            print("tersimpan: %s <%s> -> %s" % (new_name, em, args.config))
        elif c == "0":
            print("bye")
            return
        else:
            print("pilihan tidak valid")


# ---------------------------------------------------------------- main


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=DEFAULT_CONFIG, help="path config.json")
    common.add_argument("--account", help="nama akun di config (lihat 'accounts')")
    common.add_argument("--key", help="API key RunPod (menimpa config)")
    common.add_argument("--template", help="template ID")
    common.add_argument("--pick", action="store_true",
                        help="pilih template interaktif dari daftar")
    common.add_argument("--gpu", help="gpuTypeId (default dari config)")
    common.add_argument("--cloud", help="COMMUNITY / SECURE / ALL")
    common.add_argument("--count", type=int, help="target jumlah pod GOOD")
    common.add_argument("--prefix", help="prefix nama pod, mis. prl")
    common.add_argument("--stale", type=int, dest="stale",
                        help="detik sebelum pod tanpa GPU dianggap rusak")
    common.add_argument("--min-hashrate", dest="min_hashrate", type=float,
                        help="terminate pod GOOD dengan hashrate < ambang "
                             "TH/s (0 = mati, default dari config/300)")
    common.add_argument("--batch", type=int, help="deploy per ronde")
    common.add_argument("--disk", type=int, help="container disk GB")
    common.add_argument("--min-vcpu", dest="min_vcpu", type=int)
    common.add_argument("--min-mem", dest="min_mem", type=int)
    common.add_argument("--log-wait", dest="log_wait", type=int, default=15)
    common.add_argument("--concurrency", type=int, default=8)
    common.add_argument("--supply-wait", dest="supply_wait", type=int, default=20)
    common.add_argument("--round-pause", dest="round_pause", type=int, default=10)
    common.add_argument("--good-kw", dest="good_kw",
                        help="kata kunci GOOD dipisah koma, mis. 'TH/s,Accepted'")
    common.add_argument("--bad-kw", dest="bad_kw",
                        help="kata kunci BAD dipisah koma")
    common.add_argument("--tag", default=time.strftime("%m%d"),
                        help="label ronde, dipakai di nama pod")

    ap = argparse.ArgumentParser(
        description="runpod-fleet: sewa & jaga pod RunPod otomatis")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("wizard", parents=[common],
                   help="mode interaktif (default)")
    p = sub.add_parser("templates", parents=[common])
    p.add_argument("--all", action="store_true", dest="all_templates",
                   help="tampilkan juga template bawaan RunPod")
    sub.add_parser("pods", parents=[common])
    p = sub.add_parser("deploy", parents=[common])
    p.add_argument("-n", type=int, dest="n",
                   help="jumlah pod yang disewa sekarang")
    sub.add_parser("rent", parents=[common])
    sub.add_parser("verify", parents=[common])
    p = sub.add_parser("terminate", parents=[common])
    p.add_argument("ids", nargs="*", help="pod ID (kosongkan = pakai filter)")
    p.add_argument("--only-bad", action="store_true",
                   dest="only_bad", help="hanya yang verdict BAD")
    p = sub.add_parser("logs", parents=[common])
    p.add_argument("pod_id")
    sub.add_parser("stop", parents=[common])

    args = ap.parse_args()
    if args.cmd is None:
        args.cmd = "wizard"  # tanpa subcommand = mode interaktif
        # isi atribut dari parser common secara manual
        for a in common._actions:
            if not a.dest or a.dest == "help":
                continue
            if not hasattr(args, a.dest):
                setattr(args, a.dest, a.default)
    if args.cmd == "stop":
        cmd_stop(args, None, {})  # stop tidak butuh config/API key
        return
    if os.path.exists(args.config):
        cfg = load_config(args.config)
    elif args.key:
        cfg = {}  # config opsional kalau API key dikasih via --key
    else:
        sys.exit(
            "Config tidak ditemukan: %s\n"
            "Salin config.example.json -> config.json lalu isi API key, "
            "atau pakai --key." % args.config
        )

    key = args.key or cfg.get("accounts", {}).get(
        args.account or cfg.get("default_account", ""))
    if not key and args.cmd != "wizard":
        sys.exit("API key tidak ada. Pakai --account NAMA, --key, "
                 "atau isi config.json (accounts/default_account).")

    apply_defaults(args, cfg)

    if args.pick:
        args.template = pick_template(cfg, key)

    {
        "wizard": cmd_wizard,
        "templates": cmd_templates,
        "pods": cmd_pods,
        "deploy": cmd_deploy,
        "rent": cmd_rent,
        "verify": cmd_verify,
        "terminate": cmd_terminate,
        "logs": cmd_logs,
        "stop": cmd_stop,
    }[args.cmd](args, key, cfg)


if __name__ == "__main__":
    main()
