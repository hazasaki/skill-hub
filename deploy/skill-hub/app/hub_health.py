#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hub_health.py - Local management and health channel for Agent Hub.

Read-only CLI tool aggregating ~/.agent-hub sync-status, manifest,
provenance, autosync status, and native memory hook status.

Key architectural properties:
- Fast by default: uses recorded bounded status from state files.
  No expensive recursive tree scans unless --live-fingerprint is requested.
- Strictly read-only: never invokes sync or bootstrap, never acquires writer locks.
- Strict truth in reporting: unknown states are reported as None / unknown with
  visible reasons, NEVER masked as False or OK. Lock held status is explicitly
  reported as indeterminate in read-only mode without active probing.
- Visibly surfaces malformed state and JSON syntax errors.
- Separates pending source ingestion (pending_fingerprint), worker process state,
  and true resource conflicts.
- Distinguishes configured vs running watcher state.
- Scrubs credentials and secrets automatically.
- Zero external dependencies (Python standard library only).
- Modes: concise Vietnamese text (default) or structured JSON (--json).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import sys

# Ensure UTF-8 output on Windows consoles to prevent cp1252 UnicodeEncodeError with Vietnamese
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Attempt import of hub_sync from agent-memory directory for status API and optional live scan
_HERE = Path(__file__).resolve()
_AGENT_MEMORY_DIR = _HERE.parent
if str(_AGENT_MEMORY_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_MEMORY_DIR))

try:
    import hub_sync
except ImportError:
    hub_sync = None

DEFAULT_HUB = Path.home() / ".agent-hub"
DEFAULT_MEMORY = Path.home() / ".agent-memory"
DEFAULT_STALE_THRESHOLD_S = 600  # 10 minutes


def read_json_with_error(path: Path) -> tuple[dict, str | None]:
    """Safely read and parse a JSON file.

    Returns:
        (data_dict, None) on success or if file is cleanly absent.
        ({}, error_message) on malformed JSON or filesystem error.
    """
    if not path.is_file():
        return {}, None
    try:
        content = path.read_text(encoding="utf-8-sig")
        data = json.loads(content)
        if not isinstance(data, dict):
            return {}, f"{path.name}: Định dạng không hợp lệ (kỳ vọng JSON Object, nhận được {type(data).__name__})"
        return data, None
    except json.JSONDecodeError as exc:
        return {}, f"{path.name}: Tệp JSON bị lỗi cú pháp / hỏng dữ liệu ({exc})"
    except OSError as exc:
        return {}, f"{path.name}: Lỗi đọc tệp hệ thống ({exc})"


def _scrub_sensitive(obj):
    """Recursively scrub any sensitive keys from data structures."""
    sensitive_keys = {
        "token", "secret", "password", "key", "auth", "credential",
        "access_token", "api_key", "secret_key"
    }
    if isinstance(obj, dict):
        cleaned = {}
        for k, v in obj.items():
            if any(s in k.lower() for s in sensitive_keys) and not k.endswith("_file") and not k.endswith("_fingerprint"):
                cleaned[k] = "[REDACTED]"
            else:
                cleaned[k] = _scrub_sensitive(v)
        return cleaned
    if isinstance(obj, list):
        return [_scrub_sensitive(item) for item in obj]
    return obj


def check_cycle_busy_readonly(hub_root: Path | None = None, running_pid: int | None = None) -> tuple[bool | None, str]:
    """Read-only cycle lock inspection.

    Always returns (None, reason) because determining if a cross-process writer lock
    is actively held cannot be proven reliably from a strictly read-only inspection
    without acquiring/probing writer locks.
    """
    return None, "không thể xác định ở chế độ chỉ đọc (không thăm dò khoá ghi)"


