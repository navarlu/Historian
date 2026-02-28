import hashlib
import json
import logging
import os
import re
import threading
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests
from asyncua.sync import Client as OpcUaClient
from flask import Flask, redirect, request
from influxdb import InfluxDBClient

GRAFANA_URL = "http://localhost:3000"
GRAFANA_API_KEY = ""
GRAFANA_USER = "admin"
GRAFANA_PASSWORD = "admin"
GRAFANA_TIMEOUT_SECONDS = 20
GRAFANA_INFLUX_DATASOURCE_UID = ""
GRAFANA_INFLUX_DATASOURCE_TYPE = "influxdb"

WIZARD_PORT = 5051
TEMPLATE_DIR = Path("grafana/templates")
TREE_CONFIG_FILE = Path("state/grafana_view_tree.json")

INFLUX_HOST = "localhost"
INFLUX_PORT = 8086
INFLUX_DB = "opcuadata"
RAW_TAG_MEASUREMENT = "selected_tag_data"
OPCUA_URL = "opc.tcp://localhost:49320"
SELECTION_FILE = Path("state/tag_selection.json")
HISTORIAN_BACKEND_URL = "http://localhost:5050"

opc_lock = threading.Lock()
opc_client = None
logger = logging.getLogger("grafana_view_wizard")
DEBUG_EVENTS_MAX = 300
debug_events_lock = threading.Lock()
debug_events: list[dict] = []


def setup_logging() -> None:
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)


def add_debug_event(level: str, message: str, details: dict | None = None) -> None:
    event = {
        "ts": now_iso_utc(),
        "level": level,
        "message": message,
        "details": details or {},
    }
    with debug_events_lock:
        debug_events.append(event)
        if len(debug_events) > DEBUG_EVENTS_MAX:
            del debug_events[: len(debug_events) - DEBUG_EVENTS_MAX]
    line = f"{message} | {event['details']}" if event["details"] else message
    if level == "error":
        logger.error(line)
    elif level == "warn":
        logger.warning(line)
    else:
        logger.info(line)


def load_env_file(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_utc_time(value: str):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def grafana_settings() -> dict:
    return {
        "url": os.getenv("GRAFANA_URL", GRAFANA_URL).rstrip("/"),
        "api_key": os.getenv("GRAFANA_API_KEY", GRAFANA_API_KEY).strip(),
        "user": os.getenv("GRAFANA_USER", GRAFANA_USER).strip() or GRAFANA_USER,
        "password": os.getenv("GRAFANA_PASSWORD", GRAFANA_PASSWORD).strip() or GRAFANA_PASSWORD,
        "datasource_uid": os.getenv("GRAFANA_INFLUX_DATASOURCE_UID", GRAFANA_INFLUX_DATASOURCE_UID).strip(),
        "datasource_type": os.getenv("GRAFANA_INFLUX_DATASOURCE_TYPE", GRAFANA_INFLUX_DATASOURCE_TYPE).strip()
        or GRAFANA_INFLUX_DATASOURCE_TYPE,
    }


def grafana_request(method: str, path: str, payload: dict | None = None, query: dict | None = None) -> requests.Response:
    cfg = grafana_settings()
    headers = {"Content-Type": "application/json"}
    auth = None
    if cfg["api_key"]:
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    else:
        auth = (cfg["user"], cfg["password"])

    url = f"{cfg['url']}{path}"
    if query:
        url = f"{url}?{urlencode(query)}"

    response = requests.request(
        method=method,
        url=url,
        data=json.dumps(payload) if payload is not None else None,
        headers=headers,
        auth=auth,
        timeout=GRAFANA_TIMEOUT_SECONDS,
    )
    return response


def grafana_dashboard_direct_url(uid: str, path_url: str = "") -> str:
    base = grafana_settings()["url"]
    clean_uid = str(uid).strip()
    clean_path = str(path_url or "").strip()
    if clean_path.startswith("http://") or clean_path.startswith("https://"):
        root = clean_path
    elif clean_path.startswith("/"):
        root = f"{base}{clean_path}"
    else:
        root = f"{base}/d/{clean_uid}"
    sep = "&" if "?" in root else "?"
    return f"{root}{sep}orgId=1&from=now-24h&to=now&timezone=browser&var-method=raw_auto&refresh=10s"


def historian_backend_url() -> str:
    return os.getenv("HISTORIAN_BACKEND_URL", HISTORIAN_BACKEND_URL).rstrip("/")


def historian_backend_request(method: str, path: str, payload: dict | None = None) -> requests.Response:
    return requests.request(
        method=method,
        url=f"{historian_backend_url()}{path}",
        json=payload,
        timeout=10,
    )


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def template_file(template_id: str) -> Path:
    return TEMPLATE_DIR / f"{template_id}.json"


def safe_template_id(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9\-]+", "-", value.strip())
    normalized = re.sub(r"-+", "-", normalized).strip("-").lower()
    return normalized or "template"


def safe_folder_title(value: str) -> str:
    clean = value.replace("_", "-").replace("%", "pct")
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean or "Untitled"


def load_json(path: Path, fallback):
    if not path.exists():
        return fallback
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)


def get_influx_client() -> InfluxDBClient:
    client = InfluxDBClient(host=INFLUX_HOST, port=INFLUX_PORT)
    client.switch_database(INFLUX_DB)
    return client


def load_selection() -> dict:
    return load_json(SELECTION_FILE, {"tags": []})


def save_selection(selection: dict) -> None:
    save_json(SELECTION_FILE, selection)


def ensure_opc_client() -> OpcUaClient:
    global opc_client
    if opc_client is None:
        opc_client = OpcUaClient(url=os.getenv("OPCUA_URL", OPCUA_URL))
        opc_client.connect()
    return opc_client


def opc_tree_item(node) -> dict:
    browse_name = node.read_browse_name().Name
    node_class = str(node.read_node_class())
    return {
        "browse_name": browse_name,
        "nodeid": node.nodeid.to_string(),
        "node_class": node_class,
    }


def list_templates() -> list[dict]:
    ensure_dir(TEMPLATE_DIR)
    result = []
    for path in sorted(TEMPLATE_DIR.glob("*.json")):
        data = load_json(path, {})
        result.append(
            {
                "template_id": data.get("template_id", path.stem),
                "title": data.get("title", ""),
                "source_dashboard_uid": data.get("source_dashboard_uid", ""),
                "captured_at": data.get("captured_at", ""),
                "variables": data.get("variables", []),
            }
        )
    return result


def sample_tree() -> dict:
    return {
        "root_folder": "Historian",
        "tree": {
            "id": "root",
            "type": "folder",
            "name": "Plant A",
            "children": [
                {
                    "id": "zone-1",
                    "type": "folder",
                    "name": "Zone 1",
                    "children": [
                        {
                            "id": "zone-1-loop-1",
                            "type": "view",
                            "name": "Loop 1 Overview",
                            "template_id": "",
                            "tags": [],
                            "process_tags": [],
                            "variable_values": {},
                            "children": [
                                {"id": "zone-1-loop-1-tags", "type": "view_tags", "name": "tags"},
                                {"id": "zone-1-loop-1-dashboard", "type": "view_dashboard", "name": "dashboard"},
                            ],
                        },
                        {
                            "id": "zone-1-loop-2",
                            "type": "view",
                            "name": "Loop 2 Overview",
                            "template_id": "",
                            "tags": [],
                            "process_tags": [],
                            "variable_values": {},
                            "children": [
                                {"id": "zone-1-loop-2-tags", "type": "view_tags", "name": "tags"},
                                {"id": "zone-1-loop-2-dashboard", "type": "view_dashboard", "name": "dashboard"},
                            ],
                        },
                    ],
                },
                {
                    "id": "zone-2",
                    "type": "folder",
                    "name": "Zone 2",
                    "children": [
                        {
                            "id": "zone-2-loop-1",
                            "type": "view",
                            "name": "Loop 1 Overview",
                            "template_id": "",
                            "tags": [],
                            "process_tags": [],
                            "variable_values": {},
                            "children": [
                                {"id": "zone-2-loop-1-tags", "type": "view_tags", "name": "tags"},
                                {"id": "zone-2-loop-1-dashboard", "type": "view_dashboard", "name": "dashboard"},
                            ],
                        }
                    ],
                },
            ],
        },
    }


def dashboard_uid_for_instance(path_items: list[str], template_id: str, title: str) -> str:
    seed = "/".join(path_items + [template_id, title]).encode("utf-8")
    digest = hashlib.sha1(seed).hexdigest()[:12]
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", title.lower()).strip("-")
    slug = slug[:20] if slug else "view"
    return f"{template_id[:12]}-{slug}-{digest}"[:40]


def set_template_variables(dashboard: dict, variable_values: dict) -> list[str]:
    missing = []
    templating = dashboard.get("templating", {})
    variables = templating.get("list", [])

    names = set()
    for var in variables:
        name = str(var.get("name", "")).strip()
        if not name:
            continue
        names.add(name)
        if name not in variable_values:
            continue
        value = str(variable_values[name])
        var["current"] = {"text": value, "value": value, "selected": True}
        options = var.get("options")
        if isinstance(options, list):
            for option in options:
                option["selected"] = str(option.get("value")) == value or str(option.get("text")) == value

    for key in variable_values:
        if key not in names:
            missing.append(key)
    return missing


def find_folder_uid(title: str, parent_uid: str | None) -> str | None:
    query = {"limit": "2000"}
    if parent_uid:
        query["parentUid"] = parent_uid
    response = grafana_request("GET", "/api/folders", query=query)
    if response.status_code != 200:
        return None
    for folder in response.json():
        if folder.get("title") == title:
            return folder.get("uid")
    return None


def ensure_folder_path(root_folder: str, path_items: list[str]) -> tuple[str | None, list[str]]:
    issues = []
    parent_uid = None
    full_path = [root_folder] + path_items
    for index, segment in enumerate(full_path):
        title = safe_folder_title(segment)
        found_uid = find_folder_uid(title=title, parent_uid=parent_uid)
        if found_uid:
            parent_uid = found_uid
            continue

        payload = {"title": title}
        if parent_uid:
            payload["parentUid"] = parent_uid

        create_response = grafana_request("POST", "/api/folders", payload=payload)
        if create_response.status_code == 200:
            parent_uid = create_response.json().get("uid")
            continue

        if create_response.status_code == 412:
            existing_uid = find_folder_uid(title=title, parent_uid=parent_uid)
            if existing_uid:
                parent_uid = existing_uid
                continue

        if parent_uid and create_response.status_code == 400:
            # Nested folders may be disabled. Fall back to flat root folders:
            # "Root / Child / Leaf".
            flat_title = safe_folder_title(" / ".join(full_path[: index + 1]))
            flat_uid = find_folder_uid(title=flat_title, parent_uid=None)
            if flat_uid:
                parent_uid = flat_uid
                continue
            flat_resp = grafana_request("POST", "/api/folders", payload={"title": flat_title})
            if flat_resp.status_code == 200:
                parent_uid = flat_resp.json().get("uid")
                continue

        issues.append(f"Folder create failed for '{title}': {create_response.status_code} {create_response.text}")
        return None, issues
    return parent_uid, issues


