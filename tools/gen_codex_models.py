#!/usr/bin/env python3
"""Generate a Codex model catalog (model_catalog_json) from a qodercli2api proxy.

Why this exists: Codex looks model metadata up by exact slug. Slugs that are not in
its built-in registry fall back to placeholder metadata (272k window) and, worse,
Codex then ignores model_context_window from config.toml. So the Codex model picker
must be generated from GET /v1/models instead of hand-written.

Usage:
    tools/gen_codex_models.py --base-url http://127.0.0.1:8377 --out ~/.codex-qoder/models.json
    Q2A_API_KEY=... tools/gen_codex_models.py --out models.json

Key resolution: --api-key > $Q2A_API_KEY > ~/.config/qoder/env > ~/.zshrc export line.

Context windows come from the proxy's max_tokens. Entries reporting 0 keep their
previous value from the existing --out file (if any), otherwise fall back to
--default-context. --context-override NAME=TOKENS pins a specific model, e.g.
qmodel_38max=800000 to keep the 200k safety margin instead of the full 1M.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request

BUILTIN_FALLBACK = 128000

# 不稳定通道默认不进 Codex 的选单：预发布、演练、灰度和测试档位。
# 用 --keep-unstable 关掉，或用 --exclude 追加自己的规则。
# 另排除专线部署（采供线）和与 基础-* 重复的 线上化基础-* 通道；
# 套餐档位名（基础/旗舰/高级 及 Standard/Ultra/Pro 变体）保留，用户明确要求。
DEFAULT_EXCLUDES = (
    r"^预发",
    r"演练",
    r"(?i)dogfood",
    r"测试",
    r"采供线",
    r"^线上化基础",
)

EFFORT_HINTS = {
    "none": "Disable thinking",
    "low": "Fast responses with lighter reasoning",
    "medium": "Balances speed and reasoning depth",
    "high": "Greater reasoning depth for complex problems",
    "xhigh": "Extra high reasoning depth",
    "max": "Maximum reasoning depth",
}


def read_key(explicit: str | None) -> str:
    if explicit:
        return explicit.strip()
    env = os.environ.get("Q2A_API_KEY", "").strip()
    if env:
        return env
    env_file = os.path.expanduser("~/.config/qoder/env")
    if os.path.isfile(env_file):
        for line in open(env_file, encoding="utf-8", errors="replace"):
            m = re.match(r"\s*export\s+Q2A_API_KEY=(.*)", line)
            if m:
                return m.group(1).strip().strip("'\"")
    zshrc = os.path.expanduser("~/.zshrc")
    if os.path.isfile(zshrc):
        value = ""
        for line in open(zshrc, encoding="utf-8", errors="replace"):
            m = re.match(r"\s*export\s+Q2A_API_KEY=(.*)", line)
            if m:
                value = m.group(1).split("#", 1)[0].strip().strip("'\"")
        if value:
            return value
    sys.exit("no api key: pass --api-key or set Q2A_API_KEY")


def fetch_models(base_url: str, key: str) -> list[dict]:
    req = urllib.request.Request(base_url.rstrip("/") + "/v1/models")
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.load(resp)
    models = payload.get("data") or []
    if not models:
        sys.exit("proxy returned an empty model list")
    return models


def is_uuid(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value))


def dedupe(models: list[dict]) -> list[dict]:
    """One entry per display name.

    The catalog often exposes the same model twice (once under its stable key, once
    under a UUID). Keep the stable key so the shown name stays constant across
    upstream catalog refreshes; never keep two identical rows in the picker.
    """
    best: dict[str, dict] = {}
    for m in models:
        name = (m.get("display_name") or m.get("name") or m.get("id") or "").strip()
        if not name:
            continue
        m = dict(m, _name=name)
        current = best.get(name)
        if current is None:
            best[name] = m
            continue
        # Prefer: non-UUID id > has token limit > richer id
        cur_score = (not is_uuid(current["id"]), current.get("max_tokens", 0) > 0)
        new_score = (not is_uuid(m["id"]), m.get("max_tokens", 0) > 0)
        if new_score > cur_score:
            best[name] = m
        elif new_score == cur_score and not is_uuid(m["id"]) and len(m["id"]) < len(current["id"]):
            best[name] = m
    return sorted(best.values(), key=lambda m: (m["_name"].lower()))


def load_existing(path: str) -> dict:
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return {m.get("slug"): m for m in data.get("models", []) if m.get("slug")}


def base_instructions_from_codex() -> str:
    """First install (no previous catalog): borrow Codex's own model instructions."""
    import shutil
    import subprocess
    import tempfile

    # Use a throwaway CODEX_HOME: the caller's home may be exactly the one whose
    # models.json is missing, which would make `codex debug models` fail as well.
    scratch = tempfile.mkdtemp(prefix="codex-catalog-")
    env = dict(os.environ, CODEX_HOME=scratch)
    try:
        out = subprocess.run(
            ["codex", "debug", "models"],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    try:
        start = out.find("{")
        if start >= 0:
            for m in json.loads(out[start:]).get("models", []):
                text = m.get("base_instructions")
                if text:
                    return text
    except (OSError, ValueError):
        pass
    sys.exit(
        "no base_instructions available; pass --base-instructions-file "
        "(or keep the previous models.json so it can be reused)"
    )


def base_instructions(existing: dict) -> str:
    for m in existing.values():
        text = m.get("base_instructions")
        if text:
            return text
    return base_instructions_from_codex()


def parse_overrides(items: list[str]) -> dict[str, int]:
    out = {}
    for item in items:
        name, _, tokens = item.partition("=")
        if not tokens.isdigit():
            sys.exit(f"bad --context-override {item!r}, want NAME=TOKENS")
        out[name.strip()] = int(tokens)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=os.environ.get("Q2A_BASE_URL", "http://127.0.0.1:8377"))
    ap.add_argument("--api-key")
    ap.add_argument("--out", default="models.json")
    ap.add_argument("--default-context", type=int, default=BUILTIN_FALLBACK)
    ap.add_argument("--context-override", action="append", default=[])
    ap.add_argument(
        "--context-overrides-file",
        help="JSON 文件 {模型名或 slug: 上下文 token 数}，用于上游没报上限的条目",
    )
    ap.add_argument("--base-instructions-file")
    ap.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="额外排除的显示名正则（可重复），例如 --exclude 采供线",
    )
    ap.add_argument(
        "--keep-unstable",
        action="store_true",
        help="保留默认排除的 预发/演练/DogFooding/测试 条目",
    )
    args = ap.parse_args()

    key = read_key(args.api_key)
    raw = fetch_models(args.base_url, key)
    models = dedupe(raw)

    patterns = [] if args.keep_unstable else [re.compile(p) for p in DEFAULT_EXCLUDES]
    patterns += [re.compile(p) for p in args.exclude]
    kept, excluded = [], []
    for m in models:
        (excluded if any(p.search(m["_name"]) for p in patterns) else kept).append(m)
    if excluded:
        print(f"excluded {len(excluded)} unstable/dedicated entries:")
        for m in excluded:
            print(f"  - {m['_name']}  ({m['id']})")
    models = kept

    existing = load_existing(args.out)
    if args.base_instructions_file:
        with open(os.path.expanduser(args.base_instructions_file), encoding="utf-8") as fh:
            instructions = fh.read()
    else:
        instructions = base_instructions(existing)

    overrides = {}
    if args.context_overrides_file:
        try:
            with open(os.path.expanduser(args.context_overrides_file), encoding="utf-8") as fh:
                overrides.update({str(k): int(v) for k, v in json.load(fh).items()})
        except (OSError, ValueError) as exc:
            sys.exit(f"bad --context-overrides-file {args.context_overrides_file}: {exc}")
    overrides.update(parse_overrides(args.context_override))
    out_models = []
    for m in models:
        name, slug = m["_name"], m["id"]
        window = m.get("max_tokens") or 0
        if not window:
            prev = existing.get(slug) or {}
            window = prev.get("context_window") or args.default_context
        if name in overrides:
            window = overrides[name]
        if slug in overrides:
            window = overrides[slug]
        efforts = [e for e in (m.get("thinking_efforts") or []) if isinstance(e, str)]
        if efforts:
            levels = [
                {"effort": e, "description": EFFORT_HINTS.get(e, f"{e} reasoning")}
                for e in efforts
            ]
            if m.get("thinking_off_supported"):
                levels.insert(0, {"effort": "none", "description": "Disable thinking"})
            default_level = m.get("default_effort") or efforts[0]
        else:
            # Upstream gave no thinking_config (e.g. BYOK entries): keep the Codex
            # defaults rather than inventing a per-model ladder.
            levels = [
                {"effort": "low", "description": "Fast responses with lighter reasoning"},
                {"effort": "medium", "description": "Balances speed and reasoning depth"},
                {"effort": "high", "description": "Greater reasoning depth for complex problems"},
            ]
            default_level = "medium"

        out_models.append(
            {
                "slug": slug,
                "display_name": name,
                "description": f"{name} via qodercli2api",
                "default_reasoning_level": default_level,
                "supported_reasoning_levels": levels,
                "shell_type": "shell_command",
                "visibility": "list",
                "supported_in_api": True,
                "priority": 1,
                "additional_speed_tiers": [],
                "service_tiers": [],
                "availability_nux": None,
                "upgrade": None,
                "apply_patch_tool_type": "freeform",
                "truncation_policy": {"mode": "tokens", "limit": 10000},
                "supports_image_detail_original": False,
                "context_window": window,
                "max_context_window": max(window, m.get("max_tokens") or 0),
                "effective_context_window_percent": 100,
                "experimental_supported_tools": [],
                "input_modalities": ["text"],
                "supports_search_tool": False,
                "supports_experimental_context": False,
                "use_responses_lite": False,
                "supports_reasoning_effort_updates": True,
                "node_repl_auto_review_required": False,
                "node_repl_disabled": True,
                "support_verbosity": False,
                "default_verbosity": "medium",
                "default_reasoning_summary": "auto",
                "include_skills_usage_instructions": False,
                "include_plugin_usage_instructions": False,
                "include_apps_usage_instructions": False,
                "base_instructions": instructions,
            }
        )

    out_path = os.path.expanduser(args.out)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump({"models": out_models}, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    print(f"wrote {out_path}: {len(out_models)} models (from {len(raw)} upstream entries)")
    for m in out_models:
        print(f"  {m['display_name'][:36]:38s} slug={m['slug'][:44]:46s} ctx={m['context_window']}")


if __name__ == "__main__":
    main()