def collect_hub_health(
    home: Path | str | None = None,
    hub: Path | str | None = None,
    memory_root: Path | str | None = None,
    live_fingerprint: bool = False,
) -> dict:
    """Aggregate health and operational metrics for one Agent Hub."""
    home_dir = Path(home).resolve() if home else Path.home()
    hub_dir = Path(hub).resolve() if hub else (home_dir / ".agent-hub")
    mem_dir = Path(memory_root).resolve() if memory_root else (home_dir / ".agent-memory")

    state_dir = hub_dir / "state"
    autosync_json_path = state_dir / "autosync.json"
    sync_status_json_path = state_dir / "sync-status.json"
    manifest_json_path = hub_dir / "manifest.json"
    provenance_json_path = hub_dir / "provenance.json"

    state_errors = []
    recorded_autosync, err = read_json_with_error(autosync_json_path)
    if err:
        state_errors.append(err)

    # 1. Autosync / Watcher Status API
    autosync_data = {}
    if hub_sync and hasattr(hub_sync, "status"):
        try:
            autosync_data = hub_sync.status(home=home_dir, hub=hub_dir)
        except Exception as exc:
            state_errors.append(f"hub_sync.status(): Lỗi thực thi API ({exc})")
            autosync_data, err = read_json_with_error(autosync_json_path)
            if err:
                state_errors.append(err)
    else:
        autosync_data, err = read_json_with_error(autosync_json_path)
        if err:
            state_errors.append(err)

    enabled = bool(autosync_data.get("enabled"))
    running = bool(autosync_data.get("running"))
    worker_verified = bool(autosync_data.get("worker_verified"))
    pid = autosync_data.get("pid")
    cycles = int(autosync_data.get("cycles") or 0)
    interval_s = int(autosync_data.get("interval_s") or 300)
    debounce_s = int(autosync_data.get("debounce_s") or 30)
    autostart_registered = autosync_data.get("autostart_registered")
    autostart_matches = bool(autosync_data.get("autostart_matches"))
    last_cycle = autosync_data.get("last_cycle") or {}
    last_error = autosync_data.get("last_error")
    pending_fp = autosync_data.get("pending_fingerprint")

    # Watcher State Classification (Configured vs Running)
    is_configured = bool(enabled or autostart_registered)
    if running and worker_verified:
        watcher_state = "RUNNING"
        watcher_desc = f"Đang chạy bình thường (PID: {pid}, danh tính xác minh)"
    elif running and not worker_verified:
        watcher_state = "UNVERIFIED"
        watcher_desc = f"Tiến trình PID {pid} đang chạy nhưng danh tính không khớp bản ghi worker"
    elif is_configured and not running:
        watcher_state = "MERELY_CONFIGURED"
        watcher_desc = "Đã cấu hình (HKCU Run/Enabled), tiến trình hiện tạm dừng / chưa chạy"
    else:
        watcher_state = "DISABLED"
        watcher_desc = "Watcher đã tắt / chưa cấu hình tự động"

    # 2. Cycle Activity & Stale Check (Lock is strictly indeterminate in read-only mode)
    busy_flag, busy_reason = check_cycle_busy_readonly(hub_dir, pid if running else None)

    last_finished_at_str = last_cycle.get("finished_at")
    age_seconds = None
    is_stale = False
    if last_finished_at_str:
        try:
            clean_ts = last_finished_at_str.replace("Z", "+00:00")
            finished_dt = dt.datetime.fromisoformat(clean_ts)
            now_utc = dt.datetime.now(dt.timezone.utc)
            age_seconds = max(0, int((now_utc - finished_dt).total_seconds()))
            stale_threshold = max(interval_s * 2, DEFAULT_STALE_THRESHOLD_S)
            if (running or enabled) and (age_seconds > stale_threshold):
                is_stale = True
        except (ValueError, TypeError):
            pass

    # 3. Disk Sync Status (Cached status from ~/.agent-hub/state/sync-status.json)
    sync_status, err = read_json_with_error(sync_status_json_path)
    if err:
        state_errors.append(err)

    sync_counts = sync_status.get("counts") or last_cycle.get("counts", {}).get("sync") or {}
    conflict_count = int(sync_counts.get("conflict") or 0)
    conflicts_list = sync_status.get("conflicts") or []

    # Support Schema 1 and Schema 2
    raw_resources = sync_status.get("resources") or []
    warnings_list = sync_status.get("warnings") or []
    unsupported_raw = sync_status.get("unsupported") or []

    unsupported_items = []
    preserved_variant_items = []
    conflict_items = []

    if isinstance(raw_resources, list):
        total_sync_resources = len(raw_resources)
        for r in raw_resources:
            if isinstance(r, dict):
                act = r.get("action")
                if act == "unsupported":
                    unsupported_items.append({"name": r.get("name"), "kind": r.get("kind"), "detail": r.get("detail")})
                elif act == "preserved_variant":
                    preserved_variant_items.append({"name": r.get("name"), "kind": r.get("kind"), "detail": r.get("detail")})
                elif act == "conflict":
                    conflict_items.append({"name": r.get("name"), "kind": r.get("kind"), "detail": r.get("detail")})
    else:
        total_sync_resources = int(raw_resources) if isinstance(raw_resources, int) else 0

    # Schema 2 warnings[]
    for w in warnings_list:
        if isinstance(w, dict) and (w.get("action") == "preserved_variant" or "preserved_variant" in str(w.get("reason", ""))):
            preserved_variant_items.append(
                {"name": w.get("name"), "host": w.get("host"), "detail": w.get("reason", "preserved_variant")}
            )

    # Schema 2 unsupported[]
    if unsupported_raw and isinstance(unsupported_raw, list):
        for u in unsupported_raw:
            if isinstance(u, dict):
                unsupported_items.append(
                    {"name": u.get("name"), "scope": u.get("scope"), "host": u.get("host"), "detail": u.get("reason")}
                )

    if not conflicts_list and conflict_items:
        conflicts_list = conflict_items

    # Conflicts are strictly actual resource conflicts; pending_fingerprint is separate
    has_conflicts = conflict_count > 0 or len(conflicts_list) > 0
    has_pending_ingestion = pending_fp is not None

    preserved_variant_count = int(sync_counts.get("preserved_variant") or len(preserved_variant_items))
    has_scoped_unsupported = any(isinstance(item, dict) and item.get('scope')
                                 for item in unsupported_raw)
    if has_scoped_unsupported:
        unsupported_items = [dict(item, detail=item.get('reason'))
                             for item in unsupported_raw if isinstance(item, dict)]
        unsupported_resource_count = sum(item.get('scope') == 'resource'
                                         for item in unsupported_items)
        unsupported_host_count = sum(item.get('scope') == 'host'
                                     for item in unsupported_items)
        unsupported_count = unsupported_resource_count + unsupported_host_count
    else:
        unsupported_count = int(sync_counts.get("unsupported") or len(unsupported_items))
        unsupported_resource_count = unsupported_host_count = None

    hosts_data = sync_status.get("hosts") or {}
    if isinstance(hosts_data, dict):
        attached_hosts = [h for h, info in hosts_data.items() if isinstance(info, dict) and info.get("attached")]
        hosts_count = len(attached_hosts)
    else:
        attached_hosts = []
        hosts_count = int(hosts_data) if isinstance(hosts_data, int) else 0

    # 4. Manifest & Provenance Aggregation
    manifest_data, err = read_json_with_error(manifest_json_path)
    if err:
        state_errors.append(err)
    manifest_resources = manifest_data.get("resources") or {}
    manifest_count = len(manifest_resources)
    kinds_breakdown = {}
    if isinstance(manifest_resources, dict):
        for r_info in manifest_resources.values():
            if isinstance(r_info, dict):
                k = r_info.get("kind", "unknown")
                kinds_breakdown[k] = kinds_breakdown.get(k, 0) + 1

    provenance_data, err = read_json_with_error(provenance_json_path)
    if err:
        state_errors.append(err)
    prov_resources = provenance_data.get("resources") or {}
    prov_count = len(prov_resources)
    origins_breakdown = {}
    if isinstance(prov_resources, dict):
        for r_info in prov_resources.values():
            if isinstance(r_info, dict):
                orig = r_info.get("origin", "unknown")
                origins_breakdown[orig] = origins_breakdown.get(orig, 0) + 1

    # 5. Source Fingerprint (Default: bounded recorded status; Optional: live scan)
    recorded_fp = autosync_data.get("source_fingerprint")
    fingerprint_info = {
        "sha256": recorded_fp,
        "mode": "live_scanned" if live_fingerprint else "recorded_bounded",
        "truncated": (autosync_data.get('last_fingerprint') or {}).get('truncated'),
        "files": None,
        "trees": None,
        "sources": None,
        "live_scan_executed": live_fingerprint,
    }
    recorded_details = autosync_data.get('last_fingerprint') or {}
    for key in ('files', 'trees', 'sources', 'limits', 'incomplete'):
        fingerprint_info[key] = recorded_details.get(key)

    if live_fingerprint and hub_sync and hasattr(hub_sync, "source_fingerprint"):
        try:
            fp_res = hub_sync.source_fingerprint(home=home_dir)
            fingerprint_info["sha256"] = fp_res.get("sha256")
            fingerprint_info["truncated"] = bool(fp_res.get("truncated"))
            fingerprint_info["files"] = fp_res.get("files")
            fingerprint_info["trees"] = fp_res.get("trees")
            fingerprint_info["sources"] = fp_res.get("sources")
        except Exception as exc:
            state_errors.append(f"hub_sync.source_fingerprint(): {exc}")

    # 6. Native Memory Hook (~/.agent-memory/state/status.json)
    hook_info = {"available": False, "counts": {}, "retrying": 0, "state_root": str(mem_dir / "state")}
    hook_status_file = mem_dir / "state" / "status.json"
    if hook_status_file.is_file():
        h_data, err = read_json_with_error(hook_status_file)
        if err:
            state_errors.append(err)
        elif h_data:
            hook_info["available"] = True
            hook_info["counts"] = h_data.get("counts", {})
            hook_info["retrying"] = h_data.get("retrying", 0)
            hook_info["state_root"] = h_data.get("state_root", str(mem_dir / "state"))

    # Overall Status Calculation
    if state_errors:
        overall_status = "ERROR"
    elif not hub_dir.is_dir() or not sync_status:
        overall_status = "UNKNOWN"
    elif last_error or has_conflicts:
        overall_status = "ERROR" if last_error else "CONFLICT"
    elif fingerprint_info.get('incomplete') or fingerprint_info.get('truncated'):
        overall_status = 'INCOMPLETE'
    elif unsupported_resource_count:
        overall_status = 'REJECTED'
    elif last_cycle.get('ok') is False:
        overall_status = 'ERROR'
    elif is_stale:
        overall_status = "STALE"
    elif watcher_state == "MERELY_CONFIGURED":
        overall_status = "WARNING_NOT_RUNNING"
    elif preserved_variant_count > 0 or unsupported_count > 0:
        overall_status = "WARNING"
    else:
        overall_status = "HEALTHY"

    report = {
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "hub": str(hub_dir),
        "home": str(home_dir),
        "overall_status": overall_status,
        "state_errors": state_errors,
        "watcher": {
            "state": watcher_state,
            "description": watcher_desc,
            "configured": is_configured,
            "running": running,
            "worker_verified": worker_verified,
            "pid": pid,
            "enabled": enabled,
            "autostart_registered": autostart_registered is not None,
            "autostart_matches": autostart_matches,
            "interval_s": interval_s,
            "debounce_s": debounce_s,
            "cycles": cycles,
        },
        "cycle_activity": {
            "busy": busy_flag,
            "busy_visibility": busy_reason,
            "stale": is_stale,
            "last_finished_at": last_finished_at_str,
            "age_seconds": age_seconds,
            "last_duration_ms": last_cycle.get("duration_ms"),
            "last_ok": last_cycle.get("ok"),
            "last_error": last_error,
        },
        "pending_ingestion": {
            "has_pending": has_pending_ingestion,
            "pending_fingerprint": pending_fp,
            "note": "Có thay đổi nguồn đã phát hiện nhưng chưa chạy chu kỳ nạp (không phải xung đột)." if has_pending_ingestion else None,
        },
        "cached_sync_state": {
            "host_counts": sync_status.get('host_counts', {}),
            "sync_status_available": bool(sync_status),
            "generated_at": sync_status.get("generated_at"),
            "manifest_total": manifest_count,
            "manifest_kinds": kinds_breakdown,
            "provenance_total": prov_count,
            "provenance_origins": origins_breakdown,
            "total_resources_reported": total_sync_resources or manifest_count,
            "hosts_count": hosts_count,
            "attached_hosts": attached_hosts,
            "counts": sync_counts,
            "conflicts": {
                "detected": has_conflicts,
                "count": conflict_count or len(conflicts_list),
                "items": conflicts_list[:10],
            },
            "preserved_variants": {
                "detected": preserved_variant_count > 0,
                "count": preserved_variant_count,
                "warning": preserved_variant_count > 0,
                "sample_items": preserved_variant_items[:5],
                "note": "Biến thể cục bộ tại host được bảo tồn an toàn, không bị ghi đè.",
            },
            "unsupported": {
                "resource_count": unsupported_resource_count,
                "host_count": unsupported_host_count,
                "detected": unsupported_count > 0,
                "count": unsupported_count,
                "sample_items": unsupported_items[:5],
                "note": "Tài nguyên không tương thích với một số host runtimes.",
            },
        },
        "fingerprint": fingerprint_info,
        "native_hook": hook_info,
    }

    return _scrub_sensitive(report)