def normalize_node(node: dict, default_name: str) -> dict:
    node_type = str(node.get("type", "folder")).strip().lower()
    if node_type not in {"folder", "view", "view_tags", "view_dashboard"}:
        node_type = "folder"
    name = str(node.get("name", default_name)).strip() or default_name
    node_id = str(node.get("id", "")).strip() or safe_template_id(f"{node_type}-{name}")
    normalized = {"id": node_id, "type": node_type, "name": name}
    if node_type == "folder":
        children = node.get("children", [])
        if not isinstance(children, list):
            children = []
        normalized["children"] = [
            normalize_node(child, default_name="Folder")
            for child in children
            if isinstance(child, dict)
        ]
        return normalized

    if node_type in {"view_tags", "view_dashboard"}:
        normalized["name"] = "tags" if node_type == "view_tags" else "dashboard"
        return normalized

    template_id_raw = str(node.get("template_id", "")).strip()
    template_id = safe_template_id(template_id_raw) if template_id_raw else ""
    tags = node.get("tags", [])
    if isinstance(tags, str):
        tags = [x.strip() for x in tags.split(",") if x.strip()]
    if not isinstance(tags, list):
        tags = []
    variable_values = node.get("variable_values", {})
    if not isinstance(variable_values, dict):
        variable_values = {}
    generated_dashboard_uid = str(node.get("generated_dashboard_uid", "")).strip()
    generated_dashboard_url = str(node.get("generated_dashboard_url", "")).strip()
    generated_dashboard_direct_url = str(node.get("generated_dashboard_direct_url", "")).strip()
    generated_at = str(node.get("generated_at", "")).strip()
    process_tags = node.get("process_tags", [])
    normalized_process_tags = []
    if isinstance(process_tags, list):
        for item in process_tags:
            if isinstance(item, dict):
                nodeid = str(item.get("nodeid", "")).strip()
                label = str(item.get("label", "")).strip()
                if nodeid:
                    normalized_process_tags.append({"nodeid": nodeid, "label": label or nodeid})
            elif isinstance(item, str):
                nodeid = item.strip()
                if nodeid:
                    normalized_process_tags.append({"nodeid": nodeid, "label": nodeid})
    normalized["template_id"] = template_id
    normalized["tags"] = [str(x).strip() for x in tags if str(x).strip()]
    normalized["process_tags"] = normalized_process_tags
    normalized["variable_values"] = {
        str(key).strip(): value
        for key, value in variable_values.items()
        if str(key).strip()
    }
    if generated_dashboard_uid:
        normalized["generated_dashboard_uid"] = generated_dashboard_uid
    if generated_dashboard_url:
        normalized["generated_dashboard_url"] = generated_dashboard_url
    if generated_dashboard_direct_url:
        normalized["generated_dashboard_direct_url"] = generated_dashboard_direct_url
    if generated_at:
        normalized["generated_at"] = generated_at
    normalized["children"] = [
        {"id": f"{node_id}-tags", "type": "view_tags", "name": "tags"},
        {"id": f"{node_id}-dashboard", "type": "view_dashboard", "name": "dashboard"},
    ]
    return normalized


def ensure_folder_node(node: dict) -> dict:
    normalized = normalize_node(node, default_name="Plant")
    if normalized["type"] != "folder":
        return {"id": "root", "type": "folder", "name": "Plant", "children": []}
    return normalized


def legacy_nodes_to_tree(legacy_nodes: list[dict]) -> dict:
    root = {"id": "root", "type": "folder", "name": "Plant", "children": []}

    def upsert_folder(parent: dict, folder_name: str) -> dict:
        folder_name = str(folder_name).strip() or "Folder"
        for child in parent["children"]:
            if child.get("type") == "folder" and child.get("name") == folder_name:
                return child
        folder = {
            "id": safe_template_id(f"{parent.get('id', 'root')}-{folder_name}"),
            "type": "folder",
            "name": folder_name,
            "children": [],
        }
        parent["children"].append(folder)
        return folder

    for legacy in legacy_nodes:
        if not isinstance(legacy, dict):
            continue
        target = root
        for item in legacy.get("path", []):
            target = upsert_folder(target=target, folder_name=str(item))
        for view in legacy.get("views", []):
            if not isinstance(view, dict):
                continue
            title = str(view.get("title", "")).strip() or "Untitled view"
            target["children"].append(
                {
                    "id": safe_template_id(
                        f"{'/'.join(legacy.get('path', []))}-{title}-{view.get('template_id', '')}"
                    ),
                    "type": "view",
                    "name": title,
                    "template_id": safe_template_id(str(view.get("template_id", ""))),
                    "tags": view.get("tags", []),
                    "variable_values": view.get("variable_values", {}),
                }
            )
    return ensure_folder_node(root)


def ensure_unique_ids(root: dict) -> dict:
    used: set[str] = set()

    def walk(node: dict, prefix: str) -> None:
        original = str(node.get("id", "")).strip() or safe_template_id(f"{prefix}-{node.get('name', 'node')}")
        candidate = original
        counter = 1
        while candidate in used:
            counter += 1
            candidate = f"{original}-{counter}"
        node["id"] = candidate
        used.add(candidate)
        if isinstance(node.get("children"), list):
            for child in node["children"]:
                if isinstance(child, dict):
                    walk(child, prefix=prefix)

    walk(root, prefix="node")
    return root


def normalize_tree_config(tree: dict) -> dict:
    if not isinstance(tree, dict):
        return sample_tree()

    root_folder = safe_folder_title(str(tree.get("root_folder", "Historian")).strip() or "Historian")

    if isinstance(tree.get("tree"), dict):
        root_tree = ensure_folder_node(tree["tree"])
        root_tree = ensure_unique_ids(root_tree)
        return {"root_folder": root_folder, "tree": root_tree}

    legacy_nodes = tree.get("nodes", [])
    if isinstance(legacy_nodes, list):
        converted = legacy_nodes_to_tree(legacy_nodes)
        converted = ensure_unique_ids(converted)
        return {"root_folder": root_folder, "tree": converted}

    return sample_tree()


def collect_view_instances(node: dict, path_items: list[str], result: list[tuple[list[str], dict]]) -> None:
    node_type = node.get("type")
    name = str(node.get("name", "")).strip()
    if node_type == "view":
        result.append((path_items, node))
        return

    if node_type != "folder":
        return

    next_path = path_items
    if name:
        next_path = path_items + [name]
    for child in node.get("children", []):
        if isinstance(child, dict):
            collect_view_instances(child, next_path, result)


def normalize_process_tags(raw_items) -> list[dict]:
    result = []
    if not isinstance(raw_items, list):
        return result
    for item in raw_items:
        if isinstance(item, str):
            nodeid = item.strip()
            if nodeid:
                result.append({"nodeid": nodeid, "label": nodeid})
            continue
        if not isinstance(item, dict):
            continue
        nodeid = str(item.get("nodeid", "")).strip()
        if not nodeid:
            continue
        label = str(item.get("label", "")).strip() or nodeid
        result.append({"nodeid": nodeid, "label": label})
    return result