def format_vietnamese_report(data: dict) -> str:
    """Format aggregated health data into a concise Vietnamese console report."""
    status_emoji_map = {
        "HEALTHY": "[OK] HOẠT ĐỘNG TỐT",
        "WARNING": "[!] CẢNH BÁO (Có lưu ý)",
        "WARNING_NOT_RUNNING": "[!] CẢNH BÁO: ĐÃ CẤU HÌNH, CHƯA CHẠY / TẠM DỪNG",
        "CONFLICT": "[X] XUNG ĐỘT TÀI NGUYÊN",
        "ERROR": "[X] LỖI VẬN HÀNH / HỎNG DỮ LIỆU",
        "STALE": "[?] CŨ (Stale - Quá hạn cập nhật)",
        "UNKNOWN": "[?] CHƯA RÕ (Không đọc được trạng thái)",
    }
    overall = data.get("overall_status", "UNKNOWN")
    status_label = status_emoji_map.get(overall, f"[{overall}]")

    watcher = data.get("watcher", {})
    cycle = data.get("cycle_activity", {})
    pending = data.get("pending_ingestion", {})
    cached = data.get("cached_sync_state", {})
    fp = data.get("fingerprint", {})
    hook_st = data.get("native_hook", {})
    errors = data.get("state_errors", [])

    lines = []
    lines.append("======================================================================")
    lines.append(f" BÁO CÁO SỨC KHỎE AGENT HUB: {status_label}")
    lines.append("======================================================================")
    lines.append(f"- Đường dẫn Hub  : {data.get('hub')}")
    lines.append(f"- Thư mục Home   : {data.get('home')}")
    lines.append(f"- Thời điểm kiểm : {data.get('timestamp')}")

    if errors:
        lines.append("")
        lines.append("[!] CÁC LỖI TỆP TRẠNG THÁI / API PHÁT HIỆN ĐƯỢC:")
        for err in errors:
            lines.append(f"    * {err}")

    # 1. Watcher (Configured vs Running)
    lines.append("")
    lines.append("1. TRẠNG THÁI TIẾN TRÌNH WATCHER:")
    lines.append(f"   * Trạng thái tiến trình : {watcher.get('description')}")
    reg_text = "Đã đăng ký HKCU Run (Khớp lệnh)" if watcher.get("autostart_matches") else (
        "Đã đăng ký (Lệch lệnh)" if watcher.get("autostart_registered") else "Chưa đăng ký HKCU Run"
    )
    lines.append(f"   * Cấu hình khởi động    : {reg_text}")
    lines.append(f"   * Chu kỳ hoạt động      : Đã xong {watcher.get('cycles', 0)} chu kỳ (Mỗi {watcher.get('interval_s')}s, debounce {watcher.get('debounce_s')}s)")

    # 2. Activity / Stale / Busy
    lines.append("")
    lines.append("2. HOẠT ĐỘNG CHU KỲ (ACTIVITY & FRESHNESS):")
    lines.append(f"   * Tình trạng khoá chu kỳ: {cycle.get('busy_visibility')}")

    age_str = f"{cycle.get('age_seconds')} giây trước" if cycle.get("age_seconds") is not None else "Chưa rõ"
    stale_text = "CẢNH BÁO: Quá hạn cập nhật (STALE)!" if cycle.get("stale") else "Bình thường (Fresh)"
    lines.append(f"   * Chu kỳ hoàn tất gần nhất: {cycle.get('last_finished_at')} ({age_str} - {stale_text})")
    if cycle.get("last_duration_ms"):
        lines.append(f"   * Thời gian chạy gần nhất : {cycle.get('last_duration_ms')} ms (Thành công: {cycle.get('last_ok')})")
    if cycle.get("last_error"):
        lines.append(f"   * LỖI CHU KỲ GẦN NHẤT    : {cycle.get('last_error')}")

    # 3. Pending Ingestion (Separated from worker and conflicts)
    lines.append("")
    lines.append("3. THAY ĐỔI NGUỒN CHỜ NẠP (PENDING INGESTION):")
    if pending.get("has_pending"):
        lines.append(f"   * Trạng thái             : Có nguồn đang chờ ({pending.get('pending_fingerprint')[:16]}...)")
        lines.append("     -> Lưu ý: Đây là trạng thái chờ chu kỳ nạp tiếp theo, không phải xung đột.")
    else:
        lines.append("   * Trạng thái             : Không có thay đổi nguồn nào đang chờ (Đã đồng bộ)")

    # 4. Fingerprint
    lines.append("")
    lines.append("4. DẤU VÂN TAY NGUỒN (SOURCE FINGERPRINT):")
    sha_short = (fp.get("sha256") or "Chưa có")[:24] + "..." if fp.get("sha256") else "Chưa có"
    mode_str = "Quét trực tiếp (--live-fingerprint)" if fp.get("live_scan_executed") else "Lấy từ trạng thái lưu sẵn (mặc định)"
    lines.append(f"   * SHA256 Fingerprint     : {sha_short} ({mode_str})")
    if fp.get("truncated"):
        lines.append(f"   * CẢNH BÁO CẮT NGẮN      : CÓ (Đạt ngưỡng an toàn {fp.get('files')} tệp, {fp.get('trees')} cây thư mục)")
        lines.append("     -> Chưa bao phủ đủ nguồn; chu kỳ không được xem là hoàn tất thành công.")
    elif fp.get('truncated') is None:
        lines.append("   * Bao phủ nguồn          : Chưa có bằng chứng; chạy --live-fingerprint để kiểm tra")
    else:
        lines.append(f"   * Cắt ngắn (Truncation)  : Không ({fp.get('files')} tệp được quét)")

    # 5. Resources, Preserved Variants, Unsupported, Conflicts
    lines.append("")
    lines.append("5. TÀI NGUYÊN & TRẠNG THÁI ĐỒNG BỘ ĐĨA (CACHED SYNC STATE):")
    lines.append(f"   * Thời điểm sinh cache   : {cached.get('generated_at') or 'Chưa có'}")
    lines.append(f"   * Tài nguyên trong Hub   : {cached.get('manifest_total')} mục (Manifest) | {cached.get('provenance_total')} mục (Provenance)")
    if cached.get("manifest_kinds"):
        kinds_str = ", ".join(f"{k}: {v}" for k, v in cached.get("manifest_kinds", {}).items())
        lines.append(f"   * Phân loại (Kinds)      : {kinds_str}")
    if cached.get("provenance_origins"):
        origins_str = ", ".join(f"{k}: {v}" for k, v in cached.get("provenance_origins", {}).items())
        lines.append(f"   * Nguồn gốc (Origins)    : {origins_str}")

    conflicts = cached.get("conflicts", {})
    if conflicts.get("detected"):
        lines.append(f"   * [!] XUNG ĐỘT (CONFLICT): Phát hiện {conflicts.get('count')} xung đột tài nguyên!")
        if conflicts.get("items"):
            c_samples = ", ".join(x.get("name", str(x)) if isinstance(x, dict) else str(x) for x in conflicts.get("items")[:3])
            lines.append(f"     -> Chi tiết: {c_samples}")
    else:
        lines.append("   * Xung đột (Conflicts)   : 0 (Không phát hiện xung đột)")

    pv = cached.get("preserved_variants", {})
    if pv.get("detected"):
        lines.append(f"   * [!] BIẾN THỂ BẢO TỒN   : Có {pv.get('count')} biến thể được giữ nguyên trên host (Tránh ghi đè tùy biến)")
        if pv.get("sample_items"):
            pv_samples = ", ".join(x.get("name", "") for x in pv.get("sample_items")[:3])
            lines.append(f"     -> Ví dụ: {pv_samples}")
    else:
        lines.append("   * Biến thể bảo tồn       : 0")

    unsupported = cached.get("unsupported", {})
    if unsupported.get("detected"):
        if unsupported.get('resource_count') is not None:
            lines.append(f"   * [!] GIỚI HẠN          : {unsupported['resource_count']} tài nguyên bị từ chối; {unsupported['host_count']} giới hạn định dạng/bố cục theo host")
        else:
            lines.append(f"   * [!] KHÔNG TƯƠNG THÍCH  : {unsupported.get('count')} mục từ báo cáo cũ (chưa tách phạm vi)")
        if unsupported.get("sample_items"):
            un_samples = ", ".join(f"{x.get('name')} ({x.get('detail')})" for x in unsupported.get("sample_items")[:3])
            lines.append(f"     -> Ví dụ: {un_samples}")
    else:
        lines.append("   * Không tương thích      : 0")

    # 6. Native Hook
    lines.append("")
    lines.append("6. TRẠNG THÁI NATIVE MEMORY HOOK (~/.agent-memory):")
    if hook_st.get("available"):
        counts = hook_st.get("counts", {})
        counts_str = ", ".join(f"{k}: {v}" for k, v in counts.items()) if counts else "Chưa có lượt chạy"
        lines.append(f"   * Hàng đợi jobs          : {counts_str}")
        lines.append(f"   * Đang thử lại (Retry)   : {hook_st.get('retrying', 0)} lỗi")
        lines.append(f"   * Thư mục State          : {hook_st.get('state_root')}")
    else:
        lines.append("   * Trạng thái hook        : Không khả dụng hoặc chưa khởi tạo hàng đợi")

    lines.append("======================================================================")
    return "\n".join(lines)


def main(argv=None) -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description="Local management and health channel for Agent Hub (read-only)."
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Xuất báo cáo dưới dạng JSON (structured JSON output)",
    )
    parser.add_argument(
        "--hub",
        type=Path,
        default=None,
        help="Đường dẫn thư mục hub (mặc định: ~/.agent-hub)",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=None,
        help="Đường dẫn thư mục home (mặc định: user home)",
    )
    parser.add_argument(
        "--memory-root",
        type=Path,
        default=None,
        help="Đường dẫn thư mục memory (mặc định: ~/.agent-memory)",
    )
    parser.add_argument(
        "--live-fingerprint",
        action="store_true",
        help="Thực hiện quét đệ quy thời gian thực dấu vân tay nguồn (mặc định dùng trạng thái lưu sẵn)",
    )
    parser.add_argument(
        "--skip-fingerprint",
        action="store_true",
        help="Không quét live fingerprint (đây đã là mặc định, giữ để tương thích)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Trả về exit code 1 nếu trạng thái là ERROR, CONFLICT, hoặc WARNING_NOT_RUNNING",
    )

    args = parser.parse_args(argv)

    try:
        data = collect_hub_health(
            home=args.home,
            hub=args.hub,
            memory_root=args.memory_root,
            live_fingerprint=args.live_fingerprint,
        )
    except Exception as exc:
        err_payload = {"error": str(exc), "overall_status": "ERROR"}
        if args.json:
            print(json.dumps(err_payload, indent=2, ensure_ascii=False))
        else:
            print(f"[LỖI HỆ THỐNG] Không thể thu thập trạng thái hub: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(format_vietnamese_report(data))

    if args.check:
        status = data.get("overall_status")
        if status not in ("HEALTHY", "WARNING"):
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