def influx_identifier(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def influx_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def method_guard(method_name: str) -> str:
    return f"AND '{method_name}' = '$method'"


def grafana_targets_for_tag(tag: dict, index: int) -> list[dict]:
    nodeid = str(tag.get("nodeid", "")).strip()
    label = str(tag.get("label", "")).strip() or nodeid or f"Tag {index + 1}"
    safe_nodeid = influx_string(nodeid)
    safe_alias = influx_identifier(label)
    ref_seed = f"{index + 1:03d}"
    return [
        {
            "refId": f"A{ref_seed}",
            "rawQuery": True,
            "resultFormat": "time_series",
            "query": (
                f'SELECT last("value") AS "{safe_alias}" FROM "{RAW_TAG_MEASUREMENT}" '
                f"WHERE $timeFilter AND \"nodeid\" = '{safe_nodeid}' "
                f"{method_guard('raw_1s')} GROUP BY time(1s) fill(null)"
            ),
            "alias": label,
        },
        {
            "refId": f"B{ref_seed}",
            "rawQuery": True,
            "resultFormat": "time_series",
            "query": (
                f'SELECT mean("value") AS "{safe_alias}" FROM "{RAW_TAG_MEASUREMENT}" '
                f"WHERE $timeFilter AND \"nodeid\" = '{safe_nodeid}' "
                f"{method_guard('raw_auto')} GROUP BY time($__interval) fill(null)"
            ),
            "alias": label,
        },
        {
            "refId": f"C{ref_seed}",
            "rawQuery": True,
            "resultFormat": "time_series",
            "query": (
                f'SELECT last("value") AS "{safe_alias}" FROM "{RAW_TAG_MEASUREMENT}" '
                f"WHERE $timeFilter AND \"nodeid\" = '{safe_nodeid}' "
                f"{method_guard('ds_1m')} GROUP BY time(1m) fill(null)"
            ),
            "alias": label,
        },
        {
            "refId": f"D{ref_seed}",
            "rawQuery": True,
            "resultFormat": "time_series",
            "query": (
                f'SELECT mean("value") AS "{safe_alias}" FROM "{RAW_TAG_MEASUREMENT}" '
                f"WHERE $timeFilter AND \"nodeid\" = '{safe_nodeid}' "
                f"{method_guard('ds_auto')} GROUP BY time($__interval) fill(null)"
            ),
            "alias": label,
        },
    ]


def grafana_panel_for_tags(tags: list[dict], datasource_uid: str, datasource_type: str) -> dict:
    targets = []
    for index, tag in enumerate(tags):
        targets.extend(grafana_targets_for_tag(tag=tag, index=index))
    panel = {
        "id": 1,
        "type": "timeseries",
        "title": "Process Tags",
        "gridPos": {
            "h": 18,
            "w": 24,
            "x": 0,
            "y": 0,
        },
        "fieldConfig": {
            "defaults": {
                "decimals": 2,
                "color": {"mode": "palette-classic"},
            },
            "overrides": [],
        },
        "options": {
            "legend": {"displayMode": "table", "placement": "bottom", "showLegend": True},
            "tooltip": {"mode": "single", "sort": "none"},
        },
        "targets": targets,
    }
    if datasource_uid:
        panel["datasource"] = {"uid": datasource_uid, "type": datasource_type}
    return panel


def build_auto_dashboard(title: str, tags: list[str], process_tags: list[dict], datasource_uid: str, datasource_type: str) -> dict:
    normalized_tags = normalize_process_tags(process_tags)
    panels = [grafana_panel_for_tags(tags=normalized_tags, datasource_uid=datasource_uid, datasource_type=datasource_type)]
    return {
        "id": None,
        "uid": None,
        "title": title,
        "tags": tags,
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 0,
        "refresh": "10s",
        "time": {"from": "now-24h", "to": "now"},
        "panels": panels,
        "templating": {
            "list": [
                {
                    "name": "method",
                    "label": "Method",
                    "type": "custom",
                    "query": "raw_1s,raw_auto,ds_1m,ds_auto",
                    "current": {"text": "raw_auto", "value": "raw_auto"},
                }
            ]
        },
    }


def deploy_tree_config(tree: dict) -> dict:
    normalized = normalize_tree_config(tree)
    settings = grafana_settings()
    datasource_uid = settings["datasource_uid"]
    datasource_type = settings["datasource_type"]
    root_folder = normalized["root_folder"]
    root_node = normalized["tree"]
    view_instances: list[tuple[list[str], dict]] = []
    collect_view_instances(root_node, [], view_instances)

    created = []
    warnings = []
    errors = []

    for path_items_raw, view in view_instances:
        path_items = [safe_folder_title(str(item)) for item in path_items_raw if str(item).strip()]
        folder_uid, folder_issues = ensure_folder_path(root_folder=root_folder, path_items=path_items)
        warnings.extend(folder_issues)
        if not folder_uid:
            errors.append(f"Unable to resolve folder for path: {path_items}")
            continue

        title = str(view.get("name", "")).strip() or "Untitled view"

        tag_list = []
        view_tags = view.get("tags", [])
        if isinstance(view_tags, str):
            view_tags = [x.strip() for x in view_tags.split(",") if x.strip()]
        if isinstance(view_tags, list):
            tag_list.extend([str(x) for x in view_tags if str(x).strip()])
        final_tags = sorted(set(tag_list))
        process_tags = normalize_process_tags(view.get("process_tags", []))

        template_id = safe_template_id(str(view.get("template_id", "")))
        if template_id:
            template_path = template_file(template_id)
            if not template_path.exists():
                errors.append(f"Template '{template_id}' not found for view '{view.get('name', '')}'")
                continue
            template_payload = load_json(template_path, {})
            template_dashboard = template_payload.get("dashboard", {})
            if not template_dashboard:
                errors.append(f"Template '{template_id}' has no dashboard JSON.")
                continue
            dashboard = deepcopy(template_dashboard)
            if isinstance(dashboard.get("tags"), list):
                final_tags = sorted(set(final_tags + [str(x) for x in dashboard.get("tags", []) if str(x).strip()]))
        else:
            dashboard = build_auto_dashboard(
                title=title,
                tags=final_tags,
                process_tags=process_tags,
                datasource_uid=datasource_uid,
                datasource_type=datasource_type,
            )

        dashboard["title"] = title
        dashboard["id"] = None
        dashboard["uid"] = dashboard_uid_for_instance(path_items=path_items, template_id=(template_id or "blank"), title=title)
        dashboard["version"] = 0
        dashboard["tags"] = final_tags

        variable_values = view.get("variable_values", {})
        if not isinstance(variable_values, dict):
            variable_values = {}
        if template_id:
            unknown_variables = set_template_variables(dashboard=dashboard, variable_values=variable_values)
            if unknown_variables:
                warnings.append(
                    f"View '{title}' has unknown template variables: {', '.join(sorted(unknown_variables))}"
                )

        payload = {
            "dashboard": dashboard,
            "folderUid": folder_uid,
            "overwrite": True,
            "message": "Generated by grafana_view_wizard MVP",
        }
        response = grafana_request("POST", "/api/dashboards/db", payload=payload)
        if response.status_code not in (200, 201):
            errors.append(
                f"Dashboard push failed for '{title}': {response.status_code} {response.text}"
            )
            continue

        body = response.json()
        created.append(
            {
                "title": title,
                "template_id": template_id,
                "uid": body.get("uid", dashboard.get("uid", "")),
                "url": body.get("url", ""),
                "folder_path": [root_folder] + path_items,
            }
        )

    return {"created": created, "warnings": warnings, "errors": errors}


app = Flask(__name__)


@app.get("/")
def index():
    return """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Grafana Tree Editor</title>
  <link
    rel="stylesheet"
    href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.css"
  />
  <link
    rel="stylesheet"
    href="https://cdn.jsdelivr.net/npm/wunderbaum@0/dist/wunderbaum.min.css"
  />
  <style>
    html, body { height: 100%; }
    body {
      font-family: "Segoe UI", Tahoma, sans-serif;
      margin: 0;
      background: #f5f7fb;
      color: #18263a;
    }
    .layout {
      height: 100%;
      display: grid;
      grid-template-columns: 320px 1fr;
    }
    .left-pane {
      border-right: 1px solid #dbe4ef;
      background: #ffffff;
      display: flex;
      flex-direction: column;
      width: 320px;
      min-width: 320px;
      max-width: 320px;
      min-height: 0;
      overflow: hidden;
    }
    .left-body {
      flex: 1;
      min-height: 0;
      display: grid;
      grid-template-rows: minmax(0, 1fr) minmax(0, 1fr);
    }
    .pane-head {
      padding: 12px;
      border-bottom: 1px solid #e4ebf3;
      background: linear-gradient(180deg, #fcfeff 0%, #f3f7fb 100%);
    }
    .title {
      margin: 0 0 8px 0;
      font-size: 18px;
      font-weight: 700;
    }
    .actions { display: flex; gap: 8px; flex-wrap: wrap; }
    button {
      background: #1b5ea8;
      color: #fff;
      border: 1px solid #154f8d;
      border-radius: 8px;
      padding: 8px 12px;
      cursor: pointer;
      font: inherit;
      font-size: 13px;
    }
    button.secondary { background: #fff; color: #1b5ea8; }
    .tree-wrap {
      min-height: 0;
      overflow: auto;
      padding: 10px;
      background: #f8fbff;
      border-bottom: 1px solid #e4ebf3;
    }
    .debug-wrap {
      min-height: 0;
      overflow: hidden;
      background: #fbfdff;
      display: flex;
      flex-direction: column;
    }
    .debug-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      padding: 8px 10px;
      border-bottom: 1px solid #e4ebf3;
      background: #f4f8fd;
    }
    .debug-title {
      margin: 0;
      font-size: 14px;
      color: #1e3550;
    }
    .wb-host { min-height: 100%; }
    .wunderbaum { border: 0; background: transparent; }
    .wb-row { border-radius: 6px; margin: 1px 0; }
    .wb-row.wb-active { background: #e9f3ff !important; }
    .wb-title { color: #203449; font-size: 13px; }
    .wb-node[data-wb-type="view"] .wb-title { color: #1f4f88; }
    .status {
      padding: 8px 10px;
      font-size: 12px;
      color: #4b6078;
      background: #ffffff;
      font-family: Consolas, monospace;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      word-break: break-word;
      border-bottom: 1px solid #e4ebf3;
      max-height: 96px;
      overflow: auto;
    }
    .ctx-menu {
      position: fixed;
      z-index: 1000;
      width: 220px;
      background: #ffffff;
      border: 1px solid #cfd9e6;
      border-radius: 8px;
      box-shadow: 0 10px 28px rgba(18, 33, 52, 0.18);
      padding: 6px;
      display: none;
    }
    .ctx-menu button {
      width: 100%;
      text-align: left;
      background: transparent;
      color: #203449;
      border: 0;
      border-radius: 6px;
      padding: 7px 9px;
      font-size: 13px;
    }
    .ctx-menu button:hover {
      background: #eef5fd;
      color: #12457a;
    }
    .ctx-menu button.danger:hover {
      background: #fdeeee;
      color: #8a1d1d;
    }
    .right-pane {
      padding: 14px;
      background: radial-gradient(circle at 20% 10%, #fbfdff 0%, #edf2f8 55%, #e6edf5 100%);
      overflow: auto;
    }
    .panel-card {
      background: #ffffff;
      border: 1px solid #dbe4ef;
      border-radius: 10px;
      padding: 12px;
      margin-bottom: 12px;
    }
    .panel-title {
      margin: 0 0 8px 0;
      font-size: 16px;
      color: #1e3550;
    }
    .hint {
      font-size: 13px;
      color: #50637b;
    }
    .rowline {
      display: flex;
      gap: 8px;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
    }
    .tag-search {
      width: 320px;
      max-width: 100%;
      border: 1px solid #c7d3e1;
      border-radius: 8px;
      padding: 7px 8px;
      font: inherit;
      font-size: 13px;
    }
    .tag-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
      margin-top: 8px;
      background: #fff;
      border: 1px solid #dce5ef;
      border-radius: 8px;
      overflow: hidden;
    }
    .tag-table th,
    .tag-table td {
      border-bottom: 1px solid #e7edf5;
      padding: 6px 8px;
      text-align: left;
      vertical-align: middle;
    }
    .tag-table th {
      font-size: 12px;
      color: #3f5874;
      background: #f6f9fd;
    }
    .tag-table tr:last-child td { border-bottom: 0; }
    .state-pill {
      display: inline-block;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 700;
      padding: 2px 8px;
      border: 1px solid transparent;
    }
    .state-live { background: #eaf8ef; color: #206a3f; border-color: #a6ddb7; }
    .state-recent { background: #eef6ff; color: #1d4f85; border-color: #bfd7f2; }
    .state-cached { background: #fff7ea; color: #7c5a1d; border-color: #f0d5a7; }
    .state-stale { background: #fdeeee; color: #8a2323; border-color: #efb2b2; }
    .state-opc { background: #e9f6ff; color: #1b5c7f; border-color: #b3d7ec; }
    .mini-btn {
      border: 1px solid #8fb3d8;
      background: #e9f2fc;
      color: #184a7d;
      border-radius: 6px;
      padding: 4px 8px;
      font-size: 12px;
      cursor: pointer;
    }
    .mini-input {
      width: 82px;
      border: 1px solid #c6d5e6;
      border-radius: 6px;
      padding: 3px 6px;
      font: inherit;
      font-size: 12px;
      background: #fff;
      color: #23405d;
    }
    .mono { font-family: Consolas, monospace; font-size: 12px; color: #4b6078; }
    .hidden { display: none; }
    .empty {
      padding: 10px;
      border: 1px dashed #c6d4e3;
      border-radius: 8px;
      color: #58708a;
      background: #f9fbfe;
    }
    .opc-list {
      max-height: 260px;
      overflow: auto;
      border: 1px solid #dce5ef;
      border-radius: 8px;
      background: #fff;
      margin-top: 8px;
    }
    .opc-row {
      display: grid;
      grid-template-columns: 1fr auto auto;
      gap: 8px;
      align-items: center;
      border-bottom: 1px solid #e7edf5;
      padding: 6px 8px;
    }
    .opc-row:last-child { border-bottom: 0; }
    .opc-name { font-size: 13px; color: #1f3854; }
    .opc-meta { font-size: 11px; color: #6b8199; }
    .log-box {
      flex: 1;
      min-height: 0;
      overflow: auto;
      border: 1px solid #dce5ef;
      border-radius: 8px;
      background: #f7fbff;
      padding: 8px;
      font-family: Consolas, monospace;
      font-size: 12px;
      color: #344f69;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      word-break: break-word;
      margin: 8px 10px 10px 10px;
    }
    .modal-backdrop {
      position: fixed;
      inset: 0;
      background: rgba(12, 22, 36, 0.45);
      display: flex;
      align-items: center;
      justify-content: center;
      z-index: 50;
    }
    .modal-backdrop.hidden { display: none; }
    .modal-card {
      width: min(520px, 92vw);
      background: #fff;
      border: 1px solid #dce5ef;
      border-radius: 12px;
      box-shadow: 0 18px 42px rgba(16, 29, 44, 0.24);
      padding: 14px;
    }
    .modal-title {
      margin: 0 0 10px 0;
      font-size: 20px;
      color: #1f3854;
      font-weight: 700;
    }
    .form-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }
    .form-row {
      display: flex;
      flex-direction: column;
      gap: 5px;
    }
    .form-row label {
      font-size: 12px;
      color: #4b6078;
      font-weight: 600;
    }
    .form-row.full { grid-column: 1 / -1; }
    .modal-actions {
      margin-top: 12px;
      display: flex;
      justify-content: flex-end;
      gap: 8px;
    }
    .dashboard-preview-wrap {
      border: 1px solid #dce5ef;
      border-radius: 8px;
      overflow: hidden;
      background: #fff;
      min-height: 420px;
    }
    .dashboard-preview-frame {
      width: 100%;
      height: 520px;
      border: 0;
      display: block;
      background: #fff;
    }
    @media (max-width: 900px) {
      .layout {
        display: block;
      }
      .left-pane {
        width: auto;
        min-width: 0;
        max-width: none;
      }
      .right-pane { display: none; }
    }
  </style>
</head>
<body>
  <div class="layout">
    <aside class="left-pane">
      <div class="pane-head">
        <h1 class="title">Grafana Tree Editor</h1>
        <div class="actions">
          <button class="secondary" onclick="loadTree()">Reload</button>
          <button onclick="saveTree()">Save</button>
        </div>
      </div>
      <div class="left-body">
        <div class="tree-wrap">
          <div id="treeRoot" class="wb-host wb-skeleton wb-initializing wb-fade-expander"></div>
        </div>
        <div class="debug-wrap">
          <div class="debug-head">
            <h2 class="debug-title">Debug Log</h2>
            <button class="mini-btn" onclick="refreshDebugEvents()">Refresh</button>
          </div>
          <div id="status" class="status"></div>
          <div id="debugEventsBox" class="log-box"></div>
        </div>
      </div>
    </aside>
    <main class="right-pane">
      <div id="tagEditorEmpty" class="panel-card">
        <h2 class="panel-title">View Editor</h2>
        <div class="empty">Click a <b>tags</b> or <b>dashboard</b> node to edit that part of the view.</div>
      </div>

      <div id="tagEditorPanel" class="hidden">
        <div class="panel-card">
          <h2 class="panel-title">Selected View</h2>
          <div class="rowline">
            <div>
              <div id="tagEditorViewName" style="font-weight:700;"></div>
              <div id="tagEditorViewId" class="mono"></div>
              <div id="historianRawStatus" class="mono">Historian raw logger: unknown</div>
            </div>
            <div class="actions">
              <button class="mini-btn" onclick="refreshTagCatalog()">Refresh Influx Tags</button>
              <button class="mini-btn" onclick="startRawLogging()">Start Raw Logging</button>
              <button class="mini-btn" onclick="stopRawLogging()">Stop Raw Logging</button>
              <button class="mini-btn" onclick="refreshAssignedOpcLive()">Refresh OPC Live</button>
            </div>
          </div>
        </div>

        <div class="panel-card">
          <h2 class="panel-title">Assigned Tags</h2>
          <table class="tag-table">
            <thead>
              <tr>
                <th>Label</th><th>NodeId</th><th>Value</th><th>State</th><th>Age</th><th>Source</th><th></th>
              </tr>
            </thead>
            <tbody id="assignedTagsBody"></tbody>
          </table>
        </div>

        <div class="panel-card">
          <div class="rowline">
            <h2 class="panel-title" style="margin:0;">Available Influx Tags</h2>
            <input id="tagSearchInput" class="tag-search" placeholder="Filter by label or nodeid" oninput="renderTagEditor()" />
          </div>
          <div id="catalogHint" class="hint" style="margin-top:6px;"></div>
          <table class="tag-table">
            <thead>
              <tr>
                <th>Label</th><th>NodeId</th><th>Value</th><th>State</th><th>Age</th><th>Save Mode</th><th>Deadband</th><th>Heartbeat(s)</th><th></th><th></th>
              </tr>
            </thead>
            <tbody id="catalogTagsBody"></tbody>
          </table>
        </div>

        <div class="panel-card">
          <div class="rowline">
            <h2 class="panel-title" style="margin:0;">Kepserver OPC UA Tags</h2>
            <div class="actions">
              <button class="mini-btn" onclick="opcBrowseRoot()">Root</button>
              <button class="mini-btn" onclick="opcBrowseParent()">Up</button>
              <button class="mini-btn" onclick="refreshOpcBrowse()">Refresh</button>
            </div>
          </div>
          <div class="mono">Current node: <span id="opcCurrentNode">root</span></div>
          <div id="opcList" class="opc-list"></div>
        </div>

      </div>

      <div id="dashboardEditorPanel" class="hidden">
        <div class="panel-card">
          <h2 class="panel-title">Dashboard Builder</h2>
          <div class="rowline">
            <div>
              <div id="dashboardEditorViewName" style="font-weight:700;"></div>
              <div id="dashboardEditorViewId" class="mono"></div>
              <div id="dashboardEditorFolderPath" class="mono"></div>
              <div id="dashboardEditorLastResult" class="mono"></div>
            </div>
            <div class="actions">
              <button class="mini-btn" onclick="generateDashboardFromTags()">Generate Dashboard</button>
              <button class="mini-btn" onclick="openGeneratedDashboard()">Open in Grafana</button>
            </div>
          </div>
        </div>
        <div class="panel-card">
          <div class="rowline">
            <h2 class="panel-title" style="margin:0;">Dashboard Preview</h2>
            <button class="mini-btn" onclick="refreshDashboardEmbed()">Refresh Preview</button>
          </div>
          <div id="dashboardEmbedHint" class="hint" style="margin:6px 0 8px 0;">
            Generate a dashboard first to preview it here.
          </div>
          <div class="dashboard-preview-wrap">
            <iframe id="dashboardEmbedFrame" class="dashboard-preview-frame" loading="lazy"></iframe>
          </div>
          <div id="dashboardPreviewUrl" class="mono" style="margin-top:8px; padding:8px; border:1px solid #dce5ef; border-radius:8px; background:#fff; word-break:break-all;">-</div>
        </div>
        <div class="panel-card">
          <h2 class="panel-title">Source Tags (from sibling tags node)</h2>
          <div id="dashboardSourceHint" class="hint" style="margin-bottom:6px;"></div>
          <table class="tag-table">
            <thead>
              <tr>
                <th>Label</th><th>NodeId</th><th>Live Value</th><th>State</th><th>Age</th>
              </tr>
            </thead>
            <tbody id="dashboardSourceTagsBody"></tbody>
          </table>
        </div>
      </div>
    </main>
  </div>
  <div id="treeContextMenu" class="ctx-menu">
    <button type="button" data-action="add-folder">Add Folder</button>
    <button type="button" data-action="add-view">Add View</button>
    <button type="button" data-action="rename">Rename</button>
    <button type="button" data-action="copy">Copy</button>
    <button type="button" data-action="paste">Paste</button>
    <button type="button" data-action="delete" class="danger">Delete</button>
  </div>
  <div id="policyModal" class="modal-backdrop hidden">
    <div class="modal-card">
      <h3 class="modal-title">Edit Save Policy</h3>
      <div id="policyModalNode" class="mono" style="margin-bottom:10px;"></div>
      <div class="form-grid">
        <div class="form-row full">
          <label for="policyModalMode">Save Mode</label>
          <select id="policyModalMode" class="mini-input">
            <option value="always">always</option>
            <option value="on_change">on_change</option>
          </select>
        </div>
        <div class="form-row">
          <label for="policyModalDeadband">Deadband</label>
          <input id="policyModalDeadband" class="mini-input" type="number" step="0.01" min="0" />
        </div>
        <div class="form-row">
          <label for="policyModalHeartbeat">Heartbeat (s)</label>
          <input id="policyModalHeartbeat" class="mini-input" type="number" step="1" min="1" />
        </div>
      </div>
      <div class="modal-actions">
        <button class="mini-btn" onclick="closePolicyModal()">Cancel</button>
        <button class="mini-btn" onclick="savePolicyModal()">Save</button>
      </div>
    </div>
  </div>

<script src="https://cdn.jsdelivr.net/npm/wunderbaum@0/dist/wunderbaum.umd.min.js"></script>
<script>
let treeConfig = { root_folder: "Historian", tree: { id: "root", type: "folder", name: "Plant", children: [] } };
let selectedNodeId = "root";
let wbTree = null;
let contextMenu = null;
let copiedNode = null;
let activeInlineEditor = null;
let influxTagCatalog = [];
let influxCatalogLoaded = false;
let influxCatalogLoading = false;
let influxCatalogLastFetchMs = 0;
let influxCatalogMeta = null;
let opcBrowserNodeId = "";
let opcBrowserItems = [];
let historianRawRunning = null;
let historianBackendConnected = null;
let assignedOpcLiveByNode = {};
let statusLines = [];
let historianSelectionByNode = {};
let policyModalNodeid = "";

async function callApi(path, options = {}) {
  const res = await fetch(path, options);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || JSON.stringify(data));
  return data;
}

function setStatus(value) {
  const line = `[${new Date().toLocaleTimeString()}] ${value}`;
  statusLines.push(line);
  if (statusLines.length > 120) statusLines = statusLines.slice(-120);
  const el = document.getElementById("status");
  if (el) {
    el.textContent = statusLines.join("\\n");
    el.scrollTop = el.scrollHeight;
  }
}

function renderHistorianRawStatus() {
  const el = document.getElementById("historianRawStatus");
  if (!el) return;
  if (historianBackendConnected === false) {
    el.textContent = "Historian raw logger: backend disconnected (start app/historian_ui.py)";
    return;
  }
  if (historianRawRunning === true) {
    el.textContent = "Historian raw logger: RUNNING";
    return;
  }
  if (historianRawRunning === false) {
    el.textContent = "Historian raw logger: STOPPED";
    return;
  }
  el.textContent = "Historian raw logger: unknown";
}

function formatAge(seconds) {
  if (seconds === null || seconds === undefined) return "-";
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

function stateClass(state) {
  if (state === "live") return "state-live";
  if (state === "recent") return "state-recent";
  if (state === "cached") return "state-cached";
  if (state === "opc-live") return "state-opc";
  return "state-stale";
}

function domKeyForNodeid(nodeid) {
  return btoa(unescape(encodeURIComponent(nodeid))).replace(/=/g, "_");
}

function makeId(prefix) {
  return `${prefix}-${Math.random().toString(36).slice(2, 8)}-${Date.now().toString(36)}`;
}

function ensureTreeShape() {
  if (!treeConfig || typeof treeConfig !== "object") {
    treeConfig = {};
  }
  if (!treeConfig.root_folder) {
    treeConfig.root_folder = "Historian";
  }
  if (!treeConfig.tree || treeConfig.tree.type !== "folder") {
    treeConfig.tree = { id: "root", type: "folder", name: "Plant", children: [] };
  }
  if (!Array.isArray(treeConfig.tree.children)) {
    treeConfig.tree.children = [];
  }
}

function findNodeById(targetId, node = treeConfig.tree, parent = null) {
  if (!node) return null;
  if (node.id === targetId) return { node, parent };
  if (Array.isArray(node.children)) {
    for (const child of node.children) {
      const found = findNodeById(targetId, child, node);
      if (found) return found;
    }
  }
  return null;
}

function selectedRecord() {
  return findNodeById(selectedNodeId);
}

function selectedTagEditorContext() {
  const rec = selectedRecord();
  if (!rec || rec.node.type !== "view_tags" || !rec.parent || rec.parent.type !== "view") return null;
  return { tagsNode: rec.node, viewNode: rec.parent };
}

function selectedDashboardEditorContext() {
  const rec = selectedRecord();
  if (!rec || rec.node.type !== "view_dashboard" || !rec.parent || rec.parent.type !== "view") return null;
  return { dashboardNode: rec.node, viewNode: rec.parent };
}

function pathToNode(targetId, node = treeConfig.tree, trail = []) {
  if (!node) return null;
  const nextTrail = trail.concat([node]);
  if (node.id === targetId) return nextTrail;
  if (!Array.isArray(node.children)) return null;
  for (const child of node.children) {
    const found = pathToNode(targetId, child, nextTrail);
    if (found) return found;
  }
  return null;
}

function deepClone(obj) {
  return JSON.parse(JSON.stringify(obj));
}

function cloneWithNewIds(node) {
  const cloned = deepClone(node);
  function walk(curr) {
    const prefix = curr.type === "view" ? "view" : (curr.type === "folder" ? "folder" : "node");
    curr.id = makeId(prefix);
    if (Array.isArray(curr.children)) {
      for (const child of curr.children) {
        walk(child);
      }
    }
  }
  walk(cloned);
  return cloned;
}

function toWunderNode(node) {
  const title = node.name || (node.type === "view" ? "Untitled view" : "Folder");
  let icon = "bi bi-folder2";
  if (node.type === "view") icon = "bi bi-layout-text-window-reverse";
  if (node.type === "view_tags") icon = "bi bi-tags";
  if (node.type === "view_dashboard") icon = "bi bi-speedometer2";
  const mapped = {
    key: node.id,
    title,
    type: node.type,
    expanded: true,
    icon,
  };
  if (Array.isArray(node.children) && node.children.length) {
    mapped.children = node.children.map(toWunderNode);
  }
  return mapped;
}

function renderFallbackTree() {
  const host = document.getElementById("treeRoot");
  if (!host) return;
  host.innerHTML = "";
  const build = (node) => {
    const wrap = document.createElement("div");
    wrap.style.paddingLeft = "14px";
    const row = document.createElement("div");
    row.style.padding = "3px 0";
    row.style.cursor = "pointer";
    row.textContent = `${node.name || node.id} [${node.type}]`;
    row.onclick = () => {
      selectedNodeId = node.id;
      renderTagEditor();
    };
    wrap.appendChild(row);
    if (Array.isArray(node.children)) {
      for (const child of node.children) {
        wrap.appendChild(build(child));
      }
    }
    return wrap;
  };
  host.appendChild(build(treeConfig.tree));
}

function renderTree() {
  ensureTreeShape();
  const rootNode = toWunderNode(treeConfig.tree);
  if (wbTree) {
    wbTree.destroy();
    wbTree = null;
  }
  try {
    if (!window.mar10 || !mar10.Wunderbaum) {
      throw new Error("Wunderbaum library not loaded");
    }
    wbTree = new mar10.Wunderbaum({
      element: document.getElementById("treeRoot"),
      source: [rootNode],
      init: (e) => {
        const active = e.tree.findKey(selectedNodeId) || e.tree.findKey(treeConfig.tree.id);
        if (active) {
          active.setActive();
        }
      },
      activate: (e) => {
        selectedNodeId = e.node.key;
        renderTagEditor();
        if (e.node.type === "view_tags") {
          refreshAssignedOpcLive();
        }
      },
    });
  } catch (err) {
    setStatus(`Tree render error: ${err.message}`);
    renderFallbackTree();
  }
  renderTagEditor();
}

function hideContextMenu() {
  if (contextMenu) {
    contextMenu.style.display = "none";
  }
}

function showContextMenu(x, y) {
  if (!contextMenu) return;
  const maxX = window.innerWidth - 190;
  const maxY = window.innerHeight - 180;
  contextMenu.style.left = `${Math.max(6, Math.min(x, maxX))}px`;
  contextMenu.style.top = `${Math.max(6, Math.min(y, maxY))}px`;
  contextMenu.style.display = "block";
}

function cancelInlineRename() {
  if (!activeInlineEditor) return;
  const { parent, originalTitle, input } = activeInlineEditor;
  if (input && input.parentElement) {
    input.remove();
  }
  if (parent) {
    parent.textContent = originalTitle;
  }
  activeInlineEditor = null;
}

function startInlineRename() {
  const rec = selectedRecord();
  if (!rec) return;
  cancelInlineRename();

  const titleEl = document.querySelector(".wb-row.wb-active .wb-title");
  if (!titleEl || !titleEl.parentElement) {
    renameNode();
    return;
  }

  const currentName = rec.node.name || "";
  const wrapper = titleEl.parentElement;
  titleEl.textContent = "";

  const input = document.createElement("input");
  input.type = "text";
  input.value = currentName;
  input.style.width = "100%";
  input.style.boxSizing = "border-box";
  input.style.border = "1px solid #7ea6d3";
  input.style.borderRadius = "4px";
  input.style.padding = "2px 6px";
  input.style.fontSize = "13px";
  input.style.fontFamily = "inherit";
  input.style.background = "#ffffff";

  const commit = () => {
    if (!activeInlineEditor) return;
    const next = input.value.trim();
    rec.node.name = next || rec.node.name || "Untitled";
    activeInlineEditor = null;
    renderTree();
  };

  const cancel = () => {
    cancelInlineRename();
  };

  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      commit();
    } else if (event.key === "Escape") {
      event.preventDefault();
      cancel();
    }
  });
  input.addEventListener("blur", () => commit());

  wrapper.appendChild(input);
  input.focus();
  input.select();
  activeInlineEditor = { parent: titleEl, originalTitle: currentName, input };
}

function addFolder() {
  const rec = selectedRecord() || { node: treeConfig.tree, parent: null };
  let target = rec.node;
  if (target.type !== "folder") {
    target = rec.parent || treeConfig.tree;
  }
  if (!Array.isArray(target.children)) target.children = [];
  const node = { id: makeId("folder"), type: "folder", name: "New Folder", children: [] };
  target.children.push(node);
  selectedNodeId = node.id;
  renderTree();
  renderTagEditor();
}

function addView() {
  const rec = selectedRecord() || { node: treeConfig.tree, parent: null };
  let target = rec.node;
  if (target.type !== "folder") {
    target = rec.parent || treeConfig.tree;
  }
  if (!Array.isArray(target.children)) target.children = [];
  const viewId = makeId("view");
  const node = {
    id: viewId,
    type: "view",
    name: "New View",
    template_id: "",
    tags: [],
    process_tags: [],
    variable_values: {},
    children: [
      { id: `${viewId}-tags`, type: "view_tags", name: "tags" },
      { id: `${viewId}-dashboard`, type: "view_dashboard", name: "dashboard" },
    ],
  };
  target.children.push(node);
  selectedNodeId = node.id;
  renderTree();
  renderTagEditor();
}

function renameNode() {
  const rec = selectedRecord();
  if (rec && (rec.node.type === "view_tags" || rec.node.type === "view_dashboard")) {
    setStatus("Tags and dashboard are locked parts of a view and cannot be renamed.");
    return;
  }
  startInlineRename();
}

function deleteNode() {
  const rec = selectedRecord();
  if (!rec || !rec.parent) return;
  if (rec.node.type === "view_tags" || rec.node.type === "view_dashboard") {
    setStatus("Tags and dashboard are locked parts of a view and cannot be deleted separately.");
    return;
  }
  if (!confirm(`Delete '${rec.node.name}'?`)) return;
  rec.parent.children = (rec.parent.children || []).filter(x => x.id !== rec.node.id);
  selectedNodeId = rec.parent.id || "root";
  renderTree();
  renderTagEditor();
}

function copyNode() {
  const rec = selectedRecord();
  if (!rec || rec.node.id === treeConfig.tree.id) return;
  if (rec.node.type === "view_tags" || rec.node.type === "view_dashboard") {
    setStatus("Copy the parent view folder instead of tags/dashboard child.");
    return;
  }
  copiedNode = deepClone(rec.node);
  setStatus(`Copied: ${rec.node.name}`);
}

function pasteNode() {
  if (!copiedNode) {
    setStatus("Clipboard is empty.");
    return;
  }
  const rec = selectedRecord() || { node: treeConfig.tree, parent: null };
  let target = rec.node;
  if (target.type !== "folder") {
    target = rec.parent || treeConfig.tree;
  }
  if (!Array.isArray(target.children)) target.children = [];
  const cloned = cloneWithNewIds(copiedNode);
  target.children.push(cloned);
  selectedNodeId = cloned.id;
  renderTree();
  renderTagEditor();
  setStatus(`Pasted: ${cloned.name}`);
}

async function refreshTagCatalog(force = false, silent = false) {
  const now = Date.now();
  if (influxCatalogLoading) return;
  if (!force && influxCatalogLoaded && (now - influxCatalogLastFetchMs) < 3000) return;
  influxCatalogLoading = true;
  try {
    const data = await callApi("/api/influx/tags/catalog");
    influxTagCatalog = Array.isArray(data.items) ? data.items : [];
    influxCatalogMeta = data.meta || null;
    influxCatalogLoaded = true;
    influxCatalogLastFetchMs = Date.now();
    if (!silent && influxCatalogMeta) {
      setStatus(`Influx catalog loaded: total=${influxCatalogMeta.total}, live=${influxCatalogMeta.counts?.live || 0}, stale=${influxCatalogMeta.counts?.stale || 0}, query=${influxCatalogMeta.query_ms}ms`);
    } else if (!silent) {
      setStatus(`Influx catalog loaded: total=${influxTagCatalog.length}`);
    }
    renderTagEditor();
  } catch (err) {
    if (!silent) {
      setStatus(`Tag catalog refresh failed: ${err.message}`);
    }
  } finally {
    influxCatalogLoading = false;
  }
}

function ensureViewProcessTags(viewNode) {
  if (!Array.isArray(viewNode.process_tags)) viewNode.process_tags = [];
  viewNode.process_tags = viewNode.process_tags
    .map((item) => {
      if (typeof item === "string") {
        const nodeid = item.trim();
        return nodeid ? { nodeid, label: nodeid } : null;
      }
      if (!item || typeof item !== "object") return null;
      const nodeid = String(item.nodeid || "").trim();
      const label = String(item.label || "").trim();
      if (!nodeid) return null;
      return { nodeid, label: label || nodeid };
    })
    .filter(Boolean);
}

function removeProcessTag(nodeid) {
  const ctx = selectedTagEditorContext();
  if (!ctx) return;
  ensureViewProcessTags(ctx.viewNode);
  ctx.viewNode.process_tags = ctx.viewNode.process_tags.filter((tag) => tag.nodeid !== nodeid);
  renderTagEditor();
}

function addProcessTag(nodeid, label) {
  const ctx = selectedTagEditorContext();
  if (!ctx) return;
  ensureViewProcessTags(ctx.viewNode);
  if (ctx.viewNode.process_tags.some((tag) => tag.nodeid === nodeid)) return;
  ctx.viewNode.process_tags.push({ nodeid, label: label || nodeid });
  renderTagEditor();
}

async function refreshAssignedOpcLive(silent = false) {
  const ctx = selectedTagEditorContext();
  if (!ctx) return;
  ensureViewProcessTags(ctx.viewNode);
  const nodeids = ctx.viewNode.process_tags.map((tag) => tag.nodeid).filter(Boolean);
  if (!nodeids.length) {
    assignedOpcLiveByNode = {};
    renderTagEditor();
    return;
  }
  try {
    const data = await callApi("/api/opc/read_many", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ nodeids }),
    });
    const map = {};
    for (const item of (data.items || [])) {
      map[item.nodeid] = item;
    }
    assignedOpcLiveByNode = map;
    const okCount = Object.values(map).filter((item) => item && item.ok).length;
    if (!silent) {
      setStatus(`Assigned OPC live refresh: ${okCount}/${nodeids.length} readable`);
    }
    renderTagEditor();
  } catch (err) {
    if (!silent) {
      setStatus(`Assigned OPC live refresh failed: ${err.message}`);
    }
  }
}

async function refreshDebugEvents() {
  try {
    const data = await callApi("/api/debug/events?limit=140");
    const box = document.getElementById("debugEventsBox");
    if (!box) return;
    const lines = [];
    for (const event of (data.events || [])) {
      lines.push(`${event.ts} [${event.level}] ${event.message} ${JSON.stringify(event.details || {})}`);
    }
    box.textContent = lines.join("\\n") || "No events yet.";
    box.scrollTop = box.scrollHeight;
  } catch (err) {
    setStatus(`Debug events load failed: ${err.message}`);
  }
}

function renderTagEditor() {
  const tagCtx = selectedTagEditorContext();
  const dashboardCtx = selectedDashboardEditorContext();
  const empty = document.getElementById("tagEditorEmpty");
  const tagPanel = document.getElementById("tagEditorPanel");
  const dashboardPanel = document.getElementById("dashboardEditorPanel");
  if (!empty || !tagPanel || !dashboardPanel) return;
  if (!tagCtx && !dashboardCtx) {
    empty.classList.remove("hidden");
    tagPanel.classList.add("hidden");
    dashboardPanel.classList.add("hidden");
    return;
  }

  empty.classList.add("hidden");
  if (dashboardCtx) {
    tagPanel.classList.add("hidden");
    dashboardPanel.classList.remove("hidden");
    renderDashboardEditor(dashboardCtx);
    return;
  }
  tagPanel.classList.remove("hidden");
  dashboardPanel.classList.add("hidden");
  if (!influxCatalogLoaded && !influxCatalogLoading) {
    refreshTagCatalog();
  }

  const viewNode = tagCtx.viewNode;
  ensureViewProcessTags(viewNode);
  const processTags = viewNode.process_tags;
  const byNode = new Map(influxTagCatalog.map((item) => [item.nodeid, item]));
  const search = (document.getElementById("tagSearchInput").value || "").trim().toLowerCase();

  document.getElementById("tagEditorViewName").textContent = viewNode.name || "Unnamed view";
  document.getElementById("tagEditorViewId").textContent = `view_id: ${viewNode.id}`;
  renderHistorianRawStatus();

  const assignedBody = document.getElementById("assignedTagsBody");
  assignedBody.innerHTML = "";
  for (const tag of processTags) {
    const live = byNode.get(tag.nodeid);
    const opc = assignedOpcLiveByNode[tag.nodeid];
    let value = live && live.value !== null && live.value !== undefined ? live.value : "-";
    let state = live ? live.state : "stale";
    let age = live ? formatAge(live.age_seconds) : "-";
    let source = live ? "influx" : "missing";
    if (!live && opc && opc.ok) {
      value = opc.value === null || opc.value === undefined ? "-" : opc.value;
      state = "opc-live";
      age = "0s";
      source = "opc-live";
    }
    const reason = live ? "" : (opc && opc.ok ? "not in influx yet" : "no influx point");
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${tag.label || tag.nodeid}</td>
      <td class="mono">${tag.nodeid}</td>
      <td>${value}</td>
      <td><span class="state-pill ${stateClass(state)}">${state}</span></td>
      <td>${age}</td>
      <td class="mono">${source}${reason ? ` (${reason})` : ""}</td>
      <td><button class="mini-btn">Remove</button></td>
    `;
    tr.querySelector("button").onclick = () => removeProcessTag(tag.nodeid);
    assignedBody.appendChild(tr);
  }
  if (!processTags.length) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="7" class="hint">No tags assigned yet.</td>`;
    assignedBody.appendChild(tr);
  }

  const assignedSet = new Set(processTags.map((tag) => tag.nodeid));
  const catalogBody = document.getElementById("catalogTagsBody");
  const catalogHint = document.getElementById("catalogHint");
  if (catalogHint) {
    if (influxCatalogLoading) {
      catalogHint.textContent = "Loading from InfluxDB...";
    } else if (!influxTagCatalog.length) {
      catalogHint.textContent = "No tags in InfluxDB yet. Add tags from OPC UA below and start logging.";
    } else if (influxCatalogMeta) {
      catalogHint.textContent = `total=${influxCatalogMeta.total}, live=${influxCatalogMeta.counts?.live || 0}, recent=${influxCatalogMeta.counts?.recent || 0}, cached=${influxCatalogMeta.counts?.cached || 0}, stale=${influxCatalogMeta.counts?.stale || 0}, query=${influxCatalogMeta.query_ms}ms`;
    } else {
      catalogHint.textContent = `${influxTagCatalog.length} tags loaded from InfluxDB.`;
    }
  }
  catalogBody.innerHTML = "";
  for (const item of influxTagCatalog) {
    if (search) {
      const hay = `${item.label || ""} ${item.nodeid || ""}`.toLowerCase();
      if (!hay.includes(search)) continue;
    }
    const already = assignedSet.has(item.nodeid);
    const policy = historianSelectionByNode[item.nodeid] || {
      save_mode: "always",
      deadband: 0,
      heartbeat_s: 30,
    };
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${item.label || item.nodeid}</td>
      <td class="mono">${item.nodeid}</td>
      <td>${item.value === null || item.value === undefined ? "-" : item.value}</td>
      <td><span class="state-pill ${stateClass(item.state)}">${item.state}</span></td>
      <td>${formatAge(item.age_seconds)}</td>
      <td>${policy.save_mode}</td>
      <td>${Number(policy.deadband || 0)}</td>
      <td>${Number(policy.heartbeat_s || 30)}</td>
      <td><button class="mini-btn edit-policy-btn">Edit</button></td>
      <td><button class="mini-btn add-tag-btn">${already ? "Added" : "Add"}</button></td>
    `;
    tr.querySelector("button.edit-policy-btn").onclick = () => openPolicyModal(item.nodeid, item.label || item.nodeid);
    const addBtn = tr.querySelector("button.add-tag-btn");
    addBtn.disabled = already;
    addBtn.onclick = () => addProcessTag(item.nodeid, item.label || item.nodeid);
    catalogBody.appendChild(tr);
  }
  renderOpcList();
}

function renderDashboardEditor(ctx) {
  const viewNode = ctx.viewNode;
  ensureViewProcessTags(viewNode);
  const processTags = viewNode.process_tags;
  const byNode = new Map(influxTagCatalog.map((item) => [item.nodeid, item]));
  const folderPathNodes = pathToNode(viewNode.id) || [];
  const folderPath = folderPathNodes
    .filter((node) => node.type === "folder")
    .map((node) => node.name || "")
    .filter(Boolean);

  document.getElementById("dashboardEditorViewName").textContent = viewNode.name || "Unnamed view";
  document.getElementById("dashboardEditorViewId").textContent = `view_id: ${viewNode.id}`;
  document.getElementById("dashboardEditorFolderPath").textContent = `folder path: ${folderPath.join(" / ") || "(root)"}`;
  if (viewNode.generated_dashboard_uid) {
    document.getElementById("dashboardEditorLastResult").textContent =
      `last generated uid: ${viewNode.generated_dashboard_uid}`;
  } else {
    document.getElementById("dashboardEditorLastResult").textContent = "last generated uid: -";
  }

  const hint = document.getElementById("dashboardSourceHint");
  if (hint) {
    hint.textContent = `${processTags.length} tags will create ${processTags.length} time-series panels.`;
  }

  const body = document.getElementById("dashboardSourceTagsBody");
  body.innerHTML = "";
  for (const tag of processTags) {
    const live = byNode.get(tag.nodeid);
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${tag.label || tag.nodeid}</td>
      <td class="mono">${tag.nodeid}</td>
      <td>${live && live.value !== null && live.value !== undefined ? live.value : "-"}</td>
      <td><span class="state-pill ${stateClass(live ? live.state : "stale")}">${live ? live.state : "stale"}</span></td>
      <td>${live ? formatAge(live.age_seconds) : "-"}</td>
    `;
    body.appendChild(tr);
  }
  if (!processTags.length) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="5" class="hint">No tags selected in the sibling tags node.</td>`;
    body.appendChild(tr);
  }
  refreshDashboardEmbed();
}

function refreshDashboardEmbed() {
  const ctx = selectedDashboardEditorContext();
  const frame = document.getElementById("dashboardEmbedFrame");
  const hint = document.getElementById("dashboardEmbedHint");
  const urlBox = document.getElementById("dashboardPreviewUrl");
  if (!hint || !urlBox || !frame) return;
  if (!ctx) {
    hint.textContent = "Select a dashboard node to preview.";
    urlBox.textContent = "-";
    frame.removeAttribute("src");
    return;
  }
  const uid = String(ctx.viewNode.generated_dashboard_uid || "").trim();
  let directUrl = String(ctx.viewNode.generated_dashboard_direct_url || "").trim();
  if (!directUrl && uid) {
    directUrl = `/api/grafana/open-url?uid=${encodeURIComponent(uid)}`;
    ctx.viewNode.generated_dashboard_direct_url = directUrl;
  }
  if (!directUrl || !uid) {
    hint.textContent = "Generate a dashboard first to preview it here.";
    urlBox.textContent = "-";
    frame.removeAttribute("src");
    return;
  }
  const embedSrc = `/api/grafana/embed-url?uid=${encodeURIComponent(uid)}`;
  if (frame.src !== new URL(embedSrc, window.location.origin).toString()) {
    frame.src = embedSrc;
  }
  hint.textContent = "Embedded dashboard preview:";
  urlBox.textContent = directUrl;
}

async function refreshRawLoggingStatus() {
  try {
    const data = await callApi("/api/historian/raw/status");
    historianBackendConnected = !!data.backend_connected;
    historianRawRunning = !!data.running;
    renderHistorianRawStatus();
  } catch (err) {
    historianBackendConnected = false;
    historianRawRunning = null;
    renderHistorianRawStatus();
  }
}

async function refreshHistorianSelectionSettings() {
  try {
    const data = await callApi("/api/historian/selection");
    const tags = Array.isArray(data.tags) ? data.tags : [];
    const map = {};
    for (const tag of tags) {
      const nodeid = String(tag.nodeid || "").trim();
      if (!nodeid) continue;
      map[nodeid] = {
        save_mode: String(tag.save_mode || "always"),
        deadband: Number(tag.deadband ?? 0),
        heartbeat_s: Number(tag.heartbeat_s ?? 30),
      };
    }
    historianSelectionByNode = map;
    renderTagEditor();
  } catch (err) {
    setStatus(`Historian selection settings load failed: ${err.message}`);
  }
}

function openPolicyModal(nodeid, label) {
  const modal = document.getElementById("policyModal");
  const nodeLabel = document.getElementById("policyModalNode");
  const modeEl = document.getElementById("policyModalMode");
  const deadbandEl = document.getElementById("policyModalDeadband");
  const heartbeatEl = document.getElementById("policyModalHeartbeat");
  if (!modal || !nodeLabel || !modeEl || !deadbandEl || !heartbeatEl) return;
  const policy = historianSelectionByNode[nodeid] || {
    save_mode: "always",
    deadband: 0,
    heartbeat_s: 30,
  };
  policyModalNodeid = nodeid;
  nodeLabel.textContent = `${label || nodeid} | ${nodeid}`;
  modeEl.value = policy.save_mode === "on_change" ? "on_change" : "always";
  deadbandEl.value = String(Number(policy.deadband || 0));
  heartbeatEl.value = String(Number(policy.heartbeat_s || 30));
  modal.classList.remove("hidden");
}

function closePolicyModal() {
  const modal = document.getElementById("policyModal");
  if (modal) modal.classList.add("hidden");
  policyModalNodeid = "";
}

async function savePolicyModal() {
  const nodeid = policyModalNodeid;
  if (!nodeid) return;
  const modeEl = document.getElementById("policyModalMode");
  const deadbandEl = document.getElementById("policyModalDeadband");
  const heartbeatEl = document.getElementById("policyModalHeartbeat");
  if (!modeEl || !deadbandEl || !heartbeatEl) return;
  const save_mode = (modeEl.value || "always").trim();
  const deadband = Number(deadbandEl.value || 0);
  const heartbeat_s = Number(heartbeatEl.value || 30);
  if (!Number.isFinite(deadband) || deadband < 0) {
    setStatus("Policy save failed: deadband must be a number >= 0.");
    return;
  }
  if (!Number.isFinite(heartbeat_s) || heartbeat_s < 1) {
    setStatus("Policy save failed: heartbeat must be a number >= 1.");
    return;
  }
  await updateTagSavePolicy(nodeid, { save_mode, deadband, heartbeat_s });
}

async function updateTagSavePolicy(nodeid, policy) {
  const payload = {
    nodeid,
    save_mode: String(policy.save_mode || "always").trim(),
    deadband: Number(policy.deadband || 0),
    heartbeat_s: Number(policy.heartbeat_s || 30),
  };
  try {
    const data = await callApi("/api/historian/selection/policy", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
    setStatus(data.message || `Policy saved for ${nodeid}`);
    await refreshHistorianSelectionSettings();
    closePolicyModal();
  } catch (err) {
    if ((err.message || "").includes("nodeid not found in historian selection")) {
      setStatus(`Policy save failed for ${nodeid}: add this tag to historian logging first (OPC Add).`);
    } else {
      setStatus(`Policy save failed for ${nodeid}: ${err.message}`);
    }
  }
}

async function startRawLogging() {
  try {
    const data = await callApi("/api/historian/raw/start", { method: "POST" });
    setStatus(data.message || "Raw logging start requested.");
    await refreshRawLoggingStatus();
    influxCatalogLoaded = false;
    await refreshTagCatalog(true, false);
  } catch (err) {
    setStatus(`Start raw logging failed: ${err.message}`);
    await refreshRawLoggingStatus();
  }
}

async function stopRawLogging() {
  try {
    const data = await callApi("/api/historian/raw/stop", { method: "POST" });
    setStatus(data.message || "Raw logging stop requested.");
    await refreshRawLoggingStatus();
  } catch (err) {
    setStatus(`Stop raw logging failed: ${err.message}`);
    await refreshRawLoggingStatus();
  }
}

async function generateDashboardFromTags() {
  const ctx = selectedDashboardEditorContext();
  if (!ctx) {
    setStatus("Select a dashboard node first.");
    return;
  }
  ensureViewProcessTags(ctx.viewNode);
  if (!ctx.viewNode.process_tags.length) {
    setStatus("Cannot generate dashboard: no tags selected in sibling tags node.");
    return;
  }
  const pathNodes = pathToNode(ctx.viewNode.id) || [];
  const folderPath = pathNodes
    .filter((node) => node.type === "folder")
    .map((node) => node.name || "")
    .filter(Boolean);
  try {
    const data = await callApi("/api/view/dashboard/generate", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        title: ctx.viewNode.name || "Untitled view",
        view_id: ctx.viewNode.id,
        folder_path: folderPath,
        tags: Array.isArray(ctx.viewNode.tags) ? ctx.viewNode.tags : [],
        process_tags: ctx.viewNode.process_tags,
      }),
    });
    ctx.viewNode.generated_dashboard_uid = data.uid || "";
    ctx.viewNode.generated_dashboard_url = data.url || "";
    ctx.viewNode.generated_dashboard_direct_url = data.direct_url || "";
    ctx.viewNode.generated_at = new Date().toISOString();
    setStatus(
      `Dashboard generated: uid=${data.uid || "-"}, panels=${data.panel_count || 0}${data.url ? `, url=${data.url}` : ""}`
    );
    renderTagEditor();
    refreshDashboardEmbed();
    await saveTreeSilent("dashboard metadata");
  } catch (err) {
    setStatus(`Dashboard generation failed: ${err.message}`);
  }
}

function openGeneratedDashboard() {
  const ctx = selectedDashboardEditorContext();
  if (!ctx) {
    setStatus("Select a dashboard node first.");
    return;
  }
  const directUrl = String(ctx.viewNode.generated_dashboard_direct_url || "").trim();
  if (!directUrl) {
    setStatus("No generated dashboard UID yet. Click Generate Dashboard first.");
    return;
  }
  window.open(directUrl, "_blank", "noopener,noreferrer");
}

function renderOpcList() {
  const list = document.getElementById("opcList");
  const current = document.getElementById("opcCurrentNode");
  if (!list || !current) return;
  current.textContent = opcBrowserNodeId || "root";
  list.innerHTML = "";
  if (!opcBrowserItems.length) {
    list.innerHTML = `<div class="empty" style="margin:8px;">No OPC children loaded.</div>`;
    return;
  }
  for (const item of opcBrowserItems) {
    const row = document.createElement("div");
    row.className = "opc-row";
    row.innerHTML = `
      <div>
        <div class="opc-name">${item.browse_name || item.nodeid}</div>
        <div class="opc-meta">${item.node_class || ""} | ${item.nodeid}</div>
      </div>
      <button class="mini-btn">Open</button>
      <button class="mini-btn">Add</button>
    `;
    const openBtn = row.querySelectorAll("button")[0];
    const addBtn = row.querySelectorAll("button")[1];
    openBtn.onclick = async () => {
      opcBrowserNodeId = item.nodeid || "";
      await refreshOpcBrowse();
    };
    addBtn.onclick = async () => {
      try {
        const result = await callApi("/api/opc/selection/add", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ nodeid: item.nodeid, label: item.browse_name || item.nodeid }),
        });
        addProcessTag(item.nodeid, item.browse_name || item.nodeid);
        if (result.backend_connected) {
          setStatus(`Added OPC tag to Historian backend: ${item.browse_name || item.nodeid}`);
        } else {
          setStatus(`Added OPC tag locally (backend disconnected): ${item.browse_name || item.nodeid}`);
        }
      } catch (err) {
        setStatus(`Add OPC tag failed: ${err.message}`);
      }
    };
    list.appendChild(row);
  }
}

async function refreshOpcBrowse() {
  try {
    const data = await callApi(`/api/opc/browse?nodeid=${encodeURIComponent(opcBrowserNodeId || "")}`);
    opcBrowserItems = Array.isArray(data.children) ? data.children : [];
    renderOpcList();
  } catch (err) {
    setStatus(`OPC browse failed: ${err.message}`);
  }
}

function opcBrowseRoot() {
  opcBrowserNodeId = "";
  refreshOpcBrowse();
}

function opcBrowseParent() {
  if (!opcBrowserNodeId) return;
  const parts = opcBrowserNodeId.split(";");
  if (parts.length >= 2 && parts[1].includes(".")) {
    parts[1] = parts[1].split(".").slice(0, -1).join(".");
    opcBrowserNodeId = parts[1] ? parts.join(";") : "";
  } else {
    opcBrowserNodeId = "";
  }
  refreshOpcBrowse();
}

function bindContextMenu() {
  const host = document.getElementById("treeRoot");
  const pane = document.querySelector(".left-pane");
  contextMenu = document.getElementById("treeContextMenu");
  if (!host || !contextMenu || !pane) return;

  const openMenuFromEvent = (event) => {
    event.preventDefault();
    event.stopPropagation();
    if (contextMenu.contains(event.target)) {
      showContextMenu(event.clientX, event.clientY);
      return;
    }
    const info = mar10.Wunderbaum.getEventInfo(event);
    if (info && info.node) {
      selectedNodeId = info.node.key;
      info.node.setActive();
    }
    showContextMenu(event.clientX, event.clientY);
  };

  // Intercept right-click anywhere inside the left pane.
  document.addEventListener(
    "contextmenu",
    (event) => {
      if (!pane.contains(event.target)) return;
      openMenuFromEvent(event);
    },
    true
  );

  contextMenu.addEventListener("click", (event) => {
    const action = event.target && event.target.dataset ? event.target.dataset.action : "";
    if (!action) return;
    if (action === "add-folder") addFolder();
    if (action === "add-view") addView();
    if (action === "rename") renameNode();
    if (action === "copy") copyNode();
    if (action === "paste") pasteNode();
    if (action === "delete") deleteNode();
    hideContextMenu();
  });

  document.addEventListener("click", () => hideContextMenu());
  window.addEventListener("resize", () => hideContextMenu());
  window.addEventListener("scroll", () => hideContextMenu(), true);

  document.addEventListener("keydown", (event) => {
    const isCopy = (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "c";
    const isPaste = (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "v";
    const inEditable = ["INPUT", "TEXTAREA"].includes((event.target && event.target.tagName) || "");
    if (inEditable) return;
    if (isCopy) {
      event.preventDefault();
      copyNode();
      return;
    }
    if (isPaste) {
      event.preventDefault();
      pasteNode();
    }
  });
}

async function loadTree() {
  const data = await callApi("/api/tree");
  treeConfig = data.tree;
  selectedNodeId = treeConfig?.tree?.id || "root";
  renderTree();
  setStatus("Tree loaded.");
}

async function saveTree() {
  const data = await callApi("/api/tree", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ tree: treeConfig })
  });
  setStatus(JSON.stringify(data, null, 2));
}

async function saveTreeSilent(reason = "") {
  try {
    await callApi("/api/tree", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ tree: treeConfig }),
    });
    if (reason) {
      setStatus(`Tree auto-saved (${reason}).`);
    }
  } catch (err) {
    setStatus(`Tree auto-save failed: ${err.message}`);
  }
}

async function initPage() {
  bindContextMenu();
  const policyModal = document.getElementById("policyModal");
  if (policyModal) {
    policyModal.addEventListener("click", (event) => {
      if (event.target === policyModal) {
        closePolicyModal();
      }
    });
  }
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closePolicyModal();
  });
  await loadTree();
  await refreshTagCatalog(true, false);
  await refreshOpcBrowse();
  await refreshHistorianSelectionSettings();
  await refreshRawLoggingStatus();
  await refreshDebugEvents();
  setInterval(refreshRawLoggingStatus, 5000);
  setInterval(() => {
    if (selectedTagEditorContext() || selectedDashboardEditorContext()) {
      refreshAssignedOpcLive(true);
      refreshTagCatalog(true, true);
      refreshHistorianSelectionSettings();
    }
  }, 3000);
  setInterval(refreshDebugEvents, 5000);
}

initPage().catch(err => {
  setStatus(err.message);
});
</script>
</body>
</html>
"""


@app.get("/api/templates")
def api_templates():
    return {"templates": list_templates()}


@app.post("/api/template/capture")
def api_capture_template():
    payload = request.get_json(silent=True) or {}
    template_id = safe_template_id(str(payload.get("template_id", "")).strip())
    dashboard_uid = str(payload.get("dashboard_uid", "")).strip()
    if not dashboard_uid:
        return {"error": "dashboard_uid is required"}, 400

    response = grafana_request("GET", f"/api/dashboards/uid/{dashboard_uid}")
    if response.status_code != 200:
        return {
            "error": f"Grafana dashboard fetch failed: {response.status_code}",
            "details": response.text,
        }, 400

    body = response.json()
    dashboard = body.get("dashboard", {})
    if not dashboard:
        return {"error": "No dashboard JSON returned by Grafana"}, 400

    templating_list = dashboard.get("templating", {}).get("list", [])
    variables = [
        str(var.get("name", "")).strip()
        for var in templating_list
        if str(var.get("name", "")).strip()
    ]

    template_payload = {
        "template_id": template_id,
        "title": dashboard.get("title", template_id),
        "source_dashboard_uid": dashboard_uid,
        "captured_at": now_iso_utc(),
        "variables": variables,
        "dashboard": dashboard,
    }
    ensure_dir(TEMPLATE_DIR)
    save_json(template_file(template_id), template_payload)
    return {
        "saved": True,
        "template_id": template_id,
        "variables": variables,
        "path": str(template_file(template_id)),
    }


@app.get("/api/tree")
def api_tree_get():
    tree = normalize_tree_config(load_json(TREE_CONFIG_FILE, sample_tree()))
    return {"tree": tree}


@app.get("/api/tree/sample")
def api_tree_sample():
    return {"tree": normalize_tree_config(sample_tree())}


@app.post("/api/tree")
def api_tree_save():
    payload = request.get_json(silent=True) or {}
    tree = payload.get("tree")
    if not isinstance(tree, dict):
        return {"error": "tree object is required"}, 400
    tree = normalize_tree_config(tree)
    save_json(TREE_CONFIG_FILE, tree)
    return {"saved": True, "path": str(TREE_CONFIG_FILE)}


@app.post("/api/tree/deploy")
def api_tree_deploy():
    payload = request.get_json(silent=True) or {}
    tree = payload.get("tree")
    if not isinstance(tree, dict):
        tree = load_json(TREE_CONFIG_FILE, sample_tree())
    tree = normalize_tree_config(tree)

    save_json(TREE_CONFIG_FILE, tree)
    result = deploy_tree_config(tree)
    status = 200 if not result["errors"] else 400
    return result, status


@app.post("/api/view/dashboard/generate")
def api_view_dashboard_generate():
    payload = request.get_json(silent=True) or {}
    title = str(payload.get("title", "")).strip() or "Untitled view"
    view_id = str(payload.get("view_id", "")).strip() or "view"
    folder_path = payload.get("folder_path", [])
    if not isinstance(folder_path, list):
        folder_path = []
    folder_path = [safe_folder_title(str(item)) for item in folder_path if str(item).strip()]

    tag_list = payload.get("tags", [])
    if isinstance(tag_list, str):
        tag_list = [x.strip() for x in tag_list.split(",") if x.strip()]
    if not isinstance(tag_list, list):
        tag_list = []
    final_tags = sorted(set([str(x).strip() for x in tag_list if str(x).strip()]))

    process_tags = normalize_process_tags(payload.get("process_tags", []))
    if not process_tags:
        return {"error": "No process_tags provided. Select tags in sibling tags node first."}, 400

    normalized = normalize_tree_config(load_json(TREE_CONFIG_FILE, sample_tree()))
    root_folder = normalized.get("root_folder", "Historian")
    if folder_path and root_folder and folder_path[0] == root_folder:
        folder_path = folder_path[1:]
    folder_uid, folder_issues = ensure_folder_path(root_folder=root_folder, path_items=folder_path)
    if not folder_uid:
        return {"error": "Unable to resolve Grafana folder path", "warnings": folder_issues}, 400

    settings = grafana_settings()
    dashboard = build_auto_dashboard(
        title=title,
        tags=final_tags,
        process_tags=process_tags,
        datasource_uid=settings["datasource_uid"],
        datasource_type=settings["datasource_type"],
    )
    dashboard_uid = dashboard_uid_for_instance(path_items=folder_path, template_id="auto", title=f"{title}-{view_id}")
    dashboard["uid"] = dashboard_uid
    dashboard["title"] = title
    dashboard["tags"] = final_tags

    grafana_payload = {
        "dashboard": dashboard,
        "folderUid": folder_uid,
        "overwrite": True,
        "message": "Generated from view tags by grafana_view_wizard",
    }
    response = grafana_request("POST", "/api/dashboards/db", payload=grafana_payload)
    if response.status_code not in (200, 201):
        return {
            "error": f"Grafana dashboard push failed ({response.status_code})",
            "details": response.text,
            "warnings": folder_issues,
        }, 400

    body = response.json()
    uid = body.get("uid", dashboard_uid)
    url = body.get("url", "")
    direct_url = grafana_dashboard_direct_url(uid=uid, path_url=url)
    add_debug_event(
        "info",
        "Dashboard generated from view tags",
        {
            "view_id": view_id,
            "title": title,
            "uid": uid,
            "panel_count": len(dashboard.get("panels", [])),
            "folder_path": [root_folder] + folder_path,
        },
    )
    return {
        "generated": True,
        "uid": uid,
        "url": url,
        "direct_url": direct_url,
        "panel_count": len(dashboard.get("panels", [])),
        "folder_path": [root_folder] + folder_path,
        "warnings": folder_issues,
    }


@app.get("/api/grafana/open-url")
def api_grafana_open_url():
    uid = str(request.args.get("uid", "")).strip()
    if not uid:
        return {"error": "uid is required"}, 400
    return redirect(grafana_dashboard_direct_url(uid=uid), code=302)


@app.get("/api/grafana/embed-url")
def api_grafana_embed_url():
    uid = str(request.args.get("uid", "")).strip()
    if not uid:
        return {"error": "uid is required"}, 400
    url = grafana_dashboard_direct_url(uid=uid)
    join = "&" if "?" in url else "?"
    return redirect(f"{url}{join}theme=light", code=302)


@app.get("/api/influx/tags/catalog")
def api_influx_tags_catalog():
    now = datetime.now(timezone.utc)
    started = time.time()
    # Prefer Historian backend status endpoint because it already computes
    # saved value and saved age per selected tag.
    try:
        backend_resp = historian_backend_request("GET", "/api/tags/status")
        if backend_resp.status_code == 200:
            body = backend_resp.json()
            raw_items = body.get("items", [])
            items = []
            for point in raw_items:
                nodeid = str(point.get("nodeid", "")).strip()
                if not nodeid:
                    continue
                label = str(point.get("label", "")).strip() or nodeid
                saved_value = point.get("saved_value")
                rounded_value = saved_value
                if isinstance(saved_value, (int, float)):
                    rounded_value = round(float(saved_value), 2)
                age_seconds = point.get("saved_age_seconds")
                if isinstance(age_seconds, str):
                    try:
                        age_seconds = int(age_seconds)
                    except Exception:
                        age_seconds = None

                state = "unknown"
                if age_seconds is not None:
                    if age_seconds <= 5:
                        state = "live"
                    elif age_seconds <= 30:
                        state = "recent"
                    else:
                        state = "stale"

                items.append(
                    {
                        "nodeid": nodeid,
                        "label": label,
                        "value": rounded_value,
                        "raw_value": saved_value,
                        "last_time": str(point.get("saved_time", "")).strip(),
                        "age_seconds": age_seconds,
                        "state": state,
                        "live_value": point.get("live_value"),
                    }
                )

            items.sort(key=lambda item: (item["label"].lower(), item["nodeid"].lower()))
            counts = {"live": 0, "recent": 0, "cached": 0, "stale": 0, "unknown": 0}
            max_age_seconds = 0
            for item in items:
                state = str(item.get("state", "unknown"))
                counts[state] = counts.get(state, 0) + 1
                age = item.get("age_seconds")
                if isinstance(age, int):
                    max_age_seconds = max(max_age_seconds, age)
            meta = {
                "source": "historian_backend",
                "measurement": RAW_TAG_MEASUREMENT,
                "total": len(items),
                "counts": counts,
                "max_age_seconds": max_age_seconds,
                "query_ms": int((time.time() - started) * 1000),
            }
            add_debug_event("info", "Influx catalog refreshed", meta)
            return {"items": items, "meta": meta}
    except Exception as exc:
        add_debug_event("warn", "Historian backend tags/status unavailable for catalog", {"error": str(exc)})

    query = (
        f'SELECT LAST("value") AS value, LAST("from_cache") AS from_cache '
        f'FROM "{RAW_TAG_MEASUREMENT}" GROUP BY "nodeid","label"'
    )
    try:
        influx = get_influx_client()
        result = influx.query(query)
    except Exception as exc:
        add_debug_event("error", "Influx catalog query failed", {"error": str(exc)})
        return {"error": f"Influx query failed: {exc}"}, 500

    items = []
    # Fallback path for direct Influx query where tags are only available in series metadata.
    for series in result.raw.get("series", []):
        tags = series.get("tags", {}) or {}
        nodeid = str(tags.get("nodeid", "")).strip()
        if not nodeid:
            continue
        label = str(tags.get("label", "")).strip() or nodeid
        values = series.get("values", [])
        columns = series.get("columns", [])
        row = values[-1] if values else []
        row_map = {columns[i]: row[i] for i in range(min(len(columns), len(row)))}
        raw_value = row_map.get("value")
        rounded_value = raw_value
        if isinstance(raw_value, (int, float)):
            rounded_value = round(float(raw_value), 2)
        raw_time = str(row_map.get("time", "")).strip()
        age_seconds = None
        ts = parse_utc_time(raw_time)
        if ts is not None:
            age_seconds = int((now - ts).total_seconds())

        state = "unknown"
        if age_seconds is not None:
            if age_seconds <= 5:
                state = "live"
            elif age_seconds <= 30:
                state = "recent"
            else:
                state = "stale"
        if int(row_map.get("from_cache") or 0) == 1 and state != "stale":
            state = "cached"

        items.append(
            {
                "nodeid": nodeid,
                "label": label,
                "value": rounded_value,
                "raw_value": raw_value,
                "last_time": raw_time,
                "age_seconds": age_seconds,
                "state": state,
            }
        )

    items.sort(key=lambda item: (item["label"].lower(), item["nodeid"].lower()))
    counts = {"live": 0, "recent": 0, "cached": 0, "stale": 0, "unknown": 0}
    max_age_seconds = 0
    for item in items:
        state = str(item.get("state", "unknown"))
        counts[state] = counts.get(state, 0) + 1
        age = item.get("age_seconds")
        if isinstance(age, int):
            max_age_seconds = max(max_age_seconds, age)
    meta = {
        "source": "influx_fallback",
        "measurement": RAW_TAG_MEASUREMENT,
        "total": len(items),
        "counts": counts,
        "max_age_seconds": max_age_seconds,
        "query_ms": int((time.time() - started) * 1000),
    }
    add_debug_event("info", "Influx catalog refreshed", meta)
    return {"items": items, "meta": meta}


@app.get("/api/debug/events")
def api_debug_events():
    limit_raw = str(request.args.get("limit", "120")).strip()
    try:
        limit = max(1, min(500, int(limit_raw)))
    except Exception:
        limit = 120
    with debug_events_lock:
        events = list(debug_events[-limit:])
    return {"events": events}


@app.get("/api/opc/browse")
def api_opc_browse():
    nodeid = request.args.get("nodeid", "")
    try:
        with opc_lock:
            client = ensure_opc_client()
            node = client.nodes.objects if not nodeid else client.get_node(nodeid)
            children = []
            for child in node.get_children():
                try:
                    children.append(opc_tree_item(child))
                except Exception:
                    continue
        add_debug_event("info", "OPC browse", {"nodeid": nodeid or "root", "children": len(children)})
        return {"children": children}
    except Exception as exc:
        add_debug_event("error", "OPC browse failed", {"nodeid": nodeid, "error": str(exc)})
        return {"error": f"OPC browse failed: {exc}"}, 500


@app.get("/api/opc/read")
def api_opc_read():
    nodeid = str(request.args.get("nodeid", "")).strip()
    if not nodeid:
        return {"error": "nodeid is required"}, 400
    try:
        with opc_lock:
            client = ensure_opc_client()
            value = client.get_node(nodeid).read_value()
        rounded = round(float(value), 2) if isinstance(value, (int, float)) else value
        add_debug_event("info", "OPC read", {"nodeid": nodeid, "value": rounded})
        return {"nodeid": nodeid, "value": rounded, "raw_value": value}
    except Exception as exc:
        add_debug_event("error", "OPC read failed", {"nodeid": nodeid, "error": str(exc)})
        return {"error": f"OPC read failed: {exc}"}, 500


@app.post("/api/opc/read_many")
def api_opc_read_many():
    payload = request.get_json(silent=True) or {}
    nodeids = payload.get("nodeids", [])
    if not isinstance(nodeids, list):
        return {"error": "nodeids list is required"}, 400
    clean_nodeids = []
    for nodeid in nodeids:
        value = str(nodeid).strip()
        if value and value not in clean_nodeids:
            clean_nodeids.append(value)

    items = []
    try:
        with opc_lock:
            client = ensure_opc_client()
            for nodeid in clean_nodeids:
                try:
                    raw = client.get_node(nodeid).read_value()
                    rounded = round(float(raw), 2) if isinstance(raw, (int, float)) else raw
                    items.append({"nodeid": nodeid, "ok": True, "value": rounded, "raw_value": raw})
                except Exception as exc:
                    items.append({"nodeid": nodeid, "ok": False, "error": str(exc)})
    except Exception as exc:
        add_debug_event("error", "OPC read_many failed", {"error": str(exc), "count": len(clean_nodeids)})
        return {"error": f"OPC read_many failed: {exc}"}, 500

    ok_count = len([item for item in items if item.get("ok")])
    add_debug_event("info", "OPC read_many", {"count": len(clean_nodeids), "ok": ok_count})
    return {"items": items}


@app.get("/api/opc/selection")
def api_opc_selection():
    return load_selection()


@app.post("/api/opc/selection/add")
def api_opc_selection_add():
    payload = request.get_json(silent=True) or {}
    nodeid = str(payload.get("nodeid", "")).strip()
    label = str(payload.get("label", "")).strip() or nodeid
    if not nodeid:
        return {"error": "nodeid is required"}, 400

    backend_connected = False
    try:
        sel_resp = historian_backend_request("GET", "/api/selection")
        if sel_resp.status_code == 200:
            selection = sel_resp.json()
            tags = selection.get("tags", [])
            if not isinstance(tags, list):
                tags = []
            if not any(str(item.get("nodeid", "")).strip() == nodeid for item in tags if isinstance(item, dict)):
                tags.append({"nodeid": nodeid, "label": label})
            save_resp = historian_backend_request("POST", "/api/selection", {"tags": tags})
            if save_resp.status_code == 200:
                backend_connected = True
                add_debug_event(
                    "info",
                    "OPC tag added to historian backend selection",
                    {"nodeid": nodeid, "label": label, "count": len(tags)},
                )
                return {"saved": True, "count": len(tags), "backend_connected": True}
    except Exception:
        pass

    # Fallback if historian backend is not reachable.
    selection = load_selection()
    tags = selection.get("tags", [])
    if not isinstance(tags, list):
        tags = []
    if not any(str(item.get("nodeid", "")).strip() == nodeid for item in tags if isinstance(item, dict)):
        tags.append({"nodeid": nodeid, "label": label})
    save_selection({"tags": tags})
    add_debug_event(
        "warn",
        "OPC tag added to local selection fallback (backend disconnected)",
        {"nodeid": nodeid, "label": label, "count": len(tags)},
    )
    return {"saved": True, "count": len(tags), "backend_connected": backend_connected}


@app.get("/api/historian/raw/status")
def api_historian_raw_status():
    try:
        resp = historian_backend_request("GET", "/api/logging/status")
    except Exception as exc:
        add_debug_event("error", "Historian raw status failed", {"error": str(exc)})
        return {"backend_connected": False, "error": str(exc)}, 503
    if resp.status_code != 200:
        add_debug_event("error", "Historian raw status HTTP error", {"status": resp.status_code, "body": resp.text})
        return {"backend_connected": False, "error": resp.text}, 503
    body = resp.json()
    add_debug_event("info", "Historian raw status", {"running": bool(body.get("running", False))})
    return {"backend_connected": True, "running": bool(body.get("running", False))}


@app.post("/api/historian/raw/start")
def api_historian_raw_start():
    try:
        resp = historian_backend_request("POST", "/api/logging/start")
    except Exception as exc:
        add_debug_event("error", "Historian raw start failed", {"error": str(exc)})
        return {"backend_connected": False, "error": str(exc)}, 503
    if resp.status_code != 200:
        add_debug_event("error", "Historian raw start HTTP error", {"status": resp.status_code, "body": resp.text})
        return {"backend_connected": False, "error": resp.text}, 503
    body = resp.json()
    add_debug_event("info", "Historian raw start", {"message": body.get("message", "")})
    return {"backend_connected": True, "message": body.get("message", "Raw logging start requested.")}


@app.post("/api/historian/raw/stop")
def api_historian_raw_stop():
    try:
        resp = historian_backend_request("POST", "/api/logging/stop")
    except Exception as exc:
        add_debug_event("error", "Historian raw stop failed", {"error": str(exc)})
        return {"backend_connected": False, "error": str(exc)}, 503
    if resp.status_code != 200:
        add_debug_event("error", "Historian raw stop HTTP error", {"status": resp.status_code, "body": resp.text})
        return {"backend_connected": False, "error": resp.text}, 503
    body = resp.json()
    add_debug_event("info", "Historian raw stop", {"message": body.get("message", "")})
    return {"backend_connected": True, "message": body.get("message", "Raw logging stop requested.")}


@app.get("/api/historian/selection")
def api_historian_selection_get():
    try:
        resp = historian_backend_request("GET", "/api/selection")
    except Exception as exc:
        return {"backend_connected": False, "error": str(exc)}, 503
    if resp.status_code != 200:
        return {"backend_connected": False, "error": resp.text}, 503
    body = resp.json()
    tags = body.get("tags", [])
    if not isinstance(tags, list):
        tags = []
    return {"backend_connected": True, "tags": tags}


@app.post("/api/historian/selection/policy")
def api_historian_selection_policy():
    payload = request.get_json(silent=True) or {}
    nodeid = str(payload.get("nodeid", "")).strip()
    if not nodeid:
        return {"error": "nodeid is required"}, 400
    save_mode = str(payload.get("save_mode", "always")).strip().lower()
    if save_mode not in {"always", "on_change"}:
        save_mode = "always"
    try:
        deadband = float(payload.get("deadband", 0.0))
    except Exception:
        deadband = 0.0
    if deadband < 0:
        deadband = 0.0
    try:
        heartbeat_s = int(payload.get("heartbeat_s", 30))
    except Exception:
        heartbeat_s = 30
    if heartbeat_s < 1:
        heartbeat_s = 1

    try:
        sel_resp = historian_backend_request("GET", "/api/selection")
    except Exception as exc:
        return {"backend_connected": False, "error": str(exc)}, 503
    if sel_resp.status_code != 200:
        return {"backend_connected": False, "error": sel_resp.text}, 503
    selection = sel_resp.json()
    tags = selection.get("tags", [])
    if not isinstance(tags, list):
        tags = []

    updated = False
    for tag in tags:
        if not isinstance(tag, dict):
            continue
        if str(tag.get("nodeid", "")).strip() == nodeid:
            tag["save_mode"] = save_mode
            tag["deadband"] = deadband
            tag["heartbeat_s"] = heartbeat_s
            updated = True
            break
    if not updated:
        return {"error": f"nodeid not found in historian selection: {nodeid}"}, 404

    save_resp = historian_backend_request("POST", "/api/selection", {"tags": tags})
    if save_resp.status_code != 200:
        return {"backend_connected": False, "error": save_resp.text}, 503
    return {
        "backend_connected": True,
        "saved": True,
        "message": f"Saved policy for {nodeid}",
        "policy": {"save_mode": save_mode, "deadband": deadband, "heartbeat_s": heartbeat_s},
    }


if __name__ == "__main__":
    setup_logging()
    add_debug_event("info", "Wizard starting", {"port": WIZARD_PORT})
    load_env_file()
    app.run(host="0.0.0.0", port=WIZARD_PORT, debug=False)
